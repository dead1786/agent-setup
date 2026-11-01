# agent-setup bootstrap v2 (Windows, no admin)
# One-liner:  irm https://raw.githubusercontent.com/dead1786/agent-setup/main/bootstrap.ps1 | iex
# Gives a new user their own AI agent (Claude Code / Codex / Gemini CLI) with a name and personality, Windows-MCP,
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
$PyDir     = Join-Path $AgentDir 'py'
$Py        = Join-Path $PyDir 'Scripts\python.exe'
$Pyw       = Join-Path $PyDir 'Scripts\pythonw.exe'
$Tgterm    = Join-Path $AgentDir 'tgterm.py'
$CfgPath   = Join-Path $AgentDir 'config.json'
$Paused    = Join-Path $AgentDir 'paused'
$ClaudeDir = Join-Path $HOME '.claude'
$TaskName  = 'agent-watchdog'
$TgtermUrl    = 'https://raw.githubusercontent.com/dead1786/agent-setup/536cf19bff73b1df899655eaa264e8a64fc3afd6/tgterm.py'
$TgtermSha256 = '66a85bea874e62af1c6f5a1ff8272b67f40e4f497ca761e73fa389dc1b85b0d9'
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
    Say ('找到上次的答案：AI=' + $prev.ai + '　名字=' + $prev.agent_name + '　Telegram=' + $prevTg) 'Green'
    $reuse = ((Read-Host '沿用上次的答案？ [Y/n]') -notmatch '^\s*[nN]')
}
if ($reuse) {
    $ai = $prev.ai; $agentName = $prev.agent_name; $personality = $prev.personality; $style = $prev.style
    $wants = $prev.wants; $userName = $prev.user_name; $lang = $prev.language; $userWork = $prev.user_work
    $botName = $prev.bot
    $tgToken = $prevCfg.token
    $ownerId = $prevCfg.owner_id
} else {
    $ai = $null
    $d1 = if ($DryRun -and $env:AGENT_SETUP_DRYRUN_AI) { $env:AGENT_SETUP_DRYRUN_AI } else { '1' }
    while (-not $ai) {
        switch (Ask '1) 要用哪個 AI？ 1=Claude Code  2=Codex  3=Gemini CLI  [預設 1]' $d1) {
            '1' { $ai = 'claude' } '2' { $ai = 'codex' } '3' { $ai = 'gemini' }
            default { Say '    請輸入 1、2 或 3' 'Yellow' }
        }
    }
    $agentName   = Ask '2) 幫你的 AI 取個名字 [預設 小助手]' '小助手'
    $personality = Ask '3) 它的個性？（例：溫柔、毒舌、幽默、冷靜）[預設 親切可靠]' '親切可靠'
    $style       = Ask '4) 說話風格？（例：簡短直接、像朋友聊天、條列重點）[預設 簡短清楚]' '簡短清楚'
    $wants       = Ask '5) 希望它主要幫你做什麼？（例：查資料、寫文件、整理行程）[預設 日常大小事]' '日常大小事'
    $userName    = Ask '6) 它要怎麼稱呼你？ [預設 主人]' '主人'
    $lang        = Ask '7) 回覆用什麼語言？ [預設 繁體中文]' '繁體中文'
    $userWork    = Ask '8) 你目前在做什麼工作或專案？（讓它更懂你；可直接按 Enter 跳過）' ''

    $tgToken = $null; $botName = $null; $ownerId = $null
    if ($DryRun -and $env:AGENT_SETUP_DRYRUN_TG -eq '1') { $tgToken = 'dry-run-token'; $botName = 'dry_run_bot'; $ownerId = [long]123456789 }
    while (-not $DryRun) {
        $t = (Read-Host '9) Telegram 機器人 token（找 @BotFather 開機器人拿到的；不接 Telegram 直接按 Enter）').Trim()
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
            $null = Read-Host "10) 用你自己的 Telegram 打開 @$botName，按「開始」或傳任何一句話；傳完回來這裡按 Enter"
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

$answers = [ordered]@{ ai = $ai; agent_name = $agentName; personality = $personality; style = $style; wants = $wants
    user_name = $userName; language = $lang; user_work = $userWork; bot = $botName; owner_id = $ownerId; created = (Get-Date).ToString('s') }
Write-Text (Join-Path $AgentDir 'answers.json') ($answers | ConvertTo-Json)
Say ('設定：AI=' + $ai + '　名字=' + $agentName + '　稱呼=' + $userName + '　語言=' + $lang + '　Telegram=' + $(if ($tg) { '@' + $botName } else { '不接' })) 'Green'

if (-not $DryRun) {
    New-Item -ItemType Directory -Force -Path $Root, $AgentDir, $MemDir, $ToolsDir, $SetupDir, $UserBin | Out-Null
    Start-Transcript -Path (Join-Path $SetupDir ('bootstrap-' + (Get-Date).ToString('yyyyMMdd-HHmmss') + '.log')) -Append | Out-Null
}

# ---------------------------------------------------------------- 2. tools (user-level, no admin)
# always call the uv in ~/.local/bin by full path (an older uv elsewhere on PATH may be too old)
$UvExe = Join-Path $LocalBin 'uv.exe'
Step 'uv（Python 工具）' {
    if (-not (Test-Path $UvExe)) { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression }
    Add-UserPath $LocalBin
    # an old uv cannot fetch the Python that Windows-MCP needs: update it, or reinstall if self-update is unavailable
    & $UvExe self update
    if ($LASTEXITCODE -ne 0) { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression }
    & $UvExe --version; Check 'uv'
}

Step 'Python（給 Telegram 橋接與看門狗用）' {
    if (-not (Test-Path $Py)) { & $UvExe venv --python 3.12 $PyDir; Check 'uv venv' }
    if (-not (Test-Path $Pyw)) { throw "pythonw.exe missing in $PyDir" }
    & $Py --version; Check 'python'
}

Step 'tgterm（Telegram 送字橋接＋看門狗）' {
    $tmp = $Tgterm + '.download'
    Download $TgtermUrl $tmp
    $hash = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $TgtermSha256) { Remove-Item $tmp -Force; throw "tgterm.py checksum mismatch: $hash" }
    Move-Item $tmp $Tgterm -Force
    & $Py $Tgterm selftest; Check 'tgterm selftest'
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

Step 'Claude Code（安裝與遠端協助用）' {
    if (-not (Have 'claude') -and -not (Test-Path (Join-Path $LocalBin 'claude.exe'))) { Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression }
    Add-UserPath $LocalBin
    if (-not (Have 'claude')) { throw 'Claude Code install failed: claude not found' }
    claude --version; Check 'claude'
}

if ($ai -ne 'claude') {
    Step 'Node.js（Codex／Gemini 需要）' {
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
    if ($ai -eq 'codex') {
        Step 'Codex CLI' { & (Join-Path $NodeDir 'npm.cmd') install -g '@openai/codex'; Check 'npm codex'; codex --version }
    } else {
        Step 'Gemini CLI' { & (Join-Path $NodeDir 'npm.cmd') install -g '@google/gemini-cli'; Check 'npm gemini'; gemini --version }
    }
}

# ---------------------------------------------------------------- 3. Windows-MCP
$McpArgs = @('--directory', $McpDir, 'run', 'windows-mcp', 'serve', '--tools', $McpTools)
Step 'Windows-MCP（CursorTouch，只開 13 個低風險工具）' {
    $gitExe = Join-Path $GitDir 'cmd\git.exe'
    if (-not (Test-Path (Join-Path $McpDir '.git'))) { & $gitExe clone --depth 1 https://github.com/CursorTouch/Windows-MCP.git $McpDir; Check 'git clone windows-mcp' }
    & $UvExe --directory $McpDir sync; Check 'uv sync windows-mcp'
}
Step 'Windows-MCP 接到 Claude Code' {
    $listed = (cmd /c "claude mcp list 2>&1" | Out-String)
    if ($listed -match 'windows-mcp') { cmd /c "claude mcp remove windows-mcp --scope user >nul 2>&1" }
    claude mcp add windows-mcp --scope user --transport stdio --env ANONYMIZED_TELEMETRY=false -- $UvExe @McpArgs; Check 'claude mcp add'
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
    Step 'Windows-MCP 接到 Gemini CLI（並預設 Google 登入、固定視窗標題）' {
        Merge-Json (Join-Path $HOME '.gemini\settings.json') ([ordered]@{
            mcpServers = @{ 'windows-mcp' = [ordered]@{ command = $UvExe; args = $McpArgs; env = @{ ANONYMIZED_TELEMETRY = 'false' } } }
            ui = @{ hideWindowTitle = $true }
            security = @{ auth = @{ selectedType = 'oauth-personal' } }
        })
    }
}

# ---------------------------------------------------------------- 4. Claude settings (setup phase) + status line
$BashExe = (Join-Path $GitDir 'bin\bash.exe') -replace '\\', '/'
$StatusScript = Join-Path $ClaudeDir 'statusline-command.sh'
Step 'Claude 設定（安裝期間：免確認、接收協調訊息、略過首次導覽、信任工作資料夾）' {
    Merge-Json (Join-Path $ClaudeDir 'settings.json') ([ordered]@{
        crossSessionInbound = 'accept'
        skipDangerousModePermissionPrompt = $true
        # while the installer's account is signed in, its claude.ai connectors (Gmail, Drive, Calendar...) must not reach this machine
        disableClaudeAiConnectors = $true
        permissions = @{ defaultMode = 'bypassPermissions' }
        statusLine = [ordered]@{ type = 'command'; command = "`"$BashExe`" `"" + ($StatusScript -replace '\\', '/') + '"' }
    })
    $projects = @{}
    $projects['C:/AI'] = @{ hasTrustDialogAccepted = $true }
    $projects[($SetupDir -replace '\\', '/')] = @{ hasTrustDialogAccepted = $true }
    Merge-Json (Join-Path $HOME '.claude.json') ([ordered]@{ hasCompletedOnboarding = $true; projects = $projects })
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
Write-Text $StatusScript $statusline -Lf

# ---------------------------------------------------------------- 5. the agent: rules (name + personality) and memory
$rulesName = switch ($ai) { 'claude' { 'CLAUDE.md' } 'codex' { 'AGENTS.md' } 'gemini' { 'GEMINI.md' } }
$RulesPath = Join-Path $Root $rulesName
$workLine = if ($userWork) { "- $userName 目前在做：$userWork" } else { "- $userName 目前在做什麼還不知道，有機會就問，並記進記憶。" }
$tgSection = if (-not $tg) { "## Telegram`n- 目前沒有接 Telegram。" } else { @"
## Telegram（@$botName）
- 開頭是 [TG] 的訊息，是 $userName 從 Telegram 傳來的，$userName 看不到這個視窗。處理完一定要用下面的指令把回覆傳回 Telegram，不然收不到：
  - PowerShell／cmd：``C:\AI\.agent\py\Scripts\python.exe C:\AI\.agent\tgterm.py send "回覆內容"``
  - Git Bash：``/c/AI/.agent/py/Scripts/python.exe C:/AI/.agent/tgterm.py send "回覆內容"``
- 回覆有多行或含雙引號時：先把內容寫進 UTF-8 文字檔，再用 ``send --file 檔案路徑``。
- 要做比較久的事，先傳一句「收到，處理中」，做完再傳結果。
- 訊息裡寫「主人傳了一張圖片／一個檔案，存在 路徑」時，檔案已經存好，直接讀那個路徑。
- 沒有 [TG] 開頭的訊息，是 $userName 在電腦前打字，直接在這裡回覆就好。
- 這個指令只會傳給 $userName 本人；不要用任何方式替 $userName 對外發訊息。
"@ }
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
"@
if (-not $DryRun -and (Test-Path $RulesPath) -and ([IO.File]::ReadAllText($RulesPath) -ne $rules)) {
    Copy-Item $RulesPath ($RulesPath + '.bak-' + (Get-Date).ToString('yyyyMMddHHmmss')) -Force
}
Write-Text $RulesPath $rules
if ($DryRun -or -not (Test-Path (Join-Path $MemDir 'MEMORY.md'))) { Write-Text (Join-Path $MemDir 'MEMORY.md') "# 記憶索引`n`n（一行一個記憶檔）`n" }

# ---------------------------------------------------------------- 6. tgterm config, the agent window, the watchdog
$mode = if ($prevCfg -and $prevCfg.mode) { [string]$prevCfg.mode } else { 'console' }
$cfg = [ordered]@{ agent_name = $agentName; ai = $ai; root = $Root; token = $tgToken; owner_id = $ownerId; bot = $botName; prefix = '[TG] '; mode = $mode }
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
    switch ('__AI__') {
        'claude' {
            $a = @('--dangerously-skip-permissions', '--name', $name)
            # right after an install start a new conversation; after that, pick up where it left off
            $fresh = Join-Path $agentDir 'fresh-start'
            $history = Join-Path $HOME '.claude\projects\C--AI'
            if (Test-Path $fresh) { Remove-Item $fresh -Force }
            elseif ($quick -eq 0 -and (Get-ChildItem $history -Filter '*.jsonl' -ErrorAction SilentlyContinue)) { $a += '--continue' }
            & claude @a
        }
        'codex'  { & codex --dangerously-bypass-approvals-and-sandbox }
        'gemini' { & gemini --yolo }
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
$tools = @('uv', 'git', 'jq', 'claude')
if ($ai -ne 'claude') { $tools += @('node', $ai) }
foreach ($c in $tools) {
    if (-not (Get-Command $c -ErrorAction SilentlyContinue)) { Result ('tool ' + $c) $false 'not found'; continue }
    $vout = & $c --version 2>&1
    $code = $LASTEXITCODE
    $v = [string](@($vout) | Select-Object -First 1)
    Write-Output ($c + ': ' + $v)
    Result ('tool ' + $c) ($code -eq 0) $v
}
Write-Output ('python: ' + ((& $py --version 2>&1) -join ''))

Section 'windows-mcp'
git -C '__MCPDIR__' log -1 --oneline
switch ($ai) {
    'claude' {
        $reg = (claude mcp list 2>&1 | Out-String); Write-Output $reg
        Result 'windows-mcp connected' ($reg -match 'windows-mcp.*Connected') 'claude mcp list'
        Result 'claude.ai connectors off' ($reg -notmatch 'claude\.ai ') 'no Gmail/Drive/Calendar of the installer account'
    }
    'codex'  { $reg = Get-Content (Join-Path $HOME '.codex\config.toml') -Raw -Encoding UTF8; Result 'windows-mcp registered' ($reg -match 'mcp_servers\.windows-mcp') 'codex config.toml' }
    'gemini' { $reg = Get-Content (Join-Path $HOME '.gemini\settings.json') -Raw -Encoding UTF8; Result 'windows-mcp registered' ($reg -match 'windows-mcp') 'gemini settings.json' }
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
    '__PY__' = $Py; '__MCPDIR__' = $McpDir; '__RULES__' = $RulesPath; '__ROOT__' = $Root; '__TASK__' = $TaskName }
$VerifyPath = Join-Path $SetupDir 'verify.ps1'
Write-Text $VerifyPath (Fill $verify $map) -Bom

$handoff = @'
# Delivery: undo the setup-only settings, sign out the installer's Claude account, let the new owner sign in, reopen the AI window
$ErrorActionPreference = 'Continue'
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
$env:PYTHONUTF8 = '1'
$py = '__PY__'; $tgterm = '__TGTERM__'
$patch = Join-Path $env:TEMP 'agent-handoff-patch.json'
[IO.File]::WriteAllText($patch, '{"crossSessionInbound": null, "disableClaudeAiConnectors": null, "permissions": {"defaultMode": null}}', (New-Object Text.UTF8Encoding $false))
& $py $tgterm merge-json (Join-Path $HOME '.claude\settings.json') $patch
Remove-Item $patch -Force
claude auth logout
Write-Host '已登出安裝者的 Claude 帳號。' -ForegroundColor Green
if ('__AI__' -eq 'claude') {
    Write-Host '接下來請用「你自己的」Claude 帳號登入。' -ForegroundColor Yellow
    claude auth login
    & $py $tgterm close-window | Out-Null
    Write-Host 'AI 視窗會在 1 分鐘內用你的帳號重新開啟。' -ForegroundColor Green
} else {
    Write-Host '你的 AI 本來就是用你自己的帳號登入，不受影響。' -ForegroundColor Green
}
'@
Write-Text (Join-Path $SetupDir 'handoff.ps1') (Fill $handoff @{ '__PY__' = $Py; '__TGTERM__' = $Tgterm; '__AI__' = $ai }) -Bom

# ---------------------------------------------------------------- 8. sign-ins, open the AI window, hand over to the setup session
Say ''
Step 'Claude 登入（安裝用帳號）' {
    $st = cmd /c "claude auth status 2>nul" | Out-String
    if ($st -match '"loggedIn"\s*:\s*true') { Say '    已登入' 'Green' } else { claude auth login --claudeai; Check 'claude auth login' }
}
if ($ai -eq 'codex') {
    Step 'Codex 登入（用你自己的 ChatGPT 帳號）' {
        if (Test-Path (Join-Path $HOME '.codex\auth.json')) { Say '    已登入' 'Green' } else { codex login; Check 'codex login' }
    }
}

# only now (signed in, everything written) may the AI window open and the watchdog start
Step "開啟 AI 視窗「$agentName」" {
    Set-Content -Path (Join-Path $AgentDir 'fresh-start') -Value 'install'
    Remove-Item $Paused, (Join-Path $AgentDir 'watchdog.lock') -Force -ErrorAction SilentlyContinue
    & $Py $Tgterm watchdog; Check 'watchdog'
    $ok = $false
    for ($i = 0; $i -lt 20 -and -not $ok; $i++) { Start-Sleep -Seconds 1; $ok = ((& $Py $Tgterm status | Out-String) -match 'agent_pid=\d+') }
    if ($ok) { Say '    已開啟' 'Green' } else { Say "    AI 視窗沒有開起來，請看 $AgentDir\watchdog.log" 'Yellow' }
}
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

$prompt = @"
This computer was just prepared by its owner's own setup script. You are this machine's setup assistant (session agent-setup).
1. Run this command and keep its full output. It takes up to 8 minutes, so use a 10-minute timeout:
   powershell -NoProfile -ExecutionPolicy Bypass -File '$VerifyPath'
   As part of the install the owner asked for, it types one test message into the owner's AI window ($agentName) and closes that window once to check that it reopens by itself.
2. A setup coordinator session on the same account will message you. Reply to that message with the complete, unedited verify output plus one line naming anything that failed. Do not look the coordinator up by name; just reply to its message.
3. For the rest of this setup the owner pre-authorizes: re-running verify.ps1 (optionally with -SkipInject or -SkipWatchdog), reading files and logs under C:\AI and $SetupDir, and fixing the generated scripts under C:\AI\.agent when the coordinator points to a concrete bug in them. Report every change to the coordinator.
Do not change files under ~/.claude or the rules file, do not delete anything outside C:\AI\.agent, do not spend money, and do not message anyone except the coordinator.
"@
Write-Text (Join-Path $SetupDir 'setup-prompt.txt') $prompt

Say ''
Say '安裝完成，接下來會自動進行：' 'Green'
Say "  1. 「$agentName」的視窗已經開著（關掉也會在 1 分鐘內自己重開）。" 'Green'
if ($ai -eq 'gemini') { Say '  2. Gemini 第一次啟動會開瀏覽器，請用你自己的 Google 帳號登入。' 'Yellow' }
if ($tg) { Say "  *. 檢查時會傳一句「安裝測試成功」到你的 Telegram（@$botName），這是正常的。" 'Green' }
Say '  這個視窗會變成「安裝助理」，自動檢查並回報給遠端協助者，請不要關掉它。' 'Green'
if (-not $DryRun) {
    Stop-Transcript | Out-Null
    Set-Location $SetupDir
    # a fixed title so this window is not mistaken for the AI window
    $Host.UI.RawUI.WindowTitle = '安裝助理（裝完可關）'
    $env:CLAUDE_CODE_DISABLE_TERMINAL_TITLE = '1'
    claude --dangerously-skip-permissions --remote-control agent-setup $prompt
} else {
    Say ('[dry-run] would run: claude --dangerously-skip-permissions --remote-control agent-setup <prompt> (in ' + $SetupDir + ')') 'DarkGray'
    Say ('[dry-run] done. ai=' + $ai + ' rules=' + $rulesName + ' telegram=' + $tg) 'Green'
}
} finally {
    # never leave the watchdog paused (if setup stopped early, the previous install keeps working)
    if (-not $DryRun -and (Test-Path $Paused)) { Remove-Item $Paused -Force -ErrorAction SilentlyContinue }
}
