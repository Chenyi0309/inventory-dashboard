# -*- coding: utf-8 -*-
"""Google Sheets backend helpers.
Fill SHEET_URL and place your service_account.json in the project root.
The target worksheet MUST be named '购入/剩余' (you can change the name below if needed).
"""
from __future__ import annotations

import os
import datetime as dt
from typing import Dict, List, Tuple
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

# ======== CONFIG ========
SHEET_URL = os.getenv("INVENTORY_SHEET_URL", "").strip()  # paste Sheet URL or set env var
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

def _get_client():
    # Requires service_account.json in working dir
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
    else:
        # if header mismatch, do nothing (avoid overwriting user sheet);
        # you can manually align header names if needed
        pass

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

def read_records() -> pd.DataFrame:
    ws = _get_ws()
    ensure_headers(ws)
    records = ws.get_all_records()
    df = pd.DataFrame(records)

    # Normalize columns that might be slightly different in user's sheet
    # Try to alias common header variants
    aliases = {
        "日期": "日期 (Date)",
        "食材名称": "食材名称 (Item Name)",
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

    # Ensure required columns exist
    for col in HEADERS:
        if col not in df.columns:
            df[col] = None

    # Parse types
    def _parse_date(x):
        if pd.isna(x) or x == "":
            return pd.NaT
        try:
            return pd.to_datetime(x)
        except Exception:
            try:
                # Excel style serial?
                return pd.to_datetime("1899-12-30") + pd.to_timedelta(float(x), unit="D")
            except Exception:
                return pd.NaT

    df["日期 (Date)"] = df["日期 (Date)"].apply(_parse_date)
    num_cols = ["数量 (Qty)", "单价 (Unit Price)", "总价 (Total Cost)"]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # Clean strings
    for c in ["食材名称 (Item Name)", "分类 (Category)", "单位 (Unit)", "状态 (Status)", "备注 (Notes)"]:
        df[c] = df[c].astype(str).str.strip()

    # Auto compute Total Cost if missing and it's a buy row
    mask_buy = df["状态 (Status)"].eq("买入")
    need_total = mask_buy & df["总价 (Total Cost)"].isna()
    df.loc[need_total, "总价 (Total Cost)"] = df.loc[need_total, "数量 (Qty)"] * df.loc[need_total, "单价 (Unit Price)"]

    return df
