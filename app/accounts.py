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
# Intervalo da sonda de sessao nas contas PRONTAS (o watcher roda de 10 em 10min;
# 30min e o suficiente para pegar uma sessao morta antes do proximo pedido sem
# ficar batendo em /api/auth/session a toa).
_SONDA_OK = 30 * 60


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
        }


class Pool:
    def __init__(self):
        self._pw = None
        self.accounts: list[Account] = []
        self._rr = 0
        self._free = asyncio.Condition()

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
        asyncio.create_task(self._watch())

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
                detalhe = "; ".join(f"{a.id}={a.status}" for a in self.accounts)
                raise RuntimeError(f"nenhuma conta utilizável ({detalhe})")

            n = len(usable)
            for i in range(n):
                acct = usable[(self._rr + i) % n]
                if not acct.busy:
                    self._rr = (self._rr + i + 1) % n
                    await acct.lock.acquire()
                    acct.requests += 1
                    return acct

            if time.time() >= deadline:
                raise TimeoutError("todas as contas ocupadas; a fila não andou a tempo")
            await asyncio.sleep(0.5)

    def release(self, acct: Account) -> None:
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

    async def _watch(self) -> None:
        """De 10 em 10 minutos reconfere quem está sem sessão — assim uma conta
        relogada volta sozinha ao rodízio.

        E de `_SONDA_OK` em `_SONDA_OK` reconfere também quem está PRONTO: uma
        sessão que morre em silêncio ficava "ready" até alguém pedir alguma
        coisa e a chamada falhar. A sonda é um `/api/auth/session` por conta —
        barata o bastante para não valer a pena adivinhar.
        """
        proxima_sonda = time.time() + _SONDA_OK
        while True:
            await asyncio.sleep(600)
            sondar_prontas = time.time() >= proxima_sonda
            if sondar_prontas:
                proxima_sonda = time.time() + _SONDA_OK
            for acct in self.accounts:
                if acct.busy:
                    continue
                caiu = acct.status in ("no-session", "error")
                if not acct.context:
                    continue
                if not (caiu or (sondar_prontas and acct.status == "ready")):
                    continue
                # Sondar conta pronta segura o lock: `session_email` nao navega
                # (usa `page.request`), mas `refresh` mexe no titulo da janela e
                # nao vale a pena disputar a pagina com uma resposta em curso.
                # Quem ja caiu esta fora do rodizio, ninguem vai pega-la.
                if not caiu:
                    if acct.lock.locked():
                        continue
                    await acct.lock.acquire()
                try:
                    await self.refresh(acct)
                except Exception as e:
                    acct.last_error = str(e)[:300]
                finally:
                    if not caiu:
                        self.release(acct)

    async def stop(self) -> None:
        for acct in self.accounts:
            try:
                if acct.context:
                    await acct.context.close()
            except Exception:
                pass
        if self._pw:
            await self._pw.stop()


pool = Pool()
