# agent-setup update (Windows, no admin) - by Kai
# One-liner:  irm https://raw.githubusercontent.com/dead1786/agent-setup/main/update.ps1 | iex
# Updates the AI assistant's helper programs in place (needed once for installs made before Telegram /update existed;
# after that /update on Telegram does the same). Files the owner or the AI changed are kept and the new version goes
# beside them as <file>.new; rules sections are refreshed only where nobody changed them. Undo: Telegram /update 還原
# Test overrides: AGENT_SETUP_AGENT_DIR (install folder), AGENT_UPDATE_SOURCE (local folder instead of GitHub),
#   AGENT_UPDATE_PY (python.exe), AGENT_UPDATE_TEST=1 (no restarts, no Telegram, nothing typed into the AI window)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$env:PYTHONUTF8 = '1'
function Say($msg, $color = 'Cyan') { Write-Host $msg -ForegroundColor $color }
function Read-JsonObject($path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}
function Stop-ByCommandLine($pattern) {
    $all = @(Get-CimInstance Win32_Process)
    $keep = @{}; $p = [long]$PID                        # never stop this window or anything it runs inside
    while ($p -and -not $keep.ContainsKey($p)) {
        $keep[$p] = 1
        $parent = $all | Where-Object { [long]$_.ProcessId -eq $p } | Select-Object -First 1
        $p = if ($parent) { [long]$parent.ParentProcessId } else { 0 }
    }
    $all | Where-Object { -not $keep.ContainsKey([long]$_.ProcessId) -and $_.CommandLine -like $pattern } |
        ForEach-Object { cmd /c "taskkill /PID $($_.ProcessId) /T /F >nul 2>&1" }
}

$AgentDir = if ($env:AGENT_SETUP_AGENT_DIR) { $env:AGENT_SETUP_AGENT_DIR } else { 'C:\AI\.agent' }
$Py       = if ($env:AGENT_UPDATE_PY) { $env:AGENT_UPDATE_PY } else { Join-Path $AgentDir 'py\Scripts\python.exe' }
$Source   = $env:AGENT_UPDATE_SOURCE
$Test     = ($env:AGENT_UPDATE_TEST -eq '1')
$Raw      = 'https://raw.githubusercontent.com/dead1786/agent-setup'
$Utf8     = New-Object Text.UTF8Encoding $false

Say ''
Say '=================================================' 'Green'
Say '  更新 AI 助理（你和 AI 改過的東西會保留）' 'Green'
Say '                                       by Kai' 'Green'
Say '=================================================' 'Green'

$Man = Read-JsonObject (Join-Path $AgentDir 'installed.json')
if (-not $Man -or -not (Test-Path (Join-Path $AgentDir 'config.json')) -or -not (Test-Path $Py)) {
    Say ('這台電腦沒有找到完整的 AI 助理安裝（' + $AgentDir + '）。請改用安裝指令重新安裝。') 'Yellow'
    return
}
# installs from before the updater: their Telegram bridge cannot be asked to restart, so it is stopped here afterwards
$hadUpdater = [bool]$Man.version
$Cfg = Read-JsonObject (Join-Path $AgentDir 'config.json')
$hasTg = [bool]($Cfg -and $Cfg.token)

$work = Join-Path $env:TEMP ('agent-update-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$code = $null
try {
    Say '==> 下載更新清單'
    $manPath = Join-Path $work 'manifest.json'
    if ($Source) { Copy-Item (Join-Path $Source 'manifest.json') $manPath }
    else {
        Invoke-WebRequest -Uri ("$Raw/main/manifest.json?nocache=" + [DateTime]::UtcNow.Ticks) -Headers @{ 'Cache-Control' = 'no-cache' } -OutFile $manPath -UseBasicParsing
    }
    $m = [IO.File]::ReadAllText($manPath, $Utf8) | ConvertFrom-Json
    $version = [string]$m.version
    $want = [string]$m.files.'tgterm.py'
    if ($version -notmatch '^[\w.-]{1,64}$' -or $want -notmatch '^[0-9a-f]{64}$') { throw 'manifest.json is incomplete' }
    Say ('==> 下載新版更新程式（' + $version.Substring(0, [Math]::Min(7, $version.Length)) + '，核對檢查碼）')
    $newTgterm = Join-Path $work 'tgterm.py'
    if ($Source) { Copy-Item (Join-Path $Source 'tgterm.py') $newTgterm }
    else { Invoke-WebRequest -Uri "$Raw/$version/tgterm.py" -OutFile $newTgterm -UseBasicParsing }
    $hash = (Get-FileHash $newTgterm -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $want) { throw "tgterm.py checksum mismatch ($hash)" }

    Say '==> 更新中（約 1～2 分鐘）'
    $pyArgs = @($newTgterm, 'update', '--manifest', $manPath)
    if ($Source) { $pyArgs += @('--source', $Source) }
    if ($Test -or -not $hadUpdater) { $pyArgs += '--no-restart' }
    if ($Test) { $pyArgs += '--no-notify' }
    $env:AGENT_SETUP_AGENT_DIR = $AgentDir
    $ErrorActionPreference = 'Continue'
    & $Py @pyArgs
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
} catch {
    Say ('沒辦法下載更新：' + $_.Exception.Message) 'Red'
    Say '什麼都沒改。請檢查網路，過幾分鐘再貼一次這行指令。' 'Yellow'
} finally {
    Remove-Item Env:AGENT_SETUP_AGENT_DIR -ErrorAction SilentlyContinue
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}
if ($null -eq $code) { return }

$after = Read-JsonObject (Join-Path $AgentDir 'installed.json')
$changed = $after -and $after.version -and ([string]$after.version -ne [string]$Man.version)
if ($code -eq 0 -and $changed -and -not $Test -and -not $hadUpdater) {
    # the old bridge and send queue still run the old code: stop them, the watchdog starts the new ones within a minute
    Stop-ByCommandLine '*tgterm.py*bridge*'
    $wj = Join-Path $AgentDir 'winject\cli.py'
    if (Test-Path $wj) { & $Py $wj stop-dispatcher | Out-Null }
    Say '背景程式（Telegram 連線、送字佇列）已停止，看門狗 1 分鐘內會用新版重新啟動；AI 視窗沒有重開。' 'Green'
}
Say ''
if ($code -eq 0) {
    Say $(if ($hasTg) { '✅ 完成，可以關閉這個視窗了。之後要更新，在 Telegram 打 /update 就好。' } else { '✅ 完成，可以關閉這個視窗了。之後要更新，再貼一次這行指令就好。' }) 'Green'
} else {
    Say '更新沒有完成：原因在上面。檔案已保持原樣（或已自動退回），可以稍後再貼一次這行指令。' 'Yellow'
}
