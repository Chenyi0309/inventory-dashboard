# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from typing import Dict
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

SHEET_URL = os.getenv("INVENTORY_SHEET_URL", "").strip()
WORKSHEET_NAME = os.getenv("INVENTORY_WORKSHEET_NAME", "购入/剩余")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

def _get_client():
    # Use local service_account.json if it exists, otherwise fall back to env var JSON
    if os.path.exists("service_account.json"):
        creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
    else:
        # If SERVICE_ACCOUNT_JSON env exists, write it to a tmp file
        sa_json = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()
        if not sa_json:
            raise RuntimeError("Missing Google service account credentials.")
        import json, tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as f:
            f.write(sa_json.encode("utf-8"))
            tmp = f.name
        creds = Credentials.from_service_account_file(tmp, scopes=SCOPES)
    return gspread.authorize(creds)

def read_records() -> pd.DataFrame:
    if not SHEET_URL:
        raise RuntimeError("ENV INVENTORY_SHEET_URL 未设置。")
    gc = _get_client()
    sh = gc.open_by_url(SHEET_URL)
    ws = sh.worksheet(WORKSHEET_NAME)
    data = ws.get_all_records()
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    # 规范列名（与前端一致）
    rename_map: Dict[str,str] = {
        "日期 (Date)": "日期 (Date)",
        "食材名称 (Item Name)": "食材名称 (Item Name)",
        "分类 (Category)": "分类 (Category)",
        "数量 (Qty)": "数量 (Qty)",
        "单位 (Unit)": "单位 (Unit)",
        "单价 (Unit Price)": "单价 (Unit Price)",
        "总价 (Total Cost)": "总价 (Total Cost)",
        "状态 (Status)": "状态 (Status)",
        "备注 (Notes)": "备注 (Notes)",
    }
    df = df.rename(columns=rename_map)
    # 清洗数据类型
    if "日期 (Date)" in df.columns:
        df["日期 (Date)"] = pd.to_datetime(df["日期 (Date)"], errors="coerce")
    for col in ["数量 (Qty)", "单价 (Unit Price)", "总价 (Total Cost)"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    # 去掉空 item
    if "食材名称 (Item Name)" in df.columns:
        df["食材名称 (Item Name)"] = df["食材名称 (Item Name)"].astype(str).str.strip()
        df = df[df["食材名称 (Item Name)"] != ""]
    # 去掉分类空格
    if "分类 (Category)" in df.columns:
        df["分类 (Category)"] = df["分类 (Category)"].astype(str).str.strip()
    if "状态 (Status)" in df.columns:
        df["状态 (Status)"] = df["状态 (Status)"].astype(str).str.strip()
    return df
