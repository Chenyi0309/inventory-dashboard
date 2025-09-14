# gsheet.py
# -*- coding: utf-8 -*-
import os
import json
import time
import random
from functools import lru_cache
from typing import List, Dict, Tuple

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

try:
    # 有的环境会有 googleapiclient 的 HttpError
    from googleapiclient.errors import HttpError  # type: ignore
except Exception:  # pragma: no cover
    HttpError = None  # 兼容不存在的情况

try:
    # gspread 自己的 APIError
    from gspread.exceptions import APIError  # type: ignore
except Exception:  # pragma: no cover
    APIError = None


# ======== 常量 ========
SHEET_URL_ENV = "INVENTORY_SHEET_URL"   # .streamlit/secrets 或环境变量里配置的表格 URL
TARGET_WS_TITLE = "购入/剩余"             # 目标工作表（tab）名

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


# ======== 写入：指数退避重试封装 ========
def _is_429(err: Exception) -> bool:
    """
    尽量稳健地识别“配额/限流”错误（429），兼容不同异常类型/字段。
    """
    # googleapiclient.errors.HttpError
    if HttpError and isinstance(err, HttpError):
        try:
            status = getattr(getattr(err, "resp", None), "status", None)
            if str(status) == "429":
                return True
        except Exception:
            pass

    # gspread.exceptions.APIError
    if APIError and isinstance(err, APIError):
        try:
            status = getattr(getattr(err, "response", None), "status_code", None)
            if str(status) == "429":
                return True
        except Exception:
            pass

    # 兜底：看错误串
    s = str(err).lower()
    if "429" in s or "ratelimit" in s or "quota" in s:
        return True
    return False


def _retry(operation, *, max_retries: int = 5, base_delay: float = 1.0):
    """
    通用重试：遇到 429 则指数退避 + 抖动，其余异常直接抛出。
    """
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
    # 多次 429 仍失败
    raise RuntimeError("Google Sheets 限流(429)，多次重试仍失败") from last


# ======== 写入：单行 & 批量 ========
def append_record(record: Dict) -> Tuple[int, dict]:
    """
    追加一行到『购入/剩余』。返回 (估算行号, gspread响应对象)。
    为减少读请求次数，此函数不再每次读取表头，而是用 _header_cached()。
    """
    ws = _get_ws()
    header = _header_cached()  # 使用缓存的表头
    row = [record.get(col, "") for col in header]

    # 带重试的单行追加
    resp = _retry(lambda: ws.append_row(row, value_input_option="USER_ENTERED"))

    # 写完清缓存（保证后续读到最新数据）
    bust_cache()

    # gspread 对 append_row 不返回新行号，这里返回当前 row_count 的近似值即可
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

    rows = [[r.get(col, "") for col in header] for r in records]

    # 一次性写入（带重试）
    resp = _retry(lambda: ws.append_rows(rows, value_input_option="USER_ENTERED"))

    # 写完清缓存
    bust_cache()
    return resp


# ======== 可选：简易诊断写入（不影响统计） ========
def try_write_probe() -> bool:
    """
    诊断用的小写入（写一条“__probe__”，立即删除）。用于判断权限/配额是否允许写。
    注意：本函数会产生两次写请求（append + delete）。
    """
    ws = _get_ws()
    header = _header_cached()

    # 构造与表头等长的行，第一列放一个标记便于在表里辨识
    row = ["__probe__"] + [""] * (len(header) - 1)

    _retry(lambda: ws.append_row(row, value_input_option="USER_ENTERED"))
    bust_cache()

    # 尝试删除最后一行（不是强制的，有些情况下 row_count 与已用行不完全一致）
    try:
        _retry(lambda: ws.delete_rows(ws.row_count))
    except Exception:
        pass

    bust_cache()
    return True
