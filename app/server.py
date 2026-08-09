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
    await acct.lock.acquire()
    acct.requests += 1
    return acct


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
                        if acct.page.url != entry["url"]:
                            await acct.page.goto(
                                entry["url"], wait_until="domcontentloaded",
                                timeout=config.NAV_TIMEOUT * 1000,
                            )
                            await acct.page.wait_for_timeout(1500)
                        buf.conta = f"{acct.id}/continua"
                        async for delta in driver.ask_stream(
                            acct.page, content_text(last.get("content")), buf=buf
                        ):
                            yield delta
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
                        acct.last_error = str(e)[:300]
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
            await driver.open_new_chat(acct.page, slug)
            async for delta in driver.ask_stream(acct.page, prompt, buf=buf):
                yield delta
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
            acct.last_error = str(e)[:300]
            erro = e
            print(f"[answer] {acct.id}: bloqueado: {e}", flush=True)
        except Exception as e:
            acct.last_error = str(e)[:300]
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
