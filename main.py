import os
import json
import threading
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI(title="學生會設備管理系統 - V3.0 旗艦版")

# 1. 跨網域設定
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

# 3. 記憶體變數
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
        
        equip_recs = sheets["equip"].get_all_records()
        equipments.clear()
        for r in equip_recs:
            # 抓取數量上限 (Column E，傳給前端購物車防呆用)
            qty_limit = r.get("單次借用上限")
            qty_limit = int(qty_limit) if qty_limit != "" and qty_limit is not None else 1
            
            # 抓取天數期限 (Column F，留在後端算催收用)
            days_limit = r.get("借用期限(天)")
            days_limit = int(days_limit) if days_limit != "" and days_limit is not None else 3
            
            equipments[str(r["設備編號"])] = {
                "設備名稱": str(r["設備名稱"]),
                "總數量": int(r["總數量"]),
                "剩餘數量": int(r["剩餘數量"]),
                "借用上限": qty_limit,       # 傳給前端，藍牙喇叭會變回 5
                "借用期限天數": days_limit   # 留在後端計算 14 天過期
            }
            
        # C. 同步借用紀錄 (計算過期天數)
        log_recs = sheets["log"].get_all_records()
        new_transactions = {}
        max_id = 0
        now = datetime.now()
        
        for r in log_recs:
            t_id = int(r["交易編號"])
            if t_id > max_id: max_id = t_id
            
            if r["狀態"] == "借用中":
                equip_name = str(r["設備名稱"])
                
                # 計算已借用天數
                days_diff = 0
                try:
                    borrow_time = datetime.strptime(str(r["借用時間"]), "%Y-%m-%d %H:%M")
                    days_diff = (now - borrow_time).days
                except:
                    pass 
                
                # 取得該設備的【天數期限】來判定是否過期
                limit = 3
                for e_id, e_info in equipments.items():
                    if e_info["設備名稱"] == equip_name:
                        limit = e_info.get("借用期限天數", 3) # 這裡改抓獨立的天數變數
                        break

                new_transactions[t_id] = {
                    "交易編號": t_id, 
                    "設備名稱": equip_name,
                    "租借人員學號": str(r["借用人學號"]), 
                    "借用時間": str(r["借用時間"]),
                    "已借用天數": days_diff,
                    "是否過期": days_diff >= limit,
                    "狀態": "借用中"
                }
                
        transactions = new_transactions
        transaction_id_counter = max_id + 1
        print(f"✅ [同步完成] 設備:{len(equipments)}種, 待歸還:{len(transactions)}筆")
    except Exception as e:
        print(f"⚠️ [同步失敗]: {e}")

sync_all_from_cloud()

# --- 4. API 路由 ---

@app.get("/equipments")
def 取得設備清單():
    sync_all_from_cloud()
    return equipments

@app.post("/borrow_batch")
def 批量借用設備(data: dict):
    global transaction_id_counter
    sid = data.get("租借人員學號")
    items = data.get("設備清單")
    
    with db_lock:
        for item in items:
            eid = item["id"]
            if eid in equipments and equipments[eid]["剩餘數量"] > 0:
                equipments[eid]["剩餘數量"] -= 1
                t_id = transaction_id_counter
                b_time = datetime.now().strftime("%Y-%m-%d %H:%M")
                
                try:
                    sheets["log"].append_row([t_id, equipments[eid]["設備名稱"], sid, b_time, "借用中"])
                    cell = sheets["equip"].find(eid)
                    if cell:
                        sheets["equip"].update_cell(cell.row, 4, equipments[eid]["剩餘數量"])
                except Exception as e:
                    print(f"⚠️ 雲端寫入錯誤: {e}")
                
                transaction_id_counter += 1
        sync_all_from_cloud() # 強制刷新記憶體
        return {"成功": True}

@app.get("/transactions")
def 取得待歸還清單():
    sync_all_from_cloud() # 每次查看都更新天數
    return transactions

@app.post("/return")
def 單筆歸還(data: dict):
    tid = int(data.get("交易編號"))
    with db_lock:
        if tid not in transactions:
            return {"成功": False, "訊息": "找不到紀錄"}
        
        record = transactions.pop(tid)
        try:
            cell_equip = sheets["equip"].find(record["設備名稱"])
            if cell_equip:
                current_qty = int(sheets["equip"].cell(cell_equip.row, 4).value)
                sheets["equip"].update_cell(cell_equip.row, 4, current_qty + 1)
            
            cell_log = sheets["log"].find(str(tid))
            if cell_log:
                sheets["log"].update_cell(cell_log.row, 5, "已歸還")
        except Exception as e:
            print(f"⚠️ 歸還同步錯誤: {e}")
        return {"成功": True}

@app.post("/return_by_student")
def 依學號批量歸還(data: dict):
    sid = data.get("學號")
    with db_lock:
        tids_to_return = [tid for tid, req in transactions.items() if req["租借人員學號"] == sid]
        if not tids_to_return:
            return {"成功": False, "訊息": "找不到該學號的借用紀錄"}
            
        for tid in tids_to_return:
            record = transactions.pop(tid)
            try:
                cell_equip = sheets["equip"].find(record["設備名稱"])
                if cell_equip:
                    current_qty = int(sheets["equip"].cell(cell_equip.row, 4).value)
                    sheets["equip"].update_cell(cell_equip.row, 4, current_qty + 1)
                
                cell_log = sheets["log"].find(str(tid))
                if cell_log:
                    sheets["log"].update_cell(cell_log.row, 5, "已歸還")
            except Exception as e:
                pass
    return {"成功": True, "歸還數量": len(tids_to_return)}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)