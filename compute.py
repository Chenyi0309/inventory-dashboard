# -*- coding: utf-8 -*-
from __future__ import annotations

import pandas as pd
from typing import Optional, Tuple, Dict, List

# ========= 列名适配：自动识别多种中/英文/有无括号的表头 =========
ALIASES: Dict[str, List[str]] = {
    "日期 (Date)": ["日期 (Date)", "日期", "Date", "date"],
    "食材名称 (Item Name)": [
        "食材名称 (Item Name)", "食材名称", "Item Name", "item name", "物品名", "名称"
    ],
    "分类 (Category)": ["分类 (Category)", "分类", "Category", "category", "类型"],
    "数量 (Qty)": ["数量 (Qty)", "数量", "Qty", "qty", "数量(个)"],
    "单位 (Unit)": ["单位 (Unit)", "单位", "Unit", "unit"],
    "单价 (Unit Price)": ["单价 (Unit Price)", "单价", "Unit Price", "price", "unit price"],
    "总价 (Total Cost)": ["总价 (Total Cost)", "总价", "Total Cost", "amount", "cost"],
    "状态 (Status)": ["状态 (Status)", "状态", "Status", "status"],
    "备注 (Notes)": ["备注 (Notes)", "备注", "Notes", "notes"],
}

def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    cols = {c: str(c).strip() for c in df.columns}
    df = df.rename(columns=cols)
    found = {}
    lower_map = {c.lower(): c for c in df.columns}
    for std, alts in ALIASES.items():
        pick = None
        for a in alts:
            # 既匹配原样，也匹配不区分大小写
            if a in df.columns:
                pick = a; break
            if a.lower() in lower_map:
                pick = lower_map[a.lower()]; break
        if pick:
            found[pick] = std
    # 真正重命名
    df = df.rename(columns=found)
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

# ========= 14 天用量估算（稳健版） =========
def _recent_usage_14d_new(item_df: pd.DataFrame) -> Optional[float]:
    """
    估算最近14天使用量：只用【买入/剩余】两类记录。
    A) 有相邻两次“剩余”，中间无“买入”，且库存下降 → (差值/天数)*14
    B) 否则 用 最近一次“买入”→最近一次“剩余” 的 (买入量-剩余量)/天数*14
    """
    if item_df.empty or "日期 (Date)" not in item_df or "状态 (Status)" not in item_df:
        return None

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

# 兼容旧名
_recent_usage_14d_robust = _recent_usage_14d_new

def _current_stock(g: pd.DataFrame) -> Optional[float]:
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
    """
    输入：‘购入/剩余’明细 DataFrame（列名可中英文/不同写法）
    输出：每个 item 的统计表
    """
    if records is None or records.empty:
        return pd.DataFrame(columns=[
            "食材名称 (Item Name)", "当前库存", "平均最近两周使用量", "预计还能用天数", "计算下次采购量",
            "最近统计剩余日期", "最近采购日期", "最近采购数量", "最近采购单价",
            "平均采购间隔(天)", "累计支出"
        ])

    df = _normalize_columns(records.copy())

    # 基础字段兜底检测
    must_have = ["食材名称 (Item Name)", "日期 (Date)", "状态 (Status)", "数量 (Qty)"]
    missing = [c for c in must_have if c not in df.columns]
    if missing:
        # 返回空表 + 让前端显示“分类未识别”那条信息
        return pd.DataFrame(columns=[
            "食材名称 (Item Name)", "当前库存", "平均最近两周使用量", "预计还能用天数", "计算下次采购量",
            "最近统计剩余日期", "最近采购日期", "最近采购数量", "最近采购单价",
            "平均采购间隔(天)", "累计支出"
        ])

    # 类型清洗
    df["日期 (Date)"] = pd.to_datetime(df["日期 (Date)"], errors="coerce")
    for col in ["数量 (Qty)", "单价 (Unit Price)", "总价 (Total Cost)"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "分类 (Category)" in df.columns:
        df["分类 (Category)"] = df["分类 (Category)"].astype(str).str.strip()

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
    if not res.empty:
        res = res.sort_values(["预计还能用天数", "食材名称 (Item Name)"],
                              ascending=[True, True], na_position="last").reset_index(drop=True)
    return res
