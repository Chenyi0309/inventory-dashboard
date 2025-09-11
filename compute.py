# -*- coding: utf-8 -*-
from __future__ import annotations

import pandas as pd
from typing import Optional, Tuple

def _last_of(item_df: pd.DataFrame, status: str) -> Optional[pd.Series]:
    d = item_df[item_df["状态 (Status)"] == status].sort_values("日期 (Date)")
    if d.empty:
        return None
    return d.iloc[-1]

def _recent_two_remainders(item_df: pd.DataFrame) -> Tuple[Optional[pd.Series], Optional[pd.Series]]:
    d = item_df[item_df["状态 (Status)"] == "剩余"].sort_values("日期 (Date)")
    if len(d) < 2:
        return None, d.iloc[-1] if len(d)==1 else None
    return d.iloc[-2], d.iloc[-1]

def estimate_14d_use(item_df: pd.DataFrame) -> Optional[float]:
    """估算最近14天平均使用量（不需要“使用”事件，靠买入/剩余推算）。"""
    # 方案一：如果有两次剩余，且最近一次 <= 上一次，且两次之间无买入，则用这两次差值 / 天数 * 14
    prev_r, last_r = _recent_two_remainders(item_df)
    if prev_r is not None and last_r is not None:
        between = item_df[(item_df["日期 (Date)"] > prev_r["日期 (Date)"]) & (item_df["日期 (Date)"] <= last_r["日期 (Date)"])]
        has_buy_between = (between["状态 (Status)"] == "买入").any()
        days = max(1, (last_r["日期 (Date)"] - prev_r["日期 (Date)"]).days)
        if (not has_buy_between) and last_r["数量 (Qty)"] <= prev_r["数量 (Qty)"]:
            used = prev_r["数量 (Qty)"] - last_r["数量 (Qty)"]
            return float(used) / days * 14.0 if days > 0 else None

    # 方案二：找最近一次买入到最近一次剩余的区间
    last_buy = _last_of(item_df, "买入")
    last_rem = _last_of(item_df, "剩余")
    if last_buy is not None and last_rem is not None and last_rem["日期 (Date)"] >= last_buy["日期 (Date)"]:
        days = max(1, (last_rem["日期 (Date)"] - last_buy["日期 (Date)"]).days)
        used = max(0.0, float(last_buy["数量 (Qty)"]) - float(last_rem["数量 (Qty)"]))
        return float(used) / days * 14.0 if days > 0 else None

    return None

def compute_current_stock(item_df: pd.DataFrame) -> Optional[float]:
    """当前库存 = 最近一次‘剩余’数量"""
    last_r = _last_of(item_df, "剩余")
    return float(last_r["数量 (Qty)"]) if last_r is not None else None

def compute_days_left(curr_stock: Optional[float], use_14d: Optional[float]) -> Optional[float]:
    if curr_stock is None or use_14d is None or use_14d <= 0:
        return None
    daily = use_14d / 14.0
    return curr_stock / daily if daily > 0 else None

def compute_next_purchase_qty(curr_stock: Optional[float], use_14d: Optional[float]) -> Optional[float]:
    """建议下次采购量：补足到14天的用量"""
    if use_14d is None:
        return None
    target = use_14d
    if curr_stock is None:
        return target
    return max(0.0, target - curr_stock)

def compute_item_table(records: pd.DataFrame, category: str) -> pd.DataFrame:
    """按选定大类，输出每个 item 的统计。"""
    df = records.copy()
    df = df[df["分类 (Category)"] == category]
    if df.empty:
        return pd.DataFrame(columns=[
            "食材名称 (Item Name)","当前库存","平均最近两周使用量","预计还能用天数",
            "计算下次采购量","最近统计剩余日期","最近采购日期","平均采购间隔(天)",
            "最近采购数量","最近采购单价","累计支出","库存预警"
        ])

    out = []
    for item, g in df.groupby("食材名称 (Item Name)"):
        g = g.sort_values("日期 (Date)")
        curr_stock = compute_current_stock(g)
        use_14d = estimate_14d_use(g)
        days_left = compute_days_left(curr_stock, use_14d)
        next_buy = compute_next_purchase_qty(curr_stock, use_14d)

        last_rem = _last_of(g, "剩余")
        last_buy = _last_of(g, "买入")

        # 平均采购间隔（买入事件之间的平均天数）
        buys = g[g["状态 (Status)"] == "买入"].sort_values("日期 (Date)")
        if len(buys) >= 2:
            gaps = buys["日期 (Date)"].diff().dropna().dt.days
            avg_gap = float(gaps.mean()) if not gaps.empty else None
        else:
            avg_gap = None

        last_buy_qty = float(last_buy["数量 (Qty)"]) if last_buy is not None else None
        last_buy_price = float(last_buy["单价 (Unit Price)"]) if last_buy is not None else None
        total_spend = float(buys["总价 (Total Cost)"].sum()) if "总价 (Total Cost)" in g.columns else None

        warn = None
        if days_left is not None:
            warn = "⚠️ ≤3天" if days_left <= 3 else ("⏳ ≤7天" if days_left <= 7 else "✅ 正常")

        out.append({
            "食材名称 (Item Name)": item,
            "当前库存": curr_stock,
            "平均最近两周使用量": use_14d,
            "预计还能用天数": days_left,
            "计算下次采购量": next_buy,
            "最近统计剩余日期": (last_rem["日期 (Date)"] if last_rem is not None else None),
            "最近采购日期": (last_buy["日期 (Date)"] if last_buy is not None else None),
            "平均采购间隔(天)": avg_gap,
            "最近采购数量": last_buy_qty,
            "最近采购单价": last_buy_price,
            "累计支出": total_spend,
            "库存预警": warn,
        })
    res = pd.DataFrame(out)
    # 紧急度排序
    if not res.empty:
        res = res.sort_values(["库存预警","预计还能用天数"], na_position="last").reset_index(drop=True)
    return res
