---
name: watchdog-alert
description: 主人想在「背景程式掛掉時收到 Telegram 通知」或要監控某支程式是否還活著時才用
---

# 程式掛掉通知（watchdog-alert）

這份文件只教設計與做法。主人沒開口要，就不要主動替他的程式加監控；也不要監控不是主人的程式。

## 1. 概念

「程式自己說我還活著」比「從外面猜它活不活」可靠：

1. 被監控的程式每隔一小段時間更新一個**心跳檔**（寫入時間與自己的 PID）。
2. 一個**檢查器**每分鐘看一次：心跳檔多久沒更新？超過門檻就算異常。
3. 異常要**連續兩輪**才處理，避免電腦卡一下就誤報。
4. 處理順序：嘗試重啟（有冷卻與次數上限）、發 Telegram 通知（有去重與冷卻）、恢復後發一則「已恢復」。
5. 靜音時段（例如 23:00 到 08:00）不發通知，早上再補發恢復通知；重啟照做。

重點設計：

| 項目 | 做法 | 原因 |
|---|---|---|
| 門檻 | 心跳間隔的 3 倍以上，例如每 30 秒寫一次、門檻 180 秒 | 偶爾延遲不誤報 |
| 連續判定 | `need_streak` 預設 2 | 擋掉單次卡頓 |
| 重啟冷卻 | 預設 10 分鐘、最多 3 次 | 程式一啟動就崩潰時不會無限重開 |
| 通知去重 | 第一次掛掉立刻通知，之後每 60 分鐘最多一次 | 不洗版 |
| 活著但卡住 | PID 還在、心跳停了：只通知、不重啟 | 硬重啟可能重複開兩份或丟資料 |
| 恢復通知 | 心跳回來後發一次 | 主人才知道不用處理了 |

## 2. 讓被監控程式寫心跳

在該程式的主迴圈每隔一段時間呼叫一次（Python 範例）：

```python
import os, time

HB = r"C:\AI\.agent\heartbeat\myjob.txt"   # 一個程式一個檔

def beat():
    os.makedirs(os.path.dirname(HB), exist_ok=True)
    tmp = HB + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(f"{os.getpid()}\n{time.time()}")
    os.replace(tmp, HB)                    # 原子替換，檢查器不會讀到寫一半的檔
```

注意：

- 要放在「真的有在做事」的主迴圈裡，不要只放在啟動時，也不要放在另一個獨立執行緒（主工作卡死但心跳還在跳，監控就失去意義）。
- 長時間的單次工作（例如 10 分鐘的批次）要在工作中途也呼叫 `beat()`，或把門檻調大。
- 不是 Python 的程式：批次檔或 PowerShell 在迴圈裡寫入同樣兩行即可；第一行是 PID，沒有 PID 的程式第一行寫非數字也行，只是少了「卡住」判斷。
- 檢查器看的是檔案**修改時間**，內容只用來取 PID。

## 3. 檢查器

建立 `C:\AI\.agent\monitor\monitor.py`（UTF-8）。以下程式已實測過：沒心跳檔、心跳過期、PID 已死自動重啟、重啟上限、活著但卡住、恢復通知：

```python
# C:\AI\.agent\monitor\monitor.py  -- 心跳檢查器（只用標準函式庫），每分鐘跑一次
import json, os, subprocess, sys, time
from datetime import datetime

BASE = os.environ.get("MON_HOME", r"C:\AI\.agent\monitor")
PY = os.environ.get("MON_PY", r"C:\AI\.agent\py\Scripts\python.exe")
TG = os.environ.get("MON_TG", r"C:\AI\.agent\tgterm.py")
DRY = os.environ.get("MON_DRY") == "1"             # 測試用：只印不送、不重啟
NOWIN, DETACHED = 0x08000000, 0x00000008


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def log(msg):
    os.makedirs(BASE, exist_ok=True)
    with open(os.path.join(BASE, "monitor.log"), "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def tg(text):
    log("通知: " + text)
    if DRY: print("DRY tg>", text); return
    subprocess.run([PY, TG, "send", text], timeout=30, creationflags=NOWIN)


def in_quiet(rng, now):
    if not rng: return False
    f = lambda s: int(s[:2]) * 60 + int(s[3:])
    s, e, c = f(rng[0]), f(rng[1]), now.hour * 60 + now.minute
    return s <= c < e if s < e else (c >= s or c < e)


def pid_alive(pid):                                # tasklist 輸出是系統碼頁，用 bytes 比對避免解碼錯誤
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                         capture_output=True, creationflags=NOWIN).stdout
    return f',"{pid}",'.encode() in out


def read_hb(path):                                 # 回傳 (年齡秒或 None, pid 或 None)
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            return age, int(f.readline().strip())
    except Exception:
        return age, None


def restart(cmd):
    if DRY: print("DRY restart>", cmd); return True
    try:
        subprocess.Popen(cmd, creationflags=NOWIN | DETACHED, close_fds=True); return True
    except Exception as e:
        log(f"重啟失敗: {e}"); return False


def check_one(m, st, now):
    name, ts = m["name"], time.time()
    age, pid = read_hb(m["file"])
    quiet = in_quiet(m.get("quiet"), datetime.now())

    if age is not None and age <= m.get("stale_sec", 180):      # 健康
        if st.get("down"):
            st.update(down=False, restarts=0, streak=0, notify_recovery=True)
            log(f"{name} 恢復")
        st["streak"] = 0
        if st.get("notify_recovery") and not quiet:
            tg(f"{name} 已恢復正常。"); st["notify_recovery"] = False
        return

    st["streak"] = st.get("streak", 0) + 1                      # 連續 N 輪異常才算掛
    if st["streak"] < m.get("need_streak", 2): return
    first = not st.get("down")
    st["down"] = True
    why = "沒有心跳檔" if age is None else f"已 {int(age // 60)} 分鐘沒心跳"
    hung = pid is not None and pid_alive(pid)                   # 活著但沒心跳 = 可能卡住
    note = ""
    if m.get("restart_cmd") and not hung:
        cool = m.get("restart_cooldown_min", 10) * 60
        if st.get("restarts", 0) < m.get("max_restarts", 3) and ts - st.get("last_restart", 0) >= cool:
            ok = restart(m["restart_cmd"])
            st["last_restart"] = ts; st["restarts"] = st.get("restarts", 0) + 1
            note = "已嘗試自動重啟。" if ok else "自動重啟失敗。"
        elif st.get("restarts", 0) >= m.get("max_restarts", 3):
            note = "自動重啟已達上限，需要人工處理。"
    elif hung:
        note = "程式還在但沒心跳，可能卡住（不自動重啟）。"

    if not quiet and (first or ts - st.get("last_alert", 0) >= m.get("alert_cooldown_min", 60) * 60):
        st["last_alert"] = ts
        tg(f"{name} 異常：{why}。{note}")


def main():
    cfg = load(os.path.join(BASE, "monitors.json"), {"monitors": []})
    state = load(os.path.join(BASE, "state.json"), {})
    now = datetime.now()
    for m in cfg["monitors"]:
        if m.get("enabled", True):
            try:
                check_one(m, state.setdefault(m["name"], {}), now)
            except Exception as e:
                log(f"{m.get('name')} 檢查出錯: {e}")
    save(os.path.join(BASE, "state.json"), state)


if __name__ == "__main__":
    main()
```

設定檔 `C:\AI\.agent\monitor\monitors.json`：

```json
{
  "monitors": [
    {
      "name": "每日整理程式",
      "file": "C:\\AI\\.agent\\heartbeat\\myjob.txt",
      "stale_sec": 180,
      "need_streak": 2,
      "restart_cmd": ["C:\\AI\\.agent\\py\\Scripts\\pythonw.exe", "C:\\AI\\myjob\\main.py"],
      "restart_cooldown_min": 10,
      "max_restarts": 3,
      "alert_cooldown_min": 60,
      "quiet": ["23:00", "08:00"]
    }
  ]
}
```

欄位說明：

- `name`：會出現在通知文字裡，用主人看得懂的名字。
- `restart_cmd`：陣列形式的啟動指令；不想自動重啟就整個拿掉，只通知。
- `quiet`：不想有靜音就拿掉。
- `enabled`: false 可暫停單一監控。
- 重啟指令必須自己防重複（例如啟動時先檢查是否已有同一支在跑），因為檢查器只靠心跳與 PID 判斷。

### 通知內容

訊息由 `tgterm.py send` 發出，格式固定為「名稱 異常：原因。處理狀況」。保持短而白話，例如「每日整理程式 異常：已 5 分鐘沒心跳。已嘗試自動重啟。」不要貼錯誤堆疊、完整路徑或任何金鑰。

## 4. 掛上每分鐘排程

做法 A（建議）：獨立排程工作，不需要系統管理員權限：

```
schtasks /Create /TN "AIMonitor" /SC MINUTE /MO 1 /TR "C:\AI\.agent\py\Scripts\pythonw.exe C:\AI\.agent\monitor\monitor.py"
```

用 `pythonw` 才不會每分鐘閃一次黑視窗。

做法 B：在現有的每分鐘 watchdog 尾端加一行 `subprocess.Popen([pythonw, monitor.py], creationflags=0x08000000)`。改之前先備份原檔，改壞會連視窗與 Telegram 橋接的自動恢復一起失效。

另外建議監控 watchdog 本身：現有 watchdog 若也寫心跳，可再加一筆 `monitors.json` 項目盯它（只通知，不設 `restart_cmd`）。

## 5. 測試（不要拿主人正在用的程式開刀）

1. 先乾跑：設環境變數 `MON_HOME` 指到臨時資料夾、`MON_DRY=1`，通知與重啟都只印出來不執行。
2. 在臨時資料夾放一個 `monitors.json`，`stale_sec` 設 2、`restart_cmd` 設成無害的指令，手動寫心跳檔、等幾秒、連跑檢查器兩次，確認依序出現：異常通知、重啟、再次寫心跳後「已恢復」。
3. 乾跑無誤後，關掉 `MON_DRY`，用一支專門測試用的小程式（只迴圈寫心跳）做真測試：手動結束它，等 2 到 3 分鐘看是否收到一則 Telegram，再啟動它看是否收到恢復通知。
4. 查 `monitor.log`，每個動作都有紀錄；不要重複測太多次，避免對主人發太多通知。

## 6. 停用與移除

- 暫停單一監控：該項目設 `"enabled": false`。
- 全部暫停：`schtasks /Change /TN "AIMonitor" /DISABLE`。
- 完全移除：`schtasks /Delete /TN "AIMonitor" /F`，刪除 `C:\AI\.agent\monitor\` 與 `C:\AI\.agent\heartbeat\`（刪前先問主人），並把被監控程式裡的 `beat()` 拿掉。

## 7. 守則

- 通知要少而準：同一個問題不重複轟炸；沒事不要發「一切正常」。
- 自動重啟只限主人明確授權的那支程式；不要擴大成「順便把相關程式也重開」。
- 整台電腦關機或網路斷線時，這套抓不到，因為檢查器與通知都跑在同一台電腦上。要告訴主人這個限制。
- 通知文字與紀錄不要包含密碼、token、個人資料。
