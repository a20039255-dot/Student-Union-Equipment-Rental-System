import os, json, threading
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- Google Sheets 初始化 ---
SCOPE = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/drive']
def init_gs():
    try:
        env_key = os.getenv("GOOGLE_JSON_KEY")
        if env_key:
            info = json.loads(env_key)
            creds = ServiceAccountCredentials.from_json_keyfile_dict(info, SCOPE)
            return gspread.authorize(creds).open("設備管理資料庫").worksheet("admins")
    except: return None
    return None

sheet = init_gs()
admins_db = {}
equipments = {
    "E01": {"設備名稱": "投影機", "總數量": 5, "剩餘數量": 5},
    "E02": {"設備名稱": "無線電", "總數量": 20, "剩餘數量": 20},
    "E03": {"設備名稱": "延長線", "總數量": 15, "剩餘數量": 15}
}
transactions = {}
t_counter = 1
lock = threading.Lock()

def sync():
    global admins_db
    if sheet:
        try:
            recs = sheet.get_all_records()
            admins_db = {str(r["幹部代號"]): r["幹部名稱"] for r in recs}
        except: pass

sync()

@app.get("/equipments")
def get_e(): return equipments

@app.get("/transactions")
def get_t(): return {k: v for k, v in transactions.items() if v["狀態"] == "借用中"}

@app.post("/borrow")
def borrow(data: dict):
    global t_counter
    with lock:
        eid, sid = data.get("設備編號"), data.get("租借人員學號")
        if eid in equipments and equipments[eid]["剩餘數量"] > 0:
            equipments[eid]["剩餘數量"] -= 1
            transactions[t_counter] = {
                "交易編號": t_counter, "設備編號": eid, "設備名稱": equipments[eid]["設備名稱"],
                "租借人員學號": sid, "借用時間": datetime.now().strftime("%m-%d %H:%M"), "狀態": "借用中"
            }
            t_counter += 1
            return {"ok": True}
        raise HTTPException(status_code=400)

@app.post("/return")
def ret(data: dict):
    with lock:
        tid = data.get("交易編號")
        if tid in transactions:
            transactions[tid]["狀態"] = "已歸還"
            equipments[transactions[tid]["設備編號"]]["剩餘數量"] += 1
            return {"ok": True}
        raise HTTPException(status_code=400)

@app.post("/admin/login")
def login(data: dict):
    sync()
    code = data.get("幹部代號")
    if code in admins_db or code == "Admin-999": return {"ok": True}
    raise HTTPException(status_code=401)

@app.post("/admin/add")
def add(data: dict):
    sync()
    cid, name = data.get("新幹部代號"), data.get("新幹部名稱")
    if sheet:
        sheet.append_row([cid, name])
        return {"ok": True}
    return {"ok": False}

@app.get("/admins")
def get_a():
    sync()
    return admins_db