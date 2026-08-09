# install.ps1 - instala o chatgptproxy no VS Code do Windows, de uma vez.
# PowerShell 5.1+ (nada exclusivo do 7). Rodar duas vezes = ok (idempotente).
# ASCII puro de proposito: sobrevive a qualquer charset no caminho irm | iex.
#
# Um comando (pergunta a chave):
#   irm https://gptproxy.nutef.com/vscode/install.ps1 | iex
# Com a chave na linha (sem prompt) e o atalho ctrl+alt+g:
#   & ([scriptblock]::Create((irm https://gptproxy.nutef.com/vscode/install.ps1))) -Chave 'SUA-CHAVE' -Atalho
#
# O que faz, nesta ordem:
#   1. confere o proxy (/health) e valida a chave (/v1/models)
#   2. extensao Continue no VS Code (code --install-extension)
#   3. %USERPROFILE%\.continue\config.yaml - cria, ou mescla SEM destruir o
#      que ja existe (backup datado antes de qualquer mudanca)
#   4. gptagent.exe em %LOCALAPPDATA%\Programs\gptagent (pula o download se o
#      sha256 remoto bate) + gptagent.key ao lado + PATH do usuario
#   5. tarefas "gptagent:*" como TAREFAS DE USUARIO do VS Code (valem em
#      qualquer projeto, sem tasks.json por pasta)
#   6. desliga os titulos de sessao do Continue (cada titulo custaria uma
#      requisicao inteira numa das 3 contas)
#   7. com -Atalho: ctrl+alt+g abre "gptagent: fazer um pedido"
[CmdletBinding()]
param(
    [string]$Chave,
    [switch]$Atalho,
    [string]$Base = 'https://gptproxy.nutef.com'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # a barra do PS 5.1 trava downloads
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

function Passo([string]$m) { Write-Host ''; Write-Host "== $m" -ForegroundColor Cyan }
function Diga([string]$m)  { Write-Host "   $m" }
function Avise([string]$m) { Write-Host "   AVISO: $m" -ForegroundColor Yellow }
function Falha([string]$m) { Write-Host "   ERRO: $m" -ForegroundColor Red; throw $m }

# Grava UTF-8 SEM BOM (o Out-File do 5.1 poe BOM; BOM dentro de yaml/json do
# VS Code funciona, mas sem BOM nunca da problema). So regrava se mudou; se
# mudou e ja existia, deixa backup datado ao lado.
function Grava([string]$caminho, [string]$conteudo) {
    $enc = New-Object System.Text.UTF8Encoding $false
    if (Test-Path $caminho) {
        $atual = [IO.File]::ReadAllText($caminho)
        if ($atual -eq $conteudo) { Diga "sem mudanca: $caminho"; return }
        $bak = "$caminho.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
        Copy-Item $caminho $bak
        Diga "backup: $bak"
    }
    $pasta = Split-Path $caminho -Parent
    if (-not (Test-Path $pasta)) { New-Item -ItemType Directory -Force $pasta | Out-Null }
    [IO.File]::WriteAllText($caminho, $conteudo, $enc)
    Diga "gravado: $caminho"
}

# ---------------------------------------------------------------- 1. proxy
Passo 'Conferindo o proxy'
try {
    $saude = Invoke-RestMethod -Uri "$Base/health" -TimeoutSec 30 -UseBasicParsing
} catch {
    Falha "nao consegui falar com $Base - sem rede, ou o proxy caiu: $($_.Exception.Message)"
}
Diga "contas prontas: $($saude.ready)"
if (-not $saude.ready) {
    Avise 'NENHUMA conta pronta - o proxy esta no ar, mas as sessoes do ChatGPT cairam.'
    Avise 'A instalacao segue, mas nada respondera ate o relogin na VPS.'
}

if (-not $Chave) {
    $Chave = (Read-Host 'Cole a chave do proxy (API_KEY)').Trim()
}
if (-not $Chave) { Falha 'sem chave nao ha o que instalar' }

Passo 'Validando a chave'
try {
    Invoke-RestMethod -Uri "$Base/v1/models" -TimeoutSec 30 -UseBasicParsing `
        -Headers @{ Authorization = "Bearer $Chave" } | Out-Null
    Diga 'chave aceita'
} catch {
    $resp = $_.Exception.Response
    if ($resp -and [int]$resp.StatusCode -eq 401) {
        Falha 'o proxy recusou esta chave (401). Confira se ela foi COPIADA, sem espaco sobrando.'
    }
    Avise "nao deu para validar a chave agora ($($_.Exception.Message)) - seguindo mesmo assim"
}

# ---------------------------------------------------------- 2. extensao
Passo 'Extensao Continue no VS Code'
$code = Get-Command code -ErrorAction SilentlyContinue
if (-not $code) {
    Falha ('nao achei o comando "code". Instale o VS Code (winget install Microsoft.VisualStudioCode) ' +
           'ou, no VS Code, rode "Shell Command: Install code command in PATH", e tente de novo.')
}
# cmd /c: o code.cmd solta avisos no stderr, e no PS 5.1 com ErrorAction Stop
# um stderr redirecionado viraria erro fatal - pelo cmd o aviso morre no nul.
$listaExt = cmd /c "code --list-extensions 2>nul"
if (@($listaExt) -contains 'Continue.continue') {
    Diga 'ja instalada'
} else {
    cmd /c "code --install-extension Continue.continue --force >nul 2>nul"
    if ($LASTEXITCODE -ne 0) { Falha 'o code --install-extension falhou; instale a extensao Continue pela loja do VS Code' }
    Diga 'instalada'
}

# ------------------------------------------------- 3. config do Continue
Passo 'config.yaml do Continue'
$dirContinue = Join-Path $env:USERPROFILE '.continue'
$cfgContinue = Join-Path $dirContinue 'config.yaml'

# Mesmo conteudo de vscode/config.yaml, com a chave aplicada.
$blocoModelo = @"
  # Instantaneo primeiro: o gpt-5 pensante leva MINUTOS por resposta na web.
  - name: ChatGPT instantaneo (chatgptproxy)
    provider: openai
    model: gpt-5-instant
    apiBase: $Base/v1
    apiKey: $Chave
    roles:
      - chat
      - edit
      - apply
    requestOptions:
      # Em SEGUNDOS (o Continue multiplica por 1000).
      timeout: 900
    defaultCompletionOptions:
      # Forca a poda de historico no cliente - aqui o prompt e DIGITADO.
      contextLength: 32768
      maxTokens: 8192
    autocompleteOptions:
      disable: true
  - name: ChatGPT pensante (chatgptproxy)
    provider: openai
    model: gpt-5
    apiBase: $Base/v1
    apiKey: $Chave
    roles:
      - chat
      - edit
      - apply
    requestOptions:
      timeout: 900
    defaultCompletionOptions:
      contextLength: 32768
      maxTokens: 8192
    autocompleteOptions:
      disable: true
"@

$cfgNovo = @"
name: nutef
version: 1.0.0
schema: v1

models:
$blocoModelo
"@

if (-not (Test-Path $cfgContinue)) {
    Grava $cfgContinue $cfgNovo
} else {
    $linhas = @([IO.File]::ReadAllLines($cfgContinue))
    $iBase = -1
    for ($i = 0; $i -lt $linhas.Count; $i++) {
        if ($linhas[$i] -match [regex]::Escape("$Base/v1")) { $iBase = $i; break }
    }
    if ($iBase -ge 0) {
        # Nossos blocos ja existem: atualiza a apiKey de CADA bloco que aponta
        # para o proxy (a proxima linha apiKey depois de cada apiBase nosso).
        $mudou = $false
        for ($b = 0; $b -lt $linhas.Count; $b++) {
            if ($linhas[$b] -notmatch [regex]::Escape("$Base/v1")) { continue }
            for ($i = $b; $i -lt [Math]::Min($b + 12, $linhas.Count); $i++) {
                if ($linhas[$i] -match '^(\s*)apiKey:\s*(.*)$') {
                    if ($Matches[2].Trim() -ne $Chave) {
                        $linhas[$i] = "$($Matches[1])apiKey: $Chave"
                        $mudou = $true
                    }
                    break
                }
                if ($i -gt $b -and $linhas[$i] -match '^\s*-\s+name:') { break }
            }
        }
        if ($mudou) { Grava $cfgContinue (($linhas -join "`r`n") + "`r`n") }
        else { Diga 'ja aponta para o proxy com esta chave' }
    } else {
        $iModels = -1
        for ($i = 0; $i -lt $linhas.Count; $i++) {
            # "models:" solto OU o "models: []" do esqueleto que a extensao
            # cria sozinha na primeira ativacao - o [] vazio vira nossa lista.
            if ($linhas[$i] -match '^models:\s*(\[\s*\])?\s*$') {
                $iModels = $i
                $linhas[$i] = 'models:'
                break
            }
        }
        if ($iModels -ge 0) {
            # Ha outros modelos: INSERE o nosso no topo da lista, sem mexer neles.
            $antes  = $linhas[0..$iModels]
            $depois = @()
            if ($iModels + 1 -lt $linhas.Count) { $depois = $linhas[($iModels + 1)..($linhas.Count - 1)] }
            $novo = ($antes + ($blocoModelo -split "`n" | ForEach-Object { $_.TrimEnd() }) + $depois) -join "`r`n"
            Grava $cfgContinue ($novo + "`r`n")
        } else {
            Avise 'seu config.yaml existe mas nao tem a linha "models:" - nao vou adivinhar a estrutura.'
            Avise "Cole voce mesmo este bloco dentro de models: no arquivo $cfgContinue"
            Write-Host $blocoModelo
        }
    }
}

# ------------------------------------------------------- 4. gptagent.exe
Passo 'gptagent (agente de terminal)'
$dirAgente = Join-Path $env:LOCALAPPDATA 'Programs\gptagent'
if (-not (Test-Path $dirAgente)) { New-Item -ItemType Directory -Force $dirAgente | Out-Null }
$exe = Join-Path $dirAgente 'gptagent.exe'

$baixar = $true
try {
    $hashRemoto = (Invoke-RestMethod -Uri "$Base/gptagent.exe.sha256" -TimeoutSec 30 -UseBasicParsing).Trim().Split(' ')[0]
    if ($hashRemoto -and (Test-Path $exe)) {
        $hashLocal = (Get-FileHash $exe -Algorithm SHA256).Hash
        if ($hashLocal -ieq $hashRemoto) { Diga 'gptagent.exe ja esta na versao publicada'; $baixar = $false }
    }
} catch { }  # sem hash publicado: baixa sempre (comportamento antigo)

if ($baixar) {
    $tmp = "$exe.baixando"
    Invoke-WebRequest -Uri "$Base/gptagent.exe" -OutFile $tmp -UseBasicParsing -TimeoutSec 300
    Move-Item -Force $tmp $exe
    try { Unblock-File $exe } catch {}
    Diga "baixado: $exe ($([math]::Round((Get-Item $exe).Length / 1MB, 1)) MB)"
}

$arqChave = Join-Path $dirAgente 'gptagent.key'
$chaveAtual = ''
if (Test-Path $arqChave) { $chaveAtual = ([IO.File]::ReadAllText($arqChave)).Trim() }
if ($chaveAtual -ne $Chave) {
    [IO.File]::WriteAllText($arqChave, $Chave, (New-Object System.Text.UTF8Encoding $false))
    Diga "chave gravada em $arqChave"
} else { Diga 'chave ja gravada' }

$varAmb = [Environment]::GetEnvironmentVariable('GPTAGENT_KEY', 'User')
if ($varAmb -and $varAmb.Trim() -ne $Chave) {
    Avise 'existe uma variavel GPTAGENT_KEY de usuario com OUTRA chave - ela tem precedencia'
    Avise 'sobre o gptagent.key. Para remover:  [Environment]::SetEnvironmentVariable("GPTAGENT_KEY", $null, "User")'
}

$pathUser = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $pathUser) { $pathUser = '' }
$noPath = @($pathUser -split ';' | ForEach-Object { $_.Trim() }) -contains $dirAgente
if ($noPath) {
    Diga 'ja esta no PATH do usuario'
} else {
    [Environment]::SetEnvironmentVariable('Path', ($pathUser.TrimEnd(';') + ';' + $dirAgente), 'User')
    Diga "adicionado ao PATH do usuario: $dirAgente"
}

# ------------------------------------------------- 5. tarefas de usuario
Passo 'Tarefas do VS Code (nivel de usuario)'
$tasksUser = Join-Path $env:APPDATA 'Code\User\tasks.json'

$tarefasJson = @'
[
  {
    "label": "gptagent: fazer um pedido",
    "type": "process",
    "command": "${env:LOCALAPPDATA}\\Programs\\gptagent\\gptagent.exe",
    "args": ["--dir", "${workspaceFolder}", "-p", "${input:pedido}"],
    "problemMatcher": [],
    "presentation": { "reveal": "always", "focus": true, "panel": "dedicated" }
  },
  {
    "label": "gptagent: sessao interativa",
    "type": "process",
    "command": "${env:LOCALAPPDATA}\\Programs\\gptagent\\gptagent.exe",
    "args": ["--dir", "${workspaceFolder}"],
    "problemMatcher": [],
    "presentation": { "reveal": "always", "focus": true, "panel": "dedicated" }
  },
  {
    "label": "gptagent: situacao das contas",
    "type": "shell",
    "command": "curl.exe",
    "args": ["-s", "__BASE__/health"],
    "problemMatcher": [],
    "presentation": { "reveal": "always", "focus": false, "panel": "shared" }
  }
]
'@ -replace '__BASE__', $Base
$entradaJson = '[{ "id": "pedido", "type": "promptString", "description": "O que voce quer que ele faca neste projeto?" }]'
# O ForEach-Object desembrulha o PSObject que o ConvertFrom-Json poe em volta
# de array: sem ele o ConvertTo-Json final grava {"value":[...],"Count":N} no
# lugar da lista - e o VS Code ignora o tasks.json inteiro.
$tarefas = @((ConvertFrom-Json $tarefasJson) | ForEach-Object { $_ })
$entradas = @((ConvertFrom-Json $entradaJson) | ForEach-Object { $_ })

if (-not (Test-Path $tasksUser)) {
    $doc = [pscustomobject]@{ version = '2.0.0'; tasks = $tarefas; inputs = $entradas }
    Grava $tasksUser (ConvertTo-Json $doc -Depth 12)
} else {
    # tasks.json de usuario pode ter comentarios //; tira-os so para o parse.
    $cru = [IO.File]::ReadAllText($tasksUser)
    $semComentario = ($cru -split "`n" | Where-Object { $_ -notmatch '^\s*//' }) -join "`n"
    $doc = $null
    try { $doc = ConvertFrom-Json $semComentario } catch { }
    if ($null -eq $doc) {
        Avise "nao consegui interpretar $tasksUser - nao vou mexer nele."
        Avise 'Abra "Tasks: Open User Tasks" no VS Code e cole as tarefas do repo (vscode/tasks.json).'
    } else {
        if (-not $doc.PSObject.Properties['version']) {
            $doc | Add-Member -NotePropertyName version -NotePropertyValue '2.0.0'
        }
        $existentes = @()
        if ($doc.PSObject.Properties['tasks'] -and $doc.tasks) { $existentes = @($doc.tasks | ForEach-Object { $_ }) }
        $mantidas = @($existentes | Where-Object { $_.label -notlike 'gptagent:*' })
        $doc | Add-Member -NotePropertyName tasks -NotePropertyValue @(($mantidas + $tarefas) | ForEach-Object { $_ }) -Force
        $entradasVelhas = @()
        if ($doc.PSObject.Properties['inputs'] -and $doc.inputs) { $entradasVelhas = @($doc.inputs | ForEach-Object { $_ }) }
        $outras = @($entradasVelhas | Where-Object { $_.id -ne 'pedido' })
        $doc | Add-Member -NotePropertyName inputs -NotePropertyValue @(($outras + $entradas) | ForEach-Object { $_ }) -Force
        Grava $tasksUser (ConvertTo-Json $doc -Depth 12)
    }
}

# --------------------------------------- 6. titulos de sessao do Continue
# Cada conversa nova geraria um TITULO usando o mesmo modelo = 1 requisicao
# extra de minutos numa das 3 contas. Nao ha campo no config.yaml; o toggle
# da extensao persiste em sharedConfig.disableSessionTitles no
# ~/.continue/index/globalContext.json - entao gravamos direto la.
# JavaScriptSerializer (e nao ConvertFrom/To-Json): ele round-tripa arrays
# sem o artefato {"value":...,"Count":N} do PS 5.1.
Passo 'Titulos de sessao do Continue'
$gctx = Join-Path $env:USERPROFILE '.continue\index\globalContext.json'
try {
    Add-Type -AssemblyName System.Web.Extensions
    $ser = New-Object System.Web.Script.Serialization.JavaScriptSerializer
    $dic = $null
    if (Test-Path $gctx) { $dic = $ser.DeserializeObject([IO.File]::ReadAllText($gctx)) }
    if ($null -eq $dic) { $dic = New-Object 'System.Collections.Generic.Dictionary[string,object]' }
    if (-not $dic.ContainsKey('sharedConfig') -or $null -eq $dic['sharedConfig']) {
        $dic['sharedConfig'] = New-Object 'System.Collections.Generic.Dictionary[string,object]'
    }
    if ($dic['sharedConfig']['disableSessionTitles'] -eq $true) {
        Diga 'ja desligados'
    } else {
        $dic['sharedConfig']['disableSessionTitles'] = $true
        Grava $gctx ($ser.Serialize($dic))
        Diga 'desligados (a extensao aplica ao recarregar a config)'
    }
} catch {
    Avise "nao consegui gravar em $gctx ($($_.Exception.Message))."
    Avise 'Desligue a mao: painel do Continue > engrenagem > Enable Session Titles: off.'
}

# ------------------------------------------------------------ 7. atalho
if ($Atalho) {
    Passo 'Atalho ctrl+alt+g'
    $keyb = Join-Path $env:APPDATA 'Code\User\keybindings.json'
    $binding = '  { "key": "ctrl+alt+g", "command": "workbench.action.tasks.runTask", "args": "gptagent: fazer um pedido" }'
    if (-not (Test-Path $keyb)) {
        Grava $keyb ("[`r`n$binding`r`n]`r`n")
    } elseif (([IO.File]::ReadAllText($keyb)) -match 'ctrl\+alt\+g') {
        Diga 'ctrl+alt+g ja esta definido no seu keybindings.json - nao vou mexer'
    } else {
        $cru = [IO.File]::ReadAllText($keyb)
        $iFecha = $cru.LastIndexOf(']')
        if ($iFecha -lt 0) {
            Avise "keybindings.json sem ']' final - cole o atalho a mao (README do vscode/)."
        } else {
            $antes = $cru.Substring(0, $iFecha).TrimEnd()
            $temItem = $antes -match '\{'
            $sep = ''
            if ($temItem -and -not $antes.EndsWith('[')) { $sep = ',' }
            Grava $keyb ($antes + $sep + "`r`n" + $binding + "`r`n]" + $cru.Substring($iFecha + 1))
        }
    }
}

# -------------------------------------------------------------- resumo
Passo 'Pronto. O que falta e manual:'
Diga '1. FECHE TODAS as janelas do VS Code e abra de novo - o PATH novo e a'
Diga '   extensao so valem para processo novo.'
Diga '2. Fique no modo CHAT do Continue. Agent e Plan dependem de tools, que o'
Diga '   proxy nao tem.'
Diga ''
Diga 'Tarefas: Ctrl+Shift+P > "Run Task" > gptagent: ... (precisa de uma pasta'
Diga 'aberta; sem pasta o VS Code reclama de ${workspaceFolder}).'
if ($Atalho) { Diga 'Atalho: ctrl+alt+g abre "gptagent: fazer um pedido".' }
Diga "Terminal: gptagent --dir . -p `"seu pedido`"  (a chave ja esta em $arqChave)"
