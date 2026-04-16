import os
import json
import threading
import time
import requests  # 🌟 記得 requirements.txt 要加上 requests
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# 🌟 核心工具：時區與通知
# ---------------------------------------------------------

def get_tw_time():
    """取得台灣標準時間 (UTC+8)"""
    tw_tz = timezone(timedelta(hours=8))
    return datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M")

def send_discord_notify(message):
    """發送 Discord 通知 (取代即將停止服務的 LINE Notify)"""
    global system_settings
    webhook_url = system_settings.get("Discord網址")
    
    if not webhook_url or "discord.com" not in webhook_url:
        print("未設定 Discord Webhook 網址或網址格式錯誤")
        return
    
    payload = {"content": message}
    try:
        # 使用 POST 請求將訊息送往 Discord
        requests.post(webhook_url, json=payload, timeout=5)
    except Exception as e:
        print(f"Discord 傳送失敗: {e}")

# ---------------------------------------------------------
# 🌟 Google Sheets 初始化
# ---------------------------------------------------------

SCOPE = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/drive']

def init_sheets():
    try:
        env_key = os.getenv("GOOGLE_JSON_KEY")
        info = json.loads(env_key) if env_key else json.load(open('google-key.json'))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(info, SCOPE)
        client = gspread.authorize(creds)
        ss = client.open("設備管理資料庫")
        
        # 預設抓取的三張表
        sheets_dict = {
            "admin": ss.worksheet("admins"), 
            "equip": ss.worksheet("equipments"), 
            "log": ss.worksheet("log")
        }
        # 嘗試抓取 settings 表，若無則跳過
        try:
            sheets_dict["settings"] = ss.worksheet("settings")
        except:
            print("警告：找不到 settings 工作表")
        
        return sheets_dict
    except Exception as e: 
        print(f"Sheets 連線失敗: {e}")
        return None

sheets = init_sheets()
admins_db, equipments, transactions = {}, {}, {}
system_settings = {"借用天數限制": 14, "維護模式": "關閉", "系統公告": "", "Discord網址": ""}
transaction_id_counter = 1
db_lock = threading.Lock()

# ---------------------------------------------------------
# 🌟 數據同步邏輯 (精準同步，提升讀取速度)
# ---------------------------------------------------------

def sync_admin():
    if not sheets: return
    for r in sheets["admin"].get_all_records(): 
        admins_db[str(r["幹部代號"])] = r

def sync_equip():
    if not sheets: return
    for r in sheets["equip"].get_all_records(): 
        equipments[str(r["設備編號"])] = r

def sync_log():
    global transaction_id_counter
    if not sheets: return
    recs = sheets["log"].get_all_records()
    transactions.clear()
    max_id = 0
    for r in recs:
        try:
            tid = int(r.get("交易編號", 0))
            if tid > max_id: max_id = tid
            transactions[tid] = {
                "交易編號": tid,
                "設備名稱": str(r.get("設備名稱", "")),
                "租借人員學號": str(r.get("借用人學號", "")), 
                "租借人員姓名": str(r.get("借用人姓名", "")), 
                "借用時間": str(r.get("借用時間", "")),
                "狀態": str(r.get("狀態", "")),
                "處理人員": str(r.get("點收幹部", "")),
                "歸還時間": str(r.get("歸還時間", ""))
            }
        except: pass
    transaction_id_counter = max_id + 1

def sync_settings():
    global system_settings
    if not sheets or "settings" not in sheets: return
    try:
        new_settings = {}
        for r in sheets["settings"].get_all_records():
            key = str(r.get("設定項目", "")).strip()
            val = r.get("設定值", "")
            if key: new_settings[key] = val
        system_settings.update(new_settings)
    except Exception as e:
        print(f"設定檔同步失敗: {e}")

# 啟動時預載資料
if sheets:
    sync_admin()
    sync_equip()
    sync_log()
    sync_settings()

# ---------------------------------------------------------
# 🌟 API 路由區
# ---------------------------------------------------------

@app.get("/settings")
def get_settings():
    sync_settings()
    return system_settings

@app.post("/admin/login")
def admin_login(data: dict):
    sync_admin()
    code = str(data.get("代號")).strip()
    if code in admins_db: return {"成功": True, "姓名": admins_db[code]["幹部名稱"]}
    return {"成功": False, "訊息": "代號不存在"}

@app.get("/equipments")
def get_equips(): 
    sync_equip()
    return equipments

@app.get("/transactions")
def get_trans(): 
    sync_log()
    return transactions

@app.post("/borrow_batch")
def borrow(data: dict):
    global transaction_id_counter
    sid = data.get("租借人員學號")
    sname = data.get("租借人員姓名")
    items = data.get("設備清單")
    
    with db_lock:
        b_time = get_tw_time()
        new_rows = []
        equip_updates = []
        
        for item in items:
            eid, qty = item["id"], int(item["qty"])
            for _ in range(qty):
                new_rows.append([transaction_id_counter, item["name"], sid, sname, b_time, "待審核", "", ""])
                transaction_id_counter += 1
                
            cell = sheets["equip"].find(eid, in_column=1)
            if cell:
                curr = int(sheets["equip"].cell(cell.row, 4).value)
                equip_updates.append({'range': f'D{cell.row}', 'values': [[curr - qty]]})
                
        # 批量寫入 Google Sheets
        if new_rows: sheets["log"].append_rows(new_rows)
        if equip_updates: sheets["equip"].batch_update(equip_updates)
        
        # 🌟 Discord 自動通知
        try:
            item_summary = ", ".join([f"{i['name']} x{i['qty']}" for i in items])
            discord_msg = f"🔔 **【新設備借用申請】**\n👤 借用人：`{sname}`\n📦 品項：`{item_summary}`\n👉 請部長盡速至後台審核！"
            send_discord_notify(discord_msg)
        except: pass

        return {"成功": True}

@app.post("/admin/approve_batch")
def approve_batch(data: dict):
    tids = data.get("交易編號清單", [])
    action = data.get("動作")
    admin = data.get("點收幹部")
    
    if not tids: return {"成功": False, "訊息": "無資料"}
    status = "借用中" if action == "核准" else "已駁回"
    
    with db_lock:
        log_updates = []
        inventory_add = {}
        
        for tid in tids:
            cell = sheets["log"].find(str(tid), in_column=1)
            if cell:
                log_updates.append({'range': f'F{cell.row}:G{cell.row}', 'values': [[status, admin]]})
                if action == "駁回":
                    ename = sheets["log"].cell(cell.row, 2).value
                    inventory_add[ename] = inventory_add.get(ename, 0) + 1
                    
        if log_updates: sheets["log"].batch_update(log_updates)
            
        if action == "駁回" and inventory_add:
            equip_updates = []
            for ename, qty in inventory_add.items():
                c_eq = sheets["equip"].find(ename, in_column=2)
                if c_eq:
                    curr = int(sheets["equip"].cell(c_eq.row, 4).value)
                    equip_updates.append({'range': f'D{c_eq.row}', 'values': [[curr + qty]]})
            if equip_updates: sheets["equip"].batch_update(equip_updates)
                
        return {"成功": True, "處理數量": len(log_updates)}

@app.post("/return")
def return_item(data: dict):
    tid, admin = int(data.get("交易編號")), data.get("點收幹部")
    r_time = get_tw_time()
    
    with db_lock:
        cell = sheets["log"].find(str(tid), in_column=1)
        if cell:
            ename = sheets["log"].cell(cell.row, 2).value
            sheets["log"].update(f"F{cell.row}:H{cell.row}", [["已歸還", admin, r_time]])
            c_eq = sheets["equip"].find(ename, in_column=2)
            if c_eq:
                curr = int(sheets["equip"].cell(c_eq.row, 4).value)
                sheets["equip"].update_cell(c_eq.row, 4, curr + 1)
        return {"成功": True}

@app.post("/return_by_student")
def return_by_sid(data: dict):
    code = str(data.get("學號")).strip()
    admin = data.get("點收幹部")
    r_time = get_tw_time()
    
    with db_lock:
        to_return_tids = []
        inventory_add = {}
        for tid, req in transactions.items():
            if req["狀態"] == "借用中" and str(req["租借人員學號"]).endswith(code):
                to_return_tids.append(tid)
                ename = req["設備名稱"]
                inventory_add[ename] = inventory_add.get(ename, 0) + 1
                
        if not to_return_tids: return {"成功": False, "訊息": "找不到紀錄"}

        log_updates = []
        for tid in to_return_tids:
            cell = sheets["log"].find(str(tid), in_column=1)
            if cell:
                log_updates.append({'range': f'F{cell.row}:H{cell.row}', 'values': [['已歸還', admin, r_time]]})
        if log_updates: sheets["log"].batch_update(log_updates)
            
        equip_updates = []
        for ename, qty in inventory_add.items():
            cell_equip = sheets["equip"].find(ename, in_column=2)
            if cell_equip:
                curr_stock = int(sheets["equip"].cell(cell_equip.row, 4).value)
                equip_updates.append({'range': f'D{cell_equip.row}', 'values': [[curr_stock + qty]]})
        if equip_updates: sheets["equip"].batch_update(equip_updates)
            
        return {"成功": True, "歸還數量": len(log_updates)}

if __name__ == "__main__":
    import uvicorn
    # Cloud Run 會提供 PORT 環境變數，預設為 8080
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))