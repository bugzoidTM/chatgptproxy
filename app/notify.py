"""Aviso no Telegram quando só o relogin manual resolve."""
import time

import httpx

from . import config

_COOLDOWN = 6 * 3600
_last: dict[str, float] = {}


async def alert(key: str, message: str) -> None:
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return
    now = time.time()
    if now - _last.get(key, 0) < _COOLDOWN:
        return
    _last[key] = now
    text = f"🤖 chatgptproxy\n\n{message}\n\nRelogin: {config.NOVNC_HINT}"
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            await cli.post(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text},
            )
    except Exception:
        pass  # aviso não pode derrubar a requisição
