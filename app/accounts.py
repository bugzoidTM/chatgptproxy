"""Pool de contas: um Chromium persistente por conta, com rodízio.

Cada conta tem seu próprio `user_data_dir` (perfil persistente) — não
`storage_state` — porque perfil de verdade guarda também o que o Cloudflare
usa para confiar no navegador. Os três rodam com janela na tela virtual :99,
que é o que o dono vê pelo noVNC na hora de logar.
"""
import asyncio
import os
import time

from playwright.async_api import async_playwright

from . import config, driver, notify

# Cascata das janelas na tela 1920x1080: dá para alcançar as três no noVNC.
_WINDOW_SIZE = (1400, 950)
_CASCADE = 110
# Intervalo da sonda de sessao nas contas PRONTAS (a reconferencia de quem caiu
# roda de 10 em 10min; 30min e o suficiente para pegar uma sessao morta antes
# do proximo pedido sem ficar batendo em /api/auth/session a toa).
_SONDA_OK = 30 * 60
_RECONFERE_CAIDAS = 10 * 60
# Passo do watcher. Era 600s quando ele so reconferia sessao; agora ele tambem
# destrava lock preso e estaciona aba ociosa, e isso nao pode esperar 10min.
_TIQUE = 30
# Quanto um lock pode ficar preso alem do prazo da tentativa antes de o watcher
# cancelar a tarefa dona dele. E o cinto de seguranca do prazo em server.py:
# se o prazo funciona, isto nunca dispara.
_LOCK_MAX = config.PRAZO_CONTA + 120


class Account:
    def __init__(self, acct_id: str, slot: int):
        self.id = acct_id
        self.slot = slot
        self.lock = asyncio.Lock()
        self.context = None
        self.page = None
        self.email: str | None = None
        self.status = "starting"  # starting | ready | no-session | error
        self.last_error = ""
        self.rate_limited_until = 0.0
        self.requests = 0
        # Autocorrecao (2026-09-13): quem segura o lock e desde quando (para o
        # watcher cancelar tarefa pendurada), quando a aba foi usada pela
        # ultima vez (estacionamento) e quantas falhas seguidas ela acumulou
        # (recriar a aba sem esperar um crash explicito).
        self.busy_since: float | None = None
        self.task: asyncio.Task | None = None
        self.ultimo_uso = time.time()
        self.falhas_seguidas = 0
        self.recuperacoes = 0
        self.geracao = 0  # sobe a cada aba nova (ver _vigiar)

    @property
    def profile_dir(self) -> str:
        return os.path.join(config.PROFILES_DIR, self.id)

    @property
    def busy(self) -> bool:
        return self.lock.locked()

    @property
    def available(self) -> bool:
        return (
            self.status == "ready"
            and not self.busy
            and time.time() >= self.rate_limited_until
        )

    def info(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "status": self.status,
            "busy": self.busy,
            "requests": self.requests,
            "rate_limited_for": max(0, int(self.rate_limited_until - time.time())),
            "last_error": self.last_error,
            "url": self.page.url if self.page and not self.page.is_closed() else None,
            "busy_for": int(time.time() - self.busy_since) if self.busy_since else 0,
            "idle_for": int(time.time() - self.ultimo_uso),
            "falhas_seguidas": self.falhas_seguidas,
            "recuperacoes": self.recuperacoes,
        }


class Pool:
    def __init__(self):
        self._pw = None
        self.accounts: list[Account] = []
        self._rr = 0
        self._free = asyncio.Condition()
        self.started = False
        self._parando = False

    async def start(self) -> None:
        os.makedirs(config.PROFILES_DIR, exist_ok=True)
        self._pw = await async_playwright().start()
        for slot, acct_id in enumerate(config.ACCOUNTS):
            acct = Account(acct_id, slot)
            self.accounts.append(acct)
            try:
                await self._launch(acct)
            except Exception as e:
                acct.status = "error"
                acct.last_error = str(e)[:300]
                print(f"[pool] {acct.id}: falha ao subir: {e}", flush=True)
        self.started = True
        asyncio.create_task(self._watch())

    def vivo(self) -> bool:
        """O processo ainda se cura sozinho? (lido pelo HEALTHCHECK)"""
        if not self.started:
            return False
        agora = time.time()
        for a in self.accounts:
            if a.busy and a.busy_since and agora - a.busy_since > _LOCK_MAX + 300:
                return False  # o watcher deveria ter cancelado e nao conseguiu
        if self.accounts and all(a.status == "error" for a in self.accounts):
            return False
        return True

    async def _launch(self, acct: Account) -> None:
        os.makedirs(acct.profile_dir, exist_ok=True)
        # Container morto à força deixa o perfil "trancado" e o Chromium se
        # recusa a abrir ("profile appears to be in use ... on another
        # computer"). Como cada perfil só é usado por um processo nosso, o
        # lock remanescente é sempre lixo.
        for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            caminho = os.path.join(acct.profile_dir, lock)
            if os.path.islink(caminho) or os.path.exists(caminho):
                try:
                    os.unlink(caminho)
                    print(f"[pool] {acct.id}: lock antigo removido ({lock})", flush=True)
                except OSError:
                    pass
        x = acct.slot * _CASCADE
        y = acct.slot * (_CASCADE // 2)
        # SEM override de user_agent: o override não mexe nos Client Hints
        # (sec-ch-ua mantém a versão real) e o Cloudflare reprova o Turnstile
        # ao ver UA != sec-ch-ua. Para UA recente, subir o Playwright.
        acct.context = await self._pw.chromium.launch_persistent_context(
            acct.profile_dir,
            headless=False,  # headed sob Xvfb: headless é barrado pelo Cloudflare
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport=None,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                f"--window-position={x},{y}",
                f"--window-size={_WINDOW_SIZE[0]},{_WINDOW_SIZE[1]}",
            ],
        )
        acct.page = await self._main_page(acct)
        self._vigiar(acct, acct.page)
        await acct.page.goto(
            f"{config.BASE_URL}/", wait_until="domcontentloaded",
            timeout=config.NAV_TIMEOUT * 1000,
        )
        await acct.page.wait_for_timeout(2500)
        await self.refresh(acct)

    async def _main_page(self, acct: Account):
        for p in acct.context.pages:
            if not p.is_closed():
                return p
        return await acct.context.new_page()

    def _vigiar(self, acct: Account, page) -> None:
        """Crash do renderer vira recuperacao na hora, nao na proxima falha.

        Sem isto a conta morta ficava "ready" e o rodizio a escolhia para
        sempre (289 `Page crashed` em 2026-09-13). O handler compara a GERACAO
        da aba em que foi instalado, nao a identidade do objeto `page`
        (`context.pages` pode devolver outro wrapper para a mesma aba): a
        recuperacao troca a aba, e o crash de uma aba velha nao pode derrubar
        a nova.
        """
        acct.geracao += 1
        geracao = acct.geracao

        def _crash(*_):
            print(f"[pool] {acct.id}: evento crash (geracao {geracao}, atual {acct.geracao})",
                  flush=True)
            if acct.geracao == geracao:
                self.agendar_recuperacao(acct, "renderer da aba morreu (crash)")
        def _fechou(*_):
            # Navegador inteiro caiu (OOM no processo principal, SIGKILL): nao
            # ha evento de crash de pagina, so o fechamento do contexto. O
            # `_relancar` deliberado tambem passa por aqui -- nesse caso a
            # conta ja esta "recovering" e o agendamento e ignorado.
            if self._parando or acct.geracao != geracao:
                return
            print(f"[pool] {acct.id}: navegador fechou sozinho", flush=True)
            self.agendar_recuperacao(acct, "navegador fechou sozinho")
        try:
            page.on("crash", _crash)
            acct.context.on("close", _fechou)
        except Exception as e:
            print(f"[pool] {acct.id}: nao consegui vigiar a aba: {e}", flush=True)

    async def refresh(self, acct: Account) -> None:
        """Reavalia a sessão da conta e rotula a janela para o noVNC."""
        if not acct.context:
            acct.status = "error"
            return
        acct.page = await self._main_page(acct)
        email = await driver.session_email(acct.page)
        acct.email = email
        if email:
            acct.status = "ready"
            acct.last_error = ""
        else:
            acct.status = "no-session"
            acct.last_error = "sem sessão: precisa de login manual pelo noVNC"
            await notify.alert(
                f"no-session:{acct.id}",
                f"A conta *{acct.id}* está sem sessão no ChatGPT e saiu do rodízio.",
            )
        await self.label_window(acct)

    async def label_window(self, acct: Account) -> None:
        """Põe o ID da conta no título da janela — é assim que o dono sabe qual
        das três janelas é qual no noVNC (e o xdotool a encontra pelo nome)."""
        try:
            await acct.page.evaluate(
                """id => {
                    document.title = '[' + id + '] ' + document.title;
                    if (!window.__cgpTitleGuard) {
                        window.__cgpTitleGuard = setInterval(() => {
                            if (!document.title.startsWith('[' + id + ']'))
                                document.title = '[' + id + '] ' + document.title;
                        }, 2000);
                    }
                }""",
                acct.id,
            )
        except Exception:
            pass

    def get(self, acct_id: str) -> Account | None:
        for a in self.accounts:
            if a.id == acct_id:
                return a
        return None

    def ready_accounts(self) -> list[Account]:
        return [a for a in self.accounts if a.status == "ready"]

    async def acquire(self, timeout: int | None = None) -> Account:
        """Pega a próxima conta livre, em rodízio. Espera se todas estiverem
        ocupadas; falha na hora se nenhuma estiver utilizável."""
        timeout = timeout or config.SLOT_WAIT_TIMEOUT
        deadline = time.time() + timeout
        while True:
            usable = [
                a for a in self.accounts
                if a.status == "ready" and time.time() >= a.rate_limited_until
            ]
            if not usable:
                # Conta subindo ou em recuperacao vai voltar em segundos: vale
                # esperar. So falha na hora se NENHUMA tem chance de voltar.
                pendentes = [a for a in self.accounts if a.status in ("starting", "recovering")]
                if pendentes and time.time() < deadline:
                    await asyncio.sleep(1)
                    continue
                detalhe = "; ".join(f"{a.id}={a.status}" for a in self.accounts)
                raise RuntimeError(f"nenhuma conta utilizável ({detalhe})")

            n = len(usable)
            for i in range(n):
                acct = usable[(self._rr + i) % n]
                if not acct.busy:
                    self._rr = (self._rr + i + 1) % n
                    await self.take(acct)
                    return acct

            if time.time() >= deadline:
                raise TimeoutError("todas as contas ocupadas; a fila não andou a tempo")
            await asyncio.sleep(0.5)

    async def take(self, acct: Account) -> None:
        """Segura o lock de uma conta para a tarefa atual e anota desde quando.

        Toda posse de conta para RESPONDER passa por aqui (rodizio e
        continuacao): e o que permite ao watcher saber quem esta preso e
        cancelar a tarefa certa em vez de soltar o lock por baixo dela.
        """
        await acct.lock.acquire()
        acct.busy_since = time.time()
        acct.task = asyncio.current_task()
        acct.requests += 1
        acct.ultimo_uso = time.time()

    def release(self, acct: Account) -> None:
        acct.busy_since = None
        acct.task = None
        acct.ultimo_uso = time.time()
        if acct.lock.locked():
            acct.lock.release()

    def mark_ok(self, acct: Account) -> None:
        """Requisicao respondeu: apaga o erro antigo.

        Sem isto o `last_error` fica colado para sempre -- em 26/08/2026 as tres
        contas exibiam no `admin.sh contas` um erro de CINCO DIAS antes enquanto
        respondiam normalmente. Erro velho na tela e ruido exatamente na hora em
        que se esta diagnosticando.
        """
        acct.last_error = ""
        acct.falhas_seguidas = 0

    def registrar_falha(self, acct: Account, e: BaseException) -> None:
        """Falha que NAO e sessao nem limite: anota e decide se a aba e recriada.

        Recria na hora quando a pagina esta comprovadamente morta (crash,
        target fechado) ou pendurada (`Travada`); e tambem quando as falhas
        seguidas passam de FALHAS_PARA_RECRIAR -- SPA em estado ruim que o
        reload do driver nao consertou. Trocar de conta, que era tudo o que o
        servidor sabia fazer, nunca cura a conta que ficou para tras.
        """
        acct.last_error = str(e)[:300]
        acct.falhas_seguidas += 1
        morta = isinstance(e, driver.Travada) or driver.pagina_morta(e)
        fechada = bool(acct.page) and acct.page.is_closed()
        if morta or fechada:
            self.agendar_recuperacao(acct, f"aba morta/pendurada: {str(e)[:120]}")
        elif acct.falhas_seguidas >= config.FALHAS_PARA_RECRIAR:
            self.agendar_recuperacao(
                acct, f"{acct.falhas_seguidas} falhas seguidas: {str(e)[:120]}")

    def mark_rate_limited(self, acct: Account, seconds: int = 600) -> None:
        acct.rate_limited_until = time.time() + seconds
        acct.last_error = f"limite da OpenAI; fora do rodízio por {seconds}s"

    async def mark_no_session(self, acct: Account) -> None:
        acct.status = "no-session"
        acct.last_error = "sessão expirada: precisa de login manual pelo noVNC"
        await notify.alert(
            f"expired:{acct.id}",
            f"A sessão da conta *{acct.id}* expirou no meio do uso e ela saiu do rodízio.",
        )

    # ------------------------------------------------------------------
    # Autocorrecao: recriar aba, destravar lock preso, estacionar ociosa
    # ------------------------------------------------------------------

    def agendar_recuperacao(self, acct: Account, motivo: str) -> None:
        """Tira a conta do rodizio JA e recria a aba em segundo plano.

        Em segundo plano porque quem chama esta no meio de uma resposta (ou e
        um handler de evento) e nao pode esperar 20-60s; e o status muda antes
        de a tarefa rodar para que o `acquire` pare de escolher a conta no
        mesmo instante. Idempotente: recuperacao em curso nao e duplicada.
        """
        if acct.status == "recovering":
            return
        acct.status = "recovering"
        acct.last_error = f"recuperando: {motivo}"[:300]
        print(f"[pool] {acct.id}: recuperacao agendada ({motivo})", flush=True)
        asyncio.create_task(self._recuperar(acct, motivo))

    async def _recuperar(self, acct: Account, motivo: str) -> None:
        # Espera a tarefa dona do lock sair (ou ser cancelada pelo watcher).
        async with acct.lock:
            acct.recuperacoes += 1
            ok = False
            try:
                ok = await asyncio.wait_for(self._nova_aba(acct), timeout=120)
            except Exception as e:
                print(f"[pool] {acct.id}: aba nova falhou ({str(e)[:120]}); "
                      "relancando o navegador", flush=True)
            if not ok:
                try:
                    ok = await asyncio.wait_for(self._relancar(acct), timeout=240)
                except Exception as e:
                    acct.last_error = f"relancamento falhou: {str(e)[:200]}"
                    print(f"[pool] {acct.id}: relancamento falhou: {e}", flush=True)
            if ok:
                acct.falhas_seguidas = 0
                print(f"[pool] {acct.id}: recuperada ({acct.status}) apos '{motivo}'",
                      flush=True)
                return
            # `refresh` nao rodou (ou falhou): a conta fica em "error" e o
            # watcher tenta de novo em _RECONFERE_CAIDAS. So agora vale acordar
            # o dono -- e SEM a dica de relogin, que aqui nao resolve nada.
            acct.status = "error"
            await notify.alert(
                f"recover:{acct.id}",
                f"A conta *{acct.id}* caiu ({motivo}) e a recuperacao automatica "
                f"falhou: {acct.last_error}. Vou tentar de novo a cada 10min; se "
                "persistir, `./admin.sh contas` e `docker service logs`.",
                relogin=False,
            )

    async def _nova_aba(self, acct: Account) -> bool:
        """Troca a aba morta por uma nova NO MESMO CONTEXTO (mesmo perfil,
        mesma sessao -- nada de relogin). Renderer morto nao derruba o
        navegador: so a aba."""
        if not acct.context:
            return False
        velha = acct.page
        nova = await acct.context.new_page()
        # `_vigiar` sobe a geracao: o crash/close da aba velha deixa de contar.
        acct.page = nova
        self._vigiar(acct, nova)
        if velha is not None and velha is not nova:
            try:
                await asyncio.wait_for(velha.close(), timeout=10)
            except Exception:
                pass
        await nova.goto(f"{config.BASE_URL}/", wait_until="domcontentloaded",
                        timeout=config.NAV_TIMEOUT * 1000)
        await nova.wait_for_timeout(2000)
        await self.refresh(acct)
        return acct.status in ("ready", "no-session")

    async def _relancar(self, acct: Account) -> bool:
        """Navegador inteiro morto (ou irrecuperavel): fecha e sobe de novo a
        partir do perfil em disco. O perfil NUNCA e apagado -- e ele que
        guarda o login."""
        if acct.context:
            try:
                await asyncio.wait_for(acct.context.close(), timeout=20)
            except Exception:
                pass
        acct.context = None
        acct.page = None
        await self._launch(acct)
        return acct.status in ("ready", "no-session")

    async def _destravar_presa(self, acct: Account) -> None:
        """Lock preso alem de _LOCK_MAX: cancela a tarefa dona dele.

        Cancelar (e nao soltar o lock) e o unico jeito seguro: os `finally` da
        tarefa devolvem a conta, e a tarefa pendurada nao volta mais tarde
        para soltar um lock que ja e de outro.
        """
        preso = int(time.time() - acct.busy_since)
        print(f"[pool] {acct.id}: lock preso ha {preso}s; cancelando a tarefa",
              flush=True)
        if acct.task is not None and not acct.task.done():
            acct.task.cancel()
        self.agendar_recuperacao(acct, f"lock preso ha {preso}s")

    async def _estacionar(self, acct: Account) -> None:
        """Aba ociosa vai para about:blank e devolve a memoria do renderer."""
        if acct.lock.locked():
            return
        async with acct.lock:
            try:
                await acct.page.goto("about:blank", timeout=15000)
                await self.label_window(acct)
                acct.ultimo_uso = time.time()  # nao estacionar de novo a cada tique
            except Exception as e:
                print(f"[pool] {acct.id}: estacionar falhou: {str(e)[:120]}", flush=True)
                self.registrar_falha(acct, e)

    async def _watch(self) -> None:
        """Laco de manutencao, a cada _TIQUE segundos:

        - lock preso alem do prazo -> cancela a tarefa e recria a aba;
        - conta em "error" -> tenta recuperar de novo (a cada 10min);
        - conta "no-session" -> reconfere (a cada 10min), assim uma conta
          relogada volta sozinha ao rodizio;
        - conta pronta -> sonda a sessao (a cada 30min): uma sessao que morre
          em silencio ficava "ready" ate alguem pedir alguma coisa e a
          chamada falhar. A sonda e um `/api/auth/session` por conta;
        - conta pronta e ociosa -> estaciona em about:blank.
        """
        proxima_sonda = time.time() + _SONDA_OK
        proxima_reconferencia = time.time() + _RECONFERE_CAIDAS
        while True:
            await asyncio.sleep(_TIQUE)
            try:
                await self._tique(proxima_sonda, proxima_reconferencia)
            except Exception as e:
                print(f"[pool] watcher: {e}", flush=True)
            if time.time() >= proxima_sonda:
                proxima_sonda = time.time() + _SONDA_OK
            if time.time() >= proxima_reconferencia:
                proxima_reconferencia = time.time() + _RECONFERE_CAIDAS

    async def _tique(self, proxima_sonda: float, proxima_reconferencia: float) -> None:
        agora = time.time()
        sondar_prontas = agora >= proxima_sonda
        reconferir = agora >= proxima_reconferencia
        for acct in self.accounts:
            if acct.busy and acct.busy_since and agora - acct.busy_since > _LOCK_MAX:
                await self._destravar_presa(acct)
                continue
            if acct.busy or acct.status in ("starting", "recovering"):
                continue
            if acct.status == "error":
                if reconferir:
                    self.agendar_recuperacao(acct, "nova tentativa periodica")
                continue
            if not acct.context:
                continue
            if acct.status == "no-session":
                if reconferir:
                    async with acct.lock:
                        try:
                            await asyncio.wait_for(self.refresh(acct), timeout=60)
                        except Exception as e:
                            acct.last_error = str(e)[:300]
                continue
            # daqui para baixo: status == "ready"
            if sondar_prontas:
                # Sondar conta pronta segura o lock: `session_email` nao navega
                # (usa `page.request`), mas `refresh` mexe no titulo da janela e
                # nao vale a pena disputar a pagina com uma resposta em curso.
                if acct.lock.locked():
                    continue
                async with acct.lock:
                    try:
                        await asyncio.wait_for(self.refresh(acct), timeout=60)
                    except Exception as e:
                        acct.last_error = str(e)[:300]
                        self.registrar_falha(acct, e)
                continue
            if (config.OCIOSA_ESTACIONAR and agora - acct.ultimo_uso > config.OCIOSA_ESTACIONAR
                    and acct.page and not acct.page.is_closed()
                    and acct.page.url != "about:blank"):
                await self._estacionar(acct)

    async def stop(self) -> None:
        self._parando = True
        for acct in self.accounts:
            try:
                if acct.context:
                    await acct.context.close()
            except Exception:
                pass
        if self._pw:
            await self._pw.stop()


pool = Pool()
