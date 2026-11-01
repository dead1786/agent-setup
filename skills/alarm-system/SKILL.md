---
name: alarm-system
description: 主人想要「會追蹤有沒有做完」的排程（要回報完成、沒做會再提醒、夜間不打擾）時才用；簡單提醒用 tgterm remind 就好
---

# 有追蹤的排程系統（alarm-system）

這份文件只教設計與做法。主人沒開口要，就不要主動建；主人只說「提醒我 X」，用 `tgterm.py remind` 就夠了。

## 1. 什麼時候用哪個

| 需求 | 用什麼 |
|---|---|
| 「每天 9 點提醒我吃藥」「30 分鐘後叫我」 | `tgterm.py remind add ...`，到點打字或發 Telegram，沒人回也無所謂 |
| 「每天 18:00 請你整理當天信件，做完要跟我說」 | 本文的 alarm 系統：要有人做事、做完要回報 |
| 「沒做完要再催，催三次還沒好就通知我」 | 本文：重送加升級 |
| 「半夜不要吵我，早上再一次告訴我」 | 本文：夜間靜音佇列 |

判斷口訣：只要「提醒」就用 remind；需要「確認完成」才建這套。先問主人要不要，再動手。

## 2. 設計總覽

```
alarm.py（每分鐘由排程跑一次）
  讀 schedule.json ──> 到點的任務
        │ 靜音時段？ ──是──> 暫存（state.json 的 _held），靜音結束後一次補送
        │ 否
        ▼
  送出「[排程] id: 內容」（走 tgterm remind，視窗關著會變 Telegram）
        │ 寫入 pending.json（待確認）
        ▼
  AI 收到後做事 ──> 執行 `alarm.py ack <id>` ──> 從 pending 移除
        │ 沒 ack，過 10 分鐘
        ▼
  重送（最多 3 次）──> 還是沒有 ──> Telegram 通知主人（只通知一次）
```

資料夾（全在 `C:\AI\.agent\alarm\`）：

| 檔案 | 用途 |
|---|---|
| `schedule.json` | 任務清單，主人與 AI 都可改 |
| `state.json` | 每個任務上次觸發時間、夜間暫存清單 |
| `pending.json` | 已送出、還沒 ack 的任務與重送次數 |
| `alarm.log` | 每次送出、重送、升級、ack 都記一行 |

### schedule.json 欄位

```json
{
  "quiet": ["23:00", "08:00"],
  "tasks": [
    {"id": "daily-mail", "text": "整理今天的信件，列出需要回覆的", "enabled": true,
     "schedule": {"type": "daily", "time": "18:00"}},
    {"id": "weekly-report", "text": "彙整本週進度寫成三行摘要",
     "schedule": {"type": "weekly", "days": [5], "time": "17:30"}, "grace_min": 240},
    {"id": "water", "text": "提醒主人喝水", "ack": false,
     "schedule": {"type": "interval", "minutes": 120, "between": ["09:00", "18:00"]}},
    {"id": "renew", "text": "確認網域有沒有續約", "urgent": true,
     "schedule": {"type": "once", "at": "2026-12-01T09:00"}}
  ]
}
```

- `id`：英文小寫加連字號，不重複，ack 時要逐字相同。
- `schedule.type`：`daily`（`time`）、`weekly`（`days` 為 1=週一 到 7=週日，加 `time`）、`interval`（`minutes`，可加 `between` 限定時段）、`once`（`at`，ISO 時間）。
- `enabled`：預設 true，false 即停用。
- `ack`：預設 true；false 表示純通知，不追蹤、不重送。
- `urgent`：true 才會在靜音時段照常送出。
- `grace_min`：每日／每週任務錯過多久內還補送，預設 180 分鐘（電腦睡著或關機後開機，過太久就跳過這次，不補一堆舊的）。
- `quiet`：頂層設定，可跨午夜。

### AI 怎麼處理一則到點的任務

1. 看到 `[排程] <id>: <內容>`（或 `[排程·重送N]`、`[排程·夜間累積]`），代表這是排程任務，不是主人臨時講的話。
2. 照內容做事。內容不明確或需要主人決定，就用 Telegram 問，不要亂猜。
3. 做完後執行 `C:\AI\.agent\py\Scripts\python.exe C:\AI\.agent\alarm\alarm.py ack <id>`，再簡短告訴主人結果。
4. 一則內含多個任務（夜間累積）就逐一處理、逐一 ack。
5. 做不完或失敗：不要 ack，直接跟主人說原因；系統會重送，重送到上限才會通知主人。

## 3. 建置步驟

先跟主人確認：要哪些任務、幾點、靜音時段（預設 23:00 到 08:00）、哪些算急件。

### 步驟 1：建資料夾並放程式

建立 `C:\AI\.agent\alarm\`，存成 `alarm.py`（UTF-8）。以下程式已實測過到點、夜間暫存、補送、重送、升級、ack：

```python
# C:\AI\.agent\alarm\alarm.py  -- 有追蹤的排程（只用標準函式庫）
# 用法: alarm.py tick | ack <id> | list | check
import json, os, subprocess, sys
from datetime import datetime, timedelta

BASE = os.environ.get("ALARM_HOME", r"C:\AI\.agent\alarm")
PY = os.environ.get("ALARM_PY", r"C:\AI\.agent\py\Scripts\python.exe")
TG = os.environ.get("ALARM_TG", r"C:\AI\.agent\tgterm.py")
DRY = os.environ.get("ALARM_DRY") == "1"          # 測試用：只印不送
RETRY_MIN, MAX_RETRY = 10, 3                       # 每 10 分鐘重送，最多 3 次，之後升級通知
NOWIN = 0x08000000                                 # CREATE_NO_WINDOW


def p(name): return os.path.join(BASE, name)


def load(name, default):
    try:
        with open(p(name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(name, obj):
    os.makedirs(BASE, exist_ok=True)
    tmp = p(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p(name))


def log(msg):
    os.makedirs(BASE, exist_ok=True)
    with open(p("alarm.log"), "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def run_tgterm(*args):
    if DRY:
        print("DRY>", *args); return ""
    p = subprocess.run([PY, TG, *args], timeout=60, creationflags=NOWIN,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (p.stdout or "").strip()


def deliver(text):
    # 直接打進 AI 視窗（tgterm.py inject，成功時輸出以 ok 開頭）；視窗沒開就改用 Telegram 通知
    if DRY:
        run_tgterm("inject", text); return
    if not run_tgterm("inject", text).startswith("ok"):
        tg("（AI 視窗目前沒開，排程先用 Telegram 通知）" + text)


def tg(text): run_tgterm("send", text)


def mins(hhmm):
    h, m = hhmm.split(":"); return int(h) * 60 + int(m)


def in_range(rng, now):                            # rng = ["23:00","08:00"]，可跨午夜
    if not rng: return False
    s, e, c = mins(rng[0]), mins(rng[1]), now.hour * 60 + now.minute
    return s <= c < e if s < e else (c >= s or c < e)


def is_due(t, last, now):
    s = t["schedule"]; ty = s["type"]
    if ty == "interval":
        if s.get("between") and not in_range(s["between"], now): return False
        return last is None or now - last >= timedelta(minutes=s["minutes"])
    if ty == "once":
        at = datetime.fromisoformat(s["at"])
        return last is None and at <= now < at + timedelta(hours=24)
    target = now.replace(hour=mins(s["time"]) // 60, minute=mins(s["time"]) % 60, second=0, microsecond=0)
    if ty == "weekly" and now.isoweekday() not in s["days"]: return False
    if now < target or now - target > timedelta(minutes=t.get("grace_min", 180)): return False
    return last is None or last < target           # daily / weekly 一天一次


def fire(t, pending, now, tag="[排程]"):
    text = f"{tag} {t['id']}: {t['text']}"
    deliver(text)
    if t.get("ack", True):
        pending[t["id"]] = {"sent": now.isoformat(), "tries": 0, "text": t["text"]}
    log(f"送出 {t['id']}")


def tick():
    now = datetime.now()
    cfg = load("schedule.json", {"tasks": []})
    state, pending = load("state.json", {}), load("pending.json", {})
    quiet = in_range(cfg.get("quiet"), now)
    held = state.setdefault("_held", [])
    tasks = {t["id"]: t for t in cfg["tasks"]}

    for tid, t in tasks.items():
        st = state.setdefault(tid, {})
        last = datetime.fromisoformat(st["last_fire"]) if st.get("last_fire") else None
        if not t.get("enabled", True) or not is_due(t, last, now): continue
        st["last_fire"] = now.isoformat()
        if tid in pending:
            log(f"略過 {tid}：上一輪還沒 ack"); continue
        if quiet and not t.get("urgent"):
            if tid not in held: held.append(tid)
            log(f"夜間暫存 {tid}"); continue
        fire(t, pending, now)

    if not quiet and held:                         # 靜音時段結束：一次補送
        for tid in held:
            if tid in tasks and tid not in pending: fire(tasks[tid], pending, now, "[排程·夜間累積]")
        held.clear()

    for tid, pe in list(pending.items()):          # 追蹤沒 ack 的
        t = tasks.get(tid)
        if not t or not t.get("enabled", True): del pending[tid]; continue
        if quiet and not t.get("urgent"): continue
        if now - datetime.fromisoformat(pe["sent"]) < timedelta(minutes=RETRY_MIN): continue
        if pe["tries"] < MAX_RETRY:
            pe["tries"] += 1; pe["sent"] = now.isoformat()
            deliver(f"[排程·重送{pe['tries']}] {tid}: {pe['text']}"); log(f"重送 {tid} #{pe['tries']}")
        elif not pe.get("escalated"):
            pe["escalated"] = True
            tg(f"排程「{tid}」重送 {MAX_RETRY} 次仍沒回報完成，請看一下。"); log(f"升級 {tid}")
    save("state.json", state); save("pending.json", pending)


def ack(tid):
    pending = load("pending.json", {})
    if pending.pop(tid, None) is None:
        print(f"沒有待確認的 {tid}（可能已 ack 或 id 打錯）"); return 1
    save("pending.json", pending); log(f"ack {tid}"); print(f"ack: {tid}"); return 0


def check():
    cfg = load("schedule.json", None)
    if cfg is None: print("schedule.json 讀不到或不是合法 JSON"); return 1
    bad = 0
    for t in cfg.get("tasks", []):
        s = t.get("schedule", {}); ty = s.get("type")
        ok = bool(t.get("id") and t.get("text")) and (
            (ty in ("daily", "weekly") and s.get("time") and (ty == "daily" or s.get("days")))
            or (ty == "interval" and s.get("minutes")) or (ty == "once" and s.get("at")))
        if not ok: bad += 1; print("欄位有問題:", t.get("id"))
    print("OK" if not bad else f"{bad} 個任務有問題"); return 1 if bad else 0


def list_():
    cfg, state, pending = load("schedule.json", {"tasks": []}), load("state.json", {}), load("pending.json", {})
    for t in cfg["tasks"]:
        print(f"{t['id']:<20} {'on ' if t.get('enabled', True) else 'off'} {json.dumps(t['schedule'], ensure_ascii=False)}"
              f"  上次:{state.get(t['id'], {}).get('last_fire', '-')[:16]}  {'[待ack]' if t['id'] in pending else ''}")


if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else ""
    if c == "tick": tick()
    elif c == "ack" and len(sys.argv) > 2: sys.exit(ack(sys.argv[2]))
    elif c == "list": list_()
    elif c == "check": sys.exit(check())
    else: print(__doc__ or "用法: alarm.py tick | ack <id> | list | check")
```

### 步驟 2：寫 schedule.json

依主人需求寫入 `C:\AI\.agent\alarm\schedule.json`（格式見上），然後驗證：

```
C:\AI\.agent\py\Scripts\python.exe C:\AI\.agent\alarm\alarm.py check
```

應顯示 `OK`。

### 步驟 3：讓它每分鐘跑

做法 A（建議，不改現有監看程式）：新增一個使用者層級的排程工作，不需要系統管理員權限：

```
schtasks /Create /TN "AIAlarm" /SC MINUTE /MO 1 /TR "C:\AI\.agent\py\Scripts\pythonw.exe C:\AI\.agent\alarm\alarm.py tick"
```

`pythonw` 不會跳出黑視窗。該工作在主人登入期間執行，與現有 watchdog 一樣。

做法 B：在現有 watchdog 每分鐘的流程尾端加一行，用 `subprocess.Popen([pythonw, alarm.py, "tick"], creationflags=0x08000000)` 呼叫。動 watchdog 前先備份，改壞會讓視窗與 Telegram 橋接都不會自動恢復，所以預設用 A。

### 步驟 4：確認送字方式

`alarm.py` 的 `deliver()` 用 `tgterm.py inject "文字"` 直接把排程打進 AI 視窗（不會多 `[提醒]` 前綴、沒有延遲）；inject 失敗（AI 視窗沒開）時改用 `tgterm.py send` 走 Telegram 通知主人。

### 步驟 5：把規則寫進 AI 自己的指示檔

在 AI 的常駐指示檔加上一段：「看到 `[排程] id: 內容` 就執行並 `alarm.py ack id`；沒做完不要 ack」。沒有這段，AI 會把它當一般聊天。

## 4. 安全測試

- 先用乾跑模式，不送任何東西：在臨時資料夾放一份測試用 `schedule.json`，設環境變數 `ALARM_HOME`（指到該資料夾）與 `ALARM_DRY=1`，再跑 `alarm.py tick`，程式只印出「會送什麼」。
- 正式測試：建一個 `interval` 1 分鐘、`text` 為「這是測試，回覆 ok 並 ack」的任務，看到送出、ack、`pending.json` 清空後立刻把它 `"enabled"` 改成 false。
- 測靜音：暫時把 `quiet` 設成包含現在的時段，確認普通任務被暫存、`urgent` 照送，再改回來看有沒有補送。
- 每次測完看 `alarm.log` 確認沒有重複送出。

## 5. 停用與移除

- 暫停單一任務：把它的 `enabled` 改 false（pending 裡的殘留會在下一輪自動清掉）。
- 全部暫停：`schtasks /Change /TN "AIAlarm" /DISABLE`；恢復用 `/ENABLE`。
- 完全移除：`schtasks /Delete /TN "AIAlarm" /F`，再刪除 `C:\AI\.agent\alarm\` 資料夾，並移除指示檔裡的那段規則。刪資料夾前先問主人。

## 6. 成本與安全守則

- 每個排程任務都會消耗一次 AI 對話額度。頻率越高越貴，interval 任務預設不要短於 30 分鐘，建完告訴主人預估一天會觸發幾次。
- 不要吵主人：靜音時段只有 `urgent` 才送；重送最多 3 次，升級通知只發一次；同一任務還沒 ack 時，下一輪到點會略過而不是疊加。
- 到點執行的內容只能是「查詢、整理、摘要、草稿」這類可逆動作。刪檔、對外發訊息、付款、重啟程式、改設定，一律不能無人看管地由計時器觸發；排程只能先通知主人，等主人明確同意才動手。
- 排程文字裡不要放密碼、金鑰、token。
- `pending.json` 或 `state.json` 壞掉（讀不到 JSON）時，程式會當作空的重來，可能造成重複送出；主人說「怎麼又送一次」時，先看 `alarm.log` 找原因，不要直接刪檔。
