# agent-setup cleanup (Windows, no admin) - by Kai: removes everything bootstrap.ps1 installed, so the next run starts from zero.
# TEST-MACHINE RESET ONLY: it also removes Claude Code, uv, Node/npm, Git and their settings even if they existed before.
# One-liner:  irm https://raw.githubusercontent.com/dead1786/agent-setup/main/cleanup.ps1 | iex
# Shows the list first and only deletes after you type YES. Dry run (changes nothing): AGENT_SETUP_DRYRUN=1
# Run it in a NEW PowerShell window (not inside the AI window or the setup-assistant window).

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$DryRun = ($env:AGENT_SETUP_DRYRUN -eq '1')
function Say($msg, $color = 'Cyan') { Write-Host $msg -ForegroundColor $color }

if (-not $DryRun -and [Console]::IsInputRedirected) {
    throw 'cleanup must be run in an interactive PowerShell window (or set AGENT_SETUP_DRYRUN=1 for a dry run).'
}

$LocalBin = Join-Path $HOME '.local\bin'
$UserBin  = Join-Path $HOME 'bin'
$GitDir   = Join-Path $env:LOCALAPPDATA 'Programs\PortableGit'
$NodeDir  = Join-Path $env:LOCALAPPDATA 'Programs\nodejs'
$NpmDir   = Join-Path $env:APPDATA 'npm'
$UvPython = Join-Path $env:APPDATA 'uv'
$ClaudeVersions = Join-Path $HOME '.local\share\claude'
$AgyPkg   = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages') -Directory -Filter 'Google.AntigravityCLI_*' -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
$Py       = 'C:\AI\.agent\py\Scripts\python.exe'
$Tgterm   = 'C:\AI\.agent\tgterm.py'

# everything bootstrap.ps1 creates or installs (only the ones that exist are shown and removed)
$targets = @(
    'C:\AI'                                                     # the agent, its tools, memory, Telegram bridge
    (Join-Path $HOME 'agent-setup')                             # setup assistant files and logs
    (Join-Path $HOME '.claude')                                 # Claude Code settings, conversations, sign-in
    (Join-Path $HOME '.claude.json')
    (Join-Path $HOME '.claude.json.bak-agent-setup')
    $ClaudeVersions                                             # Claude Code program versions
    (Join-Path $HOME '.cache\claude')
    (Join-Path $LocalBin 'claude.exe')
    (Join-Path $LocalBin 'uv.exe')
    (Join-Path $LocalBin 'uvx.exe')
    (Join-Path $LocalBin 'uvw.exe')
    $UvPython                                                   # uv-managed Python
    (Join-Path $env:LOCALAPPDATA 'uv')                          # uv cache
    (Join-Path $HOME '.codex')                                  # Codex settings + sign-in
    (Join-Path $HOME '.gemini')                                 # Antigravity (agy) / Gemini settings + sign-in
    $AgyPkg                                                     # Antigravity CLI program (winget)
    (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\agy.exe')
    (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\agy')
    $NpmDir                                                     # Codex / Gemini CLI
    (Join-Path $env:LOCALAPPDATA 'npm-cache')
    $NodeDir
    $GitDir
    (Join-Path $UserBin 'jq.exe')
    (Join-Path ([Environment]::GetFolderPath('Startup')) 'agent-tg-bridge.vbs')   # v1 leftover
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

# programs that would lock those files: anything running from them, the AI window, the Telegram bridge, Claude Code
function Test-Under($path) {
    if (-not $path) { return $false }
    foreach ($r in @('C:\AI\', $GitDir, $NodeDir, $NpmDir, $UvPython, $LocalBin, $ClaudeVersions, $AgyPkg) | Where-Object { $_ }) {
        if ($path -like ($r.TrimEnd('\') + '\*')) { return $true }
    }
    return $false
}
$all = @(Get-CimInstance Win32_Process)
$keep = @{}; $p = [long]$PID                                   # never stop this window or what it runs inside
while ($p -and -not $keep.ContainsKey($p)) {
    $keep[$p] = 1
    $parent = $all | Where-Object { [long]$_.ProcessId -eq $p } | Select-Object -First 1
    $p = if ($parent) { [long]$parent.ParentProcessId } else { 0 }
}
$procs = @($all | Where-Object {
    -not $keep.ContainsKey([long]$_.ProcessId) -and (
        $_.CommandLine -like '*run-agent.ps1*' -or $_.CommandLine -like '*tgterm.py*' -or
        $_.CommandLine -like '*tg-bridge*bridge.py*' -or $_.CommandLine -like '*winject*dispatcher*' -or $_.Name -eq 'claude.exe' -or (Test-Under $_.ExecutablePath))
})
$task = (cmd /c "schtasks /Query /TN agent-watchdog >nul 2>&1"); $hasTask = ($LASTEXITCODE -eq 0)

Say ''
Say '=================================================' 'Yellow'
Say '  清除 AI 助理安裝（回到全新狀態）' 'Yellow'
Say '                                       by Kai' 'Yellow'
Say '=================================================' 'Yellow'
Say '會關掉這些程式：' 'Yellow'
if ($procs.Count) { $procs | ForEach-Object { Say ('  ' + $_.Name + '  pid ' + $_.ProcessId) 'Gray' } } else { Say '  （沒有）' 'Gray' }
Say '會移除排程：' 'Yellow'
Say $(if ($hasTask) { '  agent-watchdog' } else { '  （沒有）' }) 'Gray'
Say '會刪除這些資料夾／檔案（包含這台電腦上 Claude 的登入）：' 'Yellow'
if ($targets.Count) { $targets | ForEach-Object { Say ('  ' + $_) 'Gray' } } else { Say '  （沒有）' 'Gray' }
Say '並從使用者 PATH 拿掉安裝時加入、現在已不存在的路徑。' 'Yellow'

if ($DryRun) { Say '[dry-run] nothing changed.' 'DarkGray'; return }
if ((Read-Host '確定要全部清除嗎？輸入 YES 開始（其他任何字＝取消）') -cne 'YES') { Say '已取消，什麼都沒動。' 'Green'; return }

# 1. stop the watchdog first so it cannot reopen anything
if ($hasTask) { cmd /c "schtasks /Delete /TN agent-watchdog /F >nul 2>&1"; Say ('  排程 agent-watchdog：' + $(if ($LASTEXITCODE -eq 0) { '已移除' } else { '移除失敗' })) }

# 2. close the AI window the normal way, then stop whatever is left
if ((Test-Path $Py) -and (Test-Path $Tgterm)) { & $Py $Tgterm close-window 2>$null | Out-Null; Start-Sleep -Seconds 2 }
foreach ($pr in $procs) { cmd /c "taskkill /PID $($pr.ProcessId) /T /F >nul 2>&1" }
Start-Sleep -Seconds 2
# let winget forget the Antigravity CLI too (its files are removed below either way)
if ($AgyPkg -and (Get-Command winget -ErrorAction SilentlyContinue)) { cmd /c "winget uninstall --id Google.AntigravityCLI -e --disable-interactivity >nul 2>&1" }

# 3. delete (retry once: a just-stopped program can hold a file for a moment)
$failed = @()
foreach ($t in $targets) {
    Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $t) { Start-Sleep -Seconds 2; Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $t) { $failed += $t; Say ('  刪不掉：' + $t) 'Red' } else { Say ('  已刪除：' + $t) 'Gray' }
}

# 4. PATH: drop the folders bootstrap added, but only if they are gone or empty now
$added = @($LocalBin, (Join-Path $GitDir 'cmd'), (Join-Path $GitDir 'bin'), $UserBin, $NodeDir, $NpmDir)
$cur = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($cur) {
    $parts = @($cur -split ';' | Where-Object { $_ })
    $drop = @($parts | Where-Object { $added -contains $_ -and (-not (Test-Path -LiteralPath $_) -or -not (Get-ChildItem -LiteralPath $_ -Force | Select-Object -First 1)) })
    if ($drop.Count) {
        [Environment]::SetEnvironmentVariable('Path', (($parts | Where-Object { $drop -notcontains $_ }) -join ';'), 'User')
        $drop | ForEach-Object { Say ('  PATH 移除：' + $_) 'Gray' }
    }
}

Say ''
if ($failed.Count) {
    Say ('還有 ' + $failed.Count + ' 項刪不掉（多半是還有視窗開著）。把其他 PowerShell／終端視窗都關掉，再貼一次這行指令。') 'Yellow'
} else {
    Say '清除完成，這台電腦已回到全新狀態。請關掉這個視窗，重新開一個 PowerShell 再裝。' 'Green'
}
