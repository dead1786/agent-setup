"""winject selftest: offline checks in a temporary folder.

Touches no window, clipboard, mouse or keyboard, starts no process and sends nothing to Telegram: the dispatcher
logic runs with a fake sender, a fake alerter and a fake subprocess runner. Of winsend.py only the pure title
functions are used (match_title / norm_title / Win). The real data folder is never created or changed.
"""
import datetime as dt
import hashlib
import inspect
import os
import shutil
import tempfile
import time
import types

from . import common as c, control


class _Fail(AssertionError):
    pass


def run(base_dir=None):
    from . import cli, dispatcher as d
    import winject

    count = [0]

    def check(cond, what):
        if not cond:
            raise _Fail(what)
        count[0] += 1

    def raises(exc, fn, *a, **k):
        try:
            fn(*a, **k)
        except exc:
            return True
        return False

    def sha(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def write(path, text, age=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        if age is not None:
            t = time.time() - age
            os.utime(path, (t, t))

    real_data = c.Paths().data
    real_data_existed = os.path.exists(real_data)
    if base_dir:
        os.makedirs(base_dir, exist_ok=True)
    root = tempfile.mkdtemp(prefix="winject-selftest-", dir=base_dir)
    try:
        agent = os.path.join(root, "agent")
        os.makedirs(agent)
        p = c.Paths(data=os.path.join(root, "data"), agent=agent)
        check(not os.path.exists(p.data), "the data folder must only be created on demand")
        c.write_json(p.config, {"agent_name": "測試 AI", "token": "", "owner_id": ""})

        # ---- public API shape
        sig = inspect.signature(winject.send)
        check(list(sig.parameters)[:4] == ["target", "text", "source", "priority"]
              and sig.parameters["source"].default == "unknown" and sig.parameters["priority"].default == "normal",
              "send(target, text, source='unknown', priority='normal') signature: %s" % sig)

        # ---- targets
        pkg_hash = sha(p.default_targets)
        check(isinstance(c.read_json(p.default_targets), dict), "shipped targets.json is valid JSON")
        t = c.load_targets(p)
        check(list(t) == ["agent"] and t["agent"]["type"] == "window" and t["agent"]["title_from_config"] == "agent_name",
              "default targets: %r" % t)
        check(c.resolve_title(p, t["agent"]) == ("測試 AI", "config.json agent_name"), c.resolve_title(p, t["agent"]))
        check(c.resolve_title(p, {"title_from_config": "missing_key"})[0] is None, "missing config key")
        check(c.resolve_title(p, {"title": "  Untitled   - Notepad "}) == ("Untitled - Notepad", "stored"), "title spaces")
        for bad in ("", "a b", "status", "Status", "ALL", "-x", "_x", "a/b", 'q"', "x" * 41, "tab\t"):
            check(c.check_name(bad), "bad target name accepted: %r" % bad)
        for good in ("記事本", "codex-1", "Other.AI", "agent2"):
            check(c.check_name(good) is None, "good target name refused: %r" % good)
        ok, msg = c.add_target(p, "notes", "Untitled - Notepad", prefix=False)
        check(ok and os.path.exists(p.targets), msg)
        t = c.load_targets(p)
        check(sorted(t) == ["agent", "notes"] and t["notes"]["prefix"] is False and t["notes"]["type"] == "window",
              "after add: %r" % t)
        check(not c.add_target(p, "notes", "Other")[0], "duplicate add must fail")
        check(c.add_target(p, "notes", "Untitled - Notepad", prefix=False, replace=True)[0], "--replace")
        check(c.add_target(p, "family", "家人群組", kind="line")[0], "add line target")
        check(c.load_targets(p)["family"]["prefix"] is False, "LINE targets default to no prefix")
        check(not c.add_target(p, "x", "t", kind="sms")[0] and not c.add_target(p, "x", "t", priority="urgent")[0]
              and not c.add_target(p, "x", "   ")[0] and not c.add_target(p, "status", "t")[0], "bad add_target")
        check(c.add_target(p, "term2", "Other AI", classes=["CASCADIA_HOSTING_WINDOW_CLASS"])[0], "add with class")
        check(c.load_targets(p)["term2"]["class_whitelist"] == ["CASCADIA_HOSTING_WINDOW_CLASS"], "class saved")
        check(c.remove_target(p, "term2")[0] and not c.remove_target(p, "term2")[0], "remove target")
        check(sha(p.default_targets) == pkg_hash, "the shipped targets.json must never change")
        check("title 'Untitled - Notepad'" in c.describe_target(p, "notes", c.load_targets(p)["notes"]), "describe")
        with open(p.targets, "rb") as f:
            good_targets = f.read()
        write(p.targets, "{broken")
        check(raises(ValueError, c.load_targets, p), "a broken targets.json must raise, not fall back silently")
        r = c.enqueue(p, "agent", "x")
        check(not r["ok"] and r["reason"].startswith("targets_unreadable"), r)
        with open(p.targets, "wb") as f:
            f.write(good_targets)

        # ---- enqueue validation
        for args, kw, want in ((("nobody", "hi"), {}, "unknown_target"), (("agent", "   "), {}, "empty_text"),
                               (("agent", "x" * (c.MAX_TEXT + 1)), {}, "text_too_long"),
                               (("agent", "hi"), {"priority": "urgent"}, "bad_priority"),
                               (("notes", "hi"), {"mention": "阿明"}, "mention_only_for_line_targets")):
            r = c.enqueue(p, *args, **kw)
            check(not r["ok"] and r["reason"].startswith(want), "enqueue %r %r -> %r" % (args[:1], kw, r))
        for flag in (p.paused, p.agent_paused):
            write(flag, "x")
            r = c.enqueue(p, "agent", "hi")
            check(not r["ok"] and r["reason"].startswith("paused"), "paused: %r" % r)
            os.remove(flag)
        check(not os.path.exists(p.queue) or not c.queued(p), "nothing queued by refused sends")

        # ---- queue order: high first, each oldest first
        ra = c.enqueue(p, "agent", "第一則", source="cli")
        rb = c.enqueue(p, "notes", "note one", source="cli")
        rc = c.enqueue(p, "notes", "note two", source="cli")
        rd = c.enqueue(p, "notes", "urgent note", source="cli", priority="high")
        check(all(r["ok"] for r in (ra, rb, rc, rd)), "enqueue ok")
        check(ra["priority"] == "high" and rb["priority"] == "normal" and rd["priority"] == "high",
              "a high target upgrades the priority")
        order = [c.read_json(x[1])["id"] for x in c.queued(p)]
        check(order == [ra["id"], rd["id"], rb["id"], rc["id"]], "queue order %r" % order)
        rec = c.read_json(c.queued(p)[0][1])
        check(rec["text"] == "第一則" and rec["attempts"] == 0 and rec["source"] == "cli", "queued record %r" % rec)

        # ---- dispatcher with a fake sender (nothing is typed) and a fake alerter (nothing is sent)
        clock = [time.time()]
        sent, script, alerts = [], [], []

        def fake_sender(name, tgt, rec, text):
            sent.append((name, text, rec.get("mention")))
            return script.pop(0) if script else {"ok": True, "detail": "fake ok"}

        dp = d.Dispatcher(p, sender=fake_sender, alerter=lambda text: alerts.append(text) or "sent",
                          clock=lambda: clock[0], async_alerts=False)
        nxt = c.pick_next(p, clock[0])
        check(nxt and nxt[2]["id"] == ra["id"], "first pick is the high agent message")
        check(dp.process(*nxt) == "done", "process ok")
        check(sent[-1] == ("agent", "[cli→agent] 第一則", None), "typed text with prefix: %r" % (sent[-1],))
        last = c.tail_jsonl(p.done, 1)[0]
        check(last["id"] == ra["id"] and last["state"] == "done" and last["result"] == "fake ok"
              and last["attempts"] == 1, "done record %r" % last)
        check(not c.inflight_items(p), "inflight empty after done")

        fail_other = {"ok": False, "reason": "could not bring it to the front", "kind": "other"}
        nxt = c.pick_next(p, clock[0])
        check(nxt[2]["id"] == rd["id"], "then the high notes message")
        script.append(dict(fail_other))
        check(dp.process(*nxt) == "retry", "first failure -> retry")
        rec = c.read_json(nxt[1])
        check(rec["attempts"] == 1 and abs(rec["not_before"] - clock[0] - 2) < 1e-6 and rec["state"] == "queued"
              and rec["last_error"] == fail_other["reason"] and not c.inflight_items(p), "retry record %r" % rec)
        check(c.pick_next(p, clock[0]) is None, "a waiting retry holds back later messages for the same target")
        re_ = c.enqueue(p, "family", "晚上\n吃飯", source="cli", mention="阿明")
        nxt = c.pick_next(p, clock[0])
        check(nxt and nxt[2]["id"] == re_["id"], "another target goes ahead meanwhile")
        check(dp.process(*nxt) == "done" and sent[-1] == ("family", "晚上\n吃飯", "阿明"), "LINE: no prefix, mention")
        for attempt, delay in ((2, 4), (3, 8)):
            clock[0] += 100
            nxt = c.pick_next(p, clock[0])
            check(nxt[2]["id"] == rd["id"], "retry picked again")
            script.append(dict(fail_other))
            check(dp.process(*nxt) == "retry", "retry %d" % attempt)
            rec = c.read_json(nxt[1])
            check(rec["attempts"] == attempt and abs(rec["not_before"] - clock[0] - delay) < 1e-6, "backoff %r" % rec)
        clock[0] += 100
        nxt = c.pick_next(p, clock[0])
        script.append(dict(fail_other))
        check(dp.process(*nxt) == "failed", "4th failure -> failed")
        last = c.tail_jsonl(p.failed, 1)[0]
        check(last["id"] == rd["id"] and last["attempts"] == 4 and last["final_reason"] == fail_other["reason"]
              and last["maybe_sent"] is False, "failed record %r" % last)
        check(len(alerts) == 1 and "notes" in alerts[0] and "試了 4 次" in alerts[0], "one alert: %r" % alerts)

        nxt = c.pick_next(p, clock[0])
        check(nxt[2]["id"] == rb["id"], "next normal message")
        script.append({"ok": False, "reason": "paste ... (keys were already pressed)", "kind": "other",
                       "maybe_sent": True})
        check(dp.process(*nxt) == "failed", "maybe-sent is never retried")
        last = c.tail_jsonl(p.failed, 1)[0]
        check(last["id"] == rb["id"] and last["attempts"] == 1 and last["maybe_sent"] is True, "maybe-sent record")
        check(len(alerts) == 1 and dp._suppressed.get("notes") == 1, "second alert for the same target is held back")

        clock[0] += 601
        nxt = c.pick_next(p, clock[0])
        check(nxt[2]["id"] == rc["id"], "last notes message")
        script.append({"ok": False, "reason": "nothing matches 'Untitled - Notepad' exactly", "kind": "missing"})
        check(dp.process(*nxt) == "retry", "missing window -> retry")
        check(abs(c.read_json(nxt[1])["not_before"] - clock[0] - 15) < 1e-6, "missing window waits 15 s")
        check(c.remove_target(p, "notes")[0], "remove notes")
        clock[0] += 16
        n_sent = len(sent)
        nxt = c.pick_next(p, clock[0])
        check(dp.process(*nxt) == "failed" and len(sent) == n_sent, "unknown target fails at once, nothing typed")
        check("unknown target" in c.tail_jsonl(p.failed, 1)[0]["final_reason"], "unknown target reason")
        check(len(alerts) == 2 and "另有 1 則" in alerts[1], "cooldown over: alert with the held-back count")
        check(len(sent) == 8, "fake sender calls: %d" % len(sent))
        check(c.pick_next(p, clock[0]) is None and not c.queued(p), "queue empty")

        # ---- the real winsend adapter, driven by a fake winsend module (nothing is typed)
        ws = d.load_winsend(c.Paths())              # the real winsend.py next to this package; only pure parts used
        check(all(hasattr(ws, a) for a in d.WINSEND_NEEDS), "winsend.py has everything winject uses")
        W = ws.Win
        items = [W(1, "測試 AI", "CASCADIA_HOSTING_WINDOW_CLASS", True, False, 1, "WindowsTerminal.exe"),
                 W(2, "測試 AI - 筆記", "Notepad", True, False, 2, "notepad.exe"),
                 W(3, "ＡＢＣ", "Notepad", True, False, 3, "notepad.exe"),
                 W(4, "Dup", "X", True, False, 4, "x.exe"), W(5, "Dup", "X", True, False, 5, "x.exe")]
        key = (lambda w: w.title)
        strict = c.make_strict_matcher(ws.match_title, ws.norm_title)
        check(strict(items, "測試 AI", key)[0].hwnd == 1, "strict: whole title")
        check(ws.match_title(items, "筆記", key)[0].hwnd == 2 and strict(items, "筆記", key)[0] is None,
              "strict refuses a partial title that winsend alone would accept")
        check(strict(items, "abc", key)[0].hwnd == 3, "strict: ignores case and full/half width")
        r = strict(items, "Dup", key)
        check(r[0] is None and "2 windows" in r[1], "strict: two equal titles -> ambiguous: %r" % (r,))
        check(c.classify(strict(items, "zzz", key)[1]) == "missing", "strict 'nothing matches' -> missing")
        strict_cls = c.make_strict_matcher(ws.match_title, ws.norm_title, ["CASCADIA_HOSTING_WINDOW_CLASS"])
        check(strict_cls(items, "測試 AI", key)[0].hwnd == 1 and strict_cls(items, "ＡＢＣ", key)[0] is None,
              "class whitelist")

        calls = []
        orig_match = ws.match_title

        class FakeFail(Exception):
            def __init__(self, reason, maybe_sent=False, focus_lost=False):
                super().__init__(reason)
                self.reason, self.maybe_sent, self.focus_lost = reason, maybe_sent, focus_lost

        class FakeLock:
            def __enter__(self):
                calls.append("lock")

            def __exit__(self, *exc):
                calls.append("unlock")
                return False

        behaviour = {"window": None, "line": None}

        def fake_window(title, text, enter=True):
            calls.append(("window", title, text, enter, fake.match_title(items, "筆記", key)[0] is None))
            if behaviour["window"]:
                raise behaviour["window"]
            return "typed into %r" % title

        def fake_line(title, text, mention=None, enter=True):
            calls.append(("line", title, text, mention, enter))
            if behaviour["line"]:
                raise behaviour["line"]
            return "sent to LINE chat %r" % title

        fake = types.SimpleNamespace(match_title=orig_match, norm_title=ws.norm_title, SendLock=FakeLock,
                                     SendFail=FakeFail, send_window=fake_window, send_line=fake_line)
        sender = d.WinsendSender(p, ws=fake)
        tgts = c.load_targets(p)
        r = sender("agent", tgts["agent"], {"text": "hi"}, "[cli→agent] hi")
        check(r == {"ok": True, "detail": "typed into '測試 AI'"}, "adapter ok: %r" % r)
        check(calls[:3] == ["lock", ("window", "測試 AI", "[cli→agent] hi", True, True), "unlock"],
              "adapter: inside winsend's lock, strict matching during the send: %r" % calls)
        check(fake.match_title is orig_match, "winsend.match_title restored")
        behaviour["line"] = FakeFail("paste check failed ... NOT sent")
        r = sender("family", tgts["family"], {"text": "x", "mention": "阿明"}, "x")
        check(not r["ok"] and r["kind"] == "other" and r["maybe_sent"] is False and calls[-2][3] == "阿明",
              "adapter: LINE failure %r" % r)
        behaviour["window"] = FakeFail("pressing Enter ... (keys were already pressed)", maybe_sent=True)
        r = sender("agent", tgts["agent"], {"text": "x"}, "x")
        check(not r["ok"] and r["maybe_sent"] is True and fake.match_title is orig_match, "adapter: maybe-sent")
        behaviour["window"] = FakeFail("another window came to the front before pasting", focus_lost=True)
        r = sender("agent", tgts["agent"], {"text": "x"}, "x")
        check(r["kind"] == "user" and r["maybe_sent"] is False, "adapter: focus lost before paste -> retry: %r" % r)
        behaviour["window"] = FakeFail("another window came to the front before pressing Enter (the text was pasted "
                                       "into the window but not sent); stopped there", focus_lost=True)
        r = sender("agent", tgts["agent"], {"text": "x"}, "x")
        check(r["maybe_sent"] is True, "adapter: pasted but not sent -> never retried (no double paste): %r" % r)
        behaviour["window"] = FakeFail("unexpected OSError: x (details in winsend.log)")
        check(sender("agent", tgts["agent"], {"text": "x"}, "x")["maybe_sent"] is True,
              "adapter: winsend's 'unexpected' during a window send -> never retried")
        behaviour["line"] = FakeFail("another window came to the front before pressing Enter to send (the message "
                                     "is in the input box but was not sent); stopped there", focus_lost=True)
        r = sender("family", tgts["family"], {"text": "x"}, "x")
        check(r["kind"] == "user" and r["maybe_sent"] is False, "adapter: LINE clears its box, so it may retry: %r" % r)
        behaviour["window"] = RuntimeError("boom")
        r = sender("agent", tgts["agent"], {"text": "x"}, "x")
        check(not r["ok"] and r["kind"] == "other" and "boom" in r["reason"] and fake.match_title is orig_match,
              "adapter: unexpected error")
        check(sender("bad", {"type": "sms", "title": "x"}, {"text": "x"}, "x")["kind"] == "bad", "adapter: bad type")
        check(sender("bad", {"title_from_config": "nope"}, {"text": "x"}, "x")["kind"] == "bad", "adapter: no title")

        # ---- text rules
        win, line = {"type": "window"}, {"type": "line"}

        def comp(text, source, target, tgt, **extra):
            return c.compose_text(dict({"text": text, "source": source, "target": target}, **extra), tgt)

        check(comp("hi", "cli", "agent", win) == "[cli→agent] hi", "prefix")
        check(comp("hi", "system:boot", "agent", win) == "hi", "no-prefix source with :suffix")
        check(comp("hi", "Scheduler", "agent", win) == "hi", "no-prefix sources ignore case")
        check(comp("hi", "", "agent", win) == "hi", "empty source: no prefix")
        check(comp("hi", "cli", "agent", win, prefix=False) == "hi", "--no-prefix")
        check(comp("hi", "cli", "agent", {"type": "window", "prefix": False}) == "hi", "target prefix off")
        check(comp("hi", "cli", "fam", line) == "hi", "LINE: no prefix by default")
        check(comp("hi", "cli", "fam", {"type": "line", "prefix": True}) == "[cli→fam] hi", "LINE prefix when asked")
        check(comp("ＡＢＣ，１２３　中文。", "raw", "x", {"type": "window", "halfwidth": True}) == "ABC,123 中文。",
              "halfwidth")
        check(comp("ＡＢＣ", "raw", "x", win) == "ＡＢＣ", "no halfwidth unless asked")
        check(c.halfwidth("「你好」！") == "「你好」!", "halfwidth keeps CJK brackets")
        check([c.backoff("other", n) for n in (1, 2, 3, 4)] == [2, 4, 8, None], "backoff other")
        check([c.backoff("missing", n) for n in (1, 2, 3, 4)] == [15, 30, 60, None], "backoff missing")
        check([c.backoff("user", n) for n in (1, 2, 3, 4)] == [10, 20, 40, None], "backoff user")
        check(c.backoff("bad", 1) is None and c.backoff("other", 0) is None, "no backoff for bad")
        for reason, fl, want in (("nothing matches 'x' exactly (see: winsend.py list)", False, "missing"),
                                 ("LINE is not running (start LINE desktop ...)", False, "missing"),
                                 ("LINE is running but no chat is open in a window of its own; open ...", False,
                                  "missing"),
                                 ("another window came to the front before pasting; stopped there", True, "user"),
                                 ("the text is empty", False, "bad"),
                                 ("another winsend is still running (waited 60 s); try again later", False, "other"),
                                 ("the clipboard is busy (another program keeps it open); try again", False, "other")):
            check(c.classify(reason, fl) == want, "classify %r -> %s" % (reason, c.classify(reason, fl)))

        # ---- start-up recovery of in-flight leftovers
        os.makedirs(p.inflight, exist_ok=True)
        c.write_json(os.path.join(p.inflight, "%020d-aaa1.json" % 1),
                     {"id": "aaa1", "target": "agent", "text": "x", "state": "sending", "attempts": 1,
                      "priority": "high", "source": "cli"})
        c.write_json(os.path.join(p.inflight, "%020d-%s.json" % (2, ra["id"])),
                     {"id": ra["id"], "target": "agent", "text": "x", "state": "sending"})
        c.write_json(os.path.join(p.inflight, "%020d-aaa3.json" % 3),
                     {"id": "aaa3", "target": "agent", "text": "y", "attempts": 0, "priority": "normal"})
        n_alerts = len(alerts)
        check(dp.recover_inflight() == 1 and not c.inflight_items(p), "recovery")
        last = c.tail_jsonl(p.failed, 1)[0]
        check(last["id"] == "aaa1" and last["maybe_sent"] is True and last["final_reason"].startswith("interrupted"),
              "interrupted send recorded as maybe-sent, not resent: %r" % last)
        check(len(alerts) == n_alerts + 1, "interrupted send alerts the owner")
        check([os.path.basename(x[1]) for x in c.queued(p)] == ["%020d-aaa3.json" % 3], "untried item requeued")

        # ---- results, cancel
        check(c.find_result(p, ra["id"])[0] == "done" and c.find_result(p, rd["id"])[0] == "failed", "find_result")
        check(c.find_result(p, "aaa3")[0] == "queued" and c.find_result(p, "nope") is None, "find_result queued/none")
        check(c.cancel(p, "aaa3") == 1 and c.cancel(p, "nope") == 0, "cancel one")
        check(c.tail_jsonl(p.failed, 1)[0]["final_reason"] == "cancelled", "cancel recorded")
        c.enqueue(p, "agent", "a")
        c.enqueue(p, "agent", "b")
        check(c.cancel(p, "all") == 2 and not c.queued(p), "cancel all")

        # ---- files written by other programs
        bad = os.path.join(p.qdir["normal"], "%020d-bad.json" % 5)
        write(bad, "{not json", age=120)
        fresh = os.path.join(p.qdir["normal"], "%020d-fresh.json" % 6)
        write(fresh, "{still being writ")
        check(c.pick_next(p, time.time()) is None, "unreadable files are skipped")
        check(not os.path.exists(bad) and os.path.exists(fresh), "old unreadable file quarantined, fresh one kept")
        check(c.tail_jsonl(p.failed, 1)[0]["final_reason"].startswith("bad_request"), "quarantine recorded")
        os.remove(fresh)
        ext = os.path.join(p.qdir["normal"], "%020d-ext.json" % 7)
        write(ext, '{"target": "agent", "text": "hello"}')
        nxt = c.pick_next(p, time.time())
        check(nxt and nxt[2]["id"] == "%020d-ext" % 7 and nxt[2]["source"] == "file" and nxt[2]["attempts"] == 0,
              "a minimal file dropped in by another program is accepted: %r" % (nxt,))
        check(c.cancel(p, "all") == 1, "cancel the external item")
        jump = os.path.join(p.qdir["normal"], "%020d-jump.json" % 8)
        write(jump, '{"target": "agent", "text": "x", "not_before": %f}' % (time.time() + 7200))
        check((c.pick_next(p, time.time()) or [None, None, {}])[2].get("id") == "%020d-jump" % 8,
              "a retry time far in the future (clock jumped back) does not hold the queue")
        check(c.cancel(p, "all") == 1, "cancel the clock-jump item")
        r = c.enqueue(p, "agent", "x", source="  my \n script  " + "x" * 60)
        check(r["ok"] and c.find_result(p, r["id"])[1]["source"] == ("my script " + "x" * 60)[:40],
              "source is one line, at most 40 characters")
        check(c.cancel(p, r["id"]) == 1, "cancel by id")

        # ---- rotation and cleanup
        old = time.time() - 3 * 86400
        write(p.done, '{"id": "o1", "completed": "%s"}\n{"id": "t1", "completed": "%s"}\n'
              % (c.now_iso(old), c.now_iso()))
        acts = c.rotate_logs(p)
        old_day = dt.date.fromtimestamp(old).isoformat()
        check(not os.path.exists(p.done) and os.path.exists(os.path.join(p.archive, "done-%s.jsonl" % old_day)),
              "done.jsonl holding an earlier day is archived: %r" % acts)
        c.append_jsonl(p, p.done, {"id": "t2", "completed": c.now_iso()})
        c.rotate_logs(p)
        check(os.path.exists(p.done), "today's done.jsonl stays")
        stale = os.path.join(p.archive, "done-2000-01-01.jsonl")
        write(stale, "{}\n", age=(c.ARCHIVE_DAYS + 5) * 86400)
        c.rotate_logs(p)
        check(not os.path.exists(stale), "archives older than %d days are deleted" % c.ARCHIVE_DAYS)
        acts = c.rotate_logs(p, force=True)
        check(not os.path.exists(p.done) and not os.path.exists(p.failed) and len(acts) == 2, "archive now: %r" % acts)
        old_tmp = os.path.join(p.qdir["high"], "x.json.1.2.tmp")
        new_tmp = os.path.join(p.qdir["high"], "y.json.1.2.tmp")
        write(old_tmp, "x", age=c.TEMP_MAX_AGE + 60)
        write(new_tmp, "x")
        check(c.cleanup_temp(p) == 1 and not os.path.exists(old_tmp) and os.path.exists(new_tmp), "temp cleanup")
        os.remove(new_tmp)
        left = os.path.join(p.qdir["normal"], "%020d-lost.json.cancel" % 9)
        write(left, '{"id": "lost", "target": "agent", "text": "x"}', age=c.TEMP_MAX_AGE + 60)
        check(c.cleanup_temp(p) == 1 and not os.path.exists(left), "leftover .cancel cleaned")
        last = c.tail_jsonl(p.failed, 1)[0]
        check(last["id"] == "lost" and last["final_reason"] == "cancelled", "a half-done cancel is still recorded")

        # ---- heartbeat, stop request, health verdicts
        now = time.time()
        check(control.assess(None, False, now) == "start" and control.assess(None, True, now) == "starting", "assess")
        check(control.assess({"beat": now - 3, "loop": now - 3}, True, now) == "ok", "assess ok")
        check(control.assess({"beat": now - 3, "loop": now - 3}, False, now) == "start", "assess: no process")
        check(control.assess({"beat": now - c.HB_STALE - 5, "loop": now}, True, now) == "hung", "assess: no beat")
        check(control.assess({"beat": now, "loop": now - c.LOOP_STALE - 5}, True, now) == "hung", "assess: loop stuck")
        check(control.assess({"beat": now - 5, "state": "stopped"}, True, now) == "starting", "assess: just restarted")
        check(control.assess({"beat": now - c.HB_STALE - 5, "state": "stopped"}, True, now) == "hung",
              "assess: stuck on its way out")
        check(control.assess({"beat": "junk"}, True, now) == "hung", "assess: junk heartbeat")
        mine = c.process_info(os.getpid())
        check(mine and os.path.basename(mine[0]).lower().startswith("python"), "process_info(self): %r" % (mine,))
        check(control.verify_pid({"pid": os.getpid(), "started": mine[1] + 0.5}), "verify own pid")
        check(not control.verify_pid({"pid": os.getpid(), "started": 1e9}), "pid reuse guard")
        check(not control.verify_pid({"pid": 0, "started": now}) and not control.verify_pid(None), "verify junk")
        dp.write_heartbeat()
        hb = control.read_heartbeat(p)
        check(hb["pid"] == os.getpid() and control.assess(hb, True, time.time()) == "ok", "heartbeat %r" % hb)
        dp.started = time.time()
        write(p.stop, "x")
        check(dp._stop_requested() and not os.path.exists(p.stop), "stop request")
        write(p.stop, "x", age=100)
        check(not dp._stop_requested() and not os.path.exists(p.stop), "stale stop request ignored")

        # ---- named mutexes (kernel objects only)
        name = "Local\\agent-winject-selftest-%d-%d" % (os.getpid(), int(time.time() * 1000))
        h = c.acquire_mutex(name)
        check(h and c.mutex_exists(name) and c.acquire_mutex(name) is None, "single-instance mutex")
        c.close_handle(h)
        check(not c.mutex_exists(name), "mutex released")
        with c.FileLock(p) as lk:
            check(lk.owned, "file lock")

        # ---- Telegram (fake runner; nothing leaves this computer)
        def must_not_run(*a, **k):
            raise _Fail("the Telegram runner must not be called")

        check(control.tg_send(p, "x", runner=must_not_run).startswith("skipped"), "no token -> skipped")
        c.write_json(p.config, {"agent_name": "測試 AI", "token": "123:fake", "owner_id": "42"})
        check(control.tg_send(p, "x", runner=must_not_run).startswith("skipped"), "no tgterm.py -> skipped")
        write(p.tgterm, "# fake tgterm for the selftest\n")
        got = {}

        def fake_run(args, **kw):
            got["args"], got["input"] = args, kw.get("input")
            return types.SimpleNamespace(returncode=0, stderr=b"")

        check(control.tg_send(p, "測試訊息", runner=fake_run) == "sent" and got["args"][-3:] == [p.tgterm, "send", "-"]
              and got["input"] == "測試訊息".encode("utf-8"), "tgterm send - with UTF-8 stdin: %r" % got)
        check(control.alert_with_cooldown(p, "k", "t", runner=fake_run, now=1000.0) == "sent"
              and control.alert_with_cooldown(p, "k", "t", runner=fake_run, now=1500.0).startswith("suppressed")
              and control.alert_with_cooldown(p, "k", "t", runner=fake_run, now=1000.0 + 3601) == "sent",
              "ensure alerts have a cooldown")
        c.write_json(p.config, {"agent_name": "測試 AI", "token": "", "owner_id": ""})

        # ---- command line parsing (nothing is run)
        a = cli.parse_send(["agent", "hi"])
        check((a.target, a.text, a.source, a.priority, a.wait, a.no_prefix) == ("agent", "hi", "cli", "normal", 0, False),
              "parse send %r" % a)
        check(cli.parse_send(["agent", "--", "-x"]).text == "-x", "-- before a text starting with -")
        a = cli.parse_send(["agent", "--source", "x", "hi"])
        check((a.target, a.text, a.source) == ("agent", "hi", "x"), "options between target and text: %r" % a)
        a = cli.parse_send(["--priority", "high", "agent", "--wait", "5", "hi", "--no-start"])
        check((a.target, a.text, a.priority, a.wait, a.no_start) == ("agent", "hi", "high", 5, True),
              "options anywhere: %r" % a)
        check(cli.parse_send(["agent", "-3 度"]).text == "-3 度", "dash text with a space")
        a = cli.parse_send(["agent", "--file", "m.txt", "--priority", "high", "--wait", "30", "--source", "小美",
                            "--no-prefix"])
        check((a.file, a.priority, a.wait, a.source, a.no_prefix) == ("m.txt", "high", 30, "小美", True), "options")
        for bad in (["agent"], ["agent", "a", "--file", "f"], ["agent", "a", "--priority", "urgent"],
                    ["agent", "a", "b"], ["agent", "a", "--wait", "-1"], ["agent", "a", "--bogus"], []):
            check(raises(cli.UsageError, cli.parse_send, bad), "should be a usage error: %r" % (bad,))
        ap = cli.add_target_parser()
        check(ap.parse_args(["n", "T"]).prefix is None and ap.parse_args(["n", "T", "--prefix"]).prefix is True
              and ap.parse_args(["n", "T", "--no-prefix"]).prefix is False, "add-target prefix flags")
        a = ap.parse_args(["家人", "家人群組", "--line", "--priority", "high", "--class", "A", "--class", "B"])
        check((a.line, a.priority, a.classes) == (True, "high", ["A", "B"]), "add-target options %r" % a)
        check(raises(cli.UsageError, ap.parse_args, ["n", "T", "--prefix", "--no-prefix"]), "prefix flags exclusive")
        check("queue:" in cli.render_status(cli.status_data(p)), "status renders")

        check(os.path.exists(real_data) == real_data_existed, "the real data folder was not touched")
    except _Fail as e:
        print("FAIL: selftest: %s" % (e,))
        return 1
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("OK: selftest passed (%d checks; no window, clipboard, mouse or keyboard was touched)" % count[0])
    return 0
