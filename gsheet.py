# -*- coding: utf-8 -*-
"""Google Sheets backend helpers (clean, no debug prints).
The target worksheet MUST be named '购入/剩余' (you can change the name below if needed).
"""
from __future__ import annotations

import os
from typing import Dict
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import streamlit as st  # 用于缓存

# ======== CONFIG ========
SHEET_URL = os.getenv("INVENTORY_SHEET_URL", "").strip()
WORKSHEET_NAME = os.getenv("INVENTORY_WORKSHEET_NAME", "购入/剩余")

HEADERS = [
    "日期 (Date)",
    "食材名称 (Item Name)",
    "分类 (Category)",
    "数量 (Qty)",
    "单位 (Unit)",
    "单价 (Unit Price)",
    "总价 (Total Cost)",
    "状态 (Status)",    # 买入 / 剩余
    "备注 (Notes)"
]

STATUS_VALUES = ["买入", "剩余"]

# ================== 基础 ==================

def _get_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file("service_account.json", scopes=scopes)
    return gspread.authorize(creds)

def _get_ws():
    assert SHEET_URL, "Please set INVENTORY_SHEET_URL env var or hardcode SHEET_URL."
    gc = _get_client()
    sh = gc.open_by_url(SHEET_URL)
    try:
        ws = sh.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=len(HEADERS))
        ws.append_row(HEADERS)
    return ws

def ensure_headers(ws=None):
    ws = ws or _get_ws()
    values = ws.get_values("1:1")
    if not values or not values[0]:
        ws.update("1:1", [HEADERS])

# ================== 写入 ==================

def append_record(record: Dict):
    ws = _get_ws()
    ensure_headers(ws)

    row = [
        record.get("日期 (Date)",""),
        record.get("食材名称 (Item Name)","").strip(),
        record.get("分类 (Category)","").strip(),
        record.get("数量 (Qty)",""),
        record.get("单位 (Unit)","").strip(),
        record.get("单价 (Unit Price)",""),
        record.get("总价 (Total Cost)",""),
        record.get("状态 (Status)",""),
        record.get("备注 (Notes)","").strip(),
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")

# ================== 读 & 清洗 ==================

def _normalize_aliases(df: pd.DataFrame) -> pd.DataFrame:
    """列名别名对齐"""
    aliases = {
        "日期": "日期 (Date)",
        "食材名称": "食材名称 (Item Name)",
        "物品名": "食材名称 (Item Name)",
        "物品名称": "食材名称 (Item Name)",
        "类别": "分类 (Category)",
        "分类": "分类 (Category)",
        "数量": "数量 (Qty)",
        "单位": "单位 (Unit)",
        "单价": "单价 (Unit Price)",
        "总价": "总价 (Total Cost)",
        "状态": "状态 (Status)",
        "备注": "备注 (Notes)",
    }
    for old, new in aliases.items():
        if old in df.columns and new not in df.columns:
            df[new] = df.pop(old)
    # 确保所有列存在
    for col in HEADERS:
        if col not in df.columns:
            df[col] = None
    return df

def _normalize_values(df: pd.DataFrame) -> pd.DataFrame:
    """字符串去空格/清 None，状态归一，数值列转数值，自动补总价，丢弃空名称"""
    # 字符串列
    for c in ["食材名称 (Item Name)", "分类 (Category)", "单位 (Unit)", "状态 (Status)", "备注 (Notes)"]:
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()
            df[c] = df[c].replace({"None": "", "nan": ""})

    # 状态归一
    if "状态 (Status)" in df.columns:
        df["状态 (Status)"] = df["状态 (Status)"].replace({
            "购买": "买入", "采购": "买入", "进货": "买入",
            "库存": "剩余", "余量": "剩余", "余": "剩余"
        })

    # 日期解析
    def _parse_date(x):
        if pd.isna(x) or x == "":
            return pd.NaT
        try:
            return pd.to_datetime(x)
        except Exception:
            try:
                # Excel serial date
                return pd.to_datetime("1899-12-30") + pd.to_timedelta(float(x), unit="D")
            except Exception:
                return pd.NaT
    df["日期 (Date)"] = df["日期 (Date)"].apply(_parse_date)

    # 数值列
    for c in ["数量 (Qty)", "单价 (Unit Price)", "总价 (Total Cost)"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # 自动补总价（买入行）
    mask_buy = df["状态 (Status)"].eq("买入")
    need_total = mask_buy & df["总价 (Total Cost)"].isna()
    df.loc[need_total, "总价 (Total Cost)"] = df.loc[need_total, "数量 (Qty)"] * df.loc[need_total, "单价 (Unit Price)"]

    # 丢弃“食材名称”为空的行
    df = df[df["食材名称 (Item Name)"].astype(str).str.strip() != ""].copy()

    return df

def read_records() -> pd.DataFrame:
    ws = _get_ws()
    ensure_headers(ws)
    records = ws.get_all_records()
    df = pd.DataFrame(records)
    df = _normalize_aliases(df)
    df = _normalize_values(df)
    return df

# ================== 主数据（物品清单） ==================

def read_catalog():
    """读取物品主数据表，优先 ['库存产品','Content_tracker','物品清单']"""
    ws_names_try = ["库存产品", "Content_tracker", "物品清单"]
    ws = _get_ws()   # 只为拿到 spreadsheet 对象
    sh = ws.spreadsheet

    target = None
    for name in ws_names_try:
        try:
            target = sh.worksheet(name)
            break
        except Exception:
            continue
    if target is None:
        return pd.DataFrame(columns=["物品名","类型","单位","备注"])

    df = pd.DataFrame(target.get_all_records())

    # 列名映射
    aliases = {
        "物品名": "物品名",
        "物品名称": "物品名",
        "物品": "物品名",
        "物品名称 (Item)": "物品名",

        "类型": "类型",
        "类别": "类型",
        "分类": "类型",

        "单位": "单位",
        "备注": "备注"
    }
    for old, new in aliases.items():
        if old in df.columns and new not in df.columns:
            df[new] = df.pop(old)

    # 只保留关键列
    keep = [c for c in ["物品名","类型","单位","备注"] if c in df.columns]
    df = df[keep].copy()

    # 清洗
    for c in df.columns:
        if c in ["物品名","类型","单位","备注"]:
            df[c] = df[c].astype(str).str.strip()

    # 过滤空物品名
    df = df[df["物品名"].astype(bool)]
    return df

# ================== 缓存 & 清缓存 ==================

@st.cache_data(show_spinner=False, ttl=60)
def read_records_cached() -> pd.DataFrame:
    """缓存读取‘购入/剩余’ 60 秒。"""
    return read_records()

@st.cache_data(show_spinner=False, ttl=60)
def read_catalog_cached() -> pd.DataFrame:
    """缓存读取主数据（库存产品/Content_tracker/物品清单）60 秒。"""
    return read_catalog()

def bust_cache():
    """手动清除缓存（在页面上做“刷新数据”按钮时调用）"""
    try:
        st.cache_data.clear()
    except Exception:
        pass
