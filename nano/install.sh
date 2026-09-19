#!/usr/bin/env bash
# One-time root setup ON THE NANO (tools/nano.sh install copies this over and runs it with sudo).
# Idempotent — run it again after changing anything here.
#
#   phrase-panel.service        panel_api.py as a sandboxed systemd service (was an @reboot cron line):
#                               read-only everything except ~/panel and ~/phrase-recordings, private /tmp,
#                               no privilege escalation, restarted if it dies.
#   llama-server.service        Qwen fallback translator, same treatment.
#   phrase-panel-shutdown.path  the API can't sudo any more, so it touches /run/phrase-panel/shutdown and
#                               this root unit powers the Nano off.
#   phrasekit-internet          on|off|status — nftables rule that blocks every route to the internet
#                               (from the Nano and from anything on its hotspot) while a consult runs.
#                               `off` at a demo, `on` at home for fonts/Google-voice caching. Passwordless
#                               for $PANEL_USER via sudoers so tools/nano.sh internet can flip it.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
PANEL_USER="${SUDO_USER:-gadgetboy}"
HOME_DIR=$(getent passwd "$PANEL_USER" | cut -d: -f6)

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
ReadWritePaths=$HOME_DIR/panel $HOME_DIR/phrase-recordings
PrivateTmp=yes
NoNewPrivileges=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
# CUDA (whisper-cli) and ALSA need the device nodes; nothing here restricts /dev on purpose.

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

cat > /usr/local/sbin/phrasekit-internet <<'EOF'
#!/bin/sh
# phrasekit-internet on|off|status — off = nothing on the Nano, and nothing on its hotspot, can reach
# beyond the local networks. Loopback and RFC1918/link-local stay open so the panel, iPads, Postgres
# and SSH keep working. Not persisted: a reboot comes up with the internet allowed.
case "$1" in
  off)
    nft -f - <<'NFT'
table inet phrasekit
delete table inet phrasekit
table inet phrasekit {
  set lan4 { type ipv4_addr; flags interval; elements = { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8, 169.254.0.0/16, 224.0.0.0/4 } }
  chain output  { type filter hook output  priority 0; policy accept; oifname "lo" accept; ip daddr @lan4 accept; ip6 daddr { ::1, fe80::/10, ff00::/8 } accept; reject with icmpx type admin-prohibited }
  chain forward { type filter hook forward priority 0; policy accept; ip daddr @lan4 accept; ip6 daddr { fe80::/10, ff00::/8 } accept; reject with icmpx type admin-prohibited }
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

# the cron lines the services replace
crontab -u "$PANEL_USER" -l 2>/dev/null | grep -v -e panel_api.py -e llama-server | crontab -u "$PANEL_USER" - || true

systemctl daemon-reload
systemctl enable --now phrase-panel-shutdown.path llama-server.service phrase-panel.service
sleep 4
systemctl --no-pager --lines=0 status phrase-panel.service | sed -n 1,3p
echo "done — services: $(systemctl is-active phrase-panel llama-server phrase-panel-shutdown.path | tr '\n' ' ')"
