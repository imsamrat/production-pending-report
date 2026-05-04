import os
import xmlrpc.client
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from gspread_dataframe import set_with_dataframe
import pytz
import argparse
import logging
import sys

# Setup logging
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
log = logging.getLogger()

load_dotenv()

# ========= CONFIG ==========
ODOO_URL = os.getenv("ODOO_URL", "").rstrip('/')
DB = os.getenv("ODOO_DB", "")
USERNAME = os.getenv("ODOO_USERNAME", "")
PASSWORD = os.getenv("ODOO_PASSWORD", "")
API_KEY = os.getenv("ODOO_API_KEY", "")

# Google Sheets Configuration
SHEET_KEY = '1F7epdshmtSM8iPmSgYTY9l7Hwsz4X0uuvShVi87s75o'

parser = argparse.ArgumentParser()
parser.add_argument("--from_date", type=str, default=None)
parser.add_argument("--to_date", type=str, default=None)
args = parser.parse_args()

_now = datetime.now()
FROM_DATE = args.from_date if args.from_date else "2025-08-01 00:00:00"
TO_DATE = args.to_date if args.to_date else _now.strftime("%Y-%m-%d %H:%M:%S")

log.info(f"Using FROM_DATE={FROM_DATE}, TO_DATE={TO_DATE}")

# Helpers
def safe_name(val):
    return val[1] if isinstance(val, list) and len(val) > 1 else (val or "")

def main():
    # ----------------- AUTH (XML-RPC) -----------------
    log.info("🔐 Authenticating...")
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(DB, USERNAME, API_KEY or PASSWORD, {})
        if not uid:
            log.error("❌ Authentication failed.")
            sys.exit(1)
        log.info(f"✅ Authenticated, UID={uid}")
        
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    except Exception as e:
        log.error(f"❌ Connection error: {e}")
        sys.exit(1)

    # ----------------- 1. FETCH PI DATA -----------------
    log.info("🔄 Fetching PI data from Odoo (sale.order)...")
    pi_order_domain = [
        ("sales_type", "=", "sale"),
        ("state", "!=", "cancel"),
        ("pi_type", "=", "regular"),
        ("pi_date", ">=", FROM_DATE),
        ("pi_date", "<=", TO_DATE),
        ("company_id", "in", [1, 3]),
    ]
    
    pi_order_fields = ["name", "pi_date", "company_id"]
    
    try:
        pi_orders = models.execute_kw(DB, uid, API_KEY or PASSWORD, "sale.order", 'search_read', [pi_order_domain], {'fields': pi_order_fields})
        log.info(f"✅ {len(pi_orders)} PI orders found.")
        
        pi_data = []
        if pi_orders:
            pi_order_dict = {o['id']: o for o in pi_orders}
            pi_order_ids = list(pi_order_dict.keys())
            
            # Fetch corresponding lines
            pi_line_domain = [("order_id", "in", pi_order_ids)]
            pi_line_fields = ["order_id", "product_uom_qty", "price_subtotal"]
            pi_lines = models.execute_kw(DB, uid, API_KEY or PASSWORD, "sale.order.line", 'search_read', [pi_line_domain], {'fields': pi_line_fields})
            
            for line in pi_lines:
                order_id = line.get("order_id")[0] if isinstance(line.get("order_id"), list) else line.get("order_id")
                order = pi_order_dict.get(order_id, {})
                pi_data.append({
                    "pi": safe_name(order.get("name") if 'name' in order else ""),
                    "pi_date": order.get("pi_date") or "",
                    "company": safe_name(order.get("company_id")),
                    "pi_qty": float(line.get("product_uom_qty") or 0.0),
                    "pi_value": float(line.get("price_subtotal") or 0.0)
                })
    except Exception as e:
        log.error(f"❌ Failed to fetch PI records: {e}")
        pi_data = []

    df_pi_raw = pd.DataFrame(pi_data)
    if df_pi_raw.empty:
        df_pi_raw = pd.DataFrame(columns=["pi", "pi_date", "company", "pi_qty", "pi_value"])
    
    # ----------------- 2. FETCH OA DATA -----------------
    log.info("🔄 Fetching OA data from Odoo (sale.order)...")
    oa_order_domain = [
        ("sales_type", "=", "oa"),
        ("state", "!=", "cancel"),
        # ("state", "=", "sale"),
        # ("date_order", ">=", FROM_DATE),
        # ("date_order", "<=", TO_DATE),
        ("company_id", "in", [1, 3]),
    ]

    oa_order_fields = ["order_ref", "company_id"]
    
    try:
        oa_orders = models.execute_kw(DB, uid, API_KEY or PASSWORD, "sale.order", 'search_read', [oa_order_domain], {'fields': oa_order_fields})
        log.info(f"✅ {len(oa_orders)} OA orders found.")
        
        oa_data = []
        if oa_orders:
            oa_order_dict = {o['id']: o for o in oa_orders}
            oa_order_ids = list(oa_order_dict.keys())
            
            # Fetch corresponding lines
            oa_line_domain = [("order_id", "in", oa_order_ids)]
            oa_line_fields = ["order_id", "product_uom_qty", "price_subtotal"]
            oa_lines = models.execute_kw(DB, uid, API_KEY or PASSWORD, "sale.order.line", 'search_read', [oa_line_domain], {'fields': oa_line_fields})
            
            for line in oa_lines:
                order_id = line.get("order_id")[0] if isinstance(line.get("order_id"), list) else line.get("order_id")
                order = oa_order_dict.get(order_id, {})
                oa_data.append({
                    "pi": safe_name(order.get("order_ref")) if isinstance(order.get("order_ref"), list) else (order.get("order_ref") or ""),
                    "company": safe_name(order.get("company_id")),
                    "oa_qty": float(line.get("product_uom_qty") or 0.0),
                    "oa_value": float(line.get("price_subtotal") or 0.0)
                })
    except Exception as e:
        log.error(f"❌ Failed to fetch OA records: {e}")
        oa_data = []

    df_oa_raw = pd.DataFrame(oa_data)
    if df_oa_raw.empty:
        df_oa_raw = pd.DataFrame(columns=["pi", "company", "oa_qty", "oa_value"])
        
    # ----------------- 3. PROCESS DATA (Pandas matching SQL) -----------------
    # Group by for PI
    if not df_pi_raw.empty:
        df_pi = df_pi_raw.groupby(["pi", "pi_date", "company"], as_index=False).agg({"pi_qty": "sum", "pi_value": "sum"})
    else:
        df_pi = df_pi_raw

    # Group by for OA
    if not df_oa_raw.empty:
        df_oa = df_oa_raw.groupby("pi", as_index=False).agg({"oa_qty": "sum", "oa_value": "sum"})
    else:
        df_oa = df_oa_raw

    # Merge (Left Join)
    if not df_pi.empty:
        df_merged = pd.merge(df_pi, df_oa, on="pi", how="left")
        
        # Fill NaN with 0 for OA fields
        df_merged["oa_qty"] = df_merged["oa_qty"].fillna(0)
        df_merged["oa_value"] = df_merged["oa_value"].fillna(0)
        
        # Apply filter condition: (p.pi_value - IFNULL(oa.oa_value, 0)) > 0
        df_merged = df_merged[(df_merged["pi_value"] - df_merged["oa_value"]) > 0]
        
        # Date formatting for pi_month ("%b'%y")
        def format_date(dt_str):
            if not dt_str: return ""
            try:
                dt = pd.to_datetime(dt_str)
                return dt.strftime("%b'%y")
            except:
                return str(dt_str)
                
        df_merged["pi_month"] = df_merged["pi_date"].apply(format_date)
        
        # Calculate derived metrics
        df_merged["carry_over_qty"] = df_merged["pi_qty"] - df_merged["oa_qty"]
        
        # Rounding logic
        df_merged["pi_value"] = df_merged["pi_value"].round(2)
        df_merged["oa_value"] = df_merged["oa_value"].round(2)
        df_merged["carry_over_value"] = (df_merged["pi_value"] - df_merged["oa_value"]).clip(lower=0).round(2)
        
        # Select and rename final columns
        final_cols = [
            "pi", "pi_month", "company", "pi_qty", "oa_qty", 
            "carry_over_qty", "pi_value", "oa_value", "carry_over_value"
        ]
        
        df_final = df_merged[final_cols].copy()
        df_final.rename(columns={
            "pi": "PI",
            "pi_month": "PI Month",
            "company": "Company",
            "pi_qty": "PI Qty",
            "oa_qty": "OA Qty",
            "carry_over_qty": "Carry Over Qty",
            "pi_value": "PI Value",
            "oa_value": "OA Value",
            "carry_over_value": "Carry Over Value"
        }, inplace=True)
    else:
        df_final = pd.DataFrame()

    # ----------------- 4. GOOGLE SHEETS SYNC -----------------
    if not df_final.empty:
        try:
            log.info(f"🚀 Syncing {len(df_final)} rows to Google Sheets...")
            
            creds = Credentials.from_service_account_file(
                'Credentials.json', 
                scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            )
            client = gspread.authorize(creds)
            
            # Using same document key as packing_details.py, but specific worksheet
            try:
                ws = client.open_by_key(SHEET_KEY).worksheet('pi_waiting')
            except gspread.exceptions.WorksheetNotFound:
                log.info("Worksheet 'pi_waiting' not found, creating it...")
                sh = client.open_by_key(SHEET_KEY)
                ws = sh.add_worksheet(title='pi_waiting', rows=str(len(df_final)+100), cols="20")
            
            ws.batch_clear(["A:I"])
            set_with_dataframe(ws, df_final)
            
            ts = datetime.now(pytz.timezone('Asia/Dhaka')).strftime("%Y-%m-%d %H:%M:%S")
            # Using column K for timestamp sync as our columns are A-I
            ws.update(values=[[f"Sync: {ts}"]], range_name="K1")
            print(f"✅ Success at {ts}")
            
        except Exception as e:
            log.error(f"❌ Sheets Sync Error: {e}")
    else:
        log.warning("⚠️ No data found to sync.")

if __name__ == "__main__":
    main()
