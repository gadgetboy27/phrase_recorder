#!/usr/bin/env bash
# One-time root setup ON THE NANO (tools/nano.sh install copies this over and runs it with sudo).
# Idempotent — run it again after changing anything here.
#
#   phrase-panel.service        panel_api.py as a sandboxed systemd service (was an @reboot cron line):
#                               read-only everything except ~/panel and ~/phrase-recordings, private /tmp,
#                               no privilege escalation, restarted if it dies.
#   whisper-server.service      whisper.cpp's server with large-v3-turbo held on the GPU, so a transcription
#                               costs ~1 s instead of ~2.5 s (whisper-cli reloaded the model on every call).
#                               The API falls back to whisper-cli whenever it is down.
#   llama-server.service        Qwen fallback translator. Installed but DISABLED since 2026-09-21: NLLB is the
#                               translator and the 3B took ~2 GB of the 8 GB the resident Whisper now needs.
#                               `sudo systemctl start llama-server` brings it back for a session.
#   power button                logind handles the J14 button (HandlePowerKey=poweroff) and the box boots to
#                               multi-user.target — no GDM to swallow the key, and ~700 MB less RAM in use.
#   phrase-panel-shutdown.path  the API can't sudo any more, so it touches /run/phrase-panel/shutdown and
#                               this root unit powers the Nano off.
#   phrase-panel-wifi.path      the API writes an SSID to /run/phrase-panel/wifi; root switches networks
#                               (falls back to the previous one if the new one fails).
#   90-phrasekit-internet       NM dispatcher: internet off automatically on any Wi-Fi that is not home
#   phrasekit-internet          on|off|status — nftables rule that blocks every route to the internet
#                               (from the Nano and from anything on its hotspot) while a consult runs.
#                               `off` at a demo, `on` at home for fonts/Google-voice caching. Passwordless
#                               for $PANEL_USER via sudoers so tools/nano.sh internet can flip it.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
PANEL_USER="${SUDO_USER:-gadgetboy}"
HOME_DIR=$(getent passwd "$PANEL_USER" | cut -d: -f6)
mkdir -p "$HOME_DIR/.config/pulse" && chown "$PANEL_USER" "$HOME_DIR/.config/pulse"   # libpulse (loaded by aplay) wants a scratch dir

cat > /etc/systemd/system/phrase-panel.service <<EOF
[Unit]
Description=PhraseKit panel API (nano/panel_api.py)
After=network-online.target postgresql.service sound.target
Wants=network-online.target

[Service]
User=$PANEL_USER
WorkingDirectory=$HOME_DIR/panel
ExecStart=/usr/bin/python3 $HOME_DIR/panel/panel_api.py
Restart=always
RestartSec=3
StandardOutput=append:$HOME_DIR/panel/panel.log
StandardError=append:$HOME_DIR/panel/panel.log
# ---- sandbox: the API may write to its own folder and the recordings, nothing else ----
RuntimeDirectory=phrase-panel
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$HOME_DIR/panel $HOME_DIR/phrase-recordings $HOME_DIR/.config/pulse
PrivateTmp=yes
NoNewPrivileges=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
# CUDA (whisper-cli) and ALSA need the device nodes; nothing here restricts /dev on purpose.

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/whisper-server.service <<EOF
[Unit]
Description=whisper-server (resident Whisper for the panel API)
After=network.target

[Service]
User=$PANEL_USER
WorkingDirectory=$HOME_DIR/whisper.cpp
# -fa: flash attention (CUDA build). --host 127.0.0.1: the panel API is the only client; the WAV never leaves the box.
ExecStart=$HOME_DIR/whisper.cpp/build/bin/whisper-server -m $HOME_DIR/whisper.cpp/models/ggml-large-v3-turbo-q5_0.bin --host 127.0.0.1 --port 8178 -fa -nt
Restart=on-failure
RestartSec=5
StandardOutput=append:$HOME_DIR/whisper-server.log
StandardError=append:$HOME_DIR/whisper-server.log
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/llama-server.service <<EOF
[Unit]
Description=llama-server (Qwen fallback translator for the panel)
After=network.target

[Service]
User=$PANEL_USER
WorkingDirectory=$HOME_DIR/llama.cpp
ExecStart=$HOME_DIR/llama.cpp/build/bin/llama-server -m $HOME_DIR/models/Qwen2.5-3B-Instruct-Q4_K_M.gguf -ngl 99 -c 2048 --port 8080 --host 127.0.0.1
Restart=on-failure
RestartSec=5
StandardOutput=append:$HOME_DIR/llama-server.log
StandardError=append:$HOME_DIR/llama-server.log
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/phrase-panel-shutdown.path <<'EOF'
[Unit]
Description=Power off when the panel API asks (touches /run/phrase-panel/shutdown)

[Path]
PathExists=/run/phrase-panel/shutdown

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/phrase-panel-shutdown.service <<'EOF'
[Unit]
Description=Power off requested by the panel

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'rm -f /run/phrase-panel/shutdown; /usr/bin/systemctl poweroff'
EOF

# The API (sandboxed, no sudo) writes an SSID to /run/phrase-panel/wifi; this root unit switches to it.
# If the new network is not up within 40 s the previous one is restored, so a bad pick cannot strand the Nano.
cat > /usr/local/sbin/phrasekit-wifi <<'EOF'
#!/bin/bash
req=/run/phrase-panel/wifi
[ -f "$req" ] || exit 0
ssid=$(head -c 64 "$req"); rm -f "$req"
prev=$(nmcli -t -f ACTIVE,SSID dev wifi list --rescan no | awk -F: '$1=="yes"{print $2; exit}')
[ -n "$ssid" ] && [ "$ssid" != "$prev" ] || exit 0
logger -t phrasekit-wifi "switching from '$prev' to '$ssid'"
nmcli dev wifi rescan >/dev/null 2>&1; sleep 4
if nmcli --wait 40 con up "$ssid" >/dev/null 2>&1; then
  logger -t phrasekit-wifi "on '$ssid' ($(hostname -I | cut -d' ' -f1))"
else
  logger -t phrasekit-wifi "'$ssid' did not come up - back to '$prev'"
  [ -n "$prev" ] && nmcli --wait 40 con up "$prev" >/dev/null 2>&1
fi
EOF
chmod 755 /usr/local/sbin/phrasekit-wifi
cat > /etc/systemd/system/phrase-panel-wifi.path <<'EOF'
[Unit]
Description=Switch Wi-Fi when the panel API asks (writes /run/phrase-panel/wifi)

[Path]
PathExists=/run/phrase-panel/wifi

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/phrase-panel-wifi.service <<'EOF'
[Unit]
Description=Wi-Fi switch requested by the panel

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/phrasekit-wifi
EOF

# NetworkManager dispatcher: whenever Wi-Fi comes up, block the internet unless it is the home network —
# a phone hotspot is for the panel and iPads, not for apt/snap (the Nano pulled 2.66 GB through one).
cat > /etc/NetworkManager/dispatcher.d/90-phrasekit-internet <<'EOF'
#!/bin/sh
[ "$2" = "up" ] || exit 0
ssid=$(nmcli -t -f ACTIVE,SSID dev wifi list --rescan no 2>/dev/null | awk -F: '$1=="yes"{print $2; exit}')
case "$ssid" in
  gadgetboy2|"") /usr/local/sbin/phrasekit-internet on ;;
  *)             /usr/local/sbin/phrasekit-internet off ;;
esac
EOF
chmod 755 /etc/NetworkManager/dispatcher.d/90-phrasekit-internet

cat > /usr/local/sbin/phrasekit-internet <<'EOF'
#!/bin/sh
# phrasekit-internet on|off|status — off = nothing on the Nano, and nothing on its hotspot, can reach
# beyond the local networks. Loopback and RFC1918/link-local stay open so the panel, iPads, Postgres
# and SSH keep working. Not persisted: a reboot comes up with the internet allowed.
case "$1" in
  off)
    nft -f - <<'NFT' || { echo "internet: rule failed to load — still ON"; exit 1; }
table inet phrasekit
delete table inet phrasekit
table inet phrasekit {
  set lan4 {
    type ipv4_addr
    flags interval
    elements = { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8, 169.254.0.0/16, 224.0.0.0/4 }
  }
  chain output {
    type filter hook output priority 0; policy accept;
    oifname "lo" accept
    ip daddr @lan4 accept
    ip6 daddr { ::1, fe80::/10, ff00::/8 } accept
    drop
  }
  chain forward {
    type filter hook forward priority 0; policy accept;
    ip daddr @lan4 accept
    ip6 daddr { fe80::/10, ff00::/8 } accept
    drop
  }
}
NFT
    echo "internet: OFF (LAN and hotspot only)" ;;
  on)
    nft delete table inet phrasekit 2>/dev/null || true
    echo "internet: on" ;;
  status)
    if nft list table inet phrasekit >/dev/null 2>&1; then echo "internet: OFF (LAN and hotspot only)"; else echo "internet: on"; fi ;;
  *) echo "usage: phrasekit-internet on|off|status"; exit 2 ;;
esac
EOF
chmod 755 /usr/local/sbin/phrasekit-internet

cat > /etc/sudoers.d/phrasekit <<EOF
$PANEL_USER ALL=(root) NOPASSWD: /usr/local/sbin/phrasekit-internet
EOF
chmod 440 /etc/sudoers.d/phrasekit
visudo -c -f /etc/sudoers.d/phrasekit >/dev/null

# ---- the wired power button (J14 pins 11-12): a short press must be a clean shutdown ----
# With a desktop session up, gnome-settings-daemon takes the power key from logind and does nothing useful
# on a headless box, so the Nano boots to multi-user.target (no GDM; `sudo systemctl start gdm` if a
# screen is ever plugged in) and logind powers off on the key. The iPad's shutdown path is unchanged.
sed -i 's/^#\?HandlePowerKey=.*/HandlePowerKey=poweroff/' /etc/systemd/logind.conf
grep -q '^HandlePowerKey=poweroff' /etc/systemd/logind.conf || echo 'HandlePowerKey=poweroff' >> /etc/systemd/logind.conf
systemctl set-default multi-user.target >/dev/null

# the cron lines the services replace
crontab -u "$PANEL_USER" -l 2>/dev/null | grep -v -e panel_api.py -e llama-server | crontab -u "$PANEL_USER" - || true

systemctl daemon-reload
# whatever cron/setsid started before the units existed holds :8765 / :8080 — the units replace it
pkill -u "$PANEL_USER" -f "^python3 .*panel_api.py" || true
pkill -u "$PANEL_USER" -x llama-server || true
sleep 2
systemctl disable --now llama-server.service 2>/dev/null || true          # kept on disk, off the boot set (see header)
systemctl enable --now phrase-panel-shutdown.path phrase-panel-wifi.path whisper-server.service phrase-panel.service
systemctl restart whisper-server.service phrase-panel.service
sleep 4
systemctl --no-pager --lines=0 status phrase-panel.service | sed -n 1,3p
echo "done — services: $(systemctl is-active phrase-panel whisper-server phrase-panel-shutdown.path phrase-panel-wifi.path | tr '\n' ' ')  (llama-server: $(systemctl is-active llama-server), disabled by design)"
echo "power key: $(grep '^HandlePowerKey' /etc/systemd/logind.conf)  default target: $(systemctl get-default)  (logind re-reads its config at the next boot)"
