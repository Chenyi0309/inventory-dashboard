# -*- coding: utf-8 -*-
from __future__ import annotations
import pandas as pd
import numpy as np

# --------- 小工具 ---------

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

# --------- 关键口径：按你新规则计算“最近两周使用量” ---------

def _recent_usage_14d_new(item_df: pd.DataFrame) -> float:
    """
    以【最近一条“剩余”记录的日期】为窗口右端点，向前14天。
    段的构造与计量规则：
      - pair(买入 -> 剩余) 且中间无买入：用量 = 买入量 - 剩余量
      - pair(剩余 -> 剩余)：若后 >= 前 视为漏记买入 → 丢弃并从后一个重置；若后 < 前：用量 = 前 - 后
      - 每个 pair 的用量按区间天数均分，并乘以与14天窗口的相交天数
    起点选择（用于构造第一段）：
      1) 若窗口起点当天或更早有“剩余”，取“<= 起点”的最近一条；
      2) 否则取窗口内第一条“剩余”；
      3) 若窗口内没有，取窗口外最近的一条“剩余”。
    """
    df = item_df.sort_values("日期 (Date)").copy()
    df = df.dropna(subset=["日期 (Date)"])

    # 只在有“剩余”记录时才有意义
    latest_rem = _latest_remainder_row(df)
    if latest_rem is None:
        return 0.0

    window_end = pd.Timestamp(latest_rem["日期 (Date)"])
    window_start = window_end - pd.Timedelta(days=14)

    # 只保留“买入/剩余”两个状态的必要列
    core = df.loc[df["状态 (Status)"].isin(["买入", "剩余"]),
                  ["日期 (Date)", "状态 (Status)", "数量 (Qty)"]].copy()
    core["数量 (Qty)"] = pd.to_numeric(core["数量 (Qty)"], errors="coerce")
    core = core.dropna(subset=["数量 (Qty)"])
    core = core.sort_values("日期 (Date)").reset_index(drop=True)
    if core.empty:
        return 0.0

    # ---- 寻找起始锚点（一个“剩余”点）----
    rem_only = core[core["状态 (Status)"]=="剩余"].copy()
    if rem_only.empty:
        return 0.0

    # 1) 窗口起点当天或更早的最近一条
    r0 = rem_only[rem_only["日期 (Date)"] <= window_start]
    if not r0.empty:
        anchor = r0.iloc[-1]
    else:
        # 2) 窗口内最早一条
        r1 = rem_only[(rem_only["日期 (Date)"] > window_start) & (rem_only["日期 (Date)"] <= window_end)]
        if not r1.empty:
            anchor = r1.iloc[0]
        else:
            # 3) 窗口外最近一条（离起点最近的）
            before = rem_only[rem_only["日期 (Date)"] < window_start]
            after  = rem_only[rem_only["日期 (Date)"] > window_end]
            if before.empty and after.empty:
                anchor = rem_only.iloc[-1]
            elif before.empty:
                anchor = after.iloc[0]
            elif after.empty:
                anchor = before.iloc[-1]
            else:
                # 选离窗口最近的一个
                if (window_start - before.iloc[-1]["日期 (Date)"]) <= (after.iloc[0]["日期 (Date)"] - window_end):
                    anchor = before.iloc[-1]
                else:
                    anchor = after.iloc[0]

    # 构造“事件序列”：把 anchor 放进队列（确保从它开始）
    # 拿窗口起点之后、窗口终点之前的所有事件；再把 anchor 之前的事件最后一个放进来，避免断层
    events = core[(core["日期 (Date)"] >= anchor["日期 (Date)"]) &
                  (core["日期 (Date)"] <= window_end)].copy()

    # 若 anchor 不是 events 的第一行，则把 anchor 插到最前
    if events.empty or events.iloc[0]["日期 (Date)"] > anchor["日期 (Date)"]:
        events = pd.concat([pd.DataFrame([anchor]), events], ignore_index=True)

    # --- 遍历相邻事件，构造 pair 并计入窗口 ---
    total_usage = 0.0
    i = 0
    while i < len(events) - 1:
        cur = events.iloc[i]
        nxt = events.iloc[i+1]

        cur_dt = pd.Timestamp(cur["日期 (Date)"])
        nxt_dt = pd.Timestamp(nxt["日期 (Date)"])
        if nxt_dt <= cur_dt:
            i += 1
            continue

        seg_days = (nxt_dt - cur_dt).days
        if seg_days <= 0:
            i += 1
            continue

        # 计算该 pair 的“基础用量”（未考虑窗口权重）
        used = None
        if cur["状态 (Status)"] == "买入" and nxt["状态 (Status)"] == "剩余":
            # 中间无买入的保证：我们是逐对遍历，若中间又遇到买入，必然形成“买入→买入/剩余”的下一对
            used = max(float(cur["数量 (Qty)"]) - float(nxt["数量 (Qty)"]), 0.0)
        elif cur["状态 (Status)"] == "剩余" and nxt["状态 (Status)"] == "剩余":
            cur_q = float(cur["数量 (Qty)"]); nxt_q = float(nxt["数量 (Qty)"])
            if nxt_q >= cur_q:
                # 视为中间漏记买入：丢弃这段 & 从下一条重置
                i += 1
                continue
            else:
                used = cur_q - nxt_q
        else:
            # 买入 -> 买入 或 其他组合：这段不计用量（相当于重置/补货）
            i += 1
            continue

        # 计算与 14 天窗口的重叠天数（窗口是 [window_start, window_end] ）
        overlap_start = max(cur_dt, window_start)
        overlap_end = min(nxt_dt, window_end)
        overlap = (overlap_end - overlap_start).days
        if overlap > 0 and used is not None and seg_days > 0:
            per_day = used / seg_days
            total_usage += per_day * overlap

        i += 1

    return round(float(total_usage), 4)

# --------- 汇总计算 ---------

def compute_stats(df: pd.DataFrame, today: pd.Timestamp | None = None) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=[
            "食材名称 (Item Name)","当前库存","平均最近两周使用量","预计还能用天数",
            "计算下次采购量","最近统计剩余日期","最近采购日期","平均采购间隔(天)",
            "最近采购数量","最近采购单价","累计支出"
        ])

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

        # —— 使用你新规则计算最近两周使用量 ——
        use_14 = _recent_usage_14d_new(item_df)

        # 预计还能用天数 = 当前库存 ÷ (两周用量/14)
        daily = (use_14 / 14.0) if use_14 and use_14 > 0 else np.nan
        days_left = (current_stock / daily) if (daily and daily > 0 and not np.isnan(current_stock)) else np.nan

        # 计算下次采购量 = 两周目标用量 − 当前库存（<0 记 0）
        target_14 = use_14
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
    if not res.empty and "预计还能用天数" in res.columns:
        res = res.sort_values(by="预计还能用天数", na_position="last")
    return res.reset_index(drop=True)
