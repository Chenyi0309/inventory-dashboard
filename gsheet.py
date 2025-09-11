# -*- coding: utf-8 -*-
"""Google Sheets helpers: 读取/写入『购入/剩余』，可选读取主数据表。"""
from __future__ import annotations

import os
from typing import Dict, List
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

try:
    import streamlit as st
    _HAS_ST = True
except Exception:
    _HAS_ST = False

SHEET_URL = os.getenv("INVENTORY_SHEET_URL", "").strip()
WS_RECORDS = os.getenv("INVENTORY_WORKSHEET_NAME", "购入/剩余")  # 明细表
WS_CATALOG = os.getenv("INVENTORY_CATALOG_NAME", "库存产品")       # 主数据(可无)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def _get_client():
    if os.path.exists("service_account.json"):
        creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
    else:
        sa_json = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()
        if not sa_json:
            raise RuntimeError("缺少 Google Service Account 凭证（service_account.json 或 SERVICE_ACCOUNT_JSON）。")
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as f:
            f.write(sa_json.encode("utf-8"))
            path = f.name
        creds = Credentials.from_service_account_file(path, scopes=SCOPES)
    return gspread.authorize(creds)

def _open_sheet():
    if not SHEET_URL:
        raise RuntimeError("ENV INVENTORY_SHEET_URL 未设置。")
    gc = _get_client()
    sh = gc.open_by_url(SHEET_URL)
    return sh

def read_records() -> pd.DataFrame:
    sh = _open_sheet()
    ws = sh.worksheet(WS_RECORDS)
    data = ws.get_all_records()
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    # 基本清洗：保留常用列名（其余归一化交给 app/compute）
    if "日期 (Date)" in df.columns:
        df["日期 (Date)"] = pd.to_datetime(df["日期 (Date)"], errors="coerce")
    for col in ["数量 (Qty)", "单价 (Unit Price)", "总价 (Total Cost)"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "食材名称 (Item Name)" in df.columns:
        df["食材名称 (Item Name)"] = df["食材名称 (Item Name)"].astype(str).str.strip()
        df = df[df["食材名称 (Item Name)"] != ""]
    for c in ["分类 (Category)","状态 (Status)","单位 (Unit)"]:
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()
    return df

def read_catalog() -> pd.DataFrame:
    try:
        sh = _open_sheet()
        ws = sh.worksheet(WS_CATALOG)
        data = ws.get_all_records()
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df = df.rename(columns={
            "物品名": "物品名", "单位": "单位",
            "类型": "类型", "类别": "类型", "分类": "类型",
        })
        return df
    except Exception:
        return pd.DataFrame(columns=["物品名", "单位", "类型"])

def append_record(record: Dict[str, object]) -> None:
    sh = _open_sheet()
    ws = sh.worksheet(WS_RECORDS)
    headers: List[str] = ws.row_values(1)
    if not headers:
        headers = ["日期 (Date)", "食材名称 (Item Name)", "分类 (Category)", "数量 (Qty)",
                   "单位 (Unit)", "单价 (Unit Price)", "总价 (Total Cost)",
                   "状态 (Status)", "备注 (Notes)"]
        ws.append_row(headers, value_input_option="RAW")
    row = []
    for h in headers:
        v = record.get(h, "")
        if isinstance(v, (float, int)) and pd.notna(v):
            row.append(v)
        else:
            row.append("" if v is None else str(v))
    ws.append_row(row, value_input_option="USER_ENTERED")

if _HAS_ST:
    @st.cache_data(show_spinner=False, ttl=60)
    def read_records_cached() -> pd.DataFrame:
        return read_records()

    @st.cache_data(show_spinner=False, ttl=60)
    def read_catalog_cached() -> pd.DataFrame:
        return read_catalog()

    def bust_cache():
        try:
            st.cache_data.clear()
        except Exception:
            pass
