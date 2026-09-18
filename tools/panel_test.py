#!/usr/bin/env python3
"""Regression tests for the touch panel + Nano API. Run from the Mac; nothing is flashed.

    tools/panel_test.py                 # everything (glyphs, API, panel liveness)
    tools/panel_test.py glyphs          # static: can the panel's fonts draw every string it may show?
    tools/panel_test.py api             # the Nano API answers correctly for every language
    tools/panel_test.py panel [secs]    # the panel stays up (API port) for N seconds, no crash report

Exit 1 on any failure; each failure names the language / phrase / character. Stdlib only.
The glyph test reads nano/panel.yaml and asks the Nano for the texts (approved + drafts) so it
catches "boxes on the panel" before a build — the class of bug that bit us with Spanish accents.
"""
import json
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NANO = "http://192.168.68.111:8765"
PANEL = "192.168.68.113"
LATIN = {"en", "mi", "es", "vi", "tl", "sm", "to"}
ARABIC = {"ar", "fa", "prs"}
IMAGE = {"hi", "pa", "lo", "zh", "yue", "ja", "ko"}     # rendered by the Nano — no panel font needed
NO_MT = {"to"}                                          # NLLB-600M cannot translate these: human translations only

fails = []


def fail(msg):
    fails.append(msg)
    print("  FAIL", msg)


def get(path, timeout=30):
    with urllib.request.urlopen(NANO + path, timeout=timeout) as r:
        return json.loads(r.read())


def post(path, timeout=30):
    req = urllib.request.Request(NANO + path, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---- 1. glyph coverage ------------------------------------------------------------------------
def font_glyphs(yaml_text):
    """{font_id: set(codepoints)} from nano/panel.yaml (explicit glyphs + named glyphsets)."""
    try:
        import esphome_glyphsets as gs      # comes with esphome; skip glyphsets if absent
    except ImportError:
        gs = None
    fonts, anchors = {}, {}
    for block in re.split(r"\n  - file: ", yaml_text)[1:]:
        fid = re.search(r"\n\s+id: (\w+)", block)
        if not fid:
            continue
        cps = set()
        m = re.search(r'glyphs: (?:&(\w+) )?"(.*?)"(?:\n|$)', block, flags=re.S)
        if m:
            cps |= {ord(c) for c in m.group(2)}
            if m.group(1):
                anchors[m.group(1)] = {ord(c) for c in m.group(2)}
        m = re.search(r"glyphs: \*(\w+)", block)
        if m:
            cps |= anchors.get(m.group(1), set())
        m = re.search(r"glyphsets: \[([^\]]*)\]", block)
        if m and gs:
            for name in re.findall(r"\w+", m.group(1)):
                cps |= set(gs.unicodes_per_glyphset(name))
        fonts[fid.group(1)] = cps
    return fonts


def test_glyphs():
    print("glyphs:")
    fonts = font_glyphs((REPO / "nano" / "panel.yaml").read_text())
    latin = fonts.get("ui_22", set())
    arabic = fonts.get("ar_22", set())
    if not latin or not arabic:
        return fail("could not read font glyphs from nano/panel.yaml")
    langs = [l["code"] for l in get("/phrases?lang=en&src=en")["languages"]]
    checked = 0
    for lang in langs:
        if lang in IMAGE:
            continue
        d = get(f"/phrases?lang={lang}&src=en")
        for p in d["phrases"]:
            for field in ("text", "translation", "draft"):
                t = p.get(field)
                if not t:
                    continue
                want = latin if lang in LATIN or field == "text" else (latin | arabic)
                missing = sorted({c for c in t if ord(c) > 31 and ord(c) not in want and c not in "\n"})
                checked += 1
                if missing:
                    fail(f"{lang} {p['key']} {field}: no glyph for {' '.join(missing)} (U+{' U+'.join(f'{ord(c):04X}' for c in missing)}) in {t[:40]!r}")
    print(f"  {checked} strings checked against the panel fonts")


# ---- 2. API contract --------------------------------------------------------------------------
def test_api():
    print("api:")
    h = get("/health")
    for k in ("audio", "piper", "whisper", "db"):
        if not h.get(k):
            fail(f"health: {k} is {h.get(k)}")
    if not h.get("translator"):
        fail("health: no translator (NLLB missing and llama-server down)")
    langs = get("/phrases?lang=en&src=en")["languages"]
    for l in langs:
        code = l["code"]
        d = get(f"/phrases?lang={code}&src=en")
        if len(d["phrases"]) < 20:
            fail(f"{code}: only {len(d['phrases'])} phrases")
        replies = [p for p in d["phrases"] if p["category"] == "patient_replies"]
        if len(replies) != 7:
            fail(f"{code}: {len(replies)} preset replies, expected 7")
        if code != "en":
            texts = [p.get("translation") or p.get("draft") for p in d["phrases"]]
            n = sum(1 for t in texts if t)
            if n < len(texts) - 2:
                (print if code in NO_MT else fail)(f"  note {code}: {len(texts) - n} phrases have neither translation nor draft (no machine translation for this language)" if code in NO_MT else f"{code}: {len(texts) - n} phrases have neither translation nor draft")
        r = post(f"/panel/p003?lang={code}&src=en")
        if "say" not in r or "how" not in r:
            fail(f"{code}: speak reply malformed: {r}")
        elif code != "en" and r["how"] == "piper" and r["lang"] == "en":
            print(f"  note {code}: spoken in English ({r.get('note')})")
        time.sleep(0.3)
    r = post("/panel/reply/p013?lang=hi&src=en")
    if not r.get("say"):
        fail(f"reply endpoint: {r}")
    q = urllib.parse.quote("कृपया जितना हो सके स्थिर रहें")
    with urllib.request.urlopen(f"{NANO}/render?lang=hi&size=28&w=760&text={q}", timeout=30) as resp:
        if resp.headers.get("Content-Type") != "image/png" or len(resp.read()) < 500:
            fail("render endpoint did not return a PNG")
    print(f"  {len(langs)} languages exercised")


# ---- 3. panel liveness ------------------------------------------------------------------------
def test_panel(secs=120):
    print(f"panel: watching {PANEL} for {secs}s")
    for _ in range(30):                      # give a just-flashed panel up to 60 s to come back first
        s = socket.socket(); s.settimeout(2)
        try:
            s.connect((PANEL, 6053)); s.close(); break
        except OSError:
            s.close(); time.sleep(2)
    ok = bad = 0
    end = time.time() + secs
    while time.time() < end:
        s = socket.socket(); s.settimeout(2)
        try:
            s.connect((PANEL, 6053)); ok += 1
        except OSError:
            bad += 1
        finally:
            s.close()
        time.sleep(5)
    if bad:
        fail(f"panel API port unreachable {bad}/{ok + bad} checks (reboot loop or safe mode?)")
    else:
        print(f"  API port up {ok}/{ok} checks")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "glyphs"):
        test_glyphs()
    if what in ("all", "api"):
        test_api()
    if what in ("all", "panel"):
        test_panel(int(sys.argv[2]) if len(sys.argv) > 2 else 120)
    print(f"\n{'OK' if not fails else str(len(fails)) + ' FAILED'}")
    sys.exit(1 if fails else 0)
