#!/bin/bash
# Installa echo-bridge + faster-whisper + Piper in un container Debian 13. Da eseguire come root nel container,
# dalla cartella del repository https://github.com/andreadegiovine/ai-echo-dot-bridge:  bash install.sh
# Variabili opzionali: WHISPER_MODEL (default small-int8), PIPER_VOICE (default it_IT-paola-medium), THREADS (default nproc)
set -euo pipefail
cd "$(dirname "$0")"
WHISPER_MODEL=${WHISPER_MODEL:-small-int8}
PIPER_VOICE=${PIPER_VOICE:-it_IT-paola-medium}
THREADS=${THREADS:-$(nproc)}
APP=/opt/echo-bridge

echo "==> Pacchetti di sistema"
apt-get update
apt-get install -y python3 python3-venv python3-dev build-essential ca-certificates curl netcat-openbsd

echo "==> Utente di servizio"
id echobridge >/dev/null 2>&1 || useradd --system --home-dir $APP --shell /usr/sbin/nologin echobridge

echo "==> Ambiente Python"
mkdir -p $APP /var/lib/echo-bridge/{whisper,piper,state} /etc/echo-bridge
python3 -m venv $APP/venv
$APP/venv/bin/pip install --upgrade pip
$APP/venv/bin/pip install -r requirements.txt
rm -rf $APP/echobridge $APP/echo_bridge.py
cp -r echobridge $APP/
install -m 755 selftest.py $APP/
[ -f /etc/echo-bridge/echo-bridge.env ] || install -m 640 echo-bridge.env /etc/echo-bridge/echo-bridge.env
chown -R echobridge:echobridge $APP /var/lib/echo-bridge
chgrp echobridge /etc/echo-bridge/echo-bridge.env

echo "==> Servizi systemd"
cat > /etc/systemd/system/wyoming-whisper.service <<EOF
[Unit]
Description=Wyoming faster-whisper (STT)
After=network-online.target
Wants=network-online.target

[Service]
User=echobridge
ExecStart=$APP/venv/bin/python -m wyoming_faster_whisper --uri tcp://127.0.0.1:10300 \\
  --stt-library faster-whisper --model $WHISPER_MODEL --language it --beam-size 1 \\
  --cpu-threads $THREADS --data-dir /var/lib/echo-bridge/whisper
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/wyoming-piper.service <<EOF
[Unit]
Description=Wyoming Piper (TTS)
After=network-online.target
Wants=network-online.target

[Service]
User=echobridge
ExecStart=$APP/venv/bin/python -m wyoming_piper --uri tcp://127.0.0.1:10200 \\
  --voice $PIPER_VOICE --data-dir /var/lib/echo-bridge/piper
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/echo-bridge.service <<EOF
[Unit]
Description=Echo Dot (hassmic) <-> agente locale
After=network-online.target wyoming-whisper.service wyoming-piper.service
Wants=network-online.target wyoming-whisper.service wyoming-piper.service

[Service]
User=echobridge
EnvironmentFile=/etc/echo-bridge/echo-bridge.env
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python -m echobridge
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now wyoming-whisper wyoming-piper
echo "==> Attendo che STT e TTS scarichino i modelli e siano pronti (prima volta: qualche minuto)"
for port in 10300 10200; do
  for i in $(seq 1 120); do nc -z 127.0.0.1 $port && break; sleep 5; done
  nc -z 127.0.0.1 $port && echo "    porta $port pronta" || echo "    porta $port NON pronta: journalctl -u wyoming-whisper -u wyoming-piper"
done
echo
echo "Fatto. Ora:"
echo "  1. nano /etc/echo-bridge/echo-bridge.env   (ECHO_HOSTS e LLM_URL; per i nuovi parametri confronta con echo-bridge.env del repository)"
echo "  2. systemctl enable --now echo-bridge"
echo "  3. journalctl -fu echo-bridge"
