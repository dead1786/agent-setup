# vault: the owner's website logins, filled into a Chrome login page for the AI without the AI ever seeing them
#   python vault.py fill <名稱> [--only user|pass] [--tab <分頁標題開頭>]   ask the owner on Telegram, then fill the page
#   python vault.py list                                                    names, sites, masked user names (never passwords)
#   python vault.py selftest                                                offline checks: temp folder and fakes only
# The owner manages entries from Telegram: /vault (/密碼) 新增 名稱 網址 帳號 密碼 | 清單 | 刪除 名稱. tgterm.py's bridge
# hands that text to owner_command() (never to the AI, a log or the inbox) and deletes the owner's message.
# Fill: find a Chrome tab whose host is the entry's site (or a subdomain of it) -> Telegram message with ✅ / ❌ buttons
# (sent here with the bridge's bot; the bridge receives the tap and calls decide()) -> re-check the tab's host right
# before each value is set -> set it through UI Automation (ValuePattern.SetValue: no keyboard, no clipboard). The
# password only goes into a password-type field. Output: "已填入…" or why not; there is no command that prints a secret.
# Storage (BASE = this folder, or AGENT_SETUP_AGENT_DIR like tgterm.py):
#   vault\vault.dat          the entries, encrypted with Windows DPAPI (this Windows account on this PC only)
#   vault\requests\<id>.json  approvals waiting for the owner's tap
#   vault\usage.log           what was filled where and the owner's answer (no secrets)
# Limits: the AI runs on this PC as the same Windows user. This keeps passwords out of the chat, the logs and the AI's
# context and needs a tap per use; it cannot stop a determined program on this PC from decrypting the store.
# guard.py blocks the AI from touching the vault folder and allows only `vault.py fill …` / `vault.py list`.
import ctypes, datetime, json, os, re, sys, time, uuid
from ctypes import wintypes
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.environ.get("AGENT_SETUP_AGENT_DIR") or HERE)
VAULT_DIR = os.path.join(BASE, "vault")
VAULT_FILE = os.path.join(VAULT_DIR, "vault.dat")
REQ_DIR = os.path.join(VAULT_DIR, "requests")
USAGE_LOG = os.path.join(VAULT_DIR, "usage.log")
ENTROPY = b"agent-setup-vault-v1"
APPROVE_SEC, POLL_SEC = 120, 1.0                       # the owner has 2 minutes to tap
WHAT_TEXT = {"both": "帳號＋密碼", "user": "帳號", "pass": "密碼"}
NAME_OK = re.compile(r"[\w.@+-]{1,40}")                # no spaces or shell characters: the AI types it after `fill`
MUTEX = "Local\\agent-vault"


# ---------------------------------------------------------------- Win32: DPAPI, a cross-process lock, process names
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_crypt = ctypes.WinDLL("crypt32", use_last_error=True)
_k32.LocalFree.argtypes = [ctypes.c_void_p]
_k32.LocalFree.restype = ctypes.c_void_p
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CreateMutexW.restype = wintypes.HANDLE
_k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
_u32.FindWindowExW.restype = wintypes.HWND
_u32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
for _fn in (_crypt.CryptProtectData, _crypt.CryptUnprotectData):
    _fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                    wintypes.DWORD, ctypes.POINTER(_Blob)]
    _fn.restype = wintypes.BOOL


def _dpapi(data, protect):
    buf = ctypes.create_string_buffer(data, len(data))
    ent = ctypes.create_string_buffer(ENTROPY, len(ENTROPY))
    inb = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    eb = _Blob(len(ENTROPY), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = _crypt.CryptProtectData if protect else _crypt.CryptUnprotectData
    if not fn(ctypes.byref(inb), None, ctypes.byref(eb), None, None, 0x1, ctypes.byref(out)):   # 0x1 = UI_FORBIDDEN
        raise OSError("DPAPI %s failed (%d)" % ("protect" if protect else "unprotect", ctypes.get_last_error()))
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _k32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


class _locked:
    """one process at a time on the store and the approval files (bridge: add / delete / a tap; vault.py: fill)"""
    def __enter__(self):
        self.h = _k32.CreateMutexW(None, False, MUTEX)
        self.owned = bool(self.h) and _k32.WaitForSingleObject(self.h, 15000) in (0, 0x80)   # signaled / abandoned
        return self

    def __exit__(self, *exc):
        if self.h:
            if self.owned:
                _k32.ReleaseMutex(self.h)
            _k32.CloseHandle(self.h)


def _exe_of(pid):
    h = _k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        ok = _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
        return os.path.basename(buf.value).lower() if ok else ""
    finally:
        _k32.CloseHandle(h)


# ---------------------------------------------------------------- store
def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    for i in range(20):                                 # the other process may be reading it this very moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 19:
                _remove(tmp)
                raise
            time.sleep(0.1)


def _load():
    try:
        with open(VAULT_FILE, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    data = json.loads(_dpapi(raw, False).decode("utf-8"))
    return data if isinstance(data, dict) else {}


def _save(d):
    _write_bytes(VAULT_FILE, _dpapi(json.dumps(d, ensure_ascii=False).encode("utf-8"), True))


def site_of(s):
    """'https://www.GitHub.com/login' -> 'github.com': the host only (Chrome's address bar has no scheme)"""
    s = (s or "").strip().lower()
    try:
        host = urlsplit(s if "://" in s else "http://" + s).hostname or ""
    except ValueError:
        return ""
    host = host.rstrip(".")
    return host[4:] if host.startswith("www.") else host


def host_matches(host, sites):
    """the tab's host is a bound site or a subdomain of one (login.github.com for github.com, never evilgithub.com)"""
    host = (host or "").lower().rstrip(".")
    return bool(host) and any(host == s or host.endswith("." + s) for s in sites if s)


def mask(user):
    return user[:2] + "***" if len(user) > 3 else "***"


class BadEntry(ValueError):
    """a reason the owner can read (never holds the values)"""


def add(name, site, user, password):
    """store (or replace) an entry -> the bound site; BadEntry when something is missing or unreadable"""
    s = site_of(site)
    if not NAME_OK.fullmatch(name or "") or name.startswith("-"):
        raise BadEntry("名稱只能用中英文、數字和 . _ - @ +，不能有空白，最多 40 個字")
    if not s or ("." not in s and s != "localhost") or not re.fullmatch(r"[^\s/\\@]+", s):
        raise BadEntry("網址看不懂，請寫像 github.com 或 https://github.com/login 這樣")
    if not user or not password:
        raise BadEntry("帳號和密碼都要有")
    with _locked():
        d = _load()
        d[name] = {"user": user, "pass": password, "sites": [s], "added": now(), "used": (d.get(name) or {}).get("used")}
        _save(d)
    return s


def remove(name):
    with _locked():
        d = _load()
        if name not in d:
            return False
        del d[name]
        _save(d)
    return True


def list_entries():
    """[(name, [sites], masked user, last used)]: never a password"""
    return [(n, e.get("sites", []), mask(e.get("user", "")), e.get("used")) for n, e in sorted(_load().items())]


def _usage(name, host, what, result):
    try:
        os.makedirs(VAULT_DIR, exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write("%s\t%s\t%s\t%s\t%s\n" % (now(), name, host, what, result))
    except OSError:
        pass


# ---------------------------------------------------------------- the owner's /vault command (called by the bridge)
PREFIX = re.compile(r"\s*[/／]\s*(?:vault|密碼庫|密碼)(?:@[A-Za-z0-9_]+)?", re.I)
SUBS = {"新增": "add", "存": "add", "加": "add", "add": "add", "清單": "list", "列表": "list", "list": "list",
        "刪除": "del", "刪": "del", "del": "del", "delete": "del", "remove": "del", "說明": "help", "help": "help"}
HELP = ("🔐 密碼庫：存網站帳密給 AI 幫你登入用，AI 看不到密碼\n"
        "・存：/密碼 新增 名稱 網址 帳號 密碼\n"
        "　例：/密碼 新增 github github.com 我的帳號 我的密碼（存好會自動刪掉你那則訊息）\n"
        "・看：/密碼 清單\n"
        "・刪：/密碼 刪除 名稱\n"
        "AI 要填的時候會傳按鈕問你，你按「✅ 允許」才會填，而且只填到存的那個網站（含子網域）。")


def parse_owner_command(text):
    """-> (sub, args): sub is add | list | del | help | other. The rest of the line after the user name is the
    password (spaces kept); names and the rest are single words"""
    m = PREFIX.match(text or "")
    words = (text[m.end():] if m else "").split(None, 4)
    if not words:
        return "help", []
    return SUBS.get(words[0].lower(), "other"), words[1:]


def list_text():
    rows = list_entries()
    body = "\n".join("・%s：%s（帳號 %s，上次用 %s）" % (n, "、".join(s), u, (used or "—").replace("T", " "))
                     for n, s, u, used in rows)
    return "🔐 密碼庫清單\n" + (body or "（目前是空的）")


def owner_command(text):
    """the owner's /vault message -> (reply, delete the message?). The reply never holds the password; anything that
    may hold one (新增, an unknown word, extra words) is to be deleted from the chat"""
    sub, args = parse_owner_command(text)
    drop = not ((sub in ("help", "list") and not args) or (sub == "del" and len(args) == 1))
    try:
        if sub == "add":
            if len(args) < 4:
                return "✗ 格式：/密碼 新增 名稱 網址 帳號 密碼\n例：/密碼 新增 github github.com 我的帳號 我的密碼", drop
            site = add(args[0], args[1], args[2], args[3])
            return ("✅ 已存「%s」：綁定 %s，帳號 %s\nAI 只能把它填到 %s（含子網域）的登入頁，每次都會先問你。"
                    % (args[0], site, mask(args[2]), site)), drop
        if sub == "list":
            return list_text(), drop
        if sub == "del":
            if len(args) != 1:
                return "✗ 格式：/密碼 刪除 名稱（名稱看 /密碼 清單）", drop
            return ("🗑 已刪除「%s」" % args[0] if remove(args[0]) else "✗ 密碼庫裡沒有「%s」" % args[0]), drop
        if sub == "help" and not args:
            return HELP + "\n\n" + list_text(), drop
        return "✗ 看不懂這個指令。\n" + HELP, drop
    except BadEntry as e:
        return "✗ %s" % e, drop                         # add()'s own reasons, never the values
    except Exception as e:
        return "✗ 密碼庫出錯（%s）" % type(e).__name__, drop


# ---------------------------------------------------------------- approvals (the bridge calls decide() on a tap)
def _req_path(req):
    return os.path.join(REQ_DIR, req + ".json")


def _read_req(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return None


def _write_req(path, data):
    _write_bytes(path, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def decide(req, verdict):
    """the owner tapped ✅ (ok) or ❌ (no): recorded only while the request still waits -> (changed, note for the owner)"""
    if not re.fullmatch(r"[0-9a-f]{12}", req or "") or verdict not in ("ok", "no"):
        return False, "看不懂的按鈕"
    path = _req_path(req)
    with _locked():
        r = _read_req(path)
        if r is None:
            return False, "這個請求已經結束了"
        if r.get("status") != "pending" or time.time() - float(r.get("ts") or 0) > APPROVE_SEC + 10:
            return False, "這個請求已經結束（%s）" % {"ok": "已允許", "no": "已拒絕"}.get(r.get("status"), "已逾時")
        r["status"], r["decided"] = verdict, now()
        _write_req(path, r)
    return True, "已允許" if verdict == "ok" else "已拒絕"


def _telegram():
    """(tgterm module, its config): the approval goes out with the bridge's own bot and topic logic. Only sendMessage
    and editMessageText here; the bridge is the only getUpdates reader and receives the tap"""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import tgterm
    cfg = tgterm.load_json(tgterm.CFG, None)
    return tgterm, (cfg if isinstance(cfg, dict) else {})


def telegram_problem(t, cfg):
    """why no approval can be asked on this install, or None"""
    if not (cfg.get("token") and cfg.get("owner_id")):
        return "這台沒有接 Telegram：每次填密碼都要主人在 Telegram 按允許，所以密碼庫不能用。請主人自己輸入密碼。"
    if not hasattr(t, "on_button"):                     # a tgterm.py kept from before the vault (changed here)
        return "Telegram 連線程式 tgterm.py 是舊版（被改過、沒有更新），收不到允許按鈕。請主人先處理更新（/update）再用密碼庫。"
    if not t.bridge_running():
        return "Telegram 連線（tgterm.py bridge）沒有在跑，收不到主人按的按鈕。看門狗 1 分鐘內會把它開回來，等一下再試。"
    return None


def _clean_requests():
    try:
        for n in os.listdir(REQ_DIR):
            p = os.path.join(REQ_DIR, n)
            if time.time() - os.path.getmtime(p) > 3600:
                _remove(p)
    except OSError:
        pass


def wait_decision(path, seconds=None):
    """'ok' / 'no' once the owner taps, else 'expired' (written under the lock, so a late tap cannot slip in)"""
    end = time.time() + (APPROVE_SEC if seconds is None else seconds)
    while True:
        st = (_read_req(path) or {}).get("status")
        if st in ("ok", "no"):
            return st
        if time.time() >= end:
            with _locked():
                r = _read_req(path) or {}
                if r.get("status") in ("ok", "no"):
                    return r["status"]
                _write_req(path, dict(r, status="expired"))
            return "expired"
        time.sleep(POLL_SEC)


def ask_approval(name, what, host, telegram=None):
    """✅ / ❌ buttons to the owner, then wait -> 'ok' / 'no' / 'expired' / 'error: …'"""
    t, cfg = telegram or _telegram()
    why = telegram_problem(t, cfg)
    if why:
        return "error: " + why
    _clean_requests()
    req = uuid.uuid4().hex[:12]
    path = _req_path(req)
    _write_req(path, {"status": "pending", "ts": time.time(), "name": name, "host": host, "what": what})
    text = ("🔐 %s 要把「%s」的%s填進 Chrome 的 %s\n是你要它登入的嗎？2 分鐘內沒按＝取消"
            % (cfg.get("agent_name") or "AI", name, WHAT_TEXT[what], host))
    kb = json.dumps({"inline_keyboard": [[{"text": "✅ 允許", "callback_data": "vault:%s:ok" % req},
                                          {"text": "❌ 拒絕", "callback_data": "vault:%s:no" % req}]]}, ensure_ascii=False)
    try:
        res, _ = t.in_thread(t.reply_thread(cfg), t.tg, cfg, "sendMessage", chat_id=cfg["owner_id"], text=text,
                             reply_markup=kb)
        mid = res["result"]["message_id"]
    except Exception as e:
        _remove(path)
        return "error: 按鈕沒送出（%s）" % type(e).__name__
    status = wait_decision(path)
    if status == "expired":
        try:                                            # the buttons go away with the edit
            t.tg(cfg, "editMessageText", chat_id=cfg["owner_id"], message_id=mid, text=text + "\n⌛ 逾時，沒有填")
        except Exception:
            pass
    _remove(path)
    return status


# ---------------------------------------------------------------- Chrome through UI Automation
class MissingUI(Exception):
    pass


IS_PASSWORD = 30019                                     # UIA_IsPasswordPropertyId
USER_RE = re.compile(r"user|login|e-?mail|account|帳號|账号|信箱|郵件|邮箱|手機|使用者|用戶|会员|會員", re.I)


class ChromeUI:
    """the owner's Chrome. UI Automation sees only each window's active tab; another tab is brought forward with
    SelectionItemPattern.Select(), which does not take the keyboard focus"""
    def __init__(self):
        try:
            import warnings
            warnings.filterwarnings("ignore")
            import uiautomation
        except Exception as e:
            raise MissingUI("%s: %s" % (type(e).__name__, e))
        self.auto = uiautomation

    def windows(self):
        return [w for w in self.auto.GetRootControl().GetChildren()
                if w.ClassName == "Chrome_WidgetWin_1" and _exe_of(w.ProcessId) == "chrome.exe"]   # Electron apps share the class

    def host(self, w):
        """the active tab's host, from the address bar (its value has no scheme; its name is localized)"""
        ed = w.EditControl(searchDepth=10)
        if not ed.Exists(1, 0.2):
            return ""
        vp = ed.GetPattern(self.auto.PatternId.ValuePattern)
        return site_of(vp.Value) if vp else ""

    def tabs(self, w):
        out = []

        def walk(c, d=0):
            if d > 14:
                return
            for ch in c.GetChildren():
                if ch.ControlTypeName == "TabItemControl":
                    out.append(ch)
                elif ch.ControlTypeName != "DocumentControl":   # never into the web page itself
                    walk(ch, d + 1)
        walk(w)
        return out

    def select(self, tab):
        p = tab.GetPattern(self.auto.PatternId.SelectionItemPattern)
        if p:
            p.Select()
            time.sleep(0.6)
        return bool(p)

    def is_selected(self, tab):
        p = tab.GetPattern(self.auto.PatternId.SelectionItemPattern)
        return bool(p and p.IsSelected)

    def find(self, sites, tab_title=None):
        """(window, host): a window whose active tab is on the site; else switch through its tabs (by title when one is
        given), and switch back when none is"""
        wins = self.windows()
        for w in wins:
            h = self.host(w)
            if host_matches(h, sites) and (not tab_title or w.Name.startswith(tab_title)):
                return w, h
        for w in wins:
            tabs = self.tabs(w)
            orig = next((t for t in tabs if self.is_selected(t)), None)
            cands = [t for t in tabs if t.Name.startswith(tab_title)] if tab_title else tabs
            for t in cands[:40]:
                if t is orig or not self.select(t):
                    continue
                h = self.host(w)
                if host_matches(h, sites):
                    return w, h
            if orig:
                self.select(orig)
        return None, ""

    def page_doc(self, w):
        """the active tab's web page in the window's own tree: the DocumentControl named like the tab (window title
        minus " - Google Chrome"). Exactly one or none, so the address bar and side panels (an AI chat box…) never count"""
        title = (w.Name or "").rsplit(" - ", 1)[0]
        docs = []

        def walk(c, d=0):
            if d > 30:
                return
            for ch in c.GetChildren():
                if ch.ControlTypeName == "DocumentControl":
                    if title and ch.Name == title:
                        docs.append(ch)
                else:
                    walk(ch, d + 1)
        walk(w)
        return docs[0] if len(docs) == 1 else None

    def fields(self, w):
        """the active tab's input boxes in page order. Chrome shows the page under Chrome_RenderWidgetHostHWND or (seen in
        a second window on 11-05, that HWND hidden and empty) under a DocumentControl in the window's tree; it opens
        the page's accessibility tree on the first query, so try both and retry"""
        hwnd = _u32.FindWindowExW(w.NativeWindowHandle, None, "Chrome_RenderWidgetHostHWND", None)
        for _ in range(4):
            edits = []

            def walk(c, d=0):
                if d > 40:
                    return
                for ch in c.GetChildren():
                    if ch.ControlTypeName == "EditControl" and ch.IsEnabled:
                        edits.append(ch)
                    walk(ch, d + 1)
            if hwnd:
                walk(self.auto.ControlFromHandle(hwnd))
            if not edits:
                doc = self.page_doc(w)
                if doc:
                    walk(doc)
            if edits:
                return edits
            time.sleep(1.2)
        return []

    def is_password(self, e):
        return bool(e.GetPropertyValue(IS_PASSWORD))

    def label(self, e):
        return "%s %s" % (e.AutomationId or "", e.Name or "")

    def set_value(self, e, value):
        """fires the page's input and change events like typing would (checked 11-05)"""
        e.GetPattern(self.auto.PatternId.ValuePattern).SetValue(value)


def pick_fields(edits, only, ui):
    """(user field, password field): the password only into a password-type field; the user field is one named like a
    user / e-mail box, else the box right before the password"""
    pw = [e for e in edits if ui.is_password(e)]
    others = [e for e in edits if not ui.is_password(e)]
    user = None
    if only != "pass":
        named = [e for e in others if USER_RE.search(ui.label(e))]
        if named:
            user = named[0]
        elif pw:
            before = [e for e in edits[:edits.index(pw[0])] if not ui.is_password(e)]
            user = before[-1] if before else None
        elif len(others) == 1:
            user = others[0]
    return user, (pw[0] if pw and only != "user" else None)


def missing_ui_text(e):
    uv = os.path.join(os.path.expanduser("~"), ".local", "bin", "uv.exe")
    return ("這台電腦還沒裝操作 Chrome 用的 uiautomation 套件（%s）。主人在 Telegram 打 /update 會自動安裝；"
            "或執行：%s pip install --python %s uiautomation，裝好再試一次。" % (e, uv, sys.executable))


# ---------------------------------------------------------------- fill
def fill(name, only="both", tab_title=None, approve=None, ui=None):
    """-> (ok, text for the AI); the text never holds a user name or password"""
    if not _load().get(name):
        return False, "密碼庫裡沒有「%s」。用 vault.py list 看有哪些；要新增請主人在 Telegram 打 /密碼 新增 …（不要叫主人把密碼傳給你）" % name
    if approve is None:
        t, cfg = _telegram()
        why = telegram_problem(t, cfg)
        if why:
            return False, why
        approve = lambda n, w, h: ask_approval(n, w, h, (t, cfg))
    if ui is None:
        try:
            ui = ChromeUI()
        except MissingUI as e:
            return False, missing_ui_text(e)
    sites = _load()[name].get("sites", [])
    w, host = ui.find(sites, tab_title)
    if not w:
        return False, "Chrome 裡找不到網址是 %s 的分頁%s（先用 Chrome 打開登入頁再執行）" % (
            "、".join(sites), "（標題開頭「%s」）" % tab_title if tab_title else "")
    user_f, pass_f = pick_fields(ui.fields(w), only, ui)
    what = "both" if (user_f and pass_f) else ("pass" if pass_f else ("user" if user_f else None))
    if not what:
        return False, "%s 這頁找不到可以填的%s欄位" % (host, {"pass": "密碼", "user": "帳號"}.get(only, "帳號或密碼"))
    verdict = approve(name, what, host)
    if verdict != "ok":
        _usage(name, host, what, verdict if verdict in ("no", "expired") else "error")
        return False, {"no": "主人按了拒絕，沒有填。", "expired": "主人 2 分鐘內沒有按，取消，沒有填。"}.get(
            verdict, "沒有填：" + str(verdict).replace("error: ", ""))
    entry = _load().get(name)                           # read again: the owner may have changed or deleted it meanwhile
    if not entry:
        return False, "「%s」剛剛被主人刪掉了，沒有填。" % name
    sites = entry.get("sites", [])
    user_f, pass_f = pick_fields(ui.fields(w), only, ui)
    done = []
    for field, key, label, kinds in ((user_f, "user", "帳號", ("both", "user")), (pass_f, "pass", "密碼", ("both", "pass"))):
        if what not in kinds:
            continue                                    # only what the owner was asked about
        if not field:
            _usage(name, host, what, "field gone")
            return False, "按允許之後找不到%s欄位（網頁變了），%s。" % (label, "已填：" + "＋".join(done) if done else "沒有填")
        if not host_matches(ui.host(w), sites):         # every time: the tab may have changed after the tap
            _usage(name, host, what, "host changed")
            return False, "按允許之後分頁網址變了，為了安全%s沒有填，請重新執行一次。" % ("後面的" if done else "")
        try:
            ui.set_value(field, entry[key])
        except Exception as e:                          # the type only: nothing that could carry the value
            _usage(name, host, what, "set failed")
            return False, "填%s時出錯（%s），%s。" % (label, type(e).__name__, "已填：" + "＋".join(done) if done else "沒有填")
        done.append(label)
    with _locked():
        d = _load()
        if name in d:
            d[name]["used"] = now()
            _save(d)
    _usage(name, host, what, "filled")
    return True, "已填入「%s」的%s到 %s（還沒按登入；主人有要你登入才按）" % (name, "＋".join(done), host)


# ---------------------------------------------------------------- CLI
USAGE = ("用法：vault.py fill 名稱 [--only user|pass] [--tab 分頁標題開頭]\n"
         "      vault.py list")


def parse_cli(args):
    """-> ('fill', name, only, tab) / ('list',) ; ValueError with the usage"""
    if args == ["list"]:
        return ("list",)
    if len(args) < 2 or args[0] != "fill" or args[1].startswith("-"):
        raise ValueError(USAGE)
    only, tab, rest = "both", None, args[2:]
    while rest:
        if rest[0] == "--only" and len(rest) > 1 and rest[1] in ("user", "pass"):
            only = rest[1]
        elif rest[0] == "--tab" and len(rest) > 1 and rest[1]:
            tab = rest[1]
        else:
            raise ValueError(USAGE)
        rest = rest[2:]
    return "fill", args[1], only, tab


def main(argv):
    if argv[1:] == ["selftest"]:
        selftest()
        return 0
    try:
        cmd = parse_cli(argv[1:])
    except ValueError as e:
        print(e)
        return 2
    try:
        if cmd[0] == "list":
            rows = list_entries()
            print("\n".join("%s\t%s\t帳號 %s\t上次使用 %s" % (n, "、".join(s), u, used or "—") for n, s, u, used in rows)
                  or "（密碼庫是空的；請主人在 Telegram 用 /密碼 新增 存帳密）")
            return 0
        ok, msg = fill(cmd[1], cmd[2], cmd[3])
    except Exception as e:                              # the store, Chrome or Telegram broke; values never get here
        print("密碼庫出錯（%s: %s）" % (type(e).__name__, e))
        return 1
    print(msg)
    return 0 if ok else 1


# ---------------------------------------------------------------- selftest (temp folder, fakes: no Chrome, no Telegram)
class _FakeTG:
    def __init__(self, running=True):
        self.calls, self.running = [], running

    def reply_thread(self, cfg):
        return None

    def in_thread(self, thread, fn, *args, **params):
        return fn(*args, **params), None

    def tg(self, cfg, method, **params):
        self.calls.append((method, params))
        return {"ok": True, "result": {"message_id": 42}}

    def bridge_running(self):
        return self.running

    def on_button(self, cfg, cq):                       # only its presence counts (a bridge that knows the buttons)
        pass


class _Field:
    def __init__(self, label, pw=False):
        self.label, self.pw, self.value = label, pw, None


class _FakeUI:
    def __init__(self, hosts, edits):
        self.hosts, self.edits, self.finds = list(hosts), edits, 0

    def find(self, sites, tab_title=None):
        self.finds += 1
        return ("win", self.hosts[0]) if host_matches(self.hosts[0], sites) else (None, "")

    def fields(self, w):
        return self.edits

    def host(self, w):
        return self.hosts.pop(0) if len(self.hosts) > 1 else self.hosts[0]

    def is_password(self, e):
        return e.pw

    def label(self, e):
        return e.label

    def set_value(self, e, value):
        e.value = value


def selftest():
    import shutil, tempfile, threading
    global VAULT_DIR, VAULT_FILE, REQ_DIR, USAGE_LOG, APPROVE_SEC, POLL_SEC, _telegram
    saved = (VAULT_DIR, VAULT_FILE, REQ_DIR, USAGE_LOG, APPROVE_SEC, POLL_SEC, _telegram)
    d = tempfile.mkdtemp()
    VAULT_DIR = os.path.join(d, "vault")
    VAULT_FILE, REQ_DIR, USAGE_LOG = (os.path.join(VAULT_DIR, n) for n in ("vault.dat", "requests", "usage.log"))
    try:
        # sites and host matching
        assert site_of("https://www.GitHub.com/login?x=1") == "github.com" and site_of("claude.ai/new") == "claude.ai"
        assert site_of("http://192.168.1.1:8080/") == "192.168.1.1" and site_of("") == "" and site_of("http://[::1") == ""
        assert host_matches("github.com", ["github.com"]) and host_matches("login.github.com.", ["github.com"])
        for bad in ("evilgithub.com", "github.com.evil.com", "", "com"):
            assert not host_matches(bad, ["github.com"]), bad
        assert not host_matches("github.com", [""]) and mask("me@x.com") == "me***" and mask("abc") == "***"

        # store: DPAPI round trip in a temp folder, nothing readable on disk
        secret_user, secret_pw = "kai.secret@example.com", "pa ss 密碼 !@#"
        reply, drop = owner_command("/密碼 新增 github https://github.com/login %s %s" % (secret_user, secret_pw))
        assert drop and reply.startswith("✅") and "github.com" in reply and "ka***" in reply, reply
        raw = open(VAULT_FILE, "rb").read()
        assert secret_pw.encode() not in raw and secret_user.encode() not in raw and b"github" not in raw
        e = _load()["github"]
        assert e["user"] == secret_user and e["pass"] == secret_pw and e["sites"] == ["github.com"], e
        assert list_entries() == [("github", ["github.com"], "ka***", None)]

        # the owner's command: parsing, what gets deleted, replies never hold the password
        assert parse_owner_command("/vault@My_bot add a b.com u p w") == ("add", ["a", "b.com", "u", "p w"])
        assert parse_owner_command("／密碼新增 a b.com u p") == ("add", ["a", "b.com", "u", "p"])
        assert parse_owner_command("/密碼庫") == ("help", []) and parse_owner_command("/密碼 清單")[0] == "list"
        assert parse_owner_command("/密碼 github github.com u p")[0] == "other"
        cases = [("/密碼", False, "清單"), ("/密碼 清單", False, "github"), ("/密碼 說明", False, "新增"),
                 ("/密碼 新增 x", True, "格式"), ("/密碼 新增 a b c", True, "格式"),
                 ("/密碼 github github.com me %s" % secret_pw, True, "看不懂"),
                 ("/密碼 新增 壞 名 稱 x", True, "網址"), ("/密碼 新增 -x github.com u p", True, "名稱"),
                 ("/密碼 新增 a com u p", True, "網址"), ("/密碼 清單 %s" % secret_pw, True, "清單"),
                 ("/密碼 說明 %s" % secret_pw, True, "看不懂"),
                 ("/密碼 刪除", True, "格式"), ("/密碼 刪除 nope", False, "沒有"),
                 ("/密碼 新增 bank https://ebank.example.com.tw/ u2 x", True, "✅")]
        for text, want_drop, word in cases:
            reply, drop = owner_command(text)
            assert drop == want_drop and word in reply and secret_pw not in reply and "pa ss" not in reply, (text, reply)
        assert sorted(_load()) == ["bank", "github"] and _load()["bank"]["sites"] == ["ebank.example.com.tw"]
        assert owner_command("/密碼 刪除 bank") == ("🗑 已刪除「bank」", False) and sorted(_load()) == ["github"]

        # approvals: a tap while waiting, a timeout, late or odd taps
        assert decide("zz", "ok")[0] is False and decide("0123456789ab", "ok") == (False, "這個請求已經結束了")
        fake = _FakeTG()

        def tap(verdict):
            for _ in range(100):
                names = [n for n in os.listdir(REQ_DIR) if n.endswith(".json")] if os.path.isdir(REQ_DIR) else []
                if names:
                    assert decide(names[0][:-5], "maybe")[0] is False
                    assert decide(names[0][:-5], verdict) == (True, "已允許" if verdict == "ok" else "已拒絕")
                    assert decide(names[0][:-5], "no")[0] is False                     # the first tap counts
                    return
                time.sleep(0.05)
        POLL_SEC = 0.05
        for verdict in ("ok", "no"):
            th = threading.Thread(target=tap, args=(verdict,))
            th.start()
            assert ask_approval("github", "both", "github.com", (fake, {"token": "t", "owner_id": 1})) == verdict
            th.join()
        method, params = fake.calls[0]
        kb = json.loads(params["reply_markup"])["inline_keyboard"][0]
        assert method == "sendMessage" and params["chat_id"] == 1 and "github.com" in params["text"]
        assert [b["callback_data"][-3:] for b in kb] == [":ok", ":no"] and len(kb[0]["callback_data"]) == 21
        APPROVE_SEC = 0.3
        assert ask_approval("github", "pass", "github.com", (fake, {"token": "t", "owner_id": 1})) == "expired"
        assert fake.calls[-1][0] == "editMessageText" and "逾時" in fake.calls[-1][1]["text"]
        assert not [n for n in os.listdir(REQ_DIR) if n.endswith(".json")]           # nothing left behind
        req = "abcdef012345"
        _write_req(_req_path(req), {"status": "pending", "ts": time.time() - 3600})
        assert decide(req, "ok")[0] is False                                          # too late
        APPROVE_SEC = 120
        assert ask_approval("x", "both", "h", (fake, {"token": ""})).startswith("error: 這台沒有接 Telegram")
        assert "沒有在跑" in ask_approval("x", "both", "h", (_FakeTG(running=False), {"token": "t", "owner_id": 1}))

        # fill with a fake Chrome
        def run(hosts, edits, only="both", verdict="ok"):
            asked = []

            def approve(n, w, h):
                asked.append((n, w, h))
                return verdict
            ok, msg = fill("github", only, None, approve, _FakeUI(hosts, edits))
            assert secret_pw not in msg and secret_user not in msg and "pa ss" not in msg, msg
            return ok, msg, asked
        user, pw = _Field("email"), _Field("pwd", pw=True)
        ok, msg, asked = run(["login.github.com"], [_Field("search"), user, pw])
        assert ok and msg.startswith("已填入") and asked == [("github", "both", "login.github.com")], (msg, asked)
        assert user.value == secret_user and pw.value == secret_pw
        assert _load()["github"]["used"] and "filled" in open(USAGE_LOG, encoding="utf-8").read()
        assert secret_pw not in open(USAGE_LOG, encoding="utf-8").read()
        user, pw = _Field("q"), _Field("p", pw=True)                                 # no name hint: the box before it
        ok, msg, _ = run(["github.com", "evil.com"], [user, pw])                      # the tab moves after the first value
        assert not ok and "網址變了" in msg and user.value == secret_user and pw.value is None, msg
        user, pw = _Field("user"), _Field("p", pw=True)
        ok, msg, asked = run(["github.com"], [user, pw], only="pass")
        assert ok and asked[0][1] == "pass" and user.value is None and pw.value == secret_pw
        user, pw = _Field("user"), _Field("p", pw=True)
        for verdict, word in (("no", "拒絕"), ("expired", "沒有按"), ("error: 按鈕沒送出（X）", "按鈕沒送出")):
            ok, msg, _ = run(["github.com"], [user, pw], verdict=verdict)
            assert not ok and word in msg and user.value is None and pw.value is None, msg
        a, b = _Field("search"), _Field("q")                                         # no password box: nothing to fill
        ok, msg, asked = run(["github.com"], [a, b])
        assert not ok and "找不到" in msg and not asked and a.value is None and b.value is None
        ok, msg, asked = run(["github.com"], [_Field("user")], only="pass")
        assert not ok and "密碼欄位" in msg and not asked
        ok, msg, asked = run(["evilgithub.com"], [user, pw])
        assert not ok and "找不到網址" in msg and not asked
        assert fill("nope", approve=lambda *a: "ok", ui=_FakeUI(["x.com"], []))[1].startswith("密碼庫裡沒有")
        _telegram = lambda: (_FakeTG(), {"token": "", "owner_id": None})              # no Telegram: refused up front
        ui = _FakeUI(["github.com"], [user, pw])
        ok, msg = fill("github", ui=ui)
        assert not ok and "沒有接 Telegram" in msg and ui.finds == 0

        # command line
        assert parse_cli(["list"]) == ("list",)
        assert parse_cli(["fill", "github", "--only", "pass", "--tab", "GitHub"]) == ("fill", "github", "pass", "GitHub")
        for bad in ([], ["fill"], ["fill", "--only"], ["fill", "x", "--only", "both"], ["show", "x"], ["list", "x"],
                    ["fill", "x", "--tab"], ["fill", "x", "extra"]):
            try:
                parse_cli(bad)
                raise AssertionError("accepted %r" % bad)
            except ValueError:
                pass
    finally:
        shutil.rmtree(d, ignore_errors=True)
        VAULT_DIR, VAULT_FILE, REQ_DIR, USAGE_LOG, APPROVE_SEC, POLL_SEC, _telegram = saved
    print("vault selftest ok")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
