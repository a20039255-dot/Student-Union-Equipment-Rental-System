import os
import json
import threading
import time
import requests
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_tw_time():
    tw_tz = timezone(timedelta(hours=8))
    return datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M")

def send_discord_notify(message, webhook_name="Discord網址"):
    global system_settings
    # 支援頻道分流：找不到指定鑰匙時，退回預設頻道
    webhook_url = system_settings.get(webhook_name) or system_settings.get("Discord網址")
    if not webhook_url or "discord.com" not in webhook_url: return
    try: requests.post(webhook_url, json={"content": message}, timeout=5)
    except: pass

SCOPE = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/drive']

def init_sheets():
    try:
        env_key = os.getenv("GOOGLE_JSON_KEY")
        info = json.loads(env_key) if env_key else json.load(open('google-key.json'))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(info, SCOPE)
        client = gspread.authorize(creds)
        
        # 🌟 終極驅魔：不再用名稱尋找，直接綁定這份試算表的唯一 ID！
        ss = client.open_by_key("1r0vqm8FU3KWp_56fjTW-aDW-8JPK5poXQ9jk-IhZ9Sc")
        
        sheets_dict = {
            "admin": ss.worksheet("admins"), 
            "equip": ss.worksheet("equipments"), 
            "log": ss.worksheet("log")
        }
        try: sheets_dict["settings"] = ss.worksheet("settings")
        except: pass
        return sheets_dict
    except Exception as e: 
        print(f"Sheets 連線失敗: {e}")
        return None
        try: sheets_dict["settings"] = ss.worksheet("settings")
        except: pass
        return sheets_dict
    except Exception as e: 
        print(f"Sheets 連線失敗: {e}")
        return None

sheets = init_sheets()
admins_db, equipments, transactions = {}, {}, {}
system_settings = {"借用天數限制": 14, "維護模式": "關閉", "系統公告": "", "Discord網址": "", "Discord逾期網址": ""}
transaction_id_counter = 1
db_lock = threading.Lock()

def sync_admin():
    global admins_db
    if not sheets: return
    admins_db.clear() # 🌟 驅魔關鍵：清空舊記憶
    for r in sheets["admin"].get_all_records(): admins_db[str(r["幹部代號"])] = r

def sync_equip():
    global equipments
    if not sheets: return
    equipments.clear() # 🌟 驅魔關鍵：清空舊庫存記憶
    for r in sheets["equip"].get_all_records(): equipments[str(r["設備編號"])] = r

def sync_log():
    global transaction_id_counter
    if not sheets: return
    recs = sheets["log"].get_all_records()
    transactions.clear()
    max_id = 0
    for r in recs:
        try:
            tid = int(r.get("交易編號", 0))
            if tid > max_id: max_id = tid
            transactions[tid] = {
                "交易編號": tid,
                "設備名稱": str(r.get("設備名稱", "")),
                "租借人員學號": str(r.get("借用人學號", "")), 
                "租借人員姓名": str(r.get("借用人姓名", "")), 
                "借用時間": str(r.get("借用時間", "")),
                "狀態": str(r.get("狀態", "")),
                "處理人員": str(r.get("點收幹部", "")),
                "歸還時間": str(r.get("歸還時間", ""))
            }
        except: pass
    transaction_id_counter = max_id + 1

def sync_settings():
    global system_settings
    if not sheets or "settings" not in sheets: return
    try:
        # 🌟 驅魔關鍵：重置回預設狀態，避免舊設定卡住
        new_settings = {"借用天數限制": 14, "維護模式": "關閉", "系統公告": "", "Discord網址": "", "Discord逾期網址": ""}
        for r in sheets["settings"].get_all_records():
            key = str(r.get("設定項目", "")).strip()
            val = r.get("設定值", "")
            if key: new_settings[key] = val
        system_settings = new_settings
    except: pass

if sheets:
    sync_admin()
    sync_equip()
    sync_log()
    sync_settings()

def get_row_mapping(sheet, col_index=1):
    try:
        values = sheet.col_values(col_index)
        return {str(val): idx + 1 for idx, val in enumerate(values)}
    except:
        return {}

@app.get("/settings")
def get_settings(): sync_settings(); return system_settings

@app.post("/admin/login")
def admin_login(data: dict):
    sync_admin()
    code = str(data.get("代號")).strip()
    if code in admins_db: return {"成功": True, "姓名": admins_db[code]["幹部名稱"]}
    return {"成功": False, "訊息": "代號不存在"}

@app.get("/equipments")
def get_equips(): sync_equip(); return equipments

@app.get("/transactions")
def get_trans(): sync_log(); return transactions

@app.post("/borrow_batch")
def borrow(data: dict):
    global transaction_id_counter, system_settings, transactions, equipments
    
    # 強制確保資料是最新的
    try:
        sync_settings() 
        sync_log()
        sync_equip()
    except Exception as e:
        print(f"Sync Error: {e}")

    if system_settings.get("維護模式") == "開啟":
        return {"成功": False, "訊息": "系統維護中，目前暫停借用服務！"}

    sid = str(data.get("租借人員學號", "")).strip()
    sname = data.get("租借人員姓名")
    items = data.get("設備清單", [])
    
    if not sid or not items:
        return {"成功": False, "訊息": "申請資料不完整"}
    
    # 🌟 核心防護：加上安全性檢查，避免 transactions 為空時報錯
    user_borrowed = {}
    if transactions:
        for tid, req in transactions.items():
            # 增加防呆：確保欄位存在且不是 None
            req_sid = str(req.get("租借人員學號", "")).strip()
            req_status = req.get("狀態", "")
            if sid in req_sid and req_status in ["待審核", "借用中"]:
                ename = req.get("設備名稱")
                user_borrowed[ename] = user_borrowed.get(ename, 0) + 1
            
    with db_lock:
        try:
            b_time = get_tw_time()
            new_rows = []
            equip_updates = []
            
            # 這裡我們直接從 Google 抓取最新庫存，不要相信記憶體，最保險！
            equip_mapping = get_row_mapping(sheets["equip"], 1)
            stocks_col = sheets["equip"].col_values(4) # 假設數量在第 D 欄
            
            for item in items:
                ename = item["name"]
                qty = int(item["qty"])
                
                # 尋找該設備的上限
                limit = 99 # 預設一個大數字
                for eid, eq in equipments.items():
                    if eq.get("設備名稱") == ename:
                        limit = int(eq.get("單次借用上限", eq.get("借用上限", 1)))
                        break
                
                # 超額驗證
                current_b = user_borrowed.get(ename, 0)
                if current_b + qty > limit:
                    return {"成功": False, "訊息": f"【{ename}】已達配額限制！(目前持有:{current_b}, 上限:{limit})"}
            
            # 驗證通過，準備寫入
            for item in items:
                ename = item["name"]
                qty = int(item["qty"])
                eid = str(item["id"])
                
                for _ in range(qty):
                    new_rows.append([transaction_id_counter, ename, sid, sname, b_time, "待審核", "", ""])
                    transaction_id_counter += 1
                
                # 更新庫存
                if eid in equip_mapping:
                    row_idx = equip_mapping[eid]
                    try:
                        # 確保 stock 讀取正常
                        current_stock = int(stocks_col[row_idx - 1]) if row_idx <= len(stocks_col) else 0
                        equip_updates.append({'range': f'D{row_idx}', 'values': [[current_stock - qty]]})
                    except: pass
            
            if new_rows: sheets["log"].append_rows(new_rows)
            if equip_updates: sheets["equip"].batch_update(equip_updates)
            
            # Discord 通知 (包起來避免 Discord 故障導致整單失敗)
            try:
                msg = f"🔔 **新申請**: `{sname}` 借了 `{', '.join([f'{i['name']}x{i['qty']}' for i in items])}`"
                send_discord_notify(msg, system_settings.get("Discord網址"))
            except: pass
            
            sync_log()
            return {"成功": True}
        except Exception as e:
            print(f"CRITICAL BORROW ERROR: {e}") # 這行會在 Cloud Run Log 顯示噴在哪
            return {"成功": False, "訊息": f"伺服器寫入失敗: {str(e)}"}
        
@app.post("/admin/approve_batch")
def approve_batch(data: dict):
    tids = data.get("交易編號清單", [])
    action = data.get("動作")
    admin = data.get("點收幹部")
    
    if not tids: return {"成功": False, "訊息": "無資料"}
    status = "借用中" if action == "核准" else "已駁回"
    current_time = get_tw_time() 
    
    with db_lock:
        try:
            log_updates = []
            inventory_add = {}
            log_mapping = get_row_mapping(sheets["log"], 1)
            
            for tid in tids:
                str_tid = str(tid)
                current_status = transactions.get(int(tid), {}).get("狀態")
                if current_status != "待審核":
                    continue 

                if str_tid in log_mapping:
                    row = log_mapping[str_tid]
                    # 核准時同時更新借用時間，駁回則只更新狀態與幹部
                    if action == "核准":
                        log_updates.append({'range': f'E{row}:G{row}', 'values': [[current_time, status, admin]]})
                    else:
                        log_updates.append({'range': f'F{row}:G{row}', 'values': [[status, admin]]})
                        
                    if action == "駁回" and int(tid) in transactions:
                        ename = transactions[int(tid)].get("設備名稱")
                        if ename: inventory_add[ename] = inventory_add.get(ename, 0) + 1
                            
            if log_updates: sheets["log"].batch_update(log_updates)
                
            if action == "駁回" and inventory_add:
                equip_updates = []
                equip_mapping = get_row_mapping(sheets["equip"], 2)
                stocks = sheets["equip"].col_values(4)
                for ename, qty in inventory_add.items():
                    if ename in equip_mapping:
                        row = equip_mapping[ename]
                        try: curr = int(stocks[row - 1])
                        except: curr = 0
                        equip_updates.append({'range': f'D{row}', 'values': [[curr + qty]]})
                if equip_updates: sheets["equip"].batch_update(equip_updates)
                    
            sync_log()
            return {"成功": True, "處理數量": len(log_updates)}
        except Exception as e:
            print(f"Approve Error: {e}")
            return {"成功": False, "訊息": "系統連線錯誤，請稍後再試"}

@app.post("/return")
def return_item(data: dict):
    tid, admin = int(data.get("交易編號")), data.get("點收幹部")
    r_time = get_tw_time()
    
    with db_lock:
        try:
            current_status = transactions.get(tid, {}).get("狀態")
            if current_status != "借用中":
                return {"成功": False, "訊息": "該設備已被歸還或非借用狀態"}

            log_mapping = get_row_mapping(sheets["log"], 1)
            if str(tid) in log_mapping:
                row = log_mapping[str(tid)]
                ename = transactions.get(tid, {}).get("設備名稱")
                sheets["log"].update(f"F{row}:H{row}", [["已歸還", admin, r_time]])
                
                if ename:
                    equip_mapping = get_row_mapping(sheets["equip"], 2)
                    if ename in equip_mapping:
                        eq_row = equip_mapping[ename]
                        stocks = sheets["equip"].col_values(4)
                        try: curr = int(stocks[eq_row - 1])
                        except: curr = 0
                        sheets["equip"].update_cell(eq_row, 4, curr + 1)
            sync_log()
            return {"成功": True}
        except Exception as e:
            print(f"Return Error: {e}")
            return {"成功": False, "訊息": "系統連線錯誤，請稍後再試"}

@app.post("/return_by_student")
def return_by_sid(data: dict):
    code = str(data.get("學號")).strip()
    admin = data.get("點收幹部")
    r_time = get_tw_time()
    
    with db_lock:
        try:
            to_return_tids = []
            inventory_add = {}
            for tid, req in transactions.items():
                if req["狀態"] == "借用中" and str(req["租借人員學號"]).endswith(code):
                    to_return_tids.append(tid)
                    ename = req["設備名稱"]
                    inventory_add[ename] = inventory_add.get(ename, 0) + 1
                    
            if not to_return_tids: return {"成功": False, "訊息": "找不到紀錄"}

            log_mapping = get_row_mapping(sheets["log"], 1)
            log_updates = []
            for tid in to_return_tids:
                if str(tid) in log_mapping:
                    row = log_mapping[str(tid)]
                    log_updates.append({'range': f'F{row}:H{row}', 'values': [['已歸還', admin, r_time]]})
            if log_updates: sheets["log"].batch_update(log_updates)
                
            equip_mapping = get_row_mapping(sheets["equip"], 2)
            equip_updates = []
            stocks = sheets["equip"].col_values(4)
            for ename, qty in inventory_add.items():
                if ename in equip_mapping:
                    row = equip_mapping[ename]
                    try: curr = int(stocks[row - 1])
                    except: curr = 0
                    equip_updates.append({'range': f'D{row}', 'values': [[curr + qty]]})
            if equip_updates: sheets["equip"].batch_update(equip_updates)
                
            sync_log()
            return {"成功": True, "歸還數量": len(log_updates)}
        except Exception as e:
            print(f"Batch Return Error: {e}")
            return {"成功": False, "訊息": "系統連線錯誤，請稍後再試"}

@app.get("/cron/overdue_notify")
def check_overdue():
    sync_log()
    sync_settings()
    try: max_days = int(system_settings.get("借用天數限制", 14))
    except: max_days = 14
        
    today = datetime.now(timezone(timedelta(hours=8)))
    overdue_list = []
    
    for tid, req in transactions.items():
        if req.get("狀態") == "借用中":
            b_time_str = req.get("借用時間", "")
            if b_time_str:
                try:
                    b_date = datetime.strptime(b_time_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=8)))
                    diff_days = (today - b_date).days
                    if diff_days > max_days:
                        overdue_days = diff_days - max_days
                        overdue_list.append(f"🔹 **{req['租借人員姓名']}**\n  └ 設備：`{req['設備名稱']}` (已逾期 {overdue_days} 天)")
                except: pass
    
    if overdue_list:
        display_list = overdue_list[:15]
        msg = f"🚨 **【設備逾期警告】** 🚨\n目前共有 **{len(overdue_list)}** 筆未歸還設備已逾期（超過 {max_days} 天）：\n\n"
        msg += "\n".join(display_list)
        if len(overdue_list) > 15:
            msg += f"\n\n...以及其他 {len(overdue_list) - 15} 筆，請至管理後台查看完整清單。"
        msg += "\n\n👉 請值班幹部協助催討！"
        send_discord_notify(msg, "Discord逾期網址")
        return {"發送成功": True, "逾期筆數": len(overdue_list)}
    else:
        return {"發送成功": False, "訊息": "目前沒有逾期設備，大家都很乖！"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))