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

# --- 核心功能：同步與初始化 ---

def get_tw_time():
    # 取得台灣時間 (UTC+8)
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")

def init_sheets():
    try:
        env_key = os.getenv("GOOGLE_JSON_KEY")
        info = json.loads(env_key) if env_key else json.load(open('google-key.json'))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(info, SCOPE)
        client = gspread.authorize(creds)
        
        # 🌟 絕對綁定：使用您的試算表唯一 ID，徹底消滅抓錯檔案的問題
        ss = client.open_by_key("1r0vqm8FU3KWp_56fJTW-aDW-8JPK5poXQ9jk-IhZ9Sc")
        
        s_dict = {
            "admin": ss.worksheet("admins"),
            "equip": ss.worksheet("equipments"),
            "log": ss.worksheet("log")
        }
        try:
            s_dict["settings"] = ss.worksheet("settings")
        except:
            print("警告：找不到 settings 工作表")
        return s_dict  
    except Exception as e:
        print(f"Sheets 連線失敗: {e}")
        return None

def sync_admin():
    global admins_db
    if not sheets: return
    try:
        admins_db.clear() # 🌟 驅魔：清空舊記憶
        for r in sheets["admin"].get_all_records():
            code = str(r.get("幹部代號", "")).strip()
            if code: admins_db[code] = r
    except: pass

def sync_equip():
    global equipments
    if not sheets or "equip" not in sheets: 
        print("❌ 錯誤：找不到 equip 工作表物件")
        return
    try:
        equipments.clear()
        # 🌟 改用更穩定的方式讀取
        sheet_data = sheets["equip"].get_all_values()
        if len(sheet_data) < 2:
            print("⚠️ 警告：equipments 工作表看起來是空的（只有標題或完全沒資料）")
            return

        headers = sheet_data[0] # 第一列標題
        for row in sheet_data[1:]: # 從第二列開始讀
            # 將每一列轉成字典，模擬 get_all_records 的行為
            item = dict(zip(headers, row))
            eid = str(item.get("設備編號", "")).strip()
            if eid:
                equipments[eid] = item
        
        print(f"✅ 成功同步設備：共 {len(equipments)} 項")
    except Exception as e:
        print(f"❌ sync_equip 發生嚴重錯誤：{e}")

def sync_settings():
    global system_settings
    if not sheets or "settings" not in sheets: 
        print("錯誤：找不到 settings 工作表")
        return
    try:
        # 預設值防止崩潰
        new_set = {"借用天數限制": 14, "維護模式": "關閉", "系統公告": "", "Discord網址": "", "Discord逾期網址": ""}
        records = sheets["settings"].get_all_records()
        
        for r in records:
            # 使用 .strip() 去除可能的小寫或空白誤差
            key = str(r.get("設定項目", "")).strip()
            val = str(r.get("設定值", "")).strip()
            if key:
                new_set[key] = val
        
        system_settings = new_set
        print(f"✅ 設定同步成功：目前維護模式為 {system_settings.get('維護模式')}")
    except Exception as e:
        print(f"❌ 設定同步失敗：{e}")

def sync_log():
    global transactions, transaction_id_counter
    if not sheets: return
    try:
        transactions.clear() # 🌟 驅魔：清空舊記憶
        all_logs = sheets["log"].get_all_records()
        max_id = 1000
        for r in all_logs:
            tid = int(r.get("交易編號", 0))
            if tid:
                transactions[tid] = r
                if tid > max_id: max_id = tid
        transaction_id_counter = max_id + 1
    except: pass

# 初始啟動
def init_sheets():
    try:
        env_key = os.getenv("GOOGLE_JSON_KEY")
        info = json.loads(env_key) if env_key else json.load(open('google-key.json'))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(info, SCOPE)
        client = gspread.authorize(creds)
        
        # 綁定唯一 ID
        ss = client.open_by_key("1r0vqm8FU3KWp_56fjTW-aDW-8JPK5poXQ9jk-IhZ9Sc")
        
        # 🌟 這裡必須跟您的試算表標籤「一字不差」
        s_dict = {
            "admin": ss.worksheet("admins"),      # 對應 admins 標籤
            "equip": ss.worksheet("equipments"),  # 對應 equipments 標籤
            "log": ss.worksheet("log"),           # 對應 log 標籤
            "settings": ss.worksheet("settings")  # 對應 settings 標籤
        }
        return s_dict
    except Exception as e:
        print(f"Sheets 連線失敗: {e}")
        return None

# --- Discord 通知功能 ---
def send_discord_notify(msg, url):
    if not url or "http" not in url: return
    try:
        requests.post(url, json={"content": msg}, timeout=5)
    except: pass

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
    sync_equip()
    return equipments

@app.get("/transactions")
def get_transactions():
    sync_log()
    return transactions

@app.post("/borrow_batch")
def borrow(data: dict):
    global transaction_id_counter
    # 1. 強制同步確保資料最新
    sync_settings()
    sync_log()
    sync_equip()

    # 2. 檢查維護模式
    if system_settings.get("維護模式") == "開啟":
        return {"成功": False, "訊息": "系統維護中，目前暫停借用服務！"}

    sid = str(data.get("租借人員學號", "")).strip()
    sname = data.get("租借人員姓名", "未知人員")
    items = data.get("設備清單", [])

    if not sid or not items:
        return {"成功": False, "訊息": "申請資料不完整"}

    # 3. 防囤積機制：計算配額
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
            
            # 獲取最新試算表映射
            equip_data = sheets["equip"].get_all_records()
            stocks_col = sheets["equip"].col_values(4) # 假設 D 欄是剩餘數量
            
            # 建立設備名稱對應 ID 與上限的查表
            equip_meta = {}
            for idx, r in enumerate(equip_data):
                name = r.get("設備名稱")
                eid = str(r.get("設備編號"))
                limit = int(r.get("單次借用上限", r.get("借用上限", 1)))
                equip_meta[name] = {"id": eid, "limit": limit, "row": idx + 2}

            # 驗證所有品項
            for item in items:
                ename = item["name"]
                qty = int(item["qty"])
                
                if ename in equip_meta:
                    meta = equip_meta[ename]
                    current_own = user_borrowed_counts.get(ename, 0)
                    if current_own + qty > meta["limit"]:
                        return {"成功": False, "訊息": f"【{ename}】已達個人配額！(目前持有:{current_own}, 上限:{meta['limit']})"}
                else:
                    return {"成功": False, "訊息": f"系統找不到設備：{ename}"}

            # 驗證通過，執行寫入
            for item in items:
                ename = item["name"]
                qty = int(item["qty"])
                meta = equip_meta[ename]
                
                for _ in range(qty):
                    new_rows.append([transaction_id_counter, ename, sid, sname, b_time, "待審核", "", ""])
                    transaction_id_counter += 1
                
                # 更新庫存 (D 欄)
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
            return {"成功": True}
        except Exception as e:
            print(f"Borrow Error: {e}")
            return {"成功": False, "訊息": f"伺服器異常: {str(e)}"}

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
            restore_equips = {} # 駁回時回補庫存用

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
                    
                    # 回補庫存
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
            
            # 運用現成的單筆歸還邏輯
            count = 0
            for tid in to_return_tids:
                return_item({"交易編號": tid, "點收幹部": admin})
                count += 1
            return {"成功": True, "歸還數量": count}
        except:
            return {"成功": False}

# --- 逾期檢查 (Cron Job 專用) ---
@app.get("/cron/check_overdue")
def cron_check():
    sync_settings()
    sync_log()
    limit_days = int(system_settings.get("借用天數限制", 14))
    webhook = system_settings.get("Discord逾期網址")
    
    if not webhook: return {"status": "no webhook"}
    
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