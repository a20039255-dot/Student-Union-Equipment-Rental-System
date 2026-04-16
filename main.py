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

@app.post("/borrow_batch")
def borrow(data: dict):
    global transaction_id_counter
    sid, sname, items = data.get("租借人員學號"), data.get("租借人員姓名"), data.get("設備清單")
    with db_lock:
        b_time = get_tw_time() # 🌟 使用台灣時間
        for item in items:
            eid, qty = item["id"], int(item["qty"])
            for _ in range(qty):
                sheets["log"].append_row([transaction_id_counter, item["name"], sid, sname, b_time, "待審核", "", ""])
                transaction_id_counter += 1
            # 扣庫存邏輯
            cell = sheets["equip"].find(eid, in_column=1)
            if cell:
                curr = int(sheets["equip"].cell(cell.row, 4).value)
                sheets["equip"].update_cell(cell.row, 4, curr - qty)
        sync_data()
        return {"成功": True}

# 🌟 新增：批量核准/駁回專用通道
@app.post("/admin/approve_batch")
def approve_batch(data: dict):
    tids = data.get("交易編號清單", [])
    action = data.get("動作")
    admin = data.get("點收幹部")
    
    if not tids:
        return {"成功": False, "訊息": "沒有提供要處理的編號"}
        
    status = "借用中" if action == "核准" else "已駁回"
    
    with db_lock:
        inventory_add = {}
        count = 0
        
        # 1. 批量更新 Log 狀態
        for tid in tids:
            cell = sheets["log"].find(str(tid), in_column=1)
            if cell:
                # 一次寫入狀態與點收幹部 (F與G欄)
                sheets["log"].update(f"F{cell.row}:G{cell.row}", [[status, admin]])
                count += 1
                
                # 如果是駁回，需要記錄要加回的庫存
                if action == "駁回":
                    ename = sheets["log"].cell(cell.row, 2).value
                    inventory_add[ename] = inventory_add.get(ename, 0) + 1
                    
                time.sleep(0.5) # 保護 Google API 不被鎖
                
        # 2. 如果是駁回，批量把庫存加回去
        if action == "駁回" and inventory_add:
            for ename, qty in inventory_add.items():
                c_eq = sheets["equip"].find(ename, in_column=2)
                if c_eq:
                    curr = int(sheets["equip"].cell(c_eq.row, 4).value)
                    sheets["equip"].update_cell(c_eq.row, 4, curr + qty)
                    time.sleep(0.5)
                    
        sync_data()
        return {"成功": True, "處理數量": count}

@app.post("/return")
def return_item(data: dict):
    tid, admin = int(data.get("交易編號")), data.get("點收幹部")
    r_time = get_tw_time() # 🌟 使用台灣時間
    
    with db_lock:
        cell = sheets["log"].find(str(tid), in_column=1)
        if cell:
            ename = sheets["log"].cell(cell.row, 2).value
            sheets["log"].update(f"F{cell.row}:H{cell.row}", [["已歸還", admin, r_time]])
            c_eq = sheets["equip"].find(ename, in_column=2)
            if c_eq:
                sheets["equip"].update_cell(c_eq.row, 4, int(sheets["equip"].cell(c_eq.row, 4).value) + 1)
        sync_data()
        return {"成功": True}

# 🌟 已優化為批次聚合處理，解決 Google API 限流問題
@app.post("/return_by_student")
def return_by_sid(data: dict):
    code = str(data.get("學號")).strip()
    admin = data.get("點收幹部")
    r_time = get_tw_time() # 🌟 使用台灣時間
    
    with db_lock:
        to_return_tids = []
        inventory_add = {}
        
        for tid, req in transactions.items():
            if req["狀態"] == "借用中" and str(req["租借人員學號"]).endswith(code):
                to_return_tids.append(tid)
                ename = req["設備名稱"]
                inventory_add[ename] = inventory_add.get(ename, 0) + 1
                
        if not to_return_tids:
            return {"成功": False, "訊息": "找不到符合的借用紀錄"}

        count = 0
        for tid in to_return_tids:
            cell = sheets["log"].find(str(tid), in_column=1)
            if cell:
                # 批次寫入 F, G, H 欄
                sheets["log"].update(f"F{cell.row}:H{cell.row}", [["已歸還", admin, r_time]])
                count += 1
                time.sleep(1) # 保護 API
        
        for ename, qty in inventory_add.items():
            cell_equip = sheets["equip"].find(ename, in_column=2)
            if cell_equip:
                curr_stock = int(sheets["equip"].cell(cell_equip.row, 4).value)
                sheets["equip"].update_cell(cell_equip.row, 4, curr_stock + qty)
                time.sleep(1)
        
        sync_data()
        return {"成功": True, "歸還數量": count}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))