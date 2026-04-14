import os
import json
import threading
import time
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 1. 初始化大腦
app = FastAPI(title="學生會設備管理系統 - 審核制 V6.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Google Sheets 初始化
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

# 3. 記憶體變數與同步邏輯
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
        admins_db.clear()
        for r in sheets["admin"].get_all_records():
            admins_db[str(r["幹部代號"])] = {"名稱": str(r["幹部名稱"]), "職位": str(r.get("職位", "一般幹部"))}
            
        # B. 同步設備
        equip_recs = sheets["equip"].get_all_records()
        equipments.clear()
        for r in equip_recs:
            equipments[str(r["設備編號"])] = {
                "設備名稱": str(r["設備名稱"]),
                "總數量": int(r["總數量"]),
                "剩餘數量": int(r["剩餘數量"]),
                "借用上限": int(r.get("單次借用上限", 1)),
                "借用期限天數": int(r.get("借用期限(天)", 3))
            }
            
        # C. 同步待處理紀錄 (待審核 + 借用中)
        log_recs = sheets["log"].get_all_records()
        new_transactions = {}
        max_id = 0
        now = datetime.now()
        
        for r in log_recs:
            t_id = int(r.get("交易編號", 0))
            if t_id > max_id: max_id = t_id
            
            status = str(r.get("狀態"))
            if status in ["借用中", "待審核"]:
                # 🌟 這裡要讀取試算表第 7 欄，標題通常是「點收幹部」
                handler = str(r.get("點收幹部", "尚未處理"))

                new_transactions[t_id] = {
                    "交易編號": t_id, 
                    "設備名稱": str(r["設備名稱"]),
                    "租借人員學號": str(r.get("借用人學號", "")), 
                    "租借人員姓名": str(r.get("借用人姓名", "未知")),
                    "借用時間": str(r.get("借用時間", "")),
                    "狀態": status,
                    "處理人員": handler # 🌟 將處理人存入字典傳給前端
                }
        transactions = new_transactions
        transaction_id_counter = max_id + 1
    except Exception as e:
        print(f"⚠️ [同步失敗]: {e}")

sync_all_from_cloud()

# --- 4. API 路由 ---

@app.get("/equipments")
def 取得設備清單():
    sync_all_from_cloud()
    return equipments

@app.get("/transactions")
def 取得交易紀錄():
    sync_all_from_cloud()
    return transactions

@app.post("/admin/login")
def 幹部登入(data: dict):
    code = data.get("幹部代號")
    if code == "Admin-999": return {"成功": True, "名稱": "超級管理員", "職位": "管理員"}
    if code in admins_db: return {"成功": True, "名稱": admins_db[code]["名稱"], "職位": admins_db[code]["職位"]}
    return {"成功": False, "訊息": "查無此幹部代號"}

# 🌟 核心：強預扣借用申請
@app.post("/borrow_batch")
def 批量借用申請(data: dict):
    global transaction_id_counter
    sid = data.get("租借人員學號")
    sname = data.get("租借人員姓名")
    items = data.get("設備清單")
    
    with db_lock:
        for item in items:
            eid = item["id"]
            qty = int(item["qty"])
            if eid in equipments and equipments[eid]["剩餘數量"] >= qty:
                # 強預扣
                equipments[eid]["剩餘數量"] -= qty
                b_time = datetime.now().strftime("%Y-%m-%d %H:%M")
                try:
                    for _ in range(qty):
                        t_id = transaction_id_counter
                        sheets["log"].append_row([t_id, item["name"], sid, sname, b_time, "待審核", ""])
                        transaction_id_counter += 1
                        time.sleep(0.4)
                    
                    # 更新雲端庫存
                    cell = sheets["equip"].find(eid)
                    if cell: sheets["equip"].update_cell(cell.row, 4, equipments[eid]["剩餘數量"])
                except Exception as e: print(f"Log 寫入失敗: {e}")
            else:
                return {"成功": False, "訊息": f"{item['name']} 庫存不足"}
        
        sync_all_from_cloud()
        return {"成功": True}

# 🌟 核心：審核功能 (核准/駁回)
@app.post("/admin/approve")
def 審核申請(data: dict):
    tid = int(data.get("交易編號"))
    action = data.get("動作") # "核准" 或 "駁回"
    admin_name = data.get("點收幹部")
    
    with db_lock:
        try:
            cell_log = sheets["log"].find(str(tid), in_column=1)
            if not cell_log: return {"成功": False, "訊息": "找不到紀錄"}
            
            equip_name = sheets["log"].cell(cell_log.row, 2).value
            
            if action == "核准":
                sheets["log"].update_cell(cell_log.row, 6, "借用中")
                sheets["log"].update_cell(cell_log.row, 7, admin_name)
            elif action == "駁回":
                sheets["log"].update_cell(cell_log.row, 6, "已駁回")
                sheets["log"].update_cell(cell_log.row, 7, admin_name)
                # 駁回需退回庫存
                cell_equip = sheets["equip"].find(equip_name, in_column=2)
                if cell_equip:
                    curr_stock = int(sheets["equip"].cell(cell_equip.row, 4).value)
                    sheets["equip"].update_cell(cell_equip.row, 4, curr_stock + 1)
            
            sync_all_from_cloud()
            return {"成功": True}
        except Exception as e: return {"成功": False, "訊息": str(e)}

@app.post("/return")
def 歸還設備(data: dict):
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
        except Exception as e: return {"成功": False, "訊息": str(e)}
        @app.post("/return_by_student")
        def 依學號批量歸還(data: dict):
            sid = data.get("學號")
    admin_name = data.get("點收幹部")
    
    with db_lock:
        # 🌟 關鍵修正：只抓出狀態是「借用中」的紀錄，排除「待審核」或「已歸還」
        tids_to_return = [tid for tid, req in transactions.items() 
                          if req["租借人員學號"] == sid and req["狀態"] == "借用中"]
        
        if not tids_to_return: 
            return {"成功": False, "訊息": "該學號目前沒有『借用中』的設備（可能尚在審核中或已歸還）"}
            
        count = 0
        for tid in tids_to_return:
            record = transactions.get(tid)
            if not record: continue
            
            equip_name = record["設備名稱"]
            try:
                # 1. 變更 Log 狀態為已歸還
                cell_log = sheets["log"].find(str(tid), in_column=1)
                if cell_log:
                    sheets["log"].update_cell(cell_log.row, 6, "已歸還") 
                    sheets["log"].update_cell(cell_log.row, 7, admin_name) 

                # 2. 將庫存加回去
                cell_equip = sheets["equip"].find(equip_name, in_column=2)
                if cell_equip:
                    curr_stock = int(sheets["equip"].cell(cell_equip.row, 4).value)
                    sheets["equip"].update_cell(cell_equip.row, 4, curr_stock + 1)
                
                count += 1
                # 🛑 煞車：避免 Google API 頻率限制
                time.sleep(1.5)
            except Exception as e:
                print(f"批量歸還 TID {tid} 錯誤: {e}")

        sync_all_from_cloud()
        return {"成功": True, "歸還數量": count}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)