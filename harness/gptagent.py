#!/usr/bin/env python3
"""gptagent — harness mínimo que deixa o ChatGPT (via chatgptproxy) mexer nos
arquivos do seu computador.

Um arquivo só, sem dependências fora da biblioteca padrão. Roda em Windows e
Linux. O modelo não executa nada sozinho: ele PEDE uma ação num bloco ```acao,
este programa executa e devolve a saída. Fora do modo --sim-a-tudo, toda ação
que escreve ou roda comando passa pela sua confirmação.

Uso:
    python gptagent.py --dir C:\\meus-projetos\\site
    python gptagent.py --dir . --sim-a-tudo -p "conserte o bug do login"

Configuração (variáveis de ambiente ou flags):
    GPTAGENT_URL      padrão https://gptproxy.nutef.com/v1
    GPTAGENT_KEY      a chave do proxy
    GPTAGENT_MODEL    padrão gpt-5-instant — na web, o gpt-5 "pensante" leva
                      MINUTOS por passo; para os passos mecânicos de um agente
                      o instant responde em segundos. Use --model gpt-5 quando
                      o problema exigir raciocínio pesado.
"""
import argparse
import difflib
import json
import os
import platform
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# No console do Windows o Python já escreve UTF-8 (PEP 528), mas REDIRECIONADO
# (> arquivo, pipe, CI) cai na ANSI code page com errors=strict — e o primeiro
# "▸" mata o programa com UnicodeEncodeError. Força UTF-8 sempre; o "replace"
# garante que nenhum caractere vindo do modelo derrube o harness.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

VERSAO = "1.3"


def _pasta_do_programa() -> Path:
    """Onde o programa mora — no .exe é a pasta do executável, não a do script
    temporário que o PyInstaller descompacta."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _chave_de_arquivo() -> tuple[str, str]:
    """Chave num `gptagent.key` ao lado do programa ou na pasta atual.

    Existe para o .exe: quem clica num executável não define variável de
    ambiente, e repetir --key a cada uso cansa. Devolve (chave, de onde veio).
    """
    for pasta in (_pasta_do_programa(), Path.cwd()):
        arq = pasta / "gptagent.key"
        try:
            if arq.is_file():
                # utf-8-sig: o Out-File do PowerShell 5.1 grava BOM, e o BOM
                # entraria na chave como caractere invisível — 401 sem pista.
                return arq.read_text(encoding="utf-8-sig").strip(), str(arq)
        except OSError:
            continue
    return "", ""


def _resolver_chave() -> tuple[str, str, str]:
    """Decide a chave e diz de onde veio, avisando quando as duas fontes
    discordam — variável de ambiente esquecida sobrepondo o arquivo recém
    escrito foi um erro que custou três rodadas de depuração."""
    ambiente = os.environ.get("GPTAGENT_KEY", "").strip()
    arquivo, caminho = _chave_de_arquivo()
    conflito = ""
    if ambiente and arquivo and ambiente != arquivo:
        conflito = (
            f"a variável GPTAGENT_KEY (…{ambiente[-4:]}) e o {caminho} "
            f"(…{arquivo[-4:]}) têm chaves DIFERENTES; vale a variável. "
            "Apague a variável se quer usar a do arquivo:  Remove-Item Env:\\GPTAGENT_KEY"
        )
    if ambiente:
        return ambiente, "variável GPTAGENT_KEY", conflito
    if arquivo:
        return arquivo, caminho, conflito
    return "", "", conflito


PADRAO_URL = os.environ.get("GPTAGENT_URL", "https://gptproxy.nutef.com/v1")
PADRAO_KEY, ORIGEM_KEY, CONFLITO_KEY = _resolver_chave()
PADRAO_MODEL = os.environ.get("GPTAGENT_MODEL", "gpt-5-instant")

MAX_SAIDA = 6000        # caracteres de saída devolvidos ao modelo, por ação
MAX_SAIDA_TURNO = 12000  # teto do turno inteiro (várias ações)
MAX_PASSOS = 40         # teto de idas ao modelo por pedido
MAX_ACOES_TURNO = 8     # teto de ações executadas por resposta do modelo
TIMEOUT_SHELL = 300     # segundos

WINDOWS = platform.system() == "Windows"


# ---------------------------------------------------------------- aparência
def _ansi_ok() -> bool:
    """O terminal integrado do VS Code — o ambiente-alvo deste setup — define
    TERM_PROGRAM=vscode, não WT_SESSION; testar só o Windows Terminal deixava
    o diff colorido monocromático exatamente onde ele mais importa. isatty()
    cobre o inverso: nada de código ANSI dentro de arquivo redirecionado."""
    if not sys.stdout.isatty():
        return False
    if not WINDOWS:
        return True
    if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM") == "vscode":
        return True
    try:  # conhost puro: liga o suporte a VT na marra
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        modo = ctypes.c_uint32()
        return bool(k32.GetConsoleMode(h, ctypes.byref(modo))
                    and k32.SetConsoleMode(h, modo.value | 0x0004))
    except Exception:
        return False


_CORES = _ansi_ok()


class C:
    ok = "\033[32m" if _CORES else ""
    warn = "\033[33m" if _CORES else ""
    err = "\033[31m" if _CORES else ""
    dim = "\033[2m" if _CORES else ""
    bold = "\033[1m" if _CORES else ""
    off = "\033[0m" if _CORES else ""


def diz(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------ prompt do sistema
SISTEMA = """Você é um agente de programação que trabalha nos arquivos de um computador real, através de um programa que executa suas ações. Responda sempre em português brasileiro.

## Como agir

Para fazer qualquer coisa, responda APENAS com blocos ```acao — sem texto antes, entre ou depois. Pode mandar UM bloco ou VÁRIOS: eu executo todos NA ORDEM e devolvo as saídas numeradas na mensagem seguinte. Cada ida sua até mim é LENTA (minutos), então AGRUPE no mesmo turno tudo que não depende de saída que você ainda não viu — por exemplo: escrever dois arquivos E rodar os testes é UM turno com três blocos, não três turnos. Se uma ação falhar ou for recusada, as seguintes do turno não executam.

Cada bloco começa com uma linha JSON e pode trazer seções de texto cru depois, cada uma aberta por uma linha `---NOME---`. Dentro das seções NÃO se escapa nada: escreva o conteúdo literal.

REGRA ABSOLUTA: nunca afirme que leu, criou, alterou ou testou algo antes de ter recebido de mim a saída correspondente. Se você não viu a saída, o trabalho não foi feito.

Sua PRIMEIRA resposta já deve ser de blocos ```acao (normalmente `listar` e os `ler` que interessarem, juntos, para conhecer o terreno antes de mudar qualquer coisa). Não responda "entendi" nem descreva planos em prosa.

## Ações

Explorar:
```acao
{"tool": "listar", "caminho": "."}
```
```acao
{"tool": "ler", "caminho": "src/app.py"}
```
```acao
{"tool": "buscar", "padrao": "def login", "caminho": "src"}
```

Criar ou substituir um arquivo inteiro:
```acao
{"tool": "escrever", "caminho": "src/novo.py"}
---CONTEUDO---
print("olá")
```

Alterar um trecho (preferido para arquivo existente — o texto em DE precisa bater EXATAMENTE, uma única vez no arquivo):
```acao
{"tool": "editar", "caminho": "src/app.py"}
---DE---
def login(user):
    return None
---PARA---
def login(user):
    return checar(user)
```

Rodar comando (no diretório do projeto):
```acao
{"tool": "shell", "comando": "python -m pytest -q"}
```

Salvar no git (add + commit + push de uma vez):
```acao
{"tool": "git", "mensagem": "conserta o login vazio"}
```

Terminar — só quando o trabalho estiver pronto E verificado pela saída dos comandos (o `pronto` deve ser o ÚNICO bloco do turno):
```acao
{"tool": "pronto"}
---CONTEUDO---
Resumo do que foi feito e como conferi.
```

## Regras

- Leia antes de escrever. Nunca reescreva um arquivo que você não leu nesta conversa.
- Prefira `editar` a `escrever` em arquivo existente: `escrever` apaga o que havia.
- Caminhos sempre relativos ao diretório do projeto. Sair dele é bloqueado.
- Comandos precisam ser não-interativos (`-y`, `--yes`, `printf`); nunca peça confirmação no terminal.
- Se um comando falhar, leia o erro e tente outro caminho — não repita o mesmo comando.
- Escreva código no estilo do que já existe no projeto.
"""


# ------------------------------------------------------------------ cliente
class ErroProxy(Exception):
    pass


def chamar_modelo(url: str, key: str, model: str, mensagens: list, stream: bool) -> str:
    corpo = json.dumps(
        {"model": model, "messages": mensagens, "stream": stream}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/chat/completions",
        data=corpo,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "Accept": "text/event-stream" if stream else "application/json",
        },
    )
    try:
        # timeout largo de propósito: do outro lado há um navegador digitando
        with urllib.request.urlopen(req, timeout=900) as resp:
            if not stream:
                dados = json.loads(resp.read().decode("utf-8"))
                return dados["choices"][0]["message"]["content"]
            partes = []
            for linha in resp:
                linha = linha.decode("utf-8", "replace").strip()
                if not linha.startswith("data:"):
                    continue
                dado = linha[5:].strip()
                if dado == "[DONE]":
                    break
                try:
                    evt = json.loads(dado)
                except json.JSONDecodeError:
                    continue
                if "error" in evt:
                    raise ErroProxy(evt["error"].get("message", "erro no proxy"))
                delta = (evt.get("choices") or [{}])[0].get("delta", {}).get("content")
                if delta:
                    partes.append(delta)
                    sys.stdout.write(C.dim + delta + C.off)
                    sys.stdout.flush()
            if partes:
                diz()
            return "".join(partes)
    except urllib.error.HTTPError as e:
        detalhe = e.read().decode("utf-8", "replace")[:400]
        if e.code == 401:
            raise ErroProxy(
                f"o proxy recusou a chave (recebeu {len(key)} caracteres, "
                f"terminando em '...{key[-4:]}'). Confira se ela foi COPIADA, "
                "não digitada, e se o gptagent.key não tem espaço sobrando."
            )
        if e.code == 503:
            raise ErroProxy(f"nenhuma conta do ChatGPT disponível agora — {detalhe}")
        raise ErroProxy(f"HTTP {e.code}: {detalhe}")
    except urllib.error.URLError as e:
        raise ErroProxy(f"não consegui falar com o proxy: {e.reason}")


# -------------------------------------------------------------------- parser
# O rótulo `acao` NÃO sobrevive à ida e volta pela interface do ChatGPT: a UI
# renderiza o bloco como <pre> e o nome da linguagem se perde. Por isso a busca
# aceita qualquer bloco cercado e decide pelo conteúdo — se abre com um JSON
# que tem "tool", é ação.
BLOCO = re.compile(r"```([\w+-]*)[ \t]*\n(.*?)```", re.DOTALL)


def extrair_acoes(resposta: str) -> list[dict]:
    """Extrai TODAS as ações da resposta, na ordem: JSON no começo de cada
    bloco + seções ---NOME---.

    Blocos inválidos são descartados, salvo quando não sobra nenhum válido —
    aí o erro de parse volta para o modelo corrigir. Blocos idênticos
    consecutivos são deduplicados: quando a UI redesenha a resposta no meio do
    caminho, o mesmo bloco pode aparecer duas vezes.
    """
    candidatos = []
    for lang, corpo in BLOCO.findall(resposta):
        cabeca = corpo.lstrip()
        if lang.lower() == "acao" or (cabeca.startswith("{") and '"tool"' in cabeca[:200]):
            candidatos.append(corpo)
    if not candidatos:
        return []

    acoes, invalidas = [], []
    for bruto in candidatos:
        acao = _parsear_bloco(bruto)
        if acao is None:
            continue
        if acao.get("tool") == "__erro__":
            invalidas.append(acao)
            continue
        if acoes and acao == acoes[-1]:
            continue  # redesenho da UI duplicou o bloco
        acoes.append(acao)
    if not acoes and invalidas:
        return [invalidas[-1]]
    return acoes


def _parsear_bloco(bruto: str) -> dict | None:
    linhas = bruto.split("\n")

    cabecalho, corpo, i = [], [], 0
    for i, linha in enumerate(linhas):
        if linha.strip().startswith("---") and linha.strip().endswith("---"):
            corpo = linhas[i:]
            break
        cabecalho.append(linha)
    else:
        corpo = []

    try:
        acao = json.loads("\n".join(cabecalho).strip())
    except json.JSONDecodeError as e:
        return {"tool": "__erro__", "erro": f"JSON inválido no bloco de ação: {e}"}
    if not isinstance(acao, dict):
        return {"tool": "__erro__", "erro": "o bloco de ação precisa ser um objeto JSON"}

    nome, acumulado = None, []
    for linha in corpo:
        marca = linha.strip()
        if marca.startswith("---") and marca.endswith("---") and len(marca) > 6:
            if nome:
                acao[nome] = "\n".join(acumulado)
            nome, acumulado = marca.strip("-").strip().lower(), []
        elif nome is not None:
            acumulado.append(linha)
    if nome:
        # A última seção termina na quebra de linha que precede o fecho do
        # bloco; sem tirar essa linha vazia, todo `editar` injeta um espaço a
        # mais no arquivo.
        if acumulado and acumulado[-1] == "":
            acumulado.pop()
        acao[nome] = "\n".join(acumulado)
    return acao


# ---------------------------------------------------------------- ferramentas
class ForaDoProjeto(Exception):
    pass


def resolver(raiz: Path, caminho: str) -> Path:
    alvo = (raiz / (caminho or ".")).resolve()
    if alvo != raiz and raiz not in alvo.parents:
        raise ForaDoProjeto(f"'{caminho}' está fora do diretório do projeto")
    return alvo


IGNORAR = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}


def t_listar(raiz: Path, caminho: str) -> str:
    alvo = resolver(raiz, caminho)
    if not alvo.exists():
        return f"não existe: {caminho}"
    if alvo.is_file():
        return f"{caminho} é arquivo ({alvo.stat().st_size} bytes)"
    linhas = []
    for item in sorted(alvo.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if item.name in IGNORAR or item.name.startswith("."):
            continue
        rel = item.relative_to(raiz).as_posix()
        linhas.append(f"{rel}/" if item.is_dir() else f"{rel}  ({item.stat().st_size}b)")
        if len(linhas) >= 200:
            linhas.append("... (lista truncada em 200 itens)")
            break
    return "\n".join(linhas) or "(diretório vazio)"


def t_ler(raiz: Path, caminho: str, inicio: int = 1, linhas: int = 600) -> str:
    alvo = resolver(raiz, caminho)
    if not alvo.is_file():
        return f"não é um arquivo legível: {caminho}"
    try:
        conteudo = alvo.read_text(encoding="utf-8", errors="replace").split("\n")
    except Exception as e:
        return f"não consegui ler {caminho}: {e}"
    fim = min(len(conteudo), inicio - 1 + linhas)
    trecho = "\n".join(
        f"{n:>5}  {conteudo[n - 1]}" for n in range(max(1, inicio), fim + 1)
    )
    rodape = "" if fim >= len(conteudo) else f"\n... ({len(conteudo) - fim} linhas restantes)"
    return f"{caminho} ({len(conteudo)} linhas)\n{trecho}{rodape}"


def t_buscar(raiz: Path, padrao: str, caminho: str = ".") -> str:
    base = resolver(raiz, caminho)
    try:
        rx = re.compile(padrao)
    except re.error as e:
        return f"expressão inválida: {e}"
    achados = []
    arquivos = [base] if base.is_file() else base.rglob("*")
    for arq in arquivos:
        if not arq.is_file() or any(p in IGNORAR for p in arq.parts):
            continue
        try:
            texto = arq.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for n, linha in enumerate(texto.split("\n"), 1):
            if rx.search(linha):
                achados.append(f"{arq.relative_to(raiz).as_posix()}:{n}: {linha.strip()[:200]}")
                if len(achados) >= 100:
                    return "\n".join(achados) + "\n... (100 resultados; refine a busca)"
    return "\n".join(achados) or "nenhum resultado"


def diff_texto(antes: str, depois: str, nome: str) -> str:
    d = difflib.unified_diff(
        antes.split("\n"), depois.split("\n"),
        fromfile=f"a/{nome}", tofile=f"b/{nome}", lineterm="", n=2,
    )
    return "\n".join(d)


def mostrar_diff(diff: str) -> None:
    if not diff.strip():
        diz(f"{C.dim}(sem mudança){C.off}")
        return
    for linha in diff.split("\n")[:120]:
        cor = C.ok if linha.startswith("+") else C.err if linha.startswith("-") else C.dim
        diz(cor + linha + C.off)


def _decodificar(b: bytes) -> str:
    """git/python/node emitem UTF-8, mas os internos do cmd.exe (dir, findstr)
    emitem na OEM code page (cp850 no pt-BR) — decodificar tudo como UTF-8
    devolvia "Relat�rio" ao modelo, que passava a usar o nome errado."""
    if not b:
        return ""
    tentativas = ("utf-8", "oem") if WINDOWS else ("utf-8",)
    for enc in tentativas:
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", "replace")


def rodar_shell(raiz: Path, comando: str) -> str:
    try:
        r = subprocess.run(
            comando, shell=True, cwd=str(raiz), capture_output=True,
            timeout=TIMEOUT_SHELL,
        )
    except subprocess.TimeoutExpired:
        return f"o comando estourou {TIMEOUT_SHELL}s e foi interrompido"
    stdout, stderr = _decodificar(r.stdout), _decodificar(r.stderr)
    saida = stdout + (("\n[stderr]\n" + stderr) if stderr else "")
    return f"[código de saída {r.returncode}]\n{saida.strip() or '(sem saída)'}"


# ---------------------------------------------------------------------- loop
def confirmar(pergunta: str, auto: bool) -> bool:
    if auto:
        return True
    try:
        resp = input(f"{C.warn}{pergunta} [s/N] {C.off}").strip().lower()
    except EOFError:
        # stdin fechado = recusa segura. KeyboardInterrupt NÃO cai aqui de
        # propósito: Ctrl+C significa "pare tudo", não "recuse e continue" —
        # traduzi-lo em recusa mandava o modelo para MAIS uma rodada de minutos
        # que o usuário estava justamente tentando abortar.
        diz()
        return False
    return resp in ("s", "sim", "y", "yes")


def executar(acao: dict, raiz: Path, auto: bool) -> tuple[str, bool]:
    """Executa a ação. Devolve (saída para o modelo, terminou?)."""
    tool = (acao.get("tool") or "").lower()

    if tool == "__erro__":
        return acao["erro"], False

    try:
        if tool == "listar":
            caminho = acao.get("caminho", ".")
            diz(f"{C.bold}▸ listar{C.off} {caminho}")
            return t_listar(raiz, caminho), False

        if tool == "ler":
            caminho = acao.get("caminho", "")
            diz(f"{C.bold}▸ ler{C.off} {caminho}")
            return t_ler(raiz, caminho, int(acao.get("inicio", 1)), int(acao.get("linhas", 600))), False

        if tool == "buscar":
            padrao = acao.get("padrao", "")
            diz(f"{C.bold}▸ buscar{C.off} /{padrao}/ em {acao.get('caminho', '.')}")
            return t_buscar(raiz, padrao, acao.get("caminho", ".")), False

        if tool == "escrever":
            caminho = acao.get("caminho", "")
            novo = acao.get("conteudo", "")
            alvo = resolver(raiz, caminho)
            antes = alvo.read_text(encoding="utf-8", errors="replace") if alvo.is_file() else ""
            diz(f"{C.bold}▸ escrever{C.off} {caminho}"
                + (f" {C.warn}(SUBSTITUI {len(antes.splitlines())} linhas){C.off}" if antes else " (novo)"))
            mostrar_diff(diff_texto(antes, novo, caminho))
            if not confirmar("aplicar?", auto):
                return "o dono recusou esta alteração; proponha outra coisa ou pergunte", False
            alvo.parent.mkdir(parents=True, exist_ok=True)
            alvo.write_text(novo if novo.endswith("\n") else novo + "\n", encoding="utf-8")
            return f"gravado: {caminho} ({len(novo.splitlines())} linhas)", False

        if tool == "editar":
            caminho = acao.get("caminho", "")
            de, para = acao.get("de"), acao.get("para", "")
            alvo = resolver(raiz, caminho)
            if not alvo.is_file():
                return f"não existe: {caminho}", False
            if de is None:
                return "faltou a seção ---DE--- com o trecho exato a substituir", False
            antes = alvo.read_text(encoding="utf-8", errors="replace")
            ocorrencias = antes.count(de)
            if ocorrencias == 0:
                return (f"o trecho de ---DE--- não existe em {caminho}. "
                        "Releia o arquivo e copie o texto exato, com a mesma indentação."), False
            if ocorrencias > 1:
                return (f"o trecho de ---DE--- aparece {ocorrencias} vezes em {caminho}; "
                        "inclua mais linhas de contexto para ficar único."), False
            depois = antes.replace(de, para)
            diz(f"{C.bold}▸ editar{C.off} {caminho}")
            mostrar_diff(diff_texto(antes, depois, caminho))
            if not confirmar("aplicar?", auto):
                return "o dono recusou esta alteração; proponha outra coisa ou pergunte", False
            alvo.write_text(depois, encoding="utf-8")
            return f"editado: {caminho}", False

        if tool == "shell":
            comando = acao.get("comando", "")
            diz(f"{C.bold}▸ shell{C.off} {C.warn}{comando}{C.off}")
            if not confirmar("rodar?", auto):
                return "o dono recusou rodar este comando; tente outro caminho", False
            return rodar_shell(raiz, comando), False

        if tool == "git":
            msg = acao.get("mensagem", "alterações do gptagent")
            diz(f"{C.bold}▸ git{C.off} commit+push: {msg}")
            if not confirmar("commitar e enviar?", auto):
                return "o dono recusou o commit", False
            saidas = [rodar_shell(raiz, "git add -A"),
                      rodar_shell(raiz, f'git commit -m "{msg}"'),
                      rodar_shell(raiz, "git push")]
            return "\n\n".join(saidas), False

        if tool == "pronto":
            return acao.get("conteudo", "").strip() or "(sem resumo)", True

        return (f"ação desconhecida: '{tool}'. Use listar, ler, buscar, escrever, "
                "editar, shell, git ou pronto."), False

    except ForaDoProjeto as e:
        return f"bloqueado: {e}", False
    except Exception as e:
        return f"a ação falhou: {type(e).__name__}: {e}", False


def encurtar(texto: str) -> str:
    if len(texto) <= MAX_SAIDA:
        return texto
    meio = MAX_SAIDA // 2
    return (texto[:meio] + f"\n\n... [cortado: {len(texto) - MAX_SAIDA} caracteres] ...\n\n"
            + texto[-meio:])


def rodar_pedido(pedido: str, cfg, raiz: Path, mensagens: list) -> None:
    mensagens.append({"role": "user", "content": pedido})
    for passo in range(1, MAX_PASSOS + 1):
        diz(f"\n{C.dim}— passo {passo} —{C.off}")
        try:
            resposta = chamar_modelo(cfg.url, cfg.key, cfg.model, mensagens, cfg.stream)
        except ErroProxy as e:
            diz(f"{C.err}proxy: {e}{C.off}")
            return
        except KeyboardInterrupt:
            diz(f"\n{C.warn}interrompido{C.off}")
            return
        if not resposta.strip():
            diz(f"{C.err}o modelo respondeu vazio{C.off}")
            return
        mensagens.append({"role": "assistant", "content": resposta})

        acoes = extrair_acoes(resposta)
        if not acoes:
            if not cfg.stream:
                diz(resposta.strip())
            mensagens.append({"role": "user", "content":
                "Você respondeu em prosa. Mande blocos ```acao com as próximas ações "
                "(pode ser mais de um), ou o bloco `pronto` se o trabalho acabou."})
            continue

        # Executa o LOTE inteiro na ordem — cada ida ao modelo custa minutos,
        # então o turno carrega quantas ações o modelo conseguiu agrupar.
        saidas = []
        for n, acao in enumerate(acoes[:MAX_ACOES_TURNO], 1):
            try:
                saida, terminou = executar(acao, raiz, cfg.auto)
            except KeyboardInterrupt:
                diz(f"\n{C.warn}interrompido{C.off}")
                return
            if terminou:
                diz(f"\n{C.ok}✔ {saida}{C.off}")
                return
            diz(f"{C.dim}{saida[:800]}{C.off}" if len(saida) > 800 else f"{C.dim}{saida}{C.off}")
            rotulo = f"### ação {n} ({(acao.get('tool') or '?')})" if len(acoes) > 1 else ""
            saidas.append((rotulo + "\n" if rotulo else "") + encurtar(saida))
            # Falha de parse ou recusa do dono invalida o resto do plano do
            # turno — o modelo precisa VER isso antes de seguir.
            if acao.get("tool") == "__erro__" or saida.startswith("o dono recusou"):
                pulados = len(acoes) - n
                if pulados > 0:
                    saidas.append(f"(as {pulados} ações seguintes deste turno NÃO foram executadas)")
                break
        else:
            if len(acoes) > MAX_ACOES_TURNO:
                saidas.append(f"(teto de {MAX_ACOES_TURNO} ações por turno; as demais não executaram)")

        corpo = "\n\n".join(saidas)
        if len(corpo) > MAX_SAIDA_TURNO:
            corpo = encurtar(corpo[:MAX_SAIDA_TURNO * 2])[:MAX_SAIDA_TURNO]
        mensagens.append({"role": "user", "content": f"SAÍDA:\n```\n{corpo}\n```"})

    diz(f"{C.warn}parei no limite de {MAX_PASSOS} passos{C.off}")


def _sair_com_erro(msg: str) -> int:
    diz(f"{C.err}{msg}{C.off}")
    # Num .exe clicado duas vezes a janela fecharia antes de o erro ser lido.
    if getattr(sys, "frozen", False):
        try:
            input("\nEnter para fechar. ")
        except (EOFError, KeyboardInterrupt):
            pass
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description="ChatGPT mexendo nos seus arquivos, via chatgptproxy")
    p.add_argument("--dir", default=".", help="diretório do projeto (padrão: atual)")
    p.add_argument("--url", default=PADRAO_URL, help=f"endpoint do proxy (padrão: {PADRAO_URL})")
    p.add_argument("--key", default=PADRAO_KEY, help="chave do proxy (ou GPTAGENT_KEY)")
    p.add_argument("--model", default=PADRAO_MODEL)
    p.add_argument("-p", "--pedido", help="executa um pedido e sai (sem modo interativo)")
    p.add_argument("--sim-a-tudo", dest="auto", action="store_true",
                   help="não pergunta antes de escrever arquivo nem rodar comando")
    p.add_argument("--sem-stream", dest="stream", action="store_false",
                   help="não mostra a resposta sendo escrita")
    cfg = p.parse_args()

    raiz = Path(cfg.dir).resolve()
    if not raiz.is_dir():
        return _sair_com_erro(f"não é um diretório: {raiz}")
    if not cfg.key:
        return _sair_com_erro(
            "falta a chave do proxy. Use --key, ou a variável GPTAGENT_KEY, ou "
            f"crie um arquivo 'gptagent.key' com a chave dentro em:\n  {_pasta_do_programa()}"
        )

    diz(f"{C.bold}gptagent {VERSAO}{C.off}  projeto: {raiz}")
    diz(f"{C.dim}modelo {cfg.model} via {cfg.url}"
        + ("  | SEM confirmação (--sim-a-tudo)" if cfg.auto else "  | confirma antes de alterar")
        + f"{C.off}")
    # De onde veio a chave, sempre à vista: quando ela está errada, saber a
    # ORIGEM é o que resolve — o valor sozinho não diz nada.
    usou_flag = cfg.key != PADRAO_KEY
    origem = "--key" if usou_flag else (ORIGEM_KEY or "?")
    diz(f"{C.dim}chave: {len(cfg.key)} caracteres (…{cfg.key[-4:]}) de {origem}{C.off}")
    if CONFLITO_KEY and not usou_flag:  # com --key o conflito não decide nada
        diz(f"{C.warn}atenção: {CONFLITO_KEY}{C.off}")
    if cfg.auto:
        diz(f"{C.warn}atenção: neste modo o modelo escreve arquivos e roda comandos sem perguntar.{C.off}")

    mensagens = [{"role": "system", "content": SISTEMA
                  + f"\n\nDiretório do projeto: {raiz}\nSistema: {platform.system()}"
                  + (" (comandos `shell` rodam no cmd.exe — sintaxe de cmd, não PowerShell)"
                     if WINDOWS else "")}]

    if cfg.pedido:
        rodar_pedido(cfg.pedido, cfg, raiz, mensagens)
        return 0

    diz(f"{C.dim}digite o que quer, ou 'sair'.{C.off}")
    while True:
        try:
            pedido = input(f"\n{C.bold}você ▸{C.off} ").strip()
        except (EOFError, KeyboardInterrupt):
            diz()
            return 0
        if pedido.lower() in ("sair", "exit", "quit"):
            return 0
        if pedido:
            rodar_pedido(pedido, cfg, raiz, mensagens)


if __name__ == "__main__":
    sys.exit(main())
