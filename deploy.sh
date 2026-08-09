#!/bin/bash
# Deploy do chatgptproxy. Use SEMPRE este script.
#
# Por que não `docker stack deploy` direto: o swarm compara a especificação do
# serviço, e o texto `image: chatgptproxy:latest` não muda entre builds — então
# o deploy vira um NO-OP silencioso e o container continua com o código velho.
# Já custou uma sessão inteira de depuração em cima de uma correção que nunca
# tinha subido. A tag única por build faz a especificação mudar de verdade.
set -e
cd "$(dirname "$0")"

export TAG="v$(date +%Y%m%d-%H%M%S)"
echo "==> build $TAG"
docker build -q -t "chatgptproxy:$TAG" -t chatgptproxy:latest . >/dev/null

# `stack deploy` (e não `service update --image`) para que mudanças no
# docker-compose.yml — mounts novos, variáveis — também entrem. A TAG única
# garante que a especificação mude e o swarm realmente reinicie.
echo "==> deploy"
set -a; . ./.env; set +a
docker stack deploy -c docker-compose.yml chatgptproxy >/dev/null

echo "==> esperando as contas voltarem"
until curl -s --max-time 15 https://gptproxy.nutef.com/health 2>/dev/null | grep -q '"ready":3'; do
    sleep 10
done

CID=$(docker ps -q -f name=chatgptproxy_chatgptproxy | head -1)
echo "==> no ar: $(docker inspect "$CID" --format '{{.Config.Image}}')"
curl -s https://gptproxy.nutef.com/health | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('   contas prontas:', d['ready'])"
