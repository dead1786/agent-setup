"""winject control: dispatcher heartbeat check, start / ensure / stop, Telegram alerts. Standard library only.

ensure-dispatcher is what the agent watchdog calls every minute:
  C:\\AI\\.agent\\py\\Scripts\\pythonw.exe C:\\AI\\.agent\\winject\\cli.py ensure-dispatcher
It starts the dispatcher when none is running, restarts one that stopped responding (verified pid only),
and otherwise does nothing. It never types anything itself.
"""
import os
import subprocess
import sys
import time

from . import common as c

DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP = 0x00000008, 0x00000200
CREATE_BREAKAWAY_FROM_JOB, CREATE_NO_WINDOW = 0x01000000, 0x08000000
ENSURE_ALERT_COOLDOWN = 3600
ENSURE_MUTEX = "Local\\agent-winject-ensure"


def read_heartbeat(p):
    hb = c.read_json(p.heartbeat, None)
    return hb if isinstance(hb, dict) else None


def dispatcher_running():
    """a dispatcher holds the single-instance mutex for as long as it lives"""
    return c.mutex_exists(c.DISPATCHER_MUTEX)


def _num(hb, key):
    try:
        return float(hb.get(key))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def assess(hb, running, now):
    """start (none running) / starting / ok / hung"""
    if not running:
        return "start"
    if not hb:
        return "starting"                           # the mutex is there, its first heartbeat is not written yet
    if hb.get("state") == "stopped":
        # a new dispatcher writes its first heartbeat right after taking the mutex; "stopped" for longer than
        # HB_STALE while the mutex is still held means the old one is stuck on its way out
        return "starting" if now - _num(hb, "beat") <= c.HB_STALE else "hung"
    if now - _num(hb, "beat") > c.HB_STALE or now - _num(hb, "loop") > c.LOOP_STALE:
        return "hung"
    return "ok"


def verify_pid(hb):
    """is the heartbeat's pid still the very python process that wrote it? (guards against pid reuse)"""
    if not hb:
        return False
    started = _num(hb, "started")
    info = c.process_info(hb.get("pid"))
    if not info or not started:
        return False
    exe, created = info
    return os.path.basename(exe).lower().startswith("python") and created <= started + 2 and started - created < 600


def _sibling(name):
    cand = os.path.join(os.path.dirname(sys.executable), name)
    return cand if os.path.exists(cand) else sys.executable


def pythonw():
    return _sibling("pythonw.exe")


def python_console():
    return _sibling("python.exe")


def start_dispatcher(p):
    """start `pythonw winject\\dispatcher.py` detached and hidden; returns the launcher's pid"""
    script = os.path.join(c.PKG_DIR, "dispatcher.py")
    env = dict(os.environ, WINJECT_DATA=p.data, PYTHONUTF8="1")
    base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    last = None
    # leave the caller's job object when allowed, so the dispatcher outlives the program that started it
    for extra in (CREATE_BREAKAWAY_FROM_JOB, 0):
        try:
            proc = subprocess.Popen([pythonw(), script], cwd=p.agent, env=env, creationflags=base | extra,
                                    close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            return proc.pid
        except OSError as e:
            last = e
    raise last


def wait_until(pred, timeout, step=0.25):
    end = time.time() + timeout
    while True:
        if pred():
            return True
        if time.time() >= end:
            return False
        time.sleep(step)


def ensure_dispatcher(p):
    """(exit code, one-line message); only one ensure runs at a time (watchdog and a send can overlap)"""
    if not os.path.exists(p.winsend):
        return 1, "FAIL: winsend.py not found (%s); winject needs it next to the winject folder" % p.winsend
    lock = c.acquire_mutex(ENSURE_MUTEX)
    if not lock:
        return 0, "OK: another ensure-dispatcher is already taking care of it"
    try:
        return _ensure(p)
    finally:
        c.close_handle(lock)


def _ensure(p):
    hb = read_heartbeat(p)
    verdict = assess(hb, dispatcher_running(), time.time())
    if verdict == "ok":
        return 0, "OK: dispatcher running (pid %s, state %s)" % (hb.get("pid"), hb.get("state"))
    if verdict == "starting":
        return 0, "OK: dispatcher is starting"
    hung = verdict == "hung"
    if hung:
        now = time.time()
        what = "heartbeat %ds ago, main loop %ds ago" % (now - _num(hb, "beat"), now - _num(hb, "loop"))
        if not verify_pid(hb):
            c.dlog(p, "ensure: dispatcher not responding (%s) but pid %s cannot be verified; left alone"
                   % (what, hb.get("pid")))
            return 0, "WAIT: the dispatcher is not responding (%s) and its pid cannot be verified; left alone" % what
        c.dlog(p, "ensure: dispatcher pid %s not responding (%s); stopping it" % (hb.get("pid"), what))
        c.terminate_process(hb.get("pid"))
        if not wait_until(lambda: not dispatcher_running(), 6):
            alert_with_cooldown(p, "ensure-kill", "⚠️ winject 送字排程器沒有回應，而且關不掉（pid %s）。"
                                                  "請重新開機，或在 AI 視窗請 AI 處理。" % hb.get("pid"))
            return 1, "FAIL: the dispatcher (pid %s) is not responding and did not stop" % hb.get("pid")
    try:
        pid = start_dispatcher(p)
    except OSError as e:
        c.dlog(p, "ensure: could not start the dispatcher: %s" % e)
        alert_with_cooldown(p, "ensure-start", "⚠️ winject 送字排程器啟動失敗：%s" % e)
        return 1, "FAIL: could not start the dispatcher: %s" % e
    up = wait_until(dispatcher_running, 10)
    c.dlog(p, "ensure: started dispatcher (launcher pid %s)%s%s"
           % (pid, " after a hang" if hung else "", "" if up else "; it has not taken its lock after 10 s"))
    if hung:
        alert_with_cooldown(p, "ensure-restart", "ℹ️ winject 送字排程器卡住，已自動重新啟動。正在送的那一則可能沒送到，"
                                                 "請用 status 查看。")
    if not up:
        alert_with_cooldown(p, "ensure-start", "⚠️ winject 送字排程器啟動後沒有正常執行，請看 %s" % p.log)
        return 1, "FAIL: started the dispatcher (pid %s) but it is not running after 10 s; see %s" % (pid, p.log)
    return 0, "OK: dispatcher started%s" % (" (the previous one was not responding)" if hung else "")


def stop_dispatcher(p, timeout=20):
    """ask the running dispatcher to exit after its current send (the watchdog starts it again within a minute)"""
    if not dispatcher_running():
        return 0, "OK: the dispatcher is not running"
    os.makedirs(p.data, exist_ok=True)
    with open(p.stop, "w", encoding="utf-8") as f:
        f.write(c.now_iso())
    if wait_until(lambda: not dispatcher_running(), timeout):
        return 0, ("OK: dispatcher stopped (the watchdog starts it again within a minute; to stop sending, "
                   "use pause)")
    return 1, "FAIL: the dispatcher did not stop within %d s (it may be in the middle of a send); try again" % timeout


# ---------------------------------------------------------------- Telegram (through tgterm.py send)
def tg_configured(p):
    cfg = c.read_json(p.config, None)
    return isinstance(cfg, dict) and bool(cfg.get("token")) and bool(cfg.get("owner_id")) and os.path.exists(p.tgterm)


def tg_send(p, text, runner=None):
    """message the owner with `tgterm.py send -`; 'skipped' when Telegram is not set up on this computer"""
    if not tg_configured(p):
        return "skipped (Telegram is not set up)"
    runner = runner or subprocess.run
    try:
        r = runner([python_console(), p.tgterm, "send", "-"], input=text.encode("utf-8"), stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, timeout=45, cwd=p.agent, creationflags=CREATE_NO_WINDOW,
                   env=dict(os.environ, PYTHONUTF8="1"))
    except Exception as e:
        return "failed: %s: %s" % (type(e).__name__, e)
    if r.returncode == 0:
        return "sent"
    err = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return "failed: exit %s %s" % (r.returncode, err[-1] if err else "")


def alert_with_cooldown(p, key, text, cooldown=ENSURE_ALERT_COOLDOWN, runner=None, now=None):
    now = time.time() if now is None else now
    state = c.read_json(p.alert_state, {})
    state = state if isinstance(state, dict) else {}
    try:
        last = float(state[key]) if key in state else None
    except (TypeError, ValueError):
        last = None
    if last is not None and 0 <= now - last < cooldown:
        return "suppressed (cooldown)"
    state[key] = now
    try:
        c.write_json(p.alert_state, state)
    except OSError:
        pass
    r = tg_send(p, text, runner=runner)
    c.dlog(p, "alert %s: %s" % (key, r))
    return r
