# agent-setup uninstall (Windows, no admin) - by Kai
# One-liner:  irm https://raw.githubusercontent.com/dead1786/agent-setup/main/uninstall.ps1 | iex
# Removes the AI assistant and puts back the settings the installer changed. Programs are removed only if
# the installer added them (recorded in C:\AI\.agent\installed.json); anything that was there before is left alone.
# Shows everything first and asks before doing anything. Dry run (changes nothing): AGENT_SETUP_DRYRUN=1

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$env:PYTHONUTF8 = '1'
$DryRun = ($env:AGENT_SETUP_DRYRUN -eq '1')
function Say($msg, $color = 'Cyan') { Write-Host $msg -ForegroundColor $color }
function Read-JsonObject($path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}
function Remove-Path($p) {
    if (-not (Test-Path -LiteralPath $p)) { return }
    Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $p) { Start-Sleep -Seconds 2; Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $p) { $script:Failed += $p; Say ('  刪不掉：' + $p) 'Red' } else { Say ('  已刪除：' + $p) 'Gray' }
}

if (-not $DryRun -and [Console]::IsInputRedirected) {
    throw 'uninstall must be run in an interactive PowerShell window (or set AGENT_SETUP_DRYRUN=1 for a dry run).'
}

$Root      = 'C:\AI'
$AgentDir  = Join-Path $Root '.agent'
$Py        = Join-Path $AgentDir 'py\Scripts\python.exe'
$Tgterm    = Join-Path $AgentDir 'tgterm.py'
$SetupDir  = Join-Path $HOME 'agent-setup'
$ClaudeDir = Join-Path $HOME '.claude'
$LocalBin  = Join-Path $HOME '.local\bin'
$UserBin   = Join-Path $HOME 'bin'
$GitDir    = Join-Path $env:LOCALAPPDATA 'Programs\PortableGit'
$NodeDir   = Join-Path $env:LOCALAPPDATA 'Programs\nodejs'
$Man       = Read-JsonObject (Join-Path $AgentDir 'installed.json')
$script:Failed = @()

if (-not (Test-Path $Root) -and -not (Test-Path $SetupDir)) { Say '這台電腦沒有找到 AI 助理的安裝。' 'Green'; return }

# programs this installer added (never the ones that were already here)
$names = [ordered]@{ claude = 'Claude Code'; codex = 'Codex'; node = 'Node.js'; agy = 'Antigravity（agy）'; git = 'Git（PortableGit）'; jq = 'jq'; uv = 'uv（Python 工具）' }
$ours = @($names.Keys | Where-Object { $Man -and $Man.ours -and $Man.ours.$_ })

Say ''
Say '=================================================' 'Yellow'
Say '  移除 AI 助理' 'Yellow'
Say '                                       by Kai' 'Yellow'
Say '=================================================' 'Yellow'
Say '會做的事：' 'Yellow'
Say '  ・關掉 AI 視窗、Telegram 連線，移除「自動重開」排程' 'Gray'
Say '  ・刪除 C:\AI（AI 的規則、記憶、工具）和安裝紀錄' 'Gray'
Say '  ・把安裝時改過的設定改回原本的樣子（Claude／Codex／Antigravity 的設定）' 'Gray'
if ($ours.Count) {
    Say '  ・（可選）移除安裝時才裝的程式：' 'Gray'
    $ours | ForEach-Object { Say ('      ' + $names[$_]) 'Gray' }
}
if (-not $Man) { Say '  （找不到安裝紀錄：只會移除 AI 助理本身，不會動任何程式）' 'DarkGray' }

if ($DryRun) { Say '[dry-run] nothing changed.' 'DarkGray'; return }
if ((Read-Host '確定要移除嗎？輸入 YES 開始（其他任何字＝取消）') -cne 'YES') { Say '已取消，什麼都沒動。' 'Green'; return }
$keepMemory = (Test-Path (Join-Path $Root 'memory')) -and ((Read-Host '要先把 AI 的記憶備份到桌面嗎？ [Y/n]') -notmatch '^\s*[nN]')
$dropPrograms = $ours.Count -and ((Read-Host '也移除上面列的「安裝時才裝的」程式嗎？（你原本就有的不會動） [Y/n]') -notmatch '^\s*[nN]')
$drop = @{}; if ($dropPrograms) { $ours | ForEach-Object { $drop[$_] = $true } }

# 1. stop everything that would reopen or lock files (the task only if it is the one this installer made)
$taskXml = cmd /c "schtasks /Query /TN agent-watchdog /XML 2>nul" | Out-String
if ($taskXml -match 'tgterm\.py') { cmd /c "schtasks /Delete /TN agent-watchdog /F >nul 2>&1" }
$rootPre = [bool]($Man -and $Man.root_preexisted)       # C:\AI was the user's own folder: touch only what we put in it
$ownDirs = if ($rootPre) { @((Join-Path $AgentDir '*'), (Join-Path $Root 'tools\windows-mcp\*')) } else { @('C:\AI\*') }
if ((Test-Path $Py) -and (Test-Path $Tgterm)) { & $Py $Tgterm close-window 2>$null | Out-Null; Start-Sleep -Seconds 2 }
$all = @(Get-CimInstance Win32_Process)
$keep = @{}; $p = [long]$PID                            # never stop this window or what it runs inside
while ($p -and -not $keep.ContainsKey($p)) {
    $keep[$p] = 1
    $parent = $all | Where-Object { [long]$_.ProcessId -eq $p } | Select-Object -First 1
    $p = if ($parent) { [long]$parent.ParentProcessId } else { 0 }
}
foreach ($proc in $all) {
    if ($keep.ContainsKey([long]$proc.ProcessId)) { continue }
    $cmdline, $exe = [string]$proc.CommandLine, [string]$proc.ExecutablePath
    $isOurs = $cmdline -like '*run-agent.ps1*' -or $cmdline -like '*tgterm.py*' -or $cmdline -like '*winsend.py*' -or
              $cmdline -like '*winject*dispatcher*' -or
              $cmdline -like '*--remote-control agent-setup*' -or ($exe -and @($ownDirs | Where-Object { $exe -like $_ }).Count)
    if ($isOurs) { cmd /c "taskkill /PID $($proc.ProcessId) /T /F >nul 2>&1" }
}
Start-Sleep -Seconds 2
Say '已關閉 AI 視窗與背景程式、移除自動重開排程。' 'Green'

# 2. keep the memories if asked
if ($keepMemory) {
    $dest = Join-Path ([Environment]::GetFolderPath('Desktop')) ('AI記憶備份-' + (Get-Date).ToString('yyyyMMdd-HHmm'))
    Copy-Item -LiteralPath (Join-Path $Root 'memory') -Destination $dest -Recurse -Force
    Say ('記憶已備份到：' + $dest) 'Green'
}

# 3. put back the settings the installer changed (needs the installer's Python, so before C:\AI goes)
# the safety guard's hooks first: only our entries go, hooks the user added stay
$guard = Join-Path $AgentDir 'guard.py'
if ((Test-Path $Py) -and (Test-Path $guard)) { & $Py $guard remove | Out-Host }
if ((Test-Path $Py) -and (Test-Path $Tgterm)) {
    $restore = { param($file, [string[]]$keys)
        if (Test-Path -LiteralPath $file) {
            & $Py $Tgterm restore-keys $file ($file + '.bak-agent-setup') @keys | Out-Host
            Remove-Item -LiteralPath ($file + '.bak-agent-setup') -Force -ErrorAction SilentlyContinue
        }
    }
    if (-not $drop.claude) {
        & $restore (Join-Path $ClaudeDir 'settings.json') @('crossSessionInbound', 'skipDangerousModePermissionPrompt', 'disableClaudeAiConnectors', 'permissions|defaultMode', 'statusLine')
        & $restore (Join-Path $HOME '.claude.json') @('hasCompletedOnboarding', 'projects|C:/AI', ('projects|' + ($SetupDir -replace '\\', '/')))
        if ((Get-Command claude -ErrorAction SilentlyContinue) -and -not ($Man -and $Man.mcp_preexisting)) {
            cmd /c "claude mcp remove windows-mcp --scope user >nul 2>&1"
        }
        # the skills this installer copied, plus any a later update added (listed in the install record)
        $skillNames = @('winsend', 'memory-rag', 'winject', 'alarm-system', 'watchdog-alert')
        if ($Man -and $Man.file_hashes) {
            $skillNames += @($Man.file_hashes.PSObject.Properties.Name | Where-Object { $_ -match '^skills/[A-Za-z0-9_-]+/SKILL\.md$' } | ForEach-Object { ($_ -split '/')[1] })
        }
        foreach ($s in ($skillNames | Select-Object -Unique)) { Remove-Path (Join-Path $ClaudeDir "skills\$s") }
        Remove-Path (Join-Path $ClaudeDir 'agent-setup-statusline.sh')
    }
    # ~/.gemini\config stays even when Antigravity itself is removed, so always put it back
    & $restore (Join-Path $HOME '.gemini\config\mcp_config.json') @('mcpServers|windows-mcp')
    & $restore (Join-Path $HOME '.gemini\antigravity-cli\settings.json') @('trustedWorkspaces')
}
$codexCfg = Join-Path $HOME '.codex\config.toml'
if ((Test-Path $codexCfg) -and -not $drop.codex) {
    $t = [IO.File]::ReadAllText($codexCfg)
    $t2 = $t -replace '(?ms)\r?\n\[mcp_servers\.windows-mcp\]\r?\n.*?(?=\r?\n\[|\z)', '' -replace "(?m)\r?\n\[projects\.'C:\\AI'\]\r?\ntrust_level = 'trusted'\r?\n?", "`n"
    if ($t2 -ne $t) { [IO.File]::WriteAllText($codexCfg, $t2.TrimEnd() + "`n", (New-Object Text.UTF8Encoding $false)) }
}
Say '安裝時改過的設定已還原。' 'Green'

# 4. programs the installer added (only if asked)
if ($drop.codex) {
    $npm = if (Test-Path (Join-Path $NodeDir 'npm.cmd')) { Join-Path $NodeDir 'npm.cmd' } else { 'npm' }
    cmd /c "`"$npm`" uninstall -g @openai/codex >nul 2>&1"
    Remove-Path (Join-Path $HOME '.codex')
}
if ($drop.agy) {
    if (Get-Command winget -ErrorAction SilentlyContinue) { cmd /c "winget uninstall --id Google.AntigravityCLI -e --disable-interactivity >nul 2>&1" }
    Remove-Path (Join-Path $HOME '.gemini\antigravity-cli')
}
if ($drop.claude) {
    foreach ($x in (Join-Path $LocalBin 'claude.exe'), (Join-Path $HOME '.local\share\claude'), $ClaudeDir,
                   (Join-Path $HOME '.claude.json'), (Join-Path $HOME '.claude.json.bak-agent-setup')) { Remove-Path $x }
}
if ($drop.node) { Remove-Path $NodeDir }
if ($drop.git) { Remove-Path $GitDir }
if ($drop.jq) { Remove-Path (Join-Path $UserBin 'jq.exe') }

# 5. the assistant itself
if ($rootPre) {
    $rulesFile = switch ($Man.ai) { 'claude' { 'CLAUDE.md' } 'codex' { 'AGENTS.md' } 'gemini' { 'GEMINI.md' } default { $null } }
    foreach ($x in $AgentDir, (Join-Path $Root 'tools\windows-mcp'), (Join-Path $Root 'memory')) { Remove-Path $x }
    # the rules file and what the updater may have put beside it (backups live in C:\AI\.agent, removed above)
    if ($rulesFile) { foreach ($x in '', '.new-sections.md', '.update-suggestion.md') { Remove-Path (Join-Path $Root ($rulesFile + $x)) } }
} else {
    Remove-Path $Root
}
Remove-Path $SetupDir
Remove-Path (Join-Path ([Environment]::GetFolderPath('Startup')) 'agent-tg-bridge.vbs')
Remove-Path (Join-Path $HOME '.cache\huggingface\hub\models--Systran--faster-whisper-small')   # the voice model (~500MB)
if ($drop.uv) {                                         # last: nothing above needs uv any more
    foreach ($x in 'uv.exe', 'uvx.exe', 'uvw.exe') { Remove-Path (Join-Path $LocalBin $x) }
    Remove-Path (Join-Path $env:APPDATA 'uv')
    Remove-Path (Join-Path $env:LOCALAPPDATA 'uv')
}

# 6. PATH entries the installer added, once their folders are gone or empty
if ($Man -and $Man.path_added) {
    $cur = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @($cur -split ';' | Where-Object { $_ })
    $gone = @($parts | Where-Object { @($Man.path_added) -contains $_ -and (-not (Test-Path -LiteralPath $_) -or -not (Get-ChildItem -LiteralPath $_ -Force | Select-Object -First 1)) })
    if ($gone.Count) { [Environment]::SetEnvironmentVariable('Path', (($parts | Where-Object { $gone -notcontains $_ }) -join ';'), 'User') }
}

Say ''
if ($script:Failed.Count) {
    Say ('還有 ' + $script:Failed.Count + ' 項刪不掉（多半是還有視窗開著）。關掉其他終端視窗後，再貼一次這行指令。') 'Yellow'
} else {
    Say '✅ AI 助理已移除完成，可以關閉這個視窗了。' 'Green'
}
