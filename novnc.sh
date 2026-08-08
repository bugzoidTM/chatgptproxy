#!/bin/bash
# Abre o noVNC do chatgptproxy só no loopback da VPS, para o dono logar as
# contas por túnel SSH. A porta não fica publicada no swarm justamente para
# não expor um VNC sem senha na internet.
set -e

PORTA="${1:-8093}"

CID=$(docker ps -q -f name=chatgptproxy_chatgptproxy | head -1)
[ -z "$CID" ] && { echo "container do chatgptproxy não está no ar"; exit 1; }

# O IP que o host alcança é o do docker_gwbridge (172.18.x) — e ele NÃO
# aparece em `docker inspect` para serviço de swarm; só de dentro do container.
IP=$(docker exec "$CID" hostname -I | tr ' ' '\n' | grep -E '^172\.(1[6-9]|2[0-9]|3[01])\.' | head -1)
[ -z "$IP" ] && { echo "não achei o IP do container"; exit 1; }

command -v socat >/dev/null || { echo "instale o socat: apt install -y socat"; exit 1; }
pkill -f "TCP-LISTEN:${PORTA},bind=127.0.0.1" 2>/dev/null || true
nohup socat "TCP-LISTEN:${PORTA},bind=127.0.0.1,reuseaddr,fork" "TCP:${IP}:8080" \
    >/tmp/novnc-tunnel.log 2>&1 &
sleep 1

cat <<EOF
noVNC ligado em 127.0.0.1:${PORTA}  (container ${IP}:8080)

1) No SEU computador, abra o túnel e a tela:
       ssh -L ${PORTA}:127.0.0.1:${PORTA} root@147.93.116.186
       http://localhost:${PORTA}/vnc.html

2) Aqui na VPS, mande a janela da conta para a frente:
       ./admin.sh login conta1

3) Logue à mão no noVNC (janela com título "[conta1]"), e então confirme:
       ./admin.sh refresh conta1

Repita para conta2 e conta3. Fechar o túnel: pkill -f "TCP-LISTEN:${PORTA}"
EOF
