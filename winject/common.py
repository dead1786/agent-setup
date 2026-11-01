"""winject shared code: paths, files, targets, the queue, text rules, log rotation, kernel32 helpers.

Standard library only. Nothing in this module touches a window, the clipboard, the mouse or the keyboard;
the typing itself is done by winsend.py, called from dispatcher.py.

Layout (agent-setup installs this package as C:\\AI\\.agent\\winject\\):
  C:\\AI\\.agent\\winsend.py, tgterm.py, config.json     the helpers this package uses
  C:\\AI\\.agent\\winject\\targets.json                  default targets (shipped, never edited)
  C:\\AI\\.agent\\winject-data\\                         everything that changes (created on demand):
      targets.json                                     the targets in use (copied from the defaults on first edit)
      queue\\high\\*.json, queue\\normal\\*.json          one file per queued message, oldest first
      queue\\inflight\\*.json                           the message being sent right now (at most one)
      done.jsonl, failed.jsonl                         results (one JSON object per line)
      archive\\                                         rotated done/failed logs (kept ARCHIVE_DAYS days)
      dispatcher.log, heartbeat.json, paused, stop, alert-state.json
The data folder can be moved with the environment variable WINJECT_DATA.
"""
import ctypes
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import uuid
import zlib
from ctypes import wintypes as wt

VERSION = "1.0"
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.dirname(PKG_DIR)

PRIORITIES = ("high", "normal")                 # high is always taken first; a send in progress is never interrupted
TYPES = ("window", "line")
# sources whose messages go out without the "[source→target] " prefix (compared before the first ':')
NO_PREFIX_SOURCES = frozenset(("system", "scheduler", "alarm", "reminder", "raw"))
RESERVED = frozenset(("send", "status", "monitor", "targets", "add-target", "remove-target", "test-target",
                      "ensure-dispatcher", "stop-dispatcher", "pause", "resume", "cancel", "archive", "selftest",
                      "help", "all"))
MAX_TEXT = 20000
MAX_RETRIES = 3                                 # 1 try + up to 3 retries
RETRY_DELAYS = {"other": (2, 4, 8),             # focus trouble, busy clipboard, another send still running ...
                "user": (10, 20, 40),           # the user clicked / typed elsewhere during the send
                "missing": (15, 30, 60)}        # window / LINE chat not open (the watchdog reopens the AI window in ~1 min)
MAX_DELAY = max(max(v) for v in RETRY_DELAYS.values())
BAD_FILE_GRACE = 30                             # seconds a queue file may stay unreadable (still being written)
DISPATCHER_MUTEX = "Local\\agent-winject-dispatcher"
HB_INTERVAL = 5
HB_STALE = 120                                  # heartbeat thread silent this long -> the dispatcher is not responding
LOOP_STALE = 600                                # main loop stuck this long (a send takes < 2 min even when waiting)
DONE_MAX = 2_000_000
FAILED_MAX = 1_000_000
LOG_MAX = 1_000_000
ARCHIVE_DAYS = 30
TEMP_MAX_AGE = 3600
TEMP_SUFFIXES = (".tmp", ".cancel", ".bad")

TARGETS_COMMENT = ("winject targets. type: window (a window title, typed with winsend window) or line (a LINE chat "
                   "opened as its own window). title: the whole window title (matched exactly, ignoring case and "
                   "full/half width) or title_from_config: a key of config.json. priority: high|normal. prefix: "
                   "add '[source→target] ' in front. Edit with: winject\\cli.py add-target / remove-target.")


class Paths:
    def __init__(self, data=None, agent=None):
        j = os.path.join
        self.agent = os.path.abspath(agent or AGENT_DIR)
        self.data = os.path.abspath(data or os.environ.get("WINJECT_DATA") or j(self.agent, "winject-data"))
        self.queue = j(self.data, "queue")
        self.qdir = {pr: j(self.queue, pr) for pr in PRIORITIES}
        self.inflight = j(self.queue, "inflight")
        self.done = j(self.data, "done.jsonl")
        self.failed = j(self.data, "failed.jsonl")
        self.archive = j(self.data, "archive")
        self.log = j(self.data, "dispatcher.log")
        self.heartbeat = j(self.data, "heartbeat.json")
        self.targets = j(self.data, "targets.json")
        self.paused = j(self.data, "paused")
        self.stop = j(self.data, "stop")
        self.alert_state = j(self.data, "alert-state.json")
        self.default_targets = j(PKG_DIR, "targets.json")
        self.config = j(self.agent, "config.json")
        self.agent_paused = j(self.agent, "paused")
        self.tgterm = j(self.agent, "tgterm.py")
        self.winsend = j(self.agent, "winsend.py")

    def lock_name(self):
        """one cross-process lock per data folder, for appending to done/failed and rotating them"""
        return "Local\\agent-winject-files-%08x" % (zlib.crc32(os.path.normcase(self.data).encode("utf-8")) & 0xFFFFFFFF)


# ---------------------------------------------------------------- small helpers
def now_iso(t=None):
    return dt.datetime.fromtimestamp(time.time() if t is None else t).isoformat(timespec="seconds")


def preview(s, n=80):
    s = str(s or "").replace("\r", " ").replace("\n", " ")
    return s[:n] + ("..." if len(s) > n else "")


def setup_stdout():
    """UTF-8 when piped into an AI tool (instead of the ANSI code page); never crash on an odd character"""
    for s in (sys.stdout, sys.stderr):
        if s is None:                               # pythonw
            continue
        try:
            if os.environ.get("PYTHONIOENCODING") or s.isatty():
                s.reconfigure(errors="replace")
            else:
                s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def say(line=""):
    try:
        print(line, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- kernel32 (locks and processes only)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _bind(name, restype, *argtypes):
    fn = getattr(_k32, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_CreateMutexW = _bind("CreateMutexW", wt.HANDLE, ctypes.c_void_p, wt.BOOL, wt.LPCWSTR)
_OpenMutexW = _bind("OpenMutexW", wt.HANDLE, wt.DWORD, wt.BOOL, wt.LPCWSTR)
_WaitForSingleObject = _bind("WaitForSingleObject", wt.DWORD, wt.HANDLE, wt.DWORD)
_ReleaseMutex = _bind("ReleaseMutex", wt.BOOL, wt.HANDLE)
_CloseHandle = _bind("CloseHandle", wt.BOOL, wt.HANDLE)
_OpenProcess = _bind("OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
_GetExitCodeProcess = _bind("GetExitCodeProcess", wt.BOOL, wt.HANDLE, ctypes.POINTER(wt.DWORD))
_QueryFullProcessImageNameW = _bind("QueryFullProcessImageNameW", wt.BOOL, wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                    ctypes.POINTER(wt.DWORD))
_GetProcessTimes = _bind("GetProcessTimes", wt.BOOL, wt.HANDLE, *([ctypes.POINTER(wt.FILETIME)] * 4))
_TerminateProcess = _bind("TerminateProcess", wt.BOOL, wt.HANDLE, wt.UINT)
_GetStdHandle = _bind("GetStdHandle", wt.HANDLE, wt.DWORD)
_GetConsoleMode = _bind("GetConsoleMode", wt.BOOL, wt.HANDLE, ctypes.POINTER(wt.DWORD))
_SetConsoleMode = _bind("SetConsoleMode", wt.BOOL, wt.HANDLE, wt.DWORD)

ERROR_ALREADY_EXISTS, ERROR_ACCESS_DENIED = 183, 5
SYNCHRONIZE, PROCESS_TERMINATE, PROCESS_QUERY_LIMITED_INFORMATION = 0x00100000, 0x0001, 0x1000
STILL_ACTIVE, WAIT_OBJECT_0, WAIT_ABANDONED = 259, 0x0, 0x80


def acquire_mutex(name):
    """a handle when this process is the first to create the named mutex, else None (held until closed/exit)"""
    h = _CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    if not h:
        return None
    if err == ERROR_ALREADY_EXISTS:
        _CloseHandle(h)
        return None
    return h


def mutex_exists(name):
    h = _OpenMutexW(SYNCHRONIZE, False, name)
    if h:
        _CloseHandle(h)
        return True
    return ctypes.get_last_error() == ERROR_ACCESS_DENIED     # exists, created by an elevated process


def close_handle(h):
    if h:
        _CloseHandle(h)


class FileLock:
    """cross-process lock (named mutex); after 10 s it goes on without the lock rather than block forever"""
    def __init__(self, p):
        self.name = p.lock_name()
        self.h, self.owned = None, False

    def __enter__(self):
        self.h = _CreateMutexW(None, False, self.name)
        if self.h:
            self.owned = _WaitForSingleObject(self.h, 10000) in (WAIT_OBJECT_0, WAIT_ABANDONED)
        return self

    def __exit__(self, *exc):
        if self.h:
            if self.owned:
                _ReleaseMutex(self.h)
            _CloseHandle(self.h)
        return False


def process_info(pid):
    """(exe path, creation time as epoch seconds) of a live process, else None"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    h = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        code = wt.DWORD()
        if not _GetExitCodeProcess(h, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        buf = ctypes.create_unicode_buffer(32768)
        n = wt.DWORD(len(buf))
        if not _QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return None
        t = [wt.FILETIME() for _ in range(4)]
        if not _GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
            return None
        ticks = (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
        return buf.value, ticks / 1e7 - 11644473600         # FILETIME (1601, 100 ns) -> Unix epoch
    finally:
        _CloseHandle(h)


def terminate_process(pid, wait_ms=5000):
    h = _OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, int(pid))
    if not h:
        return False
    try:
        ok = bool(_TerminateProcess(h, 1))
        if ok:
            _WaitForSingleObject(h, wait_ms)
        return ok
    finally:
        _CloseHandle(h)


def enable_vt():
    """let a classic console understand the ANSI clear-screen codes used by monitor"""
    try:
        h = _GetStdHandle(wt.DWORD(-11 & 0xFFFFFFFF))       # STD_OUTPUT_HANDLE
        mode = wt.DWORD()
        if h and _GetConsoleMode(h, ctypes.byref(mode)):
            _SetConsoleMode(h, mode.value | 0x0004)          # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except OSError:
        pass


# ---------------------------------------------------------------- files
def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj, indent=None):
    """atomic: write a temp file next to it, then replace (retried while a reader has the file open)"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    for i in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 19:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            time.sleep(0.05)


def append_jsonl(p, path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with FileLock(p):
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)


def tail_jsonl(path, n=None, max_bytes=524288):
    """the last n JSON objects of a .jsonl file (reads at most max_bytes from the end)"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]                           # the first line was cut in the middle
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out[-n:] if n else out


def count_lines(path):
    try:
        with open(path, "rb") as f:
            return sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(65536), b""))
    except OSError:
        return 0


def dlog(p, msg):
    """dispatcher.log (also used by ensure-dispatcher); rotated at LOG_MAX to dispatcher.log.1"""
    try:
        os.makedirs(p.data, exist_ok=True)
        if os.path.exists(p.log) and os.path.getsize(p.log) > LOG_MAX:
            os.replace(p.log, p.log + ".1")
        with open(p.log, "a", encoding="utf-8", errors="backslashreplace") as f:
            f.write("%s [%d] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), msg))
    except Exception:                               # logging must never break a send
        pass


# ---------------------------------------------------------------- targets
_BAD_NAME = re.compile(r'[\s"\'`\\/:*?<>|\x00-\x1f\x7f]')


def check_name(name):
    """None when the target name is usable, else the reason"""
    if not name or len(name) > 40:
        return "a target name must be 1-40 characters"
    if _BAD_NAME.search(name):
        return "a target name cannot contain spaces, quotes or \\ / : * ? < > |"
    if name[0] in "-_":
        return "a target name cannot start with - or _"
    if name.lower() in RESERVED:
        return "%r is a command name; pick another target name" % name
    return None


def load_targets(p):
    """{name: target}. The file in the data folder when it exists, else the shipped defaults.
    Raises ValueError when the data-folder file exists but cannot be read (never silently drops targets)."""
    if os.path.exists(p.targets):
        data = read_json(p.targets, None)
        if not isinstance(data, dict):
            raise ValueError("%s is not valid JSON; fix or delete it (deleting brings back the defaults)" % p.targets)
    else:
        data = read_json(p.default_targets, {})
        if not isinstance(data, dict):
            data = {}
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


def save_targets(p, targets):
    data = {"_comment": TARGETS_COMMENT}
    data.update((k, targets[k]) for k in sorted(targets))
    write_json(p.targets, data, indent=2)


def add_target(p, name, title, kind="window", priority="normal", prefix=None, classes=None, halfwidth=False,
               note=None, replace=False):
    """(ok, message)"""
    err = check_name(name)
    if err:
        return False, err
    title = " ".join(str(title or "").split())
    if not title or len(title) > 200:
        return False, "the window title / chat name must be 1-200 characters"
    if kind not in TYPES:
        return False, "type must be window or line"
    if priority not in PRIORITIES:
        return False, "priority must be high or normal"
    targets = load_targets(p)
    existed = name in targets
    if existed and not replace:
        return False, "target %r already exists (add --replace to overwrite it)" % name
    t = {"type": kind, "title": title, "priority": priority,
         "prefix": (kind == "window") if prefix is None else bool(prefix)}
    classes = [x.strip() for x in (classes or []) if x and x.strip()]
    if classes and kind == "window":
        t["class_whitelist"] = classes
    if halfwidth:
        t["halfwidth"] = True
    if note:
        t["note"] = str(note)
    targets[name] = t
    save_targets(p, targets)
    return True, ("replaced" if existed else "added") + " target %r" % name


def remove_target(p, name):
    targets = load_targets(p)
    if name not in targets:
        return False, "no target named %r" % name
    del targets[name]
    save_targets(p, targets)
    return True, "removed target %r" % name


def resolve_title(p, t):
    """(title, where it came from) or (None, reason)"""
    key = t.get("title_from_config")
    if key:
        cfg = read_json(p.config, None)
        if not isinstance(cfg, dict):
            return None, "config.json is missing or invalid (%s)" % p.config
        v = " ".join(str(cfg.get(key) or "").split())
        if not v:
            return None, "config.json has no %r" % key
        return v, "config.json %s" % key
    v = " ".join(str(t.get("title") or "").split())
    return (v, "stored") if v else (None, "the target has no title")


def describe_target(p, name, t):
    kind = t.get("type", "window")
    title, where = resolve_title(p, t)
    shown = ("%r (%s)" % (title, where)) if title and where != "stored" else (repr(title) if title else "?? " + where)
    bits = ["%-14s [%s]" % (name, kind), ("chat " if kind == "line" else "title ") + shown,
            "priority " + str(t.get("priority", "normal")),
            "prefix " + ("on" if t.get("prefix", kind == "window") else "off")]
    if t.get("class_whitelist"):
        bits.append("class " + "|".join(t["class_whitelist"]))
    if t.get("halfwidth"):
        bits.append("halfwidth")
    if t.get("note"):
        bits.append("- " + str(t["note"]))
    return ", ".join(bits)


# ---------------------------------------------------------------- queue
def pause_reason(p):
    if os.path.exists(p.paused):
        return "winject is paused (run: winject\\cli.py resume)"
    if os.path.exists(p.agent_paused):
        return "the whole agent is paused (%s exists)" % p.agent_paused
    return None


_last_ns = [0]
_ns_lock = threading.Lock()


def _queue_ns():
    """strictly increasing within this process, so two quick sends keep their order"""
    with _ns_lock:
        _last_ns[0] = max(time.time_ns(), _last_ns[0] + 1)
        return _last_ns[0]


def enqueue(p, target, text, source="unknown", priority="normal", mention=None, prefix=None):
    """put one message in the queue. {"ok": True, "id", "target", "priority"} or {"ok": False, "reason"}.
    ok means queued, not sent: the result shows up in done.jsonl / failed.jsonl (cli.py status)."""
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "reason": "empty_text"}
    if len(text) > MAX_TEXT:
        return {"ok": False, "reason": "text_too_long (max %d characters)" % MAX_TEXT}
    if priority not in PRIORITIES:
        return {"ok": False, "reason": "bad_priority %r (use high or normal)" % (priority,)}
    why = pause_reason(p)
    if why:
        return {"ok": False, "reason": "paused: " + why}
    try:
        targets = load_targets(p)
    except ValueError as e:
        return {"ok": False, "reason": "targets_unreadable: %s" % e}
    t = targets.get(target)
    if t is None:
        return {"ok": False, "reason": "unknown_target %r (known: %s)" % (target, ", ".join(sorted(targets)) or "none")}
    mention = " ".join(str(mention or "").split()) or None
    if mention and t.get("type", "window") != "line":
        return {"ok": False, "reason": "mention_only_for_line_targets"}
    if t.get("priority") == "high":
        priority = "high"
    rid = uuid.uuid4().hex[:16]
    source = " ".join(str(source or "").split())[:40] or "unknown"
    rec = {"id": rid, "target": target, "text": text, "source": source,
           "priority": priority, "enqueued": now_iso(), "attempts": 0, "not_before": 0}
    if mention:
        rec["mention"] = mention
    if prefix is False:
        rec["prefix"] = False
    path = os.path.join(p.qdir[priority], "%020d-%s.json" % (_queue_ns(), rid))
    try:
        write_json(path, rec)
    except OSError as e:
        return {"ok": False, "reason": "write_failed: %s" % e}
    return {"ok": True, "id": rid, "target": target, "priority": priority}


def queued(p):
    """[(priority, path)] of every queued message: high first, each oldest first"""
    out = []
    for prio in PRIORITIES:
        d = p.qdir[prio]
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
        except OSError:
            names = []
        out.extend((prio, os.path.join(d, n)) for n in names)
    return out


def inflight_items(p):
    try:
        return [os.path.join(p.inflight, n) for n in sorted(os.listdir(p.inflight)) if n.endswith(".json")]
    except OSError:
        return []


def normalize_item(rec, path):
    """fill defaults in place (files dropped in by other programs may be minimal); None or the reason it is bad"""
    if not isinstance(rec, dict):
        return "not a JSON object"
    if not isinstance(rec.get("target"), str) or not rec["target"].strip():
        return "no target"
    if not isinstance(rec.get("text"), str) or not rec["text"].strip():
        return "no text"
    if not rec.get("id"):
        rec["id"] = os.path.basename(path)[:-5]
    if not rec.get("source"):
        rec["source"] = "file"
    try:
        rec["attempts"] = int(rec.get("attempts") or 0)
        rec["not_before"] = float(rec.get("not_before") or 0)
    except (TypeError, ValueError):
        return "attempts / not_before are not numbers"
    return None


def quarantine(p, path, reason):
    """move an unusable queue file into failed.jsonl"""
    try:
        with open(path, "rb") as f:
            raw = f.read(2000).decode("utf-8", "replace")
    except OSError:
        raw = ""
    tmp = path + ".bad"
    try:
        os.replace(path, tmp)
    except OSError:
        return False
    name = os.path.basename(path)
    append_jsonl(p, p.failed, {"id": name[:-5], "file": name, "state": "failed", "failed": now_iso(),
                               "final_reason": reason, "maybe_sent": False, "raw": raw[:500]})
    try:
        os.remove(tmp)
    except OSError:
        pass
    return True


def _load_cached(path, cache):
    """(record or None when not valid JSON, stat); (None, None) when the file is gone or cannot be read right now"""
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    key = (st.st_mtime_ns, st.st_size)
    if cache is not None:
        hit = cache.get(path)
        if hit and hit[0] == key:
            return (dict(hit[1]) if hit[1] is not None else None), st
    try:
        with open(path, encoding="utf-8-sig") as f:
            rec = json.load(f)
    except OSError:
        return None, None                           # locked for a moment (antivirus, a reader): try next round
    except ValueError:
        rec = None
    rec = rec if isinstance(rec, dict) else None
    if cache is not None:
        if len(cache) > 5000:
            cache.clear()
        cache[path] = (key, rec)
    return (dict(rec) if rec is not None else None), st


def pick_next(p, now, cache=None):
    """(priority, path, record) of the next message to send, or None.
    A message waiting for its retry time holds back the later messages for the same target (keeps their order);
    messages for other targets go ahead."""
    blocked = set()
    for prio, path in queued(p):
        rec, st = _load_cached(path, cache)
        if st is None:
            continue                                # taken / cancelled meanwhile
        if rec is None:
            if now - st.st_mtime > BAD_FILE_GRACE:
                quarantine(p, path, "bad_request: not a valid JSON object")
            continue
        err = normalize_item(rec, path)
        if err:
            quarantine(p, path, "bad_request: " + err)
            continue
        if rec["target"] in blocked:
            continue
        if now < rec["not_before"] <= now + MAX_DELAY + 5:  # further ahead: the clock jumped back; do not wait for it
            blocked.add(rec["target"])
            continue
        return prio, path, rec
    return None


def cancel(p, which):
    """drop queued messages ('all' or one id) into failed.jsonl as cancelled; the one being sent is not touched"""
    n = 0
    for _prio, path in queued(p):
        rec = read_json(path, None)
        rid = rec.get("id") if isinstance(rec, dict) else None
        rid = rid or os.path.basename(path)[:-5]
        if which != "all" and rid != which and not os.path.basename(path)[:-5].endswith("-" + which):
            continue
        tmp = path + ".cancel"
        if not _claim(path, tmp):                   # gone: the dispatcher took it a moment ago
            continue
        _record_cancel(p, tmp, rec, rid)
        n += 1
    return n


def _claim(src, dst, tries=20):
    """rename src -> dst; retried while someone has src open for a moment; False when src is gone or stays locked"""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return True
        except FileNotFoundError:
            return False
        except PermissionError:
            time.sleep(0.05)
        except OSError:
            return False
    return False


def _record_cancel(p, tmp, rec, rid):
    out = dict(rec) if isinstance(rec, dict) else {"file": os.path.basename(tmp)[:-len(".cancel")]}
    if not out.get("id"):
        out["id"] = rid
    out.update(state="failed", failed=now_iso(), final_reason="cancelled", maybe_sent=False)
    append_jsonl(p, p.failed, out)
    try:
        os.remove(tmp)
    except OSError:
        pass


def recent_ids(p, n=2000):
    return {r.get("id") for path in (p.done, p.failed) for r in tail_jsonl(path, n) if r.get("id")}


def find_result(p, rid):
    """("done"|"failed", record) / ("queued"|"sending", record) / None"""
    for kind, path in (("done", p.done), ("failed", p.failed)):
        for r in reversed(tail_jsonl(path, 3000)):
            if r.get("id") == rid:
                return kind, r
    for path in inflight_items(p):
        if path.endswith("-%s.json" % rid):
            return "sending", read_json(path, {}) or {}
    for _prio, path in queued(p):
        if path.endswith("-%s.json" % rid):
            return "queued", read_json(path, {}) or {}
    return None


# ---------------------------------------------------------------- text rules
def halfwidth(text):
    """full-width ASCII letters/digits/punctuation and the ideographic space -> half-width; CJK text unchanged"""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def wants_prefix(rec, t):
    if rec.get("prefix") is False:
        return False
    if not t.get("prefix", t.get("type", "window") == "window"):
        return False
    src = str(rec.get("source") or "").strip()
    return bool(src) and src.split(":", 1)[0].strip().lower() not in NO_PREFIX_SOURCES


def compose_text(rec, t):
    """what is actually typed: '[source→target] text' (unless no prefix applies), half-width when asked"""
    text = rec["text"]
    if wants_prefix(rec, t):
        text = "[%s→%s] %s" % (str(rec.get("source")).strip(), rec["target"], text)
    if t.get("halfwidth"):
        text = halfwidth(text)
    return text


def classify(reason, focus_lost=False):
    """kind of a winsend failure: bad (never retried), missing, user, other"""
    r = (reason or "").lower()
    if "the text is empty" in r:
        return "bad"
    if any(s in r for s in ("nothing matches", "line is not running", "no chat is open in a window of its own")):
        return "missing"
    if focus_lost:
        return "user"
    return "other"


def backoff(kind, attempts):
    """seconds before the next try after `attempts` failed tries, or None to give up"""
    delays = RETRY_DELAYS.get(kind)
    if not delays or attempts < 1 or attempts > MAX_RETRIES:
        return None
    return delays[min(attempts, len(delays)) - 1]


def make_strict_matcher(orig_match, norm, classes=None):
    """a stand-in for winsend.match_title that only accepts windows whose WHOLE title equals the target title
    (ignoring case and full/half width) and, when given, whose window class is in `classes`.
    winsend's own 'title contains it' step is what a person wants at the keyboard, but an unattended queue
    must never type into some other window that merely contains the name."""
    classes = list(classes or [])

    def strict(items, query, key):
        q = norm(query)
        keep = [it for it in items if norm(key(it)) == q and (not classes or getattr(it, "cls", None) in classes)]
        if not keep:
            extra = "; the window class must be one of: %s" % ", ".join(classes) if classes else ""
            return None, ("nothing matches %r exactly (winject only types into a window whose whole title is "
                          "exactly this%s)" % (query, extra))
        return orig_match(keep, query, key)

    return strict


# ---------------------------------------------------------------- rotation / cleanup
def record_day(rec):
    for k in ("completed", "failed", "enqueued"):
        try:
            return dt.datetime.fromisoformat(str(rec.get(k))).date()
        except (TypeError, ValueError):
            continue
    return None


def _unique(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s-%d%s" % (base, i, ext)):
        i += 1
    return "%s-%d%s" % (base, i, ext)


def rotate_logs(p, now=None, force=False):
    """done.jsonl -> archive\\done-<day>.jsonl once it holds an earlier day (or is big);
    failed.jsonl -> archive when big; archives older than ARCHIVE_DAYS are deleted. Returns what was done."""
    now = time.time() if now is None else now
    today = dt.date.fromtimestamp(now)
    actions = []
    with FileLock(p):
        for path, prefix_, limit, daily in ((p.done, "done", DONE_MAX, True), (p.failed, "failed", FAILED_MAX, False)):
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size == 0:
                continue
            first = _first_record(path)
            day = record_day(first[0]) if first else None
            day = day or dt.date.fromtimestamp(os.path.getmtime(path))
            if force or size > limit or (daily and day < today):
                os.makedirs(p.archive, exist_ok=True)
                dest = _unique(os.path.join(p.archive, "%s-%s.jsonl" % (prefix_, day.isoformat())))
                try:
                    os.replace(path, dest)
                    actions.append("archived %s -> %s" % (os.path.basename(path), os.path.basename(dest)))
                except OSError as e:
                    actions.append("could not archive %s: %s" % (os.path.basename(path), e))
    try:
        names = os.listdir(p.archive)
    except OSError:
        names = []
    for n in names:
        fp = os.path.join(p.archive, n)
        try:
            if os.path.isfile(fp) and now - os.path.getmtime(fp) > ARCHIVE_DAYS * 86400:
                os.remove(fp)
                actions.append("deleted old archive %s" % n)
        except OSError:
            pass
    return actions


def _first_record(path):
    try:
        with open(path, "rb") as f:
            line = f.readline(65536)
        obj = json.loads(line.decode("utf-8", "replace"))
        return [obj] if isinstance(obj, dict) else []
    except (OSError, ValueError):
        return []


def cleanup_temp(p, now=None):
    """leftover temp files of writers that crashed"""
    now = time.time() if now is None else now
    removed = 0
    for d in list(p.qdir.values()) + [p.inflight, p.data]:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not n.endswith(TEMP_SUFFIXES):
                continue
            fp = os.path.join(d, n)
            try:
                if now - os.path.getmtime(fp) > TEMP_MAX_AGE:
                    if n.endswith(".cancel"):       # a cancel that stopped before writing its record
                        rec = read_json(fp, None)
                        _record_cancel(p, fp, rec, n[:-len(".json.cancel")])
                    else:
                        os.remove(fp)
                    removed += 1
            except OSError:
                pass
    return removed
