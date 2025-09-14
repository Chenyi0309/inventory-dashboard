# compute.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import Optional
import pandas as pd
import numpy as np

# ======================== 列名规范化（含脏数据清洗） ========================

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
    # 括号前缺空格 -> 补空格；括号后紧接文字 -> 补空格
    s = re.sub(r"([\u4e00-\u9fffA-Za-z0-9])\(", r"\1 (", s)
    s = re.sub(r"\)\s*([A-Za-z0-9\u4e00-\u9fff])", r") \1", s)
    # 括号内部收紧空格
    s = re.sub(r"\(\s*([^)]+?)\s*\)", r"(\1)", s)
    s = re.sub(r"\s+", " ", s.strip())
    return s


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """把各类变体表头统一为标准列；把日期/数字列转为合适类型。"""
    if df is None or df.empty:
        return df.copy()

    cols = []
    for c in df.columns:
        c0 = _clean_token(c)
        cols.append(_FLAT.get(c0.lower(), c0))
    out = df.copy()
    out.columns = cols

    if "日期 (Date)" in out.columns:
        out["日期 (Date)"] = pd.to_datetime(out["日期 (Date)"], errors="coerce")

    # 数值统一："30%"->0.3；"1,234.5"->1234.5
    for col in ["数量 (Qty)", "单价 (Unit Price)", "总价 (Total Cost)"]:
        if col in out.columns:
            s = out[col].astype(str).str.replace(",", "", regex=False).str.strip()
            is_pct = s.str.endswith("%", na=False)
            s = pd.to_numeric(s.str.rstrip("%"), errors="coerce")
            s = np.where(is_pct, s / 100.0, s)
            out[col] = pd.to_numeric(s, errors="coerce")

    if "状态 (Status)" in out.columns:
        s = out["状态 (Status)"].astype(str).str.lower()
        out["状态 (Status)"] = np.where(
            s.str.contains("买|purchase|buy"), "买入",
            np.where(s.str.contains("余|remain|left|stock"), "剩余", out["状态 (Status)"])
        )
    return out


# ======================== 公用小工具 ========================

def _with_row_order(df: pd.DataFrame) -> pd.DataFrame:
    """确保存在 row_order 列，并按 日期 + row_order 升序返回副本。"""
    if df is None or df.empty:
        return df.copy()
    x = df.copy().reset_index(drop=False).rename(columns={"index": "__orig_idx__"})
    if "row_order" not in x.columns:
        x["row_order"] = x["__orig_idx__"]
    if "日期 (Date)" in x.columns:
        x["日期 (Date)"] = pd.to_datetime(x["日期 (Date)"], errors="coerce")
    return x.sort_values(["日期 (Date)", "row_order"]).reset_index(drop=True)


def _between_mask(df: pd.DataFrame,
                  start_date, start_ord: int,
                  end_date, end_ord: int,
                  include_start: bool = False,
                  include_end: bool = False) -> pd.Series:
    """日期+行号开闭区间掩码。"""
    # 左端
    left = (df["日期 (Date)"] > start_date) | ((df["日期 (Date)"] == start_date) & (df["row_order"] > start_ord))
    if include_start:
        left = (df["日期 (Date)"] > start_date) | ((df["日期 (Date)"] == start_date) & (df["row_order"] >= start_ord))
    # 右端
    right = (df["日期 (Date)"] < end_date) | ((df["日期 (Date)"] == end_date) & (df["row_order"] < end_ord))
    if include_end:
        right = (df["日期 (Date)"] < end_date) | ((df["日期 (Date)"] == end_date) & (df["row_order"] <= end_ord))
    return left & right


# ======================== 规则 1：当前库存 ========================

def _current_stock_rule(df_item: pd.DataFrame) -> Optional[float]:
    """
    当前库存：
      - 以最后一条“剩余”为基准：当前库存 = 该条“剩余”的数量 + 这条之后的所有“买入”数量之和
      - 若没有任何“剩余”，则库存 = 全部“买入”数量之和
    """
    x = _with_row_order(df_item)

    rem = x[x["状态 (Status)"] == "剩余"]
    buys = x[x["状态 (Status)"] == "买入"]

    if rem.empty:
        return float(buys["数量 (Qty)"].sum()) if not buys.empty else np.nan

    last_rem = rem.iloc[-1]
    last_date = last_rem["日期 (Date)"]
    last_ord  = last_rem["row_order"]
    last_qty  = float(last_rem["数量 (Qty)"]) if pd.notna(last_rem["数量 (Qty)"]) else 0.0

    BIG_ORD = 10**12
    mask_after = _between_mask(
        x,
        start_date=last_date, start_ord=last_ord,
        end_date=pd.Timestamp.max, end_ord=BIG_ORD,
        include_start=False, include_end=True
    )
    buys_after = x[mask_after & (x["状态 (Status)"] == "买入")]
    sum_after  = float(buys_after["数量 (Qty)"].sum()) if not buys_after.empty else 0.0

    return float(last_qty + sum_after)


# ======================== 规则 2：平均最近两周使用量 ========================

def _usage_14d_rule(df_item: pd.DataFrame) -> Optional[float]:
    """
    以最后一条“剩余”为窗口终点：
      - 若窗口内出现“连续两次剩余，第二次数量更大，且其间无买入”（视为漏记买入），从第二次剩余起算；
      - 否则选择最接近“14天前”的剩余（先窗口内；否则回退窗口外最近一条）。
      用量 = (期间买入之和 + 起点剩余 − 终点剩余) / 间隔天数 × 14
    """
    x = _with_row_order(df_item)
    if x.empty:
        return None

    rem = x[x["状态 (Status)"] == "剩余"]
    if rem.empty:
        return None

    r_last = rem.iloc[-1]
    end_date = r_last["日期 (Date)"]
    end_ord  = r_last["row_order"]
    end_qty  = float(r_last["数量 (Qty)"]) if pd.notna(r_last["数量 (Qty)"]) else None
    if end_qty is None:
        return None

    target_start = end_date - pd.Timedelta(days=14)

    # A. 漏记买入模式：窗口内 连续两次“剩余” & 第二次数量更大 & 其间无买入
    rem_win = rem[(rem["日期 (Date)"] >= target_start) & (rem["日期 (Date)"] <= end_date)].reset_index(drop=True)
    leak_start_row = None
    if len(rem_win) >= 2:
        for i in range(len(rem_win) - 1):
            r1, r2 = rem_win.iloc[i], rem_win.iloc[i + 1]
            if float(r2["数量 (Qty)"]) > float(r1["数量 (Qty)"]):
                mask_mid = _between_mask(
                    x,
                    start_date=r1["日期 (Date)"], start_ord=r1["row_order"],
                    end_date=r2["日期 (Date)"], end_ord=r2["row_order"],
                    include_start=False, include_end=False
                )
                if not any(x.loc[mask_mid, "状态 (Status)"] == "买入"):
                    leak_start_row = r2
    if leak_start_row is not None:
        start_row = leak_start_row
    else:
        # B. 选最接近 target_start 的“剩余”：优先窗口内最早；否则窗口外向前最近
        rem_all = rem[rem["日期 (Date)"] <= end_date].copy()
        cand_in = rem_all[rem_all["日期 (Date)"] >= target_start]
        if not cand_in.empty:
            start_row = cand_in.sort_values(["日期 (Date)", "row_order"]).iloc[0]
        else:
            cand_out = rem_all[rem_all["日期 (Date)"] < target_start]
            if cand_out.empty:
                return None
            start_row = cand_out.sort_values(["日期 (Date)", "row_order"]).iloc[-1]

    start_date = start_row["日期 (Date)"]
    start_ord  = start_row["row_order"]
    start_qty  = float(start_row["数量 (Qty)"]) if pd.notna(start_row["数量 (Qty)"]) else None
    if start_qty is None:
        return None

    # 区间买入之和（开区间起点，闭区间终点）
    mask_period = _between_mask(
        x,
        start_date=start_date, start_ord=start_ord,
        end_date=end_date,   end_ord=end_ord,
        include_start=False, include_end=True
    )
    buys = x[mask_period & (x["状态 (Status)"] == "买入")]
    sum_buys = float(buys["数量 (Qty)"].sum()) if not buys.empty else 0.0

    days = (end_date - start_date).days
    if days <= 0:
        return None

    used = sum_buys + start_qty - end_qty
    if used < 0:
        return None

    daily = used / days
    return float(daily * 14.0)


# 兼容 app.py 的导入别名
def _recent_usage_14d_robust(df_item: pd.DataFrame) -> Optional[float]:
    return _usage_14d_rule(df_item)


# ======================== 对外主函数 ========================

def compute_stats(df: pd.DataFrame) -> pd.DataFrame:
    """
    生成统计汇总（供 app 展示）：
      - 当前库存（规则 1）
      - 平均最近两周使用量（规则 2）
      - 预计还能用天数
      - 最近统计剩余日期 / 最近采购日期 / 最近采购数量 / 最近采购单价
      - 平均采购间隔(天) / 累计支出
      - 单位 (Unit) / 最近剩余数量（供预警使用）
    """
    df = normalize_columns(df)

    must = ["食材名称 (Item Name)", "日期 (Date)", "状态 (Status)", "数量 (Qty)"]
    missing = [c for c in must if c not in df.columns]
    if missing:
        return pd.DataFrame(columns=[
            "食材名称 (Item Name)", "当前库存", "平均最近两周使用量", "预计还能用天数",
            "最近统计剩余日期", "最近采购日期", "最近采购数量", "最近采购单价",
            "平均采购间隔(天)", "累计支出", "单位 (Unit)", "最近剩余数量"
        ])

    df = df[pd.notna(df["日期 (Date)"])].copy()
    # 主 DataFrame 也补齐 row_order，便于各函数使用
    if "row_order" not in df.columns:
        df = df.reset_index(drop=False).rename(columns={"index": "__orig_idx__"})
        df["row_order"] = df["__orig_idx__"]

    rows = []
    for item, g in df.groupby("食材名称 (Item Name)"):
        g = _with_row_order(g)

        # 最近剩余
        rem = g[g["状态 (Status)"] == "剩余"]
        last_rem_date = rem.iloc[-1]["日期 (Date)"] if not rem.empty else None
        last_rem_qty  = float(rem.iloc[-1]["数量 (Qty)"]) if (not rem.empty and pd.notna(rem.iloc[-1]["数量 (Qty)"])) else np.nan

        # 最近买入信息
        buy = g[g["状态 (Status)"] == "买入"]
        last_buy_date  = buy.iloc[-1]["日期 (Date)"] if not buy.empty else None
        last_buy_qty   = float(buy.iloc[-1]["数量 (Qty)"]) if (not buy.empty and pd.notna(buy.iloc[-1]["数量 (Qty)"])) else np.nan
        last_buy_price = float(buy.iloc[-1]["单价 (Unit Price)"]) if (not buy.empty and pd.notna(buy.iloc[-1].get("单价 (Unit Price)", np.nan))) else np.nan

        # 单位：最近一条非空
        if "单位 (Unit)" in g.columns and len(g["单位 (Unit)"].dropna()):
            last_unit = g["单位 (Unit)"].dropna().astype(str).replace("nan", "").iloc[-1]
        else:
            last_unit = ""

        # 规则 1：当前库存
        cur_stock = _current_stock_rule(g)

        # 规则 2：最近14天用量
        use14 = _usage_14d_rule(g)

        # 还能用天数
        days_left = np.nan
        if use14 and use14 > 0 and cur_stock is not None and not np.isnan(cur_stock):
            daily = use14 / 14.0
            if daily > 0:
                days_left = float(cur_stock / daily)

        # 平均采购间隔 & 累计支出
        avg_int = np.nan
        if len(buy) >= 2:
            diffs = buy["日期 (Date)"].diff().dt.days.dropna()
            if not diffs.empty:
                avg_int = float(diffs.mean())
        total_spend_item = float(buy.get("总价 (Total Cost)", pd.Series([], dtype=float)).sum())

        rows.append({
            "食材名称 (Item Name)": item,
            "当前库存": float(cur_stock) if cur_stock is not None else np.nan,
            "单位 (Unit)": last_unit,
            "平均最近两周使用量": float(use14) if use14 is not None else np.nan,
            "预计还能用天数": float(days_left) if days_left == days_left else np.nan,
            "最近统计剩余日期": pd.to_datetime(last_rem_date, errors="coerce"),
            "最近采购日期": pd.to_datetime(last_buy_date, errors="coerce"),
            "最近采购数量": last_buy_qty,
            "最近采购单价": last_buy_price,
            "平均采购间隔(天)": avg_int,
            "累计支出": total_spend_item,
            "最近剩余数量": last_rem_qty,
        })

    out = pd.DataFrame(rows)

    for c in ["最近统计剩余日期", "最近采购日期"]:
        if c in out.columns:
            out[c] = pd.to_datetime(out[c], errors="coerce").dt.strftime("%Y-%m-%d")

    if not out.empty:
        out = out.sort_values(
            by=["预计还能用天数", "平均最近两周使用量"],
            ascending=[True, False],
            na_position="last"
        ).reset_index(drop=True)

    return out
