# Usar o chatgptproxy no VS Code

Duas formas, que se complementam:

| | Continue | gptagent |
|---|---|---|
| o que faz | conversa e edita com o arquivo aberto | agente que trabalha sozinho, vários passos |
| como usa | painel lateral do VS Code | terminal integrado ou tarefa |
| melhor para | "explique isto", "refatore esta função" | "conserte o bug X e rode os testes" |

Antes de qualquer coisa, confirme que o proxy responde:

```
curl https://gptproxy.nutef.com/health
```

Tem que vir `"ready":3`. Se vier `0`, as contas caíram da sessão e precisam de
relogin na VPS — nada do que está abaixo vai funcionar até lá.

---

## 1. Continue (chat e edição dentro do editor)

Instale a extensão **Continue** no VS Code. Depois crie o arquivo de
configuração:

- **Windows:** `C:\Users\<você>\.continue\config.yaml`
- **Linux/macOS:** `~/.continue/config.yaml`

Baixe pronto e só troque a chave:

```powershell
Invoke-WebRequest https://gptproxy.nutef.com/vscode/config.yaml -OutFile $env:USERPROFILE\.continue\config.yaml
```

```bash
curl -o ~/.continue/config.yaml https://gptproxy.nutef.com/vscode/config.yaml
```

O conteúdo é este:

```yaml
name: nutef
version: 1.0.0
schema: v1

models:
  - name: ChatGPT (chatgptproxy)
    provider: openai
    model: gpt-5
    apiBase: https://gptproxy.nutef.com/v1
    apiKey: COLE-SUA-CHAVE-AQUI
    roles: [chat, edit, apply]
    requestOptions:
      timeout: 900000
    defaultCompletionOptions:
      maxTokens: 8192
    autocompleteOptions:
      disable: true
```

Reinicie o VS Code, abra o painel do Continue (**Ctrl+L**) e escolha o modelo
"ChatGPT (chatgptproxy)".

### Três ajustes que não são opcionais

**Autocomplete tem que ficar desligado.** Cada resposta aqui leva de dezenas de
segundos a minutos, porque é um navegador digitando de verdade. Sugestão
enquanto você escreve exige resposta em milissegundos — ligar isso trava o
editor e queima as três contas à toa. Por isso a configuração acima não dá o
papel `autocomplete` ao modelo e ainda desliga explicitamente.

**Nada de `embed`.** O proxy não faz embeddings. Deixe a indexação de código
com o modelo local que o próprio Continue traz; se você der o papel `embed`
para este modelo, a indexação falha inteira.

**Timeout largo.** `900000` (15 min). Com o padrão da extensão, respostas
longas morrem no meio e você vê erro de rede em vez de resposta.

---

## 2. gptagent (o agente que trabalha sozinho)

O agente edita arquivos e roda comandos por conta própria. Ele já funciona no
terminal integrado do VS Code — abra com **Ctrl+`** e chame:

```powershell
gptagent --dir .
```

Para não digitar isso toda vez, use as tarefas. Baixe para dentro do projeto:

```powershell
mkdir .vscode -Force
Invoke-WebRequest https://gptproxy.nutef.com/vscode/tasks.json -OutFile .vscode\tasks.json
```

Aí **Ctrl+Shift+P → "Run Task"** oferece:

- **gptagent: fazer um pedido** — pergunta o que você quer e executa
- **gptagent: sessão interativa** — abre a conversa
- **gptagent: situação das contas** — o `/health` do proxy

Se quiser uma tecla, adicione em *Keyboard Shortcuts (JSON)*:

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
| `401` / "chave inválida" | chave errada. No gptagent, a primeira linha mostra de onde ela veio |
| `503 nenhuma conta utilizável` | as sessões do ChatGPT caíram; precisa de relogin manual na VPS |
| `429` | as três contas no limite da OpenAI ao mesmo tempo; esperar |
| erro de rede no Continue depois de ~1 min | `requestOptions.timeout` não foi aplicado |
| o editor engasga ao digitar | autocomplete ficou ligado — veja acima |
| parece travado | veja se o terminal está pedindo `[s/N]`; e confira `/health`: se nenhuma conta está `busy`, o proxy já terminou |

Uma coisa a aceitar de saída: **isto é lento**. São três contas gratuitas
dirigidas por navegador, não uma API. Serve muito bem para pedir uma tarefa e
ir fazer outra coisa; não serve para digitação assistida em tempo real.

## O que este proxy não tem

- **Chamada de função nativa** (`tools`). Extensões que dependem disso para
  editar arquivos não vão funcionar; é por isso que o gptagent usa um protocolo
  de texto próprio.
- **Embeddings** — sem `@codebase` apoiado no proxy.
- **Visão/anexos** — só texto.
- Uma conta atende **uma requisição por vez**; com três contas, três pedidos em
  paralelo. O quarto espera na fila.
