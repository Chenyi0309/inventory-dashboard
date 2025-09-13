# compute.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import Optional, Tuple
import pandas as pd
import numpy as np

# ===================== 列名规范化（唯一实现） =====================
_NBSP = "\xa0"
_FULL_L = "（"
_FULL_R = "）"

_CANONICAL = {
    "日期 (Date)": ["日期", "日期(date)", "date", "Date", "日期 ", "日期(Date)"],
    "食材名称 (Item Name)": ["食材名称", "食材", "名称", "item name", "item", "品名", "Item Name", "食材名称(Item Name)"],
    "分类 (Category)": ["分类", "类别", "category", "Category", "分类(Category)"],
    "数量 (Qty)": ["数量", "qty", "数量 (qty)", "Qty", "数量(Qty)"],
    "单位 (Unit)": ["单位", "unit", "Unit", "单位(Unit)"],
    "单价 (Unit Price)": ["单价", "unit price", "价格/单价", "单价(元)", "Unit Price", "单价(Unit Price)"],
    "总价 (Total Cost)": ["总价", "total", "total cost", "金额", "总价(元)", "Total Cost", "总价(Total Cost)"],
    "状态 (Status)": ["状态", "status", "Status", "状态(Status)"],
    "备注 (Notes)": ["备注", "notes", "note", "Notes", "备注(Notes)"],
}

_FLAT = {k.lower(): k for k in _CANONICAL}
for k, alts in _CANONICAL.items():
    for a in alts:
        _FLAT[a.lower()] = k


def _clean_token(s: str) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace(_NBSP, " ").replace(_FULL_L, "(").replace(_FULL_R, ")").replace("\u200B", "")
    # ✨ 关键：如果括号前没有空格，自动补一个（例：食材名称(Item Name) → 食材名称 (Item Name)）
    s = re.sub(r"([\u4e00-\u9fffA-Za-z0-9])\(", r"\1 (", s)
    # 如果 ) 和下一段文字之间缺空格，补一个
    s = re.sub(r"\)\s*([A-Za-z0-9\u4e00-\u9fff])", r") \1", s)
    # 括号内部去多余空格
    s = re.sub(r"\(\s*([^)]+?)\s*\)", r"(\1)", s)
    # 折叠其它空白
    s = re.sub(r"\s+", " ", s.strip())
    return s


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """把表头统一到规范名，并对数值做基础清洗（支持'30%'、'1,234.5'）。"""
    if df is None or df.empty:
        return df.copy()
    cols = []
    for c in df.columns:
        c0 = _clean_token(c)
        cols.append(_FLAT.get(c0.lower(), c0))
    out = df.copy()
    out.columns = cols

    # 日期统一
    if "日期 (Date)" in out.columns:
        out["日期 (Date)"] = pd.to_datetime(out["日期 (Date)"], errors="coerce")

    # 数值统一
    for col in ["数量 (Qty)", "单价 (Unit Price)", "总价 (Total Cost)"]:
        if col in out.columns:
            s = (
                out[col].astype(str)
                .str.replace(",", "", regex=False)
                .str.strip()
            )
            is_pct = s.str.endswith("%", na=False)
            s = pd.to_numeric(s.str.rstrip("%"), errors="coerce")
            s = np.where(is_pct, s / 100.0, s)
            out[col] = pd.to_numeric(s, errors="coerce")

    # 状态收敛
    if "状态 (Status)" in out.columns:
        s = out["状态 (Status)"].astype(str).str.lower()
        out["状态 (Status)"] = np.where(
            s.str.contains("买|purchase|buy"), "买入",
            np.where(s.str.contains("余|remain|left|stock"), "剩余", out["状态 (Status)"])
        )
    return out


# ===================== 内部工具 =====================
def _last_remainder(df_item: pd.DataFrame) -> Tuple[Optional[pd.Timestamp], Optional[float]]:
    r = df_item[df_item["状态 (Status)"] == "剩余"].sort_values("日期 (Date)")
    if r.empty:
        return None, None
    row = r.iloc[-1]
    return row["日期 (Date)"], float(row["数量 (Qty)"]) if pd.notna(row["数量 (Qty)"]) else None


def _last_purchase_info(df_item: pd.DataFrame):
    """返回 (最近买入日期, 数量, 单价, 平均采购间隔天, 累计支出)"""
    b = df_item[df_item["状态 (Status)"] == "买入"].sort_values("日期 (Date)")
    if b.empty:
        return None, None, None, None, 0.0
    last = b.iloc[-1]
    avg_int = None
    if len(b) >= 2:
        diffs = b["日期 (Date)"].diff().dt.days.dropna()
        if not diffs.empty:
            avg_int = float(diffs.mean())
    total_spend = float(b.get("总价 (Total Cost)", pd.Series([], dtype=float)).sum())
    return (
        last["日期 (Date)"],
        float(last.get("数量 (Qty)")) if pd.notna(last.get("数量 (Qty)")) else None,
        float(last.get("单价 (Unit Price)")) if pd.notna(last.get("单价 (Unit Price)")) else None,
        float(avg_int) if avg_int is not None else None,
        total_spend
    )


def _recent_usage_14d(df_item: pd.DataFrame, asof: Optional[pd.Timestamp] = None) -> Optional[float]:
    """
    估算最近两周使用量（优先：两次连续“剩余”且后一次<=前一次且中间无买入；否则：最近“买入”->最近“剩余”）。
    返回 14 天的总用量（不是日均）。
    """
    x = df_item.sort_values("日期 (Date)")

    # 1) 连续两次“剩余”，中间无买入
    r = x[x["状态 (Status)"] == "剩余"][["日期 (Date)", "数量 (Qty)"]].dropna()
    if len(r) >= 2:
        r_prev, r_last = r.iloc[-2], r.iloc[-1]
        mid = x[(x["日期 (Date)"] > r_prev["日期 (Date)"]) & (x["日期 (Date)"] <= r_last["日期 (Date)"])]
        if not any(mid["状态 (Status)"] == "买入"):
            prev_qty, last_qty = float(r_prev["数量 (Qty)"]), float(r_last["数量 (Qty)"])
            if last_qty <= prev_qty:
                days = (r_last["日期 (Date)"] - r_prev["日期 (Date)"]).days
                if days > 0:
                    daily = (prev_qty - last_qty) / days
                    if daily >= 0:
                        return float(daily * 14.0)

    # 2) 最近“买入”->最近“剩余”
    b = x[x["状态 (Status)"] == "买入"][["日期 (Date)", "数量 (Qty)"]].dropna()
    r = x[x["状态 (Status)"] == "剩余"][["日期 (Date)", "数量 (Qty)"]].dropna()
    if len(b) >= 1 and len(r) >= 1:
        b_last, r_last = b.iloc[-1], r.iloc[-1]
        if r_last["日期 (Date)"] >= b_last["日期 (Date)"]:
            days = (r_last["日期 (Date)"] - b_last["日期 (Date)"]).days
            if days > 0:
                used = float(b_last["数量 (Qty)"]) - float(r_last["数量 (Qty)"])
                daily = used / days
                if daily >= 0:
                    return float(daily * 14.0)

    return None


# 保持你 app.py 中的 import 兼容
def _recent_usage_14d_robust(df_item: pd.DataFrame) -> Optional[float]:
    return _recent_usage_14d(df_item)


# ===================== 对外主函数 =====================
def compute_stats(df: pd.DataFrame) -> pd.DataFrame:
    """
    输入：买入/剩余明细
    输出：每个“食材名称”的统计汇总（当前库存、最近14天用量、还能用天数、下次采购量、最近日期们、累计支出等）
    """
    df = normalize_columns(df)

    must = ["食材名称 (Item Name)", "日期 (Date)", "状态 (Status)", "数量 (Qty)"]
    missing = [c for c in must if c not in df.columns]
    if missing:
        return pd.DataFrame(columns=[
            "食材名称 (Item Name)", "当前库存", "平均最近两周使用量", "预计还能用天数", "计算下次采购量",
            "最近统计剩余日期", "最近采购日期", "最近采购数量", "最近采购单价",
            "平均采购间隔(天)", "累计支出"
        ])

    df = df[pd.notna(df["日期 (Date)"])].copy()

    rows = []
    for item, g in df.groupby("食材名称 (Item Name)"):
        g = g.sort_values("日期 (Date)")

        last_rem_date, last_rem_qty = _last_remainder(g)
        last_buy_date, last_buy_qty, last_buy_price, avg_buy_interval, total_spend_item = _last_purchase_info(g)

        usage_14 = _recent_usage_14d(g)

        days_left = np.nan
        next_buy_qty = np.nan
        if usage_14 is not None and usage_14 > 0 and last_rem_qty is not None:
            daily = usage_14 / 14.0
            if daily > 0:
                days_left = float(last_rem_qty / daily)
                deficit = usage_14 - float(last_rem_qty)
                next_buy_qty = float(np.ceil(deficit)) if deficit > 0 else 0.0

        rows.append({
            "食材名称 (Item Name)": item,
            "当前库存": float(last_rem_qty) if last_rem_qty is not None else np.nan,
            "平均最近两周使用量": float(usage_14) if usage_14 is not None else np.nan,
            "预计还能用天数": float(days_left) if days_left == days_left else np.nan,
            "计算下次采购量": float(next_buy_qty) if next_buy_qty == next_buy_qty else np.nan,
            "最近统计剩余日期": last_rem_date,
            "最近采购日期": last_buy_date,
            "最近采购数量": float(last_buy_qty) if last_buy_qty is not None else np.nan,
            "最近采购单价": float(last_buy_price) if last_buy_price is not None else np.nan,
            "平均采购间隔(天)": float(avg_buy_interval) if avg_buy_interval is not None else np.nan,
            "累计支出": float(total_spend_item or 0.0),
        })

    out = pd.DataFrame(rows)

    # 日期渲染友好
    for c in ["最近统计剩余日期", "最近采购日期"]:
        if c in out.columns:
            out[c] = pd.to_datetime(out[c], errors="coerce").dt.strftime("%Y-%m-%d")

    # 排序：优先“还能用天数”小，其次“两周用量”大
    if not out.empty:
        out = out.sort_values(
            by=["预计还能用天数", "平均最近两周使用量"],
            ascending=[True, False],
            na_position="last"
        ).reset_index(drop=True)

    return out
