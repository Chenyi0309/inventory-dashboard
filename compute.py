# -*- coding: utf-8 -*-
from __future__ import annotations
import pandas as pd
import numpy as np

# Core inventory computations that mimic your '库存统计' sheet.
# Input: df with columns from gsheet.read_records()
# Output: per-item stats DataFrame

def _latest_remainder_row(item_df: pd.DataFrame):
    rem = item_df[item_df["状态 (Status)"] == "剩余"].copy()
    if rem.empty:
        return None
    rem = rem.sort_values("日期 (Date)")
    return rem.iloc[-1]

def _last_buy_row(item_df: pd.DataFrame):
    buy = item_df[item_df["状态 (Status)"] == "买入"].copy()
    if buy.empty:
        return None
    buy = buy.sort_values("日期 (Date)")
    return buy.iloc[-1]

def _avg_buy_interval_days(item_df: pd.DataFrame):
    buy = item_df[item_df["状态 (Status)"] == "买入"].copy()
    if len(buy) < 2:
        return np.nan
    buy = buy.sort_values("日期 (Date)")
    deltas = buy["日期 (Date)"].diff().dt.days.dropna()
    if deltas.empty:
        return np.nan
    return deltas.mean()

def _recent_usage_14d(item_df: pd.DataFrame, now_ts: pd.Timestamp) -> float:
    """Estimate total usage in last 14 days.
    Rules:
    - Use pairs with no intermediate '买入' between them.
    - Sources of pairs:
       (a) '买入' -> later '剩余'
       (b) '剩余' -> later '剩余'
    - If later 剩余 > earlier 剩余 and there is no 买入 in between, ignore the earlier 剩余
      (treat as baseline correction).
    - Only count pairs where the interval overlaps the last 14 days. We weight by overlap days.
    Return the 14-day total usage (not per-day).
    """
    df = item_df.sort_values("日期 (Date)")[["日期 (Date)","状态 (Status)","数量 (Qty)"]].dropna(subset=["日期 (Date)"])
    if df.empty:
        return 0.0

    # Build segments with no buy in between
    segments = []
    rows = df.to_dict("records")
    # We'll look at consecutive records; when we hit a 买入, we start a new baseline.
    for i in range(len(rows)-1):
        cur, nxt = rows[i], rows[i+1]
        # skip if any missing
        if cur["数量 (Qty)"] is None or nxt["数量 (Qty)"] is None:
            continue
        # pair only if no buy between them; since we use consecutive rows, this holds
        if cur["状态 (Status)"] == "买入" and nxt["状态 (Status)"] == "剩余":
            start_qty = float(cur["数量 (Qty)"])
            end_qty   = float(nxt["数量 (Qty)"])
            start_dt  = cur["日期 (Date)"]
            end_dt    = nxt["日期 (Date)"]
            if end_dt <= start_dt:
                continue
            used = max(start_qty - end_qty, 0.0)
            segments.append((start_dt, end_dt, used))
        elif cur["状态 (Status)"] == "剩余" and nxt["状态 (Status)"] == "剩余":
            start_qty = float(cur["数量 (Qty)"])
            end_qty   = float(nxt["数量 (Qty)"])
            start_dt  = cur["日期 (Date)"]
            end_dt    = nxt["日期 (Date)"]
            if end_dt <= start_dt:
                continue
            # if remainder increased and no buy, ignore this pair (baseline correction)
            if end_qty >= start_qty:
                # treat as correction → don't compute usage for this interval
                continue
            used = start_qty - end_qty
            segments.append((start_dt, end_dt, used))

    if not segments:
        return 0.0

    window_end = pd.to_datetime(now_ts).normalize() + pd.Timedelta(days=1)  # include today
    window_start = window_end - pd.Timedelta(days=14)

    # Weight segment usage by the overlap with last 14 days
    total_usage_in_window = 0.0
    for s_dt, e_dt, used in segments:
        s = pd.Timestamp(s_dt)
        e = pd.Timestamp(e_dt)
        seg_days = (e - s).days
        if seg_days <= 0:
            continue
        # Overlap
        overlap_start = max(s, window_start)
        overlap_end   = min(e, window_end)
        overlap_days = (overlap_end - overlap_start).days
        if overlap_days <= 0:
            continue
        # per-day usage * overlap_days
        per_day = used / seg_days if seg_days > 0 else 0.0
        total_usage_in_window += per_day * overlap_days

    return round(float(total_usage_in_window), 4)

def compute_stats(df: pd.DataFrame, today: pd.Timestamp | None = None) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=[
            "食材名称 (Item Name)","当前库存","平均最近两周使用量","预计还能用天数",
            "计算下次采购量","最近统计剩余日期","最近采购日期","平均采购间隔(天)",
            "最近采购数量","最近采购单价","累计支出"
        ])

    today = pd.to_datetime(today) if today is not None else pd.Timestamp.today()
    out_rows = []
    for item, item_df in df.groupby("食材名称 (Item Name)"):
        item_df = item_df.copy()
        latest_rem = _latest_remainder_row(item_df)
        last_buy   = _last_buy_row(item_df)

        current_stock = float(latest_rem["数量 (Qty)"]) if latest_rem is not None else np.nan
        last_rem_date = latest_rem["日期 (Date)"] if latest_rem is not None else pd.NaT

        last_buy_date = last_buy["日期 (Date)"] if last_buy is not None else pd.NaT
        last_buy_qty  = float(last_buy["数量 (Qty)"]) if last_buy is not None else np.nan
        last_buy_price = float(last_buy["单价 (Unit Price)"]) if last_buy is not None else np.nan

        # usage in last 14 days (total, not per-day)
        use_14 = _recent_usage_14d(item_df, today)

        # daily usage estimate
        daily = use_14 / 14.0 if use_14 > 0 else np.nan
        days_left = (current_stock / daily) if (daily and daily > 0 and not np.isnan(current_stock)) else np.nan

        # next purchase amount (try to keep 14 days of supply)
        target_14 = use_14  # 14-day demand
        next_buy_qty = max(target_14 - (current_stock if not np.isnan(current_stock) else 0), 0) if target_14 > 0 else 0

        avg_interval = _avg_buy_interval_days(item_df)

        total_spend = item_df.loc[item_df["状态 (Status)"] == "买入", "总价 (Total Cost)"].sum(min_count=1)

        out_rows.append({
            "食材名称 (Item Name)": item,
            "当前库存": round(current_stock, 4) if not np.isnan(current_stock) else "",
            "平均最近两周使用量": round(use_14, 4) if use_14 else 0,
            "预计还能用天数": round(days_left, 2) if days_left and not np.isnan(days_left) else "",
            "计算下次采购量": round(next_buy_qty, 2),
            "最近统计剩余日期": last_rem_date,
            "最近采购日期": last_buy_date,
            "平均采购间隔(天)": round(avg_interval, 2) if avg_interval == avg_interval else "",
            "最近采购数量": round(last_buy_qty, 4) if last_buy_qty == last_buy_qty else "",
            "最近采购单价": round(last_buy_price, 4) if last_buy_price == last_buy_price else "",
            "累计支出": round(total_spend, 2) if total_spend == total_spend else ""
        })

    res = pd.DataFrame(out_rows)
    # Sort by days left ascending (items running out first)
    if not res.empty and "预计还能用天数" in res.columns:
        res = res.sort_values(by="预计还能用天数", na_position="last")
    return res.reset_index(drop=True)
