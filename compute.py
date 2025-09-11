# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import pandas as pd
from typing import Optional, Tuple, Dict, List

# ========= 列名适配 =========
ALIASES: Dict[str, List[str]] = {
    "日期 (Date)": ["日期 (Date)", "日期", "Date", "date"],
    "食材名称 (Item Name)": ["食材名称 (Item Name)", "食材名称", "Item Name", "item name", "物品名", "名称"],
    "分类 (Category)": ["分类 (Category)", "分类", "Category", "category", "类型"],
    "数量 (Qty)": ["数量 (Qty)", "数量", "Qty", "qty"],
    "单位 (Unit)": ["单位 (Unit)", "单位", "Unit", "unit"],
    "单价 (Unit Price)": ["单价 (Unit Price)", "单价", "Unit Price", "price", "unit price"],
    "总价 (Total Cost)": ["总价 (Total Cost)", "总价", "Total Cost", "amount", "cost"],
    "状态 (Status)": ["状态 (Status)", "状态", "Status", "status"],
    "备注 (Notes)": ["备注 (Notes)", "备注", "Notes", "notes"],
}

import re

def _fix_col_token(s: str) -> str:
    s = (s or "")
    # 常见不可见字符 & 全角括号/空格
    s = s.replace("\u3000", " ")         # 全角空格
    s = s.replace("（", "(").replace("）", ")")
    s = s.replace("\u00A0", " ")         # 不换行空格
    s = s.replace("\u200B", "")          # 零宽空白
    # 把括号里的空格收紧："(  Date  )" → "(Date)"
    s = re.sub(r"\(\s*([^)]+?)\s*\)", r"(\1)", s)
    # 去掉 CJK 与括号之间多余空格："日期  (Date)" → "日期 (Date)"
    s = re.sub(r"([\u4e00-\u9fffA-Za-z0-9])\s+\(", r"\1 (", s)
    s = re.sub(r"\)\s+([\u4e00-\u9fffA-Za-z0-9])", r") \1", s)
    # 压缩其余连续空白
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    df = df.rename(columns={_fix_col_token(c): c for c in df.columns})
    lower_map = {c.lower(): c for c in df.columns}
    mapping = {}
    for std, alts in ALIASES.items():
        for a in alts:
            af = _fix_col_token(a)
            if af in df.columns:
                mapping[af] = std; break
            if af.lower() in lower_map:
                mapping[lower_map[af.lower()]] = std; break
    df = df.rename(columns=mapping)
    if "日期 (Date)" in df.columns:
        df["日期 (Date)"] = pd.to_datetime(df["日期 (Date)"], errors="coerce")
    for col in ["数量 (Qty)", "单价 (Unit Price)", "总价 (Total Cost)"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

# ========= 小工具 =========
def _last_of(g: pd.DataFrame, status: str) -> Optional[pd.Series]:
    if "状态 (Status)" not in g or "日期 (Date)" not in g:
        return None
    d = g[g["状态 (Status)"] == status].sort_values("日期 (Date)")
    if d.empty:
        return None
    return d.iloc[-1]

def _recent_two_remainders(g: pd.DataFrame) -> Tuple[Optional[pd.Series], Optional[pd.Series]]:
    if "状态 (Status)" not in g or "日期 (Date)" not in g:
        return None, None
    d = g[g["状态 (Status)"] == "剩余"].sort_values("日期 (Date)")
    if len(d) < 2:
        return (None, d.iloc[-1]) if len(d) == 1 else (None, None)
    return d.iloc[-2], d.iloc[-1]

# ========= 14 天用量估算（稳健） =========
def _recent_usage_14d_new(item_df: pd.DataFrame) -> Optional[float]:
    if item_df is None or item_df.empty:
        return None
    item_df = _normalize_columns(item_df)

    prev_r, last_r = _recent_two_remainders(item_df)
    if prev_r is not None and last_r is not None:
        between = item_df[(item_df["日期 (Date)"] > prev_r["日期 (Date)"]) &
                          (item_df["日期 (Date)"] <= last_r["日期 (Date)"])]
        has_buy_between = (between["状态 (Status)"] == "买入").any()
        days = max(1, (last_r["日期 (Date)"] - prev_r["日期 (Date)"]).days)
        q_prev = pd.to_numeric(pd.Series([prev_r["数量 (Qty)"]])).iloc[0]
        q_last = pd.to_numeric(pd.Series([last_r["数量 (Qty)"]])).iloc[0]
        if (not has_buy_between) and pd.notna(q_prev) and pd.notna(q_last) and float(q_last) <= float(q_prev):
            used = float(q_prev) - float(q_last)
            return (used / days) * 14.0 if days > 0 else None

    last_buy = _last_of(item_df, "买入")
    last_rem = _last_of(item_df, "剩余")
    if last_buy is not None and last_rem is not None and last_rem["日期 (Date)"] >= last_buy["日期 (Date)"]:
        q_buy  = pd.to_numeric(pd.Series([last_buy["数量 (Qty)"]])).iloc[0]
        q_rem  = pd.to_numeric(pd.Series([last_rem["数量 (Qty)"]])).iloc[0]
        if pd.notna(q_buy) and pd.notna(q_rem):
            days = max(1, (last_rem["日期 (Date)"] - last_buy["日期 (Date)"]).days)
            used = max(0.0, float(q_buy) - float(q_rem))
            return (used / days) * 14.0 if days > 0 else None
    return None

_recent_usage_14d_robust = _recent_usage_14d_new

def _current_stock(g: pd.DataFrame) -> Optional[float]:
    g = _normalize_columns(g)
    last_r = _last_of(g, "剩余")
    if last_r is None:
        return None
    q = pd.to_numeric(pd.Series([last_r["数量 (Qty)"]]), errors="coerce").iloc[0]
    return float(q) if pd.notna(q) else None

def _days_left(curr_stock: Optional[float], use_14d: Optional[float]) -> Optional[float]:
    if curr_stock is None or use_14d is None or use_14d <= 0:
        return None
    daily = use_14d / 14.0
    return curr_stock / daily if daily > 0 else None

def _next_purchase_qty(curr_stock: Optional[float], use_14d: Optional[float]) -> Optional[float]:
    if use_14d is None:
        return None
    target = use_14d
    return max(0.0, target - (curr_stock or 0.0))

# ========= 主函数：按“食材名称”汇总 =========
def compute_stats(records: pd.DataFrame) -> pd.DataFrame:
    if records is None or records.empty:
        return pd.DataFrame(columns=[
            "食材名称 (Item Name)", "当前库存", "平均最近两周使用量", "预计还能用天数", "计算下次采购量",
            "最近统计剩余日期", "最近采购日期", "最近采购数量", "最近采购单价",
            "平均采购间隔(天)", "累计支出"
        ])

    df = _normalize_columns(records.copy())

    # 必要字段检查
    must = ["食材名称 (Item Name)", "日期 (Date)", "状态 (Status)", "数量 (Qty)"]
    if any(c not in df.columns for c in must):
        return pd.DataFrame(columns=[
            "食材名称 (Item Name)", "当前库存", "平均最近两周使用量", "预计还能用天数", "计算下次采购量",
            "最近统计剩余日期", "最近采购日期", "最近采购数量", "最近采购单价",
            "平均采购间隔(天)", "累计支出"
        ])

    out = []
    for item, g in df.groupby("食材名称 (Item Name)"):
        g = g.sort_values("日期 (Date)")

        curr_stock = _current_stock(g)
        use_14d = _recent_usage_14d_new(g)
        days_left = _days_left(curr_stock, use_14d)
        next_buy = _next_purchase_qty(curr_stock, use_14d)

        last_rem = _last_of(g, "剩余")
        last_buy = _last_of(g, "买入")

        buys = g[g["状态 (Status)"] == "买入"].sort_values("日期 (Date)")
        if len(buys) >= 2:
            gaps = buys["日期 (Date)"].diff().dropna().dt.days
            avg_gap = float(gaps.mean()) if not gaps.empty else None
        else:
            avg_gap = None

        last_buy_qty   = float(pd.to_numeric(pd.Series([last_buy["数量 (Qty)"]]), errors="coerce").iloc[0]) if last_buy is not None else None
        last_buy_price = float(pd.to_numeric(pd.Series([last_buy["单价 (Unit Price)"]]), errors="coerce").iloc[0]) if (last_buy is not None and "单价 (Unit Price)" in g) else None
        total_spend    = float(buys["总价 (Total Cost)"].sum()) if "总价 (Total Cost)" in g.columns else None

        out.append({
            "食材名称 (Item Name)": item,
            "当前库存": curr_stock,
            "平均最近两周使用量": use_14d,
            "预计还能用天数": days_left,
            "计算下次采购量": next_buy,
            "最近统计剩余日期": (last_rem["日期 (Date)"] if last_rem is not None else None),
            "最近采购日期": (last_buy["日期 (Date)"] if last_buy is not None else None),
            "最近采购数量": last_buy_qty,
            "最近采购单价": last_buy_price,
            "平均采购间隔(天)": avg_gap,
            "累计支出": total_spend,
        })

    res = pd.DataFrame(out)
    if not res.empty and "预计还能用天数" in res.columns:
        res = res.sort_values(["预计还能用天数", "食材名称 (Item Name)"],
                              ascending=[True, True], na_position="last").reset_index(drop=True)
    return res
