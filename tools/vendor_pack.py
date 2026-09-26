#!/usr/bin/env python3
"""Build the pack to send a translation vendor for a quote: brief + source CSVs + machine baseline.

    tools/vendor_pack.py                     # writes ~/Downloads/phrasekit-translation-<date>.zip
    tools/vendor_pack.py --out ~/Desktop     # somewhere else
    tools/vendor_pack.py --keep              # also leave the unzipped folder next to the zip

Reads public/phrases.json, so it always matches what is deployed — regenerate after adding phrases.

What goes in the zip:
  README.txt                     the brief: context, quality bar, what to return, questions to answer
  tier1-patient-replies.csv      the 14 phrases a patient taps to answer. Highest priority.
  tier2-all-phrases.csv          all 72, tier 1 included and marked. For the Option B quote.
  machine-baseline/<lang>.csv    our unverified NLLB drafts, one file per language, for reference only
  LICENCE-AND-USE.txt            what we may do with the delivered translations

The CSVs have empty `translation` and `back_translation` columns for the vendor to fill. Back-translation
is the acceptance check: we read the English that comes back and see whether it still means the phrase.
Nothing here contains patient data — these are fixed clinical phrases, so no DPA is needed to quote.
Stdlib only.
"""
import argparse
import csv
import io
import json
import shutil
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PHRASES = HERE.parent / "public" / "phrases.json"
TIER1 = "patient_replies"
# Languages where we know the machine gave us nothing usable, so the human is the only source.
NO_MACHINE = {"to": "NLLB-200 has no Tongan at all — every line must be human-translated from scratch.",
              "mi": "Machine output is weak and macrons are unreliable; treat any baseline as noise.",
              "sm": "Machine output is weak; treat any baseline as noise."}
# chrF++ against professional medical translations (TICO-19, 100 lines, 2026-09-27). Context for the
# vendor on where we already know the machine baseline is poor, and for us on where review matters most.
CALIBRATION = {"hi": 59.4, "es": 59.4, "tl": 55.7, "ar": 42.3, "prs": 38.5, "fa": 31.1}

BRIEF = """PhraseKit — request for quotation: medical phrase translation
Generated {date} from the deployed phrase list.

WHAT THIS IS
------------
PhraseKit is an offline translation device used at the bedside when no human interpreter is
available — primarily in maternity and obstetric emergencies. A clinician taps a phrase; the
device shows and speaks it in the patient's language. The patient taps a reply from a short
fixed list. Everything runs on a small computer in the room; nothing goes to the internet.

We need these fixed phrases translated by professional human translators. We currently have
machine translations (NLLB-200) which we do NOT consider safe to use unreviewed, and for some
languages they are unusable or absent entirely.

WHAT WE ARE ASKING YOU TO QUOTE
-------------------------------
Option A (priority): tier1-patient-replies.csv
    {t1_phrases} phrases, {t1_words} words per language, into {n_langs} languages.
    These are what a patient taps to answer. They matter most and are the shortest.

Option B: tier2-all-phrases.csv
    {t2_phrases} phrases, {t2_words} words per language, into the same {n_langs} languages
    (tier 1 included, marked in the `tier` column).

Please price both, per language, so we can stage the work. Also tell us your minimum
charge per language — for Option A the volume is small and we expect minimums to dominate.

LANGUAGES
---------
{lang_block}

QUALITY BAR
-----------
1. Native speakers of the target language, currently living in or closely connected to the
   speech community. For Te Reo Maori, Samoan and Tongan we would prefer translators based in
   Aotearoa New Zealand or the Pacific.
2. Familiar with clinical language. Several phrases are obstetric ("Have your waters broken?",
   "You will be awake but numb from the waist down.").
3. Register: what a midwife would actually say to a frightened patient. Plain, calm, direct.
   Not formal, not literary, not a literal word-for-word rendering of the English.
4. These are SPOKEN aloud by a synthetic voice and read on a screen. Keep them short and
   natural to say. Avoid abbreviations, numerals where a word is more natural, and anything
   that depends on punctuation to be understood.
5. Where a language has politeness levels or gendered forms, choose what is appropriate for a
   clinician speaking to an adult patient they do not know, and tell us what you chose.
6. If a phrase does not work in the target culture, say so in the `notes` column and propose
   what a clinician there would say instead. We would rather change the English than ship a
   translation that lands wrong.

WHAT TO RETURN
--------------
The same CSV files, UTF-8 encoded, with these columns filled:
    translation       the target-language text, exactly as it should be displayed and spoken
    back_translation  a literal translation of YOUR translation back into English, by a second
                      translator who has not seen our English. This is our acceptance check.
    notes             anything we should know: choices made, phrases that do not transfer,
                      terms you would like us to confirm
Please do not reorder or delete rows, and keep the `key` column untouched — it is how the
text is matched back to the device.

WHAT WE WILL DO WITH IT
-----------------------
The translations go into the device and are shown and spoken to patients. We will credit
translators if you would like us to; tell us how you would like to be named. We will come back
to you for corrections if clinicians or patients report a problem with a line.

MACHINE BASELINE (reference only)
---------------------------------
machine-baseline/ holds our current unverified machine translations, one CSV per language.
They are provided only so you can see what we have now. Please do not post-edit them — we are
buying independent human translation, and we would rather have a blank than a corrected guess.
Where we have measured the machine against professional medical translations, the score is in
the language list above; low scores are why we are commissioning this.

QUESTIONS WE NEED ANSWERED WITH THE QUOTE
------------------------------------------
1. Price per language for Option A and Option B, and your minimum charge per language.
2. Which of the {n_langs} languages can you cover? Which would you subcontract?
3. Are your translators for these languages medically experienced? How do you select them?
4. Is the back-translation done by a different translator? If not, can it be?
5. Turnaround for Option A.
6. Do you charge for a second round of corrections after clinician review?
7. Can you supply translator credentials for our clinical governance record?

CONTACT
-------
[your name, role, email, phone]
[organisation]
"""

USE = """The English phrases in this pack were written for PhraseKit and may be shared with
translators and subcontractors for the purpose of quoting and performing this work.

No patient data is involved. These are fixed clinical phrases, so you do not need a data
processing agreement in order to quote.

Translations you deliver will be used in a clinical communication device: displayed on screen
and spoken aloud by synthetic speech to patients. We will hold them alongside the English and
may correct them following clinician or patient feedback. Please tell us how you wish to be
credited, and confirm that delivered translations may be used this way without a per-use fee.
"""


def rows(phrases, langs, tier1_only):
    out = []
    for p in phrases:
        if tier1_only and p["category"] != TIER1:
            continue
        for l in langs:
            out.append({
                "key": f"{p['key']}|{l['code']}",
                "language": l["label"],
                "language_code": l["code"],
                "tier": "1 (patient reply)" if p["category"] == TIER1 else "2",
                "category": p["category"],
                "english": p["text"],
                "context": CONTEXT.get(p["category"], ""),
                "translation": "",
                "back_translation": "",
                "notes": "",
            })
    return out


CONTEXT = {
    "patient_replies": "The PATIENT taps this to answer. Must read as the patient's own voice, not the clinician's.",
    "obstetric_emergency": "Said by a clinician during an obstetric emergency. Urgent but calm.",
    "intake": "Asked on arrival. Expect a yes/no or short answer.",
    "allergy": "Allergy screening. Accuracy matters more than fluency here.",
    "symptoms": "Asked to assess the patient. Expect a yes/no, a number or a gesture.",
    "examinations": "Said immediately before touching or examining the patient.",
    "medication": "Said when giving a medicine.",
    "consent": "Seeking consent. The patient must understand they may refuse.",
    "surgeries": "Explaining an operation about to happen.",
    "discharge": "Said when the patient goes home. Safety-netting.",
    "welcome": "Introduction at the start of the encounter.",
}


def write_csv(path, rs):
    with path.open("w", newline="", encoding="utf8") as f:
        w = csv.DictWriter(f, fieldnames=list(rs[0].keys()))
        w.writeheader()
        w.writerows(rs)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(Path.home() / "Downloads"), help="where to write the zip")
    ap.add_argument("--keep", action="store_true", help="leave the unzipped folder there too")
    a = ap.parse_args()

    d = json.loads(PHRASES.read_text())
    langs = [l for l in d["languages"] if l["code"] != "en"]
    phrases = [p for p in d["phrases"] if p["category"] != "panel_ui"]
    t1 = [p for p in phrases if p["category"] == TIER1]
    stamp = time.strftime("%Y-%m-%d")
    out_dir = Path(a.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    pack = out_dir / f"phrasekit-translation-{stamp}"
    if pack.exists():
        shutil.rmtree(pack)
    (pack / "machine-baseline").mkdir(parents=True)

    lang_lines = []
    for l in langs:
        bits = []
        if l["code"] in CALIBRATION:
            bits.append(f"machine baseline scores {CALIBRATION[l['code']]:.0f}/100 against professional medical translations")
        if l["code"] in NO_MACHINE:
            bits.append(NO_MACHINE[l["code"]])
        lang_lines.append(f"  - {l['label']} ({l['code']})" + (f"\n      {' '.join(bits)}" if bits else ""))

    (pack / "README.txt").write_text(BRIEF.format(
        date=stamp, n_langs=len(langs),
        t1_phrases=len(t1), t1_words=sum(len(p["text"].split()) for p in t1),
        t2_phrases=len(phrases), t2_words=sum(len(p["text"].split()) for p in phrases),
        lang_block="\n".join(lang_lines)), encoding="utf8")
    (pack / "LICENCE-AND-USE.txt").write_text(USE, encoding="utf8")
    write_csv(pack / "tier1-patient-replies.csv", rows(phrases, langs, True))
    write_csv(pack / "tier2-all-phrases.csv", rows(phrases, langs, False))

    for l in langs:
        base = [{"key": p["key"], "english": p["text"],
                 "machine_translation_unverified": (p.get("drafts") or {}).get(l["code"], "")
                                                   or p["translations"].get(l["code"], "")}
                for p in phrases]
        if any(r["machine_translation_unverified"] for r in base):
            write_csv(pack / "machine-baseline" / f"{l['code']}-{l['label'].replace(' ', '_')}.csv", base)

    zpath = out_dir / f"phrasekit-translation-{stamp}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(pack.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(pack.parent))
    if not a.keep:
        shutil.rmtree(pack)
    kb = zpath.stat().st_size / 1024
    print(f"{zpath}  ({kb:.0f} KB)")
    print(f"  Option A: {len(t1)} phrases x {len(langs)} languages = {len(t1) * len(langs)} rows, "
          f"{sum(len(p['text'].split()) for p in t1)} words per language")
    print(f"  Option B: {len(phrases)} phrases x {len(langs)} languages = {len(phrases) * len(langs)} rows, "
          f"{sum(len(p['text'].split()) for p in phrases)} words per language")
    print(f"  machine baseline: {len(list((Path(str(pack)) / 'machine-baseline').glob('*.csv'))) if a.keep else 'included'}")
    print("  Fill in the CONTACT block at the end of README.txt before sending.")


if __name__ == "__main__":
    main()
