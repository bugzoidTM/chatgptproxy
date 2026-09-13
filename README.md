# chatgptproxy

Fachada **OpenAI-compatible** para o `chatgpt.com`, com **rodízio de 3 contas** —
irmão do [qwenproxy](https://github.com/bugzoidTM/qwenproxy) que já roda nesta
VPS, mas escrito do zero em Python, porque o caminho aqui é outro: o Qwen tem
uma API HTTP interna aproveitável, o ChatGPT não. Aqui **tudo passa pela
interface de verdade**, num Chromium logado.

Não é API paga. O preço é o relógio: cada resposta leva o tempo de o navegador
digitar e o ChatGPT responder. Cliente precisa de **timeout largo**.

## O que ele oferece

| endpoint | o que faz |
|---|---|
| `POST /v1/chat/completions` | streaming e não-streaming, formato OpenAI |
| `GET /v1/models` | lista os modelos anunciados |
| `GET /health` | agregado das 3 contas (aberto, sem chave; **sem** e-mail/URL — o detalhe fica em `/admin/accounts`, com chave) |
| `POST /admin/login/{conta}` | abre a tela de login e levanta a janela no noVNC |
| `POST /admin/refresh/{conta}` | reconfere a sessão e diz **qual e-mail** entrou |
| `POST /admin/reset/{conta}` | devolve a conta a um chat novo |
| `POST /admin/recover/{conta}` | recria a aba (mesmo perfil, sem relogin) — o que o proxy faz sozinho em crash |
| `GET /admin/stack/{conta}` | cadeia de `await` da tarefa que segura a conta — diz QUAL chamada do Playwright pendurou |
| `GET /admin/screenshot/{conta}` | print da aba (para depurar sem noVNC) |

Autenticação: `Authorization: Bearer <API_KEY>` em tudo, menos `/health`.

## Como usar

```bash
curl https://gptproxy.nutef.com/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"gpt-5","messages":[{"role":"user","content":"oi"}]}'
```

Qualquer cliente OpenAI serve, apontando `base_url` para
`https://gptproxy.nutef.com/v1`.

## As duas decisões que importam

**Uma conta = um perfil de navegador persistente.** Não `storageState`, e sim
`user_data_dir` de verdade (`profiles/conta1`, …): perfil guarda também o que o
Cloudflare usa para confiar no navegador. Os três rodam **com janela** numa tela
virtual `:99` — headless é barrado.

**Conversa continuada em vez de histórico reenviado.** A API da OpenAI é sem
estado; a UI do ChatGPT tem estado. O proxy guarda a impressão digital de cada
histórico que já respondeu (`app/conversations.py`); se a próxima chamada é a
continuação de uma conversa aberta, ele manda **só a mensagem nova** naquela
mesma conversa, na mesma conta. Sem isso, um harness em loop reenviaria o
histórico inteiro pela caixa de texto a cada passo, cada vez mais devagar.
Quando não casa com nada, abre chat novo com o histórico achatado.

## Login das contas (a parte manual)

Login do ChatGPT de IP de datacenter não passa em automação: é Cloudflare mais,
quase sempre, conta Google. Então o login é à mão — **uma vez por conta**, e
depois só quando a sessão cair (o proxy avisa no Telegram).

```bash
cd /home/chatgptproxy
./novnc.sh                 # abre o noVNC só no loopback e explica o túnel
./admin.sh login conta1    # levanta a janela "[conta1]" na tela
#   … logue à mão pelo noVNC …
./admin.sh refresh conta1  # confirma QUAL e-mail entrou e devolve ao rodízio
```

O `refresh` existe por um motivo específico: ele lê `/api/auth/session` e mostra
o e-mail real. **Confira sempre** — se duas janelas acabarem na mesma conta, o
"rodízio de 3 contas" vira ficção e ninguém percebe. O proxy avisa quando
detecta e-mail repetido.

## Operação

```bash
./admin.sh contas          # situação das três
./admin.sh teste           # pergunta de verdade, ponta a ponta
./admin.sh foto conta2     # print da aba
./admin.sh recupera conta2 # recria a aba da conta (sem relogin)
./admin.sh pilha conta1    # onde a tarefa que segura a conta está parada
docker service logs -f chatgptproxy_chatgptproxy
```

Deploy: **sempre `./deploy.sh`** (tag única por build; `stack deploy` direto reverte ou vira no-op).

### Autocorreção (2026-09-13) — o que se cura sozinho e o que não

Relogin manual é **só** para sessão expirada de verdade (aviso no Telegram com a
dica do noVNC). Todo o resto o proxy resolve sem ninguém:

| Sintoma | Detecção | Cura |
|---|---|---|
| renderer morto (OOM, `Page crashed`) | evento `crash` da aba | aba nova no mesmo contexto (`_nova_aba`) |
| navegador inteiro morto | evento `close` do contexto | relança do perfil em disco (`_relancar`) |
| aba congelada (chamada do Playwright pendurada) | `PASSO_TIMEOUT` (300s sem sinal de vida) / `PRAZO_CONTA` (1000s) em `_com_prazo` | cancela a tentativa → `Travada` → aba nova |
| lock preso além de `PRAZO_CONTA`+120s | watcher (a cada 30s) | cancela a tarefa dona do lock → aba nova |
| SPA em estado ruim que o reload não conserta | `FALHAS_PARA_RECRIAR` (2) falhas seguidas | aba nova |
| renderer inchando (4,5 GB no OOM de 13/09) | aba ociosa > `OCIOSA_ESTACIONAR` (300s) | `about:blank` |
| processo que não se cura mais | `/health` → `vivo: false` | healthcheck do swarm troca o container (perfis em disco) |

`vivo` **não** cai por falta de sessão: reiniciar nesse estado só mataria a janela em que
o dono está logando pelo noVNC. Durante a recuperação a conta fica `recovering` e o
`acquire` espera em vez de falhar. `admin.sh contas` mostra `busy_for`, `idle_for`,
`falhas_seguidas` e `recuperacoes`.

## Armadilhas já pagas

- **`/dev/shm` de 64 MB** (padrão do Docker) sufoca o Chromium e vira timeout
  que *parece* bloqueio. A stack monta 1 GB de tmpfs — não remova.
- **Não sobrescreva o `user_agent`.** O override não mexe nos Client Hints
  (`sec-ch-ua` mantém a versão real) e o Cloudflare reprova o Turnstile ao ver
  a divergência. Para UA recente, suba a versão do Playwright.
- **Prompt grande vai por `fill()`, nunca colado.** O handler de colar da UI
  transforma texto longo em **anexo**, e aí a resposta ignora metade do pedido.
- **Container morto à força deixa `SingletonLock` no perfil** e o Chromium se
  recusa a abrir. O `_launch` limpa isso sozinho no boot.
- **A porta do noVNC não é publicada.** O `INPUT` desta VPS está `ACCEPT`, e o
  swarm só publica em modo host (0.0.0.0) — publicar deixaria um VNC **sem
  senha** aberto na internet. Por isso o `novnc.sh` faz um túnel de loopback.
- **`certresolver` é `letsencryptresolver`**, não `letsencrypt`.
- **O IP que o host alcança é o do `docker_gwbridge` (172.18.x)** e ele *não*
  aparece no `docker inspect` de serviço swarm — só via `hostname -I` de dentro.

## Harness e VS Code

Em [`harness/`](harness/) tem o `gptagent.py`: um agente de arquivo único que
usa este proxy para ler e editar arquivos no seu computador. Veja o
[README de lá](harness/README.md).

Em [`vscode/`](vscode/) está a integração com o VS Code (extensão Continue +
tarefas do gptagent) e o instalador de um comando para Windows:

```powershell
irm https://gptproxy.nutef.com/vscode/install.ps1 | iex
```

Nunca usou? Comece por [**Do zero ao primeiro pedido**](vscode/COMECE-AQUI.md):
instalar, abrir um projeto novo e chegar ao primeiro resultado, passo a passo.
