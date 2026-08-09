#!/bin/bash
# Atalhos de administração do chatgptproxy a partir do host da VPS.
#   ./admin.sh contas              — situação das três contas
#   ./admin.sh login conta1        — abre a tela de login e levanta a janela
#   ./admin.sh refresh conta1      — reconfere a sessão e diz qual e-mail entrou
#   ./admin.sh reset conta1        — volta a conta para um chat novo
#   ./admin.sh foto conta1         — salva /tmp/conta1.png
#   ./admin.sh teste               — pergunta de verdade, ponta a ponta
set -e
cd "$(dirname "$0")"

KEY=$(grep '^API_KEY=' .env 2>/dev/null | cut -d= -f2-)
[ -z "$KEY" ] && { echo "não achei API_KEY em .env"; exit 1; }

CID=$(docker ps -q -f name=chatgptproxy_chatgptproxy | head -1)
[ -z "$CID" ] && { echo "container do chatgptproxy não está no ar"; exit 1; }
# gwbridge (172.18.x): é o único IP do container que o host alcança, e ele só
# aparece de dentro — `docker inspect` mostra apenas o da overlay.
IP=$(docker exec "$CID" hostname -I | tr ' ' '\n' | grep -E '^172\.(1[6-9]|2[0-9]|3[01])\.' | head -1)
API="http://${IP}:3000"
H="Authorization: Bearer ${KEY}"

case "$1" in
  # /health público não mostra mais e-mail nem URL de conversa (vazava a
  # identidade do rodízio); o detalhe vem do endpoint autenticado.
  contas)  curl -s -H "$H" "$API/admin/accounts" | python3 -m json.tool ;;
  login)   curl -s -X POST -H "$H" "$API/admin/login/$2" | python3 -m json.tool ;;
  refresh) curl -s -X POST -H "$H" "$API/admin/refresh/$2" | python3 -m json.tool ;;
  reset)   curl -s -X POST -H "$H" "$API/admin/reset/$2" | python3 -m json.tool ;;
  foto)    curl -s -H "$H" "$API/admin/screenshot/$2" -o "/tmp/$2.png" && echo "/tmp/$2.png" ;;
  teste)
    curl -s --max-time 900 -X POST "$API/v1/chat/completions" \
      -H "$H" -H "Content-Type: application/json" \
      -d '{"model":"gpt-5","messages":[{"role":"user","content":"Responda apenas: funcionou"}]}' \
      | python3 -m json.tool ;;
  *) sed -n '2,9p' "$0" ;;
esac
