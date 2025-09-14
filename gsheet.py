# gsheet.py
# -*- coding: utf-8 -*-
import os
import re
import json
import time
import random
from functools import lru_cache
from typing import List, Dict, Tuple, Optional

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

try:
    from googleapiclient.errors import HttpError  # type: ignore
except Exception:  # pragma: no cover
    HttpError = None

try:
    from gspread.exceptions import APIError  # type: ignore
except Exception:  # pragma: no cover
    APIError = None


# ======== 常量 ========
SHEET_URL_ENV = "INVENTORY_SHEET_URL"     # .streamlit/secrets 或环境变量里配置的表格 URL
TARGET_WS_TITLE = "购入/剩余"               # 目标工作表（tab）名

# Sheets/Drive 权限作用域
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# 我们希望对齐到的标准列（与表头第一行一致）
EXPECTED_COLS = [
    "日期 (Date)",
    "食材名称 (Item Name)",
    "分类 (Category)",
    "数量 (Qty)",
    "单位 (Unit)",
    "单价 (Unit Price)",
    "总价 (Total Cost)",
    "状态 (Status)",
    "备注 (Notes)",
]


# ======== 基础工具 ========
def _get_creds():
    """从 app.py 写出的 service_account.json 读取凭证。"""
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
    """获取目标 worksheet 对象。"""
    sh = _open_sheet()
    try:
        ws = sh.worksheet(TARGET_WS_TITLE)
    except gspread.WorksheetNotFound:
        raise RuntimeError(f"找不到工作表『{TARGET_WS_TITLE}』，请在文件中创建同名工作表（tab）")
    return ws


# ======== 诊断工具（自检面板会用到） ========
def debug_list_sheets() -> List[str]:
    sh = _open_sheet()
    return [ws.title for ws in sh.worksheets()]


def debug_service_email() -> str:
    """在页面显示当前使用的 service account 邮箱。"""
    return json.load(open("service_account.json"))["client_email"]


# ======== 读侧缓存 ========
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
    """读明细（带缓存）。"""
    return read_records()


def bust_cache():
    """清除所有 read 侧缓存（表头 + 明细）。"""
    try:
        read_records_cached.cache_clear()
    except Exception:
        pass
    _clear_header_cache()


def read_records() -> pd.DataFrame:
    """读取『购入/剩余』全部数据（尽量少调，App 侧做了缓存）。"""
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


# ======== 规范化与行构造 ========
def _norm_col(s: str) -> str:
    """
    规范化列名：去掉各种不可见空白（NBSP/窄不间断空格等）、统一全角括号为半角、去空格并转小写。
    """
    return (
        str(s)
        .replace("（", "(").replace("）", ")")
        .replace("\u00A0", "")   # NBSP
        .replace("\u2007", "")   # Figure space
        .replace("\u202F", "")   # Narrow no-break space
        .replace(" ", "")
        .strip()
        .lower()
    )


def _clean_cell(v):
    """将 None/NaN 统一为空串，其他原样返回。"""
    try:
        if v is None:
            return ""
        if isinstance(v, float) and (v != v):  # NaN 判断
            return ""
    except Exception:
        pass
    return v


def _rows_from_records(records: List[Dict], header: List[str]) -> List[List]:
    """
    根据“实际表头顺序”构造二维数组。
    表头会做规范化后与 EXPECTED_COLS 做映射，从而避免 NBSP/全角括号导致的错列。
    """
    exp_map = {_norm_col(k): k for k in EXPECTED_COLS}
    rows: List[List] = []

    for r in records:
        row: List = []
        for h in header:
            key = exp_map.get(_norm_col(h))   # 用规范化后的表头去匹配期望列
            val = _clean_cell(r.get(key, "")) if key else ""
            row.append(val)
        rows.append(row)
    return rows


# ======== 写入侧：指数退避重试 ========
def _is_429(err: Exception) -> bool:
    """识别 429（配额/限流）。"""
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
    if "429" in s or "ratelimit" in s or "quota" in s:
        return True
    return False


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


# ======== 写入：单行 & 批量 ========
def append_record(record: Dict) -> Tuple[int, dict]:
    """
    追加一行到『购入/剩余』。返回 (估算行号, gspread响应对象)。
    """
    ws = _get_ws()
    header = _header_cached()
    rows = _rows_from_records([record], header)

    resp = _retry(lambda: ws.append_rows(
        rows,
        value_input_option="USER_ENTERED",
        table_range="A1",
        include_values_in_response=True
    ))

    bust_cache()
    return (ws.row_count, resp)


def append_records_bulk(records: List[Dict]) -> dict:
    """
    批量追加多行，显著降低写请求次数。
    会对列名做规范化匹配，避免“只写进 Notes 列”的情况。
    """
    if not records:
        return {}

    ws = _get_ws()
    header = _header_cached()
    rows = _rows_from_records(records, header)

    resp = _retry(lambda: ws.append_rows(
        rows,
        value_input_option="USER_ENTERED",
        table_range="A1",
        include_values_in_response=True
    ))

    bust_cache()
    return resp


# ======== 诊断写入（不影响统计） ========
def try_write_probe() -> bool:
    """
    诊断用的小写入（写一条“__probe__”，立即删除）。
    """
    ws = _get_ws()
    header = _header_cached()
    # 构造与表头等长的行，第一列放一个标记便于在表里辨识
    row = [["__probe__"] + [""] * (len(header) - 1)]

    _retry(lambda: ws.append_rows(
        row,
        value_input_option="USER_ENTERED",
        table_range="A1",
        include_values_in_response=False
    ))
    bust_cache()

    # 尝试删除最后一行（若 row_count 与已用行不一致，删除失败也没关系）
    try:
        _retry(lambda: ws.delete_rows(ws.row_count))
    except Exception:
        pass

    bust_cache()
    return True


# ======== 调试辅助：解析写回区间 & 表尾快照 ========
def parse_updated_range_rows(resp: dict) -> Optional[Tuple[int, int]]:
    """
    从 append_rows 的返回值中解析出起止行号。
    例：'updates': {'updatedRange': '购入/剩余!A244:I245'}
    """
    try:
        rng = resp.get("updates", {}).get("updatedRange", "")
        # 抓取末尾的行号范围（支持 A244:I244 或 A244:I245）
        m = re.search(r"[A-Z]+(\d+):[A-Z]+(\d+)$", rng)
        if m:
            return int(m.group(1)), int(m.group(2))
        # 单行情况（没有冒号）
        m2 = re.search(r"[A-Z]+(\d+)$", rng)
        if m2:
            r = int(m2.group(1))
            return r, r
    except Exception:
        pass
    return None


def tail_rows(n: int = 10) -> pd.DataFrame:
    """
    返回表尾最近 n 行（含表头）的快照，便于调试“写到哪里了”。
    """
    ws = _get_ws()
    all_values = ws.get_all_values()  # 二维列表
    if not all_values:
        return pd.DataFrame()

    header = all_values[0]
    data = all_values[1:]

    start = max(0, len(data) - n)
    tail = data[start:]

    df = pd.DataFrame(tail, columns=header)
    # 附带显示大致行号（首行是 header，数据从第 2 行开始）
    df.insert(0, "__row__", list(range(1 + 1 + start, 1 + 1 + start + len(tail))))
    return df
