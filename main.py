import os
import json
import threading
import time
from datetime import datetime
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

# Google Sheets 初始化 (請確保雲端有 admins, equipments, log 三個分頁)
SCOPE = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/drive']

def init_sheets():
    try:
        env_key = os.getenv("GOOGLE_JSON_KEY")
        info = json.loads(env_key) if env_key else json.load(open('google-key.json'))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(info, SCOPE)
        client = gspread.authorize(creds)
        ss = client.open("設備管理資料庫")
        return {"admin": ss.worksheet("admins"), "equip": ss.worksheet("equipments"), "log": ss.worksheet("log")}
    except: return None

sheets = init_sheets()
admins_db, equipments, transactions = {}, {}, {}
transaction_id_counter = 1
db_lock = threading.Lock()

def sync_data():
    global transaction_id_counter, admins_db, equipments, transactions
    if not sheets: return
    
    # 1. 同步幹部
    for r in sheets["admin"].get_all_records(): 
        admins_db[str(r["幹部代號"])] = r
        
    # 2. 同步設備
    for r in sheets["equip"].get_all_records(): 
        equipments[str(r["設備編號"])] = r
        
    # 3. 同步 Log (🌟 關鍵修正區：對齊欄位名稱)
    recs = sheets["log"].get_all_records()
    transactions.clear()
    max_id = 0
    for r in recs:
        try:
            tid = int(r.get("交易編號", 0))
            if tid > max_id: max_id = tid
            
            # 手動將 Google Sheets 的欄位名稱，轉譯成前端看得懂的名稱
            transactions[tid] = {
                "交易編號": tid,
                "設備名稱": str(r.get("設備名稱", "")),
                "租借人員學號": str(r.get("借用人學號", "")), # 對應試算表 C 欄
                "租借人員姓名": str(r.get("借用人姓名", "")), # 對應試算表 D 欄
                "借用時間": str(r.get("借用時間", "")),
                "狀態": str(r.get("狀態", "")),
                "處理人員": str(r.get("點收幹部", "")),
                "歸還時間": str(r.get("歸還時間", ""))
            }
        except Exception as e:
            print(f"解析資料列時發生錯誤: {e}")
            
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
        b_time = datetime.now().strftime("%Y-%m-%d %H:%M")
        for item in items:
            eid, qty = item["id"], int(item["qty"])
            for _ in range(qty):
                sheets["log"].append_row([transaction_id_counter, item["name"], sid, sname, b_time, "待審核", "", ""])
                transaction_id_counter += 1
            # 扣庫存邏輯 (略，同前)
        sync_data()
        return {"成功": True}

@app.post("/admin/approve")
def approve(data: dict):
    tid, action, admin = int(data.get("交易編號")), data.get("動作"), data.get("點收幹部")
    with db_lock:
        cell = sheets["log"].find(str(tid), in_column=1)
        if cell:
            status = "借用中" if action == "核准" else "已駁回"
            sheets["log"].update_cell(cell.row, 6, status)
            sheets["log"].update_cell(cell.row, 7, admin)
            # 駁回需回填庫存 (略)
        sync_data()
        return {"成功": True}

@app.post("/return")
def return_item(data: dict):
    tid, admin = int(data.get("交易編號")), data.get("點收幹部")
    r_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    with db_lock:
        cell = sheets["log"].find(str(tid), in_column=1)
        if cell:
            sheets["log"].update_cell(cell.row, 6, "已歸還")
            sheets["log"].update_cell(cell.row, 7, admin)
            sheets["log"].update_cell(cell.row, 8, r_time)
            # 回填庫存邏輯 (略)
        sync_data()
        return {"成功": True}

@app.post("/return_by_student")
def return_by_sid(data: dict):
    code = str(data.get("學號")).strip()
    admin = data.get("點收幹部")
    r_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    with db_lock:
        # 1. 蒐集要歸還的 ID，並「聚合統計」要加回的設備數量
        to_return_tids = []
        inventory_add = {}
        
        for tid, req in transactions.items():
            if req["狀態"] == "借用中" and str(req["租借人員學號"]).endswith(code):
                to_return_tids.append(tid)
                ename = req["設備名稱"]
                # 計算每種設備總共要加回多少 (例如：CALL機要加 5 台)
                inventory_add[ename] = inventory_add.get(ename, 0) + 1
                
        if not to_return_tids:
            return {"成功": False, "訊息": "找不到符合的借用紀錄"}

        # 2. 批次更新 Log 紀錄
        count = 0
        for tid in to_return_tids:
            cell = sheets["log"].find(str(tid), in_column=1)
            if cell:
                # 🌟 優化 1：將三次寫入合併為一次範圍寫入 (更新 F, G, H 欄)
                sheets["log"].update(f"F{cell.row}:H{cell.row}", [["已歸還", admin, r_time]])
                count += 1
                time.sleep(1) # 增加 1 秒緩衝，保護 API 不被 Google 鎖定
        
        # 3. 批次更新設備庫存
        for ename, qty in inventory_add.items():
            cell_equip = sheets["equip"].find(ename, in_column=2)
            if cell_equip:
                curr_stock = int(sheets["equip"].cell(cell_equip.row, 4).value)
                # 🌟 優化 2：一次把總歸還數量加回去
                sheets["equip"].update_cell(cell_equip.row, 4, curr_stock + qty)
                time.sleep(1)
        
        sync_data()
        return {"成功": True, "歸還數量": count}
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))