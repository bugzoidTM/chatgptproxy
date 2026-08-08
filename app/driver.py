"""Driver da interface web do ChatGPT.

Toda a conversa acontece na UI de verdade (chatgpt.com), num Chromium logado.
A lógica de envio, fim de streaming e remontagem do markdown vem do driver que
já roda em produção no hubbsfield (`/opt/hubbsfield/server.py`), com o
streaming incremental e o tratamento de sessão acrescentados aqui.
"""
import asyncio
import time

from . import config

EDITOR = "#prompt-textarea"
ASSISTANT = '[data-message-author-role="assistant"]'
STOP_BUTTON = '[data-testid="stop-button"]'
RATE_MODAL = '[data-testid="modal-conversation-history-rate-limit"]'
LOGIN_MARKERS = '[data-testid="login-button"], [data-testid="mobile-login-button"]'

# inner_text descarta as crases dos blocos de código (a UI os renderiza como
# <pre> com rótulo de linguagem). Sem remontar o markdown, o harness não
# consegue distinguir código de prosa.
EXTRACT_MARKDOWN = """el => {
    const md = el.querySelector('.markdown') || el;
    const parts = [];
    for (const node of md.children) {
        if (node.tagName === 'PRE') {
            const code = node.querySelector('code');
            let lang = '';
            if (code) {
                const m = (code.className || '').match(/language-([\\w+-]+)/);
                if (m) lang = m[1];
            }
            const body = (code ? code.innerText : node.innerText).replace(/\\n$/, '');
            parts.push('```' + lang + '\\n' + body + '\\n```');
        } else {
            const t = node.innerText;
            if (t && t.trim()) parts.push(t);
        }
    }
    return parts.length ? parts.join('\\n\\n') : el.innerText;
}"""


class RateLimited(Exception):
    """A própria OpenAI está limitando esta conta."""


class SessionExpired(Exception):
    """A conta não está mais logada — só relogin manual resolve."""


class Blocked(Exception):
    """Cloudflare interpôs desafio e o clique automático não passou."""


async def session_email(page) -> str | None:
    """E-mail da conta logada, ou None se a sessão caiu.

    Lido de /api/auth/session, que é a única fonte confiável de identidade —
    a presença de cookie não prova de qual conta é a sessão.
    """
    try:
        resp = await page.request.get(f"{config.BASE_URL}/api/auth/session", timeout=20000)
        if resp.status != 200:
            return None
        data = await resp.json()
    except Exception:
        return None
    user = (data or {}).get("user") or {}
    return user.get("email")


async def dismiss_modals(page) -> None:
    """Fecha o que intercepta cliques. Levanta RateLimited se o limite estiver ativo."""
    for label in ("Continuar sem fazer login", "Stay logged out", "Fechar", "Close"):
        try:
            loc = page.locator(f'text="{label}"').first
            if await loc.count():
                await loc.click(timeout=2000)
                await page.wait_for_timeout(400)
        except Exception:
            pass

    modal = page.locator(RATE_MODAL)
    if await modal.count():
        btn = page.locator(
            'button:has-text("Entendido"), button:has-text("Got it"), button:has-text("OK")'
        )
        if await btn.count():
            try:
                await btn.first.click(timeout=3000)
                await page.wait_for_timeout(800)
            except Exception:
                pass
        if await modal.count():
            raise RateLimited("ChatGPT está limitando as solicitações nesta conta")


async def try_solve_turnstile(page) -> bool:
    """Clica o checkbox do Turnstile (iframe cross-origin). Só costuma passar
    quando a sessão já é confiável."""
    for _ in range(3):
        clicked = False
        for fr in page.frames:
            if "challenges.cloudflare.com" not in (fr.url or ""):
                continue
            for sel in ('input[type="checkbox"]', "label", "body"):
                try:
                    loc = fr.locator(sel).first
                    if await loc.count():
                        await loc.click(timeout=4000)
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                break
        await page.wait_for_timeout(6000)
        if await page.locator(EDITOR).count():
            return True
        if not any("challenges.cloudflare.com" in (f.url or "") for f in page.frames):
            return True
    return False


async def open_new_chat(page, model: str | None = None) -> None:
    url = f"{config.BASE_URL}/"
    if model:
        url += f"?model={model}"
    await page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT * 1000)
    await page.wait_for_timeout(2500)


async def wait_editor(page) -> None:
    """Garante a caixa de texta pronta, resolvendo desafio/sessão pelo caminho."""
    editor = page.locator(EDITOR)
    try:
        await editor.wait_for(state="visible", timeout=config.EDITOR_TIMEOUT * 1000)
        return
    except Exception:
        pass

    if await page.locator(LOGIN_MARKERS).count():
        raise SessionExpired("sessão caiu: a página está oferecendo login")

    if not await try_solve_turnstile(page):
        raise Blocked("Cloudflare barrou e o desafio não passou sozinho")
    try:
        await editor.wait_for(state="visible", timeout=config.EDITOR_TIMEOUT * 1000)
    except Exception:
        if await page.locator(LOGIN_MARKERS).count():
            raise SessionExpired("sessão caiu: a página está oferecendo login")
        raise Blocked("a caixa de texto do ChatGPT não apareceu")


async def _current_text(page, index: int) -> str:
    """Markdown da resposta de índice `index`, ou '' se ainda não existe."""
    answers = page.locator(ASSISTANT)
    if await answers.count() <= index:
        return ""
    try:
        return await answers.nth(index).evaluate(EXTRACT_MARKDOWN)
    except Exception:
        return ""


async def ask_stream(page, prompt: str, timeout: int | None = None):
    """Manda `prompt` na conversa aberta e vai entregando o texto em pedaços.

    Gera strings (os deltas) e, no fim, o texto completo é a concatenação.
    """
    timeout = timeout or config.ANSWER_TIMEOUT
    await dismiss_modals(page)
    await wait_editor(page)

    answers = page.locator(ASSISTANT)
    before = await answers.count()

    editor = page.locator(EDITOR)
    await editor.click()
    # fill() escreve direto no contenteditable: não passa pelo handler de colar
    # (que transformaria prompt grande em ANEXO) nem dispara o submit.
    await editor.fill(prompt)
    await page.wait_for_timeout(300)
    await page.keyboard.press("Enter")

    stop = page.locator(STOP_BUTTON)
    try:
        await stop.wait_for(state="visible", timeout=25000)
    except Exception:
        pass  # resposta curta pode nem chegar a mostrar o botão de parar

    sent = ""
    deadline = time.time() + timeout
    stable = 0
    while time.time() < deadline:
        streaming = await stop.count() > 0
        text = await _current_text(page, before)
        if len(text) > len(sent) and text.startswith(sent[: len(text)]):
            yield text[len(sent):]
            sent = text
        elif text and text != sent:
            # a UI reescreveu o bloco (acontece ao fechar um ``` ): reenvia tudo
            yield "\n" + text
            sent = text

        if not streaming:
            # o botão some antes do DOM assentar; confirma com o texto parado
            stable += 1
            if stable >= 3 and sent:
                break
            if stable >= 8:
                break
        else:
            stable = 0
        await page.wait_for_timeout(config.POLL_MS)
    else:
        raise TimeoutError(f"o ChatGPT não terminou de responder em {timeout}s")

    final = await _current_text(page, before)
    if final and final != sent:
        if final.startswith(sent):
            yield final[len(sent):]
        else:
            yield "\n" + final
        sent = final

    if not sent:
        await dismiss_modals(page)  # levanta RateLimited se o limite entrou no meio
        raise RuntimeError("o ChatGPT não produziu resposta")


async def ask(page, prompt: str, timeout: int | None = None) -> str:
    parts = []
    async for delta in ask_stream(page, prompt, timeout):
        parts.append(delta)
    return "".join(parts).strip()
