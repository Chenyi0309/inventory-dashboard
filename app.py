# -*- coding: utf-8 -*-
import os
import json
import re
import pandas as pd
import numpy as np
import streamlit as st
import altair as alt

# ================= Secrets/ENV =================
if "service_account" in st.secrets:
    with open("service_account.json", "w") as f:
        json.dump(dict(st.secrets["service_account"]), f)

sheet_url = st.secrets.get("INVENTORY_SHEET_URL", None) or os.getenv("INVENTORY_SHEET_URL", None)
if sheet_url:
    os.environ["INVENTORY_SHEET_URL"] = sheet_url

# ================ Backend ======================
from gsheet import append_record, append_records_bulk
try:
    from gsheet import read_records_cached as read_records_fn, read_catalog_cached as read_catalog_fn, bust_cache
except Exception:
    from gsheet import read_records as read_records_fn, read_catalog as read_catalog_fn
    def bust_cache(): pass

# 计算逻辑与强力列名规范化均在 compute.py
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
    if s == "" or s.lower() in ("nan","none"):
        return DEFAULT_CAT
    return s if s in ALLOWED_CATS else DEFAULT_CAT

# ================ APP UI =======================
st.set_page_config(page_title="Gangnam 库存管理", layout="wide")
# 顶部布局：左边 logo，右边标题
c1, c2 = st.columns([1, 6])   # 左右列比例

with c1:
    st.image("gangnam_logo.png", width=180)  # 调大图片宽度

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

    # 主数据可选
    try:
        catalog = read_catalog_fn()
    except Exception:
        catalog = pd.DataFrame()

    c1, c2, c3 = st.columns(3)
    sel_date   = c1.date_input("日期 (Date)", pd.Timestamp.today())
    sel_type   = c2.selectbox("类型（大类）", ALLOWED_CATS, index=0)
    sel_status = c3.selectbox("状态 (Status)", ["买入", "剩余"])

    # 构造可编辑表：优先主数据，否则历史记录中该类的最近单位
    if not catalog.empty and {"物品名","单位","类型"}.issubset(catalog.columns):
        base = catalog[catalog["类型"] == sel_type][["物品名","单位"]].drop_duplicates().reset_index(drop=True)
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
                .rename(columns={"食材名称 (Item Name)":"物品名","单位 (Unit)":"单位"})
            )
            base = latest_unit
        else:
            base = pd.DataFrame(columns=["物品名","单位"])

    edit_df = base.copy()
    for col in ["物品名","单位"]:
        if col not in edit_df.columns: edit_df[col] = ""
    edit_df["数量"] = 0.0
    if sel_status == "买入":
        edit_df["单价"] = 0.0
    edit_df["备注"] = ""

    st.markdown("**在下表中填写数量（必填），单价仅在买入时填写；可添加新行录入新物品**")
    edited = st.data_editor(
        edit_df,
        use_container_width=True,
        num_rows="dynamic",
        column_config={
            "数量": st.column_config.NumberColumn(step=0.1, min_value=0.0),
            "单价": st.column_config.NumberColumn(step=0.01, min_value=0.0) if sel_status == "买入" else None,
        },
        key="bulk_editor",
    )

    if st.button("✅ 批量保存到『购入/剩余』"):
        rows = edited.copy()
        rows["数量"] = pd.to_numeric(rows["数量"], errors="coerce")
        rows = rows[(rows["数量"].fillna(0) > 0) & (rows["物品名"].astype(str).str.strip() != "")]
    if rows.empty:
        st.warning("请至少填写一个物品的‘物品名’和‘数量’")
        st.stop()

    # === 组装 records 列表（一次批量写入） ===
    records = []
    for _, r in rows.iterrows():
        qty   = float(r["数量"])
        unit  = str(r.get("单位", "") or "").strip()
        price = None
        total = None
        if sel_status == "买入" and "单价" in r and pd.notna(r["单价"]):
            price = float(r["单价"])
            total = qty * price

        records.append({
            "日期 (Date)": pd.to_datetime(sel_date).strftime("%Y-%m-%d"),
            "食材名称 (Item Name)": str(r["物品名"]).strip(),
            "分类 (Category)": sel_type,
            "数量 (Qty)": qty,
            "单位 (Unit)": unit,
            "单价 (Unit Price)": price if sel_status == "买入" else "",
            "总价 (Total Cost)": total if sel_status == "买入" else "",
            "状态 (Status)": sel_status,
            "备注 (Notes)": str(r.get("备注", "")).strip(),
        })

    try:
        # 一次调用，显著减少写请求次数
        from gsheet import append_records_bulk
        append_records_bulk(records)
        st.success(f"已成功写入 {len(records)} 条记录！")
    except Exception as e:
        st.error(f"保存失败：{e}")

        if ok and not fail:
            st.success(f"已成功写入 {ok} 条记录！")
        elif ok and fail:
            st.warning(f"部分成功：{ok} 条成功，{fail} 条失败。")
        else:
            st.error("保存失败，请检查表格权限与 Secrets 配置。")

# ================== 库存统计 ==================
with tabs[1]:
    st.subheader("库存统计")

    colR1, _ = st.columns([1, 3])
    if colR1.button("🔄 刷新数据", help="清空缓存并重新读取 Google Sheet"):
        try: bust_cache()
        except: pass
        st.rerun()

    # 读明细并统一列名 —— 使用 compute 的规范化
    try:
        df = read_records_fn()
        df = normalize_columns_compute(df)
    except Exception as e:
        st.error(f"读取表格失败：{e}")
        st.stop()

    # 调试面板
    with st.expander("🔎 调试：查看原始数据快照", expanded=False):
        st.write("shape:", df.shape)
        st.write("columns:", list(df.columns))
        for col in ["日期 (Date)","食材名称 (Item Name)","分类 (Category)","状态 (Status)"]:
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

    # 统计
    stats_all = compute_stats(df)

    # “类型”列用于筛选
    if not df.empty and "食材名称 (Item Name)" in df.columns:
        latest_cat = (
            df.sort_values("日期 (Date)")
            .groupby("食材名称 (Item Name)")["分类 (Category)"]
            .agg(lambda s: s.dropna().iloc[-1] if len(s.dropna()) else DEFAULT_CAT)
        )
        stats_all = stats_all.merge(latest_cat.rename("类型"),
                                    left_on="食材名称 (Item Name)", right_index=True, how="left")
    else:
        stats_all["类型"] = DEFAULT_CAT
    stats_all["类型"] = stats_all["类型"].apply(normalize_cat)

    # === 筛选条（作用于下方结果表） ===
    st.markdown("#### 筛选")
    fc1, _ = st.columns([1, 3])
    sel_type_bar = fc1.selectbox("选择分类", ["全部"] + ALLOWED_CATS, index=0)
    if sel_type_bar == "全部":
        stats = stats_all.copy()
    else:
        stats = stats_all[stats_all["类型"].eq(sel_type_bar)].copy()

    # 预警：普通<5；百分比/糖浆<20%
    def _is_percent_row(row: pd.Series) -> bool:
        name = str(row.get("食材名称 (Item Name)","") or "")
        unit = str(row.get("单位 (Unit)","") or "").strip()
        last_rem = pd.to_numeric(row.get("最近剩余数量"), errors="coerce")
        if "糖浆" in name:
            return True
        if unit in ["%", "％", "百分比", "percent", "ratio"]:
            return True
        if pd.notna(last_rem) and 0.0 <= float(last_rem) <= 1.0:
            return True
        return False

    def badge_row(row: pd.Series) -> str:
        if _is_percent_row(row):
            val = pd.to_numeric(row.get("最近剩余数量"), errors="coerce")
            if pd.notna(val) and float(val) < 0.2:
                return "🚨 立即下单"
            return "🟢 正常"
        else:
            val = pd.to_numeric(row.get("当前库存"), errors="coerce")
            if pd.notna(val) and float(val) < 5:
                return "🚨 立即下单"
            return "🟢 正常"

    if not stats.empty:
        stats["库存预警"] = stats.apply(badge_row, axis=1)
    else:
        stats["库存预警"] = ""

    # 仅保留一个 KPI：记录食材数（删除其余三块）
    c1, = st.columns(1)
    total_items = int(stats["食材名称 (Item Name)"].nunique()) if not stats.empty and "食材名称 (Item Name)" in stats.columns else 0
    c1.metric("记录数量", value=total_items)

    # 结果表
    display_cols = [
        "食材名称 (Item Name)", "当前库存", "单位 (Unit)", "平均最近两周使用量",
        "预计还能用天数",
        "最近统计剩余日期", "最近采购日期",
        "最近采购数量", "最近采购单价",
        "平均采购间隔(天)", "累计支出", "库存预警"
    ]
    show = stats[[c for c in display_cols if c in stats.columns]].copy()

    # 排序：按预警严重&还能用天数
    severity = {"🚨 立即下单": 0, "🟢 正常": 2, "": 3}
    if "库存预警" in show.columns:
        show["__sev__"] = show["库存预警"].map(severity).fillna(3)
        if "预计还能用天数" in show.columns:
            show = show.sort_values(["__sev__", "预计还能用天数"], ascending=[True, True])
        show = show.drop(columns="__sev__", errors="ignore")

    if show.empty:
        st.info("暂无统计结果。请检查『购入/剩余』表的表头/数据是否完整。")
    st.dataframe(show, use_container_width=True)

    # 导出
    csv = show.to_csv(index=False).encode("utf-8-sig")
    st.download_button("⬇️ 导出统计结果（CSV）", data=csv,
                       file_name=f"inventory_stats.csv", mime="text/csv")

    # ============ 下钻：物品详情 ============
    st.markdown("### 🔍 物品详情")
    detail_items = ["（不选）"] + list(show["食材名称 (Item Name)"].dropna().unique()) if "食材名称 (Item Name)" in show.columns else ["（不选）"]
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
        days_left = (cur_stock / (use14/14.0)) if (use14 and use14>0 and not np.isnan(cur_stock)) else np.nan
        stockout_date = (pd.Timestamp.today().normalize() + pd.Timedelta(days=float(days_left))).date().isoformat() \
                        if days_left == days_left else "—"

        if len(buy) >= 2:
            avg_interval = buy["日期 (Date)"].diff().dt.days.dropna().mean()
        else:
            avg_interval = np.nan

        # KPI
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        k1.metric("当前库存", f"{0 if np.isnan(cur_stock) else cur_stock}")
        k2.metric("最近14天用量", f"{0 if not use14 else round(use14,2)}")
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
        ev = item_df[item_df["日期 (Date)"] >= lookback][["日期 (Date)","状态 (Status)","数量 (Qty)","单价 (Unit Price)"]].copy()
        if not ev.empty:
            ev["dt"] = pd.to_datetime(ev["日期 (Date)"])

            # 颜色映射：按“买入/剩余”两类指定固定颜色
            status_color = alt.Color(
                "状态 (Status):N",
                scale=alt.Scale(
                    domain=["买入", "剩余"],               # 类别顺序（确保颜色不会乱）
                    range=["#1f77b4", "#E4572E"]          # 对应颜色（可改成你喜欢的）
                ),
                legend=alt.Legend(title="状态")
            )

            chart_ev = alt.Chart(ev).mark_point(filled=True, size=80).encode(
                x=alt.X("dt:T", title="日期"),
                y=alt.Y("数量 (Qty):Q"),
                color=status_color,                      # ← 新增：颜色通道
                shape="状态 (Status):N",                 # 保留形状区分（可删）
                tooltip=["状态 (Status)","数量 (Qty)","单价 (Unit Price)","日期 (Date)"]
            ).properties(title=f"{picked} — 事件时间线（近60天）")

            st.altair_chart(chart_ev, use_container_width=True)


        # 最近记录
        st.markdown(" ")
        st.markdown("#### 最近记录（原始）")
        cols = ["日期 (Date)","状态 (Status)","数量 (Qty)","单位 (Unit)","单价 (Unit Price)","总价 (Total Cost)","分类 (Category)","备注 (Notes)"]
        cols = [c for c in cols if c in item_df.columns]
        st.dataframe(item_df[cols].sort_values("日期 (Date)").iloc[::-1].head(10), use_container_width=True)
