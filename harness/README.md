# gptagent — ChatGPT editando os arquivos do seu PC

Um agente de arquivo único, **sem dependências** além do Python 3.10+. Ele
conversa com o [chatgptproxy](../README.md) e, a cada passo, executa **uma**
ação no seu computador: ler, buscar, escrever, editar, rodar comando, commitar.

Não é mágica: o modelo não roda nada sozinho. Ele *pede* uma ação num bloco
` ```acao `, este programa executa e devolve a saída. Fora do modo
`--sim-a-tudo`, **toda** ação que escreve arquivo ou roda comando para e espera
seu "s".

## Windows, sem instalar Python

O caminho fácil é o instalador de um comando, que baixa o exe, grava a chave,
põe no PATH e ainda configura o VS Code (ver [`vscode/README.md`](../vscode/README.md)):

```powershell
irm https://gptproxy.nutef.com/vscode/install.ps1 | iex
```

À mão, o equivalente é:

```powershell
New-Item -ItemType Directory -Force $env:LOCALAPPDATA\Programs\gptagent | Out-Null
$ProgressPreference = 'SilentlyContinue'   # a barra de progresso do PS 5.1 trava o download
Invoke-WebRequest https://gptproxy.nutef.com/gptagent.exe -OutFile $env:LOCALAPPDATA\Programs\gptagent\gptagent.exe -UseBasicParsing
"sua-chave-do-proxy" | Out-File -Encoding ascii $env:LOCALAPPDATA\Programs\gptagent\gptagent.key
[Environment]::SetEnvironmentVariable('Path', ([Environment]::GetEnvironmentVariable('Path','User') + ';' + "$env:LOCALAPPDATA\Programs\gptagent"), 'User')
```

Depois **feche todas as janelas do VS Code e reabra**: as tarefas herdam o
PATH do processo do VS Code, não do terminal onde você rodou isso.

O `gptagent.key` é procurado na pasta do executável e na pasta atual — assim
não é preciso repetir `--key` nem mexer em variável de ambiente. (Se um dia
existir a variável `GPTAGENT_KEY`, ela **ganha** do arquivo — o programa avisa
quando as duas discordam.)

**Se o Windows reclamar na primeira execução:** o binário não é assinado.
Baixado pelo navegador ele ganha a marca da internet e o SmartScreen barra
("Mais informações" → "Executar assim mesmo"); baixado pelo
`Invoke-WebRequest`/instalador, normalmente nem aparece aviso. Alguns
antivírus marcam executável feito com PyInstaller como suspeito, por falso
positivo — se o Defender quarentenar, restaure e adicione exclusão para
`%LOCALAPPDATA%\Programs\gptagent`. Confira a integridade quando quiser:
`Get-FileHash` do exe local contra `https://gptproxy.nutef.com/gptagent.exe.sha256`.

O executável é gerado num runner Windows do GitHub Actions
(`.github/workflows/build-windows.yml`), porque PyInstaller não cruza-compila e
a VPS é Linux.

## Ou direto do fonte (Windows ou Linux)

Copie `gptagent.py` para o seu computador. Precisa de Python 3.10+. Depois:

```powershell
# Windows (PowerShell)
$env:GPTAGENT_KEY = "a-chave-do-proxy"
python gptagent.py --dir C:\projetos\meu-site
```

```bash
# Linux / macOS
export GPTAGENT_KEY="a-chave-do-proxy"
python3 gptagent.py --dir ~/projetos/meu-site
```

Um pedido só, sem modo interativo:

```bash
python3 gptagent.py --dir . -p "adicione tratamento de erro no upload"
```

## Opções

| flag | efeito |
|---|---|
| `--dir` | diretório do projeto. **Nada fora dele é acessível** (padrão: atual) |
| `--url` | endpoint do proxy (padrão `https://gptproxy.nutef.com/v1`) |
| `--key` | chave do proxy (ou variável `GPTAGENT_KEY`) |
| `--model` | padrão `gpt-5-instant` — segundos por passo. Use `gpt-5` (pensante, MINUTOS por passo) só quando o problema exigir raciocínio pesado |
| `-p` | executa um pedido e sai |
| `--sim-a-tudo` | não pergunta antes de escrever nem de rodar comando |
| `--sem-stream` | não mostra a resposta sendo escrita |

Desde a 1.3 o modelo pode mandar **várias ações num turno só** (escrever dois
arquivos e rodar os testes = uma ida ao navegador, não três) — é o que torna o
agente utilizável em cima de um backend em que cada ida custa segundos a
minutos.

## O que o modelo pode pedir

`listar`, `ler`, `buscar` (regex), `escrever` (arquivo inteiro), `editar`
(troca de trecho exato), `shell` (comando no diretório do projeto), `git`
(add + commit + push de uma vez) e `pronto` (encerra com um resumo).

Antes de gravar qualquer coisa você vê o **diff colorido** e decide.

## Cuidados de verdade

- **`--sim-a-tudo` entrega o volante.** Nesse modo o ChatGPT escreve arquivos e
  roda comandos sem perguntar. Use só em pasta versionada no git, com tudo
  commitado antes — assim `git diff` mostra tudo que ele fez e `git checkout .`
  desfaz.
- **O confinamento é do caminho, não do comando.** As ações de arquivo não saem
  de `--dir`, mas um `shell` pode ir a qualquer lugar que o seu usuário
  alcança. É por isso que `shell` pede confirmação e mostra o comando inteiro
  antes.
- **Cada passo é uma ida ao navegador na VPS**, então é lento: conte segundos a
  dezenas de segundos por passo, não milissegundos. O proxy continua a mesma
  conversa entre os passos, o que evita reenviar o histórico.
- **`escrever` apaga o arquivo inteiro.** O prompt manda preferir `editar`, mas
  confira o diff quando vier um `escrever` sobre arquivo existente.

## Se algo travar

| sintoma | causa provável |
|---|---|
| `HTTP 401` | chave errada (`--key` / `GPTAGENT_KEY`) |
| `HTTP 503 ... nenhuma conta utilizável` | as contas caíram da sessão — relogin no noVNC |
| `HTTP 429` | limite da OpenAI nas 3 contas; espere |
| responde em prosa em vez de agir | o agente insiste sozinho uma vez; se persistir, refaça o pedido mais concreto |
| Ctrl+C "não funciona" | no prompt `[s/N]` e durante o streaming ele age; no **silêncio** antes da resposta começar (com `--sem-stream`, principalmente) o Windows só entrega o Ctrl+C quando chegam bytes — espere o próximo delta, ou mate o terminal (lixeira do painel). Abortar no cliente **não** libera a conta no proxy: o navegador termina de digitar sozinho |
