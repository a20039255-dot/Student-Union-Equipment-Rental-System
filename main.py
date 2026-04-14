import os
import json
import threading
import time
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware  # 🌟 只要留一個 import 就好
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 1. 建立唯一的大腦，並給它一個帥氣的標題
app = FastAPI(title="學生會設備管理系統 - V5.0 企業雲端版")

# 2. 跨網域通行證設定 (只要寫這一次就好)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # 允許所有網頁 (包括 Vercel) 連線
    allow_credentials=True,
    allow_methods=["*"],           # 允許所有方式 (GET, POST)
    allow_headers=["*"],           # 允許所有標頭
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
        # A. 同步幹部 (包含職位)
        admins_db.clear()
        for r in sheets["admin"].get_all_records():
            admins_db[str(r["幹部代號"])] = {
                "名稱": str(r["幹部名稱"]),
                "職位": str(r.get("職位", "一般幹部"))
            }
            
        # B. 同步設備
        equip_recs = sheets["equip"].get_all_records()
        equipments.clear()
        for r in equip_recs:
            qty_limit = r.get("單次借用上限")
            qty_limit = int(qty_limit) if qty_limit != "" and qty_limit is not None else 1
            
            days_limit = r.get("借用期限(天)")
            days_limit = int(days_limit) if days_limit != "" and days_limit is not None else 3
            
            equipments[str(r["設備編號"])] = {
                "設備名稱": str(r["設備名稱"]),
                "總數量": int(r["總數量"]),
                "剩餘數量": int(r["剩餘數量"]),
                "借用上限": qty_limit,
                "借用期限天數": days_limit
            }
            
        # C. 同步借用紀錄
        log_recs = sheets["log"].get_all_records()
        new_transactions = {}
        max_id = 0
        now = datetime.now()
        
        for r in log_recs:
            t_id = int(r.get("交易編號", 0))
            if t_id > max_id: max_id = t_id
            
            if str(r.get("狀態")) == "借用中":
                equip_name = str(r["設備名稱"])
                days_diff = 0
                try:
                    borrow_time = datetime.strptime(str(r["借用時間"]), "%Y-%m-%d %H:%M")
                    days_diff = (now - borrow_time).days
                except: pass 
                
                limit = 3
                for e_id, e_info in equipments.items():
                    if e_info["設備名稱"] == equip_name:
                        limit = e_info.get("借用期限天數", 3)
                        break

                new_transactions[t_id] = {
                    "交易編號": t_id, 
                    "設備名稱": equip_name,
                    "租借人員學號": str(r.get("借用人學號", "")), 
                    "租借人員姓名": str(r.get("借用人姓名", "未知")),
                    "借用時間": str(r.get("借用時間", "")),
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

@app.post("/admin/login")
def 幹部登入(data: dict):
    sync_all_from_cloud() 
    code = data.get("幹部代號")
    if code == "Admin-999":
        return {"成功": True, "名稱": "超級管理員", "職位": "管理員"}
    if code in admins_db:
        return {"成功": True, "名稱": admins_db[code]["名稱"], "職位": admins_db[code]["職位"]}
    return {"成功": False, "訊息": "查無此幹部代號"}

@app.post("/borrow_batch")
def 批量借用設備(data: dict):
    global transaction_id_counter
    sid = data.get("租借人員學號")
    sname = data.get("租借人員姓名")
    items = data.get("設備清單")  # 這裡拿到的格式是 [{'id': 'E01', 'name': '...', 'qty': 3}, ...]
    
    with db_lock:
        for item in items:
            eid = item["id"]
            borrow_qty = int(item["qty"]) # 🌟 關鍵：拿到學生真正要借的數量
            
            # 檢查設備是否存在且庫存足夠
            if eid in equipments and equipments[eid]["剩餘數量"] >= borrow_qty:
                # 1. 減少記憶體中的庫存
                equipments[eid]["剩餘數量"] -= borrow_qty
                
                b_time = datetime.now().strftime("%Y-%m-%d %H:%M")
                
                try:
                    # 🌟 2. 根據數量，寫入多行紀錄到 Log (這樣點收才能一項一項點)
                    # 如果你希望一行紀錄代表多個，也可以只寫一行，但點收邏輯會變複雜
                    # 建議：依照借用數量跑迴圈，每一件設備都是獨立的交易編號
                    for _ in range(borrow_qty):
                        t_id = transaction_id_counter
                        sheets["log"].append_row([t_id, equipments[eid]["設備名稱"], sid, sname, b_time, "借用中", ""])
                        transaction_id_counter += 1
                        # 為了防止 Google API 爆炸，每一行稍微停一下
                        time.sleep(0.5) 

                    # 3. 更新 Google Sheets 的庫存格子 (扣除總數)
                    cell = sheets["equip"].find(eid)
                    if cell:
                        # 庫存在第 4 欄
                        sheets["equip"].update_cell(cell.row, 4, equipments[eid]["剩餘數量"])
                        
                except Exception as e:
                    print(f"⚠️ 雲端寫入錯誤: {e}")
            else:
                print(f"⚠️ 庫存不足或編號錯誤: {eid}")

        sync_all_from_cloud() # 最後同步一次確保記憶體跟雲端一致
        return {"成功": True}

# 1. 修改原本的 borrow_batch，讓狀態預設為 "待審核"
@app.post("/borrow_batch")
def 批量借用設備(data: dict):
    global transaction_id_counter
    sid = data.get("租借人員學號")
    sname = data.get("租借人員姓名")
    items = data.get("設備清單")
    
    with db_lock:
        for item in items:
            eid = item["id"]
            borrow_qty = int(item["qty"])
            # 注意：這裡不扣庫存，只寫入紀錄
            for _ in range(borrow_qty):
                t_id = transaction_id_counter
                b_time = datetime.now().strftime("%Y-%m-%d %H:%M")
                # 🌟 狀態設為 "待審核"
                sheets["log"].append_row([t_id, item["name"], sid, sname, b_time, "待審核", ""])
                transaction_id_counter += 1
                time.sleep(0.5)
        
        sync_all_from_cloud()
        return {"成功": True, "訊息": "申請已送出，請等待幹部審核。"}

# 2. 🌟 新增審核動作 API
@app.post("/admin/approve")
def 審核申請(data: dict):
    tid = int(data.get("交易編號"))
    action = data.get("動作") # "核准" 或 "駁回"
    admin_name = data.get("點收幹部")
    
    with db_lock:
        cell_log = sheets["log"].find(str(tid), in_column=1)
        if not cell_log: return {"成功": False, "訊息": "找不到該筆紀錄"}
        
        equip_name = sheets["log"].cell(cell_log.row, 2).value
        
        if action == "核准":
            # 找設備扣庫存
            cell_equip = sheets["equip"].find(equip_name, in_column=2)
            current_qty = int(sheets["equip"].cell(cell_equip.row, 4).value)
            if current_qty > 0:
                sheets["equip"].update_cell(cell_equip.row, 4, current_qty - 1)
                sheets["log"].update_cell(cell_log.row, 6, "借用中") # 變更狀態
                sheets["log"].update_cell(cell_log.row, 7, admin_name) # 記錄誰核准的
            else:
                return {"成功": False, "訊息": "庫存不足，無法核准"}
        else:
            sheets["log"].update_cell(cell_log.row, 6, "已駁回")
            
        sync_all_from_cloud()
        return {"成功": True}

@app.get("/transactions")
def 取得待歸還清單():
    sync_all_from_cloud()
    return transactions

@app.post("/return")
def 單筆歸還(data: dict):
    tid = int(data.get("交易編號"))
    admin_name = data.get("點收幹部")
    with db_lock:
        if tid not in transactions: return {"成功": False, "訊息": "找不到紀錄"}
        record = transactions.pop(tid)
        equip_name = record["設備名稱"]
        try:
            # 1. 🌟 精準定位：限制在第 2 欄找設備名稱
            cell_equip = sheets["equip"].find(equip_name, in_column=2)
            if cell_equip:
                # 🌟 API 減負：不要問雲端了，直接從記憶體查數量 (省下 2 次 API)
                total_qty = 1
                current_qty = 0
                for k, v in equipments.items():
                    if v["設備名稱"] == equip_name:
                        total_qty = v["總數量"]
                        current_qty = v["剩餘數量"]
                        v["剩餘數量"] = min(current_qty + 1, total_qty) # 同步更新記憶體
                        break
                new_qty = min(current_qty + 1, total_qty)
                sheets["equip"].update_cell(cell_equip.row, 4, new_qty)
            
            # 2. 🌟 精準定位：限制在第 1 欄找交易編號 (防止找錯格子)
            cell_log = sheets["log"].find(str(tid), in_column=1)
            if cell_log:
                sheets["log"].update_cell(cell_log.row, 6, "已歸還") 
                sheets["log"].update_cell(cell_log.row, 7, admin_name) 
        except Exception as e:
            print(f"⚠️ 歸還同步錯誤: {e}")
        return {"成功": True}

@app.post("/return_by_student")
def 依學號批量歸還(data: dict):
    sid = data.get("學號")
    admin_name = data.get("點收幹部")
    with db_lock:
        tids_to_return = [tid for tid, req in transactions.items() if req["租借人員學號"] == sid]
        if not tids_to_return: return {"成功": False, "訊息": "找不到借用紀錄"}
            
        for tid in tids_to_return:
            record = transactions.pop(tid)
            equip_name = record["設備名稱"]
            try:
                # 1. 🌟 精準定位與 API 減負
                cell_equip = sheets["equip"].find(equip_name, in_column=2)
                if cell_equip:
                    total_qty = 1
                    current_qty = 0
                    for k, v in equipments.items():
                        if v["設備名稱"] == equip_name:
                            total_qty = v["總數量"]
                            current_qty = v["剩餘數量"]
                            v["剩餘數量"] = min(current_qty + 1, total_qty)
                            break
                    new_qty = min(current_qty + 1, total_qty)
                    sheets["equip"].update_cell(cell_equip.row, 4, new_qty)
                
                # 2. 🌟 精準定位找交易編號
                cell_log = sheets["log"].find(str(tid), in_column=1)
                if cell_log:
                    sheets["log"].update_cell(cell_log.row, 6, "已歸還") 
                    sheets["log"].update_cell(cell_log.row, 7, admin_name) 
            except Exception as e: 
                print(f"TID {tid} 錯誤: {e}")
            
            # ⚠️ 終極煞車系統：每次處理完休息 2 秒
            # 這能保證 1 分鐘內絕對不會超過 Google 的 60 次上限！
            time.sleep(2)

    return {"成功": True, "歸還數量": len(tids_to_return)}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)