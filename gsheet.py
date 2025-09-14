# gsheet.py
# -*- coding: utf-8 -*-
import os
import json
import time
import random
from functools import lru_cache
from typing import Any, Optional
from typing import List, Dict, Tuple, Any

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

# 可选依赖：在某些环境不存在，做兼容
try:
    from googleapiclient.errors import HttpError  # type: ignore
except Exception:  # pragma: no cover
    HttpError = None

try:
    from gspread.exceptions import APIError  # type: ignore
except Exception:  # pragma: no cover
    APIError = None


# ======== 常量 ========
SHEET_URL_ENV = "INVENTORY_SHEET_URL"   # .streamlit/secrets 或环境变量里的表格 URL
TARGET_WS_TITLE = "购入/剩余"             # 目标工作表（tab）名

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


# ======== 只读诊断工具（用于自检面板） ========
def debug_list_sheets() -> List[str]:
    sh = _open_sheet()
    return [ws.title for ws in sh.worksheets()]


def debug_service_email() -> str:
    return json.load(open("service_account.json"))["client_email"]


# ======== 缓存：表头 & 读记录 ========
@lru_cache(maxsize=1)
def _header_cached() -> List[str]:
    """缓存表头（首行）。"""
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
    return read_records()


def bust_cache():
    """清除所有 read 侧缓存（表头 + 明细）"""
    try:
        read_records_cached.cache_clear()
    except Exception:
        pass
    _clear_header_cache()


def _trim_trailing_empty_rows(rows: List[List[str]]) -> List[List[str]]:
    # 去掉“整行全空”的尾巴
    while rows and all((c == "" for c in rows[-1])):
        rows.pop()
    return rows


def read_records() -> pd.DataFrame:
    """
    读取『购入/剩余』全部数据（更稳健：读取所有值并手动截尾空行）。
    """
    ws = _get_ws()
    vals = ws.get_all_values()  # 含首行
    if not vals:
        return pd.DataFrame()

    header = [h or "" for h in vals[0]]
    body = vals[1:]
    body = _trim_trailing_empty_rows(body)

    if not body:
        return pd.DataFrame(columns=header)

    n = len(header)
    # 行长不足则右侧补空，超出则截断到表头长度
    norm_body = [ (row + [""] * (n - len(row)))[:n] for row in body ]
    df = pd.DataFrame(norm_body, columns=header)
    return df


# 如果后续有“库存产品”主数据，可在此实现
def read_catalog() -> pd.DataFrame:
    return pd.DataFrame()


@lru_cache(maxsize=1)
def read_catalog_cached() -> pd.DataFrame:
    return read_catalog()


# ======== 写入：指数退避重试封装 ========
def _is_429(err: Exception) -> bool:
    """识别 429（配额/限流）"""
    if HttpError and isinstance(err, HttpError):
        try:
            status = getattr(getattr(err, "resp", None), "status", None)
            if str(status) == "429":
                return True
        except Exception:
            pass

    if APIError and isinstance(err, APIError):
        try:
            status = getattr(getattr(err, "response", None), "status_code", None)
            if str(status) == "429":
                return True
        except Exception:
            pass

    s = str(err).lower()
    return ("429" in s) or ("ratelimit" in s) or ("quota" in s)


def _retry(operation, *, max_retries: int = 5, base_delay: float = 1.0):
    delay = base_delay
    last = None
    for _ in range(max_retries):
        try:
            return operation()
        except Exception as e:  # noqa
            last = e
            if _is_429(e):
                time.sleep(delay + random.uniform(0, 0.25))
                delay *= 2
                continue
            raise
    raise RuntimeError("Google Sheets 限流(429)，多次重试仍失败") from last


def _norm_cell(v: Any) -> Any:
    """将 None/NaN 统一转为空串，避免写入被丢行。"""
    try:
        if v is None:
            return ""
        if isinstance(v, float) and pd.isna(v):
            return ""
    except Exception:
        pass
    return v


# ======== 写入：单行 & 批量 ========
def append_record(record: Dict[str, Any]) -> Tuple[int, dict]:
    ws = _get_ws()
    header = _header_cached()
    row = [_norm_cell(record.get(col, "")) for col in header]

    resp = _retry(lambda: ws.append_row(
        row,
        value_input_option="USER_ENTERED",
        insert_data_option="INSERT_ROWS",
        table_range="A1",
        include_values_in_response=True,   # <<< 新增
    ))

    bust_cache()
    return (ws.row_count, resp)


def append_records_bulk(records: List[Dict[str, Any]]) -> dict:
    if not records:
        return {}

    ws = _get_ws()
    header = _header_cached()
    rows = [[_norm_cell(r.get(col, "")) for col in header] for r in records]

    resp = _retry(lambda: ws.append_rows(
        rows,
        value_input_option="USER_ENTERED",
        insert_data_option="INSERT_ROWS",
        table_range="A1",
        include_values_in_response=True,   # <<< 新增
    ))

    bust_cache()
    return resp

# 追加一个工具：把 API 返回的 A1 区间解析出起止行号
def parse_updated_range_rows(resp: dict) -> Optional[Tuple[int, int]]:
    try:
        a1 = resp.get("updates", {}).get("updatedRange", "")  # 例如 "购入/剩余!A249:I250"
        if "!" in a1:
            _, rng = a1.split("!", 1)
        else:
            rng = a1
        start, end = rng.split(":")
        import re
        s = int(re.findall(r"\d+", start)[0])
        e = int(re.findall(r"\d+", end)[0])
        return s, e
    except Exception:
        return None


# 再加一个 tail 调试：返回末尾若干行（含行号）
def tail_rows(n: int = 10) -> pd.DataFrame:
    ws = _get_ws()
    vals = ws.get_all_values()
    if not vals:
        return pd.DataFrame()
    header = vals[0]
    body = vals[1:]
    # 去掉尾部全空
    while body and all(c == "" for c in body[-1]):
        body.pop()
    if not body:
        return pd.DataFrame(columns=["__row__"] + header)
    # 末尾 n 行（加上真实行号）
    start_row = max(2, len(body) - n + 2)  # 首行是1，数据从第2行开始
    chunk = body[-n:]
    out = pd.DataFrame(chunk, columns=header)
    out.insert(0, "__row__", range(start_row, start_row + len(out)))
    return out

# ======== 诊断写入（不影响统计） ========
def try_write_probe() -> bool:
    """
    诊断用的小写入（写一条“__probe__”，随后尝试删除最后一行）。
    """
    ws = _get_ws()
    header = _header_cached()
    row = ["__probe__"] + [""] * (len(header) - 1)

    _retry(lambda: ws.append_row(
        row,
        value_input_option="USER_ENTERED",
        insert_data_option="INSERT_ROWS",
        table_range="A1",
    ))
    bust_cache()

    # 尝试删除最后一行（若失败忽略）
    try:
        _retry(lambda: ws.delete_rows(ws.row_count))
    except Exception:
        pass

    bust_cache()
    return True


