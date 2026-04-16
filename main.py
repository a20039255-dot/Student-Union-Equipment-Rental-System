import os
import json
import threading
import time
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

# 🌟 台灣時區校正器 (UTC+8)
def get_tw_time():
    tw_tz = timezone(timedelta(hours=8))
    return datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M")

# Google Sheets 初始化
SCOPE = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/drive']

def init_sheets():
    try:
        env_key = os.getenv("GOOGLE_JSON_KEY")
        info = json.loads(env_key) if env_key else json.load(open('google-key.json'))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(info, SCOPE)
        client = gspread.authorize(creds)
        ss = client.open("設備管理資料庫")
        return {"admin": ss.worksheet("admins"), "equip": ss.worksheet("equipments"), "log": ss.worksheet("log")}
    except Exception as e: 
        print(f"連線失敗: {e}")
        return None

sheets = init_sheets()
admins_db, equipments, transactions = {}, {}, {}
transaction_id_counter = 1
db_lock = threading.Lock()

def sync_data():
    global transaction_id_counter, admins_db, equipments, transactions
    if not sheets: return
    
    for r in sheets["admin"].get_all_records(): admins_db[str(r["幹部代號"])] = r
    for r in sheets["equip"].get_all_records(): equipments[str(r["設備編號"])] = r
    
    recs = sheets["log"].get_all_records()
    transactions.clear()
    max_id = 0
    for r in recs:
        try:
            tid = int(r.get("交易編號", 0))
            if tid > max_id: max_id = tid
            # 對齊試算表與網頁的變數名稱
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

sync_data()

@app.post("/admin/login")
def admin_login(data: dict):
    code = str(data.get("代號")).strip()
    if code in admins_db: return {"成功": True, "姓名": admins_db[code]["幹部名稱"]}
    return {"成功": False, "訊息": "代號不存在"}

@app.get("/equipments")
def get_equips(): sync_data(); return equipments

@app.get("/transactions")
def get_trans(): sync_data(); return transactions

# -----------------------------------------
# 以下為「極速批量寫入版」API 區塊，請覆蓋原代碼
# -----------------------------------------

@app.post("/borrow_batch")
def borrow(data: dict):
    global transaction_id_counter
    sid, sname, items = data.get("租借人員學號"), data.get("租借人員姓名"), data.get("設備清單")
    
    with db_lock:
        b_time = get_tw_time()
        new_rows = []
        equip_updates = []
        
        for item in items:
            eid, qty = item["id"], int(item["qty"])
            # 1. 準備要一次新增的 Log 資料
            for _ in range(qty):
                new_rows.append([transaction_id_counter, item["name"], sid, sname, b_time, "待審核", "", ""])
                transaction_id_counter += 1
                
            # 2. 準備要一次更新的庫存資料
            cell = sheets["equip"].find(eid, in_column=1)
            if cell:
                curr = int(sheets["equip"].cell(cell.row, 4).value)
                equip_updates.append({
                    'range': f'D{cell.row}',
                    'values': [[curr - qty]]
                })
                
        # 🌟 大絕招：一次性寫入多行 Log (只需 1 次 API 請求，無須 Sleep)
        if new_rows:
            sheets["log"].append_rows(new_rows)
            
        # 🌟 大絕招：一次性更新所有庫存 (只需 1 次 API 請求，無須 Sleep)
        if equip_updates:
            sheets["equip"].batch_update(equip_updates)
            
        sync_data()
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
                log_updates.append({
                    'range': f'F{cell.row}:G{cell.row}',
                    'values': [[status, admin]]
                })
                if action == "駁回":
                    ename = sheets["log"].cell(cell.row, 2).value
                    inventory_add[ename] = inventory_add.get(ename, 0) + 1
                    
        # 🌟 一次性更新所有 Log 狀態
        if log_updates:
            sheets["log"].batch_update(log_updates)
            
        # 🌟 如果是駁回，一次性回填庫存
        if action == "駁回" and inventory_add:
            equip_updates = []
            for ename, qty in inventory_add.items():
                c_eq = sheets["equip"].find(ename, in_column=2)
                if c_eq:
                    curr = int(sheets["equip"].cell(c_eq.row, 4).value)
                    equip_updates.append({
                        'range': f'D{c_eq.row}',
                        'values': [[curr + qty]]
                    })
            if equip_updates:
                sheets["equip"].batch_update(equip_updates)
                
        sync_data()
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
        sync_data()
        return {"成功": True}

@app.post("/return_by_student")
def return_by_sid(data: dict):
    code = str(data.get("學號")).strip()
    admin = data.get("點收幹部")
    r_time = get_tw_time()
    
    with db_lock:
        to_return_tids = []
        inventory_add = {}
        
        # 1. 蒐集要歸還的 ID 與數量
        for tid, req in transactions.items():
            if req["狀態"] == "借用中" and str(req["租借人員學號"]).endswith(code):
                to_return_tids.append(tid)
                ename = req["設備名稱"]
                inventory_add[ename] = inventory_add.get(ename, 0) + 1
                
        if not to_return_tids:
            return {"成功": False, "訊息": "找不到符合的借用紀錄"}

        # 2. 準備打包 Log 更新資料
        log_updates = []
        for tid in to_return_tids:
            cell = sheets["log"].find(str(tid), in_column=1)
            if cell:
                log_updates.append({
                    'range': f'F{cell.row}:H{cell.row}',
                    'values': [['已歸還', admin, r_time]]
                })
                
        if log_updates:
            sheets["log"].batch_update(log_updates) # 🌟 瞬間一次寫入
            
        # 3. 準備打包庫存更新資料
        equip_updates = []
        for ename, qty in inventory_add.items():
            cell_equip = sheets["equip"].find(ename, in_column=2)
            if cell_equip:
                curr_stock = int(sheets["equip"].cell(cell_equip.row, 4).value)
                equip_updates.append({
                    'range': f'D{cell_equip.row}',
                    'values': [[curr_stock + qty]]
                })
                
        if equip_updates:
            sheets["equip"].batch_update(equip_updates) # 🌟 瞬間一次更新
            
        sync_data()
        return {"成功": True, "歸還數量": len(log_updates)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))