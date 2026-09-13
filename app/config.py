"""Configuração por variável de ambiente."""
import os

PORT = int(os.environ.get("PORT", "3000"))
API_KEY = os.environ.get("API_KEY", "")

# IDs das contas (um perfil de navegador por ID, em PROFILES_DIR/<id>).
# O ID é um rótulo local — o e-mail real é descoberto pela sessão logada.
ACCOUNTS = [a.strip() for a in os.environ.get("ACCOUNTS", "conta1,conta2,conta3").split(",") if a.strip()]

PROFILES_DIR = os.environ.get("PROFILES_DIR", "/app/profiles")
BASE_URL = os.environ.get("CHATGPT_BASE_URL", "https://chatgpt.com")

DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "gpt-5")
# Modelos anunciados em /v1/models. O slug depois de ":" vai na URL
# (chatgpt.com/?model=<slug>); sem ":" o nome é o próprio slug.
MODELS = [m.strip() for m in os.environ.get(
    "MODELS", "gpt-5,gpt-5-thinking,gpt-5-instant,gpt-4o,o3"
).split(",") if m.strip()]

# Timeouts (segundos)
NAV_TIMEOUT = int(os.environ.get("NAV_TIMEOUT", "60"))
EDITOR_TIMEOUT = int(os.environ.get("EDITOR_TIMEOUT", "30"))
ANSWER_TIMEOUT = int(os.environ.get("ANSWER_TIMEOUT", "600"))
# Quanto esperar a resposta COMEÇAR antes de declarar que não veio nada. Com
# modelo pensando, o primeiro texto demora; desistir cedo derruba a conversa
# para outra conta à toa.
COMECO_TIMEOUT = int(os.environ.get("COMECO_TIMEOUT", "150"))
SLOT_WAIT_TIMEOUT = int(os.environ.get("SLOT_WAIT_TIMEOUT", "900"))
# Botao "parar" visivel e NENHUM texto por este tempo = geracao travada do
# lado da OpenAI (bolinha azul eterna; visto na conta gratis em 2026-09-13,
# em ~metade das chamadas). O driver recarrega para conferir e, se nada
# chegou, desiste da conta (quarentena). Antes esperava os 600s do
# ANSWER_TIMEOUT; reenviar nao ajudava (2 x 180s, mesmo resultado).
# Modelo pensando tambem fica sem texto -- 180s cobre o gpt-5 normal; para
# `thinking` longo, suba por ambiente.
STALL_TIMEOUT = int(os.environ.get("STALL_TIMEOUT", "180"))
# Conta que travou fica este tempo fora do rodizio (a conta gratis
# travava em ~metade das chamadas e cada uma custava 10min de espera).
STALL_COOLDOWN = int(os.environ.get("STALL_COOLDOWN", "600"))
# Prazos da AUTOCORRECAO (2026-09-13). Nem toda chamada do Playwright tem
# timeout: `count()`/`evaluate` num renderer congelado penduram para sempre, e
# foi assim que a conta1 ficou 6h "busy" com o lock preso. Todo uso de conta
# roda sob dois relogios:
#  - PASSO_TIMEOUT: maximo sem NENHUM sinal de vida do driver (ele emite
#    batimentos durante a espera da resposta), acima da maior chamada
#    bloqueante legitima (espera do composer ocupado 120s + reload 60s + editor 30s);
#  - PRAZO_CONTA: teto absoluto de uma tentativa numa conta.
# Estourou qualquer um: a tentativa e cancelada e a aba e RECRIADA.
PASSO_TIMEOUT = int(os.environ.get("PASSO_TIMEOUT", "300"))
PRAZO_CONTA = int(os.environ.get("PRAZO_CONTA", str(ANSWER_TIMEOUT + 400)))
# Aba ociosa por mais que isto vai para about:blank. Um renderer com pagina de
# conversa aberta cresce sem parar (o OOM de 2026-09-13 matou um chrome com
# 4,5 GB de RSS e derrubou a conta3 do rodizio); a pagina em branco devolve a
# memoria, e chat novo/continuacao ja fazem `goto` antes de usar a aba.
OCIOSA_ESTACIONAR = int(os.environ.get("OCIOSA_ESTACIONAR", "300"))
# Falhas seguidas (sem sessao cair) que fazem a aba ser recriada mesmo sem
# crash explicito -- uma SPA em estado ruim que nem o reload consertou, ou um
# renderer lento a ponto de estourar o `goto` (renderer congelado aparece
# assim, nao como chamada pendurada). Recriar a aba custa ~5s e nao perde
# nada, entao vale ser agressivo: 2, nao 3.
FALHAS_PARA_RECRIAR = int(os.environ.get("FALHAS_PARA_RECRIAR", "2"))
POLL_MS = int(os.environ.get("POLL_MS", "400"))

# Quantas conversas abertas guardar para continuar em vez de recomeçar.
CONVERSATION_CACHE = int(os.environ.get("CONVERSATION_CACHE", "64"))

# Teto de caracteres do texto a DIGITAR na caixa do chatgpt.com (0 desliga).
# Prompt acima disso não entra na UI de jeito nenhum — melhor um 400 imediato
# do que as 3 contas falhando devagar, uma por uma.
PROMPT_MAX_CHARS = int(os.environ.get("PROMPT_MAX_CHARS", "120000"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
NOVNC_HINT = os.environ.get(
    "NOVNC_HINT",
    "ssh -L 8093:127.0.0.1:8093 root@147.93.116.186 → http://localhost:8093/vnc.html",
)
