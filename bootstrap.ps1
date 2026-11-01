# agent-setup bootstrap v2 (Windows, no admin) - by Kai
# One-liner:  irm https://raw.githubusercontent.com/dead1786/agent-setup/main/bootstrap.ps1 | iex
# Gives a new user their own AI agent (Claude Code / Codex / Antigravity agy) with a name and personality, Windows-MCP,
# a window that reopens itself when closed, and Telegram messages typed into that same window (same conversation).
# Dry run (changes nothing): run with AGENT_SETUP_DRYRUN=1 in the environment
#   (optional: AGENT_SETUP_DRYRUN_AI=1|2|3, AGENT_SETUP_DRYRUN_TG=1 for a fake bot,
#    AGENT_SETUP_DRYRUN_OUT=<folder> to write the generated files there).

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$env:PYTHONUTF8 = '1'

$DryRun    = ($env:AGENT_SETUP_DRYRUN -eq '1')
$DryOut    = $env:AGENT_SETUP_DRYRUN_OUT
$Root      = 'C:\AI'
$AgentDir  = Join-Path $Root '.agent'
$MemDir    = Join-Path $Root 'memory'
$ToolsDir  = Join-Path $Root 'tools'
$McpDir    = Join-Path $ToolsDir 'windows-mcp'
$SetupDir  = Join-Path $HOME 'agent-setup'
$UserBin   = Join-Path $HOME 'bin'
$LocalBin  = Join-Path $HOME '.local\bin'
$Programs  = Join-Path $env:LOCALAPPDATA 'Programs'
$GitDir    = Join-Path $Programs 'PortableGit'
$NodeDir   = Join-Path $Programs 'nodejs'
$WingetLinks = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'
$PyDir     = Join-Path $AgentDir 'py'
$Py        = Join-Path $PyDir 'Scripts\python.exe'
$Pyw       = Join-Path $PyDir 'Scripts\pythonw.exe'
$Tgterm    = Join-Path $AgentDir 'tgterm.py'
$CfgPath   = Join-Path $AgentDir 'config.json'
$Paused    = Join-Path $AgentDir 'paused'
$ClaudeDir = Join-Path $HOME '.claude'
$TaskName  = 'agent-watchdog'
# the remote helper's Claude session (same account) that receives the setup report; it renames itself to this with /rename
$Coordinator = if ($env:AGENT_SETUP_COORDINATOR) { $env:AGENT_SETUP_COORDINATOR } else { 'agent-coordinator' }
# helper files, pinned to one commit and checked by SHA-256 (a CDN cache can never hand out another version)
$PinCommit = 'f22f22e7f130314dc13efd31d3483f44da9e3a34'
$PinFiles  = [ordered]@{
    'tgterm.py'                  = '5a2bd7415e9218cfa5f1a80fbaf262146410dfb38cd1294b01413a2c7e3274fe'
    'winsend.py'                 = '0089b099a5196819cb891a6339dc03508f23204743adaff4517979e8295e5fa1'
    'skills/winsend/SKILL.md'    = '1f52e2fc29145ab7fa1ca4ac1a39eac9910b5b49306cce2716e452ffd7ada891'
    'skills/memory-rag/SKILL.md' = '21a521973b965eb624b0a1853e01299aefadbc1a2d793ca3bd3ac8cd6fb0de55'
}
$Skills = @('winsend', 'memory-rag')
$script:PathAdded = @()
$RootPreexisted = (Test-Path $Root) -and -not (Test-Path (Join-Path $Root '.agent'))   # the user's own C:\AI
$script:McpPre = $false
$McpTools  = 'Screenshot,Snapshot,DisplayInventory,ControlStatus,WaitFor,Wait,Clipboard,Click,Type,Scroll,Move,Shortcut,App'
$Utf8      = New-Object Text.UTF8Encoding $false

function Say($msg, $color = 'Cyan') { Write-Host $msg -ForegroundColor $color }
function Step($name, [scriptblock]$action) {
    Say "==> $name"
    if ($DryRun) { Say '    [dry-run] skipped' 'DarkGray'; return }
    & $action
}
function Write-Text($path, $text, [switch]$Lf, [switch]$Bom) {
    if ($Lf) { $text = $text -replace "`r`n", "`n" }
    if ($DryRun) {
        if (-not $DryOut) { Say "    [dry-run] would write $path ($($text.Length) chars)" 'DarkGray'; return }
        $path = Join-Path $DryOut ($path -replace ':', '')
    }
    $dir = Split-Path $path -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $enc = if ($Bom) { New-Object Text.UTF8Encoding $true } else { $Utf8 }
    [IO.File]::WriteAllText($path, $text, $enc)
}
function Read-JsonObject($path) {
    if (-not (Test-Path $path)) { return $null }
    try { return (Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}
function Add-UserPath($dir) {
    $cur = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $cur) { $cur = '' }
    if (($cur -split ';') -notcontains $dir) {
        if (-not $DryRun) { [Environment]::SetEnvironmentVariable('Path', ($cur.TrimEnd(';') + ';' + $dir).TrimStart(';'), 'User') }
        $script:PathAdded += $dir                       # recorded for the uninstaller
        Say "    PATH += $dir" 'DarkGray'
    }
    if (($env:Path -split ';') -notcontains $dir) { $env:Path = "$env:Path;$dir" }
}
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Download($url, $out) {
    $dir = Split-Path $out -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
}
function Ask($q, $default) {
    if ($DryRun) { return $default }
    $a = Read-Host $q
    if (-not $a -or -not $a.Trim()) { return $default }
    return $a.Trim()
}
function Fill($template, $map) {
    # placeholders like __NAME__ -> values (plain text replace, no regex)
    foreach ($k in $map.Keys) { $template = $template.Replace($k, [string]$map[$k]) }
    return $template
}
function Merge-Json($target, $patch) {
    # deep-merge with Python's json module (PowerShell 5.1 ConvertTo-Json can truncate deep files)
    $tmp = Join-Path $env:TEMP ('agent-setup-' + [guid]::NewGuid().ToString('N') + '.json')
    [IO.File]::WriteAllText($tmp, ($patch | ConvertTo-Json -Depth 10), $Utf8)
    & $Py $Tgterm merge-json $target $tmp
    $code = $LASTEXITCODE
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    if ($code -ne 0) { throw "merge $target failed (exit $code)" }
}
function Stop-ByCommandLine($pattern, $why) {
    $all = @(Get-CimInstance Win32_Process)
    # never stop this window or anything it runs inside
    $keep = @{}; $p = [long]$PID
    while ($p -and -not $keep.ContainsKey($p)) {
        $keep[$p] = 1
        $parent = $all | Where-Object { [long]$_.ProcessId -eq $p } | Select-Object -First 1
        $p = if ($parent) { [long]$parent.ParentProcessId } else { 0 }
    }
    $all | Where-Object { -not $keep.ContainsKey([long]$_.ProcessId) -and $_.CommandLine -like $pattern } | ForEach-Object {
        Say "    stop $($_.Name) pid $($_.ProcessId) ($why)" 'DarkGray'
        cmd /c "taskkill /PID $($_.ProcessId) /T /F >nul 2>&1"
    }
}

# ---------------------------------------------------------------- safety: real runs must be interactive
if (-not $DryRun -and [Console]::IsInputRedirected) {
    throw 'agent-setup must be run in an interactive PowerShell window (or set AGENT_SETUP_DRYRUN=1 for a dry run).'
}

Say ''
Say '=================================================' 'Green'
Say '  AI 助理安裝：只問一次，答完就全自動完成' 'Green'
Say '                                       by Kai' 'Green'
Say '=================================================' 'Green'

try {
# ---------------------------------------------------------------- 0. pause a previous install while we work
Step '暫停舊的設定（重新安裝時避免互搶；裝完會自動重開）' {
    if (Test-Path $AgentDir) { Set-Content -Path $Paused -Value 'setup' }
    if ((Test-Path $Py) -and (Test-Path $Tgterm) -and (Test-Path $CfgPath)) { & $Py $Tgterm close-window | Out-Host; Start-Sleep -Seconds 2 }
    Stop-ByCommandLine '*run-agent.ps1*' 'old AI window'
    Stop-ByCommandLine '*tgterm.py*bridge*' 'old Telegram bridge'
    Stop-ByCommandLine '*tg-bridge*bridge.py*' 'v1 Telegram bridge'
    Stop-ByCommandLine '*--remote-control agent-setup*' 'old setup session'
    $old = Join-Path ([Environment]::GetFolderPath('Startup')) 'agent-tg-bridge.vbs'
    if (Test-Path $old) { Remove-Item $old -Force; Say "    removed $old" 'DarkGray' }
}

# ---------------------------------------------------------------- 1. questions (once)
$prev = Read-JsonObject (Join-Path $AgentDir 'answers.json')
$prevCfg = Read-JsonObject $CfgPath
$reuse = $false
$tgBase = $null
if (-not $DryRun -and $prev -and $prev.ai) {
    $prevTg = if ($prevCfg -and $prevCfg.token) { '@' + $prev.bot } else { '不接' }
    $prevRemote = if ($prev.remote -eq $false) { '不要' } else { '要' }
    Say ('找到上次的答案：遠端協助=' + $prevRemote + '　AI=' + $prev.ai + '　名字=' + $prev.agent_name + '　Telegram=' + $prevTg) 'Green'
    $reuse = ((Read-Host '沿用上次的答案？ [Y/n]') -notmatch '^\s*[nN]')
}
if ($reuse) {
    $remote = ($prev.remote -ne $false)
    $ai = $prev.ai; $agentName = $prev.agent_name; $personality = $prev.personality; $style = $prev.style
    $wants = $prev.wants; $userName = $prev.user_name; $lang = $prev.language; $userWork = $prev.user_work
    $botName = $prev.bot
    $tgToken = $prevCfg.token
    $ownerId = $prevCfg.owner_id
} else {
    # 1. remote help or not: an explicit Y/N, no default, so nobody skips past what it means
    Say ''
    Say '1) 要不要「遠端安裝協助」？' 'Cyan'
    Say '   Y＝要：' 'Cyan'
    Say '     ・會暫時安裝 Claude Code 當「安裝助理」，由協助你的人用他自己的手機授權登入' 'Gray'
    Say '     ・裝完自動檢查，並把檢查報告傳給協助者確認；確認後安裝助理自動結束、登出協助者的帳號' 'Gray'
    Say '     ・安裝期間，協助者可以遠端看到安裝助理、請它重跑檢查或修正安裝檔（不能動你的其他檔案）' 'Gray'
    Say '     ・會傳給協助者的資料：AI 名字與個性設定、你填的工作內容、軟體版本、Telegram 機器人名稱與你的 Telegram 帳號 ID、各項測試結果（不含密碼、不含 Telegram token）' 'Gray'
    Say '   N＝不要：' 'Cyan'
    Say '     ・全部在這台電腦完成，不會有任何資料傳給別人' 'Gray'
    Say '     ・選 Codex／Gemini 不會安裝 Claude Code；選 Claude 就用你自己的帳號正常登入' 'Gray'
    Say '     ・最後在這個視窗自動檢查，結果只顯示在畫面上；有問題要自己看畫面處理，或重新貼一次安裝指令' 'Gray'
    $remote = $null
    while ($null -eq $remote) {
        $r = if ($DryRun) { $(if ($env:AGENT_SETUP_DRYRUN_REMOTE -eq '0') { 'n' } else { 'y' }) } else { [string](Read-Host '   請輸入 Y 或 N') }
        if ($r -match '^\s*[yY]') { $remote = $true } elseif ($r -match '^\s*[nN]') { $remote = $false } else { Say '    請輸入 Y 或 N' 'Yellow' }
    }

    $ai = $null
    $d1 = if ($DryRun -and $env:AGENT_SETUP_DRYRUN_AI) { $env:AGENT_SETUP_DRYRUN_AI } else { '1' }
    while (-not $ai) {
        switch (Ask '2) 要用哪個 AI？ 1=Claude Code  2=Codex  3=Gemini（Antigravity）  [預設 1]' $d1) {
            '1' { $ai = 'claude' } '2' { $ai = 'codex' } '3' { $ai = 'gemini' }
            default { Say '    請輸入 1、2 或 3' 'Yellow' }
        }
    }
    $agentName   = Ask '3) 幫你的 AI 取個名字 [預設 小助手]' '小助手'
    $personality = Ask '4) 它的個性？（例：溫柔、毒舌、幽默、冷靜）[預設 親切可靠]' '親切可靠'
    $style       = Ask '5) 說話風格？（例：簡短直接、像朋友聊天、條列重點）[預設 簡短清楚]' '簡短清楚'
    $wants       = Ask '6) 希望它主要幫你做什麼？（例：查資料、寫文件、整理行程）[預設 日常大小事]' '日常大小事'
    $userName    = Ask '7) 它要怎麼稱呼你？ [預設 主人]' '主人'
    $lang        = Ask '8) 回覆用什麼語言？ [預設 繁體中文]' '繁體中文'
    $userWork    = Ask '9) 你目前在做什麼工作或專案？（讓它更懂你；可直接按 Enter 跳過）' ''

    $tgToken = $null; $botName = $null; $ownerId = $null
    if ($DryRun -and $env:AGENT_SETUP_DRYRUN_TG -eq '1') { $tgToken = 'dry-run-token'; $botName = 'dry_run_bot'; $ownerId = [long]123456789 }
    while (-not $DryRun) {
        $t = (Read-Host '10) Telegram 機器人 token（找 @BotFather 開機器人拿到的；不接 Telegram 直接按 Enter）').Trim()
        if (-not $t) { break }
        try { $me = Invoke-RestMethod -Uri "https://api.telegram.org/bot$t/getMe" -TimeoutSec 20 } catch { $me = $null }
        if ($me -and $me.ok) { $tgToken = $t; $botName = $me.result.username; Say "    OK：@$botName" 'Green'; break }
        Say '    這個 token 驗證失敗，請再貼一次（或直接按 Enter 跳過）' 'Yellow'
    }
    if ($tgToken -and -not $DryRun) {
        $api = "https://api.telegram.org/bot$tgToken"
        try { $wh = Invoke-RestMethod "$api/getWebhookInfo" -TimeoutSec 20; if ($wh.result.url) { $null = Invoke-RestMethod "$api/deleteWebhook" -TimeoutSec 20 } } catch {}
        # ignore anything sent to the bot before now
        $tgBase = 0
        try { $old = Invoke-RestMethod "$api/getUpdates?offset=-1&timeout=0" -TimeoutSec 20; if (@($old.result).Count) { $tgBase = [long]@($old.result)[-1].update_id + 1 } } catch {}
        while (-not $ownerId) {
            $null = Read-Host "11) 用你自己的 Telegram 打開 @$botName，按「開始」或傳任何一句話；傳完回來這裡按 Enter"
            try {
                $up = Invoke-RestMethod "$api/getUpdates?offset=$tgBase&timeout=10" -TimeoutSec 30
                $m = @($up.result) | Where-Object { $_.message -and $_.message.chat.type -eq 'private' } | Select-Object -First 1
                if ($m) {
                    $ownerId = [long]$m.message.'from'.id
                    $ownerName = (@($m.message.'from'.first_name, $m.message.'from'.last_name) -join ' ').Trim()
                    $tgBase = [long]@($up.result)[-1].update_id + 1
                    $null = Invoke-RestMethod "$api/getUpdates?offset=$tgBase&timeout=0" -TimeoutSec 20
                    Say "    抓到了：$ownerName（$ownerId）。只有這個 Telegram 帳號能跟 AI 對話。" 'Green'
                } else { Say '    還沒收到，請再傳一次後按 Enter' 'Yellow' }
            } catch { Say '    連線失敗，請再試一次' 'Yellow' }
        }
    }
}

# names go into window titles and scripts: keep them plain
$agentName = (($agentName -replace '[\x00-\x1f"''`$;|&<>^%]', '') -replace '\s+', ' ').Trim()
if (-not $agentName) { $agentName = 'Agent' }
$tg = [bool]$tgToken
# Claude Code is needed for the setup assistant (remote help) or as the AI itself
$NeedClaude = ($remote -or $ai -eq 'claude')

$answers = [ordered]@{ remote = $remote; ai = $ai; agent_name = $agentName; personality = $personality; style = $style; wants = $wants
    user_name = $userName; language = $lang; user_work = $userWork; bot = $botName; owner_id = $ownerId; created = (Get-Date).ToString('s') }
Write-Text (Join-Path $AgentDir 'answers.json') ($answers | ConvertTo-Json)
Say ('設定：遠端協助=' + $(if ($remote) { '要' } else { '不要' }) + '　AI=' + $ai + '　名字=' + $agentName + '　稱呼=' + $userName + '　語言=' + $lang + '　Telegram=' + $(if ($tg) { '@' + $botName } else { '不接' })) 'Green'

if (-not $DryRun) {
    New-Item -ItemType Directory -Force -Path $Root, $AgentDir, $MemDir, $ToolsDir, $SetupDir, $UserBin | Out-Null
    Start-Transcript -Path (Join-Path $SetupDir ('bootstrap-' + (Get-Date).ToString('yyyyMMdd-HHmmss') + '.log')) -Append | Out-Null
}

# ---------------------------------------------------------------- 2. tools (user-level, no admin)
# always call the uv in ~/.local/bin by full path (an older uv elsewhere on PATH may be too old)
$UvExe = Join-Path $LocalBin 'uv.exe'
# what is already here before this run: the uninstaller removes only programs this installer added
$PrevMan = Read-JsonObject (Join-Path $AgentDir 'installed.json')
$Present = {
    [ordered]@{
        uv     = (Test-Path $UvExe)
        git    = (Test-Path (Join-Path $GitDir 'cmd\git.exe'))
        jq     = (Test-Path (Join-Path $UserBin 'jq.exe'))
        claude = ((Have 'claude') -or (Test-Path (Join-Path $LocalBin 'claude.exe')))
        node   = (Test-Path (Join-Path $NodeDir 'node.exe'))
        codex  = (Have 'codex')
        agy    = (Have 'agy')
    }
}
$Pre = & $Present
Step 'uv（Python 工具）' {
    if (-not (Test-Path $UvExe)) { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression }
    Add-UserPath $LocalBin
    # an old uv cannot fetch the Python that Windows-MCP needs: update it, or reinstall if self-update is unavailable
    & $UvExe self update
    if ($LASTEXITCODE -ne 0) { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression }
    & $UvExe --version; Check 'uv'
}

# sign-in hook: Claude runs $env:BROWSER with the sign-in link; this exe sends it out as a QR code instead of opening
# a browser here, so the installer signs in on their own phone and this computer's browser never holds their session
$HookDir   = Join-Path $AgentDir 'login-hook'
$LoginHook = Join-Path $PyDir 'Scripts\agent-login-url.exe'
Write-Text (Join-Path $HookDir 'pyproject.toml') @'
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "agent-login-hook"
version = "1.0.0"

[project.scripts]
agent-login-url = "agent_login_hook:main"

[tool.setuptools]
py-modules = ["agent_login_hook"]
'@
Write-Text (Join-Path $HookDir 'agent_login_hook.py') @'
# BROWSER hook for `claude auth login`: hands the sign-in link to tgterm.py instead of opening a browser here
import runpy, sys

TGTERM = r"C:\AI\.agent\tgterm.py"


def main():
    sys.argv = [TGTERM, "login-url"] + sys.argv[1:]
    runpy.run_path(TGTERM, run_name="__main__")
'@

Step 'Python（Telegram 橋接、看門狗、登入 QR code、螢幕截圖用）' {
    if (-not (Test-Path $Py)) { & $UvExe venv --python 3.12 $PyDir; Check 'uv venv' }
    if (-not (Test-Path $Pyw)) { throw "pythonw.exe missing in $PyDir" }
    & $Py --version; Check 'python'
    & $UvExe pip install --python $Py --reinstall-package agent-login-hook segno pillow $HookDir; Check 'uv pip install'
    if (-not (Test-Path $LoginHook)) { throw "sign-in hook missing: $LoginHook" }
}

Step '輔助程式（Telegram 橋接、看門狗、LINE／視窗送字、說明書）' {
    foreach ($rel in $PinFiles.Keys) {
        $dest = Join-Path $AgentDir ($rel -replace '/', '\')
        $tmp = $dest + '.download'
        Download "https://raw.githubusercontent.com/dead1786/agent-setup/$PinCommit/$rel" $tmp
        $hash = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
        if ($hash -ne $PinFiles[$rel]) { Remove-Item $tmp -Force; throw "$rel checksum mismatch: $hash" }
        Move-Item $tmp $dest -Force
    }
    & $Py $Tgterm selftest; Check 'tgterm selftest'
    & $Py (Join-Path $AgentDir 'winsend.py') selftest; Check 'winsend selftest'
}

Step 'Git（PortableGit）' {
    $gitExe = Join-Path $GitDir 'cmd\git.exe'
    if (-not (Test-Path $gitExe)) {
        $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/git-for-windows/git/releases/latest' -Headers @{ 'User-Agent' = 'agent-setup' }
        $asset = $rel.assets | Where-Object { $_.name -match '^PortableGit-.*-64-bit\.7z\.exe$' } | Select-Object -First 1
        if (-not $asset) { throw 'PortableGit asset not found' }
        $sfx = Join-Path $env:TEMP $asset.name
        Download $asset.browser_download_url $sfx
        & $sfx -y "-o$GitDir" | Out-Null
        Check 'PortableGit extraction'
        Remove-Item $sfx -Force -ErrorAction SilentlyContinue
    }
    Add-UserPath (Join-Path $GitDir 'cmd')
    Add-UserPath (Join-Path $GitDir 'bin')
    & $gitExe --version; Check 'git'
}

Step 'jq' {
    $jq = Join-Path $UserBin 'jq.exe'
    if (-not (Test-Path $jq)) { Download 'https://github.com/jqlang/jq/releases/latest/download/jq-windows-amd64.exe' $jq }
    Add-UserPath $UserBin
    & $jq --version; Check 'jq'
}

if ($NeedClaude) {
    Step $(if ($ai -eq 'claude') { 'Claude Code' } else { 'Claude Code（只給遠端安裝助理用）' }) {
        if (-not (Have 'claude') -and -not (Test-Path (Join-Path $LocalBin 'claude.exe'))) { Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression }
        Add-UserPath $LocalBin
        if (-not (Have 'claude')) { throw 'Claude Code install failed: claude not found' }
        claude --version; Check 'claude'
    }
}

if ($ai -eq 'codex') {
    Step 'Node.js（Codex 需要）' {
        if (-not (Test-Path (Join-Path $NodeDir 'node.exe'))) {
            $sums = (Invoke-WebRequest -Uri 'https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt' -UseBasicParsing).Content
            $line = ($sums -split "`n") | Where-Object { $_ -match 'node-v[\d\.]+-win-x64\.zip' } | Select-Object -First 1
            $zipName = ($line -split '\s+')[-1].Trim()
            $zip = Join-Path $env:TEMP $zipName
            Download "https://nodejs.org/dist/latest-v22.x/$zipName" $zip
            $tmp = Join-Path $env:TEMP 'node-extract'
            if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
            Expand-Archive $zip -DestinationPath $tmp -Force
            $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
            if (Test-Path $NodeDir) { Remove-Item $NodeDir -Recurse -Force }
            Move-Item $inner.FullName $NodeDir
            Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
        Add-UserPath $NodeDir
        Add-UserPath (Join-Path $env:APPDATA 'npm')
        & (Join-Path $NodeDir 'node.exe') --version
    }
    Step 'Codex CLI' { & (Join-Path $NodeDir 'npm.cmd') install -g '@openai/codex'; Check 'npm codex'; codex --version }
}
if ($ai -eq 'gemini') {
    # Gemini CLI no longer accepts personal Google accounts; Google's Antigravity CLI (agy) replaces it
    Step 'Antigravity CLI（agy）' {
        Add-UserPath $WingetLinks
        if (-not (Have 'agy')) {
            if (-not (Have 'winget')) { throw 'winget not found: install "App Installer" from the Microsoft Store, then run this again' }
            winget install --id Google.AntigravityCLI -e --accept-source-agreements --accept-package-agreements --disable-interactivity
            Check 'winget install Google.AntigravityCLI'
        }
        agy --version; Check 'agy'
    }
}

# ---------------------------------------------------------------- 3. Windows-MCP
$McpArgs = @('--directory', $McpDir, 'run', 'windows-mcp', 'serve', '--tools', $McpTools)
Step 'Windows-MCP（CursorTouch，只開 13 個低風險工具）' {
    $gitExe = Join-Path $GitDir 'cmd\git.exe'
    if (-not (Test-Path (Join-Path $McpDir '.git'))) { & $gitExe clone --depth 1 https://github.com/CursorTouch/Windows-MCP.git $McpDir; Check 'git clone windows-mcp' }
    & $UvExe --directory $McpDir sync; Check 'uv sync windows-mcp'
}
if ($NeedClaude) {
    Step 'Windows-MCP 接到 Claude Code' {
        $listed = (cmd /c "claude mcp list 2>&1" | Out-String)
        if ($listed -match 'windows-mcp' -and $listed -notmatch [regex]::Escape($McpDir)) {
            # the user already has their own windows-mcp: leave it alone (the uninstaller will too)
            $script:McpPre = $true; Say '    已有自己的 windows-mcp，保留不動' 'DarkGray'; return
        }
        if ($listed -match 'windows-mcp') { cmd /c "claude mcp remove windows-mcp --scope user >nul 2>&1" }
        claude mcp add windows-mcp --scope user --transport stdio --env ANONYMIZED_TELEMETRY=false -- $UvExe @McpArgs; Check 'claude mcp add'
    }
}
if ($ai -eq 'codex') {
    Step 'Windows-MCP 接到 Codex（並信任 C:\AI）' {
        $cfg = Join-Path $HOME '.codex\config.toml'
        $old = if (Test-Path $cfg) { [IO.File]::ReadAllText($cfg) } else { '' }
        $add = ''
        if ($old -notmatch '\[mcp_servers\.windows-mcp\]') {
            # TOML basic strings: escape \ and " (user folders can contain anything)
            $toml = { param($s) '"' + (($s -replace '\\', '\\') -replace '"', '\"') + '"' }
            $argsToml = ($McpArgs | ForEach-Object { & $toml $_ }) -join ', '
            $add += "`n[mcp_servers.windows-mcp]`ncommand = " + (& $toml $UvExe) + "`nargs = [$argsToml]`nstartup_timeout_sec = 60`nenv = { ANONYMIZED_TELEMETRY = `"false`" }`n"
        }
        if ($old -notmatch [regex]::Escape("[projects.'$Root']")) { $add += "`n[projects.'$Root']`ntrust_level = 'trusted'`n" }
        if ($add) { Write-Text $cfg ($old + $add) }
    }
}
if ($ai -eq 'gemini') {
    Step 'Windows-MCP 接到 Antigravity（並信任 C:\AI）' {
        # global MCP config per agy's built-in docs: ~/.gemini/config/mcp_config.json
        Merge-Json (Join-Path $HOME '.gemini\config\mcp_config.json') @{
            mcpServers = @{ 'windows-mcp' = [ordered]@{ command = $UvExe; args = $McpArgs; env = @{ ANONYMIZED_TELEMETRY = 'false' } } }
        }
        $agySettings = Join-Path $HOME '.gemini\antigravity-cli\settings.json'
        $cur = Read-JsonObject $agySettings
        $trusted = @(); if ($cur -and $cur.trustedWorkspaces) { $trusted = @($cur.trustedWorkspaces) }
        if ($trusted -notcontains $Root) { $trusted += $Root }
        Merge-Json $agySettings @{ trustedWorkspaces = $trusted }
    }
}

if ($ai -eq 'claude') {
    Step 'Claude 說明書（skills：LINE／視窗送字、進階記憶與 RAG）' {
        foreach ($s in $Skills) {
            $dst = Join-Path $ClaudeDir "skills\$s"
            New-Item -ItemType Directory -Force -Path $dst | Out-Null
            Copy-Item (Join-Path $AgentDir "skills\$s\SKILL.md") (Join-Path $dst 'SKILL.md') -Force
        }
    }
}

# install record for the uninstaller: a program counts as ours if this run or an earlier run installed it
$Now = & $Present
$Ours = [ordered]@{}
foreach ($k in $Pre.Keys) { $Ours[$k] = [bool](($PrevMan -and $PrevMan.ours -and $PrevMan.ours.$k) -or ((-not $Pre[$k]) -and $Now[$k])) }
$prevPaths = if ($PrevMan -and $PrevMan.path_added) { @($PrevMan.path_added) } else { @() }
$pathAdded = @($prevPaths + $script:PathAdded | Where-Object { $_ } | Select-Object -Unique)
$rootPre = [bool](($PrevMan -and $PrevMan.root_preexisted) -or $RootPreexisted)
$mcpPre = [bool](($PrevMan -and $PrevMan.mcp_preexisting) -or $script:McpPre)
Write-Text (Join-Path $AgentDir 'installed.json') ([ordered]@{ ours = $Ours; path_added = $pathAdded; ai = $ai; remote = $remote
    root_preexisted = $rootPre; mcp_preexisting = $mcpPre
    updated = (Get-Date).ToString('s') } | ConvertTo-Json -Depth 5)

# ---------------------------------------------------------------- 4. Claude settings (setup phase) + status line
$BashExe = (Join-Path $GitDir 'bin\bash.exe') -replace '\\', '/'
$StatusScript = Join-Path $ClaudeDir 'agent-setup-statusline.sh'
if ($NeedClaude) {
    Step $(if ($remote) { 'Claude 設定（安裝期間：免確認、接收協調訊息、略過首次導覽、信任工作資料夾）' } else { 'Claude 設定（略過首次導覽、信任工作資料夾）' }) {
        $patch = [ordered]@{
            skipDangerousModePermissionPrompt = $true
            statusLine = [ordered]@{ type = 'command'; command = "`"$BashExe`" `"" + ($StatusScript -replace '\\', '/') + '"' }
        }
        if ($remote) {
            # setup phase only (handoff.ps1 removes these): the coordinator can message the setup assistant, no permission prompts,
            # and the installer's claude.ai connectors (Gmail, Drive, Calendar...) never reach this machine
            $patch.crossSessionInbound = 'accept'
            $patch.disableClaudeAiConnectors = $true
            $patch.permissions = @{ defaultMode = 'bypassPermissions' }
        }
        Merge-Json (Join-Path $ClaudeDir 'settings.json') $patch
        $projects = @{}
        $projects['C:/AI'] = @{ hasTrustDialogAccepted = $true }
        if ($remote) { $projects[($SetupDir -replace '\\', '/')] = @{ hasTrustDialogAccepted = $true } }
        Merge-Json (Join-Path $HOME '.claude.json') ([ordered]@{ hasCompletedOnboarding = $true; projects = $projects })
    }
}
$statusline = @'
#!/usr/bin/env bash
export PATH="$HOME/bin:$PATH"
input=$(cat)
reset=$'\033[0m'; green=$'\033[32m'; yellow=$'\033[33m'; red=$'\033[31m'
model=$(echo "$input" | jq -r '.model.display_name // "Unknown"')
used_pct=$(echo "$input" | jq -r '.context_window.used_percentage // 0'); used_int=${used_pct%.*}; used_int=${used_int:-0}
bar=""; for ((i=0; i<used_int/10; i++)); do bar+="█"; done; for ((i=used_int/10; i<10; i++)); do bar+="░"; done
if [ "$used_int" -lt 50 ]; then bc=$green; elif [ "$used_int" -lt 80 ]; then bc=$yellow; else bc=$red; fi
pct_str() { local v=$1; [ -z "$v" ] && { echo "—"; return; }; v=${v%.*}; v=${v:-0}; local c=$green; [ "$v" -ge 50 ] && c=$yellow; [ "$v" -ge 80 ] && c=$red; echo "${c}${v}%${reset}"; }
five=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
seven=$(echo "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty')
resets=$(echo "$input" | jq -r '.rate_limits.five_hour.resets_at // 0'); remain=$(( ${resets%.*} - $(date +%s) ))
five_str="5h: $(pct_str "$five")"; if [ -n "$five" ] && [ "$remain" -gt 0 ]; then five_str="$five_str ($((remain/3600))時$(((remain%3600)/60))分後重置)"; fi
dur=$(echo "$input" | jq -r '.cost.total_duration_ms // 0'); dur=$(( ${dur%.*} / 1000 ))
echo "🤖 ${model} │ 💭 ${bc}${bar}${reset} ${used_int}%"
echo "📊 ${five_str} │ 7d: $(pct_str "$seven") │ ⏱️ $((dur/60))分$((dur%60))秒"
'@
if ($NeedClaude) { Write-Text $StatusScript $statusline -Lf }

# ---------------------------------------------------------------- 5. the agent: rules (name + personality) and memory
$rulesName = switch ($ai) { 'claude' { 'CLAUDE.md' } 'codex' { 'AGENTS.md' } 'gemini' { 'GEMINI.md' } }
$RulesPath = Join-Path $Root $rulesName
$workLine = if ($userWork) { "- $userName 目前在做：$userWork" } else { "- $userName 目前在做什麼還不知道，有機會就問，並記進記憶。" }
$tgSection = if (-not $tg) { "## Telegram`n- 目前沒有接 Telegram。" } else { @"
## Telegram（@$botName）
- 開頭是 [TG] 的訊息，是 $userName 從 Telegram 傳來的，$userName 看不到這個視窗。處理完一定要用下面的指令把回覆傳回 Telegram，不然收不到：
  - PowerShell／cmd：``C:\AI\.agent\py\Scripts\python.exe C:\AI\.agent\tgterm.py send "回覆內容"``
  - Git Bash：``/c/AI/.agent/py/Scripts/python.exe C:/AI/.agent/tgterm.py send "回覆內容"``
  - 以下說的「tgterm」都是指上面那一整串指令。
- 回覆有多行或含雙引號時：先把內容寫進 UTF-8 文字檔，再用 ``tgterm send --text-file 檔案路徑``。
- 傳圖片／檔案／影片給 $userName：``tgterm send --photo 路徑 說明``、``tgterm send --doc 路徑 說明``、``tgterm send --video 路徑 說明``（單檔上限 50MB、圖片 10MB）。
- $userName 傳來的東西會帶標籤，後面是已經存好的檔案路徑：[圖片] [檔案] [影片] [動圖] [語音] [音訊] [貼圖]。圖片你可以直接看；[語音] 會附上轉好的文字；影片和音訊你看不到內容，只能當檔案處理。
- 要做比較久的事，先傳一句「收到，處理中」，做完再傳結果。
- 沒有 [TG] 開頭的訊息，是 $userName 在電腦前打字，直接在這裡回覆就好。
- 這個指令只會傳給 $userName 本人；不要用任何方式替 $userName 對外發訊息。
- $userName 在 Telegram 也能用 /status /stop /restart /new /screen 遙控這個視窗，這些不會送到你這裡。
"@ }
$remindNotify = if ($tg) { '，做完用 Telegram 告訴' + $userName } else { '' }
$greetTg = if ($tg) { '有接 Telegram，所以也用 tgterm send 傳一句打招呼。' } else { '' }
$extraSections = @"
## 提醒與排程
- [提醒] 開頭的訊息是排好的時間到了：照內容去做（提醒 $userName、整理晨報…）$remindNotify。
- $userName 說「幾點提醒我…」「每天早上…」時，用這些指令建立（不用另外開排程，指令就是 ``C:\AI\.agent\py\Scripts\python.exe C:\AI\.agent\tgterm.py remind ...``）：
  - 每天：``remind add --daily 08:00 "內容"``
  - 每週：``remind add --weekly 1,3,5 08:00 "內容"``（1＝週一 … 7＝週日）
  - 一次：``remind add --once "2026-11-02 15:00" "內容"``，或 ``remind add --in 30 "內容"``（30 分鐘後）
  - 查看／刪除：``remind list``、``remind del r1``
- 「內容」是寫給未來的自己看的：寫清楚到時要做什麼、要不要通知 $userName。
- 電腦關機時錯過的提醒，開機後 6 小時內會補發。

## 說明書（需要時才讀）
- 傳 LINE 訊息、把文字送到其他視窗：先讀 C:\AI\.agent\skills\winsend\SKILL.md。只有 $userName 明確要求才做，發之前先複述對象和內容。
- 進階記憶（L1～L6 分層）、整理記憶、RAG 語意搜尋：先讀 C:\AI\.agent\skills\memory-rag\SKILL.md。很花用量，只在 $userName 要求時做，平常不要主動建立。

## 第一次見面
- 如果 C:\AI\memory 裡還沒有「認識主人.md」：收到 [系統] 第一次見面的通知、或 $userName 第一次跟你說話時，先用你的個性打招呼、自我介紹（你的名字、能幫什麼、怎麼用 Telegram 和提醒），再一次問 3～5 個問題認識 $userName（工作、家人或重要的人、興趣、作息、最希望你幫什麼）。
- 把回答整理寫進 C:\AI\memory\認識主人.md，並在 MEMORY.md 加一行索引。$greetTg
"@
$rules = @"
# $agentName

## 我是誰
- 我叫「$agentName」，是 $userName 的專屬 AI 助理。
- 個性：$personality
- 說話風格：$style
- 主要幫 $userName ：$wants

## $userName
- 稱呼對方「$userName」，一律用$lang 回覆。
$workLine

## 做事方式
- 工作資料夾是 C:\AI。不確定就先查證，不知道就說不知道。
- 刪除檔案、花錢、替 $userName 對外發訊息或發文之前，一定先問。
- 這個視窗被關掉後，1 分鐘內會自動重開；重開後先讀記憶再接著做。

## 記憶
- 記憶放在 C:\AI\memory\：一個檔記一件事（$userName 的偏好、交代過的規則、進行中的事），並在 C:\AI\memory\MEMORY.md 加一行索引。
- 每次開始先讀 C:\AI\memory\MEMORY.md。$userName 說「記住」就寫進記憶。

## 工具
- 已接 Windows-MCP：可以看螢幕、截圖、點擊、打字、捲動、切換視窗、讀寫剪貼簿。

$tgSection

$extraSections
"@
if (-not $DryRun -and (Test-Path $RulesPath) -and ([IO.File]::ReadAllText($RulesPath) -ne $rules)) {
    Copy-Item $RulesPath ($RulesPath + '.bak-' + (Get-Date).ToString('yyyyMMddHHmmss')) -Force
}
Write-Text $RulesPath $rules
if ($DryRun -or -not (Test-Path (Join-Path $MemDir 'MEMORY.md'))) { Write-Text (Join-Path $MemDir 'MEMORY.md') "# 記憶索引`n`n（一行一個記憶檔）`n" }

# ---------------------------------------------------------------- 6. tgterm config, the agent window, the watchdog
$mode = if ($prevCfg -and $prevCfg.mode) { [string]$prevCfg.mode } else { 'console' }
$cfg = [ordered]@{ agent_name = $agentName; ai = $ai; root = $Root; token = $tgToken; owner_id = $ownerId; bot = $botName; prefix = '[TG] '; mode = $mode
    uv = $UvExe }
Write-Text $CfgPath ($cfg | ConvertTo-Json)
if ($null -ne $tgBase) { Write-Text (Join-Path $AgentDir 'tgterm-state.json') ((@{ offset = $tgBase }) | ConvertTo-Json) }

$runAgent = @'
# Runs the AI in this window and restarts it whenever it exits.
# Closing the window stops it; the watchdog reopens it within a minute. To keep it closed, create C:\AI\.agent\paused
$ErrorActionPreference = 'Continue'
$name = '__NAME__'
$agentDir = '__AGENTDIR__'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
$env:CLAUDE_CODE_DISABLE_TERMINAL_TITLE = '1'
[IO.File]::WriteAllText((Join-Path $agentDir 'agent.pid'), [string]$PID)
Set-Location '__ROOT__'
$quick = 0
while ($true) {
    $Host.UI.RawUI.WindowTitle = $name
    $started = Get-Date
    # right after an install start a new conversation; after that, pick up where it left off
    $fresh = Join-Path $agentDir 'fresh-start'
    $resume = $false
    if (Test-Path $fresh) { Remove-Item $fresh -Force } elseif ($quick -eq 0) { $resume = $true }
    switch ('__AI__') {
        'claude' {
            $a = @('--dangerously-skip-permissions', '--name', $name)
            if ($resume -and (Get-ChildItem (Join-Path $HOME '.claude\projects\C--AI') -Filter '*.jsonl' -ErrorAction SilentlyContinue)) { $a += '--continue' }
            & claude @a
        }
        'codex'  { & codex --dangerously-bypass-approvals-and-sandbox }
        'gemini' {
            $a = @('--dangerously-skip-permissions')
            if ($resume -and (Get-ChildItem (Join-Path $HOME '.gemini\antigravity-cli\conversations') -ErrorAction SilentlyContinue)) { $a += '--continue' }
            & agy @a
        }
    }
    if (((Get-Date) - $started).TotalSeconds -lt 20) { $quick++ } else { $quick = 0 }
    $wait = if ($quick -ge 3) { 60 } else { 3 }
    Write-Host "（AI 已結束，$wait 秒後自動重新開啟；要停止請直接關掉這個視窗）" -ForegroundColor Yellow
    Start-Sleep -Seconds $wait
}
'@
$map = @{ '__NAME__' = $agentName; '__AGENTDIR__' = $AgentDir; '__ROOT__' = $Root; '__AI__' = $ai }
Write-Text (Join-Path $AgentDir 'run-agent.ps1') (Fill $runAgent $map) -Bom

# ---------------------------------------------------------------- 7. verify + handoff (run by the setup session / at delivery)
$verify = @'
# agent-setup verify: checks everything and ends with a PASS/FAIL summary.
#   -SkipInject    do not type a test message into the AI window
#   -SkipWatchdog  do not close the AI window to test that it reopens
param([switch]$SkipInject, [switch]$SkipWatchdog)
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
$env:PYTHONUTF8 = '1'
$ai = '__AI__'; $name = '__NAME__'; $tg = __TG__
$agentDir = '__AGENTDIR__'; $py = '__PY__'; $tgterm = Join-Path $agentDir 'tgterm.py'; $cfgPath = Join-Path $agentDir 'config.json'
$results = New-Object System.Collections.Specialized.OrderedDictionary
function Section($t) { Write-Output ''; Write-Output ('### ' + $t) }
function Result($key, $ok, $detail) { $results[$key] = $(if ($ok) { 'PASS' } else { 'FAIL' }) + $(if ($detail) { '  ' + $detail } else { '' }) }
function Tgterm { & $py $tgterm @args 2>&1 | ForEach-Object { [string]$_ } }
function Status { $s = @{}; foreach ($l in (Tgterm status)) { if ($l -match '^([a-z_]+)=(.*)$') { $s[$Matches[1]] = $Matches[2] } }; $s }

Section 'answers'
Get-Content (Join-Path $agentDir 'answers.json') -Encoding UTF8

Section 'versions'
$tools = @('uv', 'git', 'jq')
if (__CLAUDE__) { $tools += @('claude') }
if ($ai -eq 'codex') { $tools += @('node', 'codex') }
if ($ai -eq 'gemini') { $tools += @('agy') }
foreach ($c in $tools) {
    if (-not (Get-Command $c -ErrorAction SilentlyContinue)) { Result ('tool ' + $c) $false 'not found'; continue }
    $vout = & $c --version 2>&1
    $code = $LASTEXITCODE
    $v = [string](@($vout) | Select-Object -First 1)
    Write-Output ($c + ': ' + $v)
    Result ('tool ' + $c) ($code -eq 0) $v
}
Write-Output ('python: ' + ((& $py --version 2>&1) -join ''))
$pil = (& $py -c "import PIL; print(PIL.__version__)" 2>&1) -join ''
Result 'pillow (screenshots)' ($LASTEXITCODE -eq 0) $pil
$ws = (& $py (Join-Path $agentDir 'winsend.py') selftest 2>&1) -join ' '
Result 'winsend selftest' ($LASTEXITCODE -eq 0) $ws
Result 'skills present' ((Test-Path (Join-Path $agentDir 'skills\winsend\SKILL.md')) -and (Test-Path (Join-Path $agentDir 'skills\memory-rag\SKILL.md'))) ''
Result 'install record' (Test-Path (Join-Path $agentDir 'installed.json')) ''

Section 'windows-mcp'
git -C '__MCPDIR__' log -1 --oneline
switch ($ai) {
    'claude' {
        $reg = (claude mcp list 2>&1 | Out-String); Write-Output $reg
        Result 'windows-mcp connected' ($reg -match 'windows-mcp.*Connected') 'claude mcp list'
        if (__REMOTE__) { Result 'claude.ai connectors off' ($reg -notmatch 'claude\.ai ') 'no Gmail/Drive/Calendar of the installer account' }
    }
    'codex'  { $reg = Get-Content (Join-Path $HOME '.codex\config.toml') -Raw -Encoding UTF8; Result 'windows-mcp registered' ($reg -match 'mcp_servers\.windows-mcp') 'codex config.toml' }
    'gemini' { $reg = Get-Content (Join-Path $HOME '.gemini\config\mcp_config.json') -Raw -Encoding UTF8; Result 'windows-mcp registered' ($reg -match 'windows-mcp') 'antigravity mcp_config.json' }
}

Section 'rules + memory'
Result 'rules file' (Test-Path '__RULES__') '__RULES__'
Result 'memory index' (Test-Path '__ROOT__\memory\MEMORY.md') ''

Section 'scheduled task'
try {
    $svc = New-Object -ComObject Schedule.Service; $svc.Connect()
    $task = $svc.GetFolder('\').GetTask('__TASK__')
    $act = $task.Definition.Actions.Item(1)
    Write-Output ('state=' + $task.State + ' lastRun=' + $task.LastRunTime + ' lastResult=' + $task.LastTaskResult + ' nextRun=' + $task.NextRunTime)
    Write-Output ('action=' + $act.Path + ' ' + $act.Arguments)
    Write-Output ('battery: disallowStart=' + $task.Definition.Settings.DisallowStartIfOnBatteries + ' stopOnBattery=' + $task.Definition.Settings.StopIfGoingOnBatteries)
    Result 'watchdog task' ($task.State -eq 3 -or $task.State -eq 4) ('state ' + $task.State)
} catch { Result 'watchdog task' $false "$_" }

Section 'AI window + bridge'
$s = Status
foreach ($k in $s.Keys) { Write-Output ($k + '=' + $s[$k]) }
Result 'AI window running' ($s.agent_pid -match '^\d+$') ('pid ' + $s.agent_pid)
Result 'window title = AI name' ([int]$s.windows_titled -ge 1) ('windows titled ' + $name + ': ' + $s.windows_titled)
if ($tg) { Result 'telegram bridge' ($s.bridge_running -eq 'True') '' }
Write-Output '--- watchdog.log'
Get-Content (Join-Path $agentDir 'watchdog.log') -Tail 5 -Encoding UTF8 -ErrorAction SilentlyContinue
if ($tg) { Write-Output '--- tgterm.log'; Get-Content (Join-Path $agentDir 'tgterm.log') -Tail 8 -Encoding UTF8 -ErrorAction SilentlyContinue }

function Test-Inject($mode) {
    $script:injectOk = $false
    $since = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $file = Join-Path $agentDir 'inject-test.txt'
    Remove-Item $file -Force -ErrorAction SilentlyContinue
    if ($tg) { $msg = '[TG] （安裝測試）請用 Telegram 回覆指令傳一句「安裝測試成功」給我，不用做別的事。' }
    else { $msg = '（安裝測試）請建立檔案 ' + $file + '，內容只寫 OK，不用做別的事。' }
    $out = (Tgterm 'inject' '--mode' $mode $msg) -join ' '
    Write-Output ('inject ' + $mode + ': ' + $out)
    if ($out -notmatch '^ok') { return }
    for ($i = 1; $i -le 36; $i++) {
        Start-Sleep -Seconds 5
        if ($tg) {
            $hit = Get-Content (Join-Path $agentDir 'tgterm.log') -Encoding UTF8 -ErrorAction SilentlyContinue |
                Where-Object { $_.Length -gt 20 -and $_.Substring(0, 19) -ge $since -and $_.Substring(20).StartsWith('sent ') }
        } else { $hit = Test-Path $file }
        if ($hit) { Write-Output ('AI acted after ' + (5 * $i) + 's'); $script:injectOk = $true; return }
    }
    Write-Output 'AI did not act within 180s'
}

if (-not $SkipInject) {
    Section 'inject test (types a test message into the AI window)'
    $pidFile = Join-Path $agentDir 'agent.pid'
    if (Test-Path $pidFile) {
        $age = ((Get-Date) - (Get-Item $pidFile).LastWriteTime).TotalSeconds
        if ($age -lt 45) { Start-Sleep -Seconds ([int](45 - $age)) }   # let a freshly opened AI finish starting
    }
    $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $mode = if ($cfg.mode) { [string]$cfg.mode } else { 'console' }
    Test-Inject $mode
    if (-not $script:injectOk) {
        $other = if ($mode -eq 'paste') { 'console' } else { 'paste' }
        Write-Output ('mode ' + $mode + ' did not work; trying ' + $other)
        Test-Inject $other
        if ($script:injectOk) {
            $cfg.mode = $other
            [IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
            Write-Output ('config mode -> ' + $other)
            $mode = $other
        }
    }
    Result 'inject -> AI acted' $script:injectOk ('mode ' + $mode)
}

if (-not $SkipWatchdog) {
    Section 'watchdog test (closes the AI window, waits for it to reopen)'
    $before = (Status).agent_pid
    if ($before -notmatch '^\d+$') { Result 'watchdog reopen' $false 'AI window was not running before the test' }
    else {
        $out = (Tgterm close-window) -join ' '
        Write-Output $out
        if ($out -notmatch 'closed [1-9]') { Write-Output 'no window with that title; ending the process tree instead'; cmd /c "taskkill /PID $before /T /F >nul 2>&1" }
        $after = $null
        for ($i = 1; $i -le 30; $i++) {
            Start-Sleep -Seconds 5
            $now = (Status).agent_pid
            if ($now -match '^\d+$' -and $now -ne $before) { $after = $now; break }
        }
        Result 'watchdog reopen' ($null -ne $after) $(if ($after) { 'new pid ' + $after + ' after ' + (5 * $i) + 's' } else { 'not reopened within 150s' })
        if ($after) {
            Start-Sleep -Seconds 3
            $s2 = Status
            Result 'reopened title = AI name' ([int]$s2.windows_titled -ge 1) ('windows titled ' + $name + ': ' + $s2.windows_titled)
        }
    }
}

Section 'SUMMARY'
foreach ($k in $results.Keys) { Write-Output ($k.PadRight(26) + $results[$k]) }
$fails = @($results.Values | Where-Object { $_ -like 'FAIL*' }).Count
Write-Output ('TOTAL ' + $results.Count + ' checks, ' + $fails + ' failed')
'@
$map = @{ '__AI__' = $ai; '__NAME__' = $agentName; '__TG__' = $(if ($tg) { '$true' } else { '$false' }); '__AGENTDIR__' = $AgentDir
    '__PY__' = $Py; '__MCPDIR__' = $McpDir; '__RULES__' = $RulesPath; '__ROOT__' = $Root; '__TASK__' = $TaskName
    '__CLAUDE__' = $(if ($NeedClaude) { '$true' } else { '$false' }); '__REMOTE__' = $(if ($remote) { '$true' } else { '$false' }) }
$VerifyPath = Join-Path $SetupDir 'verify.ps1'
Write-Text $VerifyPath (Fill $verify $map) -Bom

$handoff = @'
# Delivery: undo the setup-only settings, sign the installer's Claude account out, and (AI = Claude) reopen the AI window
# so the new owner signs in there with their own account. Runs by itself when the setup assistant ends.
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
$env:PYTHONUTF8 = '1'
$py = '__PY__'; $tgterm = '__TGTERM__'
$patch = Join-Path $env:TEMP 'agent-handoff-patch.json'
[IO.File]::WriteAllText($patch, '{"crossSessionInbound": null, "disableClaudeAiConnectors": null, "permissions": {"defaultMode": null}}', (New-Object Text.UTF8Encoding $false))
& $py $tgterm merge-json (Join-Path $HOME '.claude\settings.json') $patch | Out-Null
Remove-Item $patch -Force
cmd /c "claude auth logout >nul 2>&1"
$status = cmd /c "claude auth status 2>nul" | Out-String
if ($status -match '"loggedIn"\s*:\s*true') {
    Write-Host '⚠️ 登出沒有成功，請在這個視窗輸入 claude auth logout 再試一次。' -ForegroundColor Red
    exit 1
}
# signed out: the watchdog's safety net is no longer needed
Remove-Item (Join-Path '__AGENTDIR__' 'handoff-pending') -Force -ErrorAction SilentlyContinue
# Codex / Antigravity keep the owner's own sign-in: say hello now (a Claude AI does it after the owner signs in)
if ('__AI__' -ne 'claude') { & $py $tgterm inject '__KICK__' | Out-Null }
if ('__AI__' -eq 'claude') {
    # the AI window still holds the installer's sign-in in memory: restart it, then type /login into it
    # so the owner goes straight to the web page and signs in with their own account (Codex / agy keep their own sign-in)
    & $py $tgterm close-window | Out-Null
    Start-Sleep -Seconds 3
    Remove-Item (Join-Path '__AGENTDIR__' 'watchdog.lock') -Force -ErrorAction SilentlyContinue
    & $py $tgterm watchdog
    $up = $false
    for ($i = 0; $i -lt 30 -and -not $up; $i++) { Start-Sleep -Seconds 1; $up = ((& $py $tgterm status | Out-String) -match 'agent_pid=\d+') }
    Start-Sleep -Seconds 12   # let Claude Code finish starting before typing into it
    $typed = (& $py $tgterm inject '/login' | Out-String).Trim()
    if ($typed -like 'ok*') {
        Write-Host 'AI 視窗已重新開啟，並自動輸入了 /login：請在那個視窗選用 Claude 帳號登入，瀏覽器會打開，用「你自己的」帳號登入並授權。' -ForegroundColor Yellow
    } else {
        Write-Host ('AI 視窗已重新開啟，但沒能自動輸入（' + $typed + '）：請在那個視窗自己輸入 /login 再按 Enter，用「你自己的」Claude 帳號登入。') -ForegroundColor Yellow
    }
}
Write-Host ''
Write-Host '✅ 已登出安裝者的 Claude 帳號（已確認），可以關閉這個視窗了。' -ForegroundColor Green
Write-Host '   AI 助理安裝 by Kai' -ForegroundColor DarkGray
'@
$HandoffPath = Join-Path $SetupDir 'handoff.ps1'
$Kick = '[系統] 安裝完成了。這是你第一次見到主人：請照規則檔「第一次見面」那一段做。'
if ($remote) { Write-Text $HandoffPath (Fill $handoff @{ '__PY__' = $Py; '__TGTERM__' = $Tgterm; '__AI__' = $ai; '__AGENTDIR__' = $AgentDir; '__KICK__' = $Kick }) -Bom }

$finish = @'
# Ends the setup assistant (the Claude session started with --remote-control agent-setup).
# Its window then runs handoff.ps1 by itself, which signs the installer's Claude account out.
Get-CimInstance Win32_Process -Filter "Name='claude.exe'" |
    Where-Object { $_.CommandLine -like '*--remote-control agent-setup*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
'@
$FinishPath = Join-Path $SetupDir 'finish-setup.ps1'
if ($remote) { Write-Text $FinishPath $finish -Bom }

# ---------------------------------------------------------------- 8. sign-ins, open the AI window, hand over to the setup session
Say ''
# the AI's own sign-in first; the installer's Claude sign-in last, so the sign-out safety net starts right after it
if ($ai -eq 'codex') {
    Step 'Codex 登入（用你自己的 ChatGPT 帳號）' {
        if (Test-Path (Join-Path $HOME '.codex\auth.json')) { Say '    已登入' 'Green' } else { codex login; Check 'codex login' }
    }
}
if ($remote) {
    Step 'Claude 登入（安裝用帳號，用安裝人員自己的手機授權）' {
        $st = cmd /c "claude auth status 2>nul" | Out-String
        if ($st -match '"loggedIn"\s*:\s*true') { Say '    已登入' 'Green'; return }
        if ($tg) { Say "    登入連結會用 Telegram（@$botName）傳一張 QR code 給你：請交給安裝人員，用他自己的手機掃描並按「授權」。" 'Yellow' }
        else { Say '    會跳出一個視窗顯示 QR code：請安裝人員用他自己的手機掃描並按「授權」。' 'Yellow' }
        Say '    授權後網頁會顯示一串代碼：把代碼貼到下面「Paste code here if prompted」那一行，按 Enter。' 'Yellow'
        $env:BROWSER = $LoginHook
        try { claude auth login --claudeai; Check 'claude auth login' }
        finally { Remove-Item Env:BROWSER -ErrorAction SilentlyContinue }
    }
} elseif ($ai -eq 'claude') {
    Step 'Claude 登入（用你自己的帳號）' {
        $st = cmd /c "claude auth status 2>nul" | Out-String
        if ($st -match '"loggedIn"\s*:\s*true') { Say '    已登入' 'Green'; return }
        Say '    瀏覽器會打開：用你自己的 Claude 帳號登入並按「授權」。' 'Yellow'
        claude auth login --claudeai; Check 'claude auth login'
    }
}
if ($remote) {
    # sign-out safety net: until handoff.ps1 finishes, the watchdog signs the installer out as soon as this window is gone
    Write-Text (Join-Path $AgentDir 'handoff-pending') ([ordered]@{ pid = $PID; handoff = $HandoffPath } | ConvertTo-Json)
}

# only now (signed in, everything written) may the watchdog start and the AI window open
if (-not $DryRun) { Set-Content -Path (Join-Path $AgentDir 'fresh-start') -Value 'install' }
Step '看門狗排程（每分鐘：AI 視窗被關掉就重開、Telegram 橋接停了就重啟）' {
    schtasks.exe /Create /SC MINUTE /MO 1 /TN $TaskName /TR "$Pyw $Tgterm watchdog" /F | Out-Host
    Check 'schtasks create'
    try {
        # laptops: keep running on battery
        $svc = New-Object -ComObject Schedule.Service; $svc.Connect()
        $folder = $svc.GetFolder('\')
        $def = $folder.GetTask($TaskName).Definition
        $def.Settings.DisallowStartIfOnBatteries = $false
        $def.Settings.StopIfGoingOnBatteries = $false
        $def.Settings.StartWhenAvailable = $true
        $def.Settings.ExecutionTimeLimit = 'PT5M'
        $null = $folder.RegisterTaskDefinition($TaskName, $def, 6, $null, $null, 3)
    } catch { Say "    (battery settings not changed: $_)" 'Yellow' }
}
Step "開啟 AI 視窗「$agentName」" {
    Remove-Item $Paused, (Join-Path $AgentDir 'watchdog.lock') -Force -ErrorAction SilentlyContinue
    & $Py $Tgterm watchdog; Check 'watchdog'
    $ok = $false
    for ($i = 0; $i -lt 20 -and -not $ok; $i++) { Start-Sleep -Seconds 1; $ok = ((& $Py $Tgterm status | Out-String) -match 'agent_pid=\d+') }
    if ($ok) { Say '    已開啟' 'Green' } else { Say "    AI 視窗沒有開起來，請看 $AgentDir\watchdog.log" 'Yellow' }
}

$prompt = @"
This computer was just prepared by its owner's own setup script. You are this machine's setup assistant (session agent-setup).
1. Run this command and keep its full output. It takes up to 8 minutes, so use a 10-minute timeout:
   powershell -NoProfile -ExecutionPolicy Bypass -File '$VerifyPath'
   As part of the install the owner asked for, it types one test message into the owner's AI window ($agentName) and closes that window once to check that it reopens by itself.
2. Report to the setup coordinator, a session on the same account named exactly $Coordinator. As soon as verify finishes, use ListAgents to find the session named $Coordinator and send it the complete, unedited verify output plus one line naming anything that failed. If no session with that exact name is listed, do not message anyone else: wait, and when the coordinator messages you, reply to that message with the same content.
3. For the rest of this setup the owner pre-authorizes: re-running verify.ps1 (optionally with -SkipInject or -SkipWatchdog), reading files and logs under C:\AI and $SetupDir, and fixing the generated scripts under C:\AI\.agent when the coordinator points to a concrete bug in them. Report every change to the coordinator.
4. Finishing (the owner set this up so they can leave this computer safely): after reporting, wait for the coordinator's answer. When the coordinator confirms the setup is complete and tells you to finish, or when the owner types 結束安裝 in this window, run this command; it ends this session, and this window then signs the installer's Claude account out by itself:
   powershell -NoProfile -ExecutionPolicy Bypass -File '$FinishPath'
   If the coordinator asks for more checks instead, do them (within item 3) and report again.
Do not change files under ~/.claude or the rules file, do not delete anything outside C:\AI\.agent, do not spend money, and do not message anyone except the coordinator.
"@
if ($remote) { Write-Text (Join-Path $SetupDir 'setup-prompt.txt') $prompt }

Say ''
Say '安裝完成，接下來會自動進行：' 'Green'
Say "  1. 「$agentName」的視窗已經開著（關掉也會在 1 分鐘內自己重開）。" 'Green'
if ($ai -eq 'gemini') { Say '  2. Antigravity 第一次啟動會要你登入，請用你自己的 Google 帳號。' 'Yellow' }
if ($tg) { Say "  *. 檢查時會傳一句「安裝測試成功」到你的 Telegram（@$botName），這是正常的。" 'Green' }
if ($remote) {
    Say '  這個視窗會變成「安裝助理」，自動檢查並主動回報給遠端協助者；協助者確認沒問題後，它會自己結束並自動登出安裝者的 Claude 帳號。' 'Green'
    Say '  看到「✅ 已登出安裝者的 Claude 帳號（已確認），可以關閉這個視窗了」就代表全部完成。' 'Yellow'
} else {
    Say '  接著在這個視窗自動檢查（約 3～5 分鐘；會在 AI 視窗打一句測試訊息，並關掉重開一次 AI 視窗），結果只顯示在這裡。' 'Green'
}
if ($DryRun) {
    Say ('[dry-run] would ' + $(if ($remote) { 'start the setup assistant (claude --remote-control agent-setup) in ' + $SetupDir } else { 'run verify.ps1 here' })) 'DarkGray'
    Say ('[dry-run] done. remote=' + $remote + ' ai=' + $ai + ' rules=' + $rulesName + ' telegram=' + $tg + ' claude=' + $NeedClaude) 'Green'
} elseif ($remote) {
    Stop-Transcript | Out-Null
    Set-Location $SetupDir
    # a fixed title so this window is not mistaken for the AI window
    $Host.UI.RawUI.WindowTitle = '安裝助理（裝完可關）'
    $env:CLAUDE_CODE_DISABLE_TERMINAL_TITLE = '1'
    claude --dangerously-skip-permissions --remote-control agent-setup $prompt
    # the setup assistant has ended (it finishes itself after a clean check, or the owner exited it): always sign the installer out
    Say ''
    Say '安裝助理已結束，正在登出安裝者的 Claude 帳號…' 'Green'
    $Host.UI.RawUI.WindowTitle = '安裝助理：登出中'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $HandoffPath
    $Host.UI.RawUI.WindowTitle = $(if ($LASTEXITCODE -eq 0) { '安裝完成（可關閉）' } else { '登出失敗：請看視窗訊息' })
} else {
    # no remote help: check right here, nothing leaves this computer
    $Host.UI.RawUI.WindowTitle = '安裝檢查中'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $VerifyPath
    Start-Sleep -Seconds 15                            # verify just reopened the AI window: let it finish starting
    & $Py $Tgterm inject $Kick | Out-Null              # first meeting: the AI introduces itself now
    Stop-Transcript | Out-Null
    $Host.UI.RawUI.WindowTitle = '安裝完成（可關閉）'
    Say ''
    Say '檢查完成：上面 SUMMARY 全部 PASS 就是裝好了，可以關閉這個視窗。' 'Green'
    Say '   AI 助理安裝 by Kai' 'DarkGray'
    Say '若有 FAIL：重新貼一次安裝指令即可（會問要不要沿用上次的答案，按 Enter 就好）。' 'Yellow'
}
} finally {
    # never leave the watchdog paused (if setup stopped early, the previous install keeps working)
    if (-not $DryRun -and (Test-Path $Paused)) { Remove-Item $Paused -Force -ErrorAction SilentlyContinue }
}
