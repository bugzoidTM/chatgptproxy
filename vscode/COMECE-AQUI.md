# Do zero ao primeiro pedido — projeto novo no VS Code

Guia passo a passo para usar o ChatGPT dentro do VS Code através do
[chatgptproxy](../README.md), num projeto que você está começando agora.
Windows. Leva uns 10 minutos na primeira vez e uns 30 segundos nas seguintes.

Você ganha duas ferramentas, que servem a coisas diferentes:

| | **Continue** | **gptagent** |
|---|---|---|
| o que é | painel de chat dentro do editor | agente que edita arquivos e roda comandos sozinho |
| você usa para | "explique esta função", "refatore este trecho" | "crie o módulo X com testes e rode os testes" |
| onde aparece | barra lateral | terminal integrado (por tarefa ou atalho) |

**Antes de tudo, o combinado sobre velocidade.** Do outro lado não há API paga:
há um Chromium logado no chatgpt.com, numa VPS, com três contas gratuitas em
rodízio. Com o modelo **instantâneo**, uma resposta leva de segundos a dezenas
de segundos; com o **pensante**, minutos. Isto serve para pedir uma tarefa e ir
fazer outra coisa — não serve para digitação assistida em tempo real.

---

## Passo 1 — Instalar (uma vez por computador)

Você precisa da **chave do proxy** (`API_KEY`) — é o mesmo segredo que está no
`.env` do servidor; sem ela nada funciona. Tenha em mãos antes de começar.

Abra o **PowerShell** e rode:

```powershell
irm https://gptproxy.nutef.com/vscode/install.ps1 | iex
```

Ele pergunta a chave, valida com o servidor e faz o resto: extensão Continue,
configuração do Continue com os dois modelos, `gptagent.exe` em
`%LOCALAPPDATA%\Programs\gptagent`, as três tarefas `gptagent:*` como **tarefas
de usuário** (valem em qualquer projeto, sem copiar arquivo nenhum) e desliga
os títulos automáticos de sessão do Continue.

Quer sem prompt e já com o atalho `Ctrl+Alt+G`:

```powershell
& ([scriptblock]::Create((irm https://gptproxy.nutef.com/vscode/install.ps1))) -Chave 'SUA-CHAVE' -Atalho
```

Ele nunca sobrescreve configuração sua sem deixar um backup datado ao lado, e
rodar de novo só atualiza o que mudou.

**Deu certo?** No fim ele imprime a lista de passos e nenhum `ERRO:`.

> **Feche todas as janelas do VS Code e abra de novo.** A extensão recém
> instalada só vale para processo novo. (As tarefas funcionam mesmo sem isso,
> porque o instalador grava o caminho completo do `gptagent.exe`.)

## Passo 2 — Conferir se o serviço está no ar

```powershell
curl.exe -s https://gptproxy.nutef.com/health
```

Tem que vir `"ready":3` — as três contas logadas. Se vier `0`, as sessões do
ChatGPT caíram e alguém precisa refazer o login na VPS; **nada abaixo vai
funcionar até lá**.

> Use `curl.exe` com a extensão: no PowerShell, `curl` puro é apelido de
> `Invoke-WebRequest`, que não entende `-s` e devolve um objeto em vez do JSON.

## Passo 3 — Criar e abrir o projeto

```powershell
mkdir C:\projetos\meu-app
cd C:\projetos\meu-app
git init
code .
```

O `git init` não é enfeite: o agente **escreve arquivos de verdade**. Com o
projeto versionado, você vê cada alteração no painel *Source Control* e desfaz
o que não gostar.

## Passo 4 — Confiar na pasta

Na primeira vez que o VS Code abre uma pasta nova, ele entra em **Modo
Restrito** e mostra uma faixa amarela no topo: **Gerenciar** → **Confiar**.

Isto não é opcional aqui: **em Modo Restrito o VS Code não executa tarefas**.
Se você pular este passo, ao chamar a tarefa ele abre uma caixa perguntando se
você confia nos autores da pasta — confiar ali resolve na hora e dá no mesmo.
Enquanto a pasta não for confiável, o gptagent não roda.

## Passo 5 — Primeiro pedido no chat (Continue)

**1. Abra o painel.** Clique no ícone do **Continue** na barra de atividades, à
esquerda. Pelo teclado é `Ctrl+L` — mas saiba o que ele faz, porque não é
apenas "abrir": `Ctrl+L` começa uma **conversa nova** (e joga a seleção do
editor dentro dela, se houver); com o chat já em foco, `Ctrl+L` de novo
**fecha** a barra lateral — não é defeito. Para voltar à conversa que já estava
aberta, sem limpar nada, use `Ctrl+Shift+L`.

**2. Troque o modo para Chat.** Na barrinha abaixo da caixa de mensagem há dois
seletores. O da esquerda é o **modo**, e ele vem de fábrica em **Agent** —
clique e escolha **Chat** (ou aperte `Ctrl+.`, que cicla entre Chat, Plan e
Agent).

Isto não é preciosismo: *Agent* e *Plan* pedem chamada de ferramenta (`tools`)
ao modelo, e este proxy não devolve ferramenta nenhuma. Se você deixar em
Agent, o primeiro pedido vira prosa inútil. Para trabalho agentico, use o
gptagent (Passo 6).

**3. Escolha o modelo.** No seletor à direita, pegue
**ChatGPT instantâneo (chatgptproxy)** — é o que responde em segundos.
Deixe **ChatGPT pensante (chatgptproxy)** para problema difícil, sabendo que
ele custa minutos.

**4. Mande a primeira mensagem.** Por exemplo: *"me explique o que este projeto
precisa para servir uma API HTTP simples em Python"*.

Duas coisas para saber antes de usar:

- **`@` anexa contexto** (arquivo, seleção, terminal). Tudo que você anexa é
  digitado no navegador do outro lado — anexar um arquivo enorme deixa a
  resposta muito mais lenta. Anexe o que interessa, não a pasta inteira.
- **O botão "Apply" custa outra requisição inteira** (é um segundo modelo
  gerando o diff). Em mudança pequena, copiar e colar o bloco na mão sai
  minutos mais barato.

## Passo 6 — Primeiro pedido para o agente (gptagent)

O agente é quem cria arquivos e roda comandos. Chame com **`Ctrl+Alt+G`** (se
instalou com `-Atalho`) ou por `Ctrl+Shift+P` → **Executar Tarefa** (**Run
Task**, se o seu VS Code estiver em inglês) → `gptagent: fazer um pedido`.

Aparece uma caixa de texto. Escreva o pedido e dê Enter. Por exemplo:

```
Crie um servidor HTTP em Python (só biblioteca padrão) que responda
{"ok": true} em GET /health na porta 8000, num arquivo servidor.py.
Crie testes em test_servidor.py com unittest (não use pytest) e rode
'python -m unittest -v'. Só termine depois que os testes passarem.
```

O agente abre um terminal dedicado e trabalha em passos, mostrando cada um:
`▸ listar`, `▸ ler`, `▸ buscar`, `▸ escrever`, `▸ editar`, `▸ shell`, `▸ git`.
Ele para e pergunta antes de qualquer coisa que mexa na sua máquina:

| ação | o que você vê antes | pergunta |
|---|---|---|
| gravar ou alterar arquivo | o **diff colorido** | `aplicar? [s/N]` |
| rodar comando | a linha do comando, em amarelo | `rodar? [s/N]` |
| salvar no git (add+commit+push) | a mensagem do commit | `commitar e enviar? [s/N]` |

Responda **`s`** e Enter para aceitar (`sim`, `y` e `yes` também valem);
qualquer outra coisa recusa, e o agente tenta outro caminho.

> O terminal da tarefa abre **com foco**, de propósito: se ele não tiver foco,
> o `s` que você digita vai para o editor e a coisa parece travada.

Quando terminar, ele fecha com um `✔` e o resumo do que fez e de como conferiu.

### Como escrever um pedido que funciona bem aqui

Cada ida ao modelo custa caro, então o pedido bom é o **concreto e fechado**:

- **Diga o arquivo e o formato.** "em `servidor.py`", "com `unittest`, não
  pytest" evita uma rodada inteira de correção.
- **Mande verificar.** Terminar com *"rode os testes e só termine depois que
  passarem"* é o que separa "escreveu código" de "entregou coisa que funciona":
  o agente roda o comando e lê a saída de verdade.
- **Um objetivo por pedido.** Dois objetivos soltos viram idas e voltas.

### As outras duas tarefas

- **`gptagent: sessao interativa`** — abre uma conversa contínua no terminal,
  para vários pedidos seguidos sem reabrir a tarefa. Digite `sair` para
  encerrar.
- **`gptagent: situacao das contas`** — o `/health` do proxy sem sair do
  editor. É o primeiro lugar para olhar quando algo parece travado.

## Passo 7 — Revisar o que o agente fez

Abra o painel **Source Control** (o ícone de ramificação na barra lateral).
Cada arquivo que ele gravou aparece ali na hora; clique para ver o diff.

- Gostou: faça o commit normalmente.
- Não gostou: `git checkout .` desfaz tudo que ainda não virou commit.

É por isso que o Passo 3 pede `git init` **antes** de chamar o agente.

---

## Quando usar qual

| situação | ferramenta |
|---|---|
| entender código que já existe | Continue (chat) |
| mudar um trecho com o arquivo aberto na frente | Continue (chat) |
| criar arquivos novos, rodar testes, iterar até passar | gptagent |
| tarefa longa que você quer deixar rodando | gptagent |

## Modo sem confirmação (`--sim-a-tudo`)

Para deixar o agente trabalhar sem parar a cada passo, rode no terminal
integrado:

```powershell
gptagent --dir . --sim-a-tudo -p "seu pedido aqui"
```

Neste modo ele **escreve arquivos, roda comandos e até faz `git add` + `commit`
+ `push` sem perguntar**. Use só em pasta versionada e com tudo commitado
antes — e lembre que `git checkout .` só desfaz o que ainda não virou commit:
se ele commitou, o desfazer é `git reset --hard`; se já empurrou, é
`git revert`.

O confinamento é do *caminho*, não do comando: nenhuma ação de arquivo sai de
`--dir`, mas um `shell` alcança o que o seu usuário alcança.

## Quando algo não funciona

| sintoma | causa e conserto |
|---|---|
| a tarefa não aparece em "Executar Tarefa" | você não reabriu o VS Code depois de instalar, ou instalou em outro usuário do Windows |
| a tarefa não roda / o VS Code pergunta se você confia | pasta em Modo Restrito — Passo 4 |
| `gptagent não é reconhecido` (no terminal) | o PATH novo só vale para processo novo: feche **todas** as janelas do VS Code e reabra |
| `${workspaceFolder}` não pode ser resolvido | nenhuma pasta aberta — *Arquivo → Abrir Pasta* antes de chamar a tarefa |
| o modelo responde em prosa em vez de agir, no chat | o Continue ficou em **Agent**; troque para **Chat** (Passo 5) |
| `401` / chave inválida | chave errada. O gptagent mostra nas primeiras linhas de onde ela veio |
| `nenhuma conta utilizável (conta1=…; …)` | é o proxy, não o editor. Leia o detalhe entre parênteses: `no-session` = a sessão daquela conta caiu e precisa de relogin na VPS; se as três estão `ready`, é castigo por limite — some sozinho em até 10 minutos. Com streaming (o padrão) essa mensagem chega **dentro** da resposta, sem código HTTP |
| parece travado | rode `gptagent: situacao das contas`. Alguma conta `busy` = ele está trabalhando; nenhuma `busy` = o pedido já voltou |
| o primeiro passo demora muito mais que os outros | é o esperado: o primeiro pedido abre um chat **novo** no navegador da VPS e digita o pedido inteiro. Os passos seguintes continuam a **mesma** conversa e mandam só a mensagem nova |

---

Referência completa das opções: [`vscode/README.md`](README.md) (Continue) e
[`harness/README.md`](../harness/README.md) (gptagent).
