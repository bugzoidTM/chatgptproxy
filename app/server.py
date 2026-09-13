"""Fachada OpenAI para o chatgpt.com, com rodízio de contas.

Não é API paga: cada requisição é digitada num Chromium logado, na interface
de verdade. Vale a mesma regra do qwenproxy — o preço é o relógio, não o
dinheiro: use timeout largo no cliente.
"""
import asyncio
import json
import os
import subprocess
import time
import uuid

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import config, driver
from .accounts import pool
from .conversations import cache, content_text, flatten

app = FastAPI(title="chatgptproxy")


def _require_key(authorization: str | None) -> None:
    if not config.API_KEY:
        return
    if authorization != f"Bearer {config.API_KEY}":
        raise HTTPException(401, "chave inválida")


@app.on_event("startup")
async def _startup():
    asyncio.create_task(pool.start())


@app.on_event("shutdown")
async def _shutdown():
    await pool.stop()


# /health é aberto (o deploy e a task do VS Code dependem disso), então NÃO
# expõe e-mail, URL de conversa nem last_error — isso identifica as contas do
# rodízio para qualquer um com a URL. O detalhe fica em /admin/accounts, com chave.
_HEALTH_PRIVADO = ("email", "url", "last_error")


@app.get("/health")
async def health():
    ready = pool.ready_accounts()
    return {
        "ok": bool(ready),
        # `vivo` e o que o HEALTHCHECK do container le. NAO e "tem conta
        # logada": as tres sem sessao e um caso de login manual, e reiniciar o
        # container nesse estado so mataria a janela em que o dono esta
        # logando. `vivo` cai quando o PROCESSO nao se cura mais sozinho: pool
        # que nao subiu, lock preso alem do que o watcher deveria destravar, ou
        # todas as contas em "error" (recuperacao e relancamento falharam).
        "vivo": pool.vivo(),
        "accounts": [
            {k: v for k, v in a.info().items() if k not in _HEALTH_PRIVADO}
            for a in pool.accounts
        ],
        "ready": len(ready),
        "conversations": len(cache),
    }


# Entrega o harness ao PC do dono sem depender de SSH/scp. Aberto de propósito:
# é um cliente sem credencial nenhuma dentro, e exigir a chave aqui seria
# circular — é por este caminho que a chave chega à máquina dele.
DOWNLOADS = {
    "gptagent.py": ("/app/harness/gptagent.py", "text/x-python; charset=utf-8"),
    "gptagent.exe": ("/app/dist/gptagent.exe", "application/octet-stream"),
    # O hash permite ao instalador pular o download quando o exe não mudou —
    # e é a única verificação de integridade de um binário sem assinatura.
    "gptagent.exe.sha256": ("/app/dist/gptagent.exe.sha256", "text/plain; charset=utf-8"),
    "vscode/config.yaml": ("/app/vscode/config.yaml", "text/yaml; charset=utf-8"),
    "vscode/tasks.json": ("/app/vscode/tasks.json", "application/json; charset=utf-8"),
    "vscode/README.md": ("/app/vscode/README.md", "text/markdown; charset=utf-8"),
    "vscode/COMECE-AQUI.md": ("/app/vscode/COMECE-AQUI.md", "text/markdown; charset=utf-8"),
    # text/plain (não application/*) para `irm ... | iex` receber string pronta.
    "vscode/install.ps1": ("/app/vscode/install.ps1", "text/plain; charset=utf-8"),
}


def _entregar(nome: str) -> Response:
    caminho, tipo = DOWNLOADS[nome]
    if not os.path.isfile(caminho):
        raise HTTPException(404, f"{nome} ainda não foi publicado no servidor")
    with open(caminho, "rb") as f:
        return Response(
            content=f.read(),
            media_type=tipo,
            headers={"Content-Disposition":
                     f'attachment; filename="{os.path.basename(nome)}"'},
        )


@app.get("/gptagent.py")
async def baixar_harness_py():
    return _entregar("gptagent.py")


@app.get("/gptagent.exe")
async def baixar_harness_exe():
    return _entregar("gptagent.exe")


@app.get("/gptagent.exe.sha256")
async def baixar_harness_exe_hash():
    return _entregar("gptagent.exe.sha256")


@app.get("/vscode/{nome}")
async def baixar_vscode(nome: str):
    chave = f"vscode/{nome}"
    if chave not in DOWNLOADS:
        raise HTTPException(404, f"não há {chave} para baixar")
    return _entregar(chave)


@app.get("/v1/models")
async def models(authorization: str | None = Header(None)):
    _require_key(authorization)
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": now, "owned_by": "openai-web"}
            for m in config.MODELS
        ],
    }


# --------------------------------------------------------------------------
# núcleo: resolver uma requisição em deltas de texto
# --------------------------------------------------------------------------

def _model_slug(model: str | None) -> str | None:
    if not model:
        return None
    return model if model in config.MODELS else None


async def _acquire_specific(acct, timeout: int):
    """Espera a conta X ficar livre (continuação de conversa exige a mesma)."""
    deadline = time.time() + timeout
    while acct.busy:
        if time.time() >= deadline:
            raise TimeoutError(f"a conta {acct.id} não liberou a tempo")
        await asyncio.sleep(0.5)
    await pool.take(acct)
    return acct


async def _com_prazo(agen, acct):
    """Consome o gerador de uma tentativa sob dois relógios (config.PASSO_TIMEOUT
    e config.PRAZO_CONTA) e transforma silêncio em `driver.Travada`.

    Existe porque nem toda chamada do Playwright tem timeout: `count()` e
    `evaluate` num renderer congelado penduram para sempre — em 2026-09-13 a
    conta1 ficou 6h "busy" assim, com o lock preso e fora do rodízio. O
    cancelamento entra no `await` pendurado, o gerador morre, e os `finally`
    do chamador devolvem a conta. O driver emite deltas VAZIOS como batimento
    enquanto o modelo pensa; eles contam como sinal de vida e não saem daqui.
    """
    inicio = time.time()
    try:
        while True:
            restante = config.PRAZO_CONTA - (time.time() - inicio)
            if restante <= 0:
                raise driver.Travada(
                    f"{acct.id} estourou o prazo absoluto de {config.PRAZO_CONTA}s")
            passo = min(config.PASSO_TIMEOUT, restante)
            try:
                delta = await asyncio.wait_for(agen.__anext__(), timeout=passo)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                if time.time() - inicio >= config.PRAZO_CONTA:
                    raise driver.Travada(
                        f"{acct.id} estourou o prazo absoluto de {config.PRAZO_CONTA}s")
                raise driver.Travada(
                    f"{acct.id} ficou {int(passo)}s sem sinal de vida "
                    f"(chamada do navegador pendurada); aba será recriada")
            if delta:
                yield delta
    finally:
        try:
            await asyncio.wait_for(agen.aclose(), timeout=5)
        except BaseException:
            pass


async def _tentativa(acct, prompt: str, buf: driver.Resposta, url: str | None = None,
                     slug: str | None = None):
    """Uma tentativa numa conta: posiciona a aba (conversa antiga ou chat
    novo) e responde. Fica num gerador só para caber inteira no prazo."""
    if url is not None:
        if acct.page.url != url:
            await acct.page.goto(url, wait_until="domcontentloaded",
                                 timeout=config.NAV_TIMEOUT * 1000)
            await acct.page.wait_for_timeout(1500)
    else:
        await driver.open_new_chat(acct.page, slug)
    async for delta in driver.ask_stream(acct.page, prompt, buf=buf):
        yield delta


async def _answer(messages: list[dict], model: str | None, buf: driver.Resposta | None = None):
    """Gera os deltas da resposta. Continua a conversa quando dá, senão abre
    uma nova; troca de conta sozinho quando a escolhida falha.

    `buf` recebe o texto final exato — os deltas são aproximação (ver
    driver.Resposta)."""
    buf = buf if buf is not None else driver.Resposta()
    slug = _model_slug(model)
    last = messages[-1] if messages else {}

    # 1) tentativa de continuar a mesma conversa
    if last.get("role") == "user" and len(messages) > 1:
        entry = cache.get(messages[:-1])
        if entry:
            acct = pool.get(entry["account"])
            if acct and acct.status == "ready" and time.time() >= acct.rate_limited_until:
                try:
                    await _acquire_specific(acct, 300)
                except TimeoutError:
                    acct = None
                if acct:
                    try:
                        buf.conta = f"{acct.id}/continua"
                        tentativa = _tentativa(
                            acct, content_text(last.get("content")), buf, url=entry["url"])
                        async for delta in _com_prazo(tentativa, acct):
                            yield delta
                        pool.mark_ok(acct)
                        cache.put(
                            messages + [{"role": "assistant", "content": buf.texto}],
                            acct.id, acct.page.url,
                        )
                        return
                    except driver.RateLimited:
                        pool.mark_rate_limited(acct)
                        print(f"[answer] {acct.id}: limite da OpenAI na continuação", flush=True)
                    except driver.SessionExpired:
                        await pool.mark_no_session(acct)
                        cache.drop_account(acct.id)
                        print(f"[answer] {acct.id}: sessão caiu na continuação", flush=True)
                    except Exception as e:
                        pool.registrar_falha(acct, e)
                        print(f"[answer] {acct.id}: continuação falhou: {e}", flush=True)
                    finally:
                        pool.release(acct)
                    # caiu aqui: recomeça do zero noutra conta, logo abaixo

    # 2) conversa nova, com o histórico achatado
    prompt = flatten(messages)
    tentativas = max(1, len(pool.accounts))
    erro = None
    for _ in range(tentativas):
        acct = await pool.acquire()
        try:
            buf.conta = f"{acct.id}/novo"
            async for delta in _com_prazo(_tentativa(acct, prompt, buf, slug=slug), acct):
                yield delta
            pool.mark_ok(acct)
            cache.put(
                messages + [{"role": "assistant", "content": buf.texto}],
                acct.id, acct.page.url,
            )
            return
        except driver.RateLimited as e:
            pool.mark_rate_limited(acct)
            erro = e
        except driver.SessionExpired as e:
            await pool.mark_no_session(acct)
            cache.drop_account(acct.id)
            erro = e
        except driver.Blocked as e:
            pool.registrar_falha(acct, e)
            erro = e
            print(f"[answer] {acct.id}: bloqueado: {e}", flush=True)
        except Exception as e:
            # Inclui `Travada` e pagina morta: `registrar_falha` decide se a
            # aba e recriada -- a proxima conta e tentada em seguida, mas a
            # que falhou nao fica apodrecendo no rodizio.
            pool.registrar_falha(acct, e)
            erro = e
            print(f"[answer] {acct.id}: chat novo falhou: {e}", flush=True)
        finally:
            pool.release(acct)
    raise RuntimeError(f"todas as contas falharam: {erro}")


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)  # estimativa: a UI não informa tokens


def _checar_teto(messages: list[dict]) -> None:
    """Rejeita antes de tocar nas contas o prompt que a UI não vai aceitar.

    Sem isto, um histórico grande demais ocupa as 3 contas em sequência (cada
    uma falhando devagar ao digitar) para no fim devolver um erro genérico.
    Continuação digita só a última mensagem; chat novo digita o flatten inteiro.
    """
    if not config.PROMPT_MAX_CHARS:
        return
    if len(messages) > 1 and cache.get(messages[:-1]):
        texto = content_text(messages[-1].get("content"))
    else:
        texto = flatten(messages)
    if len(texto) > config.PROMPT_MAX_CHARS:
        raise HTTPException(400, (
            f"prompt de {len(texto)} caracteres excede o teto de "
            f"{config.PROMPT_MAX_CHARS} da caixa de texto do chatgpt.com; "
            "encurte o histórico ou o contexto anexado"
        ))


@app.post("/v1/chat/completions")
async def chat_completions(body: dict = Body(...), authorization: str | None = Header(None)):
    _require_key(authorization)
    messages = body.get("messages") or []
    if not messages:
        raise HTTPException(400, "body precisa de 'messages'")
    _checar_teto(messages)
    model = body.get("model") or config.DEFAULT_MODEL
    stream = bool(body.get("stream"))
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    prompt_tokens = _tokens(json.dumps(messages, ensure_ascii=False))

    if not stream:
        buf = driver.Resposta()
        try:
            async for _ in _answer(messages, model, buf):
                pass
        except Exception as e:
            raise _http_error(e)
        text = buf.texto
        return {
            "id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "system_fingerprint": buf.conta,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": _tokens(text),
                "total_tokens": prompt_tokens + _tokens(text),
            },
        }

    async def sse():
        def chunk(delta: dict, finish=None) -> str:
            payload = {
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        # A fila desacopla a produção dos deltas do envio: entre o aceite e o
        # primeiro texto podem passar minutos (fila de conta + navegação +
        # modelo pensando), e um intermediário com idle timeout mataria a
        # conexão em silêncio. A cada 15 s sem delta sai um chunk vazio — todo
        # cliente OpenAI ignora delta {} — só para manter bytes fluindo.
        # NÃO usar comentário SSE (": ping"): o parser do Continue trata essa
        # linha como fim de stream.
        fila: asyncio.Queue = asyncio.Queue()

        async def bombear():
            try:
                async for delta in _answer(messages, model):
                    await fila.put(("delta", delta))
                await fila.put(("fim", None))
            except Exception as e:
                await fila.put(("erro", e))

        tarefa = asyncio.create_task(bombear())
        yield chunk({"role": "assistant", "content": ""})
        try:
            while True:
                try:
                    tipo, valor = await asyncio.wait_for(fila.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield chunk({})
                    continue
                if tipo == "delta":
                    if not valor:
                        continue  # batimento do driver, nao e texto
                    yield chunk({"content": valor})
                elif tipo == "fim":
                    yield chunk({}, finish="stop")
                    yield "data: [DONE]\n\n"
                    return
                else:  # erro
                    e = valor
                    # Chunk de texto ANTES do objeto de erro: o objeto derruba
                    # a requisição no cliente, mas some da tela — o chunk deixa
                    # a causa visível na própria mensagem truncada do chat.
                    yield chunk({"content": f"\n\n⚠️ [chatgptproxy] {e}"})
                    err = {"error": {"message": str(e), "type": _error_type(e)}}
                    yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
        finally:
            # Cliente desconectou (ou terminamos): interrompe o navegador e
            # devolve a conta ao rodízio via os finally do _answer.
            tarefa.cancel()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _error_type(e: Exception) -> str:
    if isinstance(e, driver.RateLimited):
        return "rate_limit_error"
    if isinstance(e, driver.SessionExpired):
        return "authentication_error"
    return "server_error"


def _http_error(e: Exception) -> HTTPException:
    if isinstance(e, driver.RateLimited):
        return HTTPException(429, str(e))
    if isinstance(e, driver.SessionExpired):
        return HTTPException(503, f"{e} — relogin: {config.NOVNC_HINT}")
    if isinstance(e, TimeoutError):
        return HTTPException(504, str(e))
    if isinstance(e, driver.Travada):
        return HTTPException(503, str(e))
    return HTTPException(502, str(e))


# --------------------------------------------------------------------------
# admin: login manual pelo noVNC
# --------------------------------------------------------------------------

@app.get("/admin/accounts")
async def admin_accounts(authorization: str | None = Header(None)):
    _require_key(authorization)
    return [a.info() for a in pool.accounts]


@app.post("/admin/login/{acct_id}")
async def admin_login(acct_id: str, authorization: str | None = Header(None)):
    """Traz a janela desta conta para a frente na tela :99 e abre a tela de
    login. O dono loga à mão pelo noVNC; depois chama /admin/refresh."""
    _require_key(authorization)
    acct = pool.get(acct_id)
    if not acct:
        raise HTTPException(404, f"conta desconhecida: {acct_id}")
    if acct.busy:
        raise HTTPException(409, "conta ocupada respondendo; tente em instantes")
    async with acct.lock:
        await acct.page.goto(
            f"{config.BASE_URL}/auth/login", wait_until="domcontentloaded",
            timeout=config.NAV_TIMEOUT * 1000,
        )
        await acct.page.wait_for_timeout(1500)
        await pool.label_window(acct)
        try:  # levanta a janela certa entre as três
            subprocess.run(
                ["xdotool", "search", "--name", f"\\[{acct.id}\\]", "windowactivate"],
                timeout=10, capture_output=True,
            )
        except Exception:
            pass
    return {
        "ok": True,
        "conta": acct.id,
        "instrucoes": (
            f"Abra o noVNC ({config.NOVNC_HINT}), procure a janela com título "
            f"'[{acct.id}]' e faça o login. Depois chame POST /admin/refresh/{acct.id} "
            "para conferir qual e-mail entrou e devolver a conta ao rodízio."
        ),
    }


@app.post("/admin/refresh/{acct_id}")
async def admin_refresh(acct_id: str, authorization: str | None = Header(None)):
    """Reconfere a sessão. Devolve o e-mail REAL logado — é o que impede duas
    janelas de acabarem na mesma conta e o rodízio virar ficção."""
    _require_key(authorization)
    acct = pool.get(acct_id)
    if not acct:
        raise HTTPException(404, f"conta desconhecida: {acct_id}")
    if acct.busy:
        raise HTTPException(409, "conta ocupada respondendo; tente em instantes")
    async with acct.lock:
        await acct.page.goto(
            f"{config.BASE_URL}/", wait_until="domcontentloaded",
            timeout=config.NAV_TIMEOUT * 1000,
        )
        await acct.page.wait_for_timeout(2000)
        await pool.refresh(acct)
    info = acct.info()
    outras = [a.email for a in pool.accounts if a is not acct and a.email]
    if acct.email and acct.email in outras:
        info["aviso"] = (
            f"ATENÇÃO: {acct.email} já está em outra janela — duas contas iguais "
            "não dão rodízio nenhum. Faça logout e entre com outro e-mail."
        )
    return info


@app.get("/admin/screenshot/{acct_id}")
async def admin_screenshot(acct_id: str, authorization: str | None = Header(None)):
    _require_key(authorization)
    acct = pool.get(acct_id)
    if not acct:
        raise HTTPException(404, f"conta desconhecida: {acct_id}")
    png = await acct.page.screenshot(full_page=False)
    return Response(content=png, media_type="image/png")


@app.get("/admin/stack/{acct_id}")
async def admin_stack(acct_id: str, authorization: str | None = Header(None)):
    """Pilha da tarefa que segura a conta. E o que diz QUAL chamada do
    Playwright pendurou -- sem isto, "ficou 300s sem sinal de vida" nao aponta
    para lugar nenhum."""
    _require_key(authorization)
    acct = pool.get(acct_id)
    if not acct:
        raise HTTPException(404, f"conta desconhecida: {acct_id}")
    if acct.task is None or acct.task.done():
        return {"busy": acct.busy, "stack": None}
    # `Task.get_stack()` so mostra o quadro de cima; o `await` pendurado esta
    # no fundo da cadeia cr_await/ag_await, e e ele que interessa.
    frames = []
    obj = acct.task.get_coro()
    visto = 0
    while obj is not None and visto < 60:
        visto += 1
        fr = getattr(obj, "cr_frame", None) or getattr(obj, "ag_frame", None) \
            or getattr(obj, "gi_frame", None)
        if fr is not None:
            frames.append(f"{fr.f_code.co_filename.split('/')[-1]}:{fr.f_lineno} {fr.f_code.co_name}")
        obj = getattr(obj, "cr_await", None) or getattr(obj, "ag_await", None) \
            or getattr(obj, "gi_yieldfrom", None)
        if obj is not None and not hasattr(obj, "cr_frame") and not hasattr(obj, "ag_frame") \
                and not hasattr(obj, "gi_frame"):
            frames.append(f"<{type(obj).__name__}>")
            # Future/Task: segue a cadeia se for Task
            obj = obj.get_coro() if hasattr(obj, "get_coro") else None
    return {
        "busy": acct.busy,
        "busy_for": int(time.time() - acct.busy_since) if acct.busy_since else 0,
        "frames": frames,
    }


@app.post("/admin/recover/{acct_id}")
async def admin_recover(acct_id: str, authorization: str | None = Header(None)):
    """Recria a aba da conta (mesmo perfil, sem relogin) e espera o resultado.
    E o mesmo caminho que o proxy usa sozinho em crash/aba pendurada."""
    _require_key(authorization)
    acct = pool.get(acct_id)
    if not acct:
        raise HTTPException(404, f"conta desconhecida: {acct_id}")
    pool.agendar_recuperacao(acct, "pedido pelo admin")
    prazo = time.time() + 400
    while acct.status == "recovering" and time.time() < prazo:
        await asyncio.sleep(1)
    return acct.info()


@app.post("/admin/reset/{acct_id}")
async def admin_reset(acct_id: str, authorization: str | None = Header(None)):
    """Volta a conta para um chat novo (destrava aba presa em modal/desafio)."""
    _require_key(authorization)
    acct = pool.get(acct_id)
    if not acct:
        raise HTTPException(404, f"conta desconhecida: {acct_id}")
    async with acct.lock:
        await driver.open_new_chat(acct.page, None)
        cache.drop_account(acct.id)
    return {"ok": True, "url": acct.page.url}


@app.exception_handler(RuntimeError)
async def _runtime_error(request, exc):
    return JSONResponse(
        status_code=503,
        content={"error": {"message": str(exc), "type": "server_error"}},
    )


_TIPO_POR_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    404: "invalid_request_error",
    409: "invalid_request_error",
    429: "rate_limit_error",
    503: "server_error",
    504: "server_error",
}


@app.exception_handler(HTTPException)
async def _http_exception(request, exc: HTTPException):
    """Envelope da OpenAI ({"error": {...}}) em vez do {"detail": ...} do
    FastAPI — os SDKs openai extraem error.message; com "detail" o usuário vê
    só "Bad Request" e fica sem a causa."""
    return JSONResponse(
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
        content={"error": {
            "message": str(exc.detail),
            "type": _TIPO_POR_STATUS.get(exc.status_code, "api_error"),
            "code": exc.status_code,
        }},
    )
