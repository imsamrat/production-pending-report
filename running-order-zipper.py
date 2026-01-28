import os
import xmlrpc.client
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ---------------- LOAD ENVIRONMENT VARIABLES ----------------
load_dotenv()
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
ODOO_API_KEY = os.getenv("ODOO_API_KEY")

# ---------------- DYNAMIC DATE FILTER ----------------
_now = datetime.now()
# FROM_DATE = "2025-12-29 00:00:00"

FROM_DATE = (
    (_now - timedelta(days=3))
    .replace(hour=0, minute=0, second=0, microsecond=0)
    .strftime("%Y-%m-%d %H:%M:%S")
)
TO_DATE = _now.strftime("%Y-%m-%d %H:%M:%S")

# ---------------- MODEL CONFIGURATION ----------------
MODEL_NAME = "manufacturing.order"  # <-- Change model here
ORDER_DOMAIN = [
    ("fg_categ_type", "!=", ""),
    ("balance_qty", ">", 0),
    ("oa_total_balance", ">", 0),
    ("oa_id", "!=", False),
    ("state", "not in", ("closed", "cancel", "hold")),
    ("company_id", "=", 1),
    ("sale_order_line.state", "=", "sale")
]

ORDER_FIELDS = [
    "oa_id",
    "date_order",
    "company_id",
    "fg_categ_type",
    "fg_categ_group",
    "product_uom_qty",
    "partner_id",
    "buyer_name",
    "payment_term",
    "sale_order_line",
    "oa_total_balance",
    "balance_qty",
]

# ---------------- ODOO CONNECTION ----------------
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY or ODOO_PASSWORD, {})
if not uid:
    raise Exception("❌ Failed to authenticate with Odoo. Check credentials.")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

# ---------------- FETCH RECORDS ----------------
records = models.execute_kw(
    ODOO_DB,
    uid,
    ODOO_API_KEY or ODOO_PASSWORD,
    MODEL_NAME,
    "search_read",
    [ORDER_DOMAIN],
    {"fields": ORDER_FIELDS},
)

print(f"✅ {len(records)} records fetched from {MODEL_NAME}")

if not records:
    exit("⚠️ No records found for the given filters")


# ---------------- HELPER FUNCTION ----------------
def safe_name(value):
    """Return the name string from [id, name] or value; else empty string for False/None"""
    if isinstance(value, list) and len(value) > 1:
        return value[1]
    elif value:
        return str(value)
    return ""


def format_date(date_value):
    """Format date value to DD/MM/YYYY HH:MM:SS format with IST timezone conversion"""
    if not date_value:
        return ""
    if isinstance(date_value, str):
        try:
            # Parse the date string (assuming it's in UTC) and convert to IST
            parsed_date = datetime.strptime(date_value, "%Y-%m-%d %H:%M:%S")
            # Add 5 hours 30 minutes for IST timezone
            ist_date = parsed_date + timedelta(hours=6, minutes=00)
            # Format to DD/MM/YYYY HH:MM:SS
            return ist_date.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                # Try parsing without time
                parsed_date = datetime.strptime(date_value, "%Y-%m-%d")
                ist_date = parsed_date + timedelta(hours=6, minutes=00)
                return ist_date.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                return str(date_value)
    return str(date_value)


# ---------------- FETCH SALE ORDER DETAILS (PI) ----------------
oa_ids = list(set([rec.get("oa_id")[0] for rec in records if rec.get("oa_id")]))
so_details = {}
if oa_ids:
    print(f"🔄 Fetching PI details for {len(oa_ids)} Sales Orders...")
    try:
        so_records = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_API_KEY or ODOO_PASSWORD,
            "sale.order",
            "read",
            [oa_ids],
            {"fields": ["order_ref", "team_id"]},
        )
        for so in so_records:
            so_details[so["id"]] = {
                "ref": safe_name(so.get("order_ref")),
                "team": safe_name(so.get("team_id")),
            }
    except Exception as e:
        print(f"⚠️ Failed to fetch Sales Order details: {e}")

# ---------------- FETCH SALE ORDER LINE DETAILS (Amount) ----------------
sol_ids = []
for rec in records:
    val = rec.get("sale_order_line")
    if val and isinstance(val, list):
        sol_ids.append(val[0])
        
sol_ids = list(set(sol_ids))
sol_details = {}

if sol_ids:
    print(f"🔄 Fetching Amount details for {len(sol_ids)} Sales Order Lines...")
    try:
        sol_records = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_API_KEY or ODOO_PASSWORD,
            "sale.order.line",
            "read",
            [sol_ids],
            {"fields": ["price_subtotal", "salesman_id", "price_unit", "discount"]},
        )
        for sol in sol_records:
            sol_details[sol["id"]] = {
                "amount": sol.get("price_subtotal") or 0.0,
                "salesperson": safe_name(sol.get("salesman_id")),
                "price_unit": sol.get("price_unit") or 0.0,
                "discount": sol.get("discount") or 0.0,
            }
    except Exception as e:
        print(f"⚠️ Failed to fetch Sale Order Line details: {e}")


# ---------------- FORMAT DATA ----------------
all_data = []
for rec in records:
    oa_id_val = rec.get("oa_id")[0] if rec.get("oa_id") else None
    so_info = so_details.get(oa_id_val, {"ref": ""})
    
    sol_id_val = rec.get("sale_order_line")[0] if rec.get("sale_order_line") else None
    sol_data = sol_details.get(sol_id_val, {})
    amount_val = sol_data.get("amount", 0.0)
    salesperson_val = sol_data.get("salesperson", "")
    price_unit_val = sol_data.get("price_unit", 0.0)
    discount_val = sol_data.get("discount", 0.0)

    # Calculate Balance Qty and Pending Value
    # User requested 'balance_qty' specifically
    balance_qty_val = rec.get("balance_qty") or 0.0
    
    # pending_value = (balance_qty * price) - discount %
    gross_pending_value = balance_qty_val * price_unit_val
    pending_value_val = gross_pending_value * (1 - (discount_val / 100))
    
    all_data.append(
        {
            "oa": safe_name(rec.get("oa_id")),
            "PI": so_info["ref"],
            "oa_date": format_date(rec.get("date_order")),
            "customer": safe_name(rec.get("partner_id")),
            "buyer": safe_name(rec.get("buyer_name")),
            "payment_term": safe_name(rec.get("payment_term")),
            "company": safe_name(rec.get("company_id")),
            "item": safe_name(rec.get("fg_categ_type")),
            "item_group": safe_name(rec.get("fg_categ_group")),
            "salesperson": salesperson_val,
            "team": so_info.get("team", ""),
            "ID": sol_id_val or 0,
            "qty": rec.get("product_uom_qty") or 0,
            "amount": amount_val,
            "balance_qty": balance_qty_val,
            "pending_value": pending_value_val,
        }
    )

# ---------------- EXPORT TO EXCEL ----------------
df = pd.DataFrame(all_data)

# Convert date columns to datetime format
date_columns = ["oa_date"]
for col in date_columns:
    if col in df.columns:
        df[col] = pd.to_datetime(df[col], errors="coerce")

# ---------------- GROUP DATA ----------------
if not df.empty:
    # Fill NaN values for grouping columns to avoid data loss
    df["oa"] = df["oa"].fillna("")
    df["company"] = df["company"].fillna("")

    # Truncate action_date to just the date (YYYY-MM-DD) to merge same-day records
    df["oa_date"] = df["oa_date"].dt.date
    
    # Group by all descriptive columns and sum qty
    # Using as_index=False to keep the grouping columns
    group_cols = [
        "oa_date", 
        "oa", 
        "PI",
        "customer",
        "buyer",
        "payment_term",
        "company", 
        "item", 
        "item_group",
        "salesperson",
        "team",
        "ID",
    ]
    df = df.groupby(group_cols, as_index=False)[["qty", "amount", "balance_qty", "pending_value"]].sum()

    # Sort by oa_date ASC, then oa ASC
    df = df.sort_values(by=["oa_date", "oa"], ascending=[True, True])


# ---------------- GOOGLE SHEETS SYNC ----------------
try:
    import gspread
    from google.oauth2.service_account import Credentials

    print("\n🚀 Starting Google Sheets Sync...")
    
    # Configuration
    GSHEETS_CREDS = 'Credentials.json'
    SPREADSHEET_ID = '1F7epdshmtSM8iPmSgYTY9l7Hwsz4X0uuvShVi87s75o'
    SHEET_NAME = 'Zipper'
    
    # Authenticate
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(GSHEETS_CREDS, scopes=scope)
    client = gspread.authorize(creds)
    
    # Open Sheet
    sheet = client.open_by_key(SPREADSHEET_ID)
    try:
        worksheet = sheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        print(f"⚠️ Worksheet '{SHEET_NAME}' not found. Creating it...")
        worksheet = sheet.add_worksheet(title=SHEET_NAME, rows=1000, cols=20)

    # Prepare Data
    # Replace NaN with empty string for JSON compliance
    df_clean = df.fillna('')
    
    # Convert datetime objects to string for JSON compliance
    for col in date_columns:
        if col in df_clean.columns:
            df_clean[col] = df_clean[col].astype(str)

    # Get headers and values
    # Get headers and values
    # headers = [df_clean.columns.values.tolist()] # No headers needed for appending
    values = df_clean.values.tolist()
    all_data_to_write = values
    
    num_rows = len(all_data_to_write)
    num_cols = len(df_clean.columns) if not df_clean.empty else 0
    
    # Calculate range, e.g., 'A34333:E...'
    # Function to convert col index to letter (0 -> A, 22 -> W)
    def col_to_letter(n):
        string = ""
        while n >= 0:
            string = chr(n % 26 + 65) + string
            n = n // 26 - 1
        return string

    last_col_letter = col_to_letter(num_cols - 1)
    
    # Target row from user request
    START_ROW = 2
    target_range = f"A{START_ROW}:{last_col_letter}{START_ROW + num_rows}"
    
    print(f"Updating range {target_range} (Appended data)...")
    
    # Clear from START_ROW downwards to remove potential old data collision if needed
    # (Optional, but safe if we want to ensure "current month" replaces whatever was there from prev run)
    clear_range = f"A{START_ROW}:{last_col_letter}{worksheet.row_count}"
    worksheet.batch_clear([clear_range])
    
    # Update with new data
    worksheet.update(values=all_data_to_write, range_name=f"A{START_ROW}", value_input_option='USER_ENTERED')
    
    print("✅ Google Sheets update complete!")

except ImportError:
    print("⚠️ gspread library not found. Skipping Google Sheets sync.")
except Exception as e:
    print(f"❌ Google Sheets Sync Error: {e}")