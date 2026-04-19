import os
import json
import time
from datetime import datetime, timedelta
from threading import Lock
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests

# --- 基礎設定 ---
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
db_lock = Lock()

# --- 全局變數 ---
sheets = {}
admins_db = {}
equipments = {}
transactions = {}
system_settings = {
    "借用天數限制": 14,
    "維護模式": "關閉",
    "系統公告": "",
    "Discord網址": "",
    "Discord逾期網址": ""
}
transaction_id_counter = 1000

# --- 核心功能：時間與 Discord 通知 ---
def get_tw_time():
    # 取得台灣時間 (UTC+8)
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")

def send_discord_notify(msg, url):
    if not url or "http" not in url: return
    try:
        requests.post(url, json={"content": msg}, timeout=5)
    except: pass

# --- 核心功能：Google Sheets 連線與同步 ---
def init_sheets():
    try:
        # 1. 🌟 從環境變數讀取網址
        SHEET_URL = os.getenv("SHEET_URL")
        
        # 2. 🛡️ 防呆機制：如果忘記設定環境變數，直接讓它大聲尖叫（報錯）
        if not SHEET_URL:
            # 這裡我們拋出一個明顯的錯誤，方便您在 Logs 裡一眼看到
            raise ValueError("🚨 錯誤：找不到環境變數 'SHEET_URL'！請至 Cloud Run 控制台設定。")

        # 原有的金鑰讀取邏輯
        env_key = os.getenv("GOOGLE_JSON_KEY")
        info = json.loads(env_key) if env_key else json.load(open('google-key.json'))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(info, SCOPE)
        client = gspread.authorize(creds)
        
        # 3. 🚀 使用讀取到的網址連線
        ss = client.open_by_url(SHEET_URL)
        
        s_dict = {
            "admin": ss.worksheet("admins"),
            "equip": ss.worksheet("equipments"),
            "log": ss.worksheet("log"),
            "settings": ss.worksheet("settings")
        }
        return s_dict
    except Exception as e:
        # 如果連線失敗，直接印出原因，不要 pass
        print(f"❌ Sheets 連線失敗原因：{str(e)}")
        return None

def sync_admin():
    global admins_db
    if not sheets: return
    try:
        admins_db.clear()
        for r in sheets["admin"].get_all_records():
            code = str(r.get("幹部代號", "")).strip()
            if code: admins_db[code] = r
    except: pass

def sync_settings():
    global system_settings
    if not sheets or "settings" not in sheets: return
    try:
        new_set = {"借用天數限制": 14, "維護模式": "關閉", "系統公告": "", "Discord網址": "", "Discord逾期網址": ""}
        for r in sheets["settings"].get_all_records():
            key = str(r.get("設定項目", "")).strip()
            val = r.get("設定值", "")
            if key: new_set[key] = str(val).strip()
        system_settings = new_set
    except: pass

def sync_log():
    global transactions, transaction_id_counter
    if not sheets: return
    try:
        transactions.clear()
        all_logs = sheets["log"].get_all_records()
        max_id = 1000
        for r in all_logs:
            try:
                tid = int(r.get("交易編號", 0))
                if tid:
                    transactions[tid] = r
                    if tid > max_id: max_id = tid
            except: pass
        transaction_id_counter = max_id + 1
    except: pass

# 🌟 無敵強硬讀取版：無視空白行與格式錯誤
def sync_equip():
    global equipments
    if not sheets or "equip" not in sheets: return
    try:
        equipments.clear()
        raw_data = sheets["equip"].get_all_values()
        
        if len(raw_data) < 2: return # 只有標題或沒資料
        
        headers = [str(h).strip() for h in raw_data[0]]
        
        for row in raw_data[1:]:
            item = {}
            for i, header in enumerate(headers):
                if i < len(row):
                    item[header] = str(row[i]).strip()
                else:
                    item[header] = ""
            
            eid = str(row[0]).strip() if len(row) > 0 else ""
            # 過濾隱形空行
            if eid and item.get("設備名稱"):
                equipments[eid] = item
                
    except Exception as e:
        print(f"設備同步發生錯誤：{e}")

# --- 伺服器啟動初始化 ---
sheets = init_sheets()
if sheets:
    sync_admin()
    sync_equip()
    sync_log()
    sync_settings()


# --- API 路由區 ---

@app.get("/")
def home():
    return {"status": "online", "time": get_tw_time(), "maintenance": system_settings.get("維護模式")}

@app.get("/settings")
def get_settings():
    sync_settings()
    return system_settings

@app.post("/admin/login")
def admin_login(data: dict):
    sync_admin()
    code = str(data.get("代號", "")).strip()
    if code in admins_db:
        return {"成功": True, "姓名": admins_db[code].get("幹部名稱")}
    return {"成功": False}

@app.get("/equipments")
def get_equipments():
    sync_equip() # 確保冷啟動時必定能抓到最新設備
    return equipments

@app.get("/transactions")
def get_transactions():
    sync_log()
    return transactions

@app.post("/borrow_batch")
def borrow(data: dict):
    global transaction_id_counter
    # 強制確保資料最新
    sync_settings()
    sync_log()
    sync_equip()

    if system_settings.get("維護模式") == "開啟":
        return {"成功": False, "訊息": "系統維護中，目前暫停借用服務！"}

    sid = str(data.get("租借人員學號", "")).strip()
    sname = data.get("租借人員姓名", "未知人員")
    items = data.get("設備清單", [])

    if not sid or not items:
        return {"成功": False, "訊息": "申請資料不完整"}

    # 防囤積計算：計算該生未歸還的各設備數量
    user_borrowed_counts = {}
    for tid, req in transactions.items():
        req_sid = str(req.get("租借人員學號", "")).strip()
        if sid in req_sid and req.get("狀態") in ["待審核", "借用中"]:
            ename = req.get("設備名稱")
            user_borrowed_counts[ename] = user_borrowed_counts.get(ename, 0) + 1

    with db_lock:
        try:
            b_time = get_tw_time()
            new_rows = []
            equip_updates = []
            
            # 獲取庫存映射 (使用強硬讀取法對齊)
            equip_data = sheets["equip"].get_all_values()
            headers = [str(h).strip() for h in equip_data[0]]
            
            equip_meta = {}
            for idx, row in enumerate(equip_data[1:]):
                if len(row) > 1:
                    name = str(row[1]).strip() # 設備名稱在 B 欄
                    # 單次借用上限在 E 欄
                    limit_val = row[4] if len(row) > 4 else "1"
                    limit = int(limit_val) if limit_val.isdigit() else 1
                    equip_meta[name] = {"limit": limit, "row": idx + 2}

            stocks_col = sheets["equip"].col_values(4) # D 欄剩餘數量

            # 1. 驗證配額
            for item in items:
                ename = item["name"]
                qty = int(item["qty"])
                
                if ename in equip_meta:
                    limit = equip_meta[ename]["limit"]
                    current_own = user_borrowed_counts.get(ename, 0)
                    if current_own + qty > limit:
                        return {"成功": False, "訊息": f"【{ename}】已達配額限制！(目前持有:{current_own}, 上限:{limit})"}
                else:
                    return {"成功": False, "訊息": f"系統找不到設備：{ename}"}

            # 2. 驗證通過，寫入資料
            for item in items:
                ename = item["name"]
                qty = int(item["qty"])
                meta = equip_meta[ename]
                
                for _ in range(qty):
                    new_rows.append([transaction_id_counter, ename, sid, sname, b_time, "待審核", "", ""])
                    transaction_id_counter += 1
                
                # 準備更新庫存
                row_idx = meta["row"]
                try:
                    old_stock = int(stocks_col[row_idx - 1]) if row_idx <= len(stocks_col) else 0
                    equip_updates.append({'range': f'D{row_idx}', 'values': [[old_stock - qty]]})
                except: pass

            if new_rows: sheets["log"].append_rows(new_rows)
            if equip_updates: sheets["equip"].batch_update(equip_updates)

            # Discord 推播
            summary = ", ".join([f"{i['name']} x{i['qty']}" for i in items])
            send_discord_notify(f"🆕 **新借用申請**\n👤 申請人：`{sname}`\n📦 品項：`{summary}`", system_settings.get("Discord網址"))
            
            sync_log()
            sync_equip()
            return {"成功": True}
        except Exception as e:
            print(f"Borrow Error: {e}")
            return {"成功": False, "訊息": f"伺服器寫入失敗: {str(e)}"}

@app.post("/admin/approve_batch")
def approve_batch(data: dict):
    tids = data.get("交易編號清單", [])
    action = data.get("動作")
    admin = data.get("點收幹部")
    if not tids or not action: return {"成功": False}

    with db_lock:
        try:
            sync_log()
            log_data = sheets["log"].get_all_records()
            updates = []
            restore_equips = {} 

            for idx, r in enumerate(log_data):
                tid = int(r.get("交易編號", 0))
                if tid in tids and r.get("狀態") == "待審核":
                    row_num = idx + 2
                    updates.append({'range': f'F{row_num}', 'values': [[action]]})
                    updates.append({'range': f'G{row_num}', 'values': [[admin]]})
                    if action == "核准":
                        updates.append({'range': f'E{row_num}', 'values': [[get_tw_time()]]})
                    elif action == "駁回":
                        ename = r.get("設備名稱")
                        restore_equips[ename] = restore_equips.get(ename, 0) + 1

            if updates: sheets["log"].batch_update(updates)
            
            # 駁回補回庫存
            if restore_equips:
                equip_data = sheets["equip"].get_all_records()
                e_updates = []
                for idx, r in enumerate(equip_data):
                    ename = r.get("設備名稱")
                    if ename in restore_equips:
                        curr = int(r.get("剩餘數量", 0))
                        e_updates.append({'range': f'D{idx+2}', 'values': [[curr + restore_equips[ename]]]})
                if e_updates: sheets["equip"].batch_update(e_updates)

            sync_log()
            sync_equip()
            return {"成功": True, "處理數量": len(updates)//2}
        except Exception as e:
            return {"成功": False, "訊息": str(e)}

@app.post("/return")
def return_item(data: dict):
    tid = data.get("交易編號")
    admin = data.get("點收幹部")
    with db_lock:
        try:
            sync_log()
            log_data = sheets["log"].get_all_records()
            for idx, r in enumerate(log_data):
                if int(r.get("交易編號", 0)) == int(tid):
                    row = idx + 2
                    ename = r.get("設備名稱")
                    sheets["log"].update(f"F{row}:H{row}", [["已歸還", admin, get_tw_time()]])
                    
                    equip_data = sheets["equip"].get_all_records()
                    for e_idx, e_r in enumerate(equip_data):
                        if e_r.get("設備名稱") == ename:
                            curr = int(e_r.get("剩餘數量", 0))
                            sheets["equip"].update(f"D{e_idx+2}", [[curr + 1]])
                            break
                    break
            sync_log()
            sync_equip()
            return {"成功": True}
        except Exception as e:
            return {"成功": False, "訊息": str(e)}

@app.post("/return_by_student")
def return_by_student(data: dict):
    sid_suffix = str(data.get("學號", "")).strip()
    admin = data.get("點收幹部")
    with db_lock:
        try:
            sync_log()
            log_data = sheets["log"].get_all_records()
            to_return_tids = []
            for r in log_data:
                full_sid = str(r.get("租借人員學號", ""))
                if full_sid.endswith(sid_suffix) and r.get("狀態") == "借用中":
                    to_return_tids.append(int(r.get("交易編號")))
            
            if not to_return_tids: return {"成功": False}
            
            count = 0
            for tid in to_return_tids:
                return_item({"交易編號": tid, "點收幹部": admin})
                count += 1
            return {"成功": True, "歸還數量": count}
        except:
            return {"成功": False}

# --- 逾期自動檢查 ---
@app.get("/cron/check_overdue")
def cron_check():
    sync_settings()
    sync_log()
    limit_days = int(system_settings.get("借用天數限制", 14))
    webhook = system_settings.get("Discord逾期網址")
    
    if not webhook or "http" not in webhook: return {"status": "no webhook"}
    
    today = datetime.now() + timedelta(hours=8)
    overdue_list = []
    
    for tid, t in transactions.items():
        if t.get("狀態") == "借用中" and t.get("借用時間"):
            try:
                b_date = datetime.strptime(t["借用時間"], "%Y-%m-%d %H:%M")
                if (today - b_date).days > limit_days:
                    overdue_list.append(f"⚠️ ID #{tid}: {t['租借人員姓名']} - {t['設備名稱']}")
            except: pass
            
    if overdue_list:
        msg = "🚨 **【逾期未歸還名單】**\n" + "\n".join(overdue_list)
        send_discord_notify(msg, webhook)
    
    return {"status": "done", "found": len(overdue_list)}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)