#!/usr/bin/env bash
# Installerer porteføljen som en tjeneste, der starter af sig selv, når Pi'en tænder.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(whoami)"
[ -f "$DIR/config.json" ] || cp "$DIR/config.example.json" "$DIR/config.json"
mkdir -p "$DIR/eksport" "$DIR/data"
sudo tee /etc/systemd/system/portefolje.service >/dev/null <<UNIT
[Unit]
Description=Guggas portefolje (kurser og dashboard)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/app.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now portefolje
sleep 2
systemctl --no-pager status portefolje | head -n 5
echo
echo "Dashboardet kører nu på: http://$(hostname -I | awk '{print $1}'):$(python3 -c "import json;print(json.load(open('$DIR/config.json'))['port'])")"
