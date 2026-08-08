# gptagent — ChatGPT editando os arquivos do seu PC

Um agente de arquivo único, **sem dependências** além do Python 3.10+. Ele
conversa com o [chatgptproxy](../README.md) e, a cada passo, executa **uma**
ação no seu computador: ler, buscar, escrever, editar, rodar comando, commitar.

Não é mágica: o modelo não roda nada sozinho. Ele *pede* uma ação num bloco
` ```acao `, este programa executa e devolve a saída. Fora do modo
`--sim-a-tudo`, **toda** ação que escreve arquivo ou roda comando para e espera
seu "s".

## Windows, sem instalar Python

Baixe o executável e ponha a chave num arquivo ao lado dele:

```powershell
Invoke-WebRequest https://gptproxy.nutef.com/gptagent.exe -OutFile gptagent.exe
"sua-chave-do-proxy" | Out-File -Encoding ascii gptagent.key
.\gptagent.exe --dir C:\caminho\do\projeto
```

O `gptagent.key` é procurado na pasta do executável e na pasta atual — assim
não é preciso repetir `--key` nem mexer em variável de ambiente.

**O Windows vai reclamar na primeira execução.** O binário não é assinado, e o
SmartScreen barra o que não conhece: "Mais informações" → "Executar assim
mesmo". Alguns antivírus também marcam executável feito com PyInstaller como
suspeito, por falso positivo — é o preço de um `.exe` sem certificado.

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
| `--model` | padrão `gpt-5` |
| `-p` | executa um pedido e sai |
| `--sim-a-tudo` | não pergunta antes de escrever nem de rodar comando |
| `--sem-stream` | não mostra a resposta sendo escrita |

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
