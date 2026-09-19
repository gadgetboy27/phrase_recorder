"""Runs ON THE NANO (imported by panel_api.py). The panel's "Add phrase" page.

- add_phrase():   an English phrase spoken into the panel → a `phrases` row (next free key, the same
                  INSERT as tools/add_phrase.py) and, when the take is given, that recording filed as
                  an English take of the new phrase (panel_takes, so it is a normal batch).
- add_language(): one of CANDIDATES → a `languages` row (status needs_testing). The Noto font its
                  script needs is fetched into ~/panel/fonts first (internet needed once).
- refresh():      rebuild ~/panel/phrases.json from Postgres with tools/export_phrases.py's query so
                  the API serves the change at once. The Mac's public/phrases.json is refreshed by
                  tools/nano.sh panel (export before copy) — Postgres stays the source of truth.

    python3 panel_admin.py fonts     # fetch every font the language table needs (tools/nano.sh fonts)
"""
import calendar
import re
import sys
import time
import urllib.request
from pathlib import Path

import export_phrases    # tools/export_phrases.py, deployed alongside
import panel_takes
import push              # tools/push.py: psql, sql_str

DSN = panel_takes.DSN
FONTS = Path.home() / "panel" / "fonts"
CJK = "system"           # NotoSansCJK from the fonts-noto-cjk package, not ~/panel/fonts
NOTO = "https://github.com/notofonts/notofonts.github.io/raw/main/fonts/{fam}/hinted/ttf/{fam}-Regular.ttf"
q = push.sql_str

# Every language the kit knows or could take on. Whisper hears all of them except the ones in
# asr_worker.WHISPER_UNSUPPORTED; NLLB-200 translates all of them (the nllb code).
# script: "latin"  — the panel draws it with Inter (ASCII + Vietnamese + macrons + ʻ),
#         "arabic" — the panel draws Arabic glyphs shaped by the Nano,
#         "image"  — the Nano renders PNGs with the named Noto font (/render).
#                              label            nllb         script    font
LANGUAGES = {
    "en":  ("English",         "eng_Latn",  "latin",  None),
    "mi":  ("Te Reo Māori",    "mri_Latn",  "latin",  None),
    "es":  ("Spanish",         "spa_Latn",  "latin",  None),
    "vi":  ("Vietnamese",      "vie_Latn",  "latin",  None),
    "sm":  ("Samoan",          "smo_Latn",  "latin",  None),
    "to":  ("Tongan",          "ton_Latn",  "latin",  None),
    "tl":  ("Tagalog",         "tgl_Latn",  "latin",  None),
    "ar":  ("Arabic",          "arb_Arab",  "arabic", "NotoSansArabic.ttf"),
    "fa":  ("Farsi",           "pes_Arab",  "arabic", "NotoSansArabic.ttf"),
    "prs": ("Dari",            "prs_Arab",  "arabic", "NotoSansArabic.ttf"),
    "hi":  ("Hindi",           "hin_Deva",  "image",  "NotoSansDevanagari.ttf"),
    "pa":  ("Punjabi",         "pan_Guru",  "image",  "NotoSansGurmukhi.ttf"),
    "lo":  ("Lao",             "lao_Laoo",  "image",  "NotoSansLao.ttf"),
    "zh":  ("Mandarin",        "zho_Hans",  "image",  CJK),
    "yue": ("Cantonese",       "yue_Hant",  "image",  CJK),
    "ja":  ("Japanese",        "jpn_Jpan",  "image",  CJK),
    "ko":  ("Korean",          "kor_Hang",  "image",  CJK),
    # ---- not in the kit yet: what "Add a language" offers ----
    "fr":  ("French",          "fra_Latn",  "latin",  None),
    "de":  ("German",          "deu_Latn",  "latin",  None),
    "it":  ("Italian",         "ita_Latn",  "latin",  None),
    "pt":  ("Portuguese",      "por_Latn",  "latin",  None),
    "nl":  ("Dutch",           "nld_Latn",  "latin",  None),
    "id":  ("Indonesian",      "ind_Latn",  "latin",  None),
    "ms":  ("Malay",           "zsm_Latn",  "latin",  None),
    "sw":  ("Swahili",         "swh_Latn",  "latin",  None),
    "so":  ("Somali",          "som_Latn",  "latin",  None),
    "fj":  ("Fijian",          "fij_Latn",  "latin",  None),
    "tr":  ("Turkish",         "tur_Latn",  "image",  "NotoSans.ttf"),         # ş ğ ı are outside Inter's set
    "pl":  ("Polish",          "pol_Latn",  "image",  "NotoSans.ttf"),
    "ru":  ("Russian",         "rus_Cyrl",  "image",  "NotoSans.ttf"),         # Noto Sans covers Cyrillic + Greek
    "uk":  ("Ukrainian",       "ukr_Cyrl",  "image",  "NotoSans.ttf"),
    "el":  ("Greek",           "ell_Grek",  "image",  "NotoSans.ttf"),
    "he":  ("Hebrew",          "heb_Hebr",  "image",  "NotoSansHebrew.ttf"),
    "ur":  ("Urdu",            "urd_Arab",  "image",  "NotoSansArabic.ttf"),   # letters beyond the panel's Arabic set
    "ps":  ("Pashto",          "pbt_Arab",  "image",  "NotoSansArabic.ttf"),
    "bn":  ("Bengali",         "ben_Beng",  "image",  "NotoSansBengali.ttf"),
    "ta":  ("Tamil",           "tam_Taml",  "image",  "NotoSansTamil.ttf"),
    "te":  ("Telugu",          "tel_Telu",  "image",  "NotoSansTelugu.ttf"),
    "ml":  ("Malayalam",       "mal_Mlym",  "image",  "NotoSansMalayalam.ttf"),
    "gu":  ("Gujarati",        "guj_Gujr",  "image",  "NotoSansGujarati.ttf"),
    "mr":  ("Marathi",         "mar_Deva",  "image",  "NotoSansDevanagari.ttf"),
    "ne":  ("Nepali",          "npi_Deva",  "image",  "NotoSansDevanagari.ttf"),
    "si":  ("Sinhala",         "sin_Sinh",  "image",  "NotoSansSinhala.ttf"),
    "th":  ("Thai",            "tha_Thai",  "image",  "NotoSansThai.ttf"),
    "km":  ("Khmer",           "khm_Khmr",  "image",  "NotoSansKhmer.ttf"),
    "my":  ("Burmese",         "mya_Mymr",  "image",  "NotoSansMyanmar.ttf"),
    "am":  ("Amharic",         "amh_Ethi",  "image",  "NotoSansEthiopic.ttf"),
}
RTL = {"ar", "fa", "prs", "ur", "ps", "he"}
NLLB_CODES = {code: v[1] for code, v in LANGUAGES.items()}


def script(code):
    return LANGUAGES[code][2] if code in LANGUAGES else "latin"


def font_path(code):
    """Path of the Noto file a language renders with, or None (Latin: the panel draws it; CJK: system)."""
    f = LANGUAGES.get(code, (None, None, None, None))[3]
    return FONTS / f if f and f != CJK else None


def font_ok(code):
    p = font_path(code)
    return p is None or p.exists()


def fetch_font(code):
    """Download the language's Noto font into ~/panel/fonts (needs internet). True if present after."""
    p = font_path(code)
    if p is None or p.exists():
        return True
    FONTS.mkdir(parents=True, exist_ok=True)
    fam = p.stem
    try:
        with urllib.request.urlopen(NOTO.format(fam=fam), timeout=30) as r:
            data = r.read()
        if len(data) < 10_000:
            return False
        p.write_bytes(data)
        return True
    except Exception as e:                               # no internet at a demo: say so, don't crash
        print("font fetch failed:", fam, repr(e), file=sys.stderr, flush=True)
        return False


def installed():
    """{code: label} of the languages table."""
    out = push.psql(DSN, "SELECT code || '|' || label FROM languages ORDER BY code").strip()
    return dict(line.split("|", 1) for line in out.split("\n") if line)


def available():
    """Candidates for "Add a language": known to the stack, not in the table yet."""
    have = installed()
    return [{"code": c, "label": v[0], "font": font_ok(c)} for c, v in LANGUAGES.items() if c not in have]


def add_language(code, phrases_path):
    if code not in LANGUAGES:
        return 400, {"say": f"unknown language code {code!r}"}
    if code in installed():
        return 409, {"say": f"{LANGUAGES[code][0]} is already in the list"}
    if not fetch_font(code):
        return 503, {"say": f"{LANGUAGES[code][0]} needs {font_path(code).name} on the Nano — connect to the internet "
                            "and try again, or run tools/nano.sh fonts"}
    push.psql(DSN, f"INSERT INTO languages (code, label, status) VALUES ({q(code)}, {q(LANGUAGES[code][0])}, 'needs_testing')")
    refresh(phrases_path)
    return 200, {"say": f"{LANGUAGES[code][0]} added — it is on the language screens now", "code": code}


def add_phrase(text, category, phrases_path, speaker=None, take=None):
    """INSERT the phrase (tools/add_phrase.py's rules), file the take if given, refresh the JSON.
    Returns (http status, reply dict)."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) < 3:
        return 400, {"say": "That is too short for a phrase"}
    if len(text) > 200:
        return 400, {"say": "Keep a phrase under 200 characters"}
    cats = push.psql(DSN, "SELECT value FROM phrase_categories").split()
    if category not in cats:
        return 400, {"say": f"unknown category {category!r}"}
    dup = push.psql(DSN, f"SELECT phrase_key||'v'||version FROM phrases WHERE status='active' AND lower(canonical_text) = lower({q(text)})").strip()
    if dup:
        return 409, {"say": f"Already in the list as {dup}", "key": dup.split("v")[0]}
    last = push.psql(DSN, r"SELECT coalesce(max(substring(phrase_key from '^p(\d+)$')::int), 0) FROM phrases").strip()
    key = f"p{int(last) + 1:03d}"
    push.psql(DSN, f"INSERT INTO phrases (phrase_key, version, category, canonical_text) VALUES ({q(key)}, 1, {q(category)}, {q(text)})")
    out = {"say": f"Saved as {key}: {text}", "key": key}
    if take and speaker and Path(take).exists():
        m = re.match(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z", Path(take).name)
        started = calendar.timegm(time.strptime(m.group(0), "%Y%m%dT%H%M%SZ")) * 1000 if m else int(time.time() * 1000)
        phrase = {"key": key, "version": 1, "category": category, "text": text}
        try:
            batch_id, rec = panel_takes.file_take(Path(take), started, "en", speaker, phrase, text, None)
            out["file"], out["batch"] = rec["file"], batch_id
            out["say"] += f" — your recording is take {rec['take']}"
        except Exception as e:                           # the phrase is in; a lost take is not worth a 500
            print("add_phrase: take not filed:", repr(e), file=sys.stderr, flush=True)
            out["say"] += " (recording not filed — see panel.log)"
    refresh(phrases_path)
    return 200, out


def refresh(phrases_path):
    """~/panel/phrases.json ← Postgres, byte-for-byte what tools/export_phrases.py writes on the Mac."""
    Path(phrases_path).write_text(export_phrases.render(export_phrases.fetch(DSN)))


if __name__ == "__main__":
    if sys.argv[1:] == ["fonts"]:
        for c in LANGUAGES:
            p = font_path(c)
            if p is not None:
                print(f"{p.name:28s} {'ok' if fetch_font(c) else 'FAILED'}")
    else:
        print(__doc__)
