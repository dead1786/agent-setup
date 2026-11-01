# agent-setup bootstrap (Windows, no admin)
# One-liner:  irm <raw-url>/bootstrap.ps1 | iex
# Dry run:    $env:AGENT_SETUP_DRYRUN='1'; irm <raw-url>/bootstrap.ps1 | iex
# Gives a new user their own AI agent (Claude Code / Codex / Gemini CLI) + Windows-MCP + Telegram bridge.

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$DryRun   = ($env:AGENT_SETUP_DRYRUN -eq '1')
$Root     = 'C:\AI'
$SetupDir = Join-Path $Root 'setup'
$ToolsDir = Join-Path $Root 'tools'
$BridgeDir = Join-Path $Root 'tg-bridge'
$MemDir   = Join-Path $Root 'memory'
$UserBin  = Join-Path $HOME 'bin'
$Programs = Join-Path $env:LOCALAPPDATA 'Programs'
$GitDir   = Join-Path $Programs 'PortableGit'
$NodeDir  = Join-Path $Programs 'nodejs'
$LocalBin = Join-Path $HOME '.local\bin'
$McpTools = 'Screenshot,Snapshot,DisplayInventory,ControlStatus,WaitFor,Wait,Clipboard,Click,Type,Scroll,Move,Shortcut,App'
$SetupPeer = 'frosty-10'
$Utf8 = New-Object Text.UTF8Encoding $false

function Say($msg, $color = 'Cyan') { Write-Host $msg -ForegroundColor $color }
function Step($name, [scriptblock]$action) {
    Say "==> $name"
    if ($DryRun) { Say "    [dry-run] skipped" 'DarkGray'; return }
    & $action
}
function Write-Text($path, $text, [switch]$Lf, [switch]$Bom) {
    if ($Lf) { $text = $text -replace "`r`n", "`n" }
    if ($DryRun) { Say "    [dry-run] would write $path ($($text.Length) chars)" 'DarkGray'; return }
    $dir = Split-Path $path -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $enc = if ($Bom) { New-Object Text.UTF8Encoding $true } else { $Utf8 }
    [IO.File]::WriteAllText($path, $text, $enc)
}
function Read-JsonObject($path) {
    # returns a PSObject; if the file is missing/empty/invalid, backs it up and starts fresh
    if (-not (Test-Path $path)) { return (New-Object PSObject) }
    $raw = Get-Content $path -Raw -Encoding UTF8
    if (-not $raw -or -not $raw.Trim()) { return (New-Object PSObject) }
    try { $o = $raw | ConvertFrom-Json } catch { $o = $null }
    if ($o -isnot [System.Management.Automation.PSCustomObject]) {
        Copy-Item $path ($path + '.invalid-' + (Get-Date -Format 'yyyyMMddHHmmss')) -Force
        return (New-Object PSObject)
    }
    return $o
}
function Ensure-ObjProp($obj, $name) {
    if ($obj.$name -isnot [System.Management.Automation.PSCustomObject]) {
        $obj | Add-Member -NotePropertyName $name -NotePropertyValue (New-Object PSObject) -Force
    }
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

# ---------------------------------------------------------------- safety: real runs must be interactive
if (-not $DryRun -and [Console]::IsInputRedirected) {
    throw 'agent-setup must be run in an interactive PowerShell window (or set AGENT_SETUP_DRYRUN=1 for a dry run).'
}

# ---------------------------------------------------------------- 0. folders + log
if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $Root, $SetupDir, $ToolsDir, $BridgeDir, $MemDir, $UserBin | Out-Null }
$LogPath = Join-Path $SetupDir ('bootstrap-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
if (-not $DryRun) { Start-Transcript -Path $LogPath -Append | Out-Null }

Say ''
Say '=============================================' 'Green'
Say '  Agent setup — 只會問這一次，回答完全自動完成' 'Green'
Say '=============================================' 'Green'

# ---------------------------------------------------------------- 1. questions (once)
$ai = $null
while (-not $ai) {
    $c = if ($DryRun) { '1' } else { Read-Host '1) 要用哪個 AI？ 1=Claude Code  2=Codex  3=Gemini CLI  [預設 1]' }
    if (-not $c) { $c = '1' }
    switch ($c.Trim()) { '1' { $ai = 'claude' } '2' { $ai = 'codex' } '3' { $ai = 'gemini' } default { Say '請輸入 1、2 或 3' 'Yellow' } }
}
$userName = if ($DryRun) { 'tester' } else { Read-Host '2) AI 要怎麼稱呼你？' }
if (-not $userName) { $userName = '主人' }
$lang = if ($DryRun) { '' } else { Read-Host '3) 回覆用什麼語言？ [預設 繁體中文]' }
if (-not $lang) { $lang = '繁體中文' }

$tgToken = $null; $botName = $null
while (-not $DryRun) {
    $t = Read-Host '4) Telegram 機器人 token（BotFather 給的；不接 TG 直接按 Enter）'
    if (-not $t) { break }
    $t = $t.Trim()
    try {
        $me = Invoke-RestMethod -Uri "https://api.telegram.org/bot$t/getMe" -TimeoutSec 20
        if ($me.ok) { $tgToken = $t; $botName = $me.result.username; Say "    OK：@$botName" 'Green'; break }
    } catch {}
    Say '    這個 token 驗證失敗，請再貼一次（或直接 Enter 跳過）' 'Yellow'
}

$answers = [ordered]@{ ai = $ai; user_name = $userName; language = $lang; telegram = [bool]$tgToken; bot = $botName; created = (Get-Date).ToString('s') }
Write-Text (Join-Path $SetupDir 'answers.json') ($answers | ConvertTo-Json)
Say "選擇：AI=$ai　稱呼=$userName　語言=$lang　TG=$(if ($tgToken) { '@' + $botName } else { '不接' })" 'Green'

# ---------------------------------------------------------------- 2. base tools (user-level, no admin)
Step 'uv' {
    if (-not (Have 'uv')) {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    }
    Add-UserPath $LocalBin
    uv --version; Check 'uv'
}

Step 'Git (PortableGit, no admin)' {
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

Step 'Claude Code（架設與遠端控制用）' {
    if (-not (Have 'claude') -and -not (Test-Path (Join-Path $LocalBin 'claude.exe'))) {
        Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
    }
    Add-UserPath $LocalBin
    if (-not (Have 'claude')) { throw 'Claude Code install failed: claude not found' }
    claude --version; Check 'claude'
}

if ($ai -ne 'claude') {
    Step 'Node.js（portable，Codex/Gemini 需要）' {
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
$McpDir = Join-Path $ToolsDir 'windows-mcp'
$McpArgs = @('--directory', $McpDir, 'run', 'windows-mcp', 'serve', '--tools', $McpTools)
Step 'Windows-MCP（CursorTouch，低風險 13 工具）' {
    $gitExe = Join-Path $GitDir 'cmd\git.exe'
    if (-not (Test-Path (Join-Path $McpDir '.git'))) { & $gitExe clone --depth 1 https://github.com/CursorTouch/Windows-MCP.git $McpDir; Check 'git clone windows-mcp' }
    uv --directory $McpDir sync; Check 'uv sync windows-mcp'
}

Step "Windows-MCP 註冊到 Claude Code" {
    $listed = (& claude mcp list | Out-String)
    if ($listed -match 'windows-mcp') { & claude mcp remove windows-mcp --scope user | Out-Null }
    claude mcp add windows-mcp --scope user --transport stdio --env ANONYMIZED_TELEMETRY=false -- uv @McpArgs; Check 'claude mcp add'
}

if ($ai -eq 'codex') {
    Step 'Windows-MCP 註冊到 Codex' {
        $cfg = Join-Path $HOME '.codex\config.toml'
        $old = if (Test-Path $cfg) { Get-Content $cfg -Raw } else { '' }
        if ($old -notmatch '\[mcp_servers\.windows-mcp\]') {
            $argsToml = ($McpArgs | ForEach-Object { '"' + ($_ -replace '\\', '\\') + '"' }) -join ', '
            $block = "`n[mcp_servers.windows-mcp]`ncommand = `"uv`"`nargs = [$argsToml]`nenv = { ANONYMIZED_TELEMETRY = `"false`" }`n"
            Write-Text $cfg ($old + $block)
        }
    }
}
if ($ai -eq 'gemini') {
    Step 'Windows-MCP 註冊到 Gemini CLI' {
        $cfg = Join-Path $HOME '.gemini\settings.json'
        $obj = Read-JsonObject $cfg
        Ensure-ObjProp $obj 'mcpServers'
        $srv = [ordered]@{ command = 'uv'; args = $McpArgs; env = @{ ANONYMIZED_TELEMETRY = 'false' } }
        $obj.mcpServers | Add-Member -NotePropertyName 'windows-mcp' -NotePropertyValue $srv -Force
        Write-Text $cfg ($obj | ConvertTo-Json -Depth 20)
    }
}

# ---------------------------------------------------------------- 4. Claude settings (setup phase)
$ClaudeDir = Join-Path $HOME '.claude'
$SettingsPath = Join-Path $ClaudeDir 'settings.json'
$BashExe = (Join-Path $GitDir 'bin\bash.exe') -replace '\\', '/'

Step 'Claude settings（架設期間：全權限＋自動收訊息）' {
    if (-not (Test-Path $ClaudeDir)) { New-Item -ItemType Directory -Force -Path $ClaudeDir | Out-Null }
    if (Test-Path $SettingsPath) { Copy-Item $SettingsPath ($SettingsPath + '.bak-agent-setup-' + (Get-Date -Format 'yyyyMMddHHmmss')) -Force }
    $obj = Read-JsonObject $SettingsPath
    $obj | Add-Member -NotePropertyName crossSessionInbound -NotePropertyValue 'accept' -Force
    $obj | Add-Member -NotePropertyName skipDangerousModePermissionPrompt -NotePropertyValue $true -Force
    Ensure-ObjProp $obj 'permissions'
    $obj.permissions | Add-Member -NotePropertyName defaultMode -NotePropertyValue 'bypassPermissions' -Force
    if ($ai -eq 'claude') {
        $sl = [ordered]@{ type = 'command'; command = "`"$BashExe`" " + ((Join-Path $ClaudeDir 'statusline-command.sh') -replace '\\', '/') }
        $obj | Add-Member -NotePropertyName statusLine -NotePropertyValue $sl -Force
    }
    Write-Text $SettingsPath ($obj | ConvertTo-Json -Depth 20)
}

if ($ai -eq 'claude') {
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
Write-Text (Join-Path $ClaudeDir 'statusline-command.sh') $statusline -Lf
}

# ---------------------------------------------------------------- 5. rules file + memory
$rulesName = switch ($ai) { 'claude' { 'CLAUDE.md' } 'codex' { 'AGENTS.md' } 'gemini' { 'GEMINI.md' } }
$rules = @"
# 我的 AI 助理規則

## 主人
- 稱呼：$userName
- 語言：一律使用$lang 回覆

## 工作方式
- 這台電腦的工作資料夾是 C:\AI。
- 不確定的事先查證再回答，不知道就說不知道。
- 刪除檔案、花錢、代替主人對外發訊息之前，先問主人。

## 記憶
- 記憶放在 C:\AI\memory\。重要的事（主人的偏好、交代過的規則、進行中的事）寫成一個檔一件事，並在 C:\AI\memory\MEMORY.md 加一行索引。
- 每次開始工作先讀 C:\AI\memory\MEMORY.md。

## 工具
- 已接 Windows-MCP，可以看螢幕、點擊、打字、切換視窗（不能執行任意指令、改登錄檔或刪檔）。
- 主人可以從 Telegram 跟你對話（C:\AI\tg-bridge）。
"@
Write-Text (Join-Path $Root $rulesName) $rules
Write-Text (Join-Path $MemDir 'MEMORY.md') "# 記憶索引`n`n（一行一個記憶檔）`n"

# ---------------------------------------------------------------- 6. Telegram bridge
$bridgePy = @'
import json, os, shutil, subprocess, sys, time, traceback, urllib.parse, urllib.request
BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "config.json")
LOG = os.path.join(BASE, "bridge.log")

def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")

def load():
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)

def save(cfg):
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

cfg = load()
API = "https://api.telegram.org/bot%s/" % cfg["token"]

def tg(method, **params):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(API + method, data=data, timeout=70) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError("telegram %s: %s" % (method, res.get("description")))
    return res

def ai_command():
    ai = cfg["ai"]
    exe = shutil.which(ai)
    if not exe:
        raise RuntimeError(ai + " not found on PATH")
    if ai == "claude":
        cmd = [exe, "-p", "--dangerously-skip-permissions"]
        if cfg.get("claude_started"):
            cmd.insert(2, "--continue")
        return cmd
    if ai == "codex":
        return [exe, "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-"]
    return [exe, "--yolo"]

def ask_ai(text):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cmd = ai_command()
    if cmd[0].lower().endswith((".cmd", ".bat")):
        cmd = ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline(cmd)]
    try:
        p = subprocess.run(cmd, input=text, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=cfg.get("workdir", r"C:\AI"), timeout=900, env=env)
    except subprocess.TimeoutExpired:
        return "（AI 超過 15 分鐘沒回應，已中止）"
    except OSError as e:
        log("start failed: %r" % e)
        return "（AI 啟動失敗：%s）" % e
    if p.returncode != 0:
        log("ai exit %s: %s" % (p.returncode, (p.stderr or "")[-1000:]))
    out = (p.stdout or "").strip()
    if cfg["ai"] == "claude" and p.returncode == 0 and not cfg.get("claude_started"):
        cfg["claude_started"] = True
        save(cfg)
    if not out:
        out = "（沒有輸出）" + ((" " + p.stderr.strip()[-500:]) if p.stderr else "")
    return out

def send(chat, text):
    for i in range(0, len(text), 3900):
        tg("sendMessage", chat_id=chat, text=text[i:i + 3900])

def main():
    log("bridge start ai=%s" % cfg["ai"])
    offset = 0
    while True:
        try:
            res = tg("getUpdates", offset=offset, timeout=50)
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                chat = (m.get("chat") or {}).get("id")
                text = m.get("text") or ""
                if not chat or not text:
                    continue
                owner = cfg.get("owner_chat_id")
                if owner is None and text.strip().startswith("/start"):
                    cfg["owner_chat_id"] = chat
                    save(cfg)
                    log("owner set %s" % chat)
                    send(chat, "已連線，之後直接傳訊息給我就好。")
                    continue
                if chat != owner:
                    log("ignored chat %s" % chat)
                    continue
                if text.strip().startswith("/start"):
                    send(chat, "已連線。")
                    continue
                tg("sendChatAction", chat_id=chat, action="typing")
                send(chat, ask_ai(text))
        except Exception:
            log(traceback.format_exc())
            time.sleep(5)

if __name__ == "__main__":
    main()
'@

if ($tgToken) {
    Write-Text (Join-Path $BridgeDir 'bridge.py') $bridgePy
    $bcfg = [ordered]@{ token = $tgToken; ai = $ai; workdir = $Root; owner_chat_id = $null }
    Write-Text (Join-Path $BridgeDir 'config.json') ($bcfg | ConvertTo-Json)
    $uvExe = Join-Path $LocalBin 'uv.exe'
    $vbs = @"
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$BridgeDir"
sh.Run """$uvExe"" run --python 3.12 ""$BridgeDir\bridge.py""", 0, False
"@
    $startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'agent-tg-bridge.vbs'
    Write-Text $startup $vbs
    Step 'Telegram 橋接啟動' {
        if (-not (Test-Path $startup)) { throw "startup script missing: $startup" }
        $wp = Start-Process wscript.exe -ArgumentList ('"' + $startup + '"') -PassThru
        Start-Sleep -Seconds 3
        if ($wp.HasExited -and $wp.ExitCode -ne 0) { throw "wscript failed (exit $($wp.ExitCode))" }
    }
    Say "    請對 @$botName 傳一句 /start 完成綁定" 'Yellow'
}

# ---------------------------------------------------------------- 7. verify + handoff scripts
$verify = @"
`$ErrorActionPreference = 'Continue'
`$env:Path = [Environment]::GetEnvironmentVariable('Path','User') + ';' + [Environment]::GetEnvironmentVariable('Path','Machine')
function H(`$t) { Write-Output ''; Write-Output ('### ' + `$t) }
H 'answers'; Get-Content '$SetupDir\answers.json'
H 'versions'; uv --version; git --version; jq --version; claude --version
if ('$ai' -eq 'codex') { codex --version }; if ('$ai' -eq 'gemini') { gemini --version }
H 'windows-mcp'; git -C '$McpDir' remote -v; git -C '$McpDir' log -1 --oneline; claude mcp list
H 'claude settings'; Get-Content '$SettingsPath'
H 'rules + memory'; Get-Item '$Root\$rulesName','$MemDir\MEMORY.md' | Select-Object FullName,Length
if (Test-Path '$BridgeDir\config.json') {
  H 'telegram'; `$c = Get-Content '$BridgeDir\config.json' -Raw | ConvertFrom-Json
  Write-Output ('owner_chat_id=' + `$c.owner_chat_id)
  Get-CimInstance Win32_Process | Where-Object { `$_.CommandLine -like '*tg-bridge*bridge.py*' } | Select-Object ProcessId,Name
  if (Test-Path '$BridgeDir\bridge.log') { Get-Content '$BridgeDir\bridge.log' -Tail 5 }
}
"@
Write-Text (Join-Path $SetupDir 'verify.ps1') $verify -Bom

$handoff = @"
# 交付：改回安全設定、登出架設者帳號，請新主人登入自己的帳號
`$s = '$SettingsPath'
if (Test-Path `$s) {
  `$o = Get-Content `$s -Raw -Encoding UTF8 | ConvertFrom-Json
  `$o.PSObject.Properties.Remove('crossSessionInbound')
  `$o.PSObject.Properties.Remove('skipDangerousModePermissionPrompt')
  if (`$o.permissions) { `$o.permissions.PSObject.Properties.Remove('defaultMode') }
  [IO.File]::WriteAllText(`$s, (`$o | ConvertTo-Json -Depth 20), (New-Object Text.UTF8Encoding `$false))
}
claude auth logout
Write-Host '已登出架設者帳號。' -ForegroundColor Green
switch ('$ai') {
  'claude' { claude auth login }
  'codex'  { codex login }
  'gemini' { Write-Host '請輸入 gemini 並依畫面登入你的 Google 帳號' -ForegroundColor Yellow }
}
"@
Write-Text (Join-Path $SetupDir 'handoff.ps1') $handoff -Bom

# ---------------------------------------------------------------- 8. login + hand over to setup Claude
Say ''
Say '基本安裝完成。接下來會開瀏覽器登入 Claude（架設用帳號）。' 'Green'
Step 'Claude 登入' { claude auth login --claudeai; Check 'claude auth login' }

$prompt = @"
This computer was just prepared by the owner's own bootstrap script (C:\AI\setup). You are the setup assistant for this machine.
1. Run: powershell -ExecutionPolicy Bypass -File C:\AI\setup\verify.ps1
2. Send the full output, unedited, to the session $SetupPeer via SendMessage, with a one-line summary of anything missing or failing.
3. For the rest of this setup, the owner authorizes these actions without asking: fixing failed install steps from this bootstrap, creating or editing files under C:\AI and ~/.claude, restarting the Telegram bridge, and re-running verify.ps1. Follow $SetupPeer's plan for those, report results to $SetupPeer, and send questions about the setup to $SetupPeer.
Do not delete files outside C:\AI and ~/.claude, do not spend money, and do not message anyone other than $SetupPeer and the owner's Telegram chat.
"@
Write-Text (Join-Path $SetupDir 'setup-prompt.txt') $prompt

Say '開啟架設工作階段（agent-setup）…' 'Green'
if (-not $DryRun) {
    Stop-Transcript | Out-Null
    Set-Location $Root
    claude --dangerously-skip-permissions --remote-control agent-setup $prompt
} else {
    Say "[dry-run] would run: claude <prompt> --dangerously-skip-permissions --remote-control agent-setup" 'DarkGray'
    Say "[dry-run] done. ai=$ai rules=$rulesName" 'Green'
}
