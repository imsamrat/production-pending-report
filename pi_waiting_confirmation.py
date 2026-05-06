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
import re

# Setup logging
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
log = logging.getLogger()

load_dotenv()

# ========= CONFIG ==========
ODOO_URL = os.getenv("ODOO_URL", "").rstrip("/")
DB = os.getenv("ODOO_DB", "")
USERNAME = os.getenv("ODOO_USERNAME", "")
PASSWORD = os.getenv("ODOO_PASSWORD", "")
API_KEY = os.getenv("ODOO_API_KEY", "")

# Google Sheets Configuration
SHEET_KEY = "1F7epdshmtSM8iPmSgYTY9l7Hwsz4X0uuvShVi87s75o"

parser = argparse.ArgumentParser()
# PI totals per exact PI key (keeps PI/OA/CarryOver columns correct)
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


def _display_text(val) -> str:
    text = safe_name(val)
    return str(text).strip() if text is not None else ""


def _norm_text(val, *, upper: bool = False) -> str:
    """Normalize key text coming from Odoo (handles many2one/list, trims spaces)."""
    text = safe_name(val)
    text = str(text).strip() if text is not None else ""
    return text.upper() if upper else text


def _norm_pi_ref(val) -> str:
    """Normalize PI reference for matching between PI (sale) and OA orders.

    Tries to extract a token like 'S506373' from strings like:
    - 'S506373'
    - 'S506373-1'
    - 'S506373 / something'
    - 'PI: S506373'
    Falls back to trimmed, uppercased text.
    """
    text = _norm_text(val, upper=True)
    if not text:
        return ""

    m = re.search(r"\bS\d+\b", text)
    if m:
        return m.group(0)
    return text


def _extract_id(m2o_val):
    if isinstance(m2o_val, list) and m2o_val:
        return m2o_val[0]
    if isinstance(m2o_val, int):
        return m2o_val
    return None


def fetch_lines_with_fg_categ_type(
    models, uid, password, order_ids, *, line_fields=None
):
    """Fetch sale.order.line rows plus product template FG category type.

    Tries `product_template_id` first; falls back to `product_id -> product_tmpl_id`.
    Returns a list of dicts with at least: order_id, product_uom_qty, price_subtotal, fg_categ_type.
    """
    if not order_ids:
        return []

    base_fields = line_fields or [
        "order_id",
        "product_uom_qty",
        "price_subtotal",
        "finish_ref",
    ]
    domain = [("order_id", "in", order_ids)]

    excluded_keywords = ("MOULD", "DOCUMENTATION CHARGE", "OTHERS CHARGE")

    def is_excluded_template_name(name: str) -> bool:
        if not name:
            return False
        upper = str(name).strip().upper()
        return any(k in upper for k in excluded_keywords)

    # Attempt 1: direct product_template_id on sale.order.line
    try:
        fields = list(dict.fromkeys(base_fields + ["product_template_id"]))
        raw_lines = models.execute_kw(
            DB,
            uid,
            password,
            "sale.order.line",
            "search_read",
            [domain],
            {"fields": fields},
        )

        lines = []
        for l in raw_lines:
            template_name = safe_name(l.get("product_template_id"))
            if is_excluded_template_name(template_name):
                continue
            lines.append(l)

        template_ids = {
            _extract_id(l.get("product_template_id"))
            for l in lines
            if _extract_id(l.get("product_template_id"))
        }
        tmpl_map = {}
        if template_ids:
            tmpls = models.execute_kw(
                DB,
                uid,
                password,
                "product.template",
                "read",
                [list(template_ids)],
                {"fields": ["fg_categ_type"]},
            )
            tmpl_map = {t["id"]: safe_name(t.get("fg_categ_type")) for t in tmpls}

        for l in lines:
            tmpl_id = _extract_id(l.get("product_template_id"))
            l["fg_categ_type"] = tmpl_map.get(tmpl_id, "")
        return lines
    except Exception as e:
        # Attempt 2: product_id -> product_tmpl_id mapping
        err = str(e)
        if "product_template_id" not in err:
            raise

    fields = list(dict.fromkeys(base_fields + ["product_id"]))
    lines = models.execute_kw(
        DB,
        uid,
        password,
        "sale.order.line",
        "search_read",
        [domain],
        {"fields": fields},
    )

    product_ids = {
        _extract_id(l.get("product_id"))
        for l in lines
        if _extract_id(l.get("product_id"))
    }
    prod_to_tmpl = {}
    tmpl_ids = set()
    if product_ids:
        prods = models.execute_kw(
            DB,
            uid,
            password,
            "product.product",
            "read",
            [list(product_ids)],
            {"fields": ["product_tmpl_id"]},
        )
        for p in prods:
            pid = p.get("id")
            tid = _extract_id(p.get("product_tmpl_id"))
            if pid and tid:
                prod_to_tmpl[pid] = tid
                tmpl_ids.add(tid)

    tmpl_map = {}
    tmpl_name_map = {}
    if tmpl_ids:
        tmpls = models.execute_kw(
            DB,
            uid,
            password,
            "product.template",
            "read",
            [list(tmpl_ids)],
            {"fields": ["fg_categ_type", "name"]},
        )
        tmpl_map = {t["id"]: safe_name(t.get("fg_categ_type")) for t in tmpls}
        tmpl_name_map = {t["id"]: (t.get("name") or "") for t in tmpls}

    filtered_lines = []
    for l in lines:
        pid = _extract_id(l.get("product_id"))
        tid = prod_to_tmpl.get(pid)
        template_name = tmpl_name_map.get(tid, "")
        if is_excluded_template_name(template_name):
            continue
        l["fg_categ_type"] = tmpl_map.get(tid, "")
        filtered_lines.append(l)
    return filtered_lines


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
        pi_orders = models.execute_kw(
            DB,
            uid,
            API_KEY or PASSWORD,
            "sale.order",
            "search_read",
            [pi_order_domain],
            {"fields": pi_order_fields},
        )
        log.info(f"✅ {len(pi_orders)} PI orders found.")

        pi_data = []
        if pi_orders:
            pi_order_dict = {o["id"]: o for o in pi_orders}
            pi_order_ids = list(pi_order_dict.keys())

            # Fetch corresponding lines
            pi_line_fields = [
                "order_id",
                "product_uom_qty",
                "price_subtotal",
                "finish_ref",
            ]
            pi_lines = fetch_lines_with_fg_categ_type(
                models,
                uid,
                API_KEY or PASSWORD,
                pi_order_ids,
                line_fields=pi_line_fields,
            )

            for line in pi_lines:
                order_id = (
                    line.get("order_id")[0]
                    if isinstance(line.get("order_id"), list)
                    else line.get("order_id")
                )
                order = pi_order_dict.get(order_id, {})
                pi_data.append(
                    {
                        "pi": _display_text(
                            order.get("name") if "name" in order else ""
                        ),
                        "pi_key": _norm_text(
                            order.get("name") if "name" in order else "",
                            upper=True,
                        ),
                        "pi_base": _norm_pi_ref(
                            order.get("name") if "name" in order else ""
                        ),
                        "pi_date": order.get("pi_date") or "",
                        "company": _norm_text(order.get("company_id")),
                        "fg_categ_type": _norm_text(line.get("fg_categ_type")),
                        "finish_ref": _norm_text(line.get("finish_ref")),
                        "pi_qty": float(line.get("product_uom_qty") or 0.0),
                        "pi_value": float(line.get("price_subtotal") or 0.0),
                    }
                )
    except Exception as e:
        log.error(f"❌ Failed to fetch PI records: {e}")
        pi_data = []

    df_pi_raw = pd.DataFrame(pi_data)
    if df_pi_raw.empty:
        df_pi_raw = pd.DataFrame(
            columns=[
                "pi",
                "pi_key",
                "pi_base",
                "pi_date",
                "company",
                "fg_categ_type",
                "finish_ref",
                "pi_qty",
                "pi_value",
            ]
        )

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
        oa_orders = models.execute_kw(
            DB,
            uid,
            API_KEY or PASSWORD,
            "sale.order",
            "search_read",
            [oa_order_domain],
            {"fields": oa_order_fields},
        )
        log.info(f"✅ {len(oa_orders)} OA orders found.")

        oa_data = []
        if oa_orders:
            oa_order_dict = {o["id"]: o for o in oa_orders}
            oa_order_ids = list(oa_order_dict.keys())

            # Fetch corresponding lines
            oa_line_fields = [
                "order_id",
                "product_uom_qty",
                "price_subtotal",
                "finish_ref",
            ]
            oa_lines = fetch_lines_with_fg_categ_type(
                models,
                uid,
                API_KEY or PASSWORD,
                oa_order_ids,
                line_fields=oa_line_fields,
            )

            for line in oa_lines:
                order_id = (
                    line.get("order_id")[0]
                    if isinstance(line.get("order_id"), list)
                    else line.get("order_id")
                )
                order = oa_order_dict.get(order_id, {})
                oa_data.append(
                    {
                        "pi": _display_text(order.get("order_ref")),
                        "pi_key": _norm_text(order.get("order_ref"), upper=True),
                        "pi_base": _norm_pi_ref(order.get("order_ref")),
                        "company": _norm_text(order.get("company_id")),
                        "fg_categ_type": _norm_text(line.get("fg_categ_type")),
                        "finish_ref": _norm_text(line.get("finish_ref")),
                        "oa_qty": float(line.get("product_uom_qty") or 0.0),
                        "oa_value": float(line.get("price_subtotal") or 0.0),
                    }
                )
    except Exception as e:
        log.error(f"❌ Failed to fetch OA records: {e}")
        oa_data = []

    df_oa_raw = pd.DataFrame(oa_data)
    if df_oa_raw.empty:
        df_oa_raw = pd.DataFrame(
            columns=[
                "pi",
                "pi_key",
                "pi_base",
                "company",
                "fg_categ_type",
                "finish_ref",
                "oa_qty",
                "oa_value",
            ]
        )

    # ----------------- 3. PROCESS DATA (Pandas matching SQL) -----------------
    def _join_unique(series: pd.Series) -> str:
        vals = [str(v).strip() for v in series.dropna().tolist() if str(v).strip()]
        if not vals:
            return ""
        return ", ".join(sorted(set(vals)))

    # PI totals (same as your previous working logic), keyed by exact PI text
    if not df_pi_raw.empty:
        pi_group_keys = ["pi_key", "pi_date", "company"]

        df_pi_totals = df_pi_raw.groupby(pi_group_keys, as_index=False).agg(
            pi=("pi", "first"),
            pi_base=("pi_base", "first"),
            pi_qty=("pi_qty", "sum"),
            pi_value=("pi_value", "sum"),
        )

        df_pi_meta = df_pi_raw.groupby(pi_group_keys, as_index=False).agg(
            fg_categ_type=("fg_categ_type", _join_unique),
            finish_ref=("finish_ref", _join_unique),
        )

        df_pi = pd.merge(df_pi_totals, df_pi_meta, on=pi_group_keys, how="left")
    else:
        df_pi = df_pi_raw.copy()

    # OA totals: merge by exact PI key first, fallback by base PI code
    if not df_oa_raw.empty:
        df_oa_key = df_oa_raw.groupby(["pi_key"], as_index=False).agg(
            oa_qty=("oa_qty", "sum"),
            oa_value=("oa_value", "sum"),
        )
        df_oa_base = df_oa_raw.groupby(["pi_base"], as_index=False).agg(
            oa_qty=("oa_qty", "sum"),
            oa_value=("oa_value", "sum"),
        )
    else:
        df_oa_key = df_oa_raw.copy()
        df_oa_base = df_oa_raw.copy()

    if not df_pi.empty:
        df_merged = pd.merge(df_pi, df_oa_key, on=["pi_key"], how="left")

        if not df_oa_base.empty:
            df_merged = pd.merge(
                df_merged,
                df_oa_base,
                on=["pi_base"],
                how="left",
                suffixes=("", "_fb"),
            )
            df_merged["oa_qty"] = df_merged["oa_qty"].fillna(df_merged["oa_qty_fb"])
            df_merged["oa_value"] = df_merged["oa_value"].fillna(
                df_merged["oa_value_fb"]
            )
            df_merged.drop(columns=["oa_qty_fb", "oa_value_fb"], inplace=True)

        df_merged["oa_qty"] = df_merged["oa_qty"].fillna(0)
        df_merged["oa_value"] = df_merged["oa_value"].fillna(0)

        # Apply filter condition: (p.pi_value - IFNULL(oa.oa_value, 0)) > 0
        df_merged = df_merged[(df_merged["pi_value"] - df_merged["oa_value"]) > 0]

        # Date formatting for pi_month ("%b'%y")
        def format_date(dt_str):
            if not dt_str:
                return ""
            try:
                dt = pd.to_datetime(dt_str)
                return dt.strftime("%b'%y")
            except Exception:
                return str(dt_str)

        df_merged["pi_month"] = df_merged["pi_date"].apply(format_date)

        # Calculate derived metrics
        df_merged["carry_over_qty"] = df_merged["pi_qty"] - df_merged["oa_qty"]

        # Rounding logic
        df_merged["pi_value"] = df_merged["pi_value"].round(2)
        df_merged["oa_value"] = df_merged["oa_value"].round(2)
        df_merged["carry_over_value"] = (
            (df_merged["pi_value"] - df_merged["oa_value"]).clip(lower=0).round(2)
        )

        # Select and rename final columns
        final_cols = [
            "pi",
            "pi_month",
            "company",
            "fg_categ_type",
            "finish_ref",
            "pi_qty",
            "oa_qty",
            "carry_over_qty",
            "pi_value",
            "oa_value",
            "carry_over_value",
        ]

        df_final = df_merged[final_cols].copy()
        df_final.rename(
            columns={
                "pi": "PI",
                "pi_month": "PI Month",
                "company": "Company",
                "fg_categ_type": "FG Category",
                "finish_ref": "Finish Ref",
                "pi_qty": "PI Qty",
                "oa_qty": "OA Qty",
                "carry_over_qty": "Carry Over Qty",
                "pi_value": "PI Value",
                "oa_value": "OA Value",
                "carry_over_value": "Carry Over Value",
            },
            inplace=True,
        )
    else:
        df_final = pd.DataFrame()

    # ----------------- 4. GOOGLE SHEETS SYNC -----------------
    if not df_final.empty:
        try:
            log.info(f"🚀 Syncing {len(df_final)} rows to Google Sheets...")

            creds = Credentials.from_service_account_file(
                "Credentials.json",
                scopes=[
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive",
                ],
            )
            client = gspread.authorize(creds)

            # Using same document key as packing_details.py, but specific worksheet
            try:
                ws = client.open_by_key(SHEET_KEY).worksheet("pi_waiting")
            except gspread.exceptions.WorksheetNotFound:
                log.info("Worksheet 'pi_waiting' not found, creating it...")
                sh = client.open_by_key(SHEET_KEY)
                ws = sh.add_worksheet(
                    title="pi_waiting", rows=str(len(df_final) + 100), cols="20"
                )

            ws.batch_clear(["A:K"])
            set_with_dataframe(ws, df_final)

            ts = datetime.now(pytz.timezone("Asia/Dhaka")).strftime("%Y-%m-%d %H:%M:%S")
            # Using column L for timestamp sync as our columns are A-K
            ws.update(values=[[f"Sync: {ts}"]], range_name="L1")
            print(f"✅ Success at {ts}")

        except Exception as e:
            log.error(f"❌ Sheets Sync Error: {e}")
    else:
        log.warning("⚠️ No data found to sync.")


if __name__ == "__main__":
    main()
