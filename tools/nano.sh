#!/usr/bin/env bash
# Day-to-day menu for the Nano, from the Mac.
#
#   tools/nano.sh            # interactive menu
#   tools/nano.sh status     # or any item by name: status start panel install pair devices revoke internet hotspot fonts test speed
#                            #                       ingest push bench translations backup reboot shutdown
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
    echo "asr     : whisper.cpp $(ls ~/whisper.cpp/models/ggml-large-v3-turbo-q5_0.bin >/dev/null 2>&1 && echo ready || echo "MODEL MISSING"), whisper-server $(curl -s -o /dev/null -m 2 -w "%{http_code}" localhost:8178/ | grep -q "^[234]" && echo "resident (fast)" || echo "DOWN → whisper-cli per call (slow)")"
    echo "mt      : NLLB $(ls ~/models/nllb-600M-ct2-int8/model.bin >/dev/null 2>&1 && echo ready || echo MISSING); llama-server (Qwen fallback, off by design) $(pgrep -x llama-server >/dev/null && echo running || echo stopped)"
    echo "panel   : $(curl -s -m 2 localhost:8765/health || echo "api NOT running  → menu: panel")"
    echo "audio   : $(find ~/phrase-recordings -maxdepth 1 -mindepth 1 -type d -not -name "_*" -not -name ".*" | wc -l) batch dir(s)"
  '
  echo "golden  : $(psql "$PG_DSN" -At -c "select count(*)||' approved, '||count(*) filter (where status='draft')||' draft' from golden_set where status in ('approved','draft')" 2>/dev/null || echo "psql failed")"
}

start() {                                   # the Qwen fallback, for a session only — NLLB is the translator (install.sh)
  need_up || return
  ssh "$NANO_SSH" '
    if pgrep -x llama-server >/dev/null; then echo "llama-server already running"; exit 0; fi
    cd ~/llama.cpp && setsid -f ./build/bin/llama-server -m ~/models/Qwen2.5-3B-Instruct-Q4_K_M.gguf -ngl 99 -c 2048 --port 8080 --host 127.0.0.1 >~/llama-server.log 2>&1 </dev/null
    for i in $(seq 1 30); do curl -s localhost:8080/health | grep -q ok && { echo "llama-server up (${i}s)"; exit 0; }; sleep 1; done
    echo "llama-server did not come up — see ~/llama-server.log on the Nano"; tail -5 ~/llama-server.log
  '
}

# Touch panel API (nano/panel_api.py): copy it + its helpers over, (re)start it, keep it across reboots.
panel() {
  need_up || return
  # The panel can add phrases and languages itself now, so Postgres may be ahead of public/phrases.json:
  # export first, so the copy we ship (and commit) is the database, never an older file over a newer one.
  python3 tools/export_phrases.py || { echo "export_phrases failed — not deploying a possibly stale phrases.json"; return 1; }
  ssh "$NANO_SSH" 'mkdir -p ~/panel/web'
  scp -q nano/panel_api.py nano/panel_takes.py nano/panel_drafts.py nano/panel_render.py nano/panel_admin.py nano/asr_worker.py \
         tools/push.py tools/export_phrases.py public/phrases.json "$NANO_SSH:panel/"
  scp -q nano/web/* "$NANO_SSH:panel/web/"
  # The Waveshare's device token (nano/secrets.yaml) is registered as a paired device, and the Nano's own CA +
  # server certificate are (re)issued so iPads can use https://…:8766 — see nano/panel_admin.py.
  tok=$(sed -n 's/^panel_token: *"\(.*\)".*/\1/p' nano/secrets.yaml 2>/dev/null)
  if [ -n "$tok" ] && [ "$tok" != "REPLACE" ]; then
    ssh "$NANO_SSH" "cd ~/panel && python3 -c \"import panel_admin; panel_admin.ensure_device('$tok', 'waveshare', 'panel')\""
  else
    echo "warning: no panel_token in nano/secrets.yaml — the Waveshare will get 401 from the API"
  fi
  ssh "$NANO_SSH" 'cd ~/panel && python3 panel_admin.py tls'
  # The API files volunteer takes into Postgres itself, so the Nano needs the app password: reuse the
  # Mac's ~/.pgpass entry as a localhost line (mode 600). Skipped, with a warning, if the Mac has none.
  pw=$(awk -F: -v h="$HOST" '$1 == h && $3 == "interpreter_data" && $4 == "interpreter_app" {print $5; exit}' ~/.pgpass 2>/dev/null)
  if [ -n "$pw" ]; then
    printf 'localhost:5432:interpreter_data:interpreter_app:%s\n' "$pw" | ssh "$NANO_SSH" 'umask 077; cat > ~/.pgpass'
  else
    echo "warning: no ~/.pgpass entry for $HOST/interpreter_data on the Mac — panel takes will not reach Postgres"
  fi
  ssh "$NANO_SSH" '
    if systemctl list-unit-files phrase-panel.service >/dev/null 2>&1 && [ -f /etc/systemd/system/phrase-panel.service ]; then
      # no root needed: the unit has Restart=always, so ending the process is a restart
      kill "$(systemctl show -p MainPID --value phrase-panel)" 2>/dev/null; sleep 4
    else                                   # not installed as a service yet (tools/nano.sh install): the old way
      (crontab -l 2>/dev/null | grep -v -e panel_api.py -e llama-server
       echo "@reboot sleep 15 && python3 \$HOME/panel/panel_api.py >>\$HOME/panel/panel.log 2>&1"
       echo "@reboot sleep 20 && cd \$HOME/llama.cpp && ./build/bin/llama-server -m \$HOME/models/Qwen2.5-3B-Instruct-Q4_K_M.gguf -ngl 99 -c 2048 --port 8080 --host 127.0.0.1 >>\$HOME/llama-server.log 2>&1") | crontab -
      pkill -f "^python3 .*panel_api.py"; sleep 1   # anchored so it cannot match this very shell
      cd ~/panel && setsid -f python3 panel_api.py >>panel.log 2>&1 </dev/null
    fi
    for i in $(seq 1 15); do curl -s -m 1 localhost:8765/health && { echo; echo "panel api up (${i}s)"; exit 0; }; sleep 1; done
    echo "panel api did not come up — see ~/panel/panel.log on the Nano"; tail -5 ~/panel/panel.log
  '
}

# Demo kit networking: the Nano's Wi-Fi as the "PhraseKit" hotspot (10.42.0.1) the panel joins.
# Needs nmcli in /etc/sudoers.d/gadgetboy-power. With Ethernet plugged in the hotspot can stay on
# for good (plan A); without it, `hotspot off` puts the Wi-Fi back on the home LAN (plan B).
hotspot() {
  need_up || return
  # The hotspot password lives only in nano/secrets.yaml (gitignored; the panel firmware reads the same key).
  kit_pw=$(sed -n 's/^kit_password: *"\(.*\)"/\1/p' nano/secrets.yaml 2>/dev/null)
  case "${1:-status}" in
    on)
      [ -n "$kit_pw" ] || { echo "set kit_password in nano/secrets.yaml first (see nano/secrets.yaml.example)"; return 1; }
      ssh "$NANO_SSH" "
        sudo -n nmcli -t -f NAME con show | grep -qx PhraseKit || {
          sudo -n nmcli con add type wifi ifname wlP1p1s0 con-name PhraseKit autoconnect yes connection.autoconnect-priority 10 ssid PhraseKit &&
          sudo -n nmcli con modify PhraseKit 802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared ipv4.addresses 10.42.0.1/24 \\
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk '$kit_pw'; }
        echo 'switching Wi-Fi to the PhraseKit hotspot (this SSH session will drop if it came in over Wi-Fi)'
        setsid -f sudo -n nmcli con up PhraseKit >/dev/null 2>&1 </dev/null" ;;
    off)
      ssh "$NANO_SSH" 'setsid -f sudo -n nmcli con up gadgetboy2 >/dev/null 2>&1 </dev/null; echo "Wi-Fi back on gadgetboy2 in a few seconds"' ;;
    status)
      ssh "$NANO_SSH" 'nmcli -t -f DEVICE,STATE,CONNECTION dev status | grep -E "^(wlP|enP)"; ip -4 -br addr | grep -E "wlP|enP"' ;;
    *) echo "usage: tools/nano.sh hotspot [on|off|status]"; return 2 ;;
  esac
}

# Stage-by-stage speed check ON the Nano over localhost (no Wi-Fi, no panel): whisper-cli vs whisper-server,
# NLLB beam 4 vs 2, piper, HTTP connection cost. Runs the repo's current nano/*.py from a scratch folder —
# the deployed service is untouched — so it works before `install`/`panel`. nano/speedbench.py explains.
speed() {
  need_up || return
  ssh "$NANO_SSH" 'mkdir -p ~/panel_speed'
  scp -q nano/speedbench.py nano/asr_worker.py nano/panel_drafts.py tools/push.py "$NANO_SSH:panel_speed/"
  ssh "$NANO_SSH" 'cd ~/panel_speed && python3 speedbench.py'
}
# One-time root setup on the Nano: sandboxed systemd services, shutdown path unit, internet on/off rule.
# Asks for your password on the Nano (nano/install.sh explains each piece).
install()      { need_up && scp -q nano/install.sh "$NANO_SSH:panel/install.sh" && ssh -t "$NANO_SSH" 'sudo bash ~/panel/install.sh'; }
# iPad pairing: prints a 10-minute code; on the iPad open the setup page it names and type the code.
pair()         { need_up && ssh "$NANO_SSH" 'cd ~/panel && python3 panel_admin.py pair'; }
devices()      { need_up && ssh "$NANO_SSH" 'cd ~/panel && python3 panel_admin.py devices'; }
revoke()       { need_up && ssh "$NANO_SSH" "cd ~/panel && python3 panel_admin.py revoke '$1'"; }
# internet off = provably nothing leaves the Nano or its hotspot during a consult (nftables; see install.sh)
internet()     { need_up && ssh "$NANO_SSH" "sudo -n /usr/local/sbin/phrasekit-internet ${1:-status}"; }

# Every Noto font the language table (nano/panel_admin.py) can need, into ~/panel/fonts — do this at home,
# once, so "Add a language" on the panel never has to reach the internet at a demo.
fonts()        { need_up && ssh "$NANO_SSH" 'cd ~/panel && python3 panel_admin.py fonts'; }
reboot_nano()  { need_up && ssh "$NANO_SSH" 'sudo -n reboot' ; echo "rebooting — panel API and llama-server come back by cron in ~90 s"; }

test()         { PYTHONPATH="$(python3 -c 'import esphome_glyphsets,os;print(os.path.dirname(os.path.dirname(esphome_glyphsets.__file__)))' 2>/dev/null)" tools/panel_test.py "$@"; }

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
  p) panel         deploy/restart the touch-panel API (nano/panel_api.py)
  t) test          regression tests: panel fonts vs texts, Nano API per language, panel liveness
  s) speed         stage-by-stage timings on the Nano itself (old path vs new), nothing deployed
  h) hotspot       on|off|status — the Nano's PhraseKit Wi-Fi hotspot for demos
  f) fonts         fetch every Noto font "Add a language" could need (needs internet, once)
  i) install       one-time root setup on the Nano: sandboxed services, shutdown unit, internet switch
  a) pair          pairing code for an iPad (devices / revoke <name> to list / remove)
  n) internet      on|off|status — block every route off the local network during a consult
  r) reboot        restart the Nano (services come back on their own)
  3) ingest        validate zips/WAVs in ~/Downloads → staged/
  4) push          staged batches → Nano
  5) bench         score Whisper + Qwen against the golden set
  6) translations  drafts awaiting review (Whisper drafts: tools/draft_transcripts.py --lang xx)
  7) backup        pg_dump + recordings mirror → Mac
  8) shutdown      (offers a backup first)
  q) quit
EOF
    read -r -p "> " c
    case "$c" in
      1|status) status ;; 2|start) start ;; p|panel) panel ;; t|test) test ;; s|speed) speed ;; h|hotspot) read -r -p "on/off/status? " m; hotspot "$m" ;; 3|ingest) ingest ;; 4|push) push ;;
      f|fonts) fonts ;; r|reboot) reboot_nano ;; i|install) install ;; a|pair) pair ;;
      n|internet) read -r -p "on/off/status? " m; internet "$m" ;;
      5|bench) bench ;; 6|translations) translations ;; 7|backup) backup ;;
      8|shutdown) shutdown_nano ;; q|quit|"") break ;;
      *) echo "?" ;;
    esac
  done
}

case "${1:-}" in
  "") menu ;;
  status|start|panel|hotspot|fonts|test|speed|ingest|push|bench|translations|backup|install|pair|devices|revoke|internet) f="$1"; shift; "$f" "$@" ;;
  shutdown) shutdown_nano ;;
  reboot) reboot_nano ;;
  *) echo "usage: tools/nano.sh [status|start|panel|install|pair|devices|revoke|internet|hotspot|fonts|test|ingest|push|bench|translations|backup|reboot|shutdown]"; exit 2 ;;
esac
