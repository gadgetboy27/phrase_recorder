#!/usr/bin/env python3
"""Checks tools/calibrate.py's chrF++ against values taken from sacreBLEU 2.6.0.

    tools/test_calibrate.py

The expected numbers below were produced by `sacrebleu.corpus_chrf(hyps, [refs], word_order=2)`
on 2026-09-27 and are frozen here so the check needs nothing installed. To re-derive them:

    pip install sacrebleu
    python3 -c "import sacrebleu; print(sacrebleu.corpus_chrf(HYPS, [REFS], word_order=2).score)"

Exit 1 on any mismatch. Stdlib only.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from calibrate import chrf_score, _words, load_pairs  # noqa: E402

CASES = [
    # (hypotheses, references, sacreBLEU chrF++ score)
    (["Please stay as still as possible for me.", "Your baby's heart rate has dropped."],
     ["Please stay as still as you can for me.", "Your baby's heartbeat has dropped."], 73.3883),
    (["Kua heke te pānga o te ngakau o tō tamaiti.", "Tena koa noho tonu kia taea ai e au."],
     ["Kua heke te pao o te ngākau o tō pēpi.", "Tēnā koa, noho puku mai."], 39.5856),
    (["आपके बच्चे की हृदय गति कम हो गई है।"], ["आपके बच्चे की दिल की धड़कन गिर गई है।"], 44.4867),
    (["", "totally different words here"], ["a reference line", "another reference line"], 12.2946),
    (["the cat sat on the mat"], ["the cat sat on the mat"], 100.0),
]
fails = []


def check(name, got, want, tol=0.01):
    if abs(got - want) > tol:
        fails.append(f"{name}: got {got!r}, expected {want!r}")
        print(f"  FAIL {fails[-1]}")
    else:
        print(f"  ok   {name}: {got:.4f}")


print("chrF++ vs sacreBLEU 2.6.0:")
for i, (hyps, refs, want) in enumerate(CASES):
    check(f"case {i}", chrf_score(list(zip(hyps, refs))), want)

print("word tokeniser (chrF++ peels one leading/trailing punctuation mark):")
for sent, want in [("me.", ["me", "."]), ("(hi)", ["(hi", ")"]), ("a", ["a"]),
                   (".", ["."]), ("plain words here", ["plain", "words", "here"])]:
    got = _words(sent)
    if got != want:
        fails.append(f"_words({sent!r}): got {got}, expected {want}")
        print(f"  FAIL {fails[-1]}")
    else:
        print(f"  ok   {sent!r} -> {got}")

print("an untranslated line must cost score, not be skipped:")
both = chrf_score([("le chat", "le chat"), ("le chien", "le chien")])
one = chrf_score([("le chat", "le chat"), (None, "le chien")])
if not one < both:
    fails.append(f"a None hypothesis did not lower the score ({one} vs {both})")
    print(f"  FAIL {fails[-1]}")
else:
    print(f"  ok   {both:.1f} with both translated, {one:.1f} with one missing")

print("corpora on disk:")
for corpus, lang in (("flores", "mi"), ("flores", "sm"), ("tico", "fa")):
    pairs = load_pairs(corpus, lang, 5)
    if pairs is None:
        print(f"  skip {corpus}/{lang}: not downloaded (tools/calibrate.py --fetch)")
    elif len(pairs) != 5 or not all(a and b for a, b in pairs):
        fails.append(f"{corpus}/{lang}: bad pairs {pairs[:1]}")
        print(f"  FAIL {fails[-1]}")
    else:
        print(f"  ok   {corpus}/{lang}: {pairs[0][0][:40]!r} -> {pairs[0][1][:40]!r}")

print("Tongan must not reach NLLB (ton_Latn is not an NLLB-200 language):")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nano"))
import re as _re
_codes = _re.search(r"NLLB_CODES = \{(.*?)\}", (Path(__file__).resolve().parent.parent / "nano" / "panel_drafts.py").read_text(), _re.S).group(1)
if '"to"' in _codes or "ton_Latn" in _codes:
    fails.append("panel_drafts.NLLB_CODES still maps Tongan — it would be sent an unknown target token")
    print(f"  FAIL {fails[-1]}")
else:
    print("  ok   panel_drafts.NLLB_CODES has no Tongan entry")

print(f"\n{'OK' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
