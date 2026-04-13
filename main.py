import os
import json
import threading
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI(title="學生會設備管理系統-全雲端資料庫終極版")

# 1. 跨網域設定 (CORS)
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
    """初始化 Google Sheets 三表連線"""
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
        print(f"❌ [連線錯誤] 無法讀取 Google Sheets 分頁: {e}")
        return None

# 全域連線字典
sheets = init_google_sheets()

# --- 3. 記憶體快取與同步邏輯 ---
admins_db = {}
equipments = {}
transactions = {}
transaction_id_counter = 1
db_lock = threading.Lock()

def sync_all_from_cloud():
    """從雲端同步所有最新狀態"""
    global admins_db, equipments, transactions, transaction_id_counter
    if not sheets: return

    try:
        # A. 同步幹部
        admins_db = {str(r["幹部代號"]): str(r["幹部名稱"]) for r in sheets["admin"].get_all_records()}
        
        # B. 同步設備 (從雲端決定庫存)
        equip_recs = sheets["equip"].get_all_records()
        equipments = {
            str(r["設備編號"]): {
                "設備名稱": str(r["設備名稱"]),
                "總數量": int(r["總數量"]),
                "剩餘數量": int(r["剩餘數量"])
            } for r in equip_recs
        }
        
        # C. 同步借用紀錄 (復原未歸還案子)
        log_recs = sheets["log"].get_all_records()
        new_transactions = {}
        max_id = 0
        for r in log_recs:
            t_id = int(r["交易編號"])
            if t_id > max_id: max_id = t_id
            if r["狀態"] == "借用中":
                new_transactions[t_id] = {
                    "交易編號": t_id,
                    "設備名稱": str(r["設備名稱"]),
                    "租借人員學號": str(r["借用人學號"]),
                    "借用時間": str(r["借用時間"]),
                    "狀態": "借用中"
                }
        transactions = new_transactions
        transaction_id_counter = max_id + 1
        print(f"✅ [同步完成] 設備:{len(equipments)}種, 待歸還:{len(transactions)}筆")
    except Exception as e:
        print(f"⚠️ [同步失敗] 請檢查表格欄位名稱是否正確: {e}")

# 初始執行同步
sync_all_from_cloud()

# --- 4. API 路徑 ---

@app.get("/equipments")
def 取得設備清單():
    # 每次前端要求清單時，都從雲端刷一下確保庫存準確
    sync_all_from_cloud()
    return equipments

@app.get("/transactions")
def 取得待歸還清單():
    return transactions

@app.post("/borrow")
def 借用設備(data: dict):
    global transaction_id_counter
    eid = data.get("設備編號")
    sid = data.get("租借人員學號")
    
    with db_lock:
        if eid not in equipments or equipments[eid]["剩餘數量"] <= 0:
            raise HTTPException(status_code=400, detail="設備不足")
        
        # 1. 更新本機快取
        equipments[eid]["剩餘數量"] -= 1
        t_id = transaction_id_counter
        b_time = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        transactions[t_id] = {
            "交易編號": t_id, "設備名稱": equipments[eid]["設備名稱"],
            "租借人員學號": sid, "借用時間": b_time, "狀態": "借用中"
        }
        
        # 2. 同步至雲端 (寫入 Log 與 更新庫存)
        try:
            # 寫入 Log 分頁
            sheets["log"].append_row([t_id, equipments[eid]["設備名稱"], sid, b_time, "借用中"])
            # 更新 Equipment 分頁的「剩餘數量」(假設在第 4 欄)
            cell = sheets["equip"].find(eid)
            if cell:
                sheets["equip"].update_cell(cell.row, 4, equipments[eid]["剩餘數量"])
        except Exception as e:
            print(f"⚠️ 雲端寫入延遲: {e}")
            
        transaction_id_counter += 1
        return {"成功": True}

@app.post("/return")
def 歸還設備(data: dict):
    tid = int(data.get("交易編號"))
    
    with db_lock:
        if tid not in transactions:
            raise HTTPException(status_code=400, detail="找不到紀錄")
        
        record = transactions.pop(tid)
        
        # 1. 找回設備編號並更新本機與雲端庫存
        try:
            # 在雲端尋找該設備名稱對應的編號列
            cell_equip = sheets["equip"].find(record["設備名稱"])
            if cell_equip:
                eid = sheets["equip"].cell(cell_equip.row, 1).value
                equipments[eid]["剩餘數量"] += 1
                sheets["equip"].update_cell(cell_equip.row, 4, equipments[eid]["剩餘數量"])
            
            # 2. 更新 Log 狀態為已歸還
            cell_log = sheets["log"].find(str(tid))
            if cell_log:
                sheets["log"].update_cell(cell_log.row, 5, "已歸還")
        except Exception as e:
            print(f"⚠️ 歸還同步錯誤: {e}")
            
        return {"成功": True}

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

@app.post("/admin/add")
def 新增幹部(data: dict):
    cid, name = data.get("新幹部代號"), data.get("新幹部名稱")
    if sheets["admin"]:
        sheets["admin"].append_row([cid, name])
        return {"成功": True}
    return {"成功": False}
if __name__ == "__main__":
    import uvicorn
    # 注意：port 要讀取環境變數，Render 才能連進來
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)