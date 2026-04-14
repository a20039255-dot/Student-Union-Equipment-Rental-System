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
app = FastAPI(title="設備管理系統後端 - 部長級最終版")

# 解決跨域問題 (CORS)
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
        # 優先從環境變數讀取 (Cloud Run 建議)
        env_key = os.getenv("GOOGLE_JSON_KEY")
        if env_key:
            info = json.loads(env_key)
            creds = ServiceAccountCredentials.from_json_keyfile_dict(info, SCOPE)
        else:
            # 本地測試使用
            creds = ServiceAccountCredentials.from_json_keyfile_name('google-key.json', SCOPE)
        
        client = gspread.authorize(creds)
        spreadsheet = client.open("設備管理資料庫") # 請確保試算表名稱正確
        
        return {
            "admin": spreadsheet.worksheet("admins"),
            "equip": spreadsheet.worksheet("equipments"),
            "log": spreadsheet.worksheet("log")
        }
    except Exception as e:
        print(f"❌ [Sheets 連線失敗]: {e}")
        return None

sheets = init_google_sheets()

# 3. 全域變數與同步鎖定
admins_db = {}
equipments = {}
transactions = {}
transaction_id_counter = 1
db_lock = threading.Lock()

def sync_all_from_cloud():
    """ 從 Google Sheets 同步所有數據到記憶體 """
    global admins_db, equipments, transactions, transaction_id_counter
    if not sheets: return
    try:
        # A. 同步幹部名單
        admins_db.clear()
        for r in sheets["admin"].get_all_records():
            admins_db[str(r["幹部代號"])] = {"名稱": str(r["幹部名稱"]), "職位": str(r.get("職位", ""))}
            
        # B. 同步設備庫存
        equip_recs = sheets["equip"].get_all_records()
        equipments.clear()
        for r in equip_recs:
            equipments[str(r["設備編號"])] = {
                "設備名稱": str(r["設備名稱"]),
                "總數量": int(r["總數量"]),
                "剩餘數量": int(r["剩餘數量"]),
                "借用上限": int(r.get("單次借用上限", 1))
            }
            
        # C. 同步待處理交易 (待審核 + 借用中)
        log_recs = sheets["log"].get_all_records()
        new_transactions = {}
        max_id = 0
        now = datetime.now()
        
        for r in log_recs:
            t_id = int(r.get("交易編號", 0))
            if t_id > max_id: max_id = t_id
            
            status = str(r.get("狀態"))
            if status in ["借用中", "待審核"]:
                days_diff = 0
                try:
                    borrow_time = datetime.strptime(str(r["借用時間"]), "%Y-%m-%d %H:%M")
                    days_diff = (now - borrow_time).days
                except: pass 

                new_transactions[t_id] = {
                    "交易編號": t_id, 
                    "設備名稱": str(r["設備名稱"]),
                    "租借人員學號": str(r.get("借用人學號", "")), 
                    "租借人員姓名": str(r.get("借用人姓名", "")), # 這格會包含單位與電話
                    "借用時間": str(r.get("借用時間", "")),
                    "已借用天數": days_diff,
                    "狀態": status,
                    "處理人員": str(r.get("點收幹部", "尚未審核"))
                }
        transactions = new_transactions
        transaction_id_counter = max_id + 1
        print(f"✅ [同步完成] 目前處理編號至: {transaction_id_counter}")
    except Exception as e:
        print(f"⚠️ [同步異常]: {e}")

sync_all_from_cloud()

# --- 4. API 路由設計 ---

@app.get("/equipments")
def get_equipments():
    sync_all_from_cloud()
    return equipments

@app.get("/transactions")
def get_transactions():
    sync_all_from_cloud()
    return transactions

@app.post("/admin/login")
def admin_login(data: dict):
    code = data.get("幹部代號")
    if code in admins_db:
        return {"成功": True, "名稱": admins_db[code]["名稱"], "職位": admins_db[code]["職位"]}
    return {"成功": False, "訊息": "代號錯誤"}

# 🚀 學生端：強預扣借用申請
@app.post("/borrow_batch")
def borrow_batch(data: dict):
    global transaction_id_counter
    sid = data.get("租借人員學號")
    sname = data.get("租借人員姓名") # 內含 姓名 (單位) - 電話
    items = data.get("設備清單")
    
    with db_lock:
        for item in items:
            eid = item["id"]
            qty = int(item["qty"])
            
            if eid in equipments and equipments[eid]["剩餘數量"] >= qty:
                # 記憶體預扣
                equipments[eid]["剩餘數量"] -= qty
                b_time = datetime.now().strftime("%Y-%m-%d %H:%M")
                
                try:
                    for _ in range(qty):
                        t_id = transaction_id_counter
                        # 寫入試算表 Log: 編號, 名稱, 學號, 姓名資訊, 時間, 狀態, 處理人
                        sheets["log"].append_row([t_id, item["name"], sid, sname, b_time, "待審核", ""])
                        transaction_id_counter += 1
                        time.sleep(0.4) # 防止 API 頻率過快

                    # 更新雲端庫存
                    cell = sheets["equip"].find(eid)
                    if cell:
                        sheets["equip"].update_cell(cell.row, 4, equipments[eid]["剩餘數量"])
                except Exception as e:
                    print(f"寫入錯誤: {e}")
            else:
                return {"成功": False, "訊息": f"{item['name']} 庫存不足"}
        
        sync_all_from_cloud()
        return {"成功": True}

# 🛠️ 管理端：審核操作 (核准/駁回)
@app.post("/admin/approve")
def admin_approve(data: dict):
    tid = int(data.get("交易編號"))
    action = data.get("動作") # "核准" 或 "駁回"
    admin_name = data.get("點收幹部")
    
    with db_lock:
        try:
            cell_log = sheets["log"].find(str(tid), in_column=1)
            if not cell_log: return {"成功": False, "訊息": "找不到紀錄"}
            
            current_status = sheets["log"].cell(cell_log.row, 6).value
            if current_status != "待審核": return {"成功": False, "訊息": "此筆非待審核狀態"}

            if action == "核准":
                sheets["log"].update_cell(cell_log.row, 6, "借用中")
                sheets["log"].update_cell(cell_log.row, 7, admin_name)
            
            elif action == "駁回":
                # 駁回需將預扣的庫存加回去
                equip_name = sheets["log"].cell(cell_log.row, 2).value
                sheets["log"].update_cell(cell_log.row, 6, "已駁回")
                sheets["log"].update_cell(cell_log.row, 7, admin_name)
                
                cell_equip = sheets["equip"].find(equip_name, in_column=2)
                if cell_equip:
                    curr_stock = int(sheets["equip"].cell(cell_equip.row, 4).value)
                    sheets["equip"].update_cell(cell_equip.row, 4, curr_stock + 1)

            sync_all_from_cloud()
            return {"成功": True}
        except Exception as e:
            return {"成功": False, "訊息": str(e)}

# 🛠️ 管理端：單筆歸還
@app.post("/return")
def return_item(data: dict):
    tid = int(data.get("交易編號"))
    admin_name = data.get("點收幹部")
    with db_lock:
        try:
            cell_log = sheets["log"].find(str(tid), in_column=1)
            if not cell_log: return {"成功": False, "訊息": "找不到紀錄"}
            
            equip_name = sheets["log"].cell(cell_log.row, 2).value
            sheets["log"].update_cell(cell_log.row, 6, "已歸還")
            sheets["log"].update_cell(cell_log.row, 7, admin_name)
            
            cell_equip = sheets["equip"].find(equip_name, in_column=2)
            if cell_equip:
                curr_stock = int(sheets["equip"].cell(cell_equip.row, 4).value)
                sheets["equip"].update_cell(cell_equip.row, 4, curr_stock + 1)
            
            sync_all_from_cloud()
            return {"成功": True}
        except Exception as e:
            return {"成功": False, "訊息": str(e)}

# 🛠️ 管理端：批量歸還 (只處理借用中)
@app.post("/return_by_student")
def return_by_student(data: dict):
    sid = data.get("學號")
    admin_name = data.get("點收幹部")
    count = 0
    with db_lock:
        # 只過濾出該學生的「借用中」項目
        to_return = [tid for tid, req in transactions.items() 
                     if req["租借人員學號"] == sid and req["狀態"] == "借用中"]
        
        for tid in to_return:
            equip_name = transactions[tid]["設備名稱"]
            cell_log = sheets["log"].find(str(tid), in_column=1)
            if cell_log:
                sheets["log"].update_cell(cell_log.row, 6, "已歸還")
                sheets["log"].update_cell(cell_log.row, 7, admin_name)
                cell_equip = sheets["equip"].find(equip_name, in_column=2)
                if cell_equip:
                    curr_stock = int(sheets["equip"].cell(cell_equip.row, 4).value)
                    sheets["equip"].update_cell(cell_equip.row, 4, curr_stock + 1)
                count += 1
                time.sleep(0.5)
        
        sync_all_from_cloud()
        return {"成功": True, "歸還數量": count}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)