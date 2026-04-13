from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
import threading
import csv  # 👈 老兵新武器：內建的 CSV 處理套件
import os   # 👈 用來檢查檔案存不存在

app = FastAPI(title="學生會設備部租借系統 API", version="2.0持久化版")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 設備與交易資料庫 (這部分我們先保持在記憶體裡) ---
equipments = {
    "E01": {"設備名稱": "投影機", "總數量": 5, "剩餘數量": 5},
    "E02": {"設備名稱": "無線電", "總數量": 20, "剩餘數量": 20},
    "E03": {"設備名稱": "延長線", "總數量": 15, "剩餘數量": 2}
}
transactions = {}
transaction_id_counter = 1
db_lock = threading.Lock()

# ==========================================
# 💾 全新模組：幹部名冊持久化 (寫入 CSV 檔案)
# ==========================================
CSV_FILE = "admins.csv"
admins_db = {} # 這個字典現在會負責跟 CSV 檔案同步

def 載入幹部名單():
    """系統啟動時，把 CSV 的資料讀進記憶體"""
    if not os.path.exists(CSV_FILE):
        # 如果檔案不存在 (第一次執行)，就幫你建一個，並放入預設鑰匙
        # 老兵秘訣：編碼使用 'utf-8-sig'，這樣 Windows Excel 打開才不會是亂碼！
        with open(CSV_FILE, mode='w', encoding='utf-8-sig', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(["幹部代號", "幹部名稱"]) # 寫入標題列
            writer.writerow(["Admin-999", "管理者"]) # 寫入預設資料
    
    # 讀取 CSV 檔案
    with open(CSV_FILE, mode='r', encoding='utf-8-sig') as file:
        reader = csv.DictReader(file)
        for row in reader:
            admins_db[row["幹部代號"]] = row["幹部名稱"]

def 儲存幹部名單():
    """每次有新增幹部時，把記憶體的資料寫回 CSV 存檔"""
    with open(CSV_FILE, mode='w', encoding='utf-8-sig', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(["幹部代號", "幹部名稱"])
        for 代號, 名稱 in admins_db.items():
            writer.writerow([代號, 名稱])

# 🚀 系統啟動第一件事：執行載入函數！
載入幹部名單()

# ==========================================

# --- 定義前端傳來的資料格式 ---
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

# --- 設備借還 API (維持原樣) ---
@app.get("/equipments", tags=["1. 查詢系統"])
def 取得所有設備():
    return equipments

@app.get("/transactions", tags=["1. 查詢系統"])
def 取得未歸還清單():
    未歸還 = {}
    for 編號, 紀錄 in transactions.items():
        if 紀錄["狀態"] == "借用中":
            未歸還[編號] = 紀錄
    return 未歸還

@app.post("/borrow", tags=["2. 租借操作"])
def 借用設備(請求: 借用請求):
    global transaction_id_counter
    with db_lock:
        設備 = equipments.get(請求.設備編號)
        if not 設備: raise HTTPException(status_code=404, detail="找不到設備")
        if 設備["剩餘數量"] <= 0: raise HTTPException(status_code=400, detail="已被借光")

        設備["剩餘數量"] -= 1
        目前的編號 = transaction_id_counter
        transactions[目前的編號] = {
            "交易編號": 目前的編號, "設備編號": 請求.設備編號, "設備名稱": 設備["設備名稱"],
            "租借人員學號": 請求.租借人員學號, "借用時間": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "歸還時間": "尚未歸還", "歸還人員學號": "無", "狀態": "借用中"
        }
        transaction_id_counter += 1
        return {"訊息": "借用成功！", "交易紀錄": transactions[目前的編號]}

@app.post("/return", tags=["2. 租借操作"])
def 歸還設備(請求: 歸還請求):
    with db_lock:
        交易 = transactions.get(請求.交易編號)
        if not 交易: raise HTTPException(status_code=404, detail="找不到交易")
        if 交易["狀態"] != "借用中": raise HTTPException(status_code=400, detail="已歸還過")

        交易["狀態"] = "已歸還"
        交易["歸還時間"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        交易["歸還人員學號"] = 請求.歸還人員學號 
        equipments[交易["設備編號"]]["剩餘數量"] += 1
        return {"訊息": "歸還成功！", "交易紀錄": 交易}

# --- 權限管理 API (升級加入存檔功能) ---
@app.post("/admin/login", tags=["3. 權限管理"])
def 幹部登入驗證(請求: 登入請求):
    if 請求.幹部代號 in admins_db:
        return {"成功": True, "名稱": admins_db[請求.幹部代號]}
    else:
        raise HTTPException(status_code=401, detail="無效的幹部代號！")

@app.post("/admin/add", tags=["3. 權限管理"])
def 新增幹部(請求: 新增幹部請求):
    with db_lock:
        if 請求.新幹部代號 in admins_db:
            raise HTTPException(status_code=400, detail="代號已存在！")
        
        # 1. 更新記憶體
        admins_db[請求.新幹部代號] = 請求.新幹部名稱
        # 2. 觸發存檔機制！寫入 CSV！
        儲存幹部名單() 
        
        return {"訊息": f"成功新增幹部：{請求.新幹部名稱}"}

@app.get("/admins", tags=["3. 權限管理"])
def 取得幹部名單():
    return admins_db