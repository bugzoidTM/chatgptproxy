"""Driver da interface web do ChatGPT.

Toda a conversa acontece na UI de verdade (chatgpt.com), num Chromium logado.
A lógica de envio, fim de streaming e remontagem do markdown vem do driver que
já roda em produção no hubbsfield (`/opt/hubbsfield/server.py`), com o
streaming incremental e o tratamento de sessão acrescentados aqui.
"""
import asyncio
import os
import time

from . import config

EDITOR = "#prompt-textarea"
ASSISTANT = '[data-message-author-role="assistant"]'
USUARIO = '[data-message-author-role="user"]'
STOP_BUTTON = '[data-testid="stop-button"]'
RATE_MODAL = '[data-testid="modal-conversation-history-rate-limit"]'
CLICK_TIMEOUT_MS = 10000
# `fill` sem timeout explicito usa os 30s padrao do Playwright. Em 2026-09-10
# esse foi o erro dominante do servico: 77 `Locator.fill: Timeout 30000ms` em
# seis horas, distribuidos IGUALMENTE pelas tres contas (29/26/22) e SO sob
# carga -- ocioso, prompt de 60 mil caracteres passa em 9,4s. Distribuicao
# igual entre contas independentes significa causa compartilhada, entao trocar
# de conta (que era tudo o que o servidor fazia) nao resolvia nada: so queimava
# 30s na proxima aba pelo mesmo motivo.
FILL_TIMEOUT_MS = int(os.environ.get("FILL_TIMEOUT_MS", "25000"))
# Quanto esperar a resposta ANTERIOR terminar quando o composer está ocupado.
BUSY_WAIT_MS = int(os.environ.get("BUSY_WAIT_MS", "120000"))
# `#modal-no-auth-login` entrou depois de 2026-08-21: naquele dia ele ficou por
# cima da caixa de texto sem trazer botao com esses data-testid, entao a sessao
# morta passou por "editor visivel" e o envio so estourou no clique.
LOGIN_MARKERS = ('[data-testid="login-button"], [data-testid="mobile-login-button"], '
                 '#modal-no-auth-login')

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


class Travada(Exception):
    """A aba parou de dar sinal de vida (chamada do Playwright pendurada ou
    prazo absoluto estourado). Só recriar a aba resolve; relogin NÃO ajuda."""


# Trechos de mensagem que provam que a aba/navegador morreu e nenhuma nova
# tentativa NA MESMA PÁGINA vai funcionar. Em 2026-09-13 o renderer da conta3
# foi morto pelo OOM e todo `goto` devolvia "Page crashed" na hora -- 289 vezes,
# com a conta ainda "ready" no rodízio.
_MORTA = (
    "Page crashed",
    "Target crashed",
    "crashed",
    "Target closed",
    "Target page, context or browser has been closed",
    "has been closed",
    "Browser has been closed",
    "Connection closed",
    "Protocol error",
)


def pagina_morta(e: BaseException) -> bool:
    msg = str(e)
    return any(t in msg for t in _MORTA)


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


async def sessao_caiu(page) -> bool:
    """A pagina esta oferecendo login? E o unico sinal honesto de sessao morta.

    Exige o marcador VISIVEL, nao apenas presente no DOM: esta funcao agora roda
    tambem no caminho feliz (todo envio passa por ela), e um botao de login
    escondido no DOM de uma pagina logada tiraria as tres contas do rodizio e
    acordaria o dono a toa. Alarme falso custa mais caro que deteccao tardia.
    """
    try:
        loc = page.locator(LOGIN_MARKERS)
        for i in range(min(await loc.count(), 4)):
            if await loc.nth(i).is_visible():
                return True
    except Exception:
        pass
    return False


async def erro_de_clique(page, e: Exception) -> Exception:
    """Traduz um clique que nao foi no que ele significa.

    Antes de 2026-08-26 o clique estourava em 30s e subia como excecao generica:
    o servidor rodava para a proxima conta sem avisar ninguem, e a conta com a
    sessao morta continuava "ready" no rodizio.
    """
    if await sessao_caiu(page):
        return SessionExpired("sessão caiu: a página está oferecendo login")
    return Blocked(f"a caixa de texto não aceitou o clique: {e}")


async def diagnosticar_editor(page) -> dict:
    """Por que a caixa de texto não aceitou o que a gente ia escrever?

    Existe porque `Locator.fill: Timeout` não distingue as duas causas
    possíveis, e elas pedem tratamentos opostos:

      * a página ainda está GERANDO a resposta anterior — a UI mantém o
        composer desabilitado, e o certo é esperar, não recarregar;
      * a aba travou (SPA em estado ruim depois de navegar no meio de uma
        geração) — aí só recarregar resolve, e esperar é tempo jogado fora.

    Sem este diagnóstico, o serviço só sabia trocar de conta — e trocar de
    conta não resolve nenhuma das duas.
    """
    out = {"gerando": False, "desabilitado": None, "sessao_caiu": False, "erro": None}
    try:
        out["gerando"] = await page.locator(STOP_BUTTON).count() > 0
    except Exception as e:
        out["erro"] = str(e)[:120]
    try:
        out["sessao_caiu"] = await sessao_caiu(page)
    except Exception:
        pass
    try:
        out["desabilitado"] = await page.locator(EDITOR).first.evaluate(
            "el => el.getAttribute('contenteditable') === 'false'"
            " || el.disabled === true"
            " || el.getAttribute('aria-disabled') === 'true'"
        )
    except Exception:
        pass
    return out


async def recuperar_editor(page, diag: dict) -> None:
    """Devolve a aba a um estado em que dá para escrever.

    Gerando: espera terminar (a UI destrava sozinha). Travada: recarrega. Em
    ambos os casos a conversa aberta é preservada — `reload` mantém a mesma
    URL, então as contagens de mensagens que o `ask_stream` mediu continuam
    valendo, tanto na conversa nova quanto na continuação.
    """
    if diag.get("gerando"):
        print("[driver] composer ocupado: aguardando a resposta anterior terminar", flush=True)
        try:
            # Teto próprio, e não o ANSWER_TIMEOUT (600s): esperar a resposta
            # ANTERIOR terminar é razoável por um minuto ou dois; dez minutos
            # seria transformar um composer ocupado em requisição pendurada.
            await page.locator(STOP_BUTTON).first.wait_for(
                state="detached", timeout=BUSY_WAIT_MS)
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        return

    print("[driver] aba travada: recarregando antes de desistir da conta", flush=True)
    try:
        await page.reload(wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT * 1000)
    except Exception:
        pass
    await page.wait_for_timeout(2000)
    await dismiss_modals(page)
    await wait_editor(page)


async def wait_editor(page) -> None:
    """Garante a caixa de texta pronta, resolvendo desafio/sessão pelo caminho."""
    editor = page.locator(EDITOR)
    try:
        await editor.wait_for(state="visible", timeout=config.EDITOR_TIMEOUT * 1000)
        # O editor ficar VISIVEL nao prova que da para escrever nele: com o
        # modal de login por cima ele continua visivel e so o clique estoura,
        # 30s depois, como erro generico -- sem alerta e sem tirar a conta do
        # rodizio (foi assim em 2026-08-21). Conferir o login aqui e o que
        # transforma isso em SessionExpired na hora certa.
        if await sessao_caiu(page):
            raise SessionExpired("sessão caiu: a página está oferecendo login")
        return
    except SessionExpired:
        raise
    except Exception:
        pass

    if await sessao_caiu(page):
        raise SessionExpired("sessão caiu: a página está oferecendo login")

    if not await try_solve_turnstile(page):
        raise Blocked("Cloudflare barrou e o desafio não passou sozinho")
    try:
        await editor.wait_for(state="visible", timeout=config.EDITOR_TIMEOUT * 1000)
    except Exception:
        if await sessao_caiu(page):
            raise SessionExpired("sessão caiu: a página está oferecendo login")
        raise Blocked("a caixa de texto do ChatGPT não apareceu")


async def _ultima_resposta(page):
    """(locator da última resposta, id dela). (None, None) se não há nenhuma.

    Identificar a resposta nova por CONTAGEM não funciona: conversa longa vira
    lista virtualizada e o ChatGPT tira do DOM as mensagens antigas conforme
    acrescenta as novas — o total pode ficar igual, ou até cair, com resposta
    nova na tela. O `data-message-id` é o que identifica de verdade.
    """
    answers = page.locator(ASSISTANT)
    n = await answers.count()
    if n == 0:
        return None, None
    ultima = answers.nth(n - 1)
    try:
        return ultima, await ultima.get_attribute("data-message-id")
    except Exception:
        return ultima, None


async def _texto_da_nova(page, id_antes: str | None, texto_antes: str) -> str:
    """Markdown da resposta nova, ou '' enquanto ela não existir.

    Quando a página não expõe `data-message-id` (mudança de UI), cai para a
    comparação com o texto que já estava lá antes do envio.
    """
    ultima, mid = await _ultima_resposta(page)
    if ultima is None:
        return ""
    if mid is not None and mid == id_antes:
        return ""  # ainda é a resposta anterior
    try:
        texto = await ultima.evaluate(EXTRACT_MARKDOWN)
    except Exception:
        return ""
    if mid is None and texto == texto_antes:
        return ""
    return texto


class Resposta:
    """Carrega o texto final exato. Os deltas do streaming são uma
    aproximação — a UI reescreve trechos enquanto renderiza —, então quem
    precisa do texto certo (chamada não-streaming) lê daqui."""

    def __init__(self):
        self.texto = ""
        self.conta = ""   # qual conta atendeu — vai no system_fingerprint


def _prefixo_comum(a: str, b: str) -> str:
    limite = min(len(a), len(b))
    i = 0
    while i < limite and a[i] == b[i]:
        i += 1
    return a[:i]


async def ask_stream(page, prompt: str, timeout: int | None = None, buf: "Resposta | None" = None):
    """Manda `prompt` na conversa aberta e vai entregando o texto em pedaços.

    Só emite o que já está **estável** (igual em duas leituras seguidas): a UI
    reescreve o bloco enquanto fecha um ``` , e emitir cedo demais duplicava o
    trecho na resposta final.
    """
    timeout = timeout or config.ANSWER_TIMEOUT
    await dismiss_modals(page)
    await wait_editor(page)

    answers = page.locator(ASSISTANT)
    before = await answers.count()
    ultima_antes, id_antes = await _ultima_resposta(page)
    texto_antes = ""
    if ultima_antes is not None and id_antes is None:
        try:
            texto_antes = await ultima_antes.evaluate(EXTRACT_MARKDOWN)
        except Exception:
            texto_antes = ""
    perguntas = page.locator(USUARIO)
    perguntas_antes = await perguntas.count()

    editor = page.locator(EDITOR)

    recuperou = {"ja": False}

    async def _escrever():
        # Timeout curto de proposito: o padrao do Playwright (30s) fazia cada
        # conta queimar meio minuto antes de rodar para a proxima, e o erro
        # saia generico. Se o clique nao vai agora, alguma coisa esta por cima
        # -- e quem diz o que e o diagnostico abaixo, nao a espera.
        try:
            await editor.click(timeout=CLICK_TIMEOUT_MS)
        except Exception as e:
            raise await erro_de_clique(page, e) from e
        # fill() escreve direto no contenteditable: não passa pelo handler de
        # colar (que transformaria prompt grande em ANEXO) nem dispara submit.
        await editor.fill(prompt, timeout=FILL_TIMEOUT_MS)
        await page.wait_for_timeout(300)
        await page.keyboard.press("Enter")

    async def _enviar():
        """Escreve e envia, recuperando a aba UMA vez antes de desistir.

        Desistir aqui custa caro em cascata: o servidor passa para a próxima
        conta (mais 25s), e o cliente ainda tem tentativas próprias — uma
        chamada podia virar nove interações de página, todas esbarrando no
        mesmo composer indisponível. Recuperar a aba resolve na primeira.
        """
        try:
            await _escrever()
            return
        except (SessionExpired, Blocked):
            raise
        except Exception as e:
            if recuperou["ja"]:
                raise
            recuperou["ja"] = True
            diag = await diagnosticar_editor(page)
            if diag.get("sessao_caiu"):
                raise SessionExpired("sessão caiu: a página está oferecendo login") from e
            print(f"[driver] escrita falhou ({str(e)[:80]}); diagnóstico: {diag}", flush=True)
            await recuperar_editor(page, diag)
            try:
                await _escrever()
            except Exception as e2:
                # A mensagem carrega o diagnóstico: da próxima vez que isto
                # aparecer no log, ele diz QUAL das duas causas foi.
                raise Blocked(
                    f"caixa de texto indisponível mesmo após recuperação "
                    f"(gerando={diag.get('gerando')}, desabilitado={diag.get('desabilitado')}): {e2}"
                ) from e2

    # Ao voltar para uma conversa antiga, a caixa de texto fica visível antes de
    # estar funcional e o Enter cai no vazio — a mensagem nunca entra. Conferir
    # que a pergunta apareceu na conversa é a única prova de que foi enviada.
    await _enviar()
    for tentativa in (1, 2):
        try:
            await perguntas.nth(perguntas_antes).wait_for(state="attached", timeout=15000)
            break
        except Exception:
            if tentativa == 1:
                await _enviar()
            else:
                raise RuntimeError("a mensagem não entrou na conversa (interface não respondeu ao envio)")

    stop = page.locator(STOP_BUTTON)

    async def _comecou() -> bool:
        """A resposta começou? (streaming em curso ou já há mensagem NOVA)"""
        if await stop.count() > 0:
            return True
        return bool(await _texto_da_nova(page, id_antes, texto_antes))

    # Enviar logo depois do turno anterior às vezes cai no vazio: a pergunta
    # aparece na conversa (render otimista) e resposta nenhuma vem. Em vez de
    # esperar 150s por algo que não virá, recarrega e reenvia UMA vez.
    espera = time.time() + 30
    while time.time() < espera and not await _comecou():
        await page.wait_for_timeout(500)

    if not await _comecou():
        try:
            await page.reload(wait_until="domcontentloaded",
                              timeout=config.NAV_TIMEOUT * 1000)
            await page.wait_for_timeout(3000)
        except Exception:
            pass
        if not await _texto_da_nova(page, id_antes, texto_antes):  # recarga não revelou nada
            await wait_editor(page)
            await page.wait_for_timeout(1500)
            await _enviar()
            try:
                await stop.wait_for(state="visible", timeout=25000)
            except Exception:
                pass

    enviado = ""     # o que já saiu para o cliente
    anterior = ""    # leitura do poll anterior
    deadline = time.time() + timeout
    comeco_limite = time.time() + config.COMECO_TIMEOUT
    parado = 0
    # Batimento: modelo pensando pode ficar minutos sem texto nenhum, e quem
    # consome este gerador precisa distinguir "pensando" de "aba congelada".
    # Um delta VAZIO de tempos em tempos e o sinal de vida (os consumidores
    # descartam delta vazio; o prazo por passo em server.py conta com ele).
    batimento = time.time() + 10
    while time.time() < deadline:
        if time.time() >= batimento:
            batimento = time.time() + 10
            yield ""
        streaming = await stop.count() > 0
        texto = await _texto_da_nova(page, id_antes, texto_antes)

        estavel = _prefixo_comum(texto, anterior)
        if len(estavel) > len(enviado) and estavel.startswith(enviado):
            yield estavel[len(enviado):]
            enviado = estavel
        anterior = texto

        if not streaming:
            if not texto and time.time() < comeco_limite:
                # ainda nem começou a responder. Desistir aqui (eram ~3s) fazia
                # o proxy declarar "não produziu resposta" e jogar a conversa
                # para outra conta sem motivo.
                parado = 0
            else:
                # o botão some antes de o DOM assentar; confirma texto parado
                parado += 1
                if (parado >= 3 and texto) or parado >= 8:
                    break
        else:
            parado = 0
        await page.wait_for_timeout(config.POLL_MS)
    else:
        raise TimeoutError(f"o ChatGPT não terminou de responder em {timeout}s")

    final = await _texto_da_nova(page, id_antes, texto_antes)
    if buf is not None:
        buf.texto = final.strip()

    if final != enviado:
        if final.startswith(enviado):
            yield final[len(enviado):]
        else:
            # a UI reescreveu algo que já foi emitido; não dá para retirar, então
            # manda a versão boa a partir do ponto em que divergiu
            yield "\n" + final[len(_prefixo_comum(final, enviado)):]

    if not final:
        await dismiss_modals(page)  # levanta RateLimited se o limite entrou no meio
        # Sem esses números o "não produziu resposta" não diz nada: é preciso
        # saber se a resposta nem apareceu, se apareceu vazia, e onde estava.
        depois = await answers.count()
        viu_stop = await stop.count() > 0
        # Screenshot da falha: sem ver a tela, "não produziu resposta" é um
        # beco sem saída — foi o que resolveu o mistério do 403 no qwenproxy.
        try:
            os.makedirs(config.PROFILES_DIR, exist_ok=True)
            await page.screenshot(
                path=os.path.join(config.PROFILES_DIR, f"falha_{int(time.time())}.png")
            )
        except Exception:
            pass
        raise RuntimeError(
            f"o ChatGPT não produziu resposta (respostas antes={before} depois={depois}, "
            f"botão-parar={viu_stop}, esperou={int(time.time() - (deadline - timeout))}s, "
            f"url={page.url})"
        )


async def ask(page, prompt: str, timeout: int | None = None) -> str:
    buf = Resposta()
    async for _ in ask_stream(page, prompt, timeout, buf):
        pass
    return buf.texto
