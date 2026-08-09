# Usar o chatgptproxy no VS Code

> **Primeira vez?** Vá antes ao [**Do zero ao primeiro pedido**](COMECE-AQUI.md)
> — o mesmo assunto em ordem de execução, do projeto vazio ao primeiro
> resultado. Esta página aqui é a referência das opções.

Duas formas, que se complementam:

| | Continue | gptagent |
|---|---|---|
| o que faz | conversa e edita com o arquivo aberto | agente que trabalha sozinho, vários passos |
| como usa | painel lateral do VS Code | terminal integrado ou tarefa |
| melhor para | "explique isto", "refatore esta função" | "conserte o bug X e rode os testes" |

Antes de qualquer coisa, confirme que o proxy responde:

```powershell
curl.exe -s https://gptproxy.nutef.com/health
```

Tem que vir `"ready":3`. Se vier `0`, as contas caíram da sessão e precisam de
relogin na VPS — nada do que está abaixo vai funcionar até lá.

---

## Instalação em um comando (Windows)

No PowerShell (pode ser o terminal do próprio VS Code):

```powershell
irm https://gptproxy.nutef.com/vscode/install.ps1 | iex
```

Ele pergunta a chave e faz o resto: extensão Continue, `config.yaml` do
Continue (mesclado com o que você já tiver, com backup datado antes de
qualquer mudança), `gptagent.exe` em `%LOCALAPPDATA%\Programs\gptagent` +
chave + PATH, e as tarefas `gptagent:*` como tarefas de **usuário** — valem
em qualquer projeto, sem `tasks.json` por pasta. Rodar de novo atualiza tudo
(é idempotente).

Com a chave na linha (sem prompt) e o atalho `ctrl+alt+g`:

```powershell
& ([scriptblock]::Create((irm https://gptproxy.nutef.com/vscode/install.ps1))) -Chave 'SUA-CHAVE' -Atalho
```

(Essa forma deixa a chave no histórico do PowerShell; o modo com prompt não.)

Depois **feche todas as janelas do VS Code e reabra** — o PATH novo só vale
para processo novo. O restante desta página é o caminho manual (Linux/macOS,
ou quem quer entender cada passo).

---

## 1. Continue (chat e edição dentro do editor)

Instale a extensão **Continue** no VS Code. Depois crie o arquivo de
configuração:

- **Windows:** `C:\Users\<você>\.continue\config.yaml`
- **Linux/macOS:** `~/.continue/config.yaml`

Baixe pronto e só troque a chave:

```powershell
New-Item -ItemType Directory -Force $env:USERPROFILE\.continue | Out-Null
Invoke-WebRequest https://gptproxy.nutef.com/vscode/config.yaml -OutFile $env:USERPROFILE\.continue\config.yaml
```

```bash
curl --create-dirs -o ~/.continue/config.yaml https://gptproxy.nutef.com/vscode/config.yaml
```

(O `-OutFile` do PowerShell **não cria** a pasta `.continue` sozinho — daí o
`New-Item` antes; numa máquina onde o Continue nunca abriu, ela não existe.)

O conteúdo canônico é o [`config.yaml`](config.yaml) desta pasta.

Reinicie o VS Code, abra o painel do Continue e escolha no seletor o
**ChatGPT instantâneo (chatgptproxy)**; deixe o
**ChatGPT pensante (chatgptproxy)** para problema difícil. Os nomes exatos saem do [`config.yaml`](config.yaml) desta
pasta — se mudarem, mudam lá primeiro.

### Quatro ajustes que não são opcionais

**Autocomplete tem que ficar desligado.** Cada resposta aqui leva de dezenas de
segundos a minutos, porque é um navegador digitando de verdade. Sugestão
enquanto você escreve exige resposta em milissegundos — ligar isso trava o
editor e queima as três contas à toa. A configuração não dá o papel
`autocomplete` ao modelo e ainda desliga explicitamente; se quiser uma trava a
mais, desmarque "Continue: Enable Tab Autocomplete" nas settings do VS Code.

**Nada de `embed`.** O proxy não faz embeddings. Deixe a indexação de código
com o modelo local que o próprio Continue traz; se você der o papel `embed`
para este modelo, a indexação falha inteira.

**Timeout largo.** `timeout: 900` — o Continue conta em **segundos** (e
multiplica por 1000 por dentro; o default já é 7200 s = 2 h). É timeout de
inatividade do socket, então não derruba resposta que ainda está pingando.

**Desligue os títulos de sessão.** Ligado (o default), cada conversa nova
dispara uma requisição EXTRA com o mesmo modelo só para batizar a aba — aqui
isso ocupa uma das 3 contas por minutos, na frente de trabalho de verdade.
**O instalador de um comando já desliga** (grava
`sharedConfig.disableSessionTitles: true` em
`~/.continue/index/globalContext.json` — não há campo no config.yaml). À mão:
painel do Continue → engrenagem (User Settings) → **Enable Session Titles:
off**; se a página reclamar "Screen width too small", alargue o painel
arrastando a borda esquerda dele.

### E dois avisos

**Fique no modo Chat.** Os modos *Agent* e *Plan* do Continue dependem de
chamada de função (`tools`), que o proxy não tem — neles o modelo vira prosa
inútil. Para trabalho agentico, use o gptagent (abaixo).

**Apply não é grátis.** Aceitar um bloco de código ("Apply") gera OUTRA
requisição completa (é um segundo LLM gerando o diff preciso). Em mudança
pequena, copiar e colar o bloco na mão sai minutos mais barato.

---

## 2. gptagent (o agente que trabalha sozinho)

O agente edita arquivos e roda comandos por conta própria. Ele já funciona no
terminal integrado do VS Code — abra com **Ctrl+`** e chame:

```powershell
gptagent --dir .
```

O instalador de um comando já deixa as tarefas prontas em **qualquer**
projeto (tarefas de usuário). Quem preferir por projeto: baixe
[`tasks.json`](tasks.json) para `.vscode/tasks.json` da pasta aberta.

**Ctrl+Shift+P → "Run Task"** oferece:

- **gptagent: fazer um pedido** — pergunta o que você quer e executa
- **gptagent: sessao interativa** — abre a conversa
- **gptagent: situacao das contas** — o `/health` do proxy

(Os rótulos são **sem acento** de propósito: quem grava as tarefas é o
`install.ps1`, que é ASCII puro para sobreviver ao `irm | iex`.)

As tarefas exigem uma pasta aberta (*File → Open Folder*); sem pasta o VS Code
reclama de `${workspaceFolder}` — não é defeito do gptagent.

Se quiser uma tecla (o instalador faz isso com `-Atalho`), adicione em
*Keyboard Shortcuts (JSON)*:

```json
{
  "key": "ctrl+alt+g",
  "command": "workbench.action.tasks.runTask",
  "args": "gptagent: fazer um pedido"
}
```

**Detalhe importante das tarefas:** elas abrem o terminal com foco, de
propósito. O agente **pergunta antes** de gravar arquivo ou rodar comando, e
você precisa poder digitar `s`. Se o terminal não tiver foco, parece travado.

### Trabalhar com o git aberto no VS Code

O melhor fluxo é deixar o painel *Source Control* à vista enquanto o agente
trabalha: cada arquivo que ele grava aparece ali na hora, e você revê tudo com
`git diff` antes de aceitar. Commit tudo **antes** de usar `--sim-a-tudo` —
assim `git checkout .` desfaz qualquer besteira de uma vez.

---

## Quando algo não funciona

| sintoma | causa |
|---|---|
| `401` / "chave inválida" | chave errada. No gptagent, as primeiras linhas mostram de onde ela veio |
| `nenhuma conta utilizável (conta1=…; …)` | a mensagem cobre DOIS casos e o detalhe entre parênteses os separa: `no-session` = a sessão daquela conta caiu no ChatGPT e precisa de relogin manual na VPS; as três em `ready` = castigo por limite, que passa sozinho em até 10 min. Não espere um código HTTP: com streaming (o padrão do chat) isso chega **dentro** da resposta, como `⚠️ [chatgptproxy] …` |
| `"ready":3` no /health mas tudo falhando | olhe `rate_limited_for` no mesmo JSON: `ready` conta só o estado da sessão, não o castigo por limite |
| "gptagent não é reconhecido" na tarefa | PATH mudou com o VS Code aberto. Feche TODAS as janelas e reabra |
| erro de rede no meio de resposta longa | não é o timeout do Continue (default 2 h): é intermediário de rede (proxy corporativo/NAT) derrubando conexão ociosa. O proxy manda um chunk vazio a cada 15 s justamente para isso; se persistir, o problema está entre você e a VPS |
| o editor engasga ao digitar | autocomplete ficou ligado — veja acima |
| parece travado | veja se o terminal está pedindo `[s/N]`; e confira `/health`: se nenhuma conta está `busy`, o proxy já terminou |

Uma coisa a aceitar de saída: **isto é lento**. São três contas gratuitas
dirigidas por navegador, não uma API. Serve muito bem para pedir uma tarefa e
ir fazer outra coisa; não serve para digitação assistida em tempo real.

## O que este proxy não tem

- **Chamada de função nativa** (`tools`). Extensões que dependem disso para
  editar arquivos não vão funcionar; é por isso que o gptagent usa um protocolo
  de texto próprio — e por isso os modos Agent/Plan do Continue ficam de fora.
- **Embeddings** — sem `@codebase` apoiado no proxy.
- **Visão/anexos** — só texto.
- Uma conta atende **uma requisição por vez**; com três contas, três pedidos em
  paralelo. O quarto espera na fila.
