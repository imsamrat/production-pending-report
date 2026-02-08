import os
import requests
import json
import xmlrpc.client
from datetime import datetime, timedelta, date
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from gspread_dataframe import set_with_dataframe
import pytz
from dotenv import load_dotenv
import io
import time
import argparse
import logging
import sys

# Setup logging
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
log = logging.getLogger()

load_dotenv()

# ========= CONFIG ==========
ODOO_URL = os.getenv("ODOO_URL").rstrip('/')
DB = os.getenv("ODOO_DB")
USERNAME = os.getenv("ODOO_USERNAME")
PASSWORD = os.getenv("ODOO_PASSWORD")
API_KEY = os.getenv("ODOO_API_KEY")

MODEL = "mrp.report.custom"
REPORT_BUTTON_METHOD = "action_generate_xlsx_report"
REPORT_TYPE = "packing_details"

# --------- Read args or default ---------
parser = argparse.ArgumentParser()
parser.add_argument("--from_date", type=str, default=None)
parser.add_argument("--to_date", type=str, default=None)
args = parser.parse_args()

_now = datetime.now()
FROM_DATE = args.from_date if args.from_date else "2026-02-01 00:00:00"
TO_DATE = args.to_date if args.to_date else _now.strftime("%Y-%m-%d %H:%M:%S")

log.info(f"Using FROM_DATE={FROM_DATE}, TO_DATE={TO_DATE}")

COMPANIES = {1: "Zipper", 3: "Metal Trims"}

# ----------------- AUTH (XML-RPC) -----------------
log.info("🔐 Authenticating...")
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(DB, USERNAME, API_KEY or PASSWORD, {})
if not uid:
    log.error("❌ Authentication failed.")
    sys.exit(1)
log.info(f"✅ Authenticated, UID={uid}")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# ----------------- FETCHING VIA XML-RPC (operation.details) -----------------
log.info("🔄 Fetching data from Odoo (via operation.details)...")

formatted_data = []

for cid, cname in COMPANIES.items():
    print(f"🔹 Processing {cname}...")
    
    # Selection of fields as per your template image
    domain = [
        ("pack_qty", ">", 0),
        ("action_date", ">=", FROM_DATE),
        ("action_date", "<=", TO_DATE),
        ("company_id", "=", cid)
    ]
    
    fields = [
        "write_date",         # Last Updated on
        "action_date",        # Action Date
        "company_id",         # Company
        "oa_id",              # OA
        "partner_id",         # Customer
        "customer_group",     # Customer Group
        "buyer_name",         # Buyer
        "buyer_group",        # Buyer Group
        "finish",             # Finish
        "shade_name",         # Full Shade
        "sizcommon",          # Size
        "fg_categ_type",      # Item
        "product_template_id",# Product/Name
        "slidercodesfg",      # Slider Code
        "actual_qty",         # Qty
        "pack_qty",           # Pack Qty
        "price_unit",         # Final Price
        "sales_person",       # Sales Person
        "team_id",            # Team
        "invoice_line_id",    # Line/Customer Invoice
    ]

    records = models.execute_kw(DB, uid, API_KEY or PASSWORD, "operation.details", 'search_read', [domain], {'fields': fields})
    print(f"✅ {len(records)} records found.")

    def safe_name(val):
        return val[1] if isinstance(val, list) and len(val) > 1 else (val or "")

    def bd_time(dt_str, date_only=False):
        if not dt_str: return ""
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S") + timedelta(hours=6)
            return dt.strftime("%d/%m/%Y") if date_only else dt.strftime("%d/%m/%Y %H:%M:%S")
        except: return dt_str

    for r in records:
        formatted_data.append({
            "Last Updated on": bd_time(r.get("write_date")),
            "Action Date": bd_time(r.get("action_date"), True),
            "Company": safe_name(r.get("company_id")),
            "OA": safe_name(r.get("oa_id")),
            "Customer": safe_name(r.get("partner_id")),
            "Customer Group": r.get("customer_group") or "",
            "Buyer": r.get("buyer_name") or "",
            "Buyer Group": r.get("buyer_group") or "",
            "Finish": r.get("finish") or "",
            "Full Shade": r.get("shade_name") or "",
            "Size": r.get("sizcommon") or "",
            "Item": r.get("fg_categ_type") or "",
            "Product/Name": safe_name(r.get("product_template_id")),
            "Slider Code": r.get("slidercodesfg") or "",
            "Qty": r.get("actual_qty") or 0.0,
            "Pack Qty": r.get("pack_qty") or 0.0,
            "Final Price": r.get("price_unit") or 0.0,
            "Sales Person": safe_name(r.get("sales_person")),
            "Team": safe_name(r.get("team_id")),
            "Line/Customer Invoice": safe_name(r.get("invoice_line_id")),
        })

df = pd.DataFrame(formatted_data)

# ----------------- GOOGLE SHEETS SYNC -----------------
if not df.empty:
    try:
        log.info(f"🚀 Syncing {len(df)} rows to Google Sheets...")
        
        creds = Credentials.from_service_account_file('Credentials.json', scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
        client = gspread.authorize(creds)
        
        ws = client.open_by_key('1F7epdshmtSM8iPmSgYTY9l7Hwsz4X0uuvShVi87s75o').worksheet('packege_details')
        ws.clear()
        set_with_dataframe(ws, df)
        
        ts = datetime.now(pytz.timezone('Asia/Dhaka')).strftime("%Y-%m-%d %H:%M:%S")
        ws.update("U1", [[f"Sync: {ts}"]])
        print(f"✅ Success at {ts}")
        
    except Exception as e:
        log.error(f"❌ Sheets Sync Error: {e}")
else:
    log.warning("⚠️ No data found.")
