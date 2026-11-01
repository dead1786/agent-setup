# Telegram <-> agent terminal (universal: Claude Code / Codex / Gemini CLI)
#   python tgterm.py              run the bridge: owner's Telegram messages are typed into the agent's terminal
#   python tgterm.py send "text"  send a reply to the owner's Telegram (the agent calls this)
#   python tgterm.py inject "text" type one line into the agent terminal (test)
# Text is written straight into the agent console's input buffer (WriteConsoleInputW): no focus change, no SendInput.
import ctypes, json, os, sys, time, traceback, urllib.parse, urllib.request
from ctypes import wintypes

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "config.json")
LOG = os.path.join(BASE, "tgterm.log")
MARKER = "run-agent.ps1"   # command-line marker of the PowerShell window that hosts the agent


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def load():
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


cfg = load()
API = "https://api.telegram.org/bot%s/" % cfg["token"]


def tg(method, **params):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(API + method, data=data, timeout=70) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError("telegram %s: %s" % (method, res.get("description")))
    return res


def send(text):
    text = text.strip() or "（空白）"
    for i in range(0, len(text), 3900):
        tg("sendMessage", chat_id=cfg["owner_chat_id"], text=text[i:i + 3900])


# ---------------------------------------------------------------- console injection
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.AttachConsole.argtypes = [wintypes.DWORD]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
INVALID_HANDLE = ctypes.c_void_p(-1).value


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD), ("UnicodeChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("_pad", wintypes.WORD), ("Event", KEY_EVENT_RECORD)]


KEY_EVENT = 0x0001
VK_RETURN = 0x0D


def agent_pid():
    import subprocess
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
          "Where-Object { $_.CommandLine -like '*%s*' } | Select-Object -First 1 -ExpandProperty ProcessId" % MARKER)
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         creationflags=0x08000000).stdout.strip()
    return int(out) if out.isdigit() else None


def records_for(text, enter):
    recs = []
    units = text.encode("utf-16-le")
    for i in range(0, len(units), 2):
        ch = units[i:i + 2].decode("utf-16-le", errors="surrogatepass")
        for down in (True, False):
            r = INPUT_RECORD()
            r.EventType = KEY_EVENT
            r.Event.bKeyDown = down
            r.Event.wRepeatCount = 1
            r.Event.UnicodeChar = ch
            recs.append(r)
    if enter:
        for down in (True, False):
            r = INPUT_RECORD()
            r.EventType = KEY_EVENT
            r.Event.bKeyDown = down
            r.Event.wRepeatCount = 1
            r.Event.wVirtualKeyCode = VK_RETURN
            r.Event.UnicodeChar = "\r"
            recs.append(r)
    return recs


def write_console(pid, recs):
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        raise OSError("AttachConsole(%s) failed: %s" % (pid, ctypes.get_last_error()))
    try:
        h = k32.CreateFileW("CONIN$", 0xC0000000, 0x3, None, 3, 0, None)   # GENERIC_READ|WRITE, SHARE_READ|WRITE, OPEN_EXISTING
        if not h or h == INVALID_HANDLE:
            raise OSError("open CONIN$ failed: %s" % ctypes.get_last_error())
        try:
            arr = (INPUT_RECORD * len(recs))(*recs)
            n = wintypes.DWORD(0)
            if not k32.WriteConsoleInputW(h, arr, len(recs), ctypes.byref(n)):
                raise OSError("WriteConsoleInputW failed: %s" % ctypes.get_last_error())
            return n.value
        finally:
            k32.CloseHandle(h)
    finally:
        k32.FreeConsole()


def inject(text):
    pid = agent_pid()
    if not pid:
        raise RuntimeError("agent terminal not running")
    line = " ".join(text.split())               # one line: a newline would submit early
    write_console(pid, records_for(line, enter=False))
    time.sleep(0.4)                              # let the TUI take the text before Enter
    write_console(pid, records_for("", enter=True))
    return pid


# ---------------------------------------------------------------- bridge loop
def bridge():
    log("tgterm start")
    offset = 0
    owner = cfg["owner_chat_id"]
    while True:
        try:
            res = tg("getUpdates", offset=offset, timeout=50)
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                chat = (m.get("chat") or {}).get("id")
                text = (m.get("text") or m.get("caption") or "").strip()
                if not chat or not text:
                    continue
                if chat != owner:
                    log("ignored chat %s" % chat)
                    continue
                if text.startswith("/start"):
                    send("已連線，直接傳訊息給我就好。")
                    continue
                try:
                    pid = inject(cfg.get("prefix", "[TG] ") + text)
                    log("injected to pid %s: %s" % (pid, text[:80]))
                except Exception as e:
                    log("inject failed: %r" % e)
                    send("（AI 終端目前沒開，訊息沒送進去，1 分鐘內會自動重開，請稍後再傳）")
        except Exception:
            log(traceback.format_exc())
            time.sleep(5)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "send":
        send(" ".join(sys.argv[2:]))
        print("sent")
    elif len(sys.argv) >= 3 and sys.argv[1] == "inject":
        print("injected into pid", inject(" ".join(sys.argv[2:])))
    else:
        bridge()
