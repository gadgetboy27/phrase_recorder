#!/usr/bin/env python3
"""Runs ON THE NANO, over localhost only — no Wi-Fi, no panel, no fingers. Times each stage of a
turn the old way and the new way, side by side, without touching the running service:

    tools/nano.sh speed            # copies nano/*.py to ~/panel_speed/ and runs this there

  whisper   whisper-cli (a process + model load per call, what the API does today)
            vs whisper-server (model resident) — started here if nothing answers on :8178,
            stopped again afterwards. Same WAVs, texts compared.
  nllb      load time, then the same sentences at beam 4 (today) and beam 2 (new live path):
            seconds and the outputs, so a quality change is visible, not assumed.
  piper     one synthesis of an uncached sentence (unchanged by this round; the floor).
  http      a local request to the running API: HTTP/1.0 today closes after each reply; the
            connect+reply cost of that is what keep-alive removes on every later request.

Prints a table; also writes ~/phrase-recordings/bench/speed_<timestamp>.json. Stdlib only.
Memory: whisper-server (~1.6 GB) + NLLB (~0.6 GB) on top of whatever runs. If less than 2.5 GB is
free, llama-server (the Qwen fallback, coming off the boot set anyway) is stopped first with SIGTERM
— its unit does not restart on a clean signal — and the report says so.
"""
import http.client
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asr_worker  # noqa: E402
import panel_drafts  # noqa: E402

HOME = Path.home()
WHISPER = HOME / "whisper.cpp"
MODEL = WHISPER / "models" / asr_worker.SERVER_MODEL
PIPER = HOME / ".local/bin/piper"
VOICE = HOME / "piper-voices" / "en_US-lessac-medium.onnx"
OUT = HOME / "phrase-recordings" / "bench"
SENTENCES = ["Please stay as still as possible for me.",
             "Your baby's heart rate has dropped. This is an emergency.",
             "Come back immediately if the bleeding gets heavier."]
report = {"when": time.strftime("%Y-%m-%dT%H:%M:%S"), "notes": []}


def note(*a):
    msg = " ".join(str(x) for x in a)
    report["notes"].append(msg)
    print("  " + msg, flush=True)


def timed(fn, n=3, warm=0):
    for _ in range(warm):
        fn()
    ts, last = [], None
    for _ in range(n):
        t0 = time.time(); last = fn(); ts.append(time.time() - t0)
    return ts, last


def mem_free_mb():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def sample_wavs():
    """An English Piper render and, if there is one, a real recording (Farsi/Māori) with its language."""
    out = []
    en = HOME / "phrase-recordings" / "_tts_en" / "p003_en.wav"
    if en.exists():
        out.append((en, "en"))
    for lang in ("fa", "mi"):
        hits = sorted((HOME / "phrase-recordings").glob(f"*/p0*_{lang}_*.wav"))
        if hits:
            out.append((hits[0], lang)); break
    return out


def bench_whisper(wavs):
    print("whisper:")
    res = {}
    for wav, lang in wavs:
        wl = asr_worker.WHISPER_LANG.get(lang, lang)
        asr_worker.USE_SERVER = False
        cli_ts, cli_txt = timed(lambda: asr_worker.transcribe(asr_worker.SERVER_MODEL, wl, [wav])[str(wav)])
        asr_worker.USE_SERVER = True
        res[wav.name] = {"lang": lang, "cli_s": cli_ts, "cli_text": cli_txt}
        note(f"{wav.name} [{lang}] whisper-cli: {', '.join(f'{t:.2f}' for t in cli_ts)} s → {cli_txt!r}")
    started = None
    if not asr_worker.server_up():
        exe = WHISPER / "build/bin/whisper-server"
        if not exe.exists():
            note("whisper-server binary missing — rebuild whisper.cpp (cmake --build build --target whisper-server)")
            return res
        for flags in (["-fa", "-nt"], ["-nt"], []):
            log = open(HOME / "whisper-server-speed.log", "ab")
            started = subprocess.Popen([str(exe), "-m", str(MODEL), "--host", "127.0.0.1", "--port", "8178", *flags],
                                       cwd=str(WHISPER), stdout=log, stderr=log, stdin=subprocess.DEVNULL)
            for _ in range(60):
                time.sleep(1)
                asr_worker._server_seen[:] = [0.0, False]
                if asr_worker.server_up():
                    break
                if started.poll() is not None:
                    break
            if asr_worker.server_up():
                note(f"whisper-server started here with flags {flags} (pid {started.pid})")
                res["server_flags"] = flags
                break
            note(f"whisper-server would not start with {flags} — see ~/whisper-server-speed.log")
            if started.poll() is None:
                started.terminate(); started.wait(timeout=10)
            started = None
        if not started:
            return res
    else:
        note("whisper-server already answering on :8178 (using it as is)")
    for wav, lang in wavs:
        wl = asr_worker.WHISPER_LANG.get(lang, lang)
        try:
            srv_ts, srv_txt = timed(lambda: asr_worker._server_transcribe(wl, wav), warm=1)
        except Exception as e:
            note(f"{wav.name}: whisper-server rejected the file ({e!r}) — 48 kHz input? try --convert / resample")
            res[wav.name]["server_error"] = repr(e)
            continue
        res[wav.name].update(server_s=srv_ts, server_text=srv_txt)
        same = (srv_txt or "").strip().lower() == (res[wav.name]["cli_text"] or "").strip().lower()
        note(f"{wav.name} [{lang}] whisper-server: {', '.join(f'{t:.2f}' for t in srv_ts)} s → {srv_txt!r}  "
             f"({'same text' if same else 'TEXT DIFFERS'})")
    if started:
        started.terminate(); started.wait(timeout=10)
        note("whisper-server stopped again (install.sh makes it a service)")
    return res


def bench_nllb():
    print("nllb:")
    if not panel_drafts.nllb_available():
        note("NLLB not installed"); return {}
    t0 = time.time(); panel_drafts.nllb_load(); load = time.time() - t0
    note(f"load: {load:.1f} s (paid once at start-up from now on; today it was the first patient's wait)")
    res = {"load_s": load, "rows": []}
    for tgt in ("hi", "mi"):
        for s in SENTENCES:
            r = {"tgt": tgt, "src": s}
            for beam in (4, 2):
                t0 = time.time(); out = panel_drafts.nllb_translate(s, "en", tgt, beam=beam); r[f"beam{beam}_s"] = time.time() - t0
                r[f"beam{beam}"] = out
            res["rows"].append(r)
            flag = "same" if r["beam4"] == r["beam2"] else "differs"
            note(f"[{tgt}] beam4 {r['beam4_s']:.2f} s / beam2 {r['beam2_s']:.2f} s ({flag}): {s}")
            if flag == "differs":
                note(f"      beam4: {r['beam4']}\n      beam2: {r['beam2']}")
    return res


def bench_piper():
    print("piper:")
    if not (PIPER.exists() and VOICE.exists()):
        note("piper or the English voice missing"); return {}
    out = Path("/tmp/speed_piper.wav")
    def run():
        subprocess.run([str(PIPER), "--model", str(VOICE), "--output_file", str(out)],
                       input=f"Speed check {time.time():.0f}.", text=True, check=True, capture_output=True)
    ts, _ = timed(run, n=2)
    note(f"uncached sentence: {', '.join(f'{t:.2f}' for t in ts)} s (a process + voice load each time; round two makes this resident)")
    return {"s": ts}


def bench_http():
    """Cost of a new connection per request on the local API: what HTTP/1.0 pays every time."""
    print("http (localhost, running API):")
    try:
        ts = []
        for _ in range(5):
            t0 = time.time()
            c = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5); c.request("GET", "/health"); r = c.getresponse(); r.read()
            ts.append(time.time() - t0); ver = r.version; c.close()
        c = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)
        c.request("GET", "/health"); c.getresponse().read()
        kept = c.sock is not None
        try:
            c.request("GET", "/health"); c.getresponse().read(); kept = kept and c.sock is not None
        except Exception:
            kept = False
        note(f"/health new connection each time: median {statistics.median(ts) * 1000:.0f} ms; server speaks HTTP/1.{ver - 10}; "
             f"connection reused: {kept}  (over Wi-Fi + TLS the same handshake is 0.2–2 s — that is what keep-alive removes)")
        return {"health_ms": [t * 1000 for t in ts], "keepalive": kept}
    except Exception as e:
        note(f"API not answering on :8765 ({e!r})"); return {}


def main():
    free = mem_free_mb()
    note(f"memory available: {free} MB")
    if free < 2500:
        pids = subprocess.run(["pgrep", "-x", "llama-server"], capture_output=True, text=True).stdout.split()
        if pids:
            subprocess.run(["kill", "-TERM", *pids])
            time.sleep(3)
            note(f"stopped llama-server (pid {' '.join(pids)}) to make room; {mem_free_mb()} MB available now. "
                 "It stays off until the next boot — `tools/nano.sh start` brings it back")
    wavs = sample_wavs()
    if not wavs:
        note("no sample WAV under ~/phrase-recordings — nothing to transcribe"); return
    report["whisper"] = bench_whisper(wavs)
    report["nllb"] = bench_nllb()
    report["piper"] = bench_piper()
    report["http"] = bench_http()
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"speed_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    p.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\nsummary (medians):")
    for name, r in report["whisper"].items():
        if isinstance(r, dict) and "cli_s" in r:
            srv = f"{statistics.median(r['server_s']):.2f} s" if "server_s" in r else "—"
            print(f"  whisper {name} [{r['lang']}]: cli {statistics.median(r['cli_s']):.2f} s → server {srv}")
    if report["nllb"].get("rows"):
        b4 = statistics.median(x["beam4_s"] for x in report["nllb"]["rows"]); b2 = statistics.median(x["beam2_s"] for x in report["nllb"]["rows"])
        diff = sum(1 for x in report["nllb"]["rows"] if x["beam4"] != x["beam2"])
        print(f"  nllb: beam4 {b4:.2f} s → beam2 {b2:.2f} s per sentence; {diff}/{len(report['nllb']['rows'])} outputs differ; load {report['nllb']['load_s']:.1f} s")
    if report["piper"].get("s"):
        print(f"  piper: {statistics.median(report['piper']['s']):.2f} s per uncached sentence")
    if report["http"].get("health_ms"):
        print(f"  http: {statistics.median(report['http']['health_ms']):.0f} ms per new local connection; keep-alive today: {report['http']['keepalive']}")
    print(f"  report: {p}")


if __name__ == "__main__":
    main()
