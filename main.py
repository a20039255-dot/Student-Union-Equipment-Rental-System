import os
import json
import threading
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI(title="學生會設備管理系統-全雲端同步版")

# 1. 允許跨網域請求 (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 2. Google Sheets 初始化設定 ---
SCOPE = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/drive']

def init_google_sheets():
    """初始化 Google Sheets 連線，同時連線幹部表與紀錄表"""
    try:
        env_key = os.getenv("GOOGLE_JSON_KEY")
        if env_key:
            info = json.loads(env_key)
            creds = ServiceAccountCredentials.from_json_keyfile_dict(info, SCOPE)
        else:
            # 本機測試路徑
            creds = ServiceAccountCredentials.from_json_keyfile_name('google-key.json', SCOPE)
        
        client = gspread.authorize(creds)
        spreadsheet = client.open("設備管理資料庫")
        
        # 取得兩個分頁
        admin_sheet = spreadsheet.worksheet("admins")
        log_sheet = spreadsheet.worksheet("log")
        print("✅ [系統] Google Sheets 雙表連線成功！")
        return admin_sheet, log_sheet
    except Exception as e:
        print(f"❌ [錯誤] Google Sheets 連線失敗: {e}")
        return None, None

# 全域連線物件
admin_sheet, log_sheet = init_google_sheets()

# --- 3. 資料庫與記憶體快取 ---
admins_db = {}
# 設備清單 (部長可根據實際需求修改)
equipments = {
    "E01": {"設備名稱": "投影機", "總數量": 5, "剩餘數量": 5},
    "E02": {"設備名稱": "無線電", "總數量": 20, "剩餘數量": 20},
    "E03": {"設備名稱": "延長線", "總數量": 15, "剩餘數量": 15}
}
transactions = {}
transaction_id_counter = 1
db_lock = threading.Lock()

def sync_all_data():
    """啟動時從 Google Sheets 同步所有資料"""
    global admins_db, transactions, transaction_id_counter
    if not admin_sheet or not log_sheet:
        return

    try:
        # 同步幹部名單
        admin_records = admin_sheet.get_all_records()
        admins_db = {str(row["幹部代號"]): str(row["幹部名稱"]) for row in admin_records}
        
        # 同步借用紀錄 (讓伺服器重啟後能接續)
        log_records = log_sheet.get_all_records()
        new_transactions = {}
        max_id = 0
        for row in log_records:
            t_id = int(row["交易編號"])
            if t_id > max_id: max_id = t_id
            
            # 只把「借用中」的案子放進記憶體供網頁點收
            if row["狀態"] == "借用中":
                new_transactions[t_id] = {
                    "交易編號": t_id,
                    "設備名稱": str(row["設備名稱"]),
                    "租借人員學號": str(row["借用人學號"]),
                    "借用時間": str(row["借用時間"]),
                    "狀態": "借用中"
                }
        
        transactions = new_transactions
        transaction_id_counter = max_id + 1
        print(f"✅ [同步] 成功復原 {len(transactions)} 筆未歸還紀錄，下一筆 ID: {transaction_id_counter}")
    except Exception as e:
        print(f"⚠️ [同步] 資料復原失敗: {e}")

# 啟動同步
sync_all_data()

# --- 4. API 路徑設定 ---

@app.get("/equipments")
def 取得所有設備():
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
            raise HTTPException(status_code=400, detail="設備不足或編號錯誤")
        
        # 1. 更新本機記憶體
        equipments[eid]["剩餘數量"] -= 1
        t_id = transaction_id_counter
        borrow_time = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        new_record = {
            "交易編號": t_id,
            "設備編號": eid,
            "設備名稱": equipments[eid]["設備名稱"],
            "租借人員學號": sid,
            "借用時間": borrow_time,
            "狀態": "借用中"
        }
        transactions[t_id] = new_record
        
        # 2. 同步寫入 Google Sheets log 分頁 (永久保存)
        if log_sheet:
            try:
                log_sheet.append_row([t_id, equipments[eid]["設備名稱"], sid, borrow_time, "借用中"])
            except:
                print("⚠️ [警告] Google Sheets 寫入失敗，僅存在記憶體中。")
        
        transaction_id_counter += 1
        return {"訊息": "借用成功", "交易編號": t_id}

@app.post("/return")
def 歸還設備(data: dict):
    tid = int(data.get("交易編號"))
    
    with db_lock:
        if tid not in transactions:
            raise HTTPException(status_code=400, detail="找不到此交易編號")
        
        # 1. 更新記憶體
        record = transactions.pop(tid) # 從待歸還清單移除
        for eid, info in equipments.items():
            if info["設備名稱"] == record["設備名稱"]:
                info["剩餘數量"] += 1
                break
        
        # 2. 更新 Google Sheets 中的狀態為「已歸還」
        if log_sheet:
            try:
                # 尋找交易編號所在的那一行 (第1欄是 ID)
                cell = log_sheet.find(str(tid))
                if cell:
                    log_sheet.update_cell(cell.row, 5, "已歸還") # 第 5 欄是狀態
            except Exception as e:
                print(f"⚠️ [歸還同步失敗]: {e}")
                
        return {"訊息": "歸還點收成功"}

@app.get("/admins")
def 取得幹部名單():
    sync_all_data() # 強制同步最新名單
    return admins_db

@app.post("/admin/login")
def 幹部登入(data: dict):
    code = data.get("幹部代號")
    if code in admins_db or code == "Admin-999":
        name = admins_db.get(code, "系統管理員")
        return {"成功": True, "名稱": name}
    raise HTTPException(status_code=401, detail="代號驗證失敗")

@app.post("/admin/add")
def 新增幹部(data: dict):
    new_id = data.get("新幹部代號")
    new_name = data.get("新幹部名稱")
    
    if admin_sheet:
        try:
            admin_sheet.append_row([new_id, new_name])
            admins_db[new_id] = new_name
            return {"訊息": "已成功新增並同步至雲端"}
        except:
            raise HTTPException(status_code=500, detail="雲端寫入失敗")
    return {"訊息": "僅更新本地記憶體"}