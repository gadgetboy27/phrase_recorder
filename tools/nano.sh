#!/usr/bin/env bash
# Day-to-day menu for the Nano, from the Mac.
#
#   tools/nano.sh            # interactive menu
#   tools/nano.sh status     # or any item by name: status start ingest push bench translations backup shutdown
#
# Sets the env the other tools need (NANO_SSH, NANO_DEST, PG_DSN, libpq on PATH)
# so nothing has to be exported by hand. Password for Postgres comes from ~/.pgpass.
set -uo pipefail
cd "$(dirname "$0")/.."

export NANO_SSH="${NANO_SSH:-gadgetboy@192.168.68.111}"
export NANO_DEST="${NANO_DEST:-$NANO_SSH:/home/gadgetboy/phrase-recordings}"
export PG_DSN="${PG_DSN:-postgresql://interpreter_app@${NANO_SSH#*@}:5432/interpreter_data}"
export PATH="/usr/local/opt/libpq/bin:$PATH"
HOST="${NANO_SSH#*@}"

up() { nc -z -G 3 "$HOST" 22 2>/dev/null; }
need_up() { up || { echo "Nano ($HOST) is not reachable — power it on and wait ~60 s."; return 1; }; }

status() {
  need_up || return
  ssh "$NANO_SSH" '
    echo "up      : $(uptime -p)  temp $(cat /sys/devices/virtual/thermal/thermal_zone0/temp | cut -c1-2)°C"
    echo "memory  : $(free -m | awk "/Mem/{printf \"%d/%d MB used\", \$3, \$2}")   disk $(df -h / | awk "NR==2{print \$4}") free"
    pg_isready -q -h localhost && echo "postgres: ok" || echo "postgres: DOWN"
    if pgrep -x llama-server >/dev/null; then echo "llm     : llama-server running ($(pgrep -a llama-server | grep -o "[^/]*\.gguf"))"; else echo "llm     : llama-server NOT running  → menu: start"; fi
    echo "asr     : whisper.cpp $(ls ~/whisper.cpp/models/ggml-large-v3-turbo-q5_0.bin >/dev/null 2>&1 && echo ready || echo "MODEL MISSING")"
    echo "audio   : $(find ~/phrase-recordings -maxdepth 1 -mindepth 1 -type d -not -name "_*" -not -name ".*" | wc -l) batch dir(s)"
  '
  echo "golden  : $(psql "$PG_DSN" -At -c "select count(*)||' approved, '||count(*) filter (where status='draft')||' draft' from golden_set where status in ('approved','draft')" 2>/dev/null || echo "psql failed")"
}

start() {
  need_up || return
  ssh "$NANO_SSH" '
    if pgrep -x llama-server >/dev/null; then echo "llama-server already running"; exit 0; fi
    cd ~/llama.cpp && nohup ./build/bin/llama-server -m ~/models/Qwen2.5-3B-Instruct-Q4_K_M.gguf -ngl 99 -c 2048 --port 8080 --host 127.0.0.1 >~/llama-server.log 2>&1 &
    for i in $(seq 1 30); do curl -s localhost:8080/health | grep -q ok && { echo "llama-server up (${i}s)"; exit 0; }; sleep 1; done
    echo "llama-server did not come up — see ~/llama-server.log on the Nano"; tail -5 ~/llama-server.log
  '
}

ingest()       { tools/ingest.py --phrases public/phrases.json "${1:-$HOME/Downloads}"; }
push()         { need_up && tools/push.py --all; }
bench()        { need_up && tools/bench.py "$@"; }
translations() { tools/translations.py list; }
backup()       { need_up && tools/backup.py; }
shutdown_nano() {
  need_up || return
  read -r -p "Back up first? [Y/n] " yn; [[ "${yn:-y}" =~ ^[Yy] ]] && tools/backup.py
  ssh -t "$NANO_SSH" sudo shutdown -h now
}

menu() {
  while true; do
    echo
    if up; then echo "Nano: UP ($HOST)"; else echo "Nano: OFF ($HOST)"; fi
    cat <<'EOF'
  1) status        health, services, golden-set count
  2) start         start llama-server (Qwen) if it isn't running
  3) ingest        validate zips/WAVs in ~/Downloads → staged/
  4) push          staged batches → Nano
  5) bench         score Whisper + Qwen against the golden set
  6) translations  drafts awaiting review
  7) backup        pg_dump + recordings mirror → Mac
  8) shutdown      (offers a backup first)
  q) quit
EOF
    read -r -p "> " c
    case "$c" in
      1|status) status ;; 2|start) start ;; 3|ingest) ingest ;; 4|push) push ;;
      5|bench) bench ;; 6|translations) translations ;; 7|backup) backup ;;
      8|shutdown) shutdown_nano ;; q|quit|"") break ;;
      *) echo "?" ;;
    esac
  done
}

case "${1:-}" in
  "") menu ;;
  status|start|ingest|push|bench|translations|backup) f="$1"; shift; "$f" "$@" ;;
  shutdown) shutdown_nano ;;
  *) echo "usage: tools/nano.sh [status|start|ingest|push|bench|translations|backup|shutdown]"; exit 2 ;;
esac
