"""Ponte entre o formato da OpenAI (lista de mensagens, sem estado) e a UI do
ChatGPT (uma conversa com estado, que vive no site).

A ideia central: se as mensagens que chegam são a continuação de uma conversa
que já temos aberta, mandamos SÓ a mensagem nova naquela mesma conversa. É o
que torna um harness usável — sem isso, cada passo do loop reenviaria o
histórico inteiro pela caixa de texto, cada vez mais lento.
"""
import hashlib
import json
from collections import OrderedDict

from . import config

ROLE_LABEL = {
    "system": "INSTRUÇÕES DO SISTEMA",
    # O Continue (e outros clientes) reescreve system→developer para modelos
    # gpt-5/o-series; sem esta linha o prompt chegaria rotulado "### DEVELOPER".
    "developer": "INSTRUÇÕES DO SISTEMA",
    "user": "USUÁRIO",
    "assistant": "ASSISTENTE",
    "tool": "SAÍDA DA FERRAMENTA",
    "function": "SAÍDA DA FERRAMENTA",
}


def content_text(content) -> str:
    """Aceita string ou o formato multimodal em lista da OpenAI."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "image_url":
                    parts.append("[imagem não suportada por este proxy]")
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(content)


def fingerprint(messages: list[dict]) -> str:
    # strip(): o driver guarda a resposta final stripada, mas o cliente acumula
    # os deltas crus — sem normalizar, um \n de borda quebra a continuação e a
    # conversa reabre do zero sem ninguém perceber.
    canon = [
        {"role": m.get("role", "user"), "content": content_text(m.get("content")).strip()}
        for m in messages
    ]
    blob = json.dumps(canon, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def flatten(messages: list[dict]) -> str:
    """Vira a conversa inteira numa única mensagem, para abrir um chat novo."""
    if len(messages) == 1:
        return content_text(messages[0].get("content"))

    blocks = []
    for m in messages:
        role = m.get("role", "user")
        text = content_text(m.get("content"))
        if not text.strip():
            continue
        blocks.append(f"### {ROLE_LABEL.get(role, role.upper())}\n{text}")
    blocks.append(
        "### AGORA RESPONDA\nResponda apenas à última mensagem do usuário, "
        "usando o restante como contexto. Não repita o histórico."
    )
    return "\n\n".join(blocks)


class ConversationCache:
    """Lembra em qual conta e em qual URL de conversa cada histórico parou."""

    def __init__(self, size: int | None = None):
        self.size = size or config.CONVERSATION_CACHE
        self._data: OrderedDict[str, dict] = OrderedDict()

    def get(self, messages: list[dict]) -> dict | None:
        entry = self._data.get(fingerprint(messages))
        if entry:
            self._data.move_to_end(fingerprint(messages))
        return entry

    def put(self, messages: list[dict], account_id: str, url: str) -> None:
        key = fingerprint(messages)
        self._data[key] = {"account": account_id, "url": url}
        self._data.move_to_end(key)
        while len(self._data) > self.size:
            self._data.popitem(last=False)

    def drop_account(self, account_id: str) -> None:
        for k in [k for k, v in self._data.items() if v["account"] == account_id]:
            del self._data[k]

    def __len__(self) -> int:
        return len(self._data)


cache = ConversationCache()
