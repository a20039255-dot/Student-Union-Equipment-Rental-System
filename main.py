import os
import json
import threading
import time
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 1. 系統初始化
app = FastAPI(title="設備管理系統後端 - 歸還紀錄強化版")

# 解決跨域問題
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Google Sheets 連線設定
SCOPE = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/drive']

def init_google_sheets():
    try:
        env_key = os.getenv("GOOGLE_JSON_KEY")
        if env_key:
            info = json.loads(env_key)
            creds = ServiceAccountCredentials.from_json_keyfile_dict(info, SCOPE)
        else:
            creds = ServiceAccountCredentials.from_json_keyfile_name('google-key.json', SCOPE)
        
        client = gspread.authorize(creds)
        spreadsheet = client.open("設備管理資料庫")
        
        return {
            "admin": spreadsheet.worksheet("admins"),
            "equip": spreadsheet.worksheet("equipments"),
            "log": spreadsheet.worksheet("log")
        }
    except Exception as e:
        print(f"❌ [Sheets 連線失敗]: {e}")
        return None

sheets = init_google_sheets()

# 3. 全域變數
admins_db = {}
equipments = {}
transactions = {}
transaction_id_counter = 1
db_lock = threading.Lock()

def sync_all_from_cloud():
    """ 從雲端同步資料庫到記憶體 """
    global admins_db, equipments, transactions, transaction_id_counter
    if not sheets: return
    try:
        # 同步幹部
        admins_db.clear()
        for r in sheets["admin"].get_all_records():
            admins_db[str(r["幹部代號"])] = {"名稱": str(r["幹部名稱"]), "職位": str(r.get("職位", ""))}
            
        # 同步設備
        equipments.clear()
        for r in sheets["equip"].get_all_records():
            equipments[str(r["設備編號"])] = {
                "設備名稱": str(r["設備名稱"]),
                "總數量": int(r["總數量"]),
                "剩餘數量": int(r["剩餘數量"]),
                "借用上限": int(r.get("單次借用上限", 1))
            }
            
        # 同步交易紀錄 (包含第 8 欄：歸還時間)
        log_recs = sheets["log"].get_all_records()
        new_transactions = {}
        max_id = 0
        
        for r in log_recs:
            t_id = int(r.get("交易編號", 0))
            if t_id > max_id: max_id = t_id
            
            status = str(r.get("狀態"))
            # 為了讓前端能看到歸還時間，我們把所有狀態都同步進去
            new_transactions[t_id] = {
                "交易編號": t_id, 
                "設備名稱": str(r["設備名稱"]),
                "租借人員學號": str(r.get("借用人學號", "")), 
                "租借人員姓名": str(r.get("借用人姓名", "")), 
                "借用時間": str(r.get("借用時間", "")),
                "狀態": status,
                "處理人員": str(r.get("點收幹部", "")),
                "歸還時間": str(r.get("歸還時間", "")) # 🌟 對接試算表 H 欄
            }
        transactions = new_transactions
        transaction_id_counter = max_id + 1
    except Exception as e:
        print(f"⚠️ [同步異常]: {e}")

sync_all_from_cloud()

@app.get("/equipments")
def get_equipments():
    sync_all_from_cloud()
    return equipments

@app.get("/transactions")
def get_transactions():
    sync_all_from_cloud()
    return transactions

# 學生端：批量申請
@app.post("/borrow_batch")
def borrow_batch(data: dict):
    global transaction_id_counter
    sid = data.get("租借人員學號")
    sname = data.get("租借人員姓名")
    items = data.get("設備清單")
    
    with db_lock:
        for item in items:
            eid = item["id"]
            qty = int(item["qty"])
            if eid in equipments and equipments[eid]["剩餘數量"] >= qty:
                equipments[eid]["剩餘數量"] -= qty
                b_time = datetime.now().strftime("%Y-%m-%d %H:%M")
                try:
                    for _ in range(qty):
                        # 寫入 7 欄 (第 8 欄預設空白)
                        sheets["log"].append_row([transaction_id_counter, item["name"], sid, sname, b_time, "待審核", ""])
                        transaction_id_counter += 1
                    
                    cell = sheets["equip"].find(eid)
                    if cell:
                        sheets["equip"].update_cell(cell.row, 4, equipments[eid]["剩餘數量"])
                except Exception as e:
                    print(f"寫入錯誤: {e}")
            else:
                return {"成功": False, "訊息": f"{item['name']} 庫存不足"}
        sync_all_from_cloud()
        return {"成功": True}

# 管理端：審核
@app.post("/admin/approve")
def admin_approve(data: dict):
    tid = int(data.get("交易編號"))
    action = data.get("動作")
    admin_name = data.get("點收幹部")
    
    with db_lock:
        cell_log = sheets["log"].find(str(tid), in_column=1)
        if not cell_log: return {"成功": False, "訊息": "找不到紀錄"}
        
        if action == "核准":
            sheets["log"].update_cell(cell_log.row, 6, "借用中")
            sheets["log"].update_cell(cell_log.row, 7, admin_name)
        elif action == "駁回":
            sheets["log"].update_cell(cell_log.row, 6, "已駁回")
            sheets["log"].update_cell(cell_log.row, 7, admin_name)
            # 庫存回填邏輯...
            equip_name = sheets["log"].cell(cell_log.row, 2).value
            cell_equip = sheets["equip"].find(equip_name, in_column=2)
            if cell_equip:
                curr = int(sheets["equip"].cell(cell_equip.row, 4).value)
                sheets["equip"].update_cell(cell_equip.row, 4, curr + 1)
        
        sync_all_from_cloud()
        return {"成功": True}

# 🛠️ 管理端：單筆歸還 (新增歸還時間)
@app.post("/return")
def return_item(data: dict):
    tid = int(data.get("交易編號"))
    admin_name = data.get("點收幹部")
    r_time = datetime.now().strftime("%Y-%m-%d %H:%M") # 🌟 抓取時間
    
    with db_lock:
        cell_log = sheets["log"].find(str(tid), in_column=1)
        if not cell_log: return {"成功": False, "訊息": "找不到紀錄"}
        
        equip_name = sheets["log"].cell(cell_log.row, 2).value
        # 更新狀態、幹部、歸還時間 (H 欄 = 第 8 欄)
        sheets["log"].update_cell(cell_log.row, 6, "已歸還")
        sheets["log"].update_cell(cell_log.row, 7, admin_name)
        sheets["log"].update_cell(cell_log.row, 8, r_time) # 🌟 寫入 H 欄
        
        cell_equip = sheets["equip"].find(equip_name, in_column=2)
        if cell_equip:
            curr = int(sheets["equip"].cell(cell_equip.row, 4).value)
            sheets["equip"].update_cell(cell_equip.row, 4, curr + 1)
        
        sync_all_from_cloud()
        return {"成功": True}

# 🛠️ 管理端：末三碼批量歸還 (新增歸還時間)
@app.post("/return_by_student")
def return_by_student(data: dict):
    query_code = str(data.get("學號")).strip()
    admin_name = data.get("點收幹部")
    r_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    count = 0
    
    if len(query_code) < 3:
        return {"成功": False, "訊息": "請輸入至少 3 碼以確保準確"}

    with db_lock:
        # 只針對「借用中」且「末三碼符合」的 ID
        to_return = [tid for tid, req in transactions.items() 
                     if req["狀態"] == "借用中" and str(req["租借人員學號"]).endswith(query_code)]
        
        if not to_return:
            return {"成功": False, "訊息": "找不到符合條件的借用紀錄"}

        for tid in to_return:
            equip_name = transactions[tid]["設備名稱"]
            cell_log = sheets["log"].find(str(tid), in_column=1)
            if cell_log:
                sheets["log"].update_cell(cell_log.row, 6, "已歸還")
                sheets["log"].update_cell(cell_log.row, 7, admin_name)
                sheets["log"].update_cell(cell_log.row, 8, r_time) # 🌟 寫入歸還時間
                
                cell_equip = sheets["equip"].find(equip_name, in_column=2)
                if cell_equip:
                    curr = int(sheets["equip"].cell(cell_equip.row, 4).value)
                    sheets["equip"].update_cell(cell_equip.row, 4, curr + 1)
                count += 1
                time.sleep(0.5)
        
        sync_all_from_cloud()
        return {"成功": True, "歸還數量": count}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))