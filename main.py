import os
import json
import threading
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 引入 Google Sheets 專用套件
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI(title="學生會設備管理系統-雲端資料庫版")

# 1. 允許跨網域請求 (CORS) - 讓 Vercel 網頁可以連到 Render 後端
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
        # 優先讀取 Render 後台的環境變數 GOOGLE_JSON_KEY
        env_key = os.getenv("GOOGLE_JSON_KEY")
        
        if env_key:
            print("🚀 [系統] 偵測到環境變數，正在透過雲端金鑰啟動...")
            info = json.loads(env_key)
            creds = ServiceAccountCredentials.from_json_keyfile_dict(info, SCOPE)
        # 如果沒環境變數，才找本地檔案 (本機測試用)
        elif os.path.exists(KEY_FILE):
            print("🏠 [系統] 偵測到本地檔案，正在透過 google-key.json 啟動...")
            creds = ServiceAccountCredentials.from_json_keyfile_name(KEY_FILE, SCOPE)
        else:
            print("⚠️ [警告] 找不到任何 Google API 金鑰來源！")
            return None
        
        client = gspread.authorize(creds)
        # 開啟試算表與工作表
        spreadsheet = client.open("設備管理資料庫")
        sheet = spreadsheet.worksheet("admins")
        return sheet
    except Exception as e:
        print(f"❌ [錯誤] Google Sheets 連線失敗: {e}")
        return None

# 全域連線物件
admin_sheet = init_google_sheets()

# --- 3. 資料庫與同步邏輯 ---
admins_db = {}
# 設備庫存 (此部分目前存於記憶體，重啟會重置，若要永久保存可比照 admins 另建工作表)
equipments = {
    "E01": {"設備名稱": "投影機", "總數量": 5, "剩餘數量": 5},
    "E02": {"設備名稱": "無線電", "總數量": 20, "剩餘數量": 20},
    "E03": {"設備名稱": "延長線", "總數量": 15, "剩餘數量": 2}
}
transactions = {}
transaction_id_counter = 1
db_lock = threading.Lock()

def sync_from_google():
    """將 Google Sheets 裡的資料同步到 Python 記憶體"""
    global admins_db
    if admin_sheet:
        try:
            records = admin_sheet.get_all_records()
            new_db = {}
            for row in records:
                # 確保你的 Google 表格欄位名稱正確：幹部代號、幹部名稱
                new_db[str(row["幹部代號"])] = str(row["幹部名稱"])
            admins_db = new_db
            print(f"✅ [同步] 成功同步 {len(admins_db)} 位幹部資料。")
        except Exception as e:
            print(f"⚠️ [同步] 失敗: {e}")

# 啟動時執行同步
sync_from_google()

# --- 4. 資料格式定義 ---
class 借用請求(BaseModel):
    設備編號: str
    租借人員學號: str

class 歸還請求(BaseModel):
    交易編號: int
    歸還人員學號: str

class 登入請求(BaseModel):
    幹部代號: str

class 新增幹部請求(BaseModel):
    新幹部代號: str
    新幹部名稱: str

# --- 5. API 路徑設定 ---

@app.get("/equipments")
def 取得所有設備():
    return equipments

@app.post("/borrow")
def 借用設備(請求: 借用請求):
    global transaction_id_counter
    with db_lock:
        設備 = equipments.get(請求.設備編號)
        if not 設備 or 設備["剩餘數量"] <= 0:
            raise HTTPException(status_code=400, detail="設備庫存不足")
        
        設備["剩餘數量"] -= 1
        t_id = transaction_id_counter
        transactions[t_id] = {
            "交易編號": t_id,
            "設備編號": 請求.設備編號,
            "設備名稱": 設備["設備名稱"],
            "租借人員學號": 請求.租借人員學號,
            "借用時間": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "狀態": "借用中"
        }
        transaction_id_counter += 1
        return {"訊息": "借用成功", "交易紀錄": transactions[t_id]}

@app.post("/return")
def 歸還設備(請求: 歸還請求):
    with db_lock:
        交易 = transactions.get(請求.交易編號)
        if not 交易 or 交易["狀態"] != "借用中":
            raise HTTPException(status_code=400, detail="無效交易")
        
        交易["狀態"] = "已歸還"
        equipments[交易["設備編號"]]["剩餘數量"] += 1
        return {"訊息": "歸還成功"}

# --- 6. 幹部管理 (連動 Google Sheets) ---

@app.post("/admin/login")
def 幹部登入(請求: 登入請求):
    sync_from_google() # 登入時抓最新名單
    if 請求.幹部代號 in admins_db:
        return {"成功": True, "名稱": admins_db[請求.幹部代號]}
    # 萬一連線出問題，保留一個緊急後門
    if 請求.幹部代號 == "ADMIN-999":
        return {"成功": True, "名稱": "緊急管理員"}
    raise HTTPException(status_code=401, detail="驗證失敗")

@app.post("/admin/add")
def 新增幹部(請求: 新增幹部請求):
    with db_lock:
        if 請求.新幹部代號 in admins_db:
            raise HTTPException(status_code=400, detail="代號已存在")
        
        if admin_sheet:
            try:
                # 關鍵：將資料寫入 Google Sheets
                admin_sheet.append_row([請求.新幹部代號, 請求.新幹部名稱])
                # 同步到本地記憶體
                admins_db[請求.新幹部代號] = 請求.新幹部名稱
                return {"訊息": "成功新增至雲端資料庫"}
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"寫入雲端失敗: {e}")
        else:
            admins_db[請求.新幹部代號] = 請求.新幹部名稱
            return {"訊息": "僅更新暫時記憶體 (Google API 未連線)"}

@app.get("/admins")
def 取得所有幹部():
    sync_from_google()
    return admins_db