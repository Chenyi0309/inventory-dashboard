# gsheet.py
import os
import json
import time
from functools import lru_cache
from typing import List, Dict, Tuple

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials


# ======== 常量 ========
SHEET_URL_ENV = "INVENTORY_SHEET_URL"         # .streamlit/secrets 或环境变量里配置的表格 URL
TARGET_WS_TITLE = "购入/剩余"                     # 目标工作表（tab）名

# Sheets/Drive 权限作用域
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ======== 基础工具 ========
def _get_creds():
    """从 app.py 写出的 service_account.json 读取凭证"""
    with open("service_account.json", "r") as f:
        data = json.load(f)
    return Credentials.from_service_account_info(data, scopes=SCOPES)


def _get_client():
    return gspread.authorize(_get_creds())


def _open_sheet():
    url = os.getenv(SHEET_URL_ENV) or os.environ.get(SHEET_URL_ENV)
    if not url:
        raise RuntimeError(f"{SHEET_URL_ENV} 未配置")
    gc = _get_client()
    return gc.open_by_url(url)


def _get_ws():
    """获取目标 worksheet 对象"""
    sh = _open_sheet()
    try:
        ws = sh.worksheet(TARGET_WS_TITLE)
    except gspread.WorksheetNotFound:
        raise RuntimeError(f"找不到工作表『{TARGET_WS_TITLE}』，请在文件中创建同名工作表（tab）")
    return ws


# ======== 只读诊断工具（可在 app 的自检面板调用） ========
def debug_list_sheets() -> List[str]:
    sh = _open_sheet()
    return [ws.title for ws in sh.worksheets()]


def debug_service_email() -> str:
    """方便在页面显示当前使用的 service account 邮箱"""
    return json.load(open("service_account.json"))["client_email"]


# ======== 缓存：表头 & 读记录 ========
@lru_cache(maxsize=1)
def _header_cached() -> List[str]:
    """缓存表头（首行）。减少每次写入时对表头的重复读取。"""
    ws = _get_ws()
    header = ws.row_values(1)
    if not header:
        raise RuntimeError("目标工作表首行(header)为空，请确认首行是表头")
    return header


def _clear_header_cache():
    try:
        _header_cached.cache_clear()
    except Exception:
        pass


@lru_cache(maxsize=1)
def read_records_cached() -> pd.DataFrame:
    """读明细（带缓存）。注意：调用 bust_cache() 可清掉缓存。"""
    return read_records()


def bust_cache():
    """清除所有 read 侧缓存（表头 + 明细）"""
    try:
        read_records_cached.cache_clear()
    except Exception:
        pass
    _clear_header_cache()


def read_records() -> pd.DataFrame:
    """
    读取『购入/剩余』全部数据。尽量少调（已在 read_records_cached 中加缓存）。
    """
    ws = _get_ws()
    data = ws.get_all_records()  # 以首行作为 header
    df = pd.DataFrame(data)
    return df


# 如果你后面要接“库存产品”主数据，可在此实现
def read_catalog() -> pd.DataFrame:
    return pd.DataFrame()


@lru_cache(maxsize=1)
def read_catalog_cached() -> pd.DataFrame:
    return read_catalog()


# ======== 写入：单行 & 批量 ========
def append_record(record: Dict) -> Tuple[int, dict]:
    """
    追加一行到『购入/剩余』。返回 (估算行号, gspread响应对象)。
    为减少读请求次数，此函数不再每次读取表头，而是用 _header_cached()。
    """
    ws = _get_ws()
    header = _header_cached()  # 使用缓存的表头
    row = [record.get(col, "") for col in header]

    # 追加单行（用户输入格式）
    resp = ws.append_row(row, value_input_option="USER_ENTERED")

    # 写完清缓存（保证后续读到最新数据）
    bust_cache()

    # gspread 对 append_row 不返回新行号，这里返回当前 used_range 的近似值即可
    # 如果需要精准行号，可以额外读取最后一行比对；为了节省配额，这里选择“近似返回”
    return (ws.row_count, resp)


def append_records_bulk(records: List[Dict]) -> dict:
    """
    批量追加多行，能显著降低“写请求”的调用次数。
    records: [{col:value, ...}, ...]
    """
    if not records:
        return {}

    ws = _get_ws()
    header = _header_cached()  # 使用缓存的表头

    rows = []
    for r in records:
        rows.append([r.get(col, "") for col in header])

    # 一次性写入
    resp = ws.append_rows(rows, value_input_option="USER_ENTERED")

    # 写完清缓存
    bust_cache()
    return resp


# ======== 可选：简易诊断写入（不影响统计） ========
def try_write_probe() -> bool:
    """
    诊断用的小写入（写一条“__probe__”，立即删除）。用于判断权限/配额是否允许写。
    注意：本函数会产生两次写请求（append + clear）。
    """
    ws = _get_ws()
    header = _header_cached()
    # 构造与表头等长的行，第一列放日期方便你在表里辨识
    row = ["__probe__"] + [""] * (len(header) - 1)
    ws.append_row(row, value_input_option="USER_ENTERED")
    bust_cache()

    # 马上清除最后一行，避免污染数据
    last_row_index = ws.row_count
    try:
        ws.delete_rows(last_row_index)
    except Exception:
        # 某些情况下 row_count 不是实际“已用行”，这里忽略异常即可
        pass
    bust_cache()
    return True
