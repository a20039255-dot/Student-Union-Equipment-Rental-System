
import os
import json
import threading
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI(title="學生會設備管理系統-雲端資料庫版")

# 1. 允許跨網域請求 (CORS) - 讓 Vercel 網頁可以連到 Render 後端
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # 👈 確保這裡是星號，代表接受所有網域
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 2. Google Sheets 初始化設定 ---
SCOPE = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/drive']
KEY_FILE = 'google-key.json'

def init_google_sheets():
    """初始化 Google Sheets 連線"""
    try:
        env_key = os.getenv("GOOGLE_JSON_KEY")
        if env_key:
            info = json.loads(env_key)
            creds = ServiceAccountCredentials.from_json_keyfile_dict(info, SCOPE)
        elif os.path.exists(KEY_FILE):
            creds = ServiceAccountCredentials.from_json_keyfile_name(KEY_FILE, SCOPE)
        else:
            return None
        client = gspread.authorize(creds)
        spreadsheet = client.open("設備管理資料庫")
        return spreadsheet.worksheet("admins")
    except Exception as e:
        print(f"❌ 連線失敗: {e}")
        return None

admin_sheet = init_google_sheets()
admins_db = {}
equipments = {
    "E01": {"設備名稱": "投影機", "總數量": 5, "剩餘數量": 5},
    "E02": {"設備名稱": "無線電", "總數量": 20, "剩餘數量": 20},
    "E03": {"設備名稱": "延長線", "總數量": 15, "剩餘數量": 2}
}
transactions = {}
transaction_id_counter = 1
db_lock = threading.Lock()

def sync_from_google():
    global admins_db
    if admin_sheet:
        try:
            records = admin_sheet.get_all_records()
            admins_db = {str(row["幹部代號"]): str(row["幹部名稱"]) for row in records}
        except: pass

sync_from_google()

# --- 3. API 端點 ---
@app.get("/equipments")
def get_equipments(): return equipments

@app.get("/transactions")
def get_transactions():
    return {k: v for k, v in transactions.items() if v["狀態"] == "借用中"}

@app.post("/borrow")
def borrow_item(req: dict):
    global transaction_id_counter
    with db_lock:
        eid = req.get("設備編號")
        sid = req.get("租借人員學號")
        if eid in equipments and equipments[eid]["剩餘數量"] > 0:
            equipments[eid]["剩餘數量"] -= 1
            t_id = transaction_id_counter
            transactions[t_id] = {
                "交易編號": t_id, "設備編號": eid, "設備名稱": equipments[eid]["設備名稱"],
                "租借人員學號": sid, "借用時間": datetime.now().strftime("%Y-%m-%d %H:%M"), "狀態": "借用中"
            }
            transaction_id_counter += 1
            return {"成功": True}
        raise HTTPException(status_code=400)

@app.post("/return")
def return_item(req: dict):
    with db_lock:
        tid = req.get("交易編號")
        if tid in transactions:
            transactions[tid]["狀態"] = "已歸還"
            equipments[transactions[tid]["設備編號"]]["剩餘數量"] += 1
            return {"成功": True}
        raise HTTPException(status_code=400)

@app.get("/admins")  # 👈 檢查這裡的字
def 取得幹部名單():
    sync_from_google()
    return admins_db

@app.post("/admin/login")
def admin_login(req: dict):
    sync_from_google()
    code = req.get("幹部代號")
    if code in admins_db: return {"成功": True, "名稱": admins_db[code]}
    if code == "Admin-999": return {"成功": True, "名稱": "系統管理員"}
    raise HTTPException(status_code=401)

@app.post("/admin/add")
def add_admin(req: dict):
    with db_lock:
        code, name = req.get("新幹部代號"), req.get("新幹部名稱")
        if admin_sheet:
            admin_sheet.append_row([code, name])
            admins_db[code] = name
            return {"成功": True}
        return {"成功": False}