from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
import threading
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json 

app = FastAPI(title="學生會設備部-雲端資料庫版", version="3.0")

# 允許跨網域請求 (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 1. Google Sheets 初始化設定 ---
# 定義存取範圍
SCOPE = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/drive']
KEY_FILE = 'google-key.json'

# 全域變數，用來存放記憶體中的資料
admins_db = {}
equipments = {
    "E01": {"設備名稱": "投影機", "總數量": 5, "剩餘數量": 5},
    "E02": {"設備名稱": "無線電", "總數量": 20, "剩餘數量": 20},
    "E03": {"設備名稱": "延長線", "總數量": 15, "剩餘數量": 2}
}
transactions = {}
transaction_id_counter = 1
db_lock = threading.Lock()

def init_google_sheets():
    """初始化並連線至 Google Sheets"""
    try:
        if not os.path.exists(KEY_FILE):
            print(f"⚠️ 找不到 {KEY_FILE}，將切換為暫時記憶體模式")
            return None
        
        creds = ServiceAccountCredentials.from_json_keyfile_name(KEY_FILE, SCOPE)
        client = gspread.authorize(creds)
        # 打開試算表
        sheet = client.open("設備管理資料庫").worksheet("admins")
        return sheet
    except Exception as e:
        print(f"❌ Google Sheets 連線失敗: {e}")
        return None

# 啟動時先連線一次
def init_google_sheets():
    """初始化並連線至 Google Sheets (優先讀取環境變數)"""
    try:
        # 1. 嘗試從環境變數讀取字串
        env_key = os.getenv("GOOGLE_JSON_KEY")
        
        if env_key:
            print("🚀 偵測到環境變數，正在使用雲端金鑰...")
            # 將字串轉回 JSON 格式字典
            info = json.loads(env_key)
            creds = ServiceAccountCredentials.from_json_keyfile_dict(info, SCOPE) # 👈 注意這裡改用 dict
        
        # 2. 如果沒環境變數，才讀本地檔案 (開發用)
        elif os.path.exists(KEY_FILE):
            print("🏠 正在使用本地 google-key.json...")
            creds = ServiceAccountCredentials.from_json_keyfile_name(KEY_FILE, SCOPE)
            
        else:
            print("⚠️ 找不到任何金鑰來源")
            return None
        
        client = gspread.authorize(creds)
        sheet = client.open("設備管理資料庫").worksheet("admins")
        return sheet
    except Exception as e:
        print(f"❌ Google Sheets 連線失敗: {e}")
        return None
# 初始同步
sync_from_google()

# --- 2. 資料格式定義 (Pydantic Models) ---
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

# --- 3. API 端點 ---

@app.get("/equipments")
def 取得所有設備():
    return equipments

@app.get("/transactions")
def 取得未歸還清單():
    return {k: v for k, v in transactions.items() if v["狀態"] == "借用中"}

@app.post("/borrow")
def 借用設備(請求: 借用請求):
    global transaction_id_counter
    with db_lock:
        設備 = equipments.get(請求.設備編號)
        if not 設備 or 設備["剩餘數量"] <= 0:
            raise HTTPException(status_code=400, detail="設備不足或不存在")

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
            raise HTTPException(status_code=400, detail="無效的歸還請求")

        交易["狀態"] = "已歸還"
        equipments[交易["設備編號"]]["剩餘數量"] += 1
        return {"訊息": "歸還成功"}

# --- 4. 權限管理 API (連線 Google Sheets) ---

@app.post("/admin/login")
def 幹部登入驗證(請求: 登入請求):
    # 每次登入前先同步一次，確保拿到最新名單 (適合低頻率操作)
    sync_from_google()
    
    if 請求.幹部代號 in admins_db:
        return {"成功": True, "名稱": admins_db[請求.幹部代號]}
    else:
        # 預設後門，預防 API 掛掉時進不去
        if 請求.幹部代號 == "Admin-999":
            return {"成功": True, "名稱": "緊急管理員"}
        raise HTTPException(status_code=401, detail="無效代號")

@app.post("/admin/add")
def 新增幹部(請求: 新增幹部請求):
    with db_lock:
        if 請求.新幹部代號 in admins_db:
            raise HTTPException(status_code=400, detail="代號已存在")
        
        # 1. 寫入 Google Sheets (永久保存)
        if admin_sheet:
            try:
                admin_sheet.append_row([請求.新幹部代號, 請求.新幹部名稱])
                # 2. 更新記憶體
                admins_db[請求.新幹部代號] = 請求.新幹部名稱
                return {"訊息": f"成功新增並同步至雲端：{請求.新幹部名稱}"}
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"雲端寫入失敗: {e}")
        else:
            # 備用模式：僅更新記憶體
            admins_db[請求.新幹部代號] = 請求.新幹部名稱
            return {"訊息": "僅更新暫時記憶體 (Google API 未連線)"}

@app.get("/admins")
def 取得幹部名單():
    sync_from_google()
    return admins_db