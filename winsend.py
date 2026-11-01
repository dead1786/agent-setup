"""winsend.py - type text into another window, or send a LINE desktop message (Windows only).

Standard library + ctypes only. agent-setup installs it as C:\\AI\\.agent\\winsend.py and runs it with
C:\\AI\\.agent\\py\\Scripts\\python.exe.

Usage
  winsend.py window "<window title>" "<text>" [--no-enter]
  winsend.py window "<window title>" --file <utf-8 text file> [--no-enter]
  winsend.py line "<chat name>" "<text>" [--mention "<member name>"] [--no-enter]
  winsend.py line "<chat name>" --file <utf-8 text file> [--mention "<member name>"] [--no-enter]
  winsend.py list        visible windows that can be targets, and the LINE chat windows that are open
  winsend.py selftest    offline checks only: touches no window, clipboard, mouse or keyboard
  Put -- before a text that itself starts with "--". Multi-line text: use --file.

Result: one line on stdout, "OK: ..." with exit code 0, or "FAIL: <reason>" with exit code 1.
A FAIL that ends with "(keys were already pressed ...)" means something may already have been sent;
check the chat/window before trying again. Every other FAIL stopped before anything was sent.

window  Finds a visible top-level window: the exact title first, then the same title ignoring case and
        full-width/half-width differences, then the one window whose title contains the given text
        (more than one at the first tier that matches -> FAIL listing the candidates). Brings it to the
        front (minimize/restore, ALT tap, AttachThreadInput, SwitchToThisWindow, then a
        foreground-lock-timeout fallback; up to 3 tries), pastes the text with Ctrl+V, checks the window
        is still in front, presses Enter once, then hands the focus back to the window that had it.
        Line breaks, tabs and other control characters become spaces: in a terminal a line break would
        submit early or run a command. Nothing else in the text is changed. The text that arrives is not
        read back (there is no general way to); only the foreground is checked.
line    LINE desktop: Qt windows owned by LINE*.exe. The chat must be open as a window of its own (not only
        in the right pane of the main "LINE" window). Brings the chat window forward, clicks its input
        box (centered, 55 px above the window's bottom edge at 100 % scaling), clears it, pastes, reads
        the input box back (Ctrl+A, Ctrl+C) and presses Enter only when it holds exactly the message;
        then minimizes the chat window and hands the focus back. Line breaks are kept (pasting does not
        send; only the final Enter does). --mention types "@", pastes the member name, presses Enter to
        pick the first suggestion, then pastes " <message>" after it.
        --no-enter pastes and checks the message but does not send it; the chat window stays in front.

Both modes run one send at a time (named mutex Local\\agent-winsend, waits up to 60 s). The clipboard's
text is saved first and put back afterwards; images or files on the clipboard are not kept. Text this
tool puts on the clipboard is marked so that Windows clipboard history and cloud clipboard skip it.
A send takes a few seconds; do not use the keyboard or mouse meanwhile.
Log: winsend.log next to this file (rotated at 1 MB to winsend.log.1).
Environment: --mention types "@" as the real key of the current keyboard layout (Shift+2 on US/Taiwan
layouts, as the original pyautogui tool did). WINSEND_AT_KEY=unicode sends it as a Unicode character event
instead (ignores the layout; try it if a Chinese input method in Chinese mode swallows the key).
Not handled: a target window running as administrator silently ignores keys from a normal process, and
in window mode that still reports OK (nothing can be read back there).
"""
import ctypes
import os
import re
import sys
import time
import traceback
import unicodedata
import uuid
from collections import namedtuple
from ctypes import wintypes as wt

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "winsend.log")
LOG_MAX = 1_000_000
MUTEX_NAME = "Local\\agent-winsend"
MUTEX_WAIT_MS = 60_000
LINE_INPUT_OFFSET = 55          # input box: px above the chat window's bottom edge at 96 DPI
LINE_MAIN_TITLE = "LINE"        # the main LINE window; chats opened on their own carry the chat's name
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
NO_HISTORY_FORMATS = ("ExcludeClipboardContentFromMonitorProcessing",
                      "CanIncludeInClipboardHistory", "CanUploadToCloudClipboard")
MAYBE_SENT_NOTE = (" (keys were already pressed in the target, so something may already have been sent;"
                   " check the chat/window before trying again)")


class SendFail(Exception):
    def __init__(self, reason, maybe_sent=False, focus_lost=False):
        super().__init__(reason)
        self.reason = reason
        self.maybe_sent = maybe_sent    # keys that can send were already pressed
        self.focus_lost = focus_lost    # the user (or a program) brought another window forward: leave it there


class UsageError(Exception):
    pass


def log(msg):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a", encoding="utf-8", errors="backslashreplace") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " [%d] %s\n" % (os.getpid(), msg))
    except Exception:                               # logging must never turn a send into a failure
        pass


def say(line):
    try:
        print(line, flush=True)
    except Exception:
        pass


def preview(s, n=60):
    if s is None:
        return "None"
    s = s.replace("\r", "\\r").replace("\n", "\\n")
    return s[:n] + ("..." if len(s) > n else "")


# ---------------------------------------------------------------- Win32 bindings (nothing is called here)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
except OSError:
    dwmapi = None

ULONG_PTR = ctypes.c_size_t
HWND, DWORD, BOOL, UINT, HANDLE = wt.HWND, wt.DWORD, wt.BOOL, wt.UINT, wt.HANDLE
POINT, RECT = wt.POINT, wt.RECT


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", DWORD), ("dwFlags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", DWORD), ("time", DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", DWORD), ("u", _INPUTUNION)]


WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, wt.LPARAM)


def _bind(dll, name, restype, *argtypes):
    """set the prototype; None when this Windows version lacks the function"""
    try:
        fn = getattr(dll, name)
    except AttributeError:
        return None
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


for _name, _res, _args in (
        ("EnumWindows", BOOL, (WNDENUMPROC, wt.LPARAM)),
        ("IsWindow", BOOL, (HWND,)),
        ("IsWindowVisible", BOOL, (HWND,)),
        ("IsIconic", BOOL, (HWND,)),
        ("GetWindowTextLengthW", ctypes.c_int, (HWND,)),
        ("GetWindowTextW", ctypes.c_int, (HWND, wt.LPWSTR, ctypes.c_int)),
        ("GetClassNameW", ctypes.c_int, (HWND, wt.LPWSTR, ctypes.c_int)),
        ("GetWindowThreadProcessId", DWORD, (HWND, ctypes.POINTER(DWORD))),
        ("GetForegroundWindow", HWND, ()),
        ("SetForegroundWindow", BOOL, (HWND,)),
        ("BringWindowToTop", BOOL, (HWND,)),
        ("ShowWindow", BOOL, (HWND, ctypes.c_int)),
        ("SwitchToThisWindow", None, (HWND, BOOL)),
        ("SetActiveWindow", HWND, (HWND,)),
        ("SetFocus", HWND, (HWND,)),
        ("AttachThreadInput", BOOL, (DWORD, DWORD, BOOL)),
        ("IsGUIThread", BOOL, (BOOL,)),
        ("SystemParametersInfoW", BOOL, (UINT, UINT, ctypes.c_void_p, UINT)),
        ("SendInput", UINT, (UINT, ctypes.POINTER(INPUT), ctypes.c_int)),
        ("MapVirtualKeyW", UINT, (UINT, UINT)),
        ("VkKeyScanW", ctypes.c_short, (wt.WCHAR,)),
        ("GetWindowRect", BOOL, (HWND, ctypes.POINTER(RECT))),
        ("GetCursorPos", BOOL, (ctypes.POINTER(POINT),)),
        ("SetCursorPos", BOOL, (ctypes.c_int, ctypes.c_int)),
        ("WindowFromPoint", HWND, (POINT,)),
        ("GetAncestor", HWND, (HWND, UINT)),
        ("GetSystemMetrics", ctypes.c_int, (ctypes.c_int,)),
        ("OpenClipboard", BOOL, (HWND,)),
        ("CloseClipboard", BOOL, ()),
        ("EmptyClipboard", BOOL, ()),
        ("IsClipboardFormatAvailable", BOOL, (UINT,)),
        ("GetClipboardData", HANDLE, (UINT,)),
        ("SetClipboardData", HANDLE, (UINT, HANDLE)),
        ("GetClipboardSequenceNumber", DWORD, ()),
        ("RegisterClipboardFormatW", UINT, (wt.LPCWSTR,))):
    _bind(user32, _name, _res, *_args)

for _name, _res, _args in (
        ("GetCurrentThreadId", DWORD, ()),
        ("GlobalAlloc", HANDLE, (UINT, ctypes.c_size_t)),
        ("GlobalLock", ctypes.c_void_p, (HANDLE,)),
        ("GlobalUnlock", BOOL, (HANDLE,)),
        ("GlobalFree", HANDLE, (HANDLE,)),
        ("GlobalSize", ctypes.c_size_t, (HANDLE,)),
        ("CreateMutexW", HANDLE, (ctypes.c_void_p, BOOL, wt.LPCWSTR)),
        ("WaitForSingleObject", DWORD, (HANDLE, DWORD)),
        ("ReleaseMutex", BOOL, (HANDLE,)),
        ("CloseHandle", BOOL, (HANDLE,)),
        ("OpenProcess", HANDLE, (DWORD, BOOL, DWORD)),
        ("QueryFullProcessImageNameW", BOOL, (HANDLE, DWORD, wt.LPWSTR, ctypes.POINTER(DWORD)))):
    _bind(kernel32, _name, _res, *_args)

GetDpiForWindow = _bind(user32, "GetDpiForWindow", UINT, HWND)                          # Windows 10 1607+
SetProcessDpiAwarenessContext = _bind(user32, "SetProcessDpiAwarenessContext", BOOL, ctypes.c_void_p)
SetProcessDPIAware = _bind(user32, "SetProcessDPIAware", BOOL)
DwmGetWindowAttribute = _bind(dwmapi, "DwmGetWindowAttribute", ctypes.c_long,
                              HWND, DWORD, ctypes.c_void_p, DWORD) if dwmapi else None

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x02, 0x04, 0x08, 0x10
VK_BACK, VK_RETURN, VK_SHIFT, VK_CONTROL, VK_MENU = 0x08, 0x0D, 0x10, 0x11, 0x12
VK_A, VK_C, VK_V = 0x41, 0x43, 0x56
SW_SHOW, SW_MINIMIZE, SW_RESTORE = 5, 6, 9
SM_SWAPBUTTON, GA_ROOT, DWMWA_CLOAKED = 23, 2, 14
SPI_GETFOREGROUNDLOCKTIMEOUT, SPI_SETFOREGROUNDLOCKTIMEOUT, SPIF_SENDCHANGE = 0x2000, 0x2001, 0x0002
CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
WAIT_OBJECT_0, WAIT_ABANDONED = 0x0, 0x80
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# ---------------------------------------------------------------- pure helpers (covered by selftest)
Win = namedtuple("Win", "hwnd title cls visible iconic pid exe")

_NEWLINES = re.compile(r"(?:\r\n|[\r\n\x85\u2028\u2029])+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def prep_window_text(text):
    """one line: line breaks, tabs and control characters become spaces"""
    text = _NEWLINES.sub(" ", text or "")
    return _CONTROL.sub(" ", text.replace("\t", " ")).strip()


def prep_line_text(text):
    """keeps line breaks (as \\n) and tabs; other control characters become spaces"""
    text = (text or "").replace("\r\n", "\n")
    for ch in ("\r", "\x85", "\u2028", "\u2029"):
        text = text.replace(ch, "\n")
    return _CONTROL.sub(" ", text).strip()


def norm_title(s):
    """for loose title matching: full-width -> half-width (NFKC), case-insensitive, single spaces"""
    return " ".join(unicodedata.normalize("NFKC", s or "").casefold().split())


def match_title(items, query, key):
    """(item, None) when exactly one item matches at the first tier that has a match, else (None, reason)"""
    q = (query or "").strip()
    if not q:
        return None, "the name is empty"
    nq = norm_title(q)
    tiers = (("exact title", lambda t: t.strip() == q),
             ("same title ignoring case/width", lambda t: norm_title(t) == nq),
             ("title contains it", lambda t: nq in norm_title(t)))
    for label, pred in tiers:
        hits = [it for it in items if pred(key(it))]
        if len(hits) == 1:
            return hits[0], None
        if hits:
            names = ", ".join(repr(key(h)) for h in hits[:10])
            return None, "%d windows match %r (%s): %s; use the full exact title" % (len(hits), q, label, names)
    return None, "nothing matches %r" % q


def is_line_class(cls):
    return "Qt" in cls or "LINE" in cls            # LINE desktop is a Qt app (e.g. Qt663QWindowIcon)


def is_line_exe(exe):
    e = (exe or "").lower()
    return e.startswith("line") and e.endswith(".exe")


def line_windows(wins):
    return [w for w in wins if is_line_class(w.cls) and is_line_exe(w.exe)]


def line_chat_windows(wins):
    return [w for w in line_windows(wins)
            if w.visible and w.title.strip() and w.title.strip() != LINE_MAIN_TITLE]


def target_windows(wins, cloaked=None):
    cloaked = cloaked or is_cloaked
    return [w for w in wins
            if w.visible and w.title.strip() and w.cls not in SHELL_CLASSES and not cloaked(w.hwnd)]


def squash(s):
    return " ".join((s or "").split())


def paste_ok(expected, got, mention=False):
    """is the LINE input box holding exactly the message? (with --mention: '@<member> <message>')"""
    e, g = squash(expected), squash(got)
    if not e or not g:
        return False
    if mention:                                     # LINE shows the member's display name, so only the "@" is checked
        return g[:1] in ("@", "＠") and len(g) > len(e) and g.endswith(e)
    return g == e


def line_input_point(rect, dpi):
    left, top, right, bottom = rect
    offset = int(LINE_INPUT_OFFSET * (dpi or 96) / 96 + 0.5)
    return left + (right - left) // 2, bottom - offset


def paste_settle(text):
    """seconds to let the target take a paste before Enter / before the clipboard is put back"""
    return 0.6 + min(len(text) / 5000.0, 1.4)


COMMANDS = {                                       # command: (flags, options with a value)
    "window": ({"--no-enter"}, {"--file"}),
    "line": ({"--no-enter"}, {"--file", "--mention"}),
    "list": (set(), set()),
    "selftest": (set(), set()),
}
ALL_OPTIONS = {"--no-enter", "--file", "--mention"}
_OPTION_TYPO = re.compile(r"^[-‐-―−－]+([A-Za-z][A-Za-z-]*)$")


def parse_args(args):
    if not args:
        raise UsageError("no command given")
    cmd = args[0].lower()
    if cmd in ("-h", "--help", "help", "/?"):
        return {"cmd": "help"}
    if cmd not in COMMANDS:
        raise UsageError("unknown command %r" % args[0])
    flags, opts = COMMANDS[cmd]
    a = {"cmd": cmd, "target": None, "text": None, "file": None, "mention": None, "enter": True}
    pos, only_pos, i = [], False, 1
    while i < len(args):
        tok = args[i]
        i += 1
        if only_pos or not tok.startswith("--"):
            pos.append(tok)
            continue
        if tok == "--":
            only_pos = True
            continue
        name, eq, val = tok.partition("=")
        if name in flags:
            if eq:
                raise UsageError("%s takes no value" % name)
            a["enter"] = False                     # the only flag is --no-enter
            continue
        if name in opts:
            if not eq:
                if i >= len(args):
                    raise UsageError("%s needs a value" % name)
                val = args[i]
                i += 1
            if not val.strip():
                raise UsageError("%s needs a non-empty value" % name)
            a[name[2:]] = val
            continue
        if name in ALL_OPTIONS:
            raise UsageError("%s cannot be used with %s" % (name, cmd))
        raise UsageError("unknown option %s (put -- before a text that starts with --)" % tok)
    if cmd in ("list", "selftest"):
        if pos:
            raise UsageError("%s takes no arguments" % cmd)
        return a
    if not pos or not pos[0].strip():
        raise UsageError("missing the %s" % ("window title" if cmd == "window" else "chat name"))
    a["target"] = pos[0]
    if len(pos) > 2:
        raise UsageError("too many arguments; put the whole text in one pair of quotes")
    if a["file"] is not None:
        if len(pos) == 2:
            raise UsageError("give the text or --file, not both")
    elif len(pos) < 2:
        raise UsageError("missing the text (or --file <utf-8 file>)")
    else:
        a["text"] = pos[1]
    for tok in pos:                                 # "-no-enter" / "—mention" typos must not become the message
        if not only_pos and _OPTION_TYPO.match(tok) and "--" + _OPTION_TYPO.match(tok).group(1).lower() in ALL_OPTIONS:
            raise UsageError("%r looks like a mistyped option; use --%s (or put -- before the text)"
                             % (tok, _OPTION_TYPO.match(tok).group(1).lower()))
    return a


# ---------------------------------------------------------------- windows
def fg():
    return int(user32.GetForegroundWindow() or 0)


def window_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd):
    pid = DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_tid(hwnd):
    return user32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0


_exe_cache = {}


def process_exe(pid):
    if pid not in _exe_cache:
        name = ""
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid) if pid else None
        if h:
            try:
                buf = ctypes.create_unicode_buffer(32768)
                n = DWORD(len(buf))
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                    name = os.path.basename(buf.value)
            finally:
                kernel32.CloseHandle(h)
        _exe_cache[pid] = name
    return _exe_cache[pid]


def is_cloaked(hwnd):
    """hidden by DWM: other virtual desktops, suspended Store apps"""
    if not DwmGetWindowAttribute:
        return False
    v = DWORD(0)
    try:
        return DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)) == 0 and v.value != 0
    except OSError:
        return False


def enum_windows():
    hwnds = []

    def cb(h, _):
        if h:
            hwnds.append(int(h))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    out = []
    for h in hwnds:
        cls = window_class(h)
        visible = bool(user32.IsWindowVisible(h))
        if not visible and not is_line_class(cls):     # hidden windows only tell whether LINE is running
            continue
        pid = window_pid(h)
        out.append(Win(h, window_title(h), cls, visible, bool(user32.IsIconic(h)), pid, process_exe(pid)))
    return out


def window_rect(hwnd):
    r = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        raise SendFail("could not read the window position")
    return r.left, r.top, r.right, r.bottom


def dpi_for(hwnd):
    if GetDpiForWindow:
        return GetDpiForWindow(hwnd) or 96
    return 96


def root_at(x, y):
    h = user32.WindowFromPoint(POINT(x, y))
    return int(user32.GetAncestor(h, GA_ROOT) or 0) if h else 0


def make_dpi_aware():
    """real pixels everywhere, so window positions and mouse positions agree on scaled / mixed-DPI screens"""
    try:
        if SetProcessDpiAwarenessContext and SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except OSError:
        pass
    try:
        if ctypes.WinDLL("shcore").SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except (OSError, AttributeError):
        pass
    try:
        if SetProcessDPIAware and SetProcessDPIAware():
            return "system"
    except OSError:
        pass
    return "unchanged"


# ---------------------------------------------------------------- keyboard / mouse
def _key(vk, up=False):
    i = INPUT()
    i.type = INPUT_KEYBOARD
    i.ki.wVk = vk
    i.ki.wScan = user32.MapVirtualKeyW(vk, 0) & 0xFF        # MAPVK_VK_TO_VSC
    i.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    return i


def _uni(ch, up=False):
    i = INPUT()
    i.type = INPUT_KEYBOARD
    i.ki.wVk = 0
    i.ki.wScan = ord(ch)
    i.ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return i


def _mouse(flags):
    i = INPUT()
    i.type = INPUT_MOUSE
    i.mi.dwFlags = flags
    return i


def send_inputs(events):
    arr = (INPUT * len(events))(*events)
    n = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    if n != len(events):
        raise SendFail("Windows blocked the simulated input (SendInput %d/%d, error %d); is the screen locked?"
                       % (n, len(events), ctypes.get_last_error()))


def press(vk):
    send_inputs([_key(vk), _key(vk, True)])


def combo(*vks):
    """modifiers + key in one SendInput call, so a crash can never leave Ctrl held down"""
    send_inputs([_key(v) for v in vks] + [_key(v, True) for v in reversed(vks)])


def type_char(ch):
    """pyautogui style (the proven path): the keyboard layout's key + Shift/Ctrl/Alt.
    WINSEND_AT_KEY=unicode, or a layout without the character, sends a Unicode character event instead."""
    if os.environ.get("WINSEND_AT_KEY", "").strip().lower() != "unicode":
        r = user32.VkKeyScanW(ch)
        if r != -1:
            vk, state = r & 0xFF, (r >> 8) & 0xFF
            mods = [m for bit, m in ((4, VK_MENU), (2, VK_CONTROL), (1, VK_SHIFT)) if state & bit]
            combo(*(mods + [vk]))
            return
    send_inputs([_uni(ch), _uni(ch, True)])


def alt_tap():
    try:
        press(VK_MENU)
    except SendFail:
        pass


def alt_reset():
    """two ALT taps reset the foreground lock before SetForegroundWindow"""
    for _ in range(2):
        try:
            send_inputs([_key(VK_MENU)])
            time.sleep(0.05)
            send_inputs([_key(VK_MENU, True)])
        except SendFail:
            pass
        time.sleep(0.1)


def click(x, y):
    if not user32.SetCursorPos(x, y):
        raise SendFail("could not move the mouse to the LINE input box")
    time.sleep(0.05)
    if user32.GetSystemMetrics(SM_SWAPBUTTON):      # left-handed mouse: the primary button is the right one
        send_inputs([_mouse(MOUSEEVENTF_RIGHTDOWN), _mouse(MOUSEEVENTF_RIGHTUP)])
    else:
        send_inputs([_mouse(MOUSEEVENTF_LEFTDOWN), _mouse(MOUSEEVENTF_LEFTUP)])


# ---------------------------------------------------------------- foreground
def foreground_v2(hwnd):
    """minimize->restore, ALT tap, AttachThreadInput + BringWindowToTop + SetForegroundWindow,
    SwitchToThisWindow, SetActiveWindow + SetFocus"""
    cur = kernel32.GetCurrentThreadId()
    fg_tid = window_tid(fg())
    tgt_tid = window_tid(hwnd)
    if not user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        time.sleep(0.05)
    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.08)
    if fg() == hwnd:
        return True
    alt_tap()
    att_fg = att_tgt = False
    try:
        if fg_tid and fg_tid != cur:
            att_fg = bool(user32.AttachThreadInput(cur, fg_tid, True))
        if tgt_tid and tgt_tid != cur:
            att_tgt = bool(user32.AttachThreadInput(cur, tgt_tid, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SwitchToThisWindow(hwnd, True)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if att_fg:
            user32.AttachThreadInput(cur, fg_tid, False)
        if att_tgt:
            user32.AttachThreadInput(cur, tgt_tid, False)
    time.sleep(0.12)
    return fg() == hwnd


def foreground_fallback(hwnd):
    """restore, attach the target thread to the foreground thread, then briefly zero the foreground lock timeout"""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.4)
    user32.IsGUIThread(True)
    fg_tid = window_tid(fg())
    tgt_tid = window_tid(hwnd)
    my_tid = kernel32.GetCurrentThreadId()
    att_fg = att_tgt = False
    try:
        if fg_tid and fg_tid != my_tid:
            att_fg = bool(user32.AttachThreadInput(my_tid, fg_tid, True))
        if fg_tid and tgt_tid and tgt_tid != fg_tid and tgt_tid != my_tid:
            att_tgt = bool(user32.AttachThreadInput(tgt_tid, fg_tid, True))
        user32.BringWindowToTop(hwnd)
        user32.ShowWindow(hwnd, SW_SHOW)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.3)
    finally:
        if att_fg:
            user32.AttachThreadInput(my_tid, fg_tid, False)
        if att_tgt:
            user32.AttachThreadInput(tgt_tid, fg_tid, False)
    if fg() == hwnd:
        return True
    orig = DWORD(0)
    if not user32.SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(orig), 0):
        return False
    try:
        user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, None, SPIF_SENDCHANGE)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.3)
    finally:
        user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, orig.value or None, SPIF_SENDCHANGE)
    return fg() == hwnd


def activate(hwnd):
    if fg() == hwnd and not user32.IsIconic(hwnd):
        return True
    try:
        if foreground_v2(hwnd):
            return True
    except Exception as e:                          # keep going with the fallback
        log("foreground_v2 error: %r" % e)
    return foreground_fallback(hwnd)


def bring_to_front(hwnd, label, reset_lock):
    if reset_lock:
        alt_reset()
    for attempt in range(3):
        ok = activate(hwnd)
        log("activate attempt %d: ok=%s foreground=%s target=%s" % (attempt, ok, fg(), hwnd))
        if ok:
            return
        time.sleep(0.5)
        if attempt == 1:
            alt_reset()
    raise SendFail("could not bring %r to the front after 3 tries (screen locked? a full-screen program or an "
                   "administrator window in front?)" % label)


def ensure_fg(hwnd, step):
    """last check before each key: the target must be in front; waits up to 1 s, never grabs the focus again"""
    for _ in range(10):
        if fg() == hwnd:
            return
        time.sleep(0.1)
    raise SendFail("another window came to the front before %s; stopped there" % step, focus_lost=True)


def focus_back(prev, target):
    """hand the focus back to the window the user was in"""
    if not prev or prev == target or fg() == prev:
        return
    if not user32.IsWindow(prev) or not user32.IsWindowVisible(prev) or user32.IsIconic(prev):
        return
    alt_tap()
    cur = kernel32.GetCurrentThreadId()
    fg_tid = window_tid(fg())
    attached = False
    try:
        if fg_tid and fg_tid != cur:
            attached = bool(user32.AttachThreadInput(cur, fg_tid, True))
        user32.BringWindowToTop(prev)
        user32.SetForegroundWindow(prev)
    finally:
        if attached:
            user32.AttachThreadInput(cur, fg_tid, False)
    time.sleep(0.15)
    log("focus back to the previous window: %s" % ("ok" if fg() == prev else "not confirmed"))


# ---------------------------------------------------------------- clipboard
def open_clipboard():
    for _ in range(20):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise SendFail("the clipboard is busy (another program keeps it open); try again")


def _put(fmt, data):
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return False
    p = kernel32.GlobalLock(h)
    if not p:
        kernel32.GlobalFree(h)
        return False
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    if not user32.SetClipboardData(fmt, h):        # on success the clipboard owns h
        kernel32.GlobalFree(h)
        return False
    return True


def set_clipboard(text):
    """put text on the clipboard (kept out of clipboard history / cloud clipboard); '' just empties it"""
    open_clipboard()
    try:
        if not user32.EmptyClipboard():
            raise SendFail("could not clear the clipboard")
        if not text:
            return
        if not _put(CF_UNICODETEXT, (text + "\0").encode("utf-16-le", errors="surrogatepass")):
            raise SendFail("could not put the text on the clipboard")
        for name in NO_HISTORY_FORMATS:
            fmt = user32.RegisterClipboardFormatW(name)
            if fmt:
                _put(fmt, b"\0\0\0\0")
    finally:
        user32.CloseClipboard()


def get_clipboard_text():
    """the clipboard's text, or None when it holds no text"""
    open_clipboard()
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            n = kernel32.GlobalSize(h) // 2
            return ctypes.wstring_at(p, n).split("\0", 1)[0] if n else ""
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def restore_clipboard(saved):
    try:
        set_clipboard(saved or "")
    except SendFail as e:
        log("clipboard restore failed: %s" % e.reason)


# ---------------------------------------------------------------- one send at a time
class SendLock:
    def __enter__(self):
        self.h = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not self.h:
            raise SendFail("could not create the send lock (error %d)" % ctypes.get_last_error())
        r = kernel32.WaitForSingleObject(self.h, MUTEX_WAIT_MS)
        if r not in (WAIT_OBJECT_0, WAIT_ABANDONED):
            kernel32.CloseHandle(self.h)
            raise SendFail("another winsend is still running (waited %d s); try again later" % (MUTEX_WAIT_MS // 1000))
        return self

    def __exit__(self, *exc):
        kernel32.ReleaseMutex(self.h)
        kernel32.CloseHandle(self.h)
        return False


# ---------------------------------------------------------------- window mode
def send_window(query, raw_text, enter=True):
    text = prep_window_text(raw_text)
    if not text:
        raise SendFail("the text is empty")
    w, err = match_title(target_windows(enum_windows()), query, key=lambda x: x.title)
    if err:
        raise SendFail(err + " (see: winsend.py list)")
    hwnd = w.hwnd
    log("window: %r hwnd=%s exe=%s, %d chars, enter=%s: %s" % (w.title, hwnd, w.exe, len(text), enter, preview(text)))
    prev = fg()
    saved = get_clipboard_text()
    state = {"touched": False, "maybe": False, "focus_lost": False}
    note = ""
    try:
        bring_to_front(hwnd, w.title, reset_lock=False)
        time.sleep(0.5)
        state["touched"] = True
        set_clipboard(text)
        ensure_fg(hwnd, "pasting")
        combo(VK_CONTROL, VK_V)
        time.sleep(paste_settle(text))
        if enter:
            ensure_fg(hwnd, "pressing Enter (the text was pasted into the window but not sent)")
            state["maybe"] = True
            press(VK_RETURN)
            time.sleep(0.2)
            if fg() != hwnd:
                note = " (note: another window came to the front right after Enter; check that it arrived)"
                log("window: foreground changed right after Enter")
        time.sleep(0.1)
    except SendFail as e:
        e.maybe_sent = e.maybe_sent or state["maybe"]
        state["focus_lost"] = e.focus_lost
        raise
    except Exception as e:
        log("window: unexpected: " + traceback.format_exc())
        raise SendFail("unexpected %s: %s (details in winsend.log)" % (type(e).__name__, e),
                       maybe_sent=state["maybe"]) from e
    finally:
        if state["touched"]:
            restore_clipboard(saved)
        if not state["focus_lost"]:                 # never pull the focus away from a window the user chose
            focus_back(prev, hwnd)
    return "typed %d chars into %r%s%s" % (len(text), w.title,
                                           " and pressed Enter" if enter else " (Enter not pressed)", note)


# ---------------------------------------------------------------- LINE mode
def read_input_box(hwnd):
    """select all + copy in the input box; None when nothing was copied"""
    sentinel = "__WINSEND_CHECK_%s__" % uuid.uuid4().hex
    set_clipboard(sentinel)
    seq = user32.GetClipboardSequenceNumber()
    time.sleep(0.1)
    ensure_fg(hwnd, "checking the input box (Ctrl+A)")
    combo(VK_CONTROL, VK_A)
    time.sleep(0.2)
    ensure_fg(hwnd, "checking the input box (Ctrl+C)")
    combo(VK_CONTROL, VK_C)
    time.sleep(0.3)
    deadline = time.time() + 1.5
    while user32.GetClipboardSequenceNumber() == seq and time.time() < deadline:
        time.sleep(0.1)
    got = get_clipboard_text()
    if got is None or got.strip() == sentinel:
        return None
    return got


def clear_input(hwnd):
    ensure_fg(hwnd, "clearing the input box")
    combo(VK_CONTROL, VK_A)
    time.sleep(0.1)
    ensure_fg(hwnd, "clearing the input box")
    press(VK_BACK)
    time.sleep(0.1)


def send_line(query, raw_text, mention=None, enter=True):
    text = prep_line_text(raw_text)
    if not text:
        raise SendFail("the text is empty")
    mention = (mention or "").strip() or None
    wins = enum_windows()
    chats = line_chat_windows(wins)
    if not chats:
        if line_windows(wins):
            raise SendFail("LINE is running but no chat is open in a window of its own; open the chat %r as a "
                           "separate window first" % query)
        raise SendFail("LINE is not running (start LINE desktop, sign in, and open the chat %r in a window of its "
                       "own)" % query)
    w, err = match_title(chats, query, key=lambda x: x.title)
    if err:
        raise SendFail("%s; open LINE chat windows: %s" % (err, ", ".join(repr(c.title) for c in chats[:15])))
    hwnd = w.hwnd
    log("line: %r hwnd=%s exe=%s class=%s, %d chars, mention=%r, enter=%s: %s"
        % (w.title, hwnd, w.exe, w.cls, len(text), mention, enter, preview(text)))
    prev = fg()
    saved = get_clipboard_text()
    cursor = POINT()
    have_cursor = bool(user32.GetCursorPos(ctypes.byref(cursor)))
    state = {"touched": False, "maybe": False, "focus_lost": False}
    try:
        bring_to_front(hwnd, w.title, reset_lock=True)
        x, y = line_input_point(window_rect(hwnd), dpi_for(hwnd))
        if root_at(x, y) != hwnd:
            raise SendFail("the LINE input box (screen point %d,%d) is off-screen or covered by another window; "
                           "move the chat window fully onto the screen" % (x, y))
        ensure_fg(hwnd, "clicking the input box")
        click(x, y)
        time.sleep(0.4)
        ensure_fg(hwnd, "clearing the input box (Ctrl+A)")
        combo(VK_CONTROL, VK_A)
        time.sleep(0.1)
        ensure_fg(hwnd, "clearing the input box (Backspace)")
        press(VK_BACK)
        time.sleep(0.2)
        state["touched"] = True
        if mention:
            ensure_fg(hwnd, "typing @")
            type_char("@")
            time.sleep(0.4)
            set_clipboard(mention)
            time.sleep(0.1)
            ensure_fg(hwnd, "pasting the member name")
            combo(VK_CONTROL, VK_V)
            time.sleep(0.8)
            ensure_fg(hwnd, "picking the member (Enter)")
            state["maybe"] = True                   # without a member list this Enter sends "@name" on its own
            press(VK_RETURN)
            time.sleep(0.5)
            body = " " + text
        else:
            body = text
        set_clipboard(body)
        time.sleep(0.2)
        ensure_fg(hwnd, "pasting the message")
        combo(VK_CONTROL, VK_V)
        time.sleep(0.4 + min(len(body) / 5000.0, 1.0))
        got = read_input_box(hwnd)
        if not paste_ok(body, got, mention=bool(mention)):
            log("line: paste check failed: expected %s / got %s" % (preview(body), preview(got)))
            if got is not None:
                try:
                    clear_input(hwnd)
                except SendFail:
                    pass
            if mention and got is not None and squash(got) == squash(body):
                raise SendFail("the @mention of %r did not show up in the input box (LINE may have offered no member "
                               "list, so '@%s' may have been sent on its own); the message itself was NOT sent"
                               % (mention, mention))
            raise SendFail("paste check failed: the LINE input box did not hold exactly the message%s, so the "
                           "message was NOT sent" % (" (could not read it back)" if got is None else ""))
        log("line: paste check ok")
        if not enter:
            return "pasted into LINE chat %r and checked; NOT sent (--no-enter)" % w.title
        ensure_fg(hwnd, "pressing Enter to send (the message is in the input box but was not sent)")
        state["maybe"] = True
        press(VK_RETURN)
        time.sleep(0.3)
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        time.sleep(0.2)
        log("line: sent; chat window minimized=%s" % bool(user32.IsIconic(hwnd)))
        return "sent to LINE chat %r (%d chars%s)" % (w.title, len(text), ", @" + mention if mention else "")
    except SendFail as e:
        e.maybe_sent = e.maybe_sent or state["maybe"]
        state["focus_lost"] = e.focus_lost
        raise
    except Exception as e:
        log("line: unexpected: " + traceback.format_exc())
        raise SendFail("unexpected %s: %s (details in winsend.log)" % (type(e).__name__, e),
                       maybe_sent=state["maybe"]) from e
    finally:
        if state["touched"]:
            restore_clipboard(saved)
        if not state["focus_lost"]:                 # the user took over: leave their window and mouse alone
            if have_cursor:
                user32.SetCursorPos(cursor.x, cursor.y)
            if enter:
                focus_back(prev, hwnd)


# ---------------------------------------------------------------- list
def cmd_list():
    wins = enum_windows()
    targets = target_windows(wins)
    chats = line_chat_windows(wins)
    print('Windows (%d) - winsend.py window "<title>" "<text>":' % len(targets))
    for w in targets:
        print("  %s    [%s]%s" % (w.title, w.exe or "?", " (minimized)" if w.iconic else ""))
    print('LINE chat windows (%d) - winsend.py line "<chat name>" "<text>":' % len(chats))
    for w in chats:
        print("  %s%s" % (w.title, " (minimized)" if w.iconic else ""))
    if not chats:
        print("  (none: %s)" % ("LINE is running, but no chat is open in a window of its own" if line_windows(wins)
                                else "LINE is not running"))
    return 0


# ---------------------------------------------------------------- selftest (offline)
def selftest():
    count = [0]

    def check(cond, what):
        if not cond:
            raise AssertionError(what)
        count[0] += 1

    def usage_error(args):
        try:
            parse_args(args)
        except UsageError:
            return True
        return False

    try:
        p64 = ctypes.sizeof(ctypes.c_void_p) == 8
        check(ctypes.sizeof(INPUT) == (40 if p64 else 28), "sizeof(INPUT)=%d" % ctypes.sizeof(INPUT))
        check(ctypes.sizeof(KEYBDINPUT) == (24 if p64 else 16), "sizeof(KEYBDINPUT)=%d" % ctypes.sizeof(KEYBDINPUT))
        check(ctypes.sizeof(MOUSEINPUT) == (32 if p64 else 24), "sizeof(MOUSEINPUT)=%d" % ctypes.sizeof(MOUSEINPUT))
        check(INPUT.u.offset == (8 if p64 else 4), "INPUT union offset=%d" % INPUT.u.offset)
        check(ctypes.sizeof(POINT) == 8 and ctypes.sizeof(RECT) == 16, "POINT/RECT size")
        u = _uni("@", up=True)                      # builds a struct only; nothing is sent
        check(u.type == INPUT_KEYBOARD and u.ki.wVk == 0 and u.ki.wScan == 0x40
              and u.ki.dwFlags == KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, "unicode key event")
        m = _mouse(MOUSEEVENTF_LEFTDOWN)
        check(m.type == INPUT_MOUSE and m.mi.dwFlags == MOUSEEVENTF_LEFTDOWN and m.mi.dx == 0, "mouse event")

        # arguments
        a = parse_args(["window", "Notepad", "hi there"])
        check((a["cmd"], a["target"], a["text"], a["enter"]) == ("window", "Notepad", "hi there", True), a)
        check(parse_args(["window", "Notepad", "hi", "--no-enter"])["enter"] is False, "--no-enter")
        check(parse_args(["Window", "--no-enter", "Notepad", "hi"])["enter"] is False, "flag first, cmd case")
        a = parse_args(["line", "家人", "--file", "m.txt", "--mention", "阿明"])
        check((a["target"], a["text"], a["file"], a["mention"]) == ("家人", None, "m.txt", "阿明"), a)
        check(parse_args(["line", "家人", "x", "--mention=阿明"])["mention"] == "阿明", "--mention=")
        check(parse_args(["line", "家人", "-3 度"])["text"] == "-3 度", "single-dash text")
        check(parse_args(["line", "家人", "--", "--no-enter"])["text"] == "--no-enter", "-- separator")
        check(parse_args(["window", "T", "--", "-no-enter"])["text"] == "-no-enter", "-- allows option-like text")
        check(parse_args(["window", "T", "-hello"])["text"] == "-hello", "dash text that is not an option name")
        check(SendFail("x", focus_lost=True).focus_lost and not SendFail("x").maybe_sent, "SendFail flags")
        check(parse_args(["list"])["cmd"] == "list", "list")
        check(parse_args(["--help"])["cmd"] == "help", "help")
        for bad in ([], ["bogus"], ["window"], ["window", "  "], ["window", "a"], ["window", "a", "b", "c"],
                    ["line", "a", "b", "--file", "f"], ["line", "a", "b", "--mention"], ["line", "a", "b", "--mention", " "],
                    ["window", "a", "b", "--mention", "x"], ["line", "a", "b", "--nope"], ["list", "x"],
                    ["line", "a", "b", "--no-enter=1"], ["selftest", "--no-enter"],
                    ["window", "T", "-no-enter"], ["line", "a", "b", "—mention"], ["line", "a", "–file"]):
            check(usage_error(bad), "should be a usage error: %r" % (bad,))

        # title matching (fake titles)
        titles = ["Untitled - Notepad", "家人群組", "家人群組 (2)", "\U0001d40a\U0001d41a\U0001d422",  # bold "Kai"
                  "Kai's Lab", "Project Alpha", "project alpha notes", "Dup", "Dup", "ＡＢＣ會議"]
        ident = lambda t: t                         # noqa: E731

        def pick(q):
            return match_title(titles, q, ident)

        check(pick("Untitled - Notepad") == ("Untitled - Notepad", None), "exact")
        check(pick("家人群組")[0] == "家人群組", "exact beats contains")
        check(pick("Notepad")[0] == "Untitled - Notepad", "unique contains")
        check(pick(" kai ")[0] == "\U0001d40a\U0001d41a\U0001d422", "NFKC + case before contains")
        check(pick("project alpha")[0] == "Project Alpha", "case-insensitive exact before contains")
        check(pick("abc會議")[0] == "ＡＢＣ會議", "full-width title, half-width query")
        check(pick("alpha")[0] is None and "2 windows" in pick("alpha")[1], "ambiguous contains")
        check(pick("Dup")[0] is None and "exact title" in pick("Dup")[1], "duplicate exact titles")
        check(pick("群組")[0] is None, "ambiguous Chinese contains")
        check(pick("Zebra")[0] is None and "nothing matches" in pick("Zebra")[1], "no match")
        check(pick("   ")[0] is None, "empty query")

        # LINE / target window filters (fake windows)
        wins = [Win(1, "LINE", "Qt663QWindowIcon", True, False, 10, "LINE.exe"),
                Win(2, "家人群組", "Qt663QWindowIcon", True, True, 10, "LINE.exe"),
                Win(3, "家人群組", "Qt51514QWindowIcon", True, False, 20, "Telegram.exe"),
                Win(4, "舊聊天", "Qt663QWindowIcon", False, False, 10, "LINE.exe"),
                Win(5, "", "Qt663QWindowIcon", True, False, 10, "LINE.exe"),
                Win(6, "Program Manager", "Progman", True, False, 30, "explorer.exe"),
                Win(7, "Untitled - Notepad", "Notepad", True, False, 40, "notepad.exe"),
                Win(8, "Hidden", "Notepad", False, False, 40, "notepad.exe"),
                Win(9, "Settings", "ApplicationFrameWindow", True, False, 50, "ApplicationFrameHost.exe")]
        check([w.hwnd for w in line_chat_windows(wins)] == [2], "LINE chat filter")
        check([w.hwnd for w in line_windows(wins)] == [1, 2, 4, 5], "LINE process filter")
        check([w.hwnd for w in target_windows(wins, cloaked=lambda h: h == 9)] == [1, 2, 3, 7], "target filter")
        check(is_line_exe("LINE.exe") and is_line_exe("LineLauncher.exe") and not is_line_exe("Telegram.exe")
              and not is_line_exe(""), "LINE exe names")

        # text handling
        check(prep_window_text("a\r\nb\n\nc\td\x07 ") == "a b c d", repr(prep_window_text("a\r\nb\n\nc\td\x07 ")))
        check(prep_window_text("全形，標點！ＡＢＣ") == "全形，標點！ＡＢＣ", "window text keeps full-width")
        check(prep_window_text(" \n\r\n ") == "", "blank window text")
        check(prep_line_text("  第一行\r\n第二行\r第三\x07行\t!\u2028末  ") == "第一行\n第二行\n第三 行\t!\n末",
              repr(prep_line_text("  第一行\r\n第二行\r第三\x07行\t!\u2028末  ")))
        check(preview("a\nb" * 40, 11) == "a\\nba\\nba\\n...", preview("a\nb" * 40, 11))
        check(preview(None) == "None" and preview("短") == "短", "preview short/None")

        # paste check
        check(paste_ok("第一行\n第二行", "第一行\r\n第二行\r\n"), "CRLF / trailing newline")
        check(not paste_ok("第一行\n第二行", "第一行"), "truncated")
        check(not paste_ok("你好", "舊草稿你好"), "leftover draft")
        check(not paste_ok("你好", None) and not paste_ok("", ""), "nothing copied")
        check(paste_ok(" 你好", "@阿明 你好", mention=True), "mention")
        check(paste_ok(" 你好", "＠阿明 你好", mention=True), "mention with full-width @")
        check(not paste_ok(" 你好", "你好", mention=True), "mention missing")
        check(not paste_ok(" 你好", "舊草稿 你好", mention=True), "mention: leftover draft instead of @member")

        # click point
        check(line_input_point((100, 100, 900, 700), 96) == (500, 645), "click point 100%")
        check(line_input_point((100, 100, 900, 700), 144) == (500, 617), "click point 150%")
        check(line_input_point((-1920, 0, -1000, 800), 120) == (-1460, 731), "click point 125%, left monitor")
        check(abs(paste_settle("x") - 0.6002) < 1e-6 and abs(paste_settle("x" * 100000) - 2.0) < 1e-9, "paste settle")
    except AssertionError as e:
        print("FAIL: selftest: %s" % (e,))
        return 1
    print("OK: selftest passed (%d checks; no window, clipboard, mouse or keyboard was touched)" % count[0])
    return 0


# ---------------------------------------------------------------- CLI
def _setup_stdout():
    for s in (sys.stdout, sys.stderr):
        try:
            if os.environ.get("PYTHONIOENCODING") or s.isatty():
                s.reconfigure(errors="replace")
            else:                                   # piped into an AI tool: UTF-8 instead of the ANSI code page
                s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def read_text_file(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except UnicodeDecodeError:
        raise SendFail("--file %s is not UTF-8 text" % path)
    except OSError as e:
        raise SendFail("cannot read --file %s: %s" % (path, e.strerror or e))


def main(argv):
    _setup_stdout()
    try:
        a = parse_args(argv[1:])
    except UsageError as e:
        print("FAIL: %s (see: winsend.py --help)" % e)
        return 1
    if a["cmd"] == "help":
        print(__doc__)
        return 0
    if a["cmd"] == "selftest":
        return selftest()
    make_dpi_aware()
    if a["cmd"] == "list":
        try:
            return cmd_list()
        except Exception as e:
            log("list failed: " + traceback.format_exc())
            print("FAIL: could not list windows: %s: %s" % (type(e).__name__, e))
            return 1
    try:
        text = read_text_file(a["file"]) if a["file"] is not None else a["text"]
        with SendLock():
            if a["cmd"] == "window":
                result = send_window(a["target"], text, enter=a["enter"])
            else:
                result = send_line(a["target"], text, mention=a["mention"], enter=a["enter"])
    except SendFail as e:
        msg = e.reason + (MAYBE_SENT_NOTE if e.maybe_sent else "")
        log("FAIL: " + msg)
        say("FAIL: " + msg)
        return 1
    except Exception as e:                          # before any key was pressed (send_* wrap their own errors)
        log("FAIL (unexpected): " + traceback.format_exc())
        say("FAIL: unexpected error %s: %s (details in winsend.log)" % (type(e).__name__, e))
        return 1
    log("OK: " + result)                            # outside the try: reporting can never turn a send into a FAIL
    say("OK: " + result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
