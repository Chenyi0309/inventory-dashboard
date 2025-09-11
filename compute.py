# -*- coding: utf-8 -*-
from __future__ import annotations

import pandas as pd
from typing import Optional, Tuple

# ===== 小工具 =====
def _last_of(item_df: pd.DataFrame, status: str) -> Optional[pd.Series]:
    d = item_df[item_df["状态 (Status)"] == status].sort_values("日期 (Date)")
    if d.empty:
        return None
    return d.iloc[-1]

def _recent_two_remainders(item_df: pd.DataFrame) -> Tuple[Optional[pd.Series], Optional[pd.Series]]:
    d = item_df[item_df["状态 (Status)"] == "剩余"].sort_values("日期 (Date)")
    if len(d) < 2:
        return None, (d.iloc[-1] if len(d) else None)
    return d.iloc[-2], d.iloc[-1]

# ===== 14 天用量估算（稳健版） =====
def _recent_usage_14d_new(item_df: pd.DataFrame) -> Optional[float]:
    """
    估算最近14天使用量，不依赖“使用”事件，只用 买入/剩余。
    规则：
    A) 若有相邻两次“剩余”，中间无“买入”，且库存下降：用 (前一次剩余 - 最近一次剩余)/天数 * 14
    B) 否则，用 最近一次“买入” 到 最近一次“剩余”的 (买入量 - 剩余量)/天数 * 14
    """
    # A)
    prev_r, last_r = _recent_two_remainders(item_df)
    if prev_r is not None and last_r is not None:
        between = item_df[(item_df["日期 (Date)"] > prev_r["日期 (Date)"]) &
                          (item_df["日期 (Date)"] <= last_r["日期 (Date)"])]
        has_buy_between = (between["状态 (Status)"] == "买入").any()
        days = max(1, (last_r["日期 (Date)"] - prev_r["日期 (Date)"]).days)
        if (not has_buy_between) and pd.notna(prev_r["数量 (Qty)"]) and pd.notna(last_r["数量 (Qty)"]):
            if float(last_r["数量 (Qty)"]) <= float(prev_r["数量 (Qty)"]):
                used = float(prev_r["数量 (Qty)"]) - float(last_r["数量 (Qty)"])
                return (used / days) * 14.0 if days > 0 else None

    # B)
    last_buy = _last_of(item_df, "买入")
    last_rem = _last_of(item_df, "剩余")
    if last_buy is not None and last_rem is not None and last_rem["日期 (Date)"] >= last_buy["日期 (Date)"]:
        if pd.notna(last_buy["数量 (Qty)"]) and pd.notna(last_rem["数量 (Qty)"]):
            days = max(1, (last_rem["日期 (Date)"] - last_buy["日期 (Date)"]).days)
            used = max(0.0, float(last_buy["数量 (Qty)"]) - float(last_rem["数量 (Qty)"]))
            return (used / days) * 14.0 if days > 0 else None

    return None

# 兼容你 app.py 里可能导入的名字
_recent_usage_14d_robust = _recent_usage_14d_new

def _current_stock(item_df: pd.DataFrame) -> Optional[float]:
    last_r = _last_of(item_df, "剩余")
    return float(last_r["数量 (Qty)"]) if last_r is not None and pd.notna(last_r["数量 (Qty)"]) else None

def _days_left(curr_stock: Optional[float], use_14d: Optional[float]) -> Optional[float]:
    if curr_stock is None or use_14d is None or use_14d <= 0:
        return None
    daily = use_14d / 14.0
    return (curr_stock / daily) if daily > 0 else None

def _next_purchase_qty(curr_stock: Optional[float], use_14d: Optional[float]) -> Optional[float]:
    if use_14d is None:
        return None
    target = use_14d  # 目标补到 14 天用量
    if curr_stock is None:
        return target
    return max(0.0, target - curr_stock)

# ===== 主函数：按“食材名称”汇总全表 =====
def compute_stats(records: pd.DataFrame) -> pd.DataFrame:
    """
    输入：‘购入/剩余’明细 DataFrame（含中文列名）
    输出：每个 item 的统计表，包含：
        食材名称 (Item Name), 当前库存, 平均最近两周使用量, 预计还能用天数, 计算下次采购量,
        最近统计剩余日期, 最近采购日期, 最近采购数量, 最近采购单价,
        平均采购间隔(天), 累计支出
    """
    if records is None or records.empty:
        return pd.DataFrame(columns=[
            "食材名称 (Item Name)", "当前库存", "平均最近两周使用量", "预计还能用天数", "计算下次采购量",
            "最近统计剩余日期", "最近采购日期", "最近采购数量", "最近采购单价",
            "平均采购间隔(天)", "累计支出"
        ])

    df = records.copy()
    # 基本清洗（防止类型异常）
    if "日期 (Date)" in df.columns:
        df["日期 (Date)"] = pd.to_datetime(df["日期 (Date)"], errors="coerce")
    for col in ["数量 (Qty)", "单价 (Unit Price)", "总价 (Total Cost)"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

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

        last_buy_qty = float(last_buy["数量 (Qty)"]) if last_buy is not None and pd.notna(last_buy["数量 (Qty)"]) else None
        last_buy_price = float(last_buy["单价 (Unit Price)"]) if last_buy is not None and pd.notna(last_buy["单价 (Unit Price)"]) else None
        total_spend = float(buys["总价 (Total Cost)"].sum()) if "总价 (Total Cost)" in g.columns else None

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
    # 让紧急的更靠前（预计还能用天数小的排前）
    if not res.empty:
        res = res.sort_values(["预计还能用天数", "食材名称 (Item Name)"],
                              ascending=[True, True], na_position="last").reset_index(drop=True)
    return res
