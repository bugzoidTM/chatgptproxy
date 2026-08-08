#!/bin/bash
# Sobe a tela virtual (onde os navegadores das contas vivem), o noVNC para o
# dono logar à mão, e a API.
set -e

export DISPLAY="${DISPLAY:-:99}"
SCREEN="${XVFB_SCREEN:-1920x1080x24}"

Xvfb "$DISPLAY" -screen 0 "$SCREEN" -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
for i in $(seq 1 40); do
    xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break
    sleep 0.25
done

fluxbox >/tmp/fluxbox.log 2>&1 &
x11vnc -display "$DISPLAY" -forever -shared -nopw -quiet -rfbport 5900 \
    >/tmp/x11vnc.log 2>&1 &
websockify --web /usr/share/novnc 8080 localhost:5900 \
    >/tmp/websockify.log 2>&1 &

echo "[chatgptproxy] tela $DISPLAY pronta; subindo API na 3000" >&2
exec python -m uvicorn app.server:app --host 0.0.0.0 --port "${PORT:-3000}"
