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
