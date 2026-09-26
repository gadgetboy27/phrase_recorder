#!/usr/bin/env python3
"""How good is the Nano's translator, per language? Score NLLB-200 against human translations.

    tools/calibrate.py --fetch              # download the two corpora, once (~40 MB, not in the repo)
    tools/calibrate.py --lang mi            # one language (~6 min for 100 lines at both beams)
    tools/calibrate.py                      # every language the panel offers (resumable; ~1.5 h)
    tools/calibrate.py --corpus tico        # medical-domain lines instead of general (7 languages)
    tools/calibrate.py --report             # re-print the table from what is already cached

Why: we have 1,208 machine drafts and no way to know which languages to distrust. These corpora
were translated by professionals, so the model's distance from them is a per-language trust score
— it tells a reviewer which drafts to read first. It does NOT validate any individual draft, and
no score here makes a translation safe to speak to a patient.

Corpora (downloaded to ~/phrase-recordings/calibration/, never committed):
  flores  FLORES-200 devtest — 1,012 sentences, professional translators + independent review,
          covers 15 of our 16 target languages. General web text, so it measures the model, not our
          domain. Tongan is absent — `ton_Latn` is not one of FLORES/NLLB's 204 codes, which is why
          NLLB cannot translate it at all. CC-BY-SA 4.0. https://github.com/facebookresearch/flores
  tico    TICO-19 test set — medical: the first block is CMU's 1,600 clinician/patient sentences
          ("about how long have these symptoms been going on?"), translated by language service
          providers and post-edited by professionals familiar with the medical domain. CC0.
          Covers 7 of ours (ar es fa hi prs tl zh). https://tico-19.github.io/

Scoring is chrF++ (character n-grams 1-6 + word n-grams 1-2, beta=2, corpus level), the standard
for morphologically rich and low-resource languages — BLEU is too coarse for te reo. Also reported:
coverage, the share of lines NLLB translated at all (it returns nothing for Tongan).

Needs SSH to the Nano (NANO_SSH, as tools/nano.sh sets it). Stdlib only, both ends.
"""
import argparse
import json
import os
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = HERE.parent / "nano" / "calibrate_worker.py"
CACHE = Path.home() / "phrase-recordings" / "calibration"
FLORES_URL = "https://dl.fbaipublicfiles.com/nllb/flores200_dataset.tar.gz"
TICO_URL = "https://github.com/tico-19/tico-19.github.io/raw/master/data/tico19-testset.zip"
# our code -> TICO-19's code (FLORES uses the same codes as NLLB, so panel_drafts.NLLB_CODES serves)
TICO_LANG = {"ar": "ar", "es": "es-LA", "fa": "fa", "prs": "prs", "hi": "hi", "tl": "tl", "zh": "zh"}
# FLORES/NLLB codes, mirrored from nano/panel_drafts.py so this runs without importing the Nano's modules
NLLB_CODES = {"en": "eng_Latn", "mi": "mri_Latn", "ar": "arb_Arab", "es": "spa_Latn", "fa": "pes_Arab",
              "prs": "prs_Arab", "hi": "hin_Deva", "vi": "vie_Latn", "zh": "zho_Hans", "yue": "yue_Hant",
              "ja": "jpn_Jpan", "ko": "kor_Hang", "lo": "lao_Laoo", "pa": "pan_Guru", "sm": "smo_Latn",
              "tl": "tgl_Latn"}          # no ton_Latn: Tongan is not an NLLB-200 language (see panel_drafts.py)


# ---- chrF++ ---------------------------------------------------------------------------------
# sacreBLEU's defaults: char orders 1-6, word orders 1-2, beta 2, whitespace stripped from the
# character stream, statistics summed over the corpus and turned into one F-score at the end.

_PUNCTS = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def _ngrams(seq, n):
    return Counter(tuple(seq[i:i + n]) for i in range(len(seq) - n + 1))


def _words(sent):
    """chrF++'s word tokeniser: split on whitespace, then peel one leading or trailing punctuation
    mark off each word, so "me." is ["me", "."] and matches a reference that punctuates differently."""
    out = []
    for w in sent.split():
        if len(w) == 1:
            out.append(w)
        elif w[-1] in _PUNCTS:
            out += [w[:-1], w[-1]]
        elif w[0] in _PUNCTS:
            out += [w[0], w[1:]]
        else:
            out.append(w)
    return out


def chrf_stats(hyp, ref, char_order=6, word_order=2):
    """[(matches, hyp_total, ref_total)] per n-gram order, char orders then word orders."""
    out = []
    for h, r, order in (("".join(hyp.split()), "".join(ref.split()), char_order),
                        (_words(hyp), _words(ref), word_order)):
        for n in range(1, order + 1):
            hn, rn = _ngrams(h, n), _ngrams(r, n)
            # no reference n-grams at this order: the hypothesis' n-grams don't count either
            out.append((sum((hn & rn).values()), sum(hn.values()) if rn else 0, sum(rn.values())))
    return out


def chrf_score(pairs, beta=2.0):
    """chrF++ ×100 over (hypothesis, reference) pairs, corpus level — matches sacreBLEU 2.6.0's
    corpus_chrf(word_order=2) exactly (checked in tools/test_calibrate.py). A hypothesis of "" scores
    0 and drags the corpus score down: failing to translate a line is a failure, not a line to skip."""
    totals = None
    for hyp, ref in pairs:
        st = chrf_stats(hyp or "", ref)
        totals = st if totals is None else [(a + d, b + e, c + f) for (a, b, c), (d, e, f) in zip(totals, st)]
    if not totals:
        return 0.0
    # orders with no n-grams on either side are left out of the average, not counted as zero
    live = [(m, h, r) for m, h, r in totals if h > 0 and r > 0]
    if not live:
        return 0.0
    p = sum(m / h for m, h, _ in live) / len(live)
    r = sum(m / rr for m, _, rr in live) / len(live)
    if p + r == 0:
        return 0.0
    return 100 * (1 + beta ** 2) * p * r / (beta ** 2 * p + r)


# ---- corpora --------------------------------------------------------------------------------

def fetch():
    CACHE.mkdir(parents=True, exist_ok=True)
    tgz, zp = CACHE / "flores200_dataset.tar.gz", CACHE / "tico19-testset.zip"
    if not (CACHE / "flores200_dataset").is_dir():
        if not tgz.exists():
            print(f"downloading FLORES-200 (25 MB) …", flush=True)
            urllib.request.urlretrieve(FLORES_URL, tgz)
        print("extracting FLORES-200 …", flush=True)
        with tarfile.open(tgz) as t:                      # devtest only; the archive holds dev too
            members = [m for m in t.getmembers() if "/devtest/" in m.name or m.name.endswith("README")]
            t.extractall(CACHE, members=members)
    if not (CACHE / "tico19-testset").is_dir():
        if not zp.exists():
            print("downloading TICO-19 (15 MB) …", flush=True)
            urllib.request.urlretrieve(TICO_URL, zp)
        print("extracting TICO-19 …", flush=True)
        with zipfile.ZipFile(zp) as z:
            z.extractall(CACHE)
    f = len(list((CACHE / "flores200_dataset" / "devtest").glob("*.devtest")))
    t = len(list((CACHE / "tico19-testset" / "test").glob("*.tsv")))
    print(f"ready in {CACHE}: FLORES {f} languages, TICO-19 {t} language pairs")
    print("  FLORES-200 is CC-BY-SA 4.0; TICO-19 is CC0. Neither is committed to this repo.")


def load_pairs(corpus, lang, n):
    """[(english, human_translation)] for a language, or None when the corpus lacks it."""
    if corpus == "flores":
        d = CACHE / "flores200_dataset" / "devtest"
        src, tgt = d / "eng_Latn.devtest", d / f"{NLLB_CODES[lang]}.devtest"
        if not (src.exists() and tgt.exists()):
            return None
        e = src.read_text(encoding="utf8").splitlines()
        f = tgt.read_text(encoding="utf8").splitlines()
        return [(a.strip(), b.strip()) for a, b in zip(e, f) if a.strip() and b.strip()][:n]
    code = TICO_LANG.get(lang)
    if not code:
        return None
    p = CACHE / "tico19-testset" / "test" / f"test.en-{code}.tsv"
    if not p.exists():
        return None
    rows = []
    for line in p.read_text(encoding="utf8").splitlines()[1:]:      # header: sourceLang targetLang source target …
        c = line.split("\t")
        if len(c) >= 4 and c[2].strip() and c[3].strip():
            rows.append((c[2].strip(), c[3].strip()))
    return rows[:n]


# ---- running the model on the Nano ----------------------------------------------------------

def translate_on_nano(lang, pairs, beams, ssh):
    job = {"lang": lang, "beams": beams,
           "items": [{"id": i, "src": s} for i, (s, _) in enumerate(pairs)]}
    subprocess.run(["ssh", ssh, "mkdir -p ~/panel_calib"], check=True)
    subprocess.run(["scp", "-q", str(WORKER), str(WORKER.parent / "panel_drafts.py"),
                    str(WORKER.parent / "asr_worker.py"), str(HERE / "push.py"), f"{ssh}:panel_calib/"], check=True)
    r = subprocess.run(["ssh", ssh, "cd ~/panel_calib && python3 calibrate_worker.py"],
                       input=json.dumps(job), capture_output=True, text=True)
    sys.stderr.write(r.stderr)
    if r.returncode or not r.stdout.strip():
        raise RuntimeError(f"worker failed on {lang} (exit {r.returncode})")
    out = json.loads(r.stdout)
    if out.get("error"):
        raise RuntimeError(out["error"])
    return out


def band(score, coverage):
    """Triage bands, not clinical thresholds: they only decide what a human reads first. Coverage is
    called out separately because a good score on the half of the lines a model deigned to translate
    is not a good model — chrF++ already counts the blanks, but the reviewer needs to see why."""
    if coverage < 0.5:
        return "NO MT"
    base = ("usable draft" if score >= 50 else "review closely" if score >= 40
            else "weak" if score >= 30 else "do not trust")
    if coverage < 0.95:
        base += f" · {(1 - coverage) * 100:.0f}% blank"
    return base


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true", help="download and extract the corpora, then exit")
    ap.add_argument("--corpus", choices=["flores", "tico"], default="flores")
    ap.add_argument("--lang", help="one of our language codes; default every language NLLB knows")
    ap.add_argument("--n", type=int, default=100, help="lines per language (default 100)")
    ap.add_argument("--beams", default="4,2", help="beam widths to measure (default 4,2 — drafts and live)")
    ap.add_argument("--report", action="store_true", help="just re-print the table from cached translations")
    ap.add_argument("--force", action="store_true", help="re-translate even when a cached run exists")
    ap.add_argument("--ssh", default=os.environ.get("NANO_SSH", "gadgetboy@192.168.68.111"))
    a = ap.parse_args()

    if a.fetch:
        return fetch()
    if not (CACHE / "flores200_dataset").is_dir():
        sys.exit("corpora not downloaded yet — run: tools/calibrate.py --fetch")

    beams = [int(x) for x in a.beams.split(",") if x.strip()]
    langs = [a.lang] if a.lang else [c for c in NLLB_CODES if c != "en"]
    raw = CACHE / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    results = []
    todo = [l for l in langs if a.force or not (raw / f"{a.corpus}_{l}_{a.n}.json").exists()]
    if todo and not a.report:
        print(f"{len(todo)} language(s) to translate, {a.n} lines each at beam {'+'.join(map(str, beams))} "
              f"— roughly {len(todo) * a.n * 3.5 / 60:.0f} min on the Nano\n", flush=True)

    for lang in langs:
        pairs = load_pairs(a.corpus, lang, a.n)
        if not pairs:
            print(f"{lang}: not in {a.corpus}")
            continue
        cached = raw / f"{a.corpus}_{lang}_{a.n}.json"
        if cached.exists() and not a.force:
            out = json.loads(cached.read_text())
        elif a.report:
            continue
        else:
            print(f"{lang}: {len(pairs)} lines → Nano", flush=True)
            t0 = time.time()
            try:
                out = translate_on_nano(lang, pairs, beams, a.ssh)
            except Exception as e:
                print(f"  {lang}: {e}")
                continue
            out["corpus"], out["n"] = a.corpus, len(pairs)
            cached.write_text(json.dumps(out, ensure_ascii=False))
            print(f"  {lang}: done in {time.time() - t0:.0f} s", flush=True)
        refs = [r for _, r in pairs]
        row = {"lang": lang, "corpus": out.get("corpus", a.corpus), "n": len(refs)}
        for beam in beams:
            k = f"b{beam}"
            hyps = [r.get(k) for r in out["rows"]][:len(refs)]
            row[f"chrf{beam}"] = chrf_score(list(zip(hyps, refs)))
            row[f"cov{beam}"] = sum(1 for h in hyps if h) / len(hyps) if hyps else 0.0
        results.append(row)

    if not results:
        return
    main_beam = beams[0]
    results.sort(key=lambda r: -r[f"chrf{main_beam}"])
    w = max(len(f"chrf++ b{b}") for b in beams)
    head = f"{'lang':5} {'n':>4}  " + "  ".join(f"{'chrf++ b' + str(b):>{w}}" for b in beams) + f"  {'covered':>8}  verdict"
    print(f"\n{a.corpus} — NLLB-200-600M vs professional human translations\n{head}\n{'-' * len(head)}")
    for r in results:
        cells = "  ".join(f"{r[f'chrf{b}']:>{w}.1f}" for b in beams)
        print(f"{r['lang']:5} {r['n']:>4}  {cells}  {r[f'cov{main_beam}'] * 100:>7.0f}%  "
              f"{band(r[f'chrf{main_beam}'], r[f'cov{main_beam}'])}")
    if len(beams) > 1:
        d = [r[f"chrf{beams[0]}"] - r[f"chrf{beams[1]}"] for r in results]
        print(f"\nbeam {beams[0]} vs {beams[1]}: mean chrF++ difference {sum(d) / len(d):+.2f} "
              f"(positive = the wider beam is better; the live path uses beam {beams[-1]})")
    out = CACHE / f"report_{a.corpus}_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    out.write_text(json.dumps({"corpus": a.corpus, "n": a.n, "beams": beams, "rows": results}, indent=1))
    print(f"report: {out}")
    print("Bands are triage only — they say which drafts a human should read first, never that one is safe to speak.")


if __name__ == "__main__":
    main()
