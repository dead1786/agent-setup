"""winject: one central queue for typing text into windows (another AI terminal, an app) and LINE chats.

Every program that wants to type into a window puts the text in the queue; one dispatcher process types the
messages strictly one at a time (through winsend.py), retries, logs and tells the owner on Telegram when a
message could not be delivered. Nothing here types by itself: send() only writes a file into the queue.

    import sys; sys.path.insert(0, r"C:\\AI\\.agent")
    from winject import send
    r = send("agent", "the build finished", source="my-script")
    # {"ok": True, "id": "...", ...}  queued (the result shows up in winject-data\\done.jsonl / failed.jsonl)
    # {"ok": False, "reason": "..."}  not queued (unknown target, empty text, paused, ...)

Command line: C:\\AI\\.agent\\py\\Scripts\\python.exe C:\\AI\\.agent\\winject\\cli.py --help
"""
from .common import VERSION, Paths, enqueue

__all__ = ["send", "VERSION"]


def send(target, text, source="unknown", priority="normal", *, mention=None, prefix=None):
    """queue `text` for `target` (a name from targets.json). priority: "normal" or "high" (taken first; a target
    marked high in targets.json is always high). source: who sends it; it becomes the "[source→target] " prefix
    unless the target or the source says no prefix. mention: LINE targets only, "@member" in front of the text.
    prefix=False: type the text exactly as given."""
    return enqueue(Paths(), target, text, source=source, priority=priority, mention=mention, prefix=prefix)
