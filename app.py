# -*- coding: utf-8 -*-
import os
import json
import pandas as pd
import numpy as np
import streamlit as st
import altair as alt

# ================= Secrets/ENV =================
# 将 service account 写入本地，供 gspread 使用
if "service_account" in st.secrets:
    with open("service_account.json", "w") as f:
        json.dump(dict(st.secrets["service_account"]), f)

# 读取 Sheet URL（secrets 优先生效）
sheet_url = st.secrets.get("INVENTORY_SHEET_URL", None) or os.getenv("INVENTORY_SHEET_URL", None)
if sheet_url:
    os.environ["INVENTORY_SHEET_URL"] = sheet_url

# ================ Backend ======================
# 读写 Google Sheet
from gsheet import append_records_bulk
try:
    from gsheet import (
        read_records_cached as read_records_fn,
        read_catalog_cached as read_catalog_fn,
        bust_cache,
    )
except Exception:
    from gsheet import read_records as read_records_fn, read_catalog as read_catalog_fn
    def bust_cache(): pass

# 统计计算/列名规范化
try:
    from compute import compute_stats, _recent_usage_14d_robust as _recent_usage_14d_new, normalize_columns as normalize_columns_compute
except Exception:
    from compute import compute_stats, _recent_usage_14d_new
    def normalize_columns_compute(df: pd.DataFrame) -> pd.DataFrame:
        return df

# 允许的四个类别（硬编码）
ALLOWED_CATS = ["食物类", "清洁类", "消耗品", "饮品类"]
DEFAULT_CAT = "食物类"

# ============== 仅用于录入页的轻量工具 ==============
def safe_sort(df: pd.DataFrame, by: str, ascending=True):
    if df is None or df.empty or by not in df.columns:
        return df
    return df.sort_values(by, ascending=ascending)

def normalize_cat(x: str) -> str:
    if x is None:
        return DEFAULT_CAT
    s = str(x).strip()
    if s == "" or s.lower() in ("nan", "none"):
        return DEFAULT_CAT
    return s if s in ALLOWED_CATS else DEFAULT_CAT

def _blank_if_none(x):
    try:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return ""
    except Exception:
        pass
    return x

def build_item_order_from_catalog() -> dict:
    """
    从『库存产品』sheet 的行顺序构建物品显示顺序。
    返回形如 {物品名: 顺序号} 的映射；顺序号越小越靠前。
    如果读不到主清单，则返回空 dict（后面会走兜底）。
    """
    try:
        cat = read_catalog_fn()  # 复用你已有的读取主数据函数
    except Exception:
        cat = pd.DataFrame()

    if cat is None or cat.empty or "物品名" not in cat.columns:
        return {}

    order = {}
    seen = set()
    # 按在主清单中的出现顺序记录第一次出现的位置
    for idx, x in enumerate(list(cat["物品名"].astype(str))):
        name = x.strip()
        if name and name not in seen:
            order[name] = idx
            seen.add(name)
    return order

# ---------- 把数量解析成表格要写的形态 ----------
def to_qty_cell(raw, unit_in: str):
    """
    返回: (qty_cell, unit_out, is_pct)
    - 若数量里带 '%' 或 单位是百分号 -> is_pct=True，qty_cell 是 '50%' 这样的字符串；
    - 其他情况 -> is_pct=False，qty_cell 是数字(float 或 NaN)；
    - unit_out: 当单位不是 % 时按用户填写返回；若单位写成 %/percent/百分比/ratio，则返回空串。
    """
    s = "" if raw is None else str(raw).strip()
    unit_norm = (unit_in or "").replace("％", "%").strip()

    # 数量里带百分号
    if s.endswith("%"):
        num = pd.to_numeric(s[:-1], errors="coerce")
        if pd.isna(num):
            return "", ("" if unit_norm == "%" else unit_norm), True
        # 写回去就是 '50%' 这种；尽量去掉多余小数
        numf = float(num)
        qty_txt = f"{int(numf)}%" if numf.is_integer() else f"{numf}%"
        return qty_txt, ("" if unit_norm == "%" else unit_norm), True

    # 单位是百分号
    if unit_norm in {"%", "percent", "百分比", "ratio"}:
        num = pd.to_numeric(s, errors="coerce")
        if pd.isna(num):
            return "", "", True
        numf = float(num)
        qty_txt = f"{int(numf)}%" if numf.is_integer() else f"{numf}%"
        return qty_txt, "", True

    # 普通数量
    return pd.to_numeric(s, errors="coerce"), unit_norm, False

def _pct_ratio(qty_cell):
    """'50%' -> 0.5；否则返回 NaN。仅用于金额计算等数值场景。"""
    if isinstance(qty_cell, str) and qty_cell.strip().endswith("%"):
        try:
            return float(qty_cell.strip()[:-1]) / 100.0
        except Exception:
            return np.nan
    return np.nan

# ---------- 展示：表格居中 ----------
# ---------- 展示：表格居中 + 两位小数 ----------
def render_centered_table(df: pd.DataFrame):
    # 找出所有数值列
    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    # 为数值列统一设置两位小数
    fmt_map = {c: "{:.2f}" for c in num_cols}

    styler = (
        df.style
          .format(fmt_map)  # 两位小数
          .set_properties(**{"text-align": "center"})  # 单元格居中
          .set_table_styles([
              {"selector": "th", "props": [("text-align", "center")]},        # 表头居中
              {"selector": "th.col_heading", "props": [("text-align", "center")]},
              {"selector": "th.row_heading", "props": [("text-align", "center")]},
          ])
    )

    # 优先用交互表；如果版本不支持样式，就降级为静态表
    try:
        st.dataframe(styler, use_container_width=True)
    except Exception:
        st.table(styler)


# ================ APP UI =======================
st.set_page_config(page_title="Gangnam 库存管理", layout="wide")

# 顶部布局：左边 logo，右边标题说明
c1, c2 = st.columns([1, 6])
with c1:
    st.image("gangnam_logo.png", width=180)

with c2:
    st.markdown(
        """
        <div style="display:flex; flex-direction:column; justify-content:center; height:100%;">
            <h1 style="margin-bottom:0;">Gangnam 库存管理</h1>
            <p style="color:gray; font-size:16px; margin-top:4px;">
                录入‘买入/剩余’，自动保存到表格，并实时生成‘库存统计’分析
            </p>
        </div>
        """,
        unsafe_allow_html=True
    )

tabs = st.tabs(["➕ 录入记录", "📊 库存统计"])

# ================== 录入记录 ==================
with tabs[0]:
    st.subheader("录入新记录")

    # 读取“购入/剩余”用于推断已有物品（当没有主数据时）
    try:
        df_all = read_records_fn()
        df_all = normalize_columns_compute(df_all)  # 使用 compute 的强力清洗
    except Exception:
        df_all = pd.DataFrame()

    # 主数据（库存产品）
    try:
        catalog = read_catalog_fn()
    except Exception:
        catalog = pd.DataFrame()

    c1, c2, c3 = st.columns(3)
    sel_date   = c1.date_input("日期 (Date)", pd.Timestamp.today())
    sel_type   = c2.selectbox("类型（大类）", ALLOWED_CATS, index=0)
    sel_status = c3.selectbox("状态 (Status)", ["买入", "剩余"])

    # 构造可编辑表：优先主数据，否则历史记录中该类的最近单位
    if not catalog.empty and {"物品名", "单位", "类型"}.issubset(catalog.columns):
        catalog = catalog.copy()
        catalog["类型"] = catalog["类型"].apply(normalize_cat)
        base = (catalog[catalog["类型"] == sel_type][["物品名", "单位"]]
                .drop_duplicates()
                .reset_index(drop=True))
    else:
        if not df_all.empty:
            tmp = df_all.copy()
            if "分类 (Category)" not in tmp.columns:
                tmp["分类 (Category)"] = DEFAULT_CAT
            tmp["分类 (Category)"] = tmp["分类 (Category)"].apply(normalize_cat)
            latest_unit = (
                safe_sort(tmp[tmp["分类 (Category)"] == sel_type], "日期 (Date)")
                .groupby("食材名称 (Item Name)")["单位 (Unit)"]
                .agg(lambda s: s.dropna().iloc[-1] if len(s.dropna()) else "")
                .reset_index()
                .rename(columns={"食材名称 (Item Name)": "物品名", "单位 (Unit)": "单位"})
            )
            base = latest_unit
        else:
            base = pd.DataFrame(columns=["物品名", "单位"])

    # ---------- 构造可编辑表 ----------
    edit_df = base.copy()
    for col in ["物品名", "单位"]:
        if col not in edit_df.columns:
            edit_df[col] = ""
    # 数量用字符串占位，便于 TextColumn 接受 '20%' 这类输入
    edit_df["数量"] = ""
    if sel_status == "买入":
        edit_df["单价"] = np.nan
    edit_df["备注"] = ""

    st.markdown("**在下表中填写数量（必填），单价仅在买入时填写；可添加新行录入新物品**")

    edited = st.data_editor(
        edit_df.astype({"数量": "object"}),  # 明确把“数量”设为 object/str
        use_container_width=True,
        num_rows="dynamic",
        column_config={
            "数量": st.column_config.TextColumn(help="支持 3、0.5 或 20%"),
            "单价": st.column_config.NumberColumn(step=0.01, min_value=0.0) if sel_status == "买入" else None,
        },
        key="bulk_editor",
    )

    # 批量写入 Google Sheet
    if st.button("✅ 批量保存到『购入/剩余』"):
        dt = pd.to_datetime(sel_date)
        payload = []
        preview = []

        for _, r in edited.iterrows():
            name = str(r.get("物品名", "") or "").strip()
            if not name:
                continue

            unit_in = str(r.get("单位", "") or "").strip()
            qty_cell, unit_out, is_pct = to_qty_cell(r.get("数量", ""), unit_in)

            # ---- 买入 / 剩余 的校验与落表值 ----
            if sel_status == "买入":
                # 允许百分比或纯数字；都必须 > 0
                if is_pct:
                    ratio = _pct_ratio(qty_cell)   # '50%' -> 0.5
                    if not (pd.notna(ratio) and ratio > 0):
                        continue
                    qty_to_sheet = qty_cell       # 写 '50%' 到“数量”
                    qty_for_cost = ratio          # 金额用 0.5 参与计算
                else:
                    try:
                        qty_num = float(qty_cell)
                    except Exception:
                        qty_num = np.nan
                    if not (pd.notna(qty_num) and qty_num > 0):
                        continue
                    qty_to_sheet = qty_num        # 写数字到“数量”
                    qty_for_cost = qty_num        # 金额用数字
            else:
                # 剩余：允许百分比或数字（可=0）
                if is_pct:
                    qty_to_sheet = qty_cell       # '50%' 原样写回
                else:
                    try:
                        qty_num = float(qty_cell)
                    except Exception:
                        qty_num = np.nan
                    if pd.isna(qty_num):          # 剩余至少要能解析成数字(可=0)或是百分比
                        continue
                    qty_to_sheet = qty_num
                qty_for_cost = None               # 剩余不计总价

            # ---- 价格 / 总价 ----
            price = None
            total = None
            if sel_status == "买入" and pd.notna(r.get("单价", np.nan)):
                try:
                    price = float(r["单价"])
                except Exception:
                    price = None
                if price is not None:
                    # 若是百分比买入，用 ratio 计算金额；否则用数字数量
                    factor = qty_for_cost if qty_for_cost is not None else np.nan
                    if pd.notna(factor):
                        total = round(float(factor) * price, 2)

            # ---- 组装写入行 ----
            record = {
                "日期 (Date)": f"=DATE({dt.year},{dt.month},{dt.day})",
                "食材名称 (Item Name)": name,
                "分类 (Category)": sel_type,
                "数量 (Qty)": qty_to_sheet,            # 可能是数字，也可能是 '50%'
                "单位 (Unit)": unit_out,               # 正常单位；若用户填了 % 则置空
                "单价 (Unit Price)": "" if price is None else price,
                "总价 (Total Cost)": "" if total is None else total,
                "状态 (Status)": sel_status,
                "备注 (Notes)": str(r.get("备注", "") or "").strip(),
            }
            payload.append(record)

            # ---- 预览 ----
            row_preview = {
                "日期": dt.date().isoformat(),
                "物品名": name,
                "数量": qty_to_sheet,
                "单位": unit_out,
                "状态": sel_status,
            }
            if sel_status == "买入":
                row_preview["单价"] = "" if price is None else price
                row_preview["总价"] = "" if total is None else total
            preview.append(row_preview)

        # 3) 批量写入 + 显示写入明细 + 回读校验
        try:
            if payload:
                resp = append_records_bulk(payload)  # gsheet 内部已用 USER_ENTERED + table_range="A1"
                st.success(f"已成功写入 {len(payload)} 条记录！")
                st.caption(f"目标表：{st.secrets.get('INVENTORY_SHEET_URL') or os.getenv('INVENTORY_SHEET_URL')}")

                # 显示 Google 返回的写入区间（用于定位）
                from gsheet import parse_updated_range_rows, tail_rows
                rng = resp.get("updates", {}).get("updatedRange", "")
                st.caption(f"Google 返回写入区间：{rng}")
                rows_info = parse_updated_range_rows(resp)
                if rows_info:
                    st.caption(f"（起止行号：{rows_info[0]}–{rows_info[1]}）")

                # 表尾快照
                with st.expander("🔎 表尾快照（最近 10 行）", expanded=False):
                    st.dataframe(tail_rows(10), use_container_width=True)

                # 本次写入的记录（预览）
                pre_df = pd.DataFrame(preview)
                if sel_status == "买入":
                    pre_df = pre_df[["日期", "物品名", "数量", "单位", "单价", "总价", "状态"]]
                    with pd.option_context("mode.use_inf_as_na", True):
                        total_spent = pd.to_numeric(pre_df.get("总价"), errors="coerce").sum()
                    st.caption(f"本次买入合计金额：{total_spent:.2f}")
                else:
                    pre_df = pre_df[["日期", "物品名", "数量", "单位", "状态"]]
                st.markdown("**本次写入的记录**")
                st.dataframe(pre_df, use_container_width=True)

                # 回读校验
                try:
                    bust_cache()
                except Exception:
                    pass
                try:
                    df_check = read_records_fn()
                    df_check = normalize_columns_compute(df_check)
                    dd = pd.to_datetime(df_check.get("日期 (Date)"), errors="coerce").dt.date
                    names = [p["物品名"] for p in preview]
                    just_now = df_check[
                        (dd == dt.date()) &
                        (df_check.get("状态 (Status)") == sel_status) &
                        (df_check.get("食材名称 (Item Name)").isin(names))
                    ][["日期 (Date)","食材名称 (Item Name)","数量 (Qty)","状态 (Status)"]].copy()
                    st.markdown("**写入后的回读校验**")
                    if just_now.empty:
                        st.warning("表里暂未读到刚写入的行（可能被表格格式/底部空白影响）。若仍未出现，请清理表底部多余格式，并确认 gsheet.append 使用 USER_ENTERED + table_range='A1'。")
                    else:
                        st.dataframe(just_now.sort_values("日期 (Date)"), use_container_width=True)
                except Exception:
                    pass

            else:
                st.info("没有可写入的记录。")
        except Exception as e:
            st.error(f"保存失败：{e}")

# ================== 库存统计 ==================
with tabs[1]:
    st.subheader("库存统计")

    colR1, _ = st.columns([1, 3])
    if colR1.button("🔄 刷新数据", help="清空缓存并重新读取 Google Sheet"):
        try:
            bust_cache()
        except Exception:
            pass
        st.rerun()

    # 读明细并统一列名 —— 使用 compute 的规范化
    try:
        df = read_records_fn()
        df = normalize_columns_compute(df)
    except Exception as e:
        st.error(f"读取表格失败：{e}")
        st.stop()

    # 调试面板：看看实际读到了什么
    with st.expander("🔎 调试：查看原始数据快照", expanded=False):
        st.write("shape:", df.shape)
        st.write("columns:", list(df.columns))
        for col in ["日期 (Date)", "食材名称 (Item Name)", "分类 (Category)", "状态 (Status)"]:
            if col in df.columns:
                st.write(f"{col} 非空数量:", int(df[col].notna().sum()))
            else:
                st.write(f"⚠️ 未识别列：{col}")
        if not df.empty:
            st.dataframe(df.head(10), use_container_width=True)

    # 兜底分类
    if "分类 (Category)" not in df.columns:
        df["分类 (Category)"] = DEFAULT_CAT
    else:
        df["分类 (Category)"] = df["分类 (Category)"].apply(normalize_cat)

    # 统计表
    stats_all = compute_stats(df)

    # 附上“类型”列用于筛选（取该物品最近一次的分类）
    if not df.empty and "食材名称 (Item Name)" in df.columns:
        latest_cat = (
            df.sort_values("日期 (Date)")
              .groupby("食材名称 (Item Name)")["分类 (Category)"]
              .agg(lambda s: s.dropna().iloc[-1] if len(s.dropna()) else DEFAULT_CAT)
        )
        stats_all = stats_all.merge(
            latest_cat.rename("类型"),
            left_on="食材名称 (Item Name)", right_index=True, how="left"
        )
    else:
        stats_all["类型"] = DEFAULT_CAT
    stats_all["类型"] = stats_all["类型"].apply(normalize_cat)

    # ---------- 固定显示顺序：来自『库存产品』的行顺序 ----------
    def _norm_name(s):
        # 去前后空格 + 去除所有空白字符，避免“看不见的空格”影响匹配
        return (str(s) if s is not None else "").strip().replace(" ", "")

    try:
        order_df = read_catalog_fn().copy()
    except Exception:
        order_df = pd.DataFrame()

    if not order_df.empty and "物品名" in order_df.columns:
        order_df["类型"] = order_df.get("类型", "").apply(normalize_cat)
        order_df["name_norm"] = order_df["物品名"].map(_norm_name)
        order_df["__order__"] = np.arange(len(order_df), dtype=float)
        order_map = dict(zip(order_df["name_norm"], order_df["__order__"]))
    else:
        order_map = {}

    BIG = float(1e9)  # 匹配不到的放到最后
    stats_all["name_norm"] = stats_all["食材名称 (Item Name)"].map(_norm_name)
    stats_all["__order__"] = stats_all["name_norm"].map(order_map).fillna(BIG)

    # === 筛选条（作用于下方结果表） ===
    st.markdown("#### 筛选")
    fc1, _ = st.columns([1, 3])
    sel_type_bar = fc1.selectbox("选择分类", ["全部"] + ALLOWED_CATS, index=0)

    stats = stats_all if sel_type_bar == "全部" else stats_all[stats_all["类型"].eq(sel_type_bar)]
    stats = stats.copy()

    # 预警规则：百分比(<20%)；非百分比(预计还能用天数<3)
    def _is_percent_row(row: pd.Series) -> bool:
        name = str(row.get("食材名称 (Item Name)", "") or "")
        unit = str(row.get("单位 (Unit)", "") or "").strip()
        last_rem = pd.to_numeric(row.get("最近剩余数量"), errors="coerce")
    
        # 1) 名称里带“糖浆”直接按百分比处理（你之前的习惯）
        if "糖浆" in name:
            return True
    
        # 2) 单位明确是百分比
        if unit in {"%", "％", "百分比", "percent", "ratio"}:
            return True
    
        # 3) 统计表里多数百分比行单位是空串；允许统计溢出到 150%（1.5）
        if unit == "" and pd.notna(last_rem) and 0.0 <= float(last_rem) <= 1.5:
            return True

        return False

    def _unit_norm(u: str) -> str:
        """单位统一：去空格、小写。"""
        return str(u or "").strip().lower()
    
    def badge_row(row: pd.Series) -> str:
        # ===== 饮品类优先规则 =====
        if str(row.get("类型", "")).strip() == "饮品类":
            unit = _unit_norm(row.get("单位 (Unit)"))
            cur = pd.to_numeric(row.get("当前库存"), errors="coerce")
    
            if pd.notna(cur):
                s = float(cur)
                # 单位是“箱”时，小于 2 报警
                if unit in {"箱", "box"} and s < 2:
                    return "🚨 立即下单"
                # 单位是“瓶”或“袋”时，小于 6 报警
                if unit in {"瓶", "袋", "bottle", "bag"} and s < 6:
                    return "🚨 立即下单"
            # 饮品类但不符合上面两个条件 -> 走通用规则继续判断
    
        # ===== 通用规则（非饮品类，或饮品类未触发上面的阈值）=====
        # 1) 百分比物料：最近剩余数量 < 20% 报警
        if _is_percent_row(row):
            val = pd.to_numeric(row.get("最近剩余数量"), errors="coerce")
            if pd.notna(val) and float(val) < 0.2:
                return "🚨 立即下单"
            return "🟢 正常"
    
        # 2) 非百分比：预计还能用天数 < 3 天 报警
        days = pd.to_numeric(row.get("预计还能用天数"), errors="coerce")
        if pd.notna(days) and float(days) < 3:
            return "🚨 立即下单"
        return "🟢 正常"

    stats["库存预警"] = stats.apply(badge_row, axis=1) if not stats.empty else ""

    # —— 在固定顺序下排序（只按 __order__；mergesort 保持稳定）
    stats_sorted = stats.sort_values("__order__", kind="mergesort")

    # KPI（用排序后的结果）
    c1, = st.columns(1)
    total_items = int(stats_sorted["食材名称 (Item Name)"].nunique()) if not stats_sorted.empty else 0
    c1.metric("记录数量", value=total_items)

    # 结果表
    display_cols = [
        "食材名称 (Item Name)", "当前库存", "单位 (Unit)", "平均最近两周使用量",
        "预计还能用天数", "最近统计剩余日期", "最近采购日期",
        "最近采购数量", "最近采购单价", "平均采购间隔(天)", "累计支出", "库存预警"
    ]
    show = stats_sorted[[c for c in display_cols if c in stats_sorted.columns]].copy()

    if show.empty:
        st.info("暂无统计结果。请检查『购入/剩余』表的表头/数据是否完整。")
    render_centered_table(show)

    # ============ 下钻：物品详情 ============
    st.markdown("### 🔍 物品详情")
    detail_items = ["（不选）"] + (list(show["食材名称 (Item Name)"].dropna().unique()) if "食材名称 (Item Name)" in show.columns else [])
    picked = st.selectbox("选择一个物品查看详情", detail_items, index=0)

    if picked and picked != "（不选）":
        # 统一口径的“当前库存” = 最后一次剩余 + 之后买入
        item_df = normalize_columns_compute(df[df["食材名称 (Item Name)"] == picked].copy())
        item_df = item_df.reset_index(drop=False).rename(columns={"index": "__orig_idx__"})
        if "row_order" not in item_df.columns:
            item_df["row_order"] = item_df["__orig_idx__"]
        item_df = item_df.sort_values(["日期 (Date)", "row_order"])

        rem = item_df[item_df.get("状态 (Status)") == "剩余"].copy()
        buy = item_df[item_df.get("状态 (Status)") == "买入"].copy()

        if len(rem):
            last_rem = rem.iloc[-1]
            last_date = last_rem["日期 (Date)"]
            last_ord  = last_rem["row_order"]
            last_qty  = float(last_rem["数量 (Qty)"]) if pd.notna(last_rem["数量 (Qty)"]) else 0.0
            mask_after = (
                (item_df["日期 (Date)"] > last_date) |
                ((item_df["日期 (Date)"] == last_date) & (item_df["row_order"] > last_ord))
            )
            buys_after = item_df[mask_after & (item_df["状态 (Status)"] == "买入")]
            cur_stock = float(last_qty + buys_after["数量 (Qty)"].sum())
        else:
            cur_stock = float(buy["数量 (Qty)"].sum()) if len(buy) else float("nan")

        last_buy = buy.iloc[-1] if len(buy) else None
        last_buy_date = (last_buy["日期 (Date)"].date().isoformat()
                         if last_buy is not None and pd.notna(last_buy["日期 (Date)"]) else "—")
        last_buy_qty  = float(last_buy["数量 (Qty)"]) if last_buy is not None else np.nan
        last_buy_price = float(last_buy["单价 (Unit Price)"]) if (last_buy is not None and "单价 (Unit Price)" in item_df.columns) else np.nan

        use14 = _recent_usage_14d_new(item_df)
        days_left = (cur_stock / (use14 / 14.0)) if (use14 and use14 > 0 and not np.isnan(cur_stock)) else np.nan
        stockout_date = (pd.Timestamp.today().normalize() + pd.Timedelta(days=float(days_left))).date().isoformat() \
                        if days_left == days_left else "—"

        if len(buy) >= 2:
            avg_interval = buy["日期 (Date)"].diff().dt.days.dropna().mean()
        else:
            avg_interval = np.nan

        # KPI
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        k1.metric("当前库存", f"{0 if np.isnan(cur_stock) else cur_stock}")
        k2.metric("最近14天用量", f"{0 if not use14 else round(use14, 2)}")
        k3.metric("预计还能用天数", "—" if np.isnan(days_left) else f"{days_left:.2f}")
        k4.metric("预计缺货日期", stockout_date)
        k5.metric("最近采购日期", last_buy_date)
        k6.metric("平均采购间隔(天)", "—" if np.isnan(avg_interval) else f"{avg_interval:.1f}")

        # 库存轨迹（近60天）
        lookback = pd.Timestamp.today().normalize() - pd.Timedelta(days=60)
        rem60 = rem[rem["日期 (Date)"] >= lookback].copy()
        if not rem60.empty:
            rem60["dt"] = pd.to_datetime(rem60["日期 (Date)"])
            chart_stock = alt.Chart(rem60).mark_line(point=True).encode(
                x=alt.X("dt:T", title="日期"),
                y=alt.Y("数量 (Qty):Q", title="剩余数量")
            ).properties(title=f"{picked} — 剩余数量（近60天）")
            st.altair_chart(chart_stock, use_container_width=True)

        # 事件时间线（近60天）
        ev = item_df[item_df["日期 (Date)"] >= lookback][["日期 (Date)", "状态 (Status)", "数量 (Qty)", "单价 (Unit Price)"]].copy()
        if not ev.empty:
            ev["dt"] = pd.to_datetime(ev["日期 (Date)"])
            status_color = alt.Color(
                "状态 (Status):N",
                scale=alt.Scale(domain=["买入", "剩余"], range=["#1f77b4", "#E4572E"]),
                legend=alt.Legend(title="状态")
            )
            chart_ev = alt.Chart(ev).mark_point(filled=True, size=80).encode(
                x=alt.X("dt:T", title="日期"),
                y=alt.Y("数量 (Qty):Q"),
                color=status_color,
                shape="状态 (Status):N",
                tooltip=["状态 (Status)", "数量 (Qty)", "单价 (Unit Price)", "日期 (Date)"]
            ).properties(title=f"{picked} — 事件时间线（近60天）")
            st.altair_chart(chart_ev, use_container_width=True)

        # 最近记录（原始）
        st.markdown(" ")
        st.markdown("#### 最近记录（原始）")
        cols = ["日期 (Date)", "状态 (Status)", "数量 (Qty)", "单位 (Unit)", "单价 (Unit Price)", "总价 (Total Cost)", "分类 (Category)", "备注 (Notes)"]
        cols = [c for c in cols if c in item_df.columns]
        st.dataframe(item_df[cols].sort_values("日期 (Date)").iloc[::-1].head(10), use_container_width=True)
