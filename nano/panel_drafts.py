"""Runs ON THE NANO (imported by panel_api.py). Unverified machine translations + the session log.

Drafts: when a language pair is chosen, every phrase with no approved translation in that
language is translated by the llama-server (Qwen) in a background thread. Results are cached in
~/panel/drafts.json and filed as golden_set rows with status 'draft', created_by 'qwen:<model>',
so they show up in `tools/translations.py list` for a speaker to approve or reject. The panel
shows them amber, marked unverified, and the Nano never speaks them (docs/project-brief.md §5).

Session log: one JSON line per turn in ~/panel/sessions/<session>.jsonl — the brief's §9 turn
contract (speaker, sourceLang, targetLang, sourceText, translatedText, …).
"""
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import asr_worker
import push

try:                                    # pip3 install --user ctranslate2 sentencepiece; model under ~/models
    import ctranslate2
    import sentencepiece as spm
except ImportError:
    ctranslate2 = spm = None

HERE = Path(__file__).resolve().parent
CACHE = HERE / "drafts.json"
SESSIONS = HERE / "sessions"
DSN = "postgresql://interpreter_app@localhost:5432/interpreter_data"
QWEN = "qwen2.5-3b-instruct"
NLLB = "nllb-200-distilled-600M"
NLLB_DIR = Path.home() / "models" / "nllb-600M-ct2-int8"
# NLLB-200 language codes for ours (docs/data-contract.md). Every language in the list is covered.
NLLB_CODES = {"en": "eng_Latn", "mi": "mri_Latn", "ar": "arb_Arab", "es": "spa_Latn", "fa": "pes_Arab", "prs": "prs_Arab",
              "hi": "hin_Deva", "vi": "vie_Latn", "zh": "zho_Hans", "yue": "yue_Hant", "ja": "jpn_Jpan", "ko": "kor_Hang",
              "lo": "lao_Laoo", "pa": "pan_Guru", "sm": "smo_Latn", "tl": "tgl_Latn", "to": "ton_Latn"}
_nllb = None
_nllb_lock = threading.Lock()


def nllb_available():
    return ctranslate2 is not None and (NLLB_DIR / "model.bin").exists()


def nllb_load():
    """Load NLLB once (≈3 s, ≈600 MB). panel_api calls this at start-up so the first patient of the day
    doesn't pay for it; every translate call goes through it too."""
    global _nllb
    if not nllb_available():
        return None
    with _nllb_lock:
        if _nllb is None:
            t0 = time.time()                 # the Orin Nano has 6 cores; leave none idle while a sentence decodes
            _nllb = (ctranslate2.Translator(str(NLLB_DIR), device="cpu", compute_type="int8", inter_threads=1, intra_threads=6),
                     spm.SentencePieceProcessor(model_file=str(NLLB_DIR / "sentencepiece.bpe.model")))
            print(time.strftime("%H:%M:%S"), f"nllb loaded in {time.time() - t0:.1f} s", file=sys.stderr, flush=True)
    return _nllb


def nllb_translate(text, src, tgt, beam=4):
    """text from our language code src to tgt with NLLB-200 (int8, CPU), or None. beam=4 for drafts
    nobody is waiting for (~2 s); the live speech path passes beam=2 (~1.3 s, near-identical output)."""
    if not nllb_available() or src not in NLLB_CODES or tgt not in NLLB_CODES or not text:
        return None
    tr, sp = nllb_load()
    # NLLB is a sentence model: given "Your baby's heart rate has dropped. This is an emergency." it
    # translated the first sentence and dropped the second (Māori, 2026-09-21 bench). One sentence per
    # row, all rows in one batched call (cheaper than one call each), joined back in order.
    sents = [x for x in re.split(r"(?<=[.!?。！？])\s+", text.strip()) if x]
    if not sents:
        return None
    with _nllb_lock:
        batch = [[NLLB_CODES[src]] + sp.encode(x, out_type=str) + ["</s>"] for x in sents]
        outs = tr.translate_batch(batch, target_prefix=[[NLLB_CODES[tgt]]] * len(batch), beam_size=beam, max_decoding_length=160)
    parts = []
    for x, o in zip(sents, outs):
        out_text = sp.decode([t for t in o.hypotheses[0] if t != NLLB_CODES[tgt]]).strip()
        # NLLB signals "could not translate" with ⁇ and/or by echoing the source — that is not a draft.
        # One failed sentence fails the whole text: a partial translation is worse than none here.
        if not out_text or "⁇" in out_text or out_text.lower().strip(" .!?") == x.lower().strip(" .!?"):
            return None
        parts.append(out_text)
    return " ".join(parts)

_lock = threading.Lock()
_cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
_pending = set()          # "key|version|lang" being translated right now
_worker = None


def _key(p, lang):
    return f"{p['key']}|{p['version']}|{lang}"


def get(p, lang):
    """The cached draft for phrase p in lang, or None. A Qwen draft counts as missing once NLLB is
    available, so the better engine replaces it on the next request."""
    e = _cache.get(_key(p, lang)) or {}
    if e.get("model") == QWEN and nllb_available():
        return None
    return e.get("text")


def pending():
    return len(_pending)


def request(phrases, lang, lang_label, llama_up):
    """Queue drafts for every phrase in `phrases` lacking an approved `lang` translation. Non-blocking."""
    global _worker
    if lang == "en" or not (llama_up or nllb_available()):
        return
    todo = [p for p in phrases if not p["translations"].get(lang) and not get(p, lang) and _key(p, lang) not in _pending]
    if not todo:
        return
    with _lock:
        for p in todo:
            _pending.add(_key(p, lang))
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run, args=(todo, lang, lang_label), daemon=True)
        _worker.start()
    else:
        threading.Thread(target=_run, args=(todo, lang, lang_label), daemon=True).start()


def _run(todo, lang, lang_label):
    q = push.sql_str
    for p in todo:
        try:
            text, model = nllb_translate(p["text"], "en", lang), NLLB
            if not text:
                text, model = asr_worker.translate(p["text"], lang_label), QWEN
        except Exception as e:                        # engine down mid-way: leave it for next time
            print("draft failed:", p["key"], lang, repr(e), flush=True)
            with _lock:
                _pending.discard(_key(p, lang))
            continue
        with _lock:
            _cache[_key(p, lang)] = {"text": text, "model": model, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            CACHE.write_text(json.dumps(_cache, indent=1, ensure_ascii=False))
            _pending.discard(_key(p, lang))
        try:                                          # file it for review; a duplicate is fine
            push.psql(DSN, f"""INSERT INTO golden_set (language_pair, source_text, approved_translation, category, phrase_key, phrase_version, status, created_by)
                VALUES ({q('en-' + lang)}, {q(p['text'])}, {q(text)}, {q(p['category'])}, {q(p['key'])}, {p['version']}, 'draft', {q(('nllb:' if model == NLLB else 'qwen:') + model)})
                ON CONFLICT (phrase_key, phrase_version, language_pair, approved_translation) DO NOTHING""")
        except subprocess.CalledProcessError as e:
            print("draft not filed:", (e.stderr or "")[-200:], flush=True)


# ---- session log -------------------------------------------------------------------------
_session = {"id": None, "src": None, "dst": None}


def start_session(src, dst):
    _session.update(id=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"_{src}-{dst}", src=src, dst=dst)
    log_turn("system", kind="session_start")
    return _session["id"]


def log_turn(speaker, **fields):
    """speaker: clinician | patient | system. fields: kind, key, sourceText, translatedText, how, …"""
    if not _session["id"]:
        start_session(fields.get("sourceLang", "en"), fields.get("targetLang", "?"))
    SESSIONS.mkdir(exist_ok=True)
    row = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session": _session["id"],
           "speaker": speaker, "sourceLang": _session["src"], "targetLang": _session["dst"], **fields}
    with (SESSIONS / f"{_session['id']}.jsonl").open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
