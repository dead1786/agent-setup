# tgterm: Telegram <-> the agent's terminal (works the same for Claude Code / Codex / Gemini CLI)
#   pythonw tgterm.py bridge              forward the owner's Telegram messages into the running agent terminal
#   python  tgterm.py send "text"         reply to the owner on Telegram   (also: send --file <utf-8 file>, send - for stdin)
#   python  tgterm.py inject "text"       type one line into the agent terminal and press Enter (test; --mode console|paste)
#   python  tgterm.py status              bridge / agent process / agent window check
#   python  tgterm.py close-window        close the agent window like a user would (watchdog test)
#   pythonw tgterm.py watchdog            reopen the agent window / restart the bridge if gone (Task Scheduler, every minute)
#   python  tgterm.py merge-json <target.json> <patch.json>   deep-merge a patch into a JSON file (null deletes a key)
#   agent-login-url.exe <url>             BROWSER hook for `claude auth login`: no browser on this computer; the sign-in
#                                         link goes out as a QR code on Telegram, the helper signs in on their own phone
#                                         and the code shown there is pasted back into the terminal
#   python  tgterm.py selftest            offline checks only; touches no window, console or network
# Typing into the terminal (config "mode"):
#   console  write key events into the agent console's input buffer (WriteConsoleInputW): no focus change, no clipboard
#   paste    winject style: bring the agent window forward, paste with Ctrl+V, press Enter, hand focus back
import ctypes, json, os, shutil, subprocess, sys, threading, time, traceback, urllib.error, urllib.parse, urllib.request, uuid
from ctypes import wintypes

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "config.json")
STATE = os.path.join(BASE, "tgterm-state.json")
LOG = os.path.join(BASE, "tgterm.log")
WATCHDOG_LOG = os.path.join(BASE, "watchdog.log")
PIDFILE = os.path.join(BASE, "agent.pid")
PAUSED = os.path.join(BASE, "paused")
HANDOFF_MARK = os.path.join(BASE, "handoff-pending")   # remote setup: installer signed in, sign-out not done yet
INBOX = os.path.join(BASE, "tg-inbox")
LAST_SENT = os.path.join(BASE, "last-sent")            # time of the latest reply to Telegram
FRESH = os.path.join(BASE, "fresh-start")              # next AI start opens a new conversation
REMINDERS = os.path.join(BASE, "reminders.json")
MUTEX = "Local\\agent-tgterm-bridge"
# Claude Code's manual sign-in redirect (MANUAL_REDIRECT_URL in its source): the page shows a code to paste back
MANUAL_REDIRECT = "https://platform.claude.com/oauth/code/callback"
DETACHED_PROCESS, CREATE_NEW_CONSOLE, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW = 0x8, 0x10, 0x200, 0x08000000


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


def mark_sent():
    # the bridge stops showing "typing…" once a reply has gone out
    try:
        with open(LAST_SENT, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def send(cfg, text):
    text = (text or "").strip() or "（空白）"
    if not cfg.get("owner_id"):
        raise RuntimeError("owner_id missing in config.json")
    for i in range(0, len(text), 3900):
        tg(cfg, "sendMessage", chat_id=cfg["owner_id"], text=text[i:i + 3900])
    mark_sent()
    log("sent %d chars" % len(text))


# Telegram Bot API upload limits: photos 10 MB, everything else 50 MB
MEDIA = {"photo": ("sendPhoto", 10), "document": ("sendDocument", 50), "video": ("sendVideo", 50)}


def send_media(cfg, kind, path, caption=""):
    method, limit_mb = MEDIA[kind]
    if not os.path.isfile(path):
        raise RuntimeError("file not found: " + path)
    size = os.path.getsize(path)
    if size > limit_mb * 1024 * 1024:
        if kind == "photo" and size <= 50 * 1024 * 1024:
            kind, method = "document", "sendDocument"           # too big for a photo: send it as a file
        else:
            raise RuntimeError("file is %.1f MB; Telegram bots can send at most %d MB" % (size / 1048576, limit_mb))
    boundary = "----agentsetup" + uuid.uuid4().hex
    with open(path, "rb") as f:
        data = f.read()
    body = b""
    for name, value in (("chat_id", str(cfg["owner_id"])), ("caption", caption[:1000])):
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, name, value)).encode("utf-8")
    filename = os.path.basename(path).replace('"', "_")
    body += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
             'Content-Type: application/octet-stream\r\n\r\n' % (boundary, kind, filename)).encode("utf-8") + data + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode("utf-8")
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (cfg["token"], method), data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=300) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError("telegram %s: %s" % (method, res.get("description")))
    mark_sent()
    log("sent %s %s (%d bytes)" % (kind, filename, size))


def send_photo(cfg, path, caption):
    send_media(cfg, "photo", path, caption)


def download(cfg, file_id, name):
    info = tg(cfg, "getFile", file_id=file_id)["result"]
    os.makedirs(INBOX, exist_ok=True)
    ext = os.path.splitext(info.get("file_path", ""))[1]
    base = os.path.basename(name) if name else "file" + ext
    path = os.path.join(INBOX, time.strftime("%Y%m%d-%H%M%S-") + base)
    url = "https://api.telegram.org/file/bot%s/%s" % (cfg["token"], info["file_path"])
    with urllib.request.urlopen(url, timeout=300) as r, open(path, "wb") as f:
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
k32.WaitForSingleObject.restype = DWORD
k32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
k32.ReleaseMutex.argtypes = [HANDLE]
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
    # share winsend.py's lock so a LINE / window send and this paste never fight over the focus
    mutex = k32.CreateMutexW(None, False, "Local\\agent-winsend")
    if mutex and k32.WaitForSingleObject(mutex, 60000) in (0, 0x80):      # signaled or abandoned
        try:
            return _inject_paste(title, text)
        finally:
            k32.ReleaseMutex(mutex)
            k32.CloseHandle(mutex)
    if mutex:
        k32.CloseHandle(mutex)
    raise RuntimeError("another window send is still running")


def _inject_paste(title, text):
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


VK_ESCAPE = 0x1B


def press_escape(cfg):
    """interrupt whatever the AI is doing (Esc in Claude Code / Codex / agy)"""
    if cfg.get("mode") == "paste":
        wins = find_windows(cfg.get("agent_name", ""))
        if not wins or not focus(wins[0]):
            raise RuntimeError("could not focus the agent window")
        key(VK_ESCAPE); key(VK_ESCAPE, up=True)
        return "paste"
    pid = agent_pid()
    if not pid:
        raise RuntimeError("agent terminal is not running")
    recs = []
    for down in (True, False):
        r = INPUT_RECORD()
        r.EventType = KEY_EVENT
        r.Event.bKeyDown = down
        r.Event.wRepeatCount = 1
        r.Event.wVirtualKeyCode = VK_ESCAPE
        r.Event.wVirtualScanCode = 0x01
        r.Event.UnicodeChar = "\x1b"
        recs.append(r)
    write_console(pid, recs)
    return "console pid %s" % pid


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


# ---------------------------------------------------------------- screenshots, voice
def screenshot():
    from PIL import ImageGrab                           # installed by bootstrap (pillow)
    os.makedirs(INBOX, exist_ok=True)
    path = os.path.join(INBOX, time.strftime("screen-%Y%m%d-%H%M%S.png"))
    ImageGrab.grab(all_screens=True).save(path)
    return path


_voice_lock = threading.Lock()
_whisper = None
_ffmpeg = None


def uv_install(cfg, package):
    """add a package to this Python the first time a feature needs it"""
    import importlib
    uv = cfg.get("uv") or os.path.join(os.path.expanduser("~"), ".local", "bin", "uv.exe")
    p = subprocess.run([uv, "pip", "install", "--python", sys.executable, package], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError("install %s failed: %s" % (package, (p.stderr or p.stdout)[-300:]))
    importlib.invalidate_caches()


def ffmpeg_exe(cfg):
    """ffmpeg from the imageio-ffmpeg package (no admin, no PATH changes); installed on first use"""
    global _ffmpeg
    with _voice_lock:
        if _ffmpeg is None:
            try:
                import imageio_ffmpeg
            except ImportError:
                send(cfg, "第一次收到影片：正在安裝影片處理工具（約 1～2 分鐘，只有這次）…")
                uv_install(cfg, "imageio-ffmpeg")
                import imageio_ffmpeg
            _ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        return _ffmpeg


def video_frames(cfg, path, count=6):
    """evenly spaced stills from a video, so an AI that can see images can 'watch' it"""
    import re
    exe = ffmpeg_exe(cfg)
    probe = subprocess.run([exe, "-hide_banner", "-i", path], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=CREATE_NO_WINDOW)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", probe.stderr or "")
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    n = 1 if dur < 2 else min(count, max(2, int(dur // 2)))
    out_dir = os.path.splitext(path)[0] + "_frames"
    os.makedirs(out_dir, exist_ok=True)
    frames = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        out = os.path.join(out_dir, "frame_%02d_at_%ds.jpg" % (i + 1, int(t)))
        subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", "-ss", "%.2f" % t, "-i", path,
                        "-frames:v", "1", "-vf", "scale=960:-2", out], capture_output=True, creationflags=CREATE_NO_WINDOW)
        if os.path.exists(out):
            frames.append(out)
    if not frames:
        raise RuntimeError("no frames could be read")
    return out_dir, frames, dur


def transcribe(cfg, path):
    """speech -> text with a local faster-whisper model; installed and downloaded the first time it is needed"""
    global _whisper
    with _voice_lock:
        if _whisper is None:
            try:
                import faster_whisper  # noqa: F401
            except ImportError:
                send(cfg, "第一次收到語音：正在安裝語音辨識（約 3～5 分鐘，只有這次會等比較久）…")
                uv_install(cfg, "faster-whisper")
            from faster_whisper import WhisperModel
            _whisper = WhisperModel(cfg.get("voice_model", "small"), device="cpu", compute_type="int8")
        segments, _ = _whisper.transcribe(path, language=cfg.get("voice_language", "zh"),
                                          initial_prompt="以下是繁體中文的句子。")
        return "".join(s.text for s in segments).strip()


# ---------------------------------------------------------------- bridge loop
# what Telegram can send us -> (tag shown to the AI, where the file id lives)
INCOMING = (
    ("photo", "[圖片]"), ("document", "[檔案]"), ("video", "[影片]"), ("video_note", "[影片]"),
    ("animation", "[動圖]"), ("voice", "[語音]"), ("audio", "[音訊]"), ("sticker", "[貼圖]"),
)
COMMANDS = [("status", "AI 狀態"), ("stop", "中斷 AI 正在做的事"), ("restart", "重開 AI 視窗"),
            ("new", "重開並開始新對話"), ("screen", "傳目前的電腦畫面"), ("help", "指令說明")]
ALIASES = {"狀態": "status", "停": "stop", "停止": "stop", "重開": "restart", "新對話": "new",
           "畫面": "screen", "截圖": "screen", "說明": "help"}
_awaiting = {"since": 0.0}                              # when the AI got a Telegram message it has not answered yet


def typing_loop():
    """keep "typing…" visible on the owner's Telegram until the AI replies (at most 5 minutes)"""
    while True:
        time.sleep(4)
        since = _awaiting["since"]
        if not since:
            continue
        try:
            replied = os.path.exists(LAST_SENT) and os.path.getmtime(LAST_SENT) >= since
            if replied or time.time() - since > 300:
                _awaiting["since"] = 0.0
                continue
            cfg = load_cfg()
            tg(cfg, "sendChatAction", chat_id=cfg["owner_id"], action="typing")
        except Exception as e:
            log("typing: %r" % e)


_inject_lock = threading.Lock()                         # one typing into the AI window at a time (voice runs in threads)


def deliver(cfg, body, date=None):
    try:
        with _inject_lock:
            how = inject(cfg, cfg.get("prefix", "[TG] ") + body)
        _awaiting["since"] = time.time()
        # lag = from the owner pressing send (Telegram's timestamp) to the text being typed into the AI window
        lag = time.time() - (date or time.time())
        log("injected (%s) lag=%.1fs: %s" % (how, lag, one_line(body)[:80]))
    except Exception as e:
        log("inject failed: %r" % e)
        send(cfg, "（訊息沒送進 AI：%s。AI 視窗如果被關掉，1 分鐘內會自動重開，請稍後再傳一次）" % e)


def restart_agent(cfg, fresh=False):
    if fresh:
        open(FRESH, "w").close()
    for hwnd in find_windows(cfg.get("agent_name", "")):
        u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    for _ in range(20):                                 # wait for the old window to go away
        if not agent_pid():
            break
        time.sleep(0.5)
    lock = os.path.join(BASE, "watchdog.lock")
    if os.path.exists(lock):
        os.remove(lock)
    watchdog()


def command(cfg, name):
    if name == "status":
        pid = agent_pid()
        sent = time.strftime("%m/%d %H:%M", time.localtime(os.path.getmtime(LAST_SENT))) if os.path.exists(LAST_SENT) else "還沒有"
        busy = "（還在處理你上一則訊息）" if _awaiting["since"] else ""
        send(cfg, "AI 視窗：%s%s\n最後一次回覆：%s\n自動重開：%s" % (
            "運作中" if pid else "沒有開著（1 分鐘內會自動重開）", busy, sent,
            "暫停中" if os.path.exists(PAUSED) else "開啟"))
    elif name == "stop":
        with _inject_lock:
            how = press_escape(cfg)
        send(cfg, "已送出停止（等於在 AI 視窗按 Esc）：%s" % how)
        _awaiting["since"] = 0.0
    elif name in ("restart", "new"):
        with _inject_lock:
            restart_agent(cfg, fresh=(name == "new"))
        send(cfg, "AI 視窗已重開%s。" % ("，這次是新的對話" if name == "new" else "，會接著之前的對話"))
    elif name == "screen":
        send_media(cfg, "photo", screenshot(), "目前的電腦畫面 " + time.strftime("%H:%M"))
    else:
        send(cfg, "直接傳訊息就是跟 AI 說話；圖片、檔案、影片、語音都可以傳。\n" +
             "\n".join("/%s  %s" % c for c in COMMANDS) +
             "\n（也可以打中文：/狀態 /停 /重開 /新對話 /畫面）")


def handle(cfg, m):
    chat, frm = m.get("chat") or {}, m.get("from") or {}
    if chat.get("type") != "private" or frm.get("id") != cfg.get("owner_id"):
        log("ignored message from %s" % frm.get("id"))
        return
    text = (m.get("text") or m.get("caption") or "").strip()
    if text.startswith("/"):
        name = text[1:].split()[0].split("@")[0] if len(text) > 1 else ""
        name = ALIASES.get(name, name.lower())
        if name == "start":
            send(cfg, "已連線，直接傳訊息給我就好。輸入 /help 看遙控指令。")
            return
        if name in dict(COMMANDS):
            try:
                command(cfg, name)
            except Exception as e:
                log("command %s failed: %r" % (name, e))
                send(cfg, "（/%s 沒有成功：%s）" % (name, e))
            return
    for field, tag in INCOMING:
        item = m.get(field)
        if not item:
            continue
        if field == "photo":
            item = item[-1]                             # the largest size
        if item.get("file_size", 0) > 20 * 1024 * 1024:
            send(cfg, "這個%s超過 20MB，Telegram 機器人收不到，請壓縮或改用雲端連結。" % tag.strip("[]"))
            return
        try:
            path = download(cfg, item["file_id"], item.get("file_name", ""))
        except Exception as e:
            log("download failed: %r" % e)
            deliver(cfg, "%s（下載失敗：%s） %s" % (tag, e, text), m.get("date"))
            return
        if field == "sticker":
            text = (item.get("emoji", "") + " " + text).strip()
        if field in ("video", "video_note", "animation"):
            # stills the AI can look at + the soundtrack as text (animations have no sound)
            def vwork(path=path, tag=tag, caption=text, date=m.get("date"), sound=(field != "animation")):
                notes = []
                try:
                    folder, frames, dur = video_frames(cfg, path)
                    notes.append("已截 %d 張畫面（影片約 %d 秒，平均截取，請用看圖的方式看）：%s" % (len(frames), dur, folder))
                except Exception as e:
                    log("video frames failed: %r" % e)
                    notes.append("截畫面失敗：%s" % e)
                if sound:
                    try:
                        said = transcribe(cfg, path)
                        notes.append("聲音轉文字：" + (said or "（沒有說話聲）"))
                    except Exception as e:
                        log("video transcribe failed: %r" % e)
                        notes.append("聲音轉文字失敗：%s" % e)
                deliver(cfg, "%s %s（%s） %s" % (tag, path, "；".join(notes), caption), date)
            threading.Thread(target=vwork, daemon=True).start()
            return
        if field in ("voice", "audio"):
            # transcribing can take a while (and installs itself the first time): do it off the polling loop
            def work(path=path, tag=tag, caption=text, date=m.get("date")):
                try:
                    said = transcribe(cfg, path)
                    deliver(cfg, "%s 轉文字：%s（原檔 %s） %s" % (tag, said or "（聽不出內容）", path, caption), date)
                except Exception as e:
                    log("transcribe failed: %r" % e)
                    deliver(cfg, "%s %s（轉文字失敗：%s） %s" % (tag, path, e, caption), date)
            threading.Thread(target=work, daemon=True).start()
            return
        deliver(cfg, "%s %s %s" % (tag, path, text), m.get("date"))
        return
    if not text:
        send(cfg, "這種訊息目前收不到，請改傳文字、圖片、檔案、影片或語音。")
        return
    deliver(cfg, text, m.get("date"))


def bridge():
    mutex = k32.CreateMutexW(None, False, MUTEX)        # held for the life of this process
    if not mutex or ctypes.get_last_error() == 183:    # ERROR_ALREADY_EXISTS
        if mutex:
            k32.CloseHandle(mutex)
        log("bridge already running; exit")
        return
    log("bridge start")
    threading.Thread(target=typing_loop, daemon=True).start()
    try:
        cfg = load_cfg()
        tg(cfg, "setMyCommands", commands=json.dumps([{"command": c, "description": d} for c, d in COMMANDS],
                                                      ensure_ascii=False))
    except Exception as e:
        log("setMyCommands: %r" % e)
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


def handoff_guard():
    """remote setup: if the setup window is gone but the installer was never signed out, sign out now"""
    data = load_json(HANDOFF_MARK, None)
    if not isinstance(data, dict) or not data.get("handoff"):
        return
    pid = data.get("pid")
    info = process_info(int(pid)) if pid else None
    written = os.path.getmtime(HANDOFF_MARK)
    if info and os.path.basename(info[0]).lower() == "powershell.exe" and info[1] <= written + 2:
        return                                          # the setup window is still open; it signs out by itself
    if time.time() - float(data.get("tried", 0)) < 50:
        return                                          # a sign-out started less than a minute ago
    data["tried"] = time.time()
    save_json(HANDOFF_MARK, data)
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-File", data["handoff"]], creationflags=CREATE_NO_WINDOW)
    log("setup window closed before sign-out: running handoff", WATCHDOG_LOG)


# ---------------------------------------------------------------- reminders (fired by the watchdog, minute precision)
class reminders_locked:
    """cross-process lock around reminders.json (the AI's remind add vs. the watchdog firing)"""
    def __enter__(self):
        self.h = k32.CreateMutexW(None, False, "Local\\agent-reminders")
        if self.h:
            k32.WaitForSingleObject(self.h, 30000)
        return self

    def __exit__(self, *exc):
        if self.h:
            k32.ReleaseMutex(self.h)
            k32.CloseHandle(self.h)


def next_fire(item, after):
    """next time (epoch) after `after` for a reminder: once / daily / weekly(days 1=Mon..7=Sun)"""
    import datetime as dt
    if item["kind"] == "once":
        return item["at"] if item["at"] > after else None
    hh, mm = (int(x) for x in item["time"].split(":"))
    base = dt.datetime.fromtimestamp(after).replace(second=0, microsecond=0)
    for add in range(0, 8):
        day = base + dt.timedelta(days=add)
        cand = day.replace(hour=hh, minute=mm)
        if cand.timestamp() <= after:
            continue
        if item["kind"] == "daily" or cand.isoweekday() in item.get("days", []):
            return cand.timestamp()
    return None


def remind_add(args):
    with reminders_locked():
        return _remind_add(args)


def _remind_add(args):
    import datetime as dt
    items = load_json(REMINDERS, {"items": []})["items"]
    now = time.time()
    if args[0] == "--daily":
        item = {"kind": "daily", "time": args[1], "text": " ".join(args[2:])}
    elif args[0] == "--weekly":
        item = {"kind": "weekly", "days": [int(d) for d in args[1].split(",")], "time": args[2], "text": " ".join(args[3:])}
    elif args[0] == "--once":
        at = dt.datetime.strptime(args[1], "%Y-%m-%d %H:%M").timestamp()
        item = {"kind": "once", "at": at, "text": " ".join(args[2:])}
    elif args[0] == "--in":
        item = {"kind": "once", "at": now + float(args[1]) * 60, "text": " ".join(args[2:])}
    else:
        raise RuntimeError("use --daily HH:MM | --weekly 1,3,5 HH:MM | --once 'YYYY-MM-DD HH:MM' | --in MINUTES")
    if not item["text"].strip():
        raise RuntimeError("reminder text is empty")
    if "time" in item:
        hh, mm = (int(x) for x in item["time"].split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise RuntimeError("bad time " + item["time"])
    item["id"] = "r%d" % (max([int(i["id"][1:]) for i in items] or [0]) + 1)
    item["next"] = next_fire(item, now)
    if item["next"] is None:
        raise RuntimeError("that time is already past")
    items.append(item)
    save_json(REMINDERS, {"items": items})
    return item


def remind_text(item):
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["next"]))
    rule = {"daily": "每天 %s" % item.get("time"), "once": "一次",
            "weekly": "每週%s %s" % ("、".join("一二三四五六日"[d - 1] for d in item.get("days", [])), item.get("time"))}[item["kind"]]
    return "%s｜%s｜下次 %s｜%s" % (item["id"], rule, when, item["text"])


def fire_reminders(cfg):
    with reminders_locked():
        _fire_reminders(cfg)


def _fire_reminders(cfg):
    data = load_json(REMINDERS, None)
    if not isinstance(data, dict) or not data.get("items"):
        return
    now, keep, changed = time.time(), [], False
    for item in data["items"]:
        if item.get("next") and item["next"] <= now:
            changed = True
            if now - item["next"] <= 6 * 3600:          # the computer was on (or came back soon enough)
                try:
                    if agent_pid():
                        inject(cfg, "[提醒] " + item["text"])
                    elif cfg.get("token"):
                        send(cfg, "⏰ 提醒：" + item["text"])
                    log("reminder %s fired" % item["id"], WATCHDOG_LOG)
                except Exception as e:
                    log("reminder %s failed: %r" % (item["id"], e), WATCHDOG_LOG)
            item["next"] = next_fire(item, now)
            if item["next"] is None:
                continue                                # one-off: done
        keep.append(item)
    if changed:
        save_json(REMINDERS, {"items": keep})


def watchdog():
    try:
        handoff_guard()                                 # runs even while paused
    except Exception:
        log("handoff guard: " + traceback.format_exc(), WATCHDOG_LOG)
    if os.path.exists(PAUSED):
        return
    cfg = load_cfg()
    try:
        fire_reminders(cfg)
    except Exception:
        log("reminders: " + traceback.format_exc(), WATCHDOG_LOG)
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


def restore_keys(target, backup, keys):
    """put the listed keys (paths split by |) back to their value in the pre-install backup, or remove them"""
    if not os.path.exists(target):
        return 0
    data = load_json(target, None)
    if not isinstance(data, dict):
        return 0
    before = load_json(backup, {}) if os.path.exists(backup) else {}
    if not isinstance(before, dict):
        before = {}
    changed = 0
    for path in keys:
        parts = path.split("|")
        dst, src = data, before
        for p in parts[:-1]:
            dst = dst.get(p) if isinstance(dst, dict) else None
            src = src.get(p) if isinstance(src, dict) else None
        if not isinstance(dst, dict):
            continue
        last = parts[-1]
        if isinstance(src, dict) and last in src:
            if dst.get(last) != src[last]:
                dst[last], changed = src[last], changed + 1
        elif last in dst:
            del dst[last]
            changed += 1
    if changed:
        save_json(target, data)
    return changed


# ---------------------------------------------------------------- Claude sign-in on another device
def manual_login_url(url):
    """Claude's browser sign-in link -> its manual twin (same PKCE challenge and state; the page shows a code)"""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    redirect = dict(query).get("redirect_uri", "")
    if "/oauth/authorize" not in parts.path or not redirect.startswith("http://localhost:"):
        return None
    query = [(k, MANUAL_REDIRECT if k == "redirect_uri" else v) for k, v in query]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query, quote_via=urllib.parse.quote)))


def login_url(url):
    manual = manual_login_url(url)
    if not manual:
        log("login hook: not a Claude sign-in link, ignored")
        return 0
    qr = os.path.join(BASE, "login-qr.png")
    try:
        import segno
        segno.make(manual, error="m").save(qr, scale=8, border=4)
    except Exception as e:
        log("login hook: QR failed: %r" % e)
        qr = None
    cfg = load_json(CFG, {}) or {}
    if cfg.get("token") and cfg.get("owner_id"):
        sent = False
        if qr:
            try:
                send_photo(cfg, qr, "Claude 安裝登入：請交給安裝人員，用他自己的手機掃描並按「授權」。"
                                    "授權後網頁會顯示一串代碼，把代碼貼到電腦上「Paste code here if prompted」那一行，再按 Enter。")
                sent = True
            except Exception as e:
                log("login hook: QR photo failed: %r" % e)
        try:
            send(cfg, "同一個登入網址（QR 掃不到時用）：\n" + manual)
            sent = True
        except Exception as e:
            log("login hook: link message failed: %r" % e)
        if sent:
            log("login hook: sign-in link sent to Telegram")
            return 0
    # no Telegram: show the QR in a window of its own
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "show-qr", manual], creationflags=CREATE_NEW_CONSOLE)
    log("login hook: QR window opened")
    return 0


def show_qr(url):
    try:
        import segno
        segno.make(url, error="m").terminal(compact=True)
    except Exception:
        pass
    print("\n" + url + "\n")
    print("用手機掃描上面的 QR code（或打開這個網址）並授權，再把網頁上的代碼貼回安裝視窗。")
    input("看完按 Enter 關閉這個視窗")


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
    # restore-keys: back to the backup's value, or removed when the backup never had it
    save_json(t, {"a": 1, "added": 2, "nest": {"x": 9, "mine": 3}, "keep": 5})
    save_json(p, {"a": 0, "nest": {"x": 1}, "keep": 5})
    assert restore_keys(t, p, ["a", "added", "nest|x", "nest|mine", "missing|deep"]) == 4
    assert load_json(t, None) == {"a": 0, "nest": {"x": 1}, "keep": 5}, load_json(t, None)
    os.remove(p)
    assert restore_keys(t, p, ["keep"]) == 1 and load_json(t, None) == {"a": 0, "nest": {"x": 1}}
    shutil.rmtree(d)
    auto = ("https://claude.ai/oauth/authorize?code=true&client_id=abc&response_type=code"
            "&redirect_uri=http%3A%2F%2Flocalhost%3A54545%2Fcallback&scope=user%3Ainference+user%3Aprofile"
            "&code_challenge=XYZ&code_challenge_method=S256&state=ST")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(manual_login_url(auto)).query))
    assert q["redirect_uri"] == MANUAL_REDIRECT and q["code_challenge"] == "XYZ" and q["state"] == "ST", q
    assert q["scope"] == "user:inference user:profile", q
    assert manual_login_url("https://example.com/page") is None
    # reminders: schedule math and the add/list/remove file round trip (on a temp file)
    import datetime as dt
    monday9 = dt.datetime(2026, 11, 2, 9, 0).timestamp()               # a Monday
    assert next_fire({"kind": "daily", "time": "08:00"}, monday9) == dt.datetime(2026, 11, 3, 8, 0).timestamp()
    assert next_fire({"kind": "daily", "time": "10:00"}, monday9) == dt.datetime(2026, 11, 2, 10, 0).timestamp()
    assert next_fire({"kind": "weekly", "days": [3], "time": "08:00"}, monday9) == dt.datetime(2026, 11, 4, 8, 0).timestamp()
    assert next_fire({"kind": "weekly", "days": [1], "time": "08:00"}, monday9) == dt.datetime(2026, 11, 9, 8, 0).timestamp()
    assert next_fire({"kind": "once", "at": monday9}, monday9 + 1) is None
    global REMINDERS
    saved, REMINDERS = REMINDERS, os.path.join(tempfile.mkdtemp(), "reminders.json")
    try:
        a = remind_add(["--daily", "07:30", "喝水"])
        b = remind_add(["--weekly", "1,5", "21:00", "倒垃圾"])
        c = remind_add(["--in", "5", "看", "爐子"])
        assert [a["id"], b["id"], c["id"]] == ["r1", "r2", "r3"] and c["text"] == "看 爐子"
        assert "每週一、五 21:00" in remind_text(b)
        for bad in (["--daily", "25:00", "x"], ["--once", "2000-01-01 00:00", "x"], ["--daily", "08:00"]):
            try:
                remind_add(bad)
                raise AssertionError("accepted " + repr(bad))
            except RuntimeError:
                pass
    finally:
        shutil.rmtree(os.path.dirname(REMINDERS))
        REMINDERS = saved
    print("selftest ok")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "bridge"
    if cmd == "bridge":
        bridge()
    elif cmd == "send":
        cfg = load_cfg()
        args = argv[2:]
        if len(args) > 1 and args[0] in ("--photo", "--doc", "--video"):
            send_media(cfg, {"--photo": "photo", "--doc": "document", "--video": "video"}[args[0]], args[1], " ".join(args[2:]))
        else:
            if len(args) > 1 and args[0] in ("--text-file", "--file"):
                with open(args[1], encoding="utf-8-sig") as f:
                    text = f.read()
            elif args == ["-"]:
                text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
            else:
                text = " ".join(args)
            send(cfg, text)
        print("sent")
    elif cmd == "remind":
        sub, args = (argv[2] if len(argv) > 2 else "list"), argv[3:]
        if sub == "add":
            print("added " + remind_text(remind_add(args)))
        elif sub in ("del", "remove", "rm"):
            with reminders_locked():
                items = load_json(REMINDERS, {"items": []})["items"]
                left = [i for i in items if i["id"] not in args]
                save_json(REMINDERS, {"items": left})
            print("removed %d" % (len(items) - len(left)))
        else:
            items = load_json(REMINDERS, {"items": []})["items"]
            print("\n".join(remind_text(i) for i in items) or "（沒有提醒）")
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
    elif cmd == "login-url":
        return login_url(argv[2] if len(argv) > 2 else "")
    elif cmd == "show-qr":
        show_qr(argv[2])
    elif cmd == "merge-json":
        merge_json(argv[2], argv[3])
        print("merged " + argv[2])
    elif cmd == "restore-keys":
        print("restored %d key(s) in %s" % (restore_keys(argv[2], argv[3], argv[4:]), argv[2]))
    elif cmd == "selftest":
        selftest()
    else:
        print(__doc__ or "usage: see the top of tgterm.py")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
