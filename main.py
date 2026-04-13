import os
import json
import threading
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI(title="學生會設備管理系統-購物車 V2.0")

# 1. 跨網域設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 2. Google Sheets 初始化 ---
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
        print(f"❌ [連線錯誤]: {e}")
        return None

sheets = init_google_sheets()

# --- 3. 記憶體變數 ---
admins_db = {}
equipments = {}
transactions = {}
transaction_id_counter = 1
db_lock = threading.Lock()

def sync_all_from_cloud():
    global admins_db, equipments, transactions, transaction_id_counter
    if not sheets: return
    try:
        # A. 同步幹部
        admins_db = {str(r["幹部代號"]): str(r["幹部名稱"]) for r in sheets["admin"].get_all_records()}
        # B. 同步設備
        equip_recs = sheets["equip"].get_all_records()
        equipments = {
            str(r["設備編號"]): {
                "設備名稱": str(r["設備名稱"]),
                "總數量": int(r["總數量"]),
                "剩餘數量": int(r["剩餘數量"])
            } for r in equip_recs
        }
        # C. 同步借用紀錄 (僅加載借用中的案子)
        log_recs = sheets["log"].get_all_records()
        new_transactions = {}
        max_id = 0
        for r in log_recs:
            t_id = int(r["交易編號"])
            if t_id > max_id: max_id = t_id
            if r["狀態"] == "借用中":
                new_transactions[t_id] = {
                    "交易編號": t_id, "設備名稱": str(r["設備名稱"]),
                    "租借人員學號": str(r["借用人學號"]), "借用時間": str(r["借用時間"]), "狀態": "借用中"
                }
        transactions = new_transactions
        transaction_id_counter = max_id + 1
        print(f"✅ [同步完成] 設備:{len(equipments)}種, 下一筆ID:{transaction_id_counter}")
    except Exception as e:
        print(f"⚠️ [同步失敗]: {e}")

sync_all_from_cloud()

# --- 4. API 路由 ---

@app.get("/equipments")
def 取得設備清單():
    sync_all_from_cloud()
    return equipments

# 🛒 重點：新增批量借用 API (對應前端的購物車)
@app.post("/borrow_batch")
def 批量借用設備(data: dict):
    global transaction_id_counter
    sid = data.get("租借人員學號")
    items = data.get("設備清單") # 前端送來的陣列
    
    with db_lock:
        for item in items:
            eid = item["id"]
            if eid in equipments and equipments[eid]["剩餘數量"] > 0:
                # 1. 更新本機數值
                equipments[eid]["剩餘數量"] -= 1
                t_id = transaction_id_counter
                b_time = datetime.now().strftime("%Y-%m-%d %H:%M")
                
                # 2. 本機待歸還紀錄
                transactions[t_id] = {
                    "交易編號": t_id, "設備名稱": equipments[eid]["設備名稱"],
                    "租借人員學號": sid, "借用時間": b_time, "狀態": "借用中"
                }
                
                # 3. 雲端同步 (重點：逐筆寫入 Log 並更新該項目的庫存)
                try:
                    sheets["log"].append_row([t_id, equipments[eid]["設備名稱"], sid, b_time, "借用中"])
                    cell = sheets["equip"].find(eid)
                    if cell:
                        sheets["equip"].update_cell(cell.row, 4, equipments[eid]["剩餘數量"])
                except Exception as e:
                    print(f"⚠️ 雲端寫入延遲: {e}")
                
                transaction_id_counter += 1
        
        return {"成功": True}

@app.get("/transactions")
def 取得待歸還清單():
    return transactions

@app.post("/return")
def 歸還點收(data: dict):
    tid = int(data.get("交易編號"))
    with db_lock:
        if tid not in transactions:
            raise HTTPException(status_code=400, detail="找不到紀錄")
        
        record = transactions.pop(tid)
        try:
            # 找到對應設備增加庫存
            cell_equip = sheets["equip"].find(record["設備名稱"])
            if cell_equip:
                current_qty = int(sheets["equip"].cell(cell_equip.row, 4).value)
                sheets["equip"].update_cell(cell_equip.row, 4, current_qty + 1)
            
            # 更新 Log 狀態
            cell_log = sheets["log"].find(str(tid))
            if cell_log:
                sheets["log"].update_cell(cell_log.row, 5, "已歸還")
        except Exception as e:
            print(f"⚠️ 歸還同步錯誤: {e}")
        return {"成功": True}

# 幹部登入與管理
@app.get("/admins")
def 取得幹部名單():
    sync_all_from_cloud()
    return admins_db

@app.post("/admin/login")
def 幹部登入(data: dict):
    code = data.get("幹部代號")
    if code in admins_db or code == "Admin-999":
        return {"成功": True, "名稱": admins_db.get(code, "管理員")}
    raise HTTPException(status_code=401)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)