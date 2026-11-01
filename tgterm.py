# tgterm: Telegram <-> the agent's terminal (works the same for Claude Code / Codex / Gemini CLI)
#   pythonw tgterm.py bridge              forward the owner's Telegram messages into the running agent terminal
#   python  tgterm.py send "text"         reply to the owner on Telegram   (also: send --file <utf-8 file>, send - for stdin)
#   python  tgterm.py inject "text"       type one line into the agent terminal and press Enter (test; --mode console|paste)
#   python  tgterm.py status              bridge / agent process / agent window check
#   python  tgterm.py close-window        close the agent window like a user would (watchdog test)
#   pythonw tgterm.py watchdog            reopen the agent window / restart the bridge if gone (Task Scheduler, every minute)
#   python  tgterm.py merge-json <target.json> <patch.json>   deep-merge a patch into a JSON file (null deletes a key)
#   python  tgterm.py selftest            offline checks only; touches no window, console or network
# Typing into the terminal (config "mode"):
#   console  write key events into the agent console's input buffer (WriteConsoleInputW): no focus change, no clipboard
#   paste    winject style: bring the agent window forward, paste with Ctrl+V, press Enter, hand focus back
import ctypes, json, os, shutil, subprocess, sys, time, traceback, urllib.error, urllib.parse, urllib.request
from ctypes import wintypes

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "config.json")
STATE = os.path.join(BASE, "tgterm-state.json")
LOG = os.path.join(BASE, "tgterm.log")
WATCHDOG_LOG = os.path.join(BASE, "watchdog.log")
PIDFILE = os.path.join(BASE, "agent.pid")
PAUSED = os.path.join(BASE, "paused")
INBOX = os.path.join(BASE, "tg-inbox")
MUTEX = "Local\\agent-tgterm-bridge"
DETACHED_PROCESS, CREATE_NEW_CONSOLE, CREATE_NEW_PROCESS_GROUP = 0x8, 0x10, 0x200


def log(msg, path=LOG):
    try:
        if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_cfg():
    cfg = load_json(CFG, None)
    if not isinstance(cfg, dict):
        raise RuntimeError("config.json missing or invalid: " + CFG)
    return cfg


# ---------------------------------------------------------------- Telegram
def tg(cfg, method, **params):
    if not cfg.get("token"):
        raise RuntimeError("Telegram is not set up (no token in config.json)")
    data = urllib.parse.urlencode(params).encode()
    url = "https://api.telegram.org/bot%s/%s" % (cfg["token"], method)
    with urllib.request.urlopen(url, data=data, timeout=70) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError("telegram %s: %s" % (method, res.get("description")))
    return res


def send(cfg, text):
    text = (text or "").strip() or "（空白）"
    if not cfg.get("owner_id"):
        raise RuntimeError("owner_id missing in config.json")
    for i in range(0, len(text), 3900):
        tg(cfg, "sendMessage", chat_id=cfg["owner_id"], text=text[i:i + 3900])
    log("sent %d chars" % len(text))


def download(cfg, file_id, name):
    info = tg(cfg, "getFile", file_id=file_id)["result"]
    os.makedirs(INBOX, exist_ok=True)
    ext = os.path.splitext(info.get("file_path", ""))[1]
    base = os.path.basename(name) if name else "file" + ext
    path = os.path.join(INBOX, time.strftime("%Y%m%d-%H%M%S-") + base)
    url = "https://api.telegram.org/file/bot%s/%s" % (cfg["token"], info["file_path"])
    with urllib.request.urlopen(url, timeout=120) as r, open(path, "wb") as f:
        f.write(r.read())
    return path


# ---------------------------------------------------------------- Win32
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)
HANDLE, DWORD, BOOL, UINT = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.UINT
k32.CreateFileW.restype = HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, DWORD, DWORD, ctypes.c_void_p, DWORD, DWORD, HANDLE]
k32.AttachConsole.argtypes = [DWORD]
k32.CloseHandle.argtypes = [HANDLE]
k32.OpenProcess.restype = HANDLE
k32.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
k32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
k32.GetProcessTimes.argtypes = [HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
k32.QueryFullProcessImageNameW.argtypes = [HANDLE, DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD)]
k32.CreateMutexW.restype = HANDLE
k32.CreateMutexW.argtypes = [ctypes.c_void_p, BOOL, wintypes.LPCWSTR]
k32.OpenMutexW.restype = HANDLE
k32.OpenMutexW.argtypes = [DWORD, BOOL, wintypes.LPCWSTR]
k32.GlobalAlloc.restype = ctypes.c_void_p
k32.GlobalAlloc.argtypes = [UINT, ctypes.c_size_t]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalLock.argtypes = [ctypes.c_void_p]
k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
k32.GlobalFree.argtypes = [ctypes.c_void_p]
u32.OpenClipboard.argtypes = [wintypes.HWND]
u32.GetClipboardData.restype = ctypes.c_void_p
u32.GetClipboardData.argtypes = [UINT]
u32.SetClipboardData.restype = ctypes.c_void_p
u32.SetClipboardData.argtypes = [UINT, ctypes.c_void_p]
u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.IsWindowVisible.argtypes = [wintypes.HWND]
u32.IsIconic.argtypes = [wintypes.HWND]
u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
u32.SetForegroundWindow.argtypes = [wintypes.HWND]
u32.GetForegroundWindow.restype = wintypes.HWND
u32.PostMessageW.argtypes = [wintypes.HWND, UINT, wintypes.WPARAM, wintypes.LPARAM]
u32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, DWORD, ctypes.c_void_p]
WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, wintypes.HWND, wintypes.LPARAM)
u32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
INVALID_HANDLE = ctypes.c_void_p(-1).value
STILL_ACTIVE = 259


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD), ("UnicodeChar", wintypes.WCHAR), ("dwControlKeyState", DWORD)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("_pad", wintypes.WORD), ("Event", KEY_EVENT_RECORD)]


k32.WriteConsoleInputW.argtypes = [HANDLE, ctypes.POINTER(INPUT_RECORD), DWORD, ctypes.POINTER(DWORD)]
KEY_EVENT, VK_RETURN, SCAN_RETURN = 0x0001, 0x0D, 0x1C
VK_MENU, VK_CONTROL, VK_V, KEYEVENTF_KEYUP = 0x12, 0x11, 0x56, 0x0002
CF_UNICODETEXT, GMEM_MOVEABLE, WM_CLOSE = 13, 0x0002, 0x0010


def process_info(pid):
    """(exe path, start time as epoch seconds) of a live process, else None"""
    h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        buf = ctypes.create_unicode_buffer(32768)
        n = DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return None
        t = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
            return None
        ticks = (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
        return buf.value, ticks / 1e7 - 11644473600     # FILETIME (1601, 100 ns) -> Unix epoch
    finally:
        k32.CloseHandle(h)


def agent_pid():
    """pid of the PowerShell that hosts the agent (run-agent.ps1 writes it at start)"""
    try:
        with open(PIDFILE, encoding="utf-8-sig") as f:
            pid = int(f.read().strip())
        written = os.path.getmtime(PIDFILE)
    except (OSError, ValueError):
        return None
    info = process_info(pid)
    if not info or os.path.basename(info[0]).lower() != "powershell.exe":
        return None
    # a process started after the pid file was written is a different process that reused the pid
    return pid if info[1] <= written + 2 else None


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
            r.Event.wVirtualScanCode = SCAN_RETURN
            r.Event.UnicodeChar = "\r"
            recs.append(r)
    return recs


def write_console(pid, recs):
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        raise OSError("AttachConsole(%s) failed: %s" % (pid, ctypes.get_last_error()))
    try:
        h = k32.CreateFileW("CONIN$", 0xC0000000, 0x3, None, 3, 0, None)  # GENERIC_READ|WRITE, SHARE_READ|WRITE, OPEN_EXISTING
        if not h or h == INVALID_HANDLE:
            raise OSError("open CONIN$ failed: %s" % ctypes.get_last_error())
        try:
            for i in range(0, len(recs), 256):
                chunk = recs[i:i + 256]
                arr = (INPUT_RECORD * len(chunk))(*chunk)
                n = DWORD(0)
                if not k32.WriteConsoleInputW(h, arr, len(chunk), ctypes.byref(n)):
                    raise OSError("WriteConsoleInputW failed: %s" % ctypes.get_last_error())
        finally:
            k32.CloseHandle(h)
    finally:
        k32.FreeConsole()


def inject_console(text):
    pid = agent_pid()
    if not pid:
        raise RuntimeError("agent terminal is not running")
    write_console(pid, records_for(text, enter=False))
    time.sleep(0.6)                                     # let the TUI take the text before Enter
    write_console(pid, records_for("", enter=True))
    return "console pid %s" % pid


def find_windows(title):
    found = []

    def cb(hwnd, _):
        if u32.IsWindowVisible(hwnd):
            n = u32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value.strip() == title:
                    found.append(hwnd)
        return True

    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def open_clipboard():
    for _ in range(10):
        if u32.OpenClipboard(None):
            return
        time.sleep(0.1)
    raise OSError("clipboard busy")


def clipboard_text():
    """the clipboard's text ('' when it holds no text)"""
    open_clipboard()
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = k32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p) if p else ""
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


def set_clipboard(text):
    """put text on the clipboard; empty text just clears it"""
    open_clipboard()
    try:
        u32.EmptyClipboard()
        if not text:
            return
        data = (text + "\0").encode("utf-16-le")
        h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            raise OSError("GlobalAlloc failed")
        p = k32.GlobalLock(h)
        if not p:
            k32.GlobalFree(h)
            raise OSError("GlobalLock failed")
        ctypes.memmove(p, data, len(data))
        k32.GlobalUnlock(h)
        if not u32.SetClipboardData(CF_UNICODETEXT, h):     # on success the clipboard owns h
            k32.GlobalFree(h)
            raise OSError("SetClipboardData failed")
    finally:
        u32.CloseClipboard()


def key(vk, up=False):
    u32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, None)


def focus(hwnd):
    key(VK_MENU)                                        # ALT tap lets this process move the foreground window
    key(VK_MENU, up=True)
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 9)                         # SW_RESTORE
    u32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    return u32.GetForegroundWindow() == hwnd


def inject_paste(title, text):
    wins = find_windows(title)
    if not wins:
        raise RuntimeError("no window titled %r" % title)
    hwnd = wins[0]
    prev_win, prev_clip = u32.GetForegroundWindow(), clipboard_text()
    try:
        set_clipboard(text)
        if not focus(hwnd):
            raise RuntimeError("could not focus the agent window")
        key(VK_CONTROL); key(VK_V); key(VK_V, up=True); key(VK_CONTROL, up=True)
        time.sleep(0.6)
        if u32.GetForegroundWindow() != hwnd:
            raise RuntimeError("focus moved away before Enter")
        key(VK_RETURN); key(VK_RETURN, up=True)
        time.sleep(0.2)
    finally:
        # put the user's text back (or clear ours); a non-text clipboard (e.g. an image) cannot be kept in this mode
        try:
            set_clipboard(prev_clip)
        except OSError:
            pass
        if prev_win and prev_win != hwnd:
            focus(prev_win)
    return "paste hwnd %s" % hwnd


def one_line(text):
    # a newline would submit early; control characters would act as keys
    text = "".join(ch if ch >= " " or ch in "\t\n\r" else " " for ch in text)
    return " ".join(text.split())


def inject(cfg, text, mode=None):
    text = one_line(text)
    if not text:
        return "empty"
    mode = mode or cfg.get("mode", "console")
    if mode == "paste":
        return inject_paste(cfg.get("agent_name", ""), text)
    return inject_console(text)


# ---------------------------------------------------------------- bridge loop
def handle(cfg, m):
    chat, frm = m.get("chat") or {}, m.get("from") or {}
    if chat.get("type") != "private" or frm.get("id") != cfg.get("owner_id"):
        log("ignored message from %s" % frm.get("id"))
        return
    text = (m.get("text") or m.get("caption") or "").strip()
    if text.startswith("/start"):
        send(cfg, "已連線，直接傳訊息給我就好。")
        return
    notes = []
    try:
        if m.get("photo"):
            notes.append("（主人傳了一張圖片，存在 %s）" % download(cfg, m["photo"][-1]["file_id"], ""))
        if m.get("document"):
            d = m["document"]
            notes.append("（主人傳了一個檔案，存在 %s）" % download(cfg, d["file_id"], d.get("file_name", "")))
    except Exception as e:
        log("download failed: %r" % e)
        notes.append("（主人傳了檔案，但下載失敗：%s）" % e)
    if not text and not notes:
        send(cfg, "目前只能收文字、圖片和檔案，請改用打字。")
        return
    body = " ".join(notes + [text]).strip()
    try:
        how = inject(cfg, cfg.get("prefix", "[TG] ") + body)
        log("injected (%s): %s" % (how, body[:80]))
    except Exception as e:
        log("inject failed: %r" % e)
        send(cfg, "（訊息沒送進 AI：%s。AI 視窗如果被關掉，1 分鐘內會自動重開，請稍後再傳一次）" % e)


def bridge():
    mutex = k32.CreateMutexW(None, False, MUTEX)        # held for the life of this process
    if not mutex or ctypes.get_last_error() == 183:    # ERROR_ALREADY_EXISTS
        if mutex:
            k32.CloseHandle(mutex)
        log("bridge already running; exit")
        return
    log("bridge start")
    offset = load_json(STATE, {}).get("offset", 0)
    while True:
        try:
            cfg = load_cfg()
            res = tg(cfg, "getUpdates", offset=offset, timeout=50, allowed_updates='["message"]')
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                save_json(STATE, {"offset": offset})
                handle(cfg, u.get("message") or {})
        except urllib.error.HTTPError as e:
            log("telegram http %s (409 = another program uses this bot)" % e.code)
            time.sleep(15)
        except Exception:
            log(traceback.format_exc())
            time.sleep(5)


# ---------------------------------------------------------------- watchdog
def bridge_running():
    h = k32.OpenMutexW(0x00100000, False, MUTEX)        # SYNCHRONIZE
    if h:
        k32.CloseHandle(h)
        return True
    return False


def open_agent_window(cfg):
    ps = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(BASE, "run-agent.ps1")]
    root = cfg.get("root", r"C:\AI")
    wt = shutil.which("wt.exe")
    if wt:
        # Windows Terminal keeps this title no matter what the AI prints
        subprocess.Popen([wt, "-w", "new", "--title", cfg.get("agent_name", "Agent"), "--suppressApplicationTitle", "-d", root] + ps)
        return "Windows Terminal"
    subprocess.Popen(ps, cwd=root, creationflags=CREATE_NEW_CONSOLE)
    return "console"


def watchdog():
    if os.path.exists(PAUSED):
        return
    cfg = load_cfg()
    if not agent_pid():
        lock = os.path.join(BASE, "watchdog.lock")
        if not (os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 90):
            with open(lock, "w") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
            log("opened agent window (%s)" % open_agent_window(cfg), WATCHDOG_LOG)
    if cfg.get("token") and not bridge_running():
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        subprocess.Popen([pyw if os.path.exists(pyw) else sys.executable, os.path.abspath(__file__), "bridge"],
                         cwd=BASE, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, close_fds=True)
        log("started Telegram bridge", WATCHDOG_LOG)


def merge_json(target, patch_file):
    def merge(dst, src):
        for k, v in src.items():
            if v is None:
                dst.pop(k, None)
            elif isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v

    data = {}
    if os.path.exists(target):
        backup = target + ".bak-agent-setup"
        if not os.path.exists(backup):                  # keep the state from before the first setup
            shutil.copy2(target, backup)
        data = load_json(target, None)
        if not isinstance(data, dict):
            shutil.copy2(target, target + ".invalid-" + time.strftime("%Y%m%d%H%M%S"))
            data = {}
    patch = load_json(patch_file, None)
    if not isinstance(patch, dict):
        raise RuntimeError("patch is not a JSON object: " + patch_file)
    merge(data, patch)
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    save_json(target, data)


# ---------------------------------------------------------------- CLI


def selftest():
    assert ctypes.sizeof(INPUT_RECORD) == 20, ctypes.sizeof(INPUT_RECORD)
    recs = records_for("測試a😀", enter=True)
    assert len(recs) == (5 + 1) * 2, len(recs)          # 測 試 a + emoji as 2 UTF-16 units, each down+up, plus Enter
    assert recs[-2].Event.wVirtualKeyCode == VK_RETURN and recs[-2].Event.bKeyDown
    assert one_line("a\nb\r\n  c\x1bd") == "a b c d", one_line("a\nb\r\n  c\x1bd")
    import tempfile
    d = tempfile.mkdtemp()
    t, p = os.path.join(d, "t.json"), os.path.join(d, "p.json")
    save_json(t, {"keep": 1, "drop": 2, "nest": {"a": 1, "b": 2}})
    save_json(p, {"drop": None, "nest": {"b": None, "c": "名"}, "new": True})
    merge_json(t, p)
    assert load_json(t, None) == {"keep": 1, "nest": {"a": 1, "c": "名"}, "new": True}, load_json(t, None)
    assert load_json(t + ".bak-agent-setup", None)["drop"] == 2
    shutil.rmtree(d)
    print("selftest ok")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "bridge"
    if cmd == "bridge":
        bridge()
    elif cmd == "send":
        cfg = load_cfg()
        if len(argv) > 3 and argv[2] == "--file":
            with open(argv[3], encoding="utf-8-sig") as f:
                text = f.read()
        elif len(argv) > 2 and argv[2] == "-":
            text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        else:
            text = " ".join(argv[2:])
        send(cfg, text)
        print("sent")
    elif cmd == "inject":
        args = argv[2:]
        mode = None
        if len(args) > 1 and args[0] == "--mode":
            mode, args = args[1], args[2:]
        # console mode must run detached: attaching to the agent console would cut this process off its own window
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "_inject", mode or "", " ".join(args)],
                           creationflags=DETACHED_PROCESS, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=dict(os.environ, PYTHONUTF8="1"))
        print((p.stdout or p.stderr or "").strip())
        return p.returncode
    elif cmd == "_inject":
        try:
            how = inject(load_cfg(), argv[3], argv[2] or None)
            log("test inject (%s): %s" % (how, argv[3][:80]))
            print("ok " + how)
        except Exception as e:
            log("test inject failed: %r" % e)
            print("FAIL %r" % e)
            return 1
    elif cmd == "status":
        cfg = load_cfg()
        name = cfg.get("agent_name", "")
        print("agent_name=%s" % name)
        print("mode=%s" % cfg.get("mode", "console"))
        print("bridge_running=%s" % bridge_running())
        print("agent_pid=%s" % agent_pid())
        print("windows_titled=%d" % len(find_windows(name)))
    elif cmd == "close-window":
        wins = find_windows(load_cfg().get("agent_name", ""))
        for w in wins:
            u32.PostMessageW(w, WM_CLOSE, 0, 0)
        print("closed %d window(s)" % len(wins))
        return 0 if wins else 1
    elif cmd == "watchdog":
        try:
            watchdog()
        except Exception:
            log(traceback.format_exc(), WATCHDOG_LOG)
            return 1
    elif cmd == "merge-json":
        merge_json(argv[2], argv[3])
        print("merged " + argv[2])
    elif cmd == "selftest":
        selftest()
    else:
        print(__doc__ or "usage: see the top of tgterm.py")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
