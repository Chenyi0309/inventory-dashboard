# gsheet.py
import os
import json
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
from functools import lru_cache

SHEET_URL_ENV = "INVENTORY_SHEET_URL"
TARGET_WS_TITLE = "购入/剩余"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def _get_creds():
    # service_account.json 由 app.py 启动时从 secrets 写出
    with open("service_account.json", "r") as f:
        data = json.load(f)
    return Credentials.from_service_account_info(data, scopes=SCOPES)

def _get_client():
    return gspread.authorize(_get_creds())

def _open_sheet():
    url = os.getenv(SHEET_URL_ENV) or os.environ.get(SHEET_URL_ENV)  # 兼容
    if not url:
        raise RuntimeError(f"{SHEET_URL_ENV} 未配置")
    gc = _get_client()
    return gc.open_by_url(url)

def _get_ws():
    sh = _open_sheet()
    try:
        ws = sh.worksheet(TARGET_WS_TITLE)
    except gspread.WorksheetNotFound:
        raise RuntimeError(f"找不到工作表『{TARGET_WS_TITLE}』，请在该文件中创建一个同名工作表（tab）")
    return ws

# ============ 自检辅助 ============
def _debug_list_sheets():
    sh = _open_sheet()
    return [ws.title for ws in sh.worksheets()]

def _debug_read_header():
    ws = _get_ws()
    header = ws.row_values(1)
    return header

# ============ 读 ============
@lru_cache(maxsize=1)
def read_records_cached():
    return read_records()

def bust_cache():
    read_records_cached.cache_clear()

def read_records():
    ws = _get_ws()
    data = ws.get_all_records()  # 以首行作为header
    df = pd.DataFrame(data)
    return df

def read_catalog():
    # 如果你有“库存产品”主数据工作表，可以类似读；这里保持占位
    return pd.DataFrame()

def read_catalog_cached():
    return read_catalog()

# ============ 写 ============
# gsheet.py（只贴 append_record 的新版本）
def append_record(record: dict):
    """
    追加一行到『购入/剩余』。按表头映射每列。返回(新行号, API回执)。
    """
    ws = _get_ws()
    header = ws.row_values(1)
    if not header:
        raise RuntimeError("目标工作表首行(header)为空，请确认首行是表头")

    row = [record.get(col, "") for col in header]

    # 这里用 append_row 并要求返回回执
    resp = ws.append_row(row, value_input_option="USER_ENTERED")
    # gspread 对 append_row 通常不返回行号，这里手动用当前已用行数估算：
    # 更稳妥：再读一次最后一行看是否等于我们刚写入的“标记”
    bust_cache()
    return (ws.row_count, resp)

