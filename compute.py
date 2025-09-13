# compute.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import Tuple, Optional
import pandas as pd
import numpy as np

# =============== 列名规范化（唯一实现；app.py 也复用它） ===============
_NBSP = "\xa0"
_FULL_L = "（"
_FULL_R = "）"

_CANONICAL = {
    # 规范名 -> 各种可能的别名（宽松匹配）
    "日期 (Date)": [
        "日期", "日期(date)", "date", "Date", "日期 ", " 日 期 ", "日期" + _NBSP,
    ],
    "食材名称 (Item Name)": [
        "食材名称", "食材", "名称", "item name", "item", "品名", "Item Name", "食材名称 " + _NBSP,
    ],
    "分类 (Category)": ["分类", "类别", "category", "Category"],
    "数量 (Qty)": ["数量", "qty", "数量 (qty)", "数量 " + _NBSP, "Qty"],
    "单位 (Unit)": ["单位", "unit", "Unit"],
    "单价 (Unit Price)": ["单价", "unit price", "价格/单价", "单价(元)", "Unit Price"],
    "总价 (Total Cost)": ["总价", "total", "total cost", "金额", "总价(元)", "Total Cost"],
    "状态 (Status)": ["状态", "status", "Status"],
    "Notes": ["备注", "notes", "note", "Notes"],
}

_CANON_KEYS = list(_CANONICAL.keys())
_FLAT_MAP = {k.lower(): k for k in _CANON_KEYS}
for canon, alts in _CANONICAL.items():
    for a in alts:
        _FLAT_MAP[a.lower()] = canon


def _clean_col_token(s: str) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace(_NBSP, " ").replace(_FULL_L, "(").replace(_FULL_R, ")")
    s = re.sub(r"\s+", " ", s.strip())
    return s


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """把各类中/英/含空格/全角括号的列名统一到规范名；数值列做基础清洗。"""
    if df is None or len(df) == 0:
        return df.copy()

    cols = []
    for c in df.columns:
        c0 = _clean_col_token(c)
        canon = _FLAT_MAP.get(c0.lower())
        cols.append(canon if canon else c0)
    out = df.copy()
    out.columns = cols

    # 统一日期
    if "日期 (Date)" in out.columns:
        out["日期 (Date)"] = pd.to_datetime(out["日期 (Date)"], errors="coerce")

    # 数值列：支持 "30%" -> 0.3；"1,234.50" -> 1234.5
    for num_col in ["数量 (Qty)", "单价 (Unit Price)", "总价 (Total Cost)"]:
        if num_col in out.columns:
            s = (
                out[num_col]
                .astype(str)
                .str.replace(",", "", regex=False)
                .str.strip()
            )
            is_pct = s.str.endswith("%", na=False)
            s_num = pd.to_numeric(s.str.rstrip("%"), errors="coerce")
            s_num = np.where(is_pct, s_num / 100.0, s_num)
            out[num_col] = pd.to_numeric(s_num, errors="coerce")

    # 状态字段收敛
    if "状态 (Status)" in out.columns:
        s = out["状态 (Status)"].astype(str).str.strip().str.lower()
        s = s.replace({"buy": "买入", "purchase": "买入", "剩余量": "剩余", "remain": "剩余"})
        # 保留原中文
        out["状态 (Status)"] = np.where(s.str.contains("买"), "买入",
                                 np.where(s.str.contains("余"), "剩余", out["状态 (Status)"]))
    return out


# ===================== 统计核心 =====================

def _last_remainder(df_item: pd.DataFrame) -> Tuple[Optional[pd.Timestamp], Optional[float]]:
    """同一物品最近一次“剩余”的日期与数量。"""
    d = df_item[df_item["状态 (Status)"] == "剩余"].sort_values("日期 (Date)")
    if len(d) == 0:
        return None, None
    row = d.iloc[-1]
    return row["日期 (Date)"], float(row["数量 (Qty)"]) if pd.notna(row["数量 (Qty)"]) else None


def _last_purchase_info(df_item: pd.DataFrame) -> Tuple[Optional[pd.Timestamp], Optional[float], Optional[float], Optional[float], Optional[float]]:
    """最近一次买入的日期/数量/单价，以及平均采购间隔（天）与累计支出。"""
    b = df_item[df_item["状态 (Status)"] == "买入"].sort_values("日期 (Date)")
    if len(b) == 0:
        return None, None, None, None, 0.0
    last = b.iloc[-1]
    # 平均采购间隔
    avg_int = None
    if len(b) >= 2:
        diffs = b["日期 (Date)"].diff().dt.days.dropna()
        if len(diffs):
            avg_int = float(np.mean(diffs))
    total_spend = b.get("总价 (Total Cost)")
    total_spend = float(total_spend.sum()) if total_spend is not None else 0.0
    return (
        last["日期 (Date)"],
        float(last["数量 (Qty)"]) if pd.notna(last["数量 (Qty)"]) else None,
        float(last.get("单价 (Unit Price)", np.nan)) if pd.notna(last.get("单价 (Unit Price)", np.nan)) else None,
        float(avg_int) if avg_int is not None else None,
        total_spend
    )


def _recent_usage_14d(df_item: pd.DataFrame, asof: Optional[pd.Timestamp]) -> Optional[float]:
    """
    估算最近两周使用量（优先：两次连续“剩余”且后一次 <= 前一次；否则：最近“买入”到最近“剩余”）。
    返回14天总用量（不是日均）。
    """
    x = df_item.sort_values("日期 (Date)")

    # 1) 连续两次“剩余”，中间没有“买入”
    r = x[x["状态 (Status)"] == "剩余"][["日期 (Date)", "数量 (Qty)"]].dropna().reset_index(drop=True)
    if len(r) >= 2:
        r_prev = r.iloc[-2]
        r_last = r.iloc[-1]
        # 检查两次之间是否存在买入
        between = x[(x["日期 (Date)"] > r_prev["日期 (Date)"]) & (x["日期 (Date)"] <= r_last["日期 (Date)"])]
        if not any(between["状态 (Status)"] == "买入"):
            prev_qty = float(r_prev["数量 (Qty)"])
            last_qty = float(r_last["数量 (Qty)"])
            if pd.notna(prev_qty) and pd.notna(last_qty) and last_qty <= prev_qty:
                days = (r_last["日期 (Date)"] - r_prev["日期 (Date)"]).days
                if days > 0:
                    daily = (prev_qty - last_qty) / days
                    if daily >= 0:
                        return float(daily * 14.0)

    # 2) 最近“买入”-> 最近“剩余”
    b = x[x["状态 (Status)"] == "买入"][["日期 (Date)", "数量 (Qty)"]].dropna()
    r = x[x["状态 (Status)"] == "剩余"][["日期 (Date)", "数量 (Qty)"]].dropna()
    if len(b) >= 1 and len(r) >= 1:
        b_last = b.iloc[-1]
        r_last = r.iloc[-1]
        if r_last["日期 (Date)"] >= b_last["日期 (Date)"]:
            days = (r_last["日期 (Date)"] - b_last["日期 (Date)"]).days
            if days > 0:
                used = float(b_last["数量 (Qty)"]) - float(r_last["数量 (Qty)"])
                daily = used / days
                if daily >= 0:
                    return float(daily * 14.0)

    return None


def compute_stats(df: pd.DataFrame) -> pd.DataFrame:
    """
    输入：原始记录（买入/剩余）
    输出：每个“食材名称”的统计表：
      - 当前库存（最近一次“剩余”的数量）
      - 平均最近两周使用量（14天总量）
      - 预计还能用天数 = 当前库存 / (两周用量/14)
      - 计算下次采购量 = max(0, 两周用量 - 当前库存)
      - 最近统计剩余日期 / 最近采购日期 / 平均采购间隔(天)
      - 最近采购数量 / 最近采购单价
      - 累计支出（按买入 Total Cost 求和）
    """
    df = normalize_columns(df)

    must = ["食材名称 (Item Name)", "日期 (Date)", "状态 (Status)", "数量 (Qty)"]
    missing = [c for c in must if c not in df.columns]
    if missing:
        return pd.DataFrame(columns=[
            "食材名称 (Item Name)", "当前库存", "平均最近两周使用量", "预计还能用天数", "计算下次采购量",
            "最近统计剩余日期", "最近采购日期", "平均采购间隔(天)", "最近采购数量", "最近采购单价", "累计支出"
        ])

    # 分组逐个物品计算
    rows = []
    # 只用有效日期
    df = df[pd.notna(df["日期 (Date)"])].copy()

    for item, g in df.groupby("食材名称 (Item Name)"):
        g = g.sort_values("日期 (Date)").reset_index(drop=True)

        # 当前库存 = 最近一次“剩余”数量
        last_rem_date, last_rem_qty = _last_remainder(g)

        # 最近采购信息
        (last_buy_date, last_buy_qty, last_buy_price,
         avg_buy_interval, total_spend_item) = _last_purchase_info(g)

        # 最近两周使用量（总量）
        asof = max(g["日期 (Date)"]) if len(g) else None
        usage_14 = _recent_usage_14d(g, asof)

        # 预计还能用天数 / 下次采购量（目标保障 14 天）
        days_left = None
        next_buy_qty = None
        if usage_14 is not None and usage_14 > 0 and last_rem_qty is not None:
            daily = usage_14 / 14.0
            days_left = float(last_rem_qty / daily) if daily > 0 else None
            target_14 = usage_14
            deficit = target_14 - float(last_rem_qty)
            next_buy_qty = float(np.ceil(deficit)) if deficit > 0 else 0.0

        rows.append({
            "食材名称 (Item Name)": item,
            "当前库存": float(last_rem_qty) if last_rem_qty is not None else np.nan,
            "平均最近两周使用量": float(usage_14) if usage_14 is not None else np.nan,
            "预计还能用天数": float(days_left) if days_left is not None else np.nan,
            "计算下次采购量": float(next_buy_qty) if next_buy_qty is not None else np.nan,
            "最近统计剩余日期": last_rem_date,
            "最近采购日期": last_buy_date,
            "平均采购间隔(天)": float(avg_buy_interval) if avg_buy_interval is not None else np.nan,
            "最近采购数量": float(last_buy_qty) if last_buy_qty is not None else np.nan,
            "最近采购单价": float(last_buy_price) if last_buy_price is not None else np.nan,
            "累计支出": float(total_spend_item or 0.0),
        })

    out = pd.DataFrame(rows)

    # 展示友好：日期转字符串
    for c in ["最近统计剩余日期", "最近采购日期"]:
        if c in out.columns:
            out[c] = pd.to_datetime(out[c], errors="coerce").dt.strftime("%Y-%m-%d")

    # 排序：优先缺货风险（还能用天数少），再按两周用量多
    if "预计还能用天数" in out.columns:
        out = out.sort_values(
            by=["预计还能用天数", "平均最近两周使用量"],
            ascending=[True, False],
            na_position="last"
        ).reset_index(drop=True)

    return out
