"""winject: queue text for a window (another AI terminal, an app) or a LINE chat; one dispatcher types it.

Run with the agent's Python from any folder (W = C:\\AI\\.agent\\winject\\cli.py):
  python W <target> "<text>" [--source NAME] [--priority high] [--mention NAME] [--no-prefix]
                             [--wait SECONDS] [--file F | --stdin] [--no-start]
  python W status [--last N] [--json]   dispatcher, queue, latest done / failed
  python W monitor [--interval S]       status, refreshed until Ctrl+C
  python W targets                      the targets
  python W add-target <name> "<window title>" [--line] [--priority high] [--no-prefix | --prefix]
                             [--class CLASS ...] [--halfwidth] [--note TEXT] [--replace]
  python W remove-target <name>
  python W test-target <name>           only checks that the window / LINE chat is there; types nothing
  python W ensure-dispatcher            start the dispatcher if it is not running (restart it if it hangs)
  python W stop-dispatcher              ask the dispatcher to exit after the current send
  python W pause | resume               stop / restart typing (queued messages wait; new ones are refused)
  python W cancel <id>|all              drop queued messages (not the one being typed right now)
  python W archive                      move done.jsonl / failed.jsonl into the archive folder now
  python W selftest [--dir D]           offline checks in a temp folder; touches no window, clipboard, mouse, keys

Sending prints "QUEUED: id=..." (exit 0) or "FAIL: <reason>" (exit 1, nothing queued). With --wait it then waits
for the result: "OK: ..." (0), "FAIL: ..." (1) or "PENDING: ..." (3, still queued). Usage errors exit 2.
Put -- before a text that starts with "-". Multi-line text or text with quotes: --file (UTF-8) or --stdin.
Data: C:\\AI\\.agent\\winject-data (WINJECT_DATA moves it).
"""
import argparse
import json
import os
import sys
import time

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)
from winject import common as c, control  # noqa: E402

say = c.say
MAYBE_NOTE = " (it may already have been typed; check that window before sending again)"


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


# ---------------------------------------------------------------- send
def send_parser():
    ap = _Parser(prog="winject <target>", add_help=False)
    ap.add_argument("target")
    ap.add_argument("text", nargs="?")
    ap.add_argument("--source", default="cli")
    ap.add_argument("--priority", choices=c.PRIORITIES, default="normal")
    ap.add_argument("--mention")
    ap.add_argument("--no-prefix", action="store_true")
    ap.add_argument("--wait", type=float, default=0.0)
    ap.add_argument("--file")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--no-start", action="store_true")
    return ap


def parse_send(argv):
    # intermixed: `agent --source x "text"` must work too (plain parse_args would drop the optional text there)
    a = send_parser().parse_intermixed_args(argv)
    given = sum(x is not None and x is not False for x in (a.text, a.file, a.stdin or None))
    if given != 1:
        raise UsageError("give the text, or --file <utf-8 file>, or --stdin (exactly one)")
    if a.wait < 0 or a.wait > 3600:
        raise UsageError("--wait must be 0-3600 seconds")
    return a


def read_text(a):
    if a.file is not None:
        try:
            with open(a.file, encoding="utf-8-sig") as f:
                return f.read()
        except UnicodeDecodeError:
            raise UsageError("--file %s is not UTF-8 text" % a.file)
        except OSError as e:
            raise UsageError("cannot read --file %s: %s" % (a.file, e.strerror or e))
    if a.stdin:
        return sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    return a.text


def cmd_send(argv):
    a = parse_send(argv)
    text = read_text(a)
    if a.stdin or a.file is not None:
        text = text.rstrip("\r\n")
    p = c.Paths()
    r = c.enqueue(p, a.target, text, source=a.source, priority=a.priority, mention=a.mention,
                  prefix=False if a.no_prefix else None)
    if not r.get("ok"):
        say("FAIL: not queued: %s" % r.get("reason"))
        return 1
    say("QUEUED: id=%s target=%s priority=%s" % (r["id"], r["target"], r["priority"]))
    if not control.dispatcher_running():
        if a.no_start:
            say("NOTE: the dispatcher is not running; the watchdog starts it within a minute")
        else:
            code, msg = control.ensure_dispatcher(p)
            if code:
                say("NOTE: " + msg)
    if a.wait <= 0:
        return 0
    end = time.time() + a.wait
    while True:
        res = c.find_result(p, r["id"])
        if res and res[0] in ("done", "failed"):
            return report_result(*res)
        if time.time() >= end:
            break
        time.sleep(0.5)
    rec = res[1] if res else {}
    extra = ("; last error: %s" % rec["last_error"]) if rec.get("last_error") else ""
    say("PENDING: id=%s not finished after %g s (%s%s); see: winject\\cli.py status"
        % (r["id"], a.wait, res[0] if res else "unknown", extra))
    return 3


def report_result(kind, rec):
    if kind == "done":
        say("OK: %s" % (rec.get("result") or "sent"))
        return 0
    say("FAIL: %s (after %s tries)%s" % (rec.get("final_reason"), rec.get("attempts"),
                                         MAYBE_NOTE if rec.get("maybe_sent") else ""))
    return 1


# ---------------------------------------------------------------- status / monitor
def status_data(p, last=5):
    now = time.time()
    running = control.dispatcher_running()
    hb = control.read_heartbeat(p)
    verdict = control.assess(hb, running, now)
    counts, waiting = {"high": 0, "normal": 0}, []
    for prio, path in c.queued(p):
        counts[prio] += 1
        rec = c.read_json(path, None)
        if not isinstance(rec, dict):
            continue
        try:
            nb = float(rec.get("not_before") or 0)
        except (TypeError, ValueError):
            nb = 0
        if now < nb <= now + c.MAX_DELAY + 5:
            waiting.append({"id": rec.get("id"), "target": rec.get("target"), "retry_in": round(nb - now),
                            "attempts": rec.get("attempts"), "last_error": rec.get("last_error")})
    sending = [c.read_json(x, {}) or {} for x in c.inflight_items(p)]
    return {
        "dispatcher": {"verdict": verdict, "running": running, "pid": (hb or {}).get("pid"),
                       "state": (hb or {}).get("state"), "current": (hb or {}).get("current"),
                       "heartbeat_age": round(now - control._num(hb, "beat")) if hb else None,
                       "version": (hb or {}).get("version")},
        "paused": c.pause_reason(p),
        "queue": {"high": counts["high"], "normal": counts["normal"], "waiting_retry": waiting,
                  "sending": [{"id": s.get("id"), "target": s.get("target")} for s in sending]},
        "done_count": c.count_lines(p.done), "failed_count": c.count_lines(p.failed),
        "last_done": c.tail_jsonl(p.done, last) if last else [],
        "last_failed": c.tail_jsonl(p.failed, last) if last else [],
        "data": p.data,
    }


VERDICT_TEXT = {"ok": "running", "start": "NOT RUNNING (the watchdog starts it within a minute; or run "
                                         "ensure-dispatcher)",
                "starting": "starting", "hung": "NOT RESPONDING (ensure-dispatcher restarts it)"}


def render_status(d):
    ds = d["dispatcher"]
    head = VERDICT_TEXT.get(ds["verdict"], ds["verdict"])
    if ds["verdict"] in ("ok", "hung"):
        head += " - pid %s, state %s, heartbeat %ss ago" % (ds["pid"], ds["state"], ds["heartbeat_age"])
        if ds.get("current"):
            head += ", typing id=%s for %s" % (ds["current"].get("id"), ds["current"].get("target"))
    q = d["queue"]
    out = ["dispatcher: " + head,
           "paused:     " + (d["paused"] or "no"),
           "queue:      high %d, normal %d (%d waiting to retry), typing now %d"
           % (q["high"], q["normal"], len(q["waiting_retry"]), len(q["sending"]))]
    for w in q["waiting_retry"]:
        out.append("  retry in %ss: id=%s target=%s after %s tries: %s"
                   % (w["retry_in"], w["id"], w["target"], w["attempts"], c.preview(w["last_error"], 100)))
    out.append("done.jsonl: %d   failed.jsonl: %d   (data: %s)" % (d["done_count"], d["failed_count"], d["data"]))
    if d["last_done"]:
        out.append("latest done:")
        for r in d["last_done"]:
            out.append("  %s  %s <- %s  %s" % (r.get("completed", "?"), r.get("target"), r.get("source"),
                                               c.preview(r.get("text"), 50)))
    if d["last_failed"]:
        out.append("latest failed:")
        for r in d["last_failed"]:
            out.append("  %s  %s <- %s  id=%s  %s%s" % (r.get("failed", "?"), r.get("target"), r.get("source"),
                                                       r.get("id"), c.preview(r.get("final_reason"), 90),
                                                       "  [MAY HAVE BEEN SENT]" if r.get("maybe_sent") else ""))
    return "\n".join(out)


def cmd_status(argv):
    ap = _Parser(prog="winject status", add_help=False)
    ap.add_argument("--last", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    d = status_data(c.Paths(), max(0, min(a.last, 200)))
    say(json.dumps(d, ensure_ascii=False, indent=1) if a.json else render_status(d))
    return 0


def cmd_monitor(argv):
    ap = _Parser(prog="winject monitor", add_help=False)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--last", type=int, default=5)
    a = ap.parse_args(argv)
    p = c.Paths()
    c.enable_vt()
    try:
        while True:
            body = render_status(status_data(p, max(0, min(a.last, 50))))
            sys.stdout.write("\x1b[H\x1b[2J%s\n\n%s  (Ctrl+C to quit)\n" % (body, time.strftime("%H:%M:%S")))
            sys.stdout.flush()
            time.sleep(max(0.5, a.interval))
    except KeyboardInterrupt:
        return 0


# ---------------------------------------------------------------- targets
def cmd_targets(argv):
    if argv:
        raise UsageError("targets takes no arguments")
    p = c.Paths()
    try:
        targets = c.load_targets(p)
    except ValueError as e:
        say("FAIL: %s" % e)
        return 1
    where = p.targets if os.path.exists(p.targets) else p.default_targets + " (defaults)"
    say("targets (%d) from %s:" % (len(targets), where))
    for name in sorted(targets):
        say("  " + c.describe_target(p, name, targets[name]))
    if not targets:
        say('  (none) add one: winject\\cli.py add-target <name> "<window title>"')
    return 0


def add_target_parser():
    ap = _Parser(prog="winject add-target", add_help=False)
    ap.add_argument("name")
    ap.add_argument("title")
    ap.add_argument("--line", action="store_true")
    ap.add_argument("--priority", choices=c.PRIORITIES, default="normal")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--no-prefix", dest="prefix", action="store_false", default=None)
    g.add_argument("--prefix", dest="prefix", action="store_true")
    ap.add_argument("--class", dest="classes", action="append", default=[])
    ap.add_argument("--halfwidth", action="store_true")
    ap.add_argument("--note")
    ap.add_argument("--replace", action="store_true")
    return ap


def cmd_add_target(argv):
    a = add_target_parser().parse_args(argv)
    if a.line and a.classes:
        raise UsageError("--class is only for window targets")
    p = c.Paths()
    try:
        ok, msg = c.add_target(p, a.name, a.title, kind="line" if a.line else "window", priority=a.priority,
                               prefix=a.prefix, classes=a.classes, halfwidth=a.halfwidth, note=a.note,
                               replace=a.replace)
    except (ValueError, OSError) as e:
        ok, msg = False, str(e)
    if not ok:
        say("FAIL: " + msg)
        return 1
    say("OK: %s: %s" % (msg, c.describe_target(p, a.name, c.load_targets(p)[a.name])))
    say("check it with: winject\\cli.py test-target %s" % a.name)
    return 0


def cmd_remove_target(argv):
    ap = _Parser(prog="winject remove-target", add_help=False)
    ap.add_argument("name")
    a = ap.parse_args(argv)
    try:
        ok, msg = c.remove_target(c.Paths(), a.name)
    except (ValueError, OSError) as e:
        ok, msg = False, str(e)
    say(("OK: " if ok else "FAIL: ") + msg)
    return 0 if ok else 1


def cmd_test_target(argv):
    """read-only: lists windows (EnumWindows); never focuses, types, clicks or touches the clipboard"""
    ap = _Parser(prog="winject test-target", add_help=False)
    ap.add_argument("name")
    a = ap.parse_args(argv)
    p = c.Paths()
    try:
        t = c.load_targets(p).get(a.name)
    except ValueError as e:
        say("FAIL: %s" % e)
        return 1
    if t is None:
        say("FAIL: no target named %r (see: winject\\cli.py targets)" % a.name)
        return 1
    title, why = c.resolve_title(p, t)
    if not title:
        say("FAIL: %s" % why)
        return 1
    from winject import dispatcher
    try:
        ws = dispatcher.load_winsend(p)
        wins = ws.enum_windows()
    except Exception as e:
        say("FAIL: cannot list windows: %s: %s" % (type(e).__name__, e))
        return 1
    kind = t.get("type", "window")
    if kind == "line":
        items = ws.line_chat_windows(wins)
        if not items:
            say("FAIL: %s" % ("LINE is running but no chat is open in a window of its own" if ws.line_windows(wins)
                              else "LINE is not running"))
            return 1
    else:
        items = ws.target_windows(wins)
    strict = c.make_strict_matcher(ws.match_title, ws.norm_title, t.get("class_whitelist") if kind == "window" else None)
    w, err = strict(items, title, lambda x: x.title)
    if err:
        say("FAIL: %s" % err)
        near = [x for x in items if ws.norm_title(title) in ws.norm_title(x.title)
                or ws.norm_title(x.title) in ws.norm_title(title)][:8]
        if near:
            say("similar titles: " + ", ".join("%r [%s, class %s]" % (x.title, x.exe or "?", x.cls) for x in near))
        return 1
    say("OK: %s %r is there [%s, class %s]%s; nothing was typed"
        % ("LINE chat" if kind == "line" else "window", w.title, w.exe or "?", w.cls,
           " (minimized)" if w.iconic else ""))
    return 0


# ---------------------------------------------------------------- dispatcher control, pause, cancel, archive
def _no_args(name, argv):
    if argv:
        raise UsageError("%s takes no arguments" % name)


def cmd_ensure(argv):
    _no_args("ensure-dispatcher", argv)
    code, msg = control.ensure_dispatcher(c.Paths())
    say(msg)
    return code


def cmd_stop(argv):
    _no_args("stop-dispatcher", argv)
    code, msg = control.stop_dispatcher(c.Paths())
    say(msg)
    return code


def cmd_pause(argv):
    _no_args("pause", argv)
    p = c.Paths()
    os.makedirs(p.data, exist_ok=True)
    with open(p.paused, "w", encoding="utf-8") as f:
        f.write(c.now_iso())
    say("OK: paused. Nothing more is typed (a send already in progress finishes); queued messages wait and new "
        "ones are refused. Undo: winject\\cli.py resume   Drop the queue: winject\\cli.py cancel all")
    return 0


def cmd_resume(argv):
    _no_args("resume", argv)
    p = c.Paths()
    try:
        os.remove(p.paused)
    except FileNotFoundError:
        pass
    why = c.pause_reason(p)
    say("OK: resumed" + ("; but still on hold: " + why if why else ""))
    return 0


def cmd_cancel(argv):
    ap = _Parser(prog="winject cancel", add_help=False)
    ap.add_argument("which")
    a = ap.parse_args(argv)
    n = c.cancel(c.Paths(), a.which.strip())
    say("OK: cancelled %d queued message(s)" % n if n else "FAIL: nothing queued matches %r (already typed or "
                                                           "being typed right now?)" % a.which)
    return 0 if n else 1


def cmd_archive(argv):
    _no_args("archive", argv)
    actions = c.rotate_logs(c.Paths(), force=True)
    say("OK: " + ("; ".join(actions) if actions else "nothing to archive"))
    return 0


def cmd_selftest(argv):
    ap = _Parser(prog="winject selftest", add_help=False)
    ap.add_argument("--dir")
    a = ap.parse_args(argv)
    from winject import selftest
    return selftest.run(a.dir)


COMMANDS = {"status": cmd_status, "monitor": cmd_monitor, "targets": cmd_targets, "add-target": cmd_add_target,
            "remove-target": cmd_remove_target, "test-target": cmd_test_target, "ensure-dispatcher": cmd_ensure,
            "stop-dispatcher": cmd_stop, "pause": cmd_pause, "resume": cmd_resume, "cancel": cmd_cancel,
            "archive": cmd_archive, "selftest": cmd_selftest}


def main(argv):
    c.setup_stdout()
    if not argv or argv[0] in ("-h", "--help", "help", "/?"):
        say(__doc__)
        return 0 if argv else 2
    cmd = argv[0].lower()
    try:
        if cmd in COMMANDS:
            return COMMANDS[cmd](argv[1:])
        return cmd_send(argv[1:] if cmd == "send" else argv)
    except UsageError as e:
        say("FAIL: usage: %s (see: winject\\cli.py --help)" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
