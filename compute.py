# -*- coding: utf-8 -*-
from __future__ import annotations

import pandas as pd
from typing import Tuple, Optional

# =============== 基础小工具 ===============

def _latest_remainder_row(item_df: pd.DataFrame) -> Optional[pd.Series]:
    """最近一条‘剩余’记录。"""
    rem = item_df[item_df["状态 (Status)"] == "剩余"].sort_values("日期 (Date)")
    if rem.empty:
        return None
    return rem.iloc[-1]

def _last_buy_row(item_df: pd.DataFrame) -> Optional[pd.Series]:
    """最近一条‘买入’记录。"""
    buy = item_df[item_df["状态 (Status)"] == "买入"].sort_values("日期 (Date)")
    if buy.empty:
        return None
    return buy.iloc[-1]

def _avg_buy_interval_days(item_df: pd.DataFrame) -> float:
    """平均采购间隔（天）。按不同的买入日期算相邻差分的平均。"""
    buy_dates = (
        item_df[item_df["状态 (Status)"] == "买入"]["日期 (Date)"]
        .dropna()
        .drop_duplicates()
        .sort_values()
        .tolist()
    )
    if len(buy_dates) < 2:
        return 0.0
    diffs = [(buy_dates[i] - buy_dates[i - 1]).days for i in range(1, len(buy_dates))]
    return float(pd.Series(diffs).mean())

# =============== 两周用量（新版算法） ===============

def _recent_usage_14d_robust(item_df: pd.DataFrame) -> Tuple[float, float, Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    """
    返回：(两周用量, 当前库存, 最近统计剩余日期E, 基线日期S)
    规则：
      - E = 最近一条‘剩余’的日期；
      - S = E-14天附近的基线‘剩余’（优先窗口内最早；否则窗口外最近的前一条）；
      - 若 [S, E] 内出现‘剩余’上涨且其间没有‘买入’，视为漏记进货，基线重置到“上涨后的那条剩余”；
      - 消耗 = 基线剩余 + 区间内买入总量 - 终点剩余；
      - 按天数折算 14 天（D<=0 时不折算）。
    """
    if item_df.empty:
        return 0.0, 0.0, None, None

    df = item_df.sort_values("日期 (Date)").copy()
    rem = df[df["状态 (Status)"] == "剩余"][["日期 (Date)", "数量 (Qty)"]].dropna()
    buy = df[df["状态 (Status)"] == "买入"][["日期 (Date)", "数量 (Qty)"]].dropna()

    if rem.empty:
        return 0.0, 0.0, None, None

    # 终点 E
    end_row = rem.iloc[-1]
    end_date = pd.to_datetime(end_row["日期 (Date)"])
    end_stock = float(end_row["数量 (Qty)"])

    # 目标起点 E - 14 天
    target_start = end_date - pd.Timedelta(days=14)

    # 选择 S：窗口内最早；否则取窗口外最近的前一条
    rem_in_win = rem[(rem["日期 (Date)"] >= target_start) & (rem["日期 (Date)"] <= end_date)]
    if not rem_in_win.empty:
        base_row = rem_in_win.iloc[0]
    else:
        rem_before = rem[rem["日期 (Date)"] < target_start]
        if not rem_before.empty:
            base_row = rem_before.iloc[-1]
        else:
            # 没有更早记录，就只能从第一条剩余算起（防止空）
            base_row = rem.iloc[0]

    base_date = pd.to_datetime(base_row["日期 (Date)"])
    base_stock = float(base_row["数量 (Qty)"])

    # 检测 [S, E] 中是否有“剩余上涨但无买入”的情况，若有，重置基线到上涨后的那条
    rem_window = rem[(rem["日期 (Date)"] >= base_date) & (rem["日期 (Date)"] <= end_date)].reset_index(drop=True)
    if len(rem_window) >= 2:
        for i in range(1, len(rem_window)):
            pre = rem_window.loc[i - 1]
            cur = rem_window.loc[i]
            if float(cur["数量 (Qty)"]) > float(pre["数量 (Qty)"]):
                # 检查两条剩余之间是否有买入
                seg_buy = buy[(buy["日期 (Date)"] > pre["日期 (Date)"]) & (buy["日期 (Date)"] <= cur["日期 (Date)"])]
                if seg_buy.empty:
                    # 无买入但库存上涨 => 漏记买入；基线重置为上涨后的这一条
                    base_date = pd.to_datetime(cur["日期 (Date)"])
                    base_stock = float(cur["数量 (Qty)"])

    # 计算区间内消耗：开库存 + 买入 - 期末库存
    buy_sum = 0.0
    if not buy.empty:
        buy_sum = float(
            buy[(buy["日期 (Date)"] > base_date) & (buy["日期 (Date)"] <= end_date)]["数量 (Qty)"].sum()
        )

    consumption = (base_stock if pd.notna(base_stock) else 0.0) + buy_sum - (end_stock if pd.notna(end_stock) else 0.0)
    if consumption < 0:
        consumption = 0.0  # 数据噪声保护

    days = max((end_date - base_date).days, 0)
    if days > 0:
        use14 = consumption * 14.0 / days
    else:
        use14 = consumption  # 同日，无法折算，只能用当日值（一般为0）

    return float(use14), float(end_stock), end_date, base_date

# =============== 汇总统计 ===============

def compute_stats(df: pd.DataFrame) -> pd.DataFrame:
    """
    输入：整表 DataFrame（已清洗）
    输出：每个物品的统计行，包含：
      - 食材名称 (Item Name)
      - 当前库存
      - 平均最近两周使用量
      - 预计还能用天数
      - 计算下次采购量（= 两周目标用量 - 当前库存，目标用量取“最近两周用量”）
      - 最近统计剩余日期
      - 最近采购日期
      - 平均采购间隔(天)
      - 最近采购数量 / 最近采购单价
      - 累计支出
    """
    if df.empty:
        cols = [
            "食材名称 (Item Name)", "当前库存", "平均最近两周使用量", "预计还能用天数",
            "计算下次采购量", "最近统计剩余日期", "最近采购日期",
            "平均采购间隔(天)", "最近采购数量", "最近采购单价", "累计支出"
        ]
        return pd.DataFrame(columns=cols)

    out_rows = []

    for item, item_df in df.groupby("食材名称 (Item Name)"):
        item_df = item_df.copy().sort_values("日期 (Date)")

        # 两周用量（新版）
        use14, cur_stock, end_date, base_date = _recent_usage_14d_robust(item_df)

        # 预计还能用天数
        if use14 and use14 > 0:
            days_left = cur_stock / (use14 / 14.0)
        else:
            days_left = float("nan")

        # 下次采购量（两周目标量 - 当前库存；目标量取 use14）
        target14 = use14 if use14 is not None else 0.0
        next_buy_qty = max(0.0, target14 - cur_stock)

        # 最近采购
        last_buy = _last_buy_row(item_df)
        last_buy_date = last_buy["日期 (Date)"].date().isoformat() if last_buy is not None and pd.notna(last_buy["日期 (Date)"]) else None
        last_buy_qty = float(last_buy["数量 (Qty)"]) if last_buy is not None and pd.notna(last_buy["数量 (Qty)"]) else None
        last_buy_price = float(last_buy["单价 (Unit Price)"]) if last_buy is not None and pd.notna(last_buy["单价 (Unit Price)"]) else None

        # 平均采购间隔
        avg_buy_interval = _avg_buy_interval_days(item_df)

        # 累计支出
        total_spend = item_df.loc[item_df["状态 (Status)"] == "买入", "总价 (Total Cost)"].sum(min_count=1)
        total_spend = float(total_spend) if pd.notna(total_spend) else 0.0

        out_rows.append({
            "食材名称 (Item Name)": item,
            "当前库存": float(cur_stock) if pd.notna(cur_stock) else None,
            "平均最近两周使用量": float(use14) if pd.notna(use14) else 0.0,
            "预计还能用天数": float(days_left) if pd.notna(days_left) else None,
            "计算下次采购量": float(next_buy_qty),
            "最近统计剩余日期": end_date.date().isoformat() if end_date is not None else None,
            "最近采购日期": last_buy_date,
            "平均采购间隔(天)": float(avg_buy_interval) if pd.notna(avg_buy_interval) else 0.0,
            "最近采购数量": last_buy_qty,
            "最近采购单价": last_buy_price,
            "累计支出": total_spend,
        })

    result = pd.DataFrame(out_rows)

    # 排序：可按预计还能用天数升序，把紧急项放前
    if not result.empty and "预计还能用天数" in result.columns:
        result = result.sort_values(
            by=["预计还能用天数", "食材名称 (Item Name)"],
            ascending=[True, True],
            na_position="last"
        ).reset_index(drop=True)

    return result
