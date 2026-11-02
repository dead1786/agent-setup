"""winject dispatcher: the one process that types queued messages, strictly one at a time.

Started hidden by `winject\\cli.py ensure-dispatcher` (the agent watchdog runs that every minute):
  C:\\AI\\.agent\\py\\Scripts\\pythonw.exe C:\\AI\\.agent\\winject\\dispatcher.py
Single instance (named mutex Local\\agent-winject-dispatcher); a second copy exits at once.

Each message: take the oldest from queue\\high, else from queue\\normal -> move it to queue\\inflight ->
type it with winsend.py (window: send_window, line: send_line; under winsend's lock Local\\agent-winsend, so a
one-off winsend or tgterm paste never fights over the focus) -> done.jsonl, or a retry later, or failed.jsonl
plus a Telegram message to the owner. A message that may already have been typed is never retried.
This computer's AI window (common.uses_console) is first typed straight into its terminal by `tgterm.py _inject
console` in a detached child (no focus, no clipboard; tgterm takes the same lock itself); when that does not
work, winsend pastes it as above. done.jsonl says which way it went (via console / paste).
A heartbeat thread writes heartbeat.json every 5 s; the main loop's own time is in it too.
The dispatcher idles while winject-data\\paused or the agent's paused file exists, exits on winject-data\\stop
(newer than its start) and exits when winsend.py disappears (uninstalled).
"""
import importlib.util
import os
import subprocess
import sys
import threading
import time
import traceback

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)
from winject import common as c, control  # noqa: E402

POLL = 0.3                  # seconds between queue checks when idle
POST_DELAY = 1.0            # pause after each send before the next one
MAINT_EVERY = 3600          # log rotation / temp cleanup
ALERT_COOLDOWN = 600        # at most one Telegram alert per target per 10 minutes (the rest are counted)
PASTED_NOT_SENT = "pasted into the window but not sent"     # winsend.send_window, focus lost just before Enter
WINSEND_NEEDS = ("send_window", "send_line", "SendLock", "SendFail", "match_title", "norm_title", "make_dpi_aware",
                 "enum_windows", "target_windows", "line_windows", "line_chat_windows")
CONSOLE_TYPED = "already typed"         # in tgterm's FAIL line when keys reached the terminal: never paste it again
CONSOLE_TIMEOUT = 120                   # tgterm waits up to 60 s for the send lock, then types for a few seconds
DETACHED_PROCESS = 0x00000008


def load_winsend(p):
    """import the winsend.py that sits next to the winject folder (exactly that file)"""
    spec = importlib.util.spec_from_file_location("winsend", p.winsend)
    if spec is None or not os.path.exists(p.winsend):
        raise RuntimeError("winsend.py not found: %s" % p.winsend)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    missing = [a for a in WINSEND_NEEDS if not hasattr(mod, a)]
    if missing:
        raise RuntimeError("winsend.py lacks %s (an incompatible version)" % ", ".join(missing))
    return mod


def run_console(p, text, runner=None):
    """`tgterm.py _inject console <text>` in a detached child (attaching to the AI's console needs a process
    without one); its one output line: "ok ..." when typed, else "FAIL ..." """
    runner = runner or subprocess.run
    env = dict(os.environ, PYTHONUTF8="1", AGENT_SETUP_AGENT_DIR=p.agent)
    try:
        # pythonw: the venv's python.exe is a launcher whose interpreter child would open a console window here
        r = runner([control.pythonw(), p.tgterm, "_inject", "console", text], cwd=p.agent, env=env,
                   creationflags=DETACHED_PROCESS, stdin=subprocess.DEVNULL, capture_output=True,
                   timeout=CONSOLE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "FAIL no answer from tgterm.py within %d s (keys may be already typed)" % CONSOLE_TIMEOUT
    except Exception as e:                          # could not start it (e.g. the text is too long for a command line)
        return "FAIL could not run tgterm.py: %s: %s" % (type(e).__name__, e)
    out = (r.stdout or b"").decode("utf-8", "replace").strip()
    err = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return out or "FAIL exit %s %s" % (r.returncode, err[-1] if err else "(no output)")


class WinsendSender:
    """types one message (this computer's AI window: tgterm's console path first) with winsend;
    returns {"ok", "detail", "via"} or {"ok": False, "reason", "kind", "maybe_sent"}"""

    def __init__(self, p, ws=None, console=None):
        self.p = p
        # the real console path only next to the real winsend: a sender built around a fake ws never types
        self.console = console if console is not None else (None if ws else (lambda text: run_console(p, text)))
        self.ws = ws or load_winsend(p)
        self._orig_match = self.ws.match_title

    def __call__(self, name, t, rec, text):
        kind = t.get("type", "window")
        if kind not in c.TYPES:
            return {"ok": False, "reason": "target %r has an unknown type %r" % (name, kind), "kind": "bad"}
        title, why = c.resolve_title(self.p, t)
        if not title:
            return {"ok": False, "reason": "target %r: %s" % (name, why), "kind": "bad"}
        console_error = None
        if self.console and c.uses_console(self.p, t):
            try:
                out = str(self.console(text) or "").strip()      # outside winsend's lock: tgterm takes it itself
            except Exception as e:
                out = "FAIL %s: %s" % (type(e).__name__, e)
            if out.startswith("ok ") and out != "ok empty":
                return {"ok": True, "detail": "typed into the AI terminal (%s)" % out[3:], "via": "console"}
            if CONSOLE_TYPED in out:
                return {"ok": False, "reason": "typing straight into the AI terminal stopped halfway: " + out,
                        "kind": "other", "maybe_sent": True}
            console_error = c.preview(out, 200) or "(no answer)"
            c.dlog(self.p, "console typing into %s did not work (%s); pasting instead" % (name, console_error))
        res = self._winsend(kind, title, t, rec, text)
        if res.get("ok"):
            res["via"] = "paste"
            if console_error:
                res["console_error"] = console_error
        return res

    def _winsend(self, kind, title, t, rec, text):
        ws = self.ws
        strict = c.make_strict_matcher(self._orig_match, ws.norm_title,
                                       t.get("class_whitelist") if kind == "window" else None)
        try:
            with ws.SendLock():
                ws.match_title = strict             # exact-title matching only (see common.make_strict_matcher)
                try:
                    if kind == "line":
                        detail = ws.send_line(title, text, mention=rec.get("mention"), enter=True)
                    else:
                        detail = ws.send_window(title, text, enter=t.get("enter", True) is not False)
                finally:
                    ws.match_title = self._orig_match
        except ws.SendFail as e:
            reason = e.reason or ""
            maybe = bool(getattr(e, "maybe_sent", False))
            if kind == "window" and not maybe and (PASTED_NOT_SENT in reason or reason.startswith("unexpected ")):
                # the text may sit unsent in the window's input box: a retry would paste it a second time
                # (LINE is safe: send_line clears its input box before pasting)
                maybe = True
            return {"ok": False, "reason": reason, "kind": c.classify(reason, getattr(e, "focus_lost", False)),
                    "maybe_sent": maybe}
        except Exception as e:                      # raised before winsend pressed any key
            c.dlog(self.p, "sender: unexpected: " + traceback.format_exc())
            return {"ok": False, "reason": "unexpected %s: %s" % (type(e).__name__, e), "kind": "other"}
        return {"ok": True, "detail": detail}


def alert_text(rec, reason, maybe_sent):
    lines = ["⚠️ winject 送字失敗",
             "目標：%s　來源：%s" % (rec.get("target"), rec.get("source")),
             "試了 %s 次，最後的原因：%s" % (rec.get("attempts"), reason)]
    if maybe_sent:
        lines.append("❗ 可能已經送出一部分，所以沒有自動重送；請先到那個視窗確認，再決定要不要重送。")
    lines.append("內容開頭：" + c.preview(rec.get("text"), 120))
    lines.append("（詳細：winject\\cli.py status）")
    return "\n".join(lines)


class Dispatcher:
    def __init__(self, p, sender=None, alerter=None, clock=time.time, async_alerts=True):
        self.p = p
        self.sender = sender
        self.alerter = alerter or (lambda text: control.tg_send(p, text))
        self.clock = clock
        self.async_alerts = async_alerts
        self.started = clock()
        self.loop_ts = self.started
        self.state = {"state": "starting", "current": None}
        self._alerted, self._suppressed, self._cache = {}, {}, {}

    def log(self, msg):
        c.dlog(self.p, msg)

    # ------------------------------------------------------------ alerts
    def alert(self, target, text):
        now = self.clock()
        last = self._alerted.get(target)
        if last is not None and now - last < ALERT_COOLDOWN:
            self._suppressed[target] = self._suppressed.get(target, 0) + 1
            self.log("alert for %s suppressed (cooldown)" % target)
            return False
        n = self._suppressed.pop(target, 0)
        if n:
            text += "\n（這段時間另有 %d 則送往「%s」的失敗沒有另外通知）" % (n, target)
        self._alerted[target] = now

        def run():
            try:
                r = self.alerter(text)
            except Exception as e:
                r = "failed: %r" % e
            self.log("alert for %s: %s" % (target, r))

        if self.async_alerts:
            threading.Thread(target=run, daemon=True).start()
        else:
            run()
        return True

    def fail_final(self, rec, reason, maybe_sent=False, alert=True):
        out = dict(rec, state="failed", failed=c.now_iso(), final_reason=reason, maybe_sent=bool(maybe_sent))
        try:
            c.append_jsonl(self.p, self.p.failed, out)
        except OSError as e:                        # e.g. disk full: keep the record in the log at least
            self.log("could not write failed.jsonl (%s): %s" % (e, c.preview(repr(out), 400)))
        self.log("FAILED id=%s target=%s attempts=%s maybe_sent=%s: %s"
                 % (rec.get("id"), rec.get("target"), rec.get("attempts"), bool(maybe_sent), reason))
        if alert:
            self.alert(str(rec.get("target")), alert_text(rec, reason, maybe_sent))

    # ------------------------------------------------------------ one message
    def process(self, prio, path, rec):
        """send one queued message; returns done / retry / failed / gone / busy"""
        p = self.p
        name = os.path.basename(path)
        inflight = os.path.join(p.inflight, name)
        os.makedirs(p.inflight, exist_ok=True)
        try:
            os.replace(path, inflight)              # claim it (cancel / another reader cannot take it now)
        except FileNotFoundError:
            return "gone"
        except PermissionError:
            return "busy"                           # someone has the file open this instant; next round
        rec = dict(rec, attempts=int(rec.get("attempts") or 0) + 1, state="sending", last_try=c.now_iso(),
                   priority=prio)
        try:
            c.write_json(inflight, rec)
        except OSError as e:                        # e.g. disk full: put it back untouched, try again later
            self.log("cannot write in-flight file (%s); message put back" % e)
            try:
                os.replace(inflight, path)
            except OSError:
                pass
            return "busy"
        self.state.update(state="sending", current={"id": rec.get("id"), "target": rec.get("target")})
        try:
            try:
                targets, terr = c.load_targets(p), None
            except ValueError as e:
                targets, terr = None, str(e)
            t = targets.get(rec["target"]) if targets is not None else None
            if targets is None:
                result = {"ok": False, "reason": terr, "kind": "other"}
            elif t is None:
                result = {"ok": False, "reason": "unknown target %r (removed from the targets?)" % rec["target"],
                          "kind": "bad"}
            else:
                result = self.sender(rec["target"], t, rec, c.compose_text(rec, t))
        except Exception as e:
            self.log("process: unexpected: " + traceback.format_exc())
            result = {"ok": False, "reason": "unexpected %s: %s" % (type(e).__name__, e), "kind": "other"}
        finally:
            self.state.update(state="idle", current=None)
        return self.conclude(path, inflight, rec, result or {})

    def conclude(self, path, inflight, rec, result):
        p = self.p
        if result.get("ok"):
            out = dict(rec, state="done", completed=c.now_iso(), result=result.get("detail", ""))
            out.update((k, result[k]) for k in ("via", "console_error") if result.get(k))
            try:
                c.append_jsonl(p, p.done, out)
            except OSError as e:                    # it was typed: never leave it where it could be retried
                self.log("could not write done.jsonl (%s): %s" % (e, c.preview(repr(out), 400)))
            _remove(inflight)
            self.log("ok id=%s target=%s attempt=%s via=%s: %s" % (rec.get("id"), rec.get("target"), rec.get("attempts"),
                                                                  result.get("via", "?"), result.get("detail", "")))
            return "done"
        reason = result.get("reason") or "unknown error"
        maybe = bool(result.get("maybe_sent"))
        rec["last_error"] = reason
        delay = None if maybe else c.backoff(result.get("kind") or "other", rec["attempts"])
        if delay is None:
            self.fail_final(rec, reason, maybe_sent=maybe)
            _remove(inflight)
            return "failed"
        # mark it queued inside inflight first, then move it back under the same name (same place in the queue);
        # if anything here fails, the hourly recovery requeues a 'queued' inflight file instead of dropping it
        try:
            c.write_json(inflight, dict(rec, state="queued", not_before=self.clock() + delay))
        except OSError as e:
            self.log("could not update the in-flight file for a retry (%s)" % e)
        try:
            os.replace(inflight, path)
        except OSError as e:
            self.log("could not requeue id=%s (%s); the hourly recovery will" % (rec.get("id"), e))
        self.log("retry in %ss id=%s target=%s attempt=%s: %s" % (delay, rec.get("id"), rec.get("target"),
                                                                 rec.get("attempts"), reason))
        return "retry"

    # ------------------------------------------------------------ start-up recovery
    def recover_inflight(self):
        """a message left in inflight means the previous dispatcher stopped while sending it"""
        p = self.p
        items = c.inflight_items(p)
        if not items:
            return 0
        recent = c.recent_ids(p)
        n = 0
        for path in items:
            try:
                n += self._recover_one(path, recent)
            except Exception:                       # one bad file must never stop the dispatcher from starting
                self.log("recovery of %s failed: %s" % (os.path.basename(path), traceback.format_exc()))
        return n

    def _recover_one(self, path, recent):
        p = self.p
        name = os.path.basename(path)
        rec = c.read_json(path, None)
        if not isinstance(rec, dict):
            c.quarantine(p, path, "interrupted: unreadable in-flight file")
            return 0
        if rec.get("id") in recent or any(os.path.exists(os.path.join(d, name)) for d in p.qdir.values()):
            _remove(path)                           # its result had already been written
            return 0
        if rec.get("state") != "sending":           # claimed but not being typed (never tried, or a retry): put back
            prio = rec.get("priority") if rec.get("priority") in c.PRIORITIES else "normal"
            os.makedirs(p.qdir[prio], exist_ok=True)
            os.replace(path, os.path.join(p.qdir[prio], name))
            self.log("requeued in-flight id=%s (state %s)" % (rec.get("id"), rec.get("state")))
            return 0
        self.fail_final(rec, "interrupted: the dispatcher stopped during this send, so it may or may not have "
                             "arrived; not resent automatically", maybe_sent=True)
        _remove(path)
        return 1

    # ------------------------------------------------------------ heartbeat
    def write_heartbeat(self):
        hb = {"pid": os.getpid(), "started": self.started, "beat": time.time(), "loop": self.loop_ts,
              "state": self.state["state"], "current": self.state["current"], "version": c.VERSION}
        try:
            c.write_json(self.p.heartbeat, hb)
        except OSError:
            pass

    def _beat_loop(self, stop):
        while not stop.is_set():
            self.write_heartbeat()
            stop.wait(c.HB_INTERVAL)

    def _stop_requested(self):
        try:
            m = os.path.getmtime(self.p.stop)
        except OSError:
            return False
        _remove(self.p.stop)
        if m >= self.started - 1:
            return True
        self.log("removed a stale stop request")
        return False

    def maintenance(self, now):
        self.recover_inflight()                     # runs between sends: anything in inflight now is stranded
        for a in c.rotate_logs(self.p, now):
            self.log("rotate: " + a)
        n = c.cleanup_temp(self.p, now)
        if n:
            self.log("removed %d leftover temp file(s)" % n)

    # ------------------------------------------------------------ main loop
    def run(self):
        p = self.p
        handle = c.acquire_mutex(c.DISPATCHER_MUTEX)
        if not handle:
            return 0                                # another dispatcher is running
        self.started = self.loop_ts = time.time()
        self.log("start: winject %s, pid %d, data %s" % (c.VERSION, os.getpid(), p.data))
        stop = threading.Event()
        try:
            # beat from the very start: a hang while loading winsend or recovering shows up as a stale main loop
            threading.Thread(target=self._beat_loop, args=(stop,), daemon=True).start()
            if self.sender is None:
                try:
                    self.sender = WinsendSender(p)
                except Exception as e:
                    self.log("cannot load winsend.py: %r; exiting" % e)
                    return 1
            try:
                self.log("dpi awareness: %s" % self.sender.ws.make_dpi_aware())
            except Exception as e:
                self.log("dpi awareness failed: %r" % e)
            try:
                self.recover_inflight()
            except Exception:
                self.log("start-up recovery failed: " + traceback.format_exc())
            last_maint = 0.0
            while True:
                now = self.loop_ts = time.time()
                try:
                    if self._stop_requested():
                        self.log("stop requested; exiting")
                        break
                    if not os.path.exists(p.winsend):
                        self.log("winsend.py is gone (uninstalled?); exiting")
                        break
                    if c.pause_reason(p):
                        self.state["state"] = "paused"
                        time.sleep(1)
                        continue
                    if now - last_maint > MAINT_EVERY:
                        last_maint = now
                        self.maintenance(now)
                    nxt = c.pick_next(p, now, self._cache)
                    if not nxt:
                        self.state["state"] = "idle"
                        time.sleep(POLL)
                        continue
                    self.process(*nxt)
                    time.sleep(POST_DELAY)
                except Exception:
                    self.log("loop error: " + traceback.format_exc())
                    time.sleep(1)
        finally:
            stop.set()
            self.state.update(state="stopped", current=None)
            self.write_heartbeat()
            self.log("exit")
            c.close_handle(handle)
        return 0


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def main():
    p = c.Paths()
    try:
        return Dispatcher(p).run()
    except Exception:                               # pythonw has no console: the traceback goes to the log
        c.dlog(p, "dispatcher crashed: " + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
