# tgterm: Telegram <-> the agent's terminal (works the same for Claude Code / Codex / Gemini CLI)
#   pythonw tgterm.py bridge              forward the owner's Telegram messages into the running agent terminal
#   python  tgterm.py send "text"         reply to the owner on Telegram   (also: send --file <utf-8 file>, send - for stdin;
#                                         send --photo|--doc|--video <path> [caption]); goes to the owner's current topic
#   python  tgterm.py inject "text"       type text into the agent terminal and press Enter (test; --mode console|paste)
#   python  tgterm.py status              bridge / agent process / agent window check
#   python  tgterm.py close-window        close the agent window like a user would (watchdog test)
#   pythonw tgterm.py watchdog            reopen the agent window / restart the bridge if gone (Task Scheduler, every minute)
#   python  tgterm.py merge-json <target.json> <patch.json>   deep-merge a patch into a JSON file (null deletes a key)
#   agent-login-url.exe <url>             BROWSER hook for `claude auth login`: no browser on this computer; the sign-in
#                                         link goes out as a QR code on Telegram, the helper signs in on their own phone
#                                         and the code shown there is pasted back into the terminal
#   python  tgterm.py selftest            offline checks only; touches no window, console or network
#   python  tgterm.py update              update the helper files in place (files the owner changed are kept, the new
#                                         version goes beside them as <file>.new); --check only looks, --rollback undoes
#                                         the last update. Options: --source <folder|url> --manifest <file> --telegram
#                                         --no-restart --no-notify (tests: no restarts, no Telegram, nothing typed)
#   python  tgterm.py render-rules --config C --template T --out O   the rules sections the installer manages
#   python  tgterm.py rules-record --rules R --installed I            remember each managed section's hash
# AGENT_SETUP_AGENT_DIR=<folder> points at another install folder (update.ps1 runs a downloaded tgterm.py this way).
# Typing into the terminal (config "mode"):
#   console  write key events into the agent console's input buffer (WriteConsoleInputW): no focus change, no clipboard;
#            line breaks kept (one bracketed paste, then Enter twice); Codex gets one line
#   paste    winject style: bring the agent window forward, paste one line with Ctrl+V, press Enter, hand focus back
# Telegram extras (config keys; the owner flips them with /stream and /topics):
#   stream   default off: `send` first grows the reply in a draft bubble (sendMessageDraft, ~2.5 s), then sends it
#   topics   default on: replies go to the private-chat topic the owner wrote in last (topic mode is turned on by the
#            owner in @BotFather; without it, or outside a topic, nothing changes)
# Password vault: /vault (/密碼) goes to vault.py beside this file and never to the AI, a log or the inbox; the bridge
# also receives the ✅ / ❌ taps (callback queries) that `vault.py fill` asks for and records them with vault.decide().
import ctypes, hashlib, json, os, re, shutil, subprocess, sys, threading, time, traceback, urllib.error, urllib.parse
import urllib.request, uuid
from ctypes import wintypes

BASE = os.path.abspath(os.environ.get("AGENT_SETUP_AGENT_DIR") or os.path.dirname(os.path.abspath(__file__)))
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
THREAD = os.path.join(BASE, "tg-thread.json")          # the topic of the owner's latest message (private-chat topics)
TASK = "agent-watchdog"                                # the scheduled task bootstrap.ps1 registers
LOGON_MARK = os.path.join(BASE, "logon-trigger")       # "ok …" once the task has its "at sign-in" trigger
KEPT_DIFF = os.path.join(BASE, "update-kept.md")       # the last update's skipped files: what differs, for the AI
MUTEX = "Local\\agent-tgterm-bridge"
# Claude Code's manual sign-in redirect (MANUAL_REDIRECT_URL in its source): the page shows a code to paste back
MANUAL_REDIRECT = "https://platform.claude.com/oauth/code/callback"
DETACHED_PROCESS, CREATE_NEW_CONSOLE, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW = 0x8, 0x10, 0x200, 0x08000000


def log(msg, path=None):
    path = path or LOG                                  # looked up now, so the selftest can point it at a temp file
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
def tg(cfg, method, _timeout=70, **params):
    if not cfg.get("token"):
        raise RuntimeError("Telegram is not set up (no token in config.json)")
    data = urllib.parse.urlencode(params).encode()
    url = "https://api.telegram.org/bot%s/%s" % (cfg["token"], method)
    with urllib.request.urlopen(url, data=data, timeout=_timeout) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError("telegram %s: %s" % (method, res.get("description")))
    return res


def http_error_text(e):
    """Telegram's own reason in an HTTP error ("Bad Request: message thread not found"...)"""
    try:
        return json.loads(e.read().decode("utf-8", "replace")).get("description") or str(e)
    except Exception:
        return str(e)


def mark_sent():
    # the bridge stops showing "typing…" once a reply has gone out
    try:
        with open(LAST_SENT, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


# config keys the owner flips from Telegram (/stream, /topics) -> value when the key is missing
TOGGLES = {"stream": False, "topics": True}            # stream is opt-in: 11-04 a phone app hung around a draft
ON_WORDS, OFF_WORDS = ("開", "開啟", "on"), ("關", "關閉", "off")


def toggle_on(cfg, name):
    v = cfg.get(name)
    return TOGGLES[name] if v is None else bool(v)


def set_toggle(cfg, name, arg=""):
    """flip a TOGGLES key in config.json (or set it: 開/關/on/off); cfg gets the new value too -> new value"""
    disk = load_cfg()
    arg = (arg or "").strip().lower()
    on = True if arg in ON_WORDS else False if arg in OFF_WORDS else not toggle_on(disk, name)
    disk[name] = cfg[name] = on
    save_json(CFG, disk)
    return on


# ---------------------------------------------------------------- Telegram topics (private chats, Bot API 9.3+)
# Topic mode is the owner's choice in @BotFather; nothing here creates topics. The bridge keeps the topic of the
# owner's latest message in THREAD, and every reply (also from the separate `tgterm.py send` process) goes there.
def thread_of(m):
    t = m.get("message_thread_id")
    return t if type(t) is int and t > 0 else None


def thread_locked():
    """one process at a time on THREAD: the bridge rewrites it while `send` processes read it (on Windows a file open
    for reading cannot be replaced), and forgetting a refused topic must never delete a newer one"""
    return named_lock("Local\\agent-tg-thread", 5000)


def _saved_thread():
    data = load_json(THREAD, None)
    return thread_of(data) if isinstance(data, dict) else None


def saved_thread():
    with thread_locked():
        return _saved_thread()


def remember_thread(m):
    """the topic of the owner's message, or none (written only when it changes)"""
    thread = thread_of(m)
    with thread_locked():
        if thread == _saved_thread():
            return
        try:
            if thread:
                save_json(THREAD, {"message_thread_id": thread, "since": time.strftime("%Y-%m-%d %H:%M:%S")})
            else:
                os.remove(THREAD)
        except OSError as e:
            log("topic not saved: %r" % e)


def forget_thread(thread):
    with thread_locked():
        if _saved_thread() == thread:                   # a newer topic the bridge just wrote stays
            try:
                os.remove(THREAD)
            except OSError:
                pass


def reply_thread(cfg):
    """the topic replies go to; None = no topic (topics off, or the owner's last message was not in a topic)"""
    return saved_thread() if toggle_on(cfg, "topics") else None


def in_thread(thread, fn, *args, **params):
    """fn(..., message_thread_id=thread) when there is a topic. A topic Telegram refuses (deleted, or topic mode turned
    off in @BotFather) must not lose the reply: it goes out without the topic and the topic is forgotten.
    -> (fn's result, the topic actually used)"""
    if thread:
        try:
            return fn(*args, message_thread_id=thread, **params), thread
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            why = http_error_text(e)
        res = fn(*args, **params)
        forget_thread(thread)
        log("topic %s refused (%s): sent without it" % (thread, why))
        return res, None
    return fn(*args, **params), None


def bot_topics(cfg):
    """topic mode for this bot in @BotFather (getMe has_topics_enabled); None when Telegram cannot be asked"""
    try:
        return bool(tg(cfg, "getMe", _timeout=15)["result"].get("has_topics_enabled"))
    except Exception as e:
        log("getMe: %r" % e)
        return None


# ---------------------------------------------------------------- streamed replies (sendMessageDraft, Bot API 9.3+)
# times in seconds: "Thinking…" alone, the whole animation, least time left worth one more draft, hard cap
DRAFT_STEPS, DRAFT_THINK, DRAFT_SECONDS, DRAFT_MIN_CALL, DRAFT_LIMIT = 20, 0.4, 2.5, 0.5, 4.0


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2           # Telegram counts text length in UTF-16 units


def draft_cuts(n, steps=DRAFT_STEPS):
    """prefix lengths the draft grows through: at most `steps`, about even, the last one the whole text"""
    if n <= 0:
        return []
    size = -(-n // max(1, min(steps, n // 3)))
    return list(range(size, n, size)) + [n]


def cuts_due(elapsed, count, seconds=DRAFT_SECONDS, think=DRAFT_THINK):
    """how many cuts should be on screen `elapsed` s after "Thinking…": the first at `think`, the last at `seconds`"""
    if count <= 0 or elapsed < think:
        return 0
    if count == 1 or elapsed >= seconds:
        return count
    return min(count, 1 + int((elapsed - think) * (count - 1) / (seconds - think) + 1e-9))


def cut_time(k, count, seconds=DRAFT_SECONDS, think=DRAFT_THINK):
    """when cut k (1-based) is due, in seconds after "Thinking…" """
    return think if count <= 1 else think + (seconds - think) * (k - 1) / (count - 1)


def draft_poster(cfg, thread):
    """post(text, timeout): one sendMessageDraft for this reply, in the same chat and topic as the message after it"""
    did = int(time.time() * 1000) % 2000000000 or 1         # non-zero; one id per reply, so its updates animate
    where = {"message_thread_id": thread} if thread else {}

    def post(part, timeout):
        tg(cfg, "sendMessageDraft", _timeout=timeout, chat_id=cfg["owner_id"], draft_id=did, text=part, **where)
        if not part:
            mark_sent()                                 # "Thinking…" takes over from "typing…"
    return post


def stream_draft(cfg, text, thread=None, post=None, scale=1.0):
    """show the reply growing in a draft bubble before the real message: "Thinking…", then longer and longer prefixes
    for about DRAFT_SECONDS. Drafts are an ephemeral preview: send() still sends the message itself, right after.
    A slow network skips steps instead of stretching the animation, nothing here holds the message back longer than
    DRAFT_LIMIT, and any failure just ends the preview quietly. post / scale are for the selftest.
    -> number of drafts shown"""
    cuts = draft_cuts(len(text))
    if not cuts or utf16_len(text) > 4096:
        return 0
    think, seconds, min_call, limit = (x * scale for x in (DRAFT_THINK, DRAFT_SECONDS, DRAFT_MIN_CALL, DRAFT_LIMIT))
    post = post or draft_poster(cfg, thread)
    start, shown, calls = time.monotonic(), 0, 0
    try:
        post("", limit)                                 # empty text = the "Thinking…" placeholder
        calls = 1
        while shown < len(cuts):
            elapsed = time.monotonic() - start
            if limit - elapsed < min_call:              # the network is too slow today: send the message now
                break
            due = cuts_due(elapsed, len(cuts), seconds, think)
            if due <= shown:                            # early: wait for the next cut (its time counts from start)
                wake = min(cut_time(shown + 1, len(cuts), seconds, think), limit - min_call)
                time.sleep(max(0.001, wake - elapsed))
                continue
            post(text[:cuts[due - 1]], limit - elapsed)  # late cuts are skipped, the animation keeps its length
            shown, calls = due, calls + 1
    except Exception as e:
        why = http_error_text(e) if isinstance(e, urllib.error.HTTPError) else repr(e)
        log("draft stopped after %d: %s" % (calls, why))
    return calls


def send(cfg, text, stream=False):
    text = (text or "").strip() or "（空白）"
    if not cfg.get("owner_id"):
        raise RuntimeError("owner_id missing in config.json")
    thread = reply_thread(cfg)
    drafts = stream_draft(cfg, text, thread) if stream else 0
    for i in range(0, len(text), 3900):
        _, thread = in_thread(thread, tg, cfg, "sendMessage", chat_id=cfg["owner_id"], text=text[i:i + 3900])
    mark_sent()
    log("sent %d chars%s%s" % (len(text), " in topic %s" % thread if thread else "",
                               " after %d drafts" % drafts if drafts else ""))


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
    with open(path, "rb") as f:
        data = f.read()
    filename = os.path.basename(path).replace('"', "_")
    _, thread = in_thread(reply_thread(cfg), upload, cfg, method, kind, filename, data, caption)
    mark_sent()
    log("sent %s %s (%d bytes)%s" % (kind, filename, size, " in topic %s" % thread if thread else ""))


def upload(cfg, method, kind, filename, data, caption, message_thread_id=None):
    """one multipart sendPhoto / sendDocument / sendVideo"""
    boundary = "----agentsetup" + uuid.uuid4().hex
    fields = [("chat_id", str(cfg["owner_id"])), ("caption", caption[:1000])]
    if message_thread_id:
        fields.append(("message_thread_id", str(message_thread_id)))
    body = b""
    for name, value in fields:
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, name, value)).encode("utf-8")
    body += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
             'Content-Type: application/octet-stream\r\n\r\n' % (boundary, kind, filename)).encode("utf-8") + data + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode("utf-8")
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (cfg["token"], method), data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=300) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError("telegram %s: %s" % (method, res.get("description")))
    return res


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


class AlreadyTyped(RuntimeError):
    """keys already reached the agent's input box: the caller must not type the text again (winject checks the note)"""
    def __init__(self, msg):
        super().__init__(msg + " (keys were already typed)")


def _key(ch="", vk=0, scan=0):
    recs = []
    for down in (True, False):
        r = INPUT_RECORD()
        r.EventType = KEY_EVENT
        r.Event.bKeyDown = down
        r.Event.wRepeatCount = 1
        r.Event.wVirtualKeyCode = vk
        r.Event.wVirtualScanCode = scan
        r.Event.UnicodeChar = ch
        recs.append(r)
    return recs


def enter_records():
    return _key("\r", VK_RETURN, SCAN_RETURN)


def _chars(s):
    recs = []
    units = s.encode("utf-16-le")
    for i in range(0, len(units), 2):
        recs += _key(units[i:i + 2].decode("utf-16-le", errors="surrogatepass"))
    return recs


def clean_lines(text):
    """line breaks become \\n and tabs stay; every other control character (ESC included) becomes a space"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return "".join(ch if ch in "\t\n" or not (ch < " " or "\x7f" <= ch <= "\x9f") else " " for ch in text)


PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"
OLD_STYLE = ("codex", "gemini")                        # bracketed paste verified on Claude Code only (11-02)
END_RECS = 2 * len(PASTE_END)                           # records of the closing marker, written in one piece


def records_for(text, kind=None):
    """key records for the text. Claude Code: one bracketed paste, each line break an Enter inside it
    (11-02: typed text was taken as a paste and the Enter after it swallowed). Codex on Windows has no bracketed
    paste (ESC would reach it as a key) and Antigravity was never tested with it: one line, as before."""
    if kind in OLD_STYLE:
        return _chars(one_line(text))
    recs = _key(PASTE_START[0]) + _chars(PASTE_START[1:])
    for i, line in enumerate(clean_lines(text).split("\n")):
        if i:
            recs += enter_records()
        recs += _chars(line)
    return recs + _key(PASTE_END[0]) + _chars(PASTE_END[1:])


def _chunks(recs, size=256):
    """write-sized pieces; the last END_RECS records never straddle two writes (a lone ESC would act as a key)"""
    i, n = 0, len(recs)
    while i < n:
        j = min(i + size, n)
        if n - END_RECS < j < n:
            j = n - END_RECS
        yield recs[i:j]
        i = j


def write_console(pid, recs):
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        raise OSError("AttachConsole(%s) failed: %s" % (pid, ctypes.get_last_error()))
    try:
        h = k32.CreateFileW("CONIN$", 0xC0000000, 0x3, None, 3, 0, None)  # GENERIC_READ|WRITE, SHARE_READ|WRITE, OPEN_EXISTING
        if not h or h == INVALID_HANDLE:
            raise OSError("open CONIN$ failed: %s" % ctypes.get_last_error())
        try:
            done = 0
            for chunk in _chunks(recs):
                arr = (INPUT_RECORD * len(chunk))(*chunk)
                n = DWORD(0)
                if not k32.WriteConsoleInputW(h, arr, len(chunk), ctypes.byref(n)) or n.value != len(chunk):
                    err = "WriteConsoleInputW failed: %s" % ctypes.get_last_error()
                    raise AlreadyTyped(err) if done or n.value else OSError(err)
                done += len(chunk)
        finally:
            k32.CloseHandle(h)
    finally:
        k32.FreeConsole()


def inject_console(text, kind=None):
    pid = agent_pid()
    if not pid:
        raise RuntimeError("agent terminal is not running")
    recs = records_for(text, kind)
    write_console(pid, recs)
    try:
        if kind in OLD_STYLE:
            time.sleep(0.6)                             # let the TUI take the text before Enter
            write_console(pid, enter_records())
            return "console pid %s" % pid
        time.sleep(1.5 + min(len(recs), 40000) / 20000)   # let the TUI finish taking the paste before Enter
        write_console(pid, enter_records())
    except AlreadyTyped:
        raise
    except Exception as e:
        raise AlreadyTyped("Enter failed after the text was typed: %s" % e)
    time.sleep(1.0)
    try:
        write_console(pid, enter_records())             # covers a swallowed first Enter; on an empty prompt it does nothing
    except Exception as e:
        return "console pid %s (second Enter failed: %s)" % (pid, e)
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


def with_winsend_lock(fn, *args):
    # share winsend.py / winject's lock so a LINE or window send and our typing never interleave
    mutex = k32.CreateMutexW(None, False, "Local\\agent-winsend")
    if mutex and k32.WaitForSingleObject(mutex, 60000) in (0, 0x80):      # signaled or abandoned
        try:
            return fn(*args)
        finally:
            k32.ReleaseMutex(mutex)
            k32.CloseHandle(mutex)
    if mutex:
        k32.CloseHandle(mutex)
    raise RuntimeError("another window send is still running")


def inject_paste(title, text):
    return with_winsend_lock(_inject_paste, title, text)


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
    write_console(pid, _key("\x1b", VK_ESCAPE, 0x01))
    return "console pid %s" % pid


def one_line(text):
    # a newline would submit early; control characters would act as keys
    text = "".join(ch if ch >= " " or ch in "\t\n\r" else " " for ch in text)
    return " ".join(text.split())


def inject(cfg, text, mode=None):
    if not one_line(text):
        return "empty"
    mode = mode or cfg.get("mode", "console")
    if mode == "paste":
        return inject_paste(cfg.get("agent_name", ""), one_line(text))
    # console keeps line breaks (inside a bracketed paste; see records_for)
    return with_winsend_lock(inject_console, text, cfg.get("ai"))


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
            ("new", "重開並開始新對話"), ("screen", "傳目前的電腦畫面"),
            ("allow", "放行安全防護剛擋下的動作一次"), ("update", "更新輔助程式（/update 還原＝退回上一版）"),
            ("stream", "回覆逐字顯示 開／關（預設關）"), ("topics", "回在你傳訊息的主題 開／關"),
            ("vault", "密碼庫：存網站帳密給 AI 幫你登入，AI 看不到密碼（/密碼 看用法）"),
            ("help", "指令說明")]
HIDDEN_COMMANDS = ("rollback",)                         # not in the Telegram menu: /還原 or /update 還原
ALIASES = {"狀態": "status", "停": "stop", "停止": "stop", "重開": "restart", "新對話": "new",
           "畫面": "screen", "截圖": "screen", "允許": "allow", "放行": "allow", "說明": "help",
           "更新": "update", "還原": "rollback", "串流": "stream", "主題": "topics", "密碼": "vault", "密碼庫": "vault"}
# /vault /密碼 /密碼庫, also with a full-width slash or no space before the sub-command ("/密碼新增 …"): the text may
# hold a password, so it is matched loosely and handled before anything else (see vault_command)
VAULT_TEXT = re.compile(r"[/／]\s*(?:vault|密碼)", re.I)
ALLOWED_UPDATES = '["message","callback_query"]'        # callback_query: the vault's ✅ / ❌ buttons
# the owner made, renamed, closed... a topic: nothing for the AI, nothing to answer
TOPIC_EVENTS = ("forum_topic_created", "forum_topic_edited", "forum_topic_closed", "forum_topic_reopened",
                "general_forum_topic_hidden", "general_forum_topic_unhidden")
ROLLBACK_WORDS = ("還原", "退回", "rollback", "undo")
CHECK_WORDS = ("檢查", "check")
ALLOW = os.path.join(BASE, "allow-once")                # read and removed by guard.py


def last_blocked():
    try:
        with open(os.path.join(BASE, "guard.log"), encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if " BLOCKED " in l]
    except OSError:
        return ""
    return lines[-1].split(" BLOCKED ", 1)[1][:400] if lines else ""
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
            in_thread(reply_thread(cfg), tg, cfg, "sendChatAction", chat_id=cfg["owner_id"], action="typing")
        except Exception as e:
            log("typing: %r" % e)


_inject_lock = threading.Lock()                         # one typing into the AI window at a time (voice runs in threads)
_handling = threading.Lock()                            # held while received messages are handled
_jobs, _jobs_lock = [0], threading.Lock()               # voice / video work still running in threads
RESTART_BRIDGE = os.path.join(BASE, "bridge-restart")   # the updater asks the bridge to exit so the new code loads


def run_job(fn):
    with _jobs_lock:
        _jobs[0] += 1

    def run():
        try:
            fn()
        finally:
            with _jobs_lock:
                _jobs[0] -= 1
    threading.Thread(target=run, daemon=True).start()


def restart_watch():
    """exit once the updater asks and nothing is in progress; the updater (or the watchdog) starts the new code"""
    while True:
        time.sleep(2)
        if not os.path.exists(RESTART_BRIDGE) or _jobs[0] or not _handling.acquire(blocking=False):
            continue
        if _jobs[0] or not _inject_lock.acquire(blocking=False):
            _handling.release()
            continue
        try:
            os.remove(RESTART_BRIDGE)
        except OSError:
            pass
        log("bridge exit: restart asked by the updater")
        os._exit(0)


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


def command(cfg, name, arg=""):
    if name == "status":
        pid = agent_pid()
        sent = time.strftime("%m/%d %H:%M", time.localtime(os.path.getmtime(LAST_SENT))) if os.path.exists(LAST_SENT) else "還沒有"
        busy = "（還在處理你上一則訊息）" if _awaiting["since"] else ""
        send(cfg, "AI 視窗：%s%s\n最後一次回覆：%s\n自動重開：%s\n%s\n版本：%s" % (
            "運作中" if pid else "沒有開著（1 分鐘內會自動重開）", busy, sent,
            "暫停中" if os.path.exists(PAUSED) else "開啟", toggles_text(cfg), version_text()))
    elif name in TOGGLES:
        send(cfg, toggle_reply(cfg, name, set_toggle(cfg, name, arg)))
    elif name in ("update", "rollback"):
        if name == "rollback" or arg in ROLLBACK_WORDS:
            spawn_update("--rollback")
            send(cfg, "開始還原到更新前的版本，好了會通知你。")
        elif arg in CHECK_WORDS:
            spawn_update("--check")
        else:
            spawn_update()
            send(cfg, "開始更新（約 1～2 分鐘），好了會傳報告給你。你或 AI 改過的檔案不會被蓋掉。")
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
    elif name == "allow":
        with open(ALLOW, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        what = last_blocked()
        send(cfg, "已放行：AI 在 10 分鐘內可以做「一次」剛才被安全防護擋下的動作，它會馬上重試。" +
             ("\n最近一次被擋的是：%s" % what if what else "") +
             "\n不確定它要做什麼的話，先問清楚再放行。")
        deliver(cfg, "主人打了 /allow：同意放行剛才被安全防護擋下的那個動作（只放行一次，10 分鐘內有效）。"
                     "請現在重試那個動作，做完回報結果。")
    else:
        send(cfg, help_text())


def help_text():
    return ("直接傳訊息就是跟 AI 說話；圖片、檔案、影片、語音都可以傳。\n" +
            "\n".join("/%s  %s" % c for c in COMMANDS) +
            "\n/update 檢查  只看有沒有新版" +
            "\n/stream 開｜關、/topics 開｜關  直接指定（不加就是切換）" +
            "\n（也可以打中文：/狀態 /停 /重開 /新對話 /畫面 /允許 /更新 /還原 /串流 /主題 /密碼）")


def toggles_text(cfg):
    bot = {True: "（機器人已開主題模式）", False: "（機器人沒開主題模式，回覆都在同一個聊天室）", None: ""}
    return "逐字顯示：%s\n跟著主題回覆：%s%s" % ("開" if toggle_on(cfg, "stream") else "關",
                                         "開" if toggle_on(cfg, "topics") else "關",
                                         bot[bot_topics(cfg)] if toggle_on(cfg, "topics") else "")


def toggle_reply(cfg, name, on):
    if name == "stream":
        if on:
            return ("逐字顯示：已開啟。AI 的回覆會先出現「Thinking…」，再一段段長出來（約 2.5 秒），最後變成正式訊息。\n"
                    "如果 Telegram 因此卡住或怪怪的，打 /串流 關 就恢復原狀。")
        return "逐字顯示：已關閉，AI 的回覆會整則直接送出。要再開：/串流 開"
    if not on:
        return "跟著主題回覆：已關閉，AI 的回覆不分主題，一律送到同一個聊天室。要再開：/主題 開"
    bot = bot_topics(cfg)
    return "跟著主題回覆：已開啟，你在哪個主題傳訊息，AI 就回在哪個主題。" + (
        "\n（你的機器人還沒開主題模式，所以目前只有一個聊天室；想分主題，先到 @BotFather 幫機器人開啟主題模式）"
        if bot is False else "")


# ---------------------------------------------------------------- password vault (vault.py beside this file)
_vault = []


def load_vault():
    """vault.py, loaded once; the bridge only calls owner_command() and decide() (no Chrome, no getUpdates there)"""
    if not _vault:
        import importlib.util
        path = os.path.join(BASE, "vault.py")
        spec = importlib.util.spec_from_file_location("agent_vault", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)                    # FileNotFoundError when vault.py is not installed
        _vault.append(mod)
    return _vault[0]


def vault_command(cfg, m):
    """/vault (/密碼): the owner stores, lists and deletes logins. The text may hold a password: it goes to vault.py and
    nowhere else (not the AI, not a log, not the inbox), and a message that may hold one is deleted from the chat,
    even when it is malformed or vault.py cannot be loaded. Errors are logged by type only."""
    raw = m.get("text") or m.get("caption") or ""
    try:
        reply, drop = load_vault().owner_command(raw)
    except Exception as e:
        reply, drop = "密碼庫現在不能用（%s）。在 Telegram 打 /update 更新後再試。" % type(e).__name__, len(raw.split()) > 1
    deleted = None
    if drop:
        try:
            tg(cfg, "deleteMessage", chat_id=(m.get("chat") or {}).get("id"), message_id=m.get("message_id"))
            deleted = True
        except Exception as e:
            deleted = False
            log("vault: the owner's message was not deleted (%s)" % type(e).__name__)
        reply += ("\n🧹 你剛傳的那則已經刪掉（裡面可能有密碼）" if deleted else
                  "\n⚠️ 你剛傳的那則刪不掉，請自己長按刪除（裡面可能有密碼）")
    try:
        send(cfg, reply)
    except Exception as e:
        log("vault: reply failed (%s)" % type(e).__name__)
    log("vault command%s" % {True: " (message deleted)", False: " (message NOT deleted)", None: ""}[deleted])


def on_button(cfg, cq):
    """a tap on an inline button: the vault's ✅ / ❌ that `vault.py fill` sent. Only the owner's taps count; the
    answer is recorded only while the request still waits (vault.decide), then shown on the message"""
    frm, data, msg, note = cq.get("from") or {}, str(cq.get("data") or ""), cq.get("message") or {}, ""
    verdict, changed = "", False
    try:
        if frm.get("id") != cfg.get("owner_id") or (msg.get("chat") or {}).get("id", frm.get("id")) != frm.get("id"):
            note = "只有主人能按"
            log("ignored button from %s" % frm.get("id"))
        elif data.startswith("vault:"):
            _, req, verdict = (data.split(":") + ["", ""])[:3]
            changed, note = load_vault().decide(req, verdict)
            log("vault button %s: %s" % (verdict if verdict in ("ok", "no") else "?", "recorded" if changed else "too late"))
        else:
            note = "這個按鈕已經沒有作用了"
    except Exception as e:
        note = "出錯了（%s）" % type(e).__name__
        log("button failed: %r" % e)
    try:
        tg(cfg, "answerCallbackQuery", callback_query_id=cq.get("id"), text=note[:190])
    except Exception as e:
        log("answerCallbackQuery: %r" % e)
    where = {"chat_id": (msg.get("chat") or {}).get("id"), "message_id": msg.get("message_id")}
    if not data.startswith("vault:") or not frm.get("id") == where["chat_id"] == cfg.get("owner_id") or not where["message_id"]:
        return
    try:
        if changed:                                     # the answer stays on the message, the buttons go away
            tg(cfg, "editMessageText", text=(msg.get("text") or "") + ("\n→ ✅ 已允許" if verdict == "ok" else "\n→ ❌ 已拒絕"),
               **where)
        else:                                           # a finished request: just take the buttons away
            tg(cfg, "editMessageReplyMarkup", **where)
    except Exception as e:
        log("button message not edited: %r" % e)


def dispatch(cfg, u):
    """one update from getUpdates"""
    if u.get("callback_query"):
        on_button(cfg, u["callback_query"])
    else:
        handle(cfg, u.get("message") or {})


def handle(cfg, m):
    chat, frm = m.get("chat") or {}, m.get("from") or {}
    if chat.get("type") != "private" or frm.get("id") != cfg.get("owner_id"):
        log("ignored message from %s" % frm.get("id"))
        return
    remember_thread(m)                                  # replies (also the AI's `send`) go to this message's topic
    if any(k in m for k in TOPIC_EVENTS):
        return
    text = (m.get("text") or m.get("caption") or "").strip()
    if VAULT_TEXT.match(text):                          # may hold a password: never to the AI, a log or the inbox
        vault_command(cfg, m)
        return
    if text.startswith("/"):
        parts = text[1:].split(None, 1)
        name = parts[0].split("@")[0] if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        name = ALIASES.get(name, name.lower())
        if name == "start":
            send(cfg, "已連線，直接傳訊息給我就好。輸入 /help 看遙控指令。")
            return
        if name in dict(COMMANDS) or name in HIDDEN_COMMANDS:
            try:
                command(cfg, name, arg)
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
            run_job(vwork)
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
            run_job(work)
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
    try:
        os.remove(RESTART_BRIDGE)                       # this process already runs the current code
    except OSError:
        pass
    threading.Thread(target=typing_loop, daemon=True).start()
    threading.Thread(target=restart_watch, daemon=True).start()
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
            res = tg(cfg, "getUpdates", offset=offset, timeout=50, allowed_updates=ALLOWED_UPDATES)
            with _handling:
                for u in res.get("result", []):
                    offset = u["update_id"] + 1
                    save_json(STATE, {"offset": offset})
                    dispatch(cfg, u)
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
    if not wt:
        # the Store alias is not always on PATH for the watchdog (11-03: a PC opened the AI in a plain console,
        # and the AI then renamed the window); it still lives here when Windows Terminal is installed
        alias = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps", "wt.exe")
        wt = alias if os.path.lexists(alias) else None
    if wt:
        # Windows Terminal keeps this title no matter what the AI prints
        try:
            subprocess.Popen([wt, "-w", "new", "--title", cfg.get("agent_name", "Agent"), "--suppressApplicationTitle", "-d", root] + ps)
            return "Windows Terminal"
        except OSError as e:
            log("wt.exe failed (%r): opening a plain console instead" % e, WATCHDOG_LOG)
    subprocess.Popen(ps, cwd=root, creationflags=CREATE_NEW_CONSOLE)
    return "console (no Windows Terminal: the AI may rename the window)"


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
class named_lock:
    """cross-process lock (a named mutex); after `wait` ms the caller goes on anyway"""
    def __init__(self, name, wait=30000):
        self.name, self.wait = name, wait

    def __enter__(self):
        self.h = k32.CreateMutexW(None, False, self.name)
        self.owned = bool(self.h) and k32.WaitForSingleObject(self.h, self.wait) in (0, 0x80)   # signaled / abandoned
        return self

    def __exit__(self, *exc):
        if self.h:
            if self.owned:
                k32.ReleaseMutex(self.h)
            k32.CloseHandle(self.h)


class reminders_locked(named_lock):
    """cross-process lock around reminders.json (the AI's remind add vs. the watchdog firing)"""
    def __init__(self):
        super().__init__("Local\\agent-reminders")


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


def task_xml_text(b):
    """schtasks /XML output or a task file: UTF-16 (with or without BOM), else UTF-8, else the ANSI code page"""
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16")
    if len(b) > 1 and b[1:2] == b"\x00":
        return b.decode("utf-16-le")
    for enc in ("utf-8-sig", "mbcs"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass
    return b.decode("utf-8", "replace")


def with_logon_trigger(xml):
    """task XML plus an "at sign-in" trigger for the task's own user that repeats every minute; None if it has one"""
    if "<LogonTrigger" in xml:
        return None
    if "</Triggers>" not in xml:
        raise ValueError("no <Triggers> in the task XML")
    m = re.search(r"<UserId>([^<]+)</UserId>", xml.split("<Principals>")[-1])
    user = m.group(1) if m else "%s\\%s" % (os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""))
    user = user.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") if not m else user
    trig = ("<LogonTrigger><Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd>"
            "</Repetition><Enabled>true</Enabled><UserId>%s</UserId><Delay>PT15S</Delay></LogonTrigger>" % user)
    xml = xml.replace("</Triggers>", trig + "</Triggers>", 1)
    return re.sub(r'encoding="[^"]*"', 'encoding="UTF-16"', xml, count=1)


def ensure_logon_trigger(task=TASK, run=subprocess.run):
    """the task used to have only its every-minute timer; after one restart that timer never fired again (2060,
    2026-11-05) and nothing reopened the AI window. An "at sign-in" trigger starts the watchdog at every sign-in.
    Done once per install (LOGON_MARK); a failure is retried after 6 hours, not every minute. schtasks only."""
    mark = load_text(LOGON_MARK).strip()
    if mark.startswith("ok"):
        return None
    if mark.startswith("fail "):
        try:
            if time.time() - float(mark.split()[1]) < 6 * 3600:
                return None
        except (IndexError, ValueError):
            pass
    try:
        out = add_logon_trigger(task, run)
    except Exception as e:                              # schtasks missing, timeout...: back off like any failure
        out = "error: %r" % e
    with open(LOGON_MARK, "w", encoding="utf-8") as f:
        f.write(("ok " if out.startswith("OK") else "fail %d " % time.time()) + out[:300])
    return out


def add_logon_trigger(task, run):
    q = run(["schtasks.exe", "/Query", "/TN", task, "/XML"], capture_output=True, timeout=60,
            creationflags=CREATE_NO_WINDOW)
    if q.returncode != 0:
        return "query failed (exit %d)" % q.returncode
    try:
        xml = with_logon_trigger(task_xml_text(q.stdout).strip().lstrip("﻿"))
    except ValueError as e:
        return str(e)
    if xml is None:
        return "OK: already there"
    tmp = os.path.join(BASE, "watchdog-task.xml")
    with open(tmp, "w", encoding="utf-16") as f:        # schtasks wants UTF-16 (the codec writes the BOM)
        f.write(xml)
    try:
        c = run(["schtasks.exe", "/Create", "/TN", task, "/XML", tmp, "/F"], capture_output=True, timeout=60,
                creationflags=CREATE_NO_WINDOW)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if c.returncode != 0:
        return "create failed (exit %d): %s" % (c.returncode, " ".join(task_xml_text(c.stdout + c.stderr).split())[:200])
    return "OK: added"


def watchdog():
    try:
        handoff_guard()                                 # runs even while paused
    except Exception:
        log("handoff guard: " + traceback.format_exc(), WATCHDOG_LOG)
    if os.path.exists(PAUSED):
        return
    try:
        out = ensure_logon_trigger()
        if out:
            log("logon trigger: " + out, WATCHDOG_LOG)
    except Exception:
        log("logon trigger: " + traceback.format_exc(), WATCHDOG_LOG)
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
        start_bridge()
        log("started Telegram bridge", WATCHDOG_LOG)
    wj = os.path.join(BASE, "winject", "cli.py")
    if os.path.exists(wj):                              # keep the winject send queue running
        try:
            py = os.path.join(os.path.dirname(sys.executable), "python.exe")
            r = subprocess.run([py if os.path.exists(py) else sys.executable, wj, "ensure-dispatcher"], cwd=BASE,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                               creationflags=CREATE_NO_WINDOW)
            if not (r.stdout or "").startswith("OK: dispatcher running"):
                log("winject: " + ((r.stdout or "") + (r.stderr or "")).strip()[:500], WATCHDOG_LOG)
        except Exception:
            log("winject: " + traceback.format_exc(), WATCHDOG_LOG)
    try:
        daily_update_check(cfg)
    except Exception as e:                              # offline, GitHub down...: try again tomorrow
        log("update check: %r" % e, WATCHDOG_LOG)


def pythonw_exe():
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pyw if os.path.exists(pyw) else sys.executable


def python_exe():
    py = os.path.join(os.path.dirname(sys.executable), "python.exe")
    return py if os.path.exists(py) else sys.executable


def start_bridge():
    env = {k: v for k, v in os.environ.items() if k != "AGENT_SETUP_AGENT_DIR"}
    subprocess.Popen([pythonw_exe(), os.path.join(BASE, "tgterm.py"), "bridge"], cwd=BASE, env=env,
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, close_fds=True)


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


# ---------------------------------------------------------------- rules sections (one template for installer and updater)
RULES_NAMES = {"claude": "CLAUDE.md", "codex": "AGENTS.md", "gemini": "GEMINI.md"}
TEMPLATE = os.path.join(BASE, "rules-sections.md")
ANSWERS = os.path.join(BASE, "answers.json")
BLOCK_RE = re.compile(r"<!--[ \t]*agent-setup:begin[ \t]+([\w-]+)[ \t]*-->(.*?)<!--[ \t]*agent-setup:end[ \t]+\1[ \t]*-->",
                      re.S)
SECTION_LINE = re.compile(r"^\{\{([#^])(\w+)\}\}\n(.*?)^\{\{/\2\}\}(?:\n|\Z)", re.M | re.S)
SECTION = re.compile(r"\{\{([#^])(\w+)\}\}(.*?)\{\{/\2\}\}", re.S)
VAR = re.compile(r"\{\{(\w+)\}\}")
LABELS = {"telegram": "Telegram", "reminders": "提醒與排程", "manuals": "說明書", "guard": "安全防護",
          "vault": "密碼庫", "first-meeting": "第一次見面"}


def fill(text, values):
    """{{name}} placeholders and {{#flag}}...{{/flag}} / {{^flag}}...{{/flag}} sections (no nesting); a section tag
    alone on its line takes the line with it; values go in last and are never read as template"""
    def section(m):
        return m.group(3) if bool(values.get(m.group(2))) == (m.group(1) == "#") else ""
    text = SECTION.sub(section, SECTION_LINE.sub(section, text))
    return VAR.sub(lambda m: "" if values.get(m.group(1)) is None else str(values[m.group(1)]), text)


def norm_body(text):
    return text.replace("\r\n", "\n").strip("\n")


def block_sha(body):
    return hashlib.sha256(norm_body(body).encode("utf-8")).hexdigest()


def block_text(name, body):
    return "<!-- agent-setup:begin %s -->\n%s\n<!-- agent-setup:end %s -->" % (name, norm_body(body), name)


def render_sections(template, values):
    """[(name, body)] in template order; text outside the markers is ignored"""
    return [(m.group(1), norm_body(fill(m.group(2), values))) for m in BLOCK_RE.finditer(template.replace("\r\n", "\n"))]


def sections_text(blocks):
    return "\n\n".join(block_text(n, b) for n, b in blocks) + "\n"


def rules_vars(cfg, answers=None):
    """what the template needs; installs from before the updater fall back to answers.json"""
    a = answers if isinstance(answers, dict) else {}
    values = {"user": a.get("user_name") or "主人", "bot": cfg.get("bot"), "tg": bool(cfg.get("token")),
              "agent": cfg.get("agent_name"), "lang": a.get("language") or "繁體中文"}
    values.update(cfg.get("rules_vars") or {})
    return values


def rules_path(cfg):
    name = RULES_NAMES.get(cfg.get("ai"))
    return os.path.join(cfg.get("root") or r"C:\AI", name) if name else None


def merge_rules(text, new, old, recorded):
    """refresh the managed sections of a rules file (LF text). A section is replaced only if nobody changed it since it
    was written: its hash was recorded then, or it equals what the installed template renders. -> (text, result)"""
    news, olds = dict(new), dict(old)
    res = {k: [] for k in ("replaced", "same", "kept", "added", "removed")}
    out, pos, present = [], 0, set()

    def pristine(name, body):
        return block_sha(body) == recorded.get(name) or (name in olds and body == olds[name])
    for m in BLOCK_RE.finditer(text):
        name, body = m.group(1), norm_body(m.group(2))
        present.add(name)
        out.append(text[pos:m.start()])
        pos = m.end()
        if name not in news and pristine(name, body):   # the new version no longer has this section
            res["removed"].append(name)
            out[-1] = out[-1].rstrip("\n")
            continue
        if name not in news:
            out.append(m.group(0))
        elif body == news[name]:
            res["same"].append(name)
            out.append(m.group(0))
        elif pristine(name, body):
            res["replaced"].append(name)
            out.append(block_text(name, news[name]))
        else:
            res["kept"].append(name)
            out.append(m.group(0))
    out.append(text[pos:])
    text = "".join(out)
    for name, body in new:
        if name in present:
            continue
        if name in recorded:                            # taken out here: offered again, not forced back
            res["kept"].append(name)
        else:
            res["added"].append(name)
            text = text.rstrip("\n") + "\n\n" + block_text(name, body) + "\n"
    return text, res


def recorded_after(res, new, recorded):
    """hashes of what the updater wrote; a section somebody changed keeps the old record so it is never overwritten"""
    news = dict(new)
    rec = {n: block_sha(news[n]) for n in res["replaced"] + res["same"] + res["added"]}
    rec.update({n: recorded[n] for n in res["kept"] if n in recorded})
    return rec


def cli_opts(args, names):
    o, i = {}, 0
    while i < len(args):
        if args[i] not in names or i + 1 >= len(args):
            raise SystemExit("unknown option or missing value: " + args[i])
        o[args[i][2:]] = args[i + 1]
        i += 2
    return o


def render_rules_cmd(args):
    o = cli_opts(args, ("--config", "--template", "--out"))
    cfg_path = o.get("config", CFG)
    cfg = load_json(cfg_path, None)
    if not isinstance(cfg, dict):
        raise SystemExit("config.json missing or invalid: " + cfg_path)
    answers = load_json(os.path.join(os.path.dirname(os.path.abspath(cfg_path)), "answers.json"), None)
    with open(o.get("template", TEMPLATE), encoding="utf-8-sig") as f:
        blocks = render_sections(f.read(), rules_vars(cfg, answers))
    if not blocks:
        raise SystemExit("no agent-setup sections in the template")
    if o.get("out"):
        write_text(o["out"], sections_text(blocks))
    else:
        sys.stdout.write(sections_text(blocks))


def rules_record_cmd(args):
    o = cli_opts(args, ("--rules", "--installed"))
    rules = o.get("rules") or rules_path(load_cfg())
    path = o.get("installed", INSTALLED)
    data = load_json(path, None)
    data = data if isinstance(data, dict) else {}
    data["rules_blocks"] = {m.group(1): block_sha(m.group(2)) for m in BLOCK_RE.finditer(read_text(rules)[0])}
    save_json(path, data)
    print("recorded %d rules section(s) in %s" % (len(data["rules_blocks"]), path))


# ---------------------------------------------------------------- update in place
# manifest.json on main: {"version": helpers commit, "files": {rel: sha256}, "known": {rel: [every sha256 it has had]},
#                         "requirements": [pip packages], "notes": [what changed]}; files come from raw/<version>/<rel>.
# A file is replaced only if it is unchanged since it was installed; otherwise the new one goes beside it as <file>.new.
RAW = "https://raw.githubusercontent.com/dead1786/agent-setup"
INSTALLED = os.path.join(BASE, "installed.json")
UPDATE_LOG = os.path.join(BASE, "update.log")
UPDATE_LOCK = os.path.join(BASE, "update.lock")
UPDATE_STATE = os.path.join(BASE, "update-check.json")
BACKUPS = os.path.join(BASE, "backup")
STAGING = os.path.join(BASE, "update-staging")
KEEP_BACKUPS = 3
SKILL_REL = re.compile(r"^skills/([A-Za-z0-9_-]+)/SKILL\.md$")
REQ_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-\[\],<>=!~]*")
UPDATE_FLAGS = ("check", "rollback", "telegram", "no_restart", "no_notify", "stage2", "locked", "force")
SUGGEST_HEAD = ("# 規則檔更新建議（版本 %s）\n\n"
                "這是新版會寫進規則檔的段落。你的規則檔是舊版格式（沒有 agent-setup 標記），所以更新程式沒有動它。\n"
                "請 AI 跟目前的規則檔比對、說明差在哪裡；主人同意後，用下面的段落取代規則檔裡對應的段落"
                "（Telegram、提醒與排程、說明書、安全防護、密碼庫、第一次見面），保留主人自己加的內容，"
                "並保留每段前後的 agent-setup 標記，之後的更新才能自動套用。\n\n")
NEWSEC_HEAD = ("# 規則檔新版段落（版本 %s）\n\n"
               "規則檔裡這幾段被改過（或拿掉了），所以更新程式沒有覆蓋。請 AI 跟規則檔裡同名的段落比對、說明差在哪裡；"
               "主人同意後再合併，並保留每段前後的 agent-setup 標記。\n\n")


class UpdateError(Exception):
    """a reason shown to the owner as is"""


def load_text(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except (OSError, ValueError):
        return ""


def sha_of(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def installed_version():
    data = load_json(INSTALLED, None)
    return data.get("version") if isinstance(data, dict) else None


def short_version(v):
    return v[:7] if v else "舊版（沒有版本紀錄）"


def kept_hint(installed):
    """the last update left these files alone (changed here): its fixes in them are not running"""
    kept = installed.get("kept_files") if isinstance(installed, dict) else None
    return "（%s 被改過、沒更新，新版在旁邊的 .new 檔）" % "、".join(kept) if kept else ""


def version_text():
    data = load_json(INSTALLED, None)
    return short_version(installed_version()) + kept_hint(data)


def home_dir():
    return os.environ.get("AGENT_SETUP_HOME") or os.path.expanduser("~")


def update_source(opt=None):
    return opt or os.environ.get("AGENT_UPDATE_SOURCE") or RAW


def fetch(url, timeout):
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "agent-setup-update"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def source_read(src, rel, version):
    """a file of that version: raw.githubusercontent.com/<repo>/<version>/<rel>, or <folder>/<rel> for a local source"""
    if os.path.isdir(src):
        with open(os.path.join(src, *rel.split("/")), "rb") as f:
            return f.read()
    return fetch("%s/%s/%s" % (src, version, rel), 60)


def load_manifest(src, path=None, timeout=30):
    if path or os.path.isdir(src):
        with open(path or os.path.join(src, "manifest.json"), "rb") as f:
            raw = f.read()
    else:
        raw = fetch("%s/main/manifest.json?nocache=%d" % (src, time.time()), timeout)
    try:
        m = json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        raise UpdateError("更新清單（manifest.json）讀不懂")
    check_manifest(m)
    return m


def safe_rel(rel):
    return isinstance(rel, str) and all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) and p not in (".", "..") for p in rel.split("/"))


def check_manifest(m):
    ok = isinstance(m, dict) and isinstance(m.get("version"), str) and re.fullmatch(r"[\w.-]{1,64}", m["version"])
    ok = ok and isinstance(m.get("files"), dict) and m["files"] and all(
        safe_rel(r) and isinstance(s, str) and re.fullmatch(r"[0-9a-f]{64}", s) for r, s in m["files"].items())
    if not ok:
        raise UpdateError("更新清單（manifest.json）格式不對")
    for key, kind in (("known", dict), ("requirements", list), ("notes", list)):
        if not isinstance(m.get(key), kind):
            m[key] = kind()
    m["known"] = {r: [x for x in v if isinstance(x, str)] for r, v in m["known"].items() if isinstance(v, list)}


def take_lock():
    """one update at a time; a lock left by an update that died (or older than 15 minutes) is taken over"""
    for _ in range(2):
        try:
            fd = os.open(UPDATE_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(UPDATE_LOCK)
                with open(UPDATE_LOCK) as f:
                    pid = int((f.read().split() or ["0"])[0])
            except (OSError, ValueError):
                age, pid = 1e9, 0
            if age < 900 and (process_info(pid) if pid else age < 10):
                return False
            try:
                os.remove(UPDATE_LOCK)
            except OSError:
                return False
            continue
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    return False


def release_lock():
    try:
        with open(UPDATE_LOCK) as f:
            mine = f.read().strip() == str(os.getpid())
        if mine:
            os.remove(UPDATE_LOCK)
    except OSError:
        pass


def read_text(path):
    """(LF text, had BOM, used CRLF)"""
    with open(path, "rb") as f:
        raw = f.read()
    return raw.decode("utf-8-sig").replace("\r\n", "\n"), raw.startswith(b"\xef\xbb\xbf"), b"\r\n" in raw


def write_bytes(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".updating"
    with open(tmp, "wb") as f:
        f.write(data)
    for i in range(10):                                 # a file being read at this moment cannot be replaced yet
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 9:
                os.remove(tmp)
                raise
            time.sleep(0.5)


def write_text(path, text, bom=False, crlf=False):
    if crlf:
        text = text.replace("\n", "\r\n")
    write_bytes(path, (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))


def agent_copy(rel):
    return os.path.join(BASE, *rel.split("/"))


def plan_files(m, installed, ai, home):
    """[(rel, dest, action)]: same | add | replace (unchanged since installed) | keep (changed here: <dest>.new)"""
    known, recorded, plan = m["known"], installed.get("file_hashes") or {}, []
    for rel, want in m["files"].items():
        dests = [agent_copy(rel)]
        skill = SKILL_REL.match(rel)
        if skill and ai == "claude":
            dests.append(os.path.join(home, ".claude", "skills", skill.group(1), "SKILL.md"))
        for dest in dests:
            cur = sha_of(dest)
            if cur == want:
                act = "same"
            elif not os.path.exists(dest):
                act = "add"
            elif cur and (cur in (known.get(rel) or []) or cur == recorded.get(rel)):
                act = "replace"
            else:
                act = "keep"
            plan.append((rel, dest, act))
    return plan


def stage(src, m, rels):
    """download and check every file first: nothing is touched unless all of them arrived intact"""
    staged = {}
    for rel in sorted(rels):
        data = source_read(src, rel, m["version"])
        if hashlib.sha256(data).hexdigest() != m["files"][rel]:
            raise UpdateError("下載的 %s 檢查碼不對（可能是網路或 GitHub 快取問題）。什麼都沒改，請過幾分鐘再試。" % rel)
        path = os.path.join(STAGING, "files", *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        staged[rel] = path
    return staged


def plan_rules(cfg, installed, template, version, old_ok=True):
    """{'mode': none | missing | unreadable | suggest | managed, ...}; never edits a rules file without markers.
    old_ok: the installed template is unchanged, so a section equal to its rendering counts as untouched"""
    path = rules_path(cfg)
    if not template or not path:
        return {"mode": "none"}
    if not os.path.exists(path):
        return {"mode": "missing", "path": path}
    try:
        text, bom, crlf = read_text(path)
    except (OSError, ValueError):
        return {"mode": "unreadable", "path": path}
    values = rules_vars(cfg, load_json(ANSWERS, None))
    new = render_sections(template, values)
    if not new:                                         # a broken template must never empty the rules file
        return {"mode": "none"}
    if not BLOCK_RE.search(text):
        return {"mode": "suggest", "path": path, "side": path + ".update-suggestion.md",
                "side_text": SUGGEST_HEAD % version + sections_text(new)}
    old = []
    if old_ok:
        try:
            with open(TEMPLATE, encoding="utf-8-sig") as f:
                old = render_sections(f.read(), values)
        except (OSError, ValueError):
            pass
    recorded = installed.get("rules_blocks") or {}
    merged, res = merge_rules(text, new, old, recorded)
    plan = {"mode": "managed", "path": path, "res": res, "record": recorded_after(res, new, recorded),
            "text": merged if merged != text else None, "bom": bom, "crlf": crlf}
    if res["kept"]:
        news = dict(new)
        plan["side"] = path + ".new-sections.md"
        plan["side_text"] = NEWSEC_HEAD % version + sections_text([(n, news[n]) for n in res["kept"]])
    # suggestions an earlier update left that no longer apply (merged, or superseded by this version)
    plan["stale"] = [p for p in (path + ".new-sections.md", path + ".update-suggestion.md")
                     if os.path.exists(p) and p != plan.get("side")]
    return plan


def new_backup_dir():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path, n = os.path.join(BACKUPS, stamp), 1
    while os.path.exists(path):
        path, n = os.path.join(BACKUPS, "%s-%d" % (stamp, n)), n + 1
    os.makedirs(path)
    return path


def save_backup(bdir, info, paths):
    """copy every file the update will write (or note that it is new) so --rollback can put things back"""
    info["files"] = []
    for i, path in enumerate(paths):
        saved = None
        if os.path.exists(path):
            saved = "f%03d" % i
            shutil.copy2(path, os.path.join(bdir, saved))
        info["files"].append({"path": path, "saved": saved})
    save_json(os.path.join(bdir, "backup.json"), info)


def restore_backup(bdir):
    info = load_json(os.path.join(bdir, "backup.json"), None)
    if not isinstance(info, dict) or not isinstance(info.get("files"), list):
        raise UpdateError("備份 %s 不完整，沒辦法還原" % bdir)
    failed = []
    for f in info["files"]:
        try:
            if f.get("saved"):
                with open(os.path.join(bdir, f["saved"]), "rb") as fh:
                    write_bytes(f["path"], fh.read())
            elif os.path.exists(f["path"]):
                os.remove(f["path"])
        except OSError as e:
            failed.append("%s（%s）" % (f["path"], e))
    if failed:
        raise UpdateError("有檔案沒還原成功：" + "、".join(failed))
    return info


def backup_dirs():
    """newest first (names are <yyyymmdd-HHMMSS> or <yyyymmdd-HHMMSS>-<n>)"""
    try:
        names = [n for n in os.listdir(BACKUPS) if os.path.isdir(os.path.join(BACKUPS, n))]
    except OSError:
        return []
    return sorted(names, key=lambda n: (n[:15], int(n[16:]) if n[16:].isdigit() else 0), reverse=True)


def ensure_requirements(cfg, reqs):
    import importlib.metadata as md
    missing = []
    for req in reqs:
        if not isinstance(req, str) or not REQ_OK.fullmatch(req):
            continue
        try:
            md.version(re.split(r"[\[<>=!~]", req, maxsplit=1)[0])
        except md.PackageNotFoundError:
            missing.append(req)
    if not missing:
        return ""
    uv = cfg.get("uv") or os.path.join(home_dir(), ".local", "bin", "uv.exe")
    if not os.path.exists(uv):
        return "套件：沒裝（找不到 uv，之後用到時會再試）：" + "、".join(missing)
    p = subprocess.run([uv, "pip", "install", "--python", python_exe()] + missing, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900, creationflags=CREATE_NO_WINDOW)
    if p.returncode:
        log("requirements: " + (p.stderr or p.stdout)[-500:], UPDATE_LOG)
        return "套件：安裝失敗（之後用到時會再試）：" + "、".join(missing)
    return "套件：已安裝 " + "、".join(missing)


def reinstall_guard(ai):
    guard = os.path.join(BASE, "guard.py")
    if ai not in RULES_NAMES or not os.path.exists(guard):
        return ""
    p = subprocess.run([python_exe(), guard, "install", ai], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120, creationflags=CREATE_NO_WINDOW)
    if p.returncode:
        log("guard install: " + (p.stderr or p.stdout)[-500:], UPDATE_LOG)
        return "安全防護：重新套用失敗（原本的設定還在，詳情在 %s）" % UPDATE_LOG
    return "安全防護：已重新套用"


def restart_bridge():
    if not bridge_running():
        return "Telegram 連線：看門狗 1 分鐘內會用新版啟動"
    with open(RESTART_BRIDGE, "w") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
    for _ in range(45):
        time.sleep(2)
        if not bridge_running():
            break
    else:
        return "Telegram 連線：正在處理訊息，空閒後會自動用新版重開"
    if os.path.exists(PAUSED):
        return "Telegram 連線：已停（目前是暫停狀態）"
    start_bridge()
    return "Telegram 連線：已用新版重新啟動"


def restart_background(cfg):
    """new code for what runs in the background (the winject queue, the Telegram bridge); never the AI window"""
    notes = []
    wj = os.path.join(BASE, "winject", "cli.py")
    if os.path.exists(wj):
        r = subprocess.run([python_exe(), wj, "stop-dispatcher"], cwd=BASE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90, creationflags=CREATE_NO_WINDOW)
        notes.append("送字佇列：1 分鐘內用新版重開" if r.returncode == 0 else "送字佇列：送完手上這則後由看門狗用新版重開")
    if cfg.get("token"):
        notes.append(restart_bridge())
    notes.append("AI 視窗：沒有重開，正在做的事不受影響")
    return "\n".join(notes)


def tell_ai(text):
    """type a [系統] note into the AI window from another process (attaching to its console would cut ours off)"""
    try:
        p = subprocess.run([python_exe(), os.path.join(BASE, "tgterm.py"), "_inject", "", "[系統] " + text],
                           creationflags=DETACHED_PROCESS, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120, env=dict(os.environ, PYTHONUTF8="1"))
        return (p.stdout or "").startswith("ok")
    except (OSError, subprocess.SubprocessError):
        return False


def write_kept_diff(frm, m, kept):
    """files the update left alone (changed here): current vs new as a diff the AI can read without running any
    command (the guard blocks commands that touch the helper files). None, and no stale file, when nothing was kept"""
    import difflib
    if not kept:
        if os.path.exists(KEPT_DIFF):
            os.remove(KEPT_DIFF)
        return None
    out = ["# 更新沒有覆蓋的檔案（%s → %s）" % (short_version(frm), short_version(m["version"])), "",
           "這些檔案被改過，更新程式沒有換掉；新版放在同一個資料夾的「檔名.new」。",
           "差異裡 - 開頭＝目前在用的（含這台改過的地方），+ 開頭＝新版（含這次更新的修正）。", ""]
    if m.get("notes"):
        out += ["## 這次更新的內容"] + ["- " + str(n) for n in m["notes"][:5]] + [""]
    for rel, dest in kept:
        diff = list(difflib.unified_diff(load_text(dest).splitlines(), load_text(dest + ".new").splitlines(),
                                         "目前的 " + rel, "新版 " + rel, n=2, lineterm=""))
        more = ["…（差異太長，只列前 400 行；完整新版在 %s.new）" % dest] if len(diff) > 400 else []
        out += ["## " + file_label(rel, dest) + "（" + dest + "）", "```diff"] + (diff[:400] or ["（內容一樣）"]) + more + ["```", ""]
    write_text(KEPT_DIFF, "\n".join(out) + "\n")
    return KEPT_DIFF


def kept_note(kept, side, m, cfg):
    who = rules_vars(cfg, load_json(ANSWERS, None)).get("user") or "主人"
    how = "（用 Telegram 問）" if cfg.get("token") else ""
    names = "、".join(file_label(rel, dest) for rel, dest in kept)
    where = ("差異已經整理在 %s，直接讀這個檔就好，不用自己下指令比對。" % side) if side else "新版放在同名的 .new 檔。"
    return ("輔助程式剛更新到 %s，但 %s 被改過，更新程式沒有覆蓋（新版放在旁邊的 .new 檔），所以這次更新的修正可能還沒生效。%s"
            "請讀完後用白話跟%s說明%s：①目前的版本改了什麼 ②新版改了什麼 ③建議怎麼做（合併兩邊／改用新版、放棄這裡的改動／先維持現狀），"
            "等%s決定再動手；改輔助程式會被安全防護擋，要%s同意並放行%s。%s沒有同意前不要動這些檔案。"
            % (short_version(m["version"]), names, where, who, how, who, who,
               "（在 Telegram 打 /allow）" if cfg.get("token") else "", who))


def rules_note(plan, cfg):
    who = rules_vars(cfg, load_json(ANSWERS, None)).get("user") or "主人"
    how = "（用 Telegram 問）" if cfg.get("token") else ""
    if plan["mode"] == "suggest":
        return ("輔助程式剛更新。規則檔 %s 是舊版格式，更新程式沒有動它；新版的規則段落放在 %s。請讀這個檔，跟目前的規則檔比對，"
                "跟%s說明差在哪裡%s；%s同意後再合併：用新段落取代規則檔裡對應的段落，保留%s自己加的內容，"
                "並保留 agent-setup 標記，之後的更新才能自動套用。沒有同意就不要改規則檔。"
                % (plan["path"], plan["side"], who, how, who, who))
    return ("輔助程式剛更新。規則檔 %s 裡有 %d 段被改過（或拿掉了），更新程式沒有覆蓋；這幾段的新版放在 %s。"
            "請讀這個檔，跟規則檔裡同名的段落比對，跟%s說明差在哪裡%s；%s同意後再合併，並保留 agent-setup 標記。"
            "沒有同意就不要改規則檔。" % (plan["path"], len(plan["res"]["kept"]), plan["side"], who, how, who))


def rules_lines(r, told, cfg):
    mode, merge = r["mode"], "已請 AI 讀過後跟你確認再合併" if told else "可以請 AI 讀這個檔，經你同意後再合併"
    if mode == "missing":
        return ["規則檔：找不到 %s，沒動" % r["path"]]
    if mode == "unreadable":
        return ["規則檔：讀不懂（不是 UTF-8），沒動"]
    if mode == "suggest":
        return ["規則檔：舊版格式（沒有更新標記），沒有自動改。新版段落放在 %s，%s" % (r["side"], merge)]
    if mode != "managed":
        return []
    res, out = r["res"], []

    def names(ns):
        return "、".join(LABELS.get(n, n) for n in ns)
    if res["replaced"]:
        out.append("規則檔：更新了「%s」" % names(res["replaced"]))
    if res["added"]:
        out.append("規則檔：新增「%s」" % names(res["added"]))
    if res["removed"]:
        out.append("規則檔：拿掉新版不需要的「%s」" % names(res["removed"]))
    if res["kept"]:
        out.append("規則檔：「%s」被改過，沒動；新版放在 %s，%s" % (names(res["kept"]), r["side"], merge))
    if not out:
        out.append("規則檔：不用改")
    if r.get("text"):
        out.append("規則有變：%s讓 AI 重新讀規則（會接著之前的對話）"
                   % ("在 Telegram 打 /restart " if cfg.get("token") else "關掉 AI 視窗（1 分鐘內自動重開）"))
    return out


def file_label(rel, dest):
    return rel if dest == agent_copy(rel) else "Claude 的 %s 說明書" % SKILL_REL.match(rel).group(1)


def rollback_hint():
    return ("要退回上一版：Telegram 打 /update 還原，或在 PowerShell 執行\n%s %s update --rollback"
            % (python_exe(), os.path.join(BASE, "tgterm.py")))


def update_report(frm, m, plan, rules, extra, told, cfg):
    def files(act):
        return [file_label(rel, dest) for rel, dest, a in plan if a == act]
    kept = files("keep")
    if kept:                                            # partly updated: say so first, not in the middle
        lines = ["⚠️ 更新只完成一部分：%s → %s" % (short_version(frm), short_version(m["version"])),
                 "⚠️ %s 被你或 AI 改過，沒有換成新版（新版放在旁邊的 .new 檔），這次的修正可能還沒生效。%s"
                 % ("、".join(kept), "已請 AI 讀差異後跟你說明、問你要怎麼處理" if told else
                    "可以請 AI 讀 %s 後跟你說明怎麼處理" % KEPT_DIFF if os.path.exists(KEPT_DIFF) else
                    "可以請 AI 比對旁邊的 .new 檔後跟你說明怎麼處理")]
    else:
        lines = ["✅ 更新完成：%s → %s" % (short_version(frm), short_version(m["version"]))]
    for act, title in (("replace", "更新"), ("add", "新增")):
        if files(act):
            lines.append("%s %d 個：%s" % (title, len(files(act)), "、".join(files(act))))
    if not any(a != "same" for _, _, a in plan):
        lines.append("檔案：都已經是新版")
    lines += rules_lines(rules, told, cfg) + [x for x in extra if x]
    if m["notes"]:
        lines.append("這次更新：" + "".join("\n・" + str(n) for n in m["notes"][:5]))
    lines.append(rollback_hint())
    return "\n".join(lines)


def update_say(cfg, o, text, code=0):
    print(text)
    log(text.replace("\n", " | "), UPDATE_LOG)
    if (o["telegram"] or not o["check"]) and not o["no_notify"] and cfg.get("token") and cfg.get("owner_id"):
        try:
            send(cfg, text)
        except Exception as e:
            log("report not sent: %r" % e, UPDATE_LOG)
    return code


def safely(what, fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        log("%s: %s" % (what, traceback.format_exc()), UPDATE_LOG)
        return "%s：沒有成功（%s）" % (what, e)


def run_newest_updater(src, m, o):
    """an older tgterm.py hands the work to the new one, so the newest update logic always runs
    (contract kept across versions: update --stage2 --locked --manifest F --source S [flags])"""
    want = m["files"].get("tgterm.py")
    if not want or sha_of(os.path.abspath(__file__)) == want:
        return None
    data = source_read(src, "tgterm.py", m["version"])
    if hashlib.sha256(data).hexdigest() != want:
        raise UpdateError("下載的 tgterm.py 檢查碼不對（可能是網路或 GitHub 快取問題）。什麼都沒改，請過幾分鐘再試。")
    folder = os.path.join(STAGING, "self")
    os.makedirs(folder, exist_ok=True)
    me, mf = os.path.join(folder, "tgterm.py"), os.path.join(folder, "manifest.json")
    with open(me, "wb") as f:
        f.write(data)
    save_json(mf, m)
    args = [sys.executable, me, "update", "--stage2", "--locked", "--manifest", mf, "--source", src]
    args += ["--" + k.replace("_", "-") for k in ("telegram", "no_restart", "no_notify", "force") if o[k]]
    return subprocess.run(args, env=dict(os.environ, AGENT_SETUP_AGENT_DIR=BASE, PYTHONUTF8="1")).returncode


def update_run(cfg, o):
    installed = load_json(INSTALLED, None)
    if not isinstance(installed, dict):
        raise UpdateError("找不到安裝紀錄（installed.json），請用安裝指令重新安裝")
    if load_text(PAUSED).strip() == "setup":
        raise UpdateError("正在重新安裝中，裝完再更新")
    src = update_source(o["source"])
    m = load_manifest(src, o["manifest"])
    if not o["stage2"]:
        code = run_newest_updater(src, m, o)
        if code is not None:
            return code
    frm = installed.get("version")
    if frm == m["version"] and not o["force"]:
        return update_say(cfg, o, "已經是最新版（%s），不用更新。%s" % (short_version(frm), kept_hint(installed)))
    ai = cfg.get("ai") or installed.get("ai")
    plan = plan_files(m, installed, ai, home_dir())
    staged = stage(src, m, {rel for rel, _, act in plan if act != "same"})
    template = None
    if "rules-sections.md" in m["files"]:
        with open(staged.get("rules-sections.md") or TEMPLATE, encoding="utf-8-sig") as f:
            template = f.read()
    old_ok = all(act != "keep" for rel, dest, act in plan if rel == "rules-sections.md")
    rules = plan_rules(cfg, installed, template, short_version(m["version"]), old_ok)

    record = dict(installed, version=m["version"], updated=time.strftime("%Y-%m-%dT%H:%M:%S"))
    record["file_hashes"] = dict(installed.get("file_hashes") or {})
    for rel, dest, act in plan:
        if act != "keep" and dest == agent_copy(rel):
            record["file_hashes"][rel] = m["files"][rel]
    if rules["mode"] == "managed":
        record["rules_blocks"] = rules["record"]
    record["kept_files"] = sorted(file_label(rel, dest) for rel, dest, act in plan if act == "keep")   # for /status
    writes = [(dest, staged[rel]) for rel, dest, act in plan if act in ("add", "replace")]
    writes += [(dest + ".new", staged[rel]) for rel, dest, act in plan if act == "keep"]
    # a <file>.new from an earlier update is stale once the file itself is current
    stale = [dest + ".new" for rel, dest, act in plan if act != "keep" and os.path.exists(dest + ".new")]
    stale += rules.get("stale", [])
    paths = [d for d, _ in writes] + [p for p in (rules["path"] if rules.get("text") else None, rules.get("side")) if p]
    bdir = new_backup_dir()
    save_backup(bdir, {"from": frm, "to": m["version"], "time": time.strftime("%Y-%m-%d %H:%M:%S")},
                paths + stale + [INSTALLED])
    try:
        for dest, path in writes:
            with open(path, "rb") as f:
                write_bytes(dest, f.read())
        for path in stale:
            os.remove(path)
        if rules.get("text"):
            write_text(rules["path"], rules["text"], rules["bom"], rules["crlf"])
        if rules.get("side"):
            write_text(rules["side"], rules["side_text"])
        save_json(INSTALLED, record)
    except Exception as e:
        log(traceback.format_exc(), UPDATE_LOG)
        try:
            restore_backup(bdir)
        except UpdateError as e2:
            raise UpdateError("寫入檔案時出錯（%s），自動退回也沒有完全成功：%s。備份在 %s" % (e, e2, bdir))
        shutil.rmtree(bdir, ignore_errors=True)
        raise UpdateError("寫入檔案時出錯（%s），已自動退回原本的版本" % e)
    for old in backup_dirs()[KEEP_BACKUPS:]:
        shutil.rmtree(os.path.join(BACKUPS, old), ignore_errors=True)
    shutil.rmtree(os.path.join(STAGING, "files"), ignore_errors=True)
    extra = [safely("套件", ensure_requirements, cfg, m["requirements"]), safely("安全防護", reinstall_guard, ai)]
    if not o["no_restart"]:
        extra.append(safely("背景程式", restart_background, cfg))
    kept = [(rel, dest) for rel, dest, act in plan if act == "keep"]
    try:
        side = write_kept_diff(frm, m, kept)
    except Exception:
        log(traceback.format_exc(), UPDATE_LOG)
        side = None
        try:                                            # never point anyone at an old diff
            os.remove(KEPT_DIFF)
        except OSError:
            pass
    notes =([rules_note(rules, cfg)] if rules.get("side") else []) + ([kept_note(kept, side, m, cfg)] if kept else [])
    told = bool(notes) and not o["no_notify"] and tell_ai(" ".join(notes))
    return update_say(cfg, o, update_report(frm, m, plan, rules, extra, told, cfg))


def update_rollback(cfg, o):
    names = [n for n in backup_dirs() if os.path.exists(os.path.join(BACKUPS, n, "backup.json"))]
    if not names:
        return update_say(cfg, o, "沒有可以還原的備份（還沒更新過，或已經還原過了）。", 1)
    bdir = os.path.join(BACKUPS, names[0])
    info = restore_backup(bdir)
    try:
        os.remove(KEPT_DIFF)
    except OSError:
        pass
    shutil.rmtree(bdir, ignore_errors=True)
    installed = load_json(INSTALLED, None) or {}
    ai = cfg.get("ai") or (installed.get("ai") if isinstance(installed, dict) else None)
    lines = ["↩️ 已還原到更新前的版本：%s → %s" % (short_version(info.get("to")), short_version(info.get("from"))),
             "還原 %d 個檔案，移除更新時新增的 %d 個" % (sum(1 for f in info["files"] if f.get("saved")) - 1,
                                               sum(1 for f in info["files"] if not f.get("saved"))),
             safely("安全防護", reinstall_guard, ai)]
    if not o["no_restart"]:
        lines.append(safely("背景程式", restart_background, cfg))
    if any(f["path"] == rules_path(cfg) for f in info["files"]):
        lines.append("規則檔也還原了：%s讓 AI 重新讀規則" % ("在 Telegram 打 /restart " if cfg.get("token") else "關掉 AI 視窗（1 分鐘內自動重開）"))
    if not info.get("from"):
        lines.append("這是還沒有 /update 的舊版：之後要更新，請在 PowerShell 貼更新指令（update.ps1）")
    return update_say(cfg, o, "\n".join(x for x in lines if x))


def update_check(cfg, o):
    try:
        m = load_manifest(update_source(o["source"]), o["manifest"], timeout=15)
    except OSError as e:
        return update_say(cfg, o, "無法檢查更新（連不上：%s）" % e, 3)
    except UpdateError as e:
        return update_say(cfg, o, "無法檢查更新（%s）" % e, 1)
    cur = installed_version()
    if cur == m["version"]:
        return update_say(cfg, o, "目前是最新版（%s）。" % short_version(cur))
    return update_say(cfg, o, "有新版：%s → %s%s\n要更新：Telegram 打 /update，或在 PowerShell 貼更新指令。" % (
        short_version(cur), short_version(m["version"]), "".join("\n・" + str(n) for n in m["notes"][:5])))


def update_opts(args):
    o = dict.fromkeys(UPDATE_FLAGS, False)
    o.update(source=None, manifest=None)
    i = 0
    while i < len(args):
        key = args[i][2:].replace("-", "_") if args[i].startswith("--") else ""
        if key in ("source", "manifest") and i + 1 < len(args):
            o[key], i = args[i + 1], i + 2
        elif key in UPDATE_FLAGS:
            o[key], i = True, i + 1
        else:
            raise SystemExit("update: unknown option " + args[i])
    return o


def update_main(args):
    o = update_opts(args)
    cfg = load_json(CFG, None)
    if not isinstance(cfg, dict):
        print("找不到 config.json：這台電腦沒有完整安裝 AI 助理（%s）" % CFG)
        return 1
    if o["check"]:
        return update_check(cfg, o)
    what = "還原" if o["rollback"] else "更新"
    if not o["locked"] and not take_lock():
        return update_say(cfg, o, "另一個更新正在進行中，請等它做完（約 1～2 分鐘）再試。", 1)
    try:
        return (update_rollback if o["rollback"] else update_run)(cfg, o)
    except UpdateError as e:
        return update_say(cfg, o, "❌ %s沒有完成：%s" % (what, e), 1)
    except OSError as e:
        log(traceback.format_exc(), UPDATE_LOG)
        return update_say(cfg, o, "❌ %s沒有完成（%s）。檔案沒有被改，請稍後再試。" % (what, e), 1)
    except Exception as e:
        log(traceback.format_exc(), UPDATE_LOG)
        return update_say(cfg, o, "❌ %s沒有完成（%r），詳情在 %s" % (what, e, UPDATE_LOG), 1)
    finally:
        if not o["locked"]:
            release_lock()
        shutil.rmtree(os.path.join(STAGING, "files") if o["stage2"] else STAGING, ignore_errors=True)


def spawn_update(*flags):
    """the update runs apart from the bridge, so it can restart the bridge when it is done"""
    subprocess.Popen([pythonw_exe(), os.path.join(BASE, "tgterm.py"), "update", "--telegram"] + list(flags), cwd=BASE,
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, close_fds=True)


def daily_update_check(cfg):
    """once a day: tell the owner on Telegram when a newer version is out (once per version)"""
    if not cfg.get("token") or os.path.exists(UPDATE_LOCK):
        return
    state = load_json(UPDATE_STATE, None)
    state = state if isinstance(state, dict) else {}
    today = time.strftime("%Y-%m-%d")
    if state.get("day") == today:
        return
    state["day"] = today
    save_json(UPDATE_STATE, state)                      # also when the check below fails: at most once a day
    m = load_manifest(update_source(), None, timeout=15)
    cur = installed_version()
    if m["version"] in (cur, state.get("announced")):
        return
    send(cfg, "🆕 輔助程式有新版（%s → %s）%s\n打 /update 更新：你或 AI 改過的檔案不會被蓋掉，/update 還原 可以退回。"
         % (short_version(cur), short_version(m["version"]), "".join("\n・" + str(n) for n in m["notes"][:5])))
    state["announced"] = m["version"]
    save_json(UPDATE_STATE, state)
    log("announced version %s" % m["version"], WATCHDOG_LOG)


# ---------------------------------------------------------------- CLI
def selftest():
    assert ctypes.sizeof(INPUT_RECORD) == 20, ctypes.sizeof(INPUT_RECORD)
    # console typing: records are only built here, never written

    def typed(recs):
        assert len(recs) % 2 == 0 and all(recs[i].Event.bKeyDown and not recs[i + 1].Event.bKeyDown
                                          and recs[i].Event.UnicodeChar == recs[i + 1].Event.UnicodeChar
                                          for i in range(0, len(recs), 2)), "down/up pairs"
        for r in recs:
            enter = r.Event.wVirtualKeyCode == VK_RETURN
            assert enter == (r.Event.UnicodeChar == "\r") and (r.Event.wVirtualScanCode == SCAN_RETURN) == enter
        return "".join(r.Event.UnicodeChar for r in recs if r.Event.bKeyDown)
    recs = records_for("測試a😀")
    assert len(recs) == (5 + 12) * 2, len(recs)         # 測 試 a + emoji as 2 UTF-16 units, plus ESC[200~ and ESC[201~
    assert typed(recs) == "\x1b[200~測試a\ud83d\ude00\x1b[201~", repr(typed(recs))
    got = typed(records_for("\n第一行\r\n第二行\n\n第三\x1b行\t尾\x07\x7f\x85\r\n"))
    assert got == "\x1b[200~第一行\r第二行\r\r第三 行\t尾   \x1b[201~", repr(got)   # line breaks -> Enter, controls -> space
    assert got.count("\x1b") == 2 and "\\" not in got and got.count("\r") == 3, repr(got)
    assert typed(records_for("a\\b")) == "\x1b[200~a\\b\x1b[201~"            # the user's own backslash stays, none added
    assert typed(records_for("a\n b\x1b", "codex")) == "a b", typed(records_for("a\n b\x1b", "codex"))
    assert typed(records_for("", None)) == "\x1b[200~\x1b[201~" and typed(enter_records()) == "\r"
    assert clean_lines("\r\na\rb\r\n\x00\tc\n\n") == "a\nb\n \tc"
    for n in (1, 12, 200, 256, 260, 268, 517, 1000):
        recs = [object()] * n
        parts = list(_chunks(recs))
        assert sum(map(len, parts)) == n and all(0 < len(x) <= 256 for x in parts), (n, list(map(len, parts)))
        assert len(parts[-1]) >= min(n, END_RECS), (n, list(map(len, parts)))      # the closing ESC[201~ stays whole
    assert "keys were already typed" in repr(AlreadyTyped("Enter failed"))
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
    # rules template: sections, placeholders (values are never read as template), managed-section merge
    tpl = ("head {{#tg}}x{{/tg}}\n<!-- agent-setup:begin a -->\n{{#tg}}\n## A（@{{bot}}）\n{{/tg}}\n{{^tg}}\n## A\n{{/tg}}\n"
           "<!-- agent-setup:end a -->\n\n<!-- agent-setup:begin b -->\n- {{user}}{{#tg}}，TG{{/tg}}。{{#tg}}\n- tg {{user}}{{/tg}}\n"
           "<!-- agent-setup:end b -->\n")
    on = render_sections(tpl, {"tg": True, "bot": "b{{user}}", "user": "主人$1"})
    assert on == [("a", "## A（@b{{user}}）"), ("b", "- 主人$1，TG。\n- tg 主人$1")], on
    off = render_sections(tpl, {"tg": False, "bot": None, "user": "U"})
    assert off == [("a", "## A"), ("b", "- U。")], off
    rules, rec = "# me\n\n" + sections_text(off), {n: block_sha(b) for n, b in off}
    text, res = merge_rules(rules, on, off, rec)
    assert res["replaced"] == ["a", "b"] and text == "# me\n\n" + sections_text(on), (res, text)
    assert merge_rules(text, on, off, rec)[1]["same"] == ["a", "b"]
    assert merge_rules(rules, on, off, {})[1]["replaced"] == ["a", "b"]           # equal to the installed rendering
    mine = rules.replace("- U。", "- U。我的規則").replace(block_text("a", "## A") + "\n\n", "")
    text, res = merge_rules(mine, on + [("c", "C")], off, rec)
    assert res["kept"] == ["b", "a"] and res["added"] == ["c"] and "我的規則" in text, (res, text)
    assert text.endswith(block_text("c", "C") + "\n") and "## A" not in text, text
    assert recorded_after(res, on + [("c", "C")], rec) == {"c": block_sha("C"), "b": rec["b"], "a": rec["a"]}
    text, res = merge_rules(rules, [("a", "## A")], off, rec)                      # a section the new version dropped
    assert res["removed"] == ["b"] and text == "# me\n\n" + sections_text([("a", "## A")]), (res, text)
    assert block_sha("x\r\ny\n") == block_sha("x\ny") and not BLOCK_RE.search("# old rules, no markers")
    o = update_opts(["--rollback", "--source", "d", "--no-restart"])
    assert o["rollback"] and o["source"] == "d" and o["no_restart"] and not o["check"], o
    assert safe_rel("winject/cli.py") and not safe_rel("../x") and not safe_rel("a/../b") and not safe_rel("C:/x")
    global BACKUPS
    saved, BACKUPS = BACKUPS, tempfile.mkdtemp()
    try:
        for n in ("20261101-120000", "20261101-120000-2", "20261101-120000-10", "20261031-235959-11"):
            os.makedirs(os.path.join(BACKUPS, n))
        assert backup_dirs() == ["20261101-120000-10", "20261101-120000-2", "20261101-120000", "20261031-235959-11"]
    finally:
        shutil.rmtree(BACKUPS)
        BACKUPS = saved
    selftest_logon_trigger()
    selftest_kept_update()
    selftest_telegram()
    print("selftest ok")


def selftest_kept_update():
    """an update that could not replace a changed file says so first, writes the diff and tells the AI what to do"""
    import tempfile
    global BASE, KEPT_DIFF, INSTALLED
    saved, d = (BASE, KEPT_DIFF, INSTALLED), tempfile.mkdtemp()
    BASE, KEPT_DIFF, INSTALLED = d, os.path.join(d, "update-kept.md"), os.path.join(d, "installed.json")
    try:
        dest = agent_copy("tgterm.py")
        write_text(dest, "a\nmine\nc\n")
        write_text(dest + ".new", "a\nfix\nc\n")
        m = {"version": "8276877" + "0" * 33, "notes": ["修正：重開機會自己開"], "files": {}}
        plan = [("tgterm.py", dest, "keep"), ("guard.py", agent_copy("guard.py"), "replace")]
        side = write_kept_diff("467f411" + "0" * 33, m, [("tgterm.py", dest)])
        text = load_text(side)
        assert side == KEPT_DIFF and "-mine" in text and "+fix" in text and "修正：重開機會自己開" in text, text
        note = kept_note([("tgterm.py", dest)], side, m, {"token": "x"})
        assert "tgterm.py" in note and side in note and "/allow" in note and "沒有同意前不要動" in note, note
        r = update_report("467f411", m, plan, {"mode": "none"}, [], True, {"token": "x"})
        first = r.split("\n")
        assert first[0].startswith("⚠️ 更新只完成一部分") and "tgterm.py" in first[1] and "已請 AI" in first[1], r
        assert "更新 1 個：guard.py" in r and "✅" not in first[0]
        assert KEPT_DIFF in update_report("467f411", m, plan, {"mode": "none"}, [], False, {})   # AI not told: point at the diff
        os.rename(KEPT_DIFF, KEPT_DIFF + ".x")
        assert "比對旁邊的 .new 檔" in update_report("467f411", m, plan, {"mode": "none"}, [], False, {})   # no diff: no dead link
        os.rename(KEPT_DIFF + ".x", KEPT_DIFF)
        r2 = update_report("467f411", m, [plan[1]], {"mode": "none"}, [], False, {})
        assert r2.startswith("✅ 更新完成") and "⚠️" not in r2, r2
        save_json(INSTALLED, {"version": m["version"], "kept_files": ["tgterm.py"]})
        assert version_text() == "8276877（tgterm.py 被改過、沒更新，新版在旁邊的 .new 檔）", version_text()
        save_json(INSTALLED, {"version": m["version"], "kept_files": []})
        assert version_text() == "8276877" and kept_hint(None) == ""
        assert write_kept_diff("x", m, []) is None and not os.path.exists(KEPT_DIFF)   # stale diff removed
    finally:
        shutil.rmtree(d)
        BASE, KEPT_DIFF, INSTALLED = saved


def selftest_logon_trigger():
    """the watchdog task's "at sign-in" trigger: XML edit and the once-only mark; schtasks is faked, never run"""
    import tempfile
    global LOGON_MARK, BASE
    saved, d = (LOGON_MARK, BASE), tempfile.mkdtemp()
    LOGON_MARK, BASE = os.path.join(d, "logon-trigger"), d
    task = ('<?xml version="1.0" encoding="UTF-16"?><Task><Triggers><TimeTrigger><Repetition><Interval>PT1M</Interval>'
            '</Repetition><StartBoundary>2026-11-01T14:44:00</StartBoundary><Enabled>true</Enabled></TimeTrigger>'
            '</Triggers><Principals><Principal id="Author"><UserId>PC\\Kai</UserId><LogonType>InteractiveToken'
            '</LogonType></Principal></Principals></Task>')
    calls, created = [], []

    class R:
        def __init__(self, code, out=b""):
            self.returncode, self.stdout, self.stderr = code, out, b""

    def fake(args, **kw):
        calls.append(args[1])
        if args[1] == "/Query":
            return R(0, ("﻿" + task).encode("utf-16-le"))
        with open(args[args.index("/XML") + 1], "rb") as f:
            created.append(task_xml_text(f.read()))
        return R(0)
    try:
        x = with_logon_trigger(task)
        assert x.index("<TimeTrigger>") < x.index("<LogonTrigger>") < x.index("</Triggers>"), x
        assert "<UserId>PC\\Kai</UserId><Delay>PT15S</Delay></LogonTrigger>" in x and with_logon_trigger(x) is None
        assert ensure_logon_trigger("t", fake) == "OK: added" and calls == ["/Query", "/Create"]
        assert "<LogonTrigger>" in created[0] and created[0].count("<TimeTrigger>") == 1
        assert load_text(LOGON_MARK).startswith("ok ") and not os.path.exists(os.path.join(d, "watchdog-task.xml"))
        assert ensure_logon_trigger("t", fake) is None and len(calls) == 2           # once per install
        os.remove(LOGON_MARK)
        bad = lambda args, **kw: R(1)
        assert ensure_logon_trigger("t", bad).startswith("query failed") and load_text(LOGON_MARK).startswith("fail ")
        assert ensure_logon_trigger("t", fake) is None and len(calls) == 2           # failure: wait 6 hours
        with open(LOGON_MARK, "w") as f:
            f.write("fail %d x" % (time.time() - 7 * 3600))
        assert ensure_logon_trigger("t", fake) == "OK: added"                         # then try again
        os.remove(LOGON_MARK)

        def boom(args, **kw):
            raise subprocess.TimeoutExpired(args, 60)
        assert ensure_logon_trigger("t", boom).startswith("error:") and load_text(LOGON_MARK).startswith("fail ")
        assert task_xml_text("a".encode("utf-16")) == "a" and task_xml_text(b"abc") == "abc"
    finally:
        shutil.rmtree(d)
        LOGON_MARK, BASE = saved


def selftest_telegram():
    """streamed replies and topics: pure logic plus temp files; Telegram is never called (configs have no token)"""
    import tempfile
    global THREAD, CFG, LOG
    saved, d = (THREAD, CFG, LOG), tempfile.mkdtemp()
    THREAD, CFG, LOG = (os.path.join(d, name) for name in ("tg-thread.json", "config.json", "tgterm.log"))
    try:
        selftest_drafts()
        selftest_topics()
    finally:
        shutil.rmtree(d)
        THREAD, CFG, LOG = saved
    names = dict(COMMANDS)
    assert all(v in names or v in HIDDEN_COMMANDS for v in ALIASES.values()) and set(TOGGLES) <= set(names)
    assert ALIASES["串流"] == "stream" and ALIASES["主題"] == "topics"
    assert all("/" + c in help_text() for c in names) and "/串流" in help_text() and "/主題" in help_text()
    assert all(re.fullmatch(r"[a-z0-9_]{1,32}", c) and 0 < len(t) <= 256 for c, t in COMMANDS)   # setMyCommands rules
    assert "/密碼" in help_text() and all(VAULT_TEXT.match("/" + a) for a, v in ALIASES.items() if v == "vault")
    assert not any(VAULT_TEXT.match("/" + a) for a, v in list(ALIASES.items()) + [(c, c) for c in names] if v != "vault")
    selftest_vault()


def selftest_vault():
    """/vault and the ✅ / ❌ buttons: the text reaches vault.py only (never the AI, a log or the inbox) and a message
    that may hold a password is deleted. Telegram, the AI window and vault.py are fakes; the real vault is never loaded"""
    import tempfile
    global LOG, INBOX, THREAD, tg, send, deliver, load_vault
    saved, d = (LOG, INBOX, THREAD, tg, send, deliver, load_vault), tempfile.mkdtemp()
    LOG, INBOX, THREAD = (os.path.join(d, n) for n in ("tgterm.log", "tg-inbox", "tg-thread.json"))
    calls, sent, delivered, fail = [], [], [], set()
    secret = "s3cr3t pa ss 密碼"

    def fake_tg(cfg, method, _timeout=70, **params):
        calls.append((method, params))
        if method in fail:
            raise urllib.error.HTTPError("https://api.telegram.org/x", 400, "Bad Request", None, None)
        return {"ok": True, "result": True}

    class FakeVault:
        def __init__(self):
            self.seen, self.taps = [], []

        def owner_command(self, text):
            self.seen.append(text)
            return "✅ 已存", "新增" in text

        def decide(self, req, verdict):
            self.taps.append((req, verdict))
            return (True, "已允許") if req == "0123456789ab" else (False, "這個請求已經結束了")

    def missing():
        raise FileNotFoundError("vault.py")
    fv = FakeVault()
    tg, send = fake_tg, lambda cfg, text, stream=False: sent.append(text)
    deliver = lambda cfg, body, date=None: delivered.append(body)
    cfg, owner = {"token": "t", "owner_id": 1}, {"chat": {"type": "private", "id": 1}, "from": {"id": 1}, "message_id": 9}
    try:
        load_vault = missing                            # vault.py not installed: still deleted, still not forwarded
        handle(cfg, dict(owner, text="/密碼 新增 github github.com me " + secret))
        assert calls == [("deleteMessage", {"chat_id": 1, "message_id": 9})] and "不能用" in sent[-1] and "已經刪掉" in sent[-1]
        handle(cfg, dict(owner, text="/密碼"))
        assert len(calls) == 1 and "不能用" in sent[-1] and "刪" not in sent[-1]
        load_vault = lambda: fv
        for text in ("／密碼新增 a b.com u " + secret, "/vault@my_bot add a b.com u " + secret, "/ 密碼庫 新增 x " + secret):
            handle(cfg, dict(owner, text=text))
            assert fv.seen[-1] == text and calls[-1] == ("deleteMessage", {"chat_id": 1, "message_id": 9}), text
        handle(cfg, dict(owner, photo=[{"file_id": "f", "file_size": 1}], caption="/密碼 新增 p p.com u " + secret))
        assert fv.seen[-1].endswith(secret) and calls[-1][0] == "deleteMessage"   # a caption too; the photo is not downloaded
        n = len(calls)
        handle(cfg, dict(owner, text="/vault 清單"))
        assert fv.seen[-1] == "/vault 清單" and len(calls) == n and sent[-1] == "✅ 已存"
        fail.add("deleteMessage")
        handle(cfg, dict(owner, text="/密碼 新增 a b.com u " + secret))
        assert "刪不掉" in sent[-1] and "NOT deleted" in load_text(LOG)
        handle(cfg, {"chat": {"type": "private", "id": 2}, "from": {"id": 2}, "message_id": 3, "text": "/密碼 新增 " + secret})
        assert len(fv.seen) == 6 and calls[-1][1].get("message_id") == 9   # someone else's: ignored
        handle(cfg, dict(owner, text="密碼是 1234"))                    # not a command: goes to the AI as before
        assert delivered == ["密碼是 1234"] and not os.path.exists(INBOX) and "getFile" not in [c for c, _ in calls]
        assert secret not in load_text(LOG) and not any(secret in s for s in sent)

        # buttons
        del calls[:]
        fail.clear()
        msg = {"chat": {"id": 1}, "message_id": 5, "text": "🔐 AI 要把「github」的密碼填進 Chrome 的 github.com"}
        tap = {"id": "c1", "from": {"id": 1}, "data": "vault:0123456789ab:ok", "message": msg}
        dispatch(cfg, {"update_id": 1, "callback_query": tap})
        assert fv.taps == [("0123456789ab", "ok")] and calls[0] == ("answerCallbackQuery", {"callback_query_id": "c1", "text": "已允許"})
        assert calls[1][0] == "editMessageText" and calls[1][1]["text"].endswith("→ ✅ 已允許") and calls[1][1]["message_id"] == 5
        del calls[:]
        dispatch(cfg, {"update_id": 2, "callback_query": dict(tap, data="vault:aaaaaaaaaaaa:no")})   # finished already
        assert calls[0][1]["text"] == "這個請求已經結束了" and calls[1][0] == "editMessageReplyMarkup" and len(calls) == 2
        del calls[:]
        dispatch(cfg, {"update_id": 3, "callback_query": dict(tap, **{"from": {"id": 2}})})          # not the owner
        assert len(fv.taps) == 2 and calls == [("answerCallbackQuery", {"callback_query_id": "c1", "text": "只有主人能按"})]
        del calls[:]
        dispatch(cfg, {"update_id": 3, "callback_query": dict(tap, message=dict(msg, chat={"id": -100}))})  # not the private chat
        assert len(fv.taps) == 2 and calls == [("answerCallbackQuery", {"callback_query_id": "c1", "text": "只有主人能按"})]
        del calls[:]
        dispatch(cfg, {"update_id": 4, "callback_query": dict(tap, data="other")})
        assert calls == [("answerCallbackQuery", {"callback_query_id": "c1", "text": "這個按鈕已經沒有作用了"})]
        del calls[:]
        fail.add("editMessageText")
        load_vault = missing
        dispatch(cfg, {"update_id": 5, "callback_query": tap})          # vault.py broken: answered, buttons removed
        assert calls[0][1]["text"] == "出錯了（FileNotFoundError）" and calls[1][0] == "editMessageReplyMarkup"
        assert "callback_query" in json.loads(ALLOWED_UPDATES) and "message" in json.loads(ALLOWED_UPDATES)
        assert len(delivered) == 1 and secret not in load_text(LOG)
    finally:
        shutil.rmtree(d, ignore_errors=True)
        LOG, INBOX, THREAD, tg, send, deliver, load_vault = saved


def selftest_drafts():
    """streamed replies: the cut / step math, then the timing against a fake network"""
    for n in (1, 2, 3, 10, 59, 60, 61, 100, 101, 3900, 4096):
        cuts = draft_cuts(n)
        assert cuts[-1] == n and 1 <= len(cuts) <= DRAFT_STEPS and cuts == sorted(set(cuts)), (n, cuts)
    assert draft_cuts(0) == [] and draft_cuts(1) == [1] and draft_cuts(10) == [4, 8, 10] and len(draft_cuts(100)) == 20
    for count in (1, 2, 7, 20):
        assert cuts_due(0, count) == 0 and cuts_due(DRAFT_SECONDS, count) == count == cuts_due(99, count)
        for k in range(1, count + 1):
            t = cut_time(k, count)
            assert cuts_due(t, count) >= k > cuts_due(t - 0.01, count), (count, k, t)   # due on time, not before
    assert cut_time(1, 20) == DRAFT_THINK and abs(cut_time(20, 20) - DRAFT_SECONDS) < 1e-9
    assert utf16_len("a😀") == 3 and DRAFT_LIMIT <= 4.0

    scale = 0.1                                         # the whole timing at a tenth: 2.5 s -> 0.25 s

    def run(text, delay=0.0, fail_at=None):
        """stream with a fake network: each call takes `delay` (real-time) s, or times out past its timeout"""
        posted = []

        def post(part, timeout):
            posted.append(part)
            if fail_at == len(posted) or delay * scale > timeout:
                time.sleep(min(delay * scale, timeout))
                raise OSError("timed out" if delay * scale > timeout else "draft refused")
            time.sleep(delay * scale)
        t0 = time.monotonic()
        n = stream_draft({}, text, post=post, scale=scale)
        return n, posted, (time.monotonic() - t0) / scale
    full = "一二三四五六七八九十" * 10
    n, posted, took = run(full)                         # fast network: "Thinking…", then 20 growing prefixes in ~2.5 s
    assert posted[0] == "" and posted[-1] == full and n == len(posted) <= DRAFT_STEPS + 1, (n, posted)
    assert all(full.startswith(p) and len(p) > len(q) for p, q in zip(posted[1:], posted)), posted
    assert DRAFT_SECONDS - 0.3 <= took < DRAFT_LIMIT, took
    for delay in (0.75, 1.5, 2.25, 4.5):                # slow networks: fewer steps, never past the cap
        n, posted, took = run(full, delay)
        assert took <= DRAFT_LIMIT + 0.4 and n <= len(posted), (delay, took, n)
        assert delay > 1.5 or posted[-1] == full, (delay, posted)
    n, posted, took = run(full, fail_at=3)              # a refused draft ends the preview quietly
    assert n == 2 and len(posted) == 3 and took < DRAFT_LIMIT, (n, posted, took)
    assert run("x" * 4097)[1] == [] and run("😀" * 2049)[1] == [] and run("")[1] == []   # over 4096: no preview
    assert run("😀" * 2048)[1][-1] == "😀" * 2048


def selftest_topics():
    """topics: the remembered topic across processes, refused topics, the toggles, topic events (temp files only)"""
    import io
    assert saved_thread() is None and reply_thread({}) is None
    remember_thread({"message_thread_id": 7, "text": "hi"})
    # `tgterm.py send` is another process: it only has the file
    assert load_json(THREAD, None)["message_thread_id"] == 7 and saved_thread() == 7
    assert reply_thread({}) == 7 and reply_thread({"topics": True}) == 7 and reply_thread({"topics": False}) is None
    forget_thread(8)
    assert saved_thread() == 7                          # forgetting another topic leaves this one
    for m in ({"text": "not in a topic"}, {"message_thread_id": True}, {"message_thread_id": "7"},
              {"message_thread_id": 0}):
        remember_thread({"message_thread_id": 7})
        remember_thread(m)
        assert saved_thread() is None and not os.path.exists(THREAD), m
    save_json(THREAD, ["broken"])
    assert saved_thread() is None
    # a `send` forgetting refused topic 5 waits for the lock, then finds the bridge's newer topic 6 and leaves it
    remember_thread({"message_thread_id": 5})
    done = []
    with thread_locked():
        w = threading.Thread(target=lambda: done.append(forget_thread(5)))
        w.start()
        time.sleep(0.2)
        assert not done                                 # blocked behind the lock
        save_json(THREAD, {"message_thread_id": 6})
    w.join(5)
    assert done and saved_thread() == 6

    calls = []

    def fake(code=400, always=False):
        def fn(**kw):
            calls.append(kw)
            if always or "message_thread_id" in kw:
                raise urllib.error.HTTPError("https://api.telegram.org/x", code, "err", None, io.BytesIO(
                    b'{"ok":false,"description":"Bad Request: message thread not found"}'))
            return "ok"
        return fn
    remember_thread({"message_thread_id": 5})
    assert in_thread(5, fake(), text="x") == ("ok", None)           # a refused topic: sent without it, forgotten
    assert calls == [{"text": "x", "message_thread_id": 5}, {"text": "x"}] and saved_thread() is None, calls
    assert "message thread not found" in load_text(LOG)
    del calls[:]
    assert in_thread(None, fake(), text="y") == ("ok", None) and calls == [{"text": "y"}]
    try:
        in_thread(5, fake(429), text="z")               # anything but a refused topic is not retried
        raise AssertionError("429 retried")
    except urllib.error.HTTPError as e:
        assert e.code == 429 and len(calls) == 2
    remember_thread({"message_thread_id": 5})
    try:
        in_thread(5, fake(always=True), text="too long")   # refused with and without the topic: not the topic's fault
        raise AssertionError("error swallowed")
    except urllib.error.HTTPError:
        assert len(calls) == 4 and saved_thread() == 5  # the error reaches the caller and the topic is kept

    save_json(CFG, {"token": "", "owner_id": 1, "agent_name": "名字"})
    cfg = load_cfg()
    assert not toggle_on(cfg, "stream") and toggle_on(cfg, "topics")                  # defaults: stream off, topics on
    assert set_toggle(cfg, "stream") is True and cfg["stream"] is True and load_cfg()["stream"] is True
    assert set_toggle(cfg, "stream", "開") is True and set_toggle(cfg, "stream", " OFF ") is False
    assert set_toggle(cfg, "topics") is False and load_cfg()["topics"] is False
    assert set_toggle(cfg, "topics", "on") is True and load_cfg()["agent_name"] == "名字"
    assert toggle_on({"stream": 0}, "stream") is False and toggle_on({"topics": None}, "topics") is True

    # the owner making a topic: remembered, not answered (with no token any reply would raise)
    handle(cfg, {"chat": {"type": "private"}, "from": {"id": 1}, "message_thread_id": 12,
                 "forum_topic_created": {"name": "工作", "icon_color": 7322096}})
    assert saved_thread() == 12
    handle(cfg, {"chat": {"type": "private"}, "from": {"id": 2}, "message_thread_id": 13, "text": "x"})
    assert saved_thread() == 12                         # someone else's message never moves the topic


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
            send(cfg, text, stream=toggle_on(cfg, "stream"))   # the AI's replies; the bridge's own notes never stream
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
            log("inject (%s): %s" % (how, one_line(argv[3])[:80]))
            print("ok " + how)
        except Exception as e:
            log("inject failed: %r" % e)
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
        print("version=%s" % installed_version())
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
    elif cmd == "update":
        return update_main(argv[2:])
    elif cmd == "render-rules":
        render_rules_cmd(argv[2:])
    elif cmd == "rules-record":
        rules_record_cmd(argv[2:])
    elif cmd == "selftest":
        selftest()
    else:
        print(__doc__ or "usage: see the top of tgterm.py")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
