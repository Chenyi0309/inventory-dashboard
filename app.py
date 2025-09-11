# -*- coding: utf-8 -*-
import os
import json
import pandas as pd
import numpy as np
import streamlit as st
import altair as alt

# ====== Secrets -> 写临时密钥文件 + 同步 SHEET_URL ======
if "service_account" in st.secrets:
    with open("service_account.json", "w") as f:
        json.dump(dict(st.secrets["service_account"]), f)

sheet_url = st.secrets.get("INVENTORY_SHEET_URL", None) or os.getenv("INVENTORY_SHEET_URL", None)
if sheet_url:
    os.environ["INVENTORY_SHEET_URL"] = sheet_url  # 供 gsheet.py 使用

# ====== Backend ======
from gsheet import append_record, STATUS_VALUES
# 兼容：有缓存优先用缓存
try:
    from gsheet import read_records_cached as read_records_fn
except Exception:
    from gsheet import read_records as read_records_fn

try:
    from gsheet import read_catalog_cached as read_catalog_fn
except Exception:
    try:
        from gsheet import read_catalog as read_catalog_fn
    except Exception:
        def read_catalog_fn():
            return pd.DataFrame()

# ====== UI 基础 ======
st.set_page_config(page_title="库存管理 Dashboard", layout="wide")
st.title("🍱 库存管理 Dashboard")
st.caption("录入‘买入/剩余’，自动保存到表格，并实时生成‘库存统计’分析")

with st.sidebar:
    st.header("⚙️ 设置 / Setup")
    st.write("请先在项目根目录放置 `service_account.json`（部署时由 Secrets 自动生成），并设置/填好 `INVENTORY_SHEET_URL`。")
    st.code(f"INVENTORY_SHEET_URL={sheet_url or '(未设置)'}")
    if not sheet_url:
        st.error("未检测到 INVENTORY_SHEET_URL。请到 App → Settings → Secrets 中设置。")
    st.markdown("---")
    st.write("**如何找到 URL?** 打开目标表格 → 复制浏览器地址栏完整 URL。")

tabs = st.tabs(["➕ 录入记录", "📊 库存统计"])

# ===================== 录入 =====================
with tabs[0]:
    st.subheader("录入新记录（三步：选择 → 批量填写 → 保存）")

    # 1) 读取主数据（物品清单）
    try:
        catalog = read_catalog_fn()
    except Exception as e:
        st.error(f"读取物品清单失败：{e}")
        st.stop()

    if catalog.empty or not {"物品名", "类型"}.issubset(set(catalog.columns)):
        st.warning("未找到“库存产品/Content_tracker/物品清单”工作表，或缺少‘物品名/类型’列。")
        st.stop()

    # -------- Step A：三项选择 --------
    c1, c2, c3 = st.columns(3)
    sel_date   = c1.date_input("日期 (Date)", pd.Timestamp.today())
    sel_type   = c2.selectbox("类型", ["食物类", "清洁类", "消耗品", "饮品类"])
    sel_status = c3.selectbox("状态 (Status)", ["买入", "剩余"])

    # 2) 根据类型过滤物品清单
    items_df = catalog[catalog["类型"] == sel_type].copy().reset_index(drop=True)
    if items_df.empty:
        st.info("该类型下暂无物品。请先到主数据表中补充。")
        st.stop()

    # 3) 批量编辑表
    st.markdown("**在下表中为需要录入的物品填写数量（必填）与单价（仅买入时）**")
    edit_df = items_df[["物品名", "单位"]].copy()
    edit_df["数量"] = 0.0
    if sel_status == "买入":
        edit_df["单价"] = 0.0
    edit_df["备注"] = ""

    edited = st.data_editor(
        edit_df,
        use_container_width=True,
        num_rows="dynamic",
        column_config={
            "物品名": st.column_config.Column(disabled=True),
            "单位": st.column_config.Column(disabled=True),
            "数量": st.column_config.NumberColumn(step=0.1, min_value=0.0),
            "单价": st.column_config.NumberColumn(step=0.01, min_value=0.0) if sel_status == "买入" else None,
        },
        key="bulk_editor",
    )

    # 保存
    if st.button("✅ 批量保存到『购入/剩余』"):
        if "数量" not in edited.columns:
            st.error("请至少为一个物品填写‘数量’。")
            st.stop()

        rows = edited.copy()
        rows = rows[pd.to_numeric(rows["数量"], errors="coerce").fillna(0) > 0]
        if rows.empty:
            st.warning("你还没有为任何物品填写数量。")
            st.stop()

        ok, fail = 0, 0
        for _, r in rows.iterrows():
            qty   = float(r["数量"])
            price = float(r["单价"]) if sel_status == "买入" and "单价" in r else None
            total = (qty * price) if (sel_status == "买入" and price is not None) else None
            unit  = str(r.get("单位", "") or "")

            record = {
                "日期 (Date)": pd.to_datetime(sel_date).strftime("%Y-%m-%d"),
                "食材名称 (Item Name)": str(r["物品名"]).strip(),
                "分类 (Category)": sel_type,   # 用类型作为分类
                "数量 (Qty)": qty,
                "单位 (Unit)": unit,
                "单价 (Unit Price)": price if sel_status == "买入" else "",
                "总价 (Total Cost)": total if sel_status == "买入" else "",
                "状态 (Status)": sel_status,
                "备注 (Notes)": str(r.get("备注", "")).strip(),
            }

            try:
                append_record(record)
                ok += 1
            except Exception as e:
                fail += 1
                st.error(f"保存失败：{e}")

        # 结果提示
        if ok and not fail:
            st.success(f"已成功写入 {ok} 条记录！")
        elif ok and fail:
            st.warning(f"部分成功：{ok} 条成功，{fail} 条失败。")
        else:
            st.error("保存失败，请检查表格权限与 Secrets 配置。")

    st.caption("提示：单价只在‘买入’状态下需要填写；‘剩余’只统计数量。")

# ===================== 统计 =====================
with tabs[1]:
    st.subheader("库存统计（自动根据最近 14 天使用量估算）")

    # 刷新按钮（清缓存 + 重新运行）
    try:
        from gsheet import bust_cache
    except Exception:
        def bust_cache(): pass

    colR1, colR2 = st.columns([1, 3])
    if colR1.button("🔄 刷新数据", help="清空缓存并重新读取 Google Sheet"):
        try:
            bust_cache()
        except Exception:
            pass
        st.rerun()  # 新版 Streamlit

    # 读取数据
    try:
        df = read_records_fn()
    except Exception as e:
        st.error(f"读取表格失败：{e}")
        st.stop()

    # 侧边栏：预警阈值 + 类型筛选
    with st.sidebar:
        st.markdown("### 🔎 统计筛选")
        warn_days = st.number_input("关注阈值（天）", min_value=1, max_value=60, value=7, step=1)
        urgent_days = st.number_input("紧急阈值（天）", min_value=1, max_value=60, value=3, step=1)
        sel_type = st.selectbox("类型（筛选）", ["全部", "食物类", "清洁类", "消耗品", "饮品类"], index=0)

    # 计算汇总
    from compute import compute_stats, _recent_usage_14d_new, _latest_remainder_row, _last_buy_row
    stats_all = compute_stats(df)

    # 给出“库存预警”列 + 类型列（取每个 item 最近一次记录的分类）
    def badge(days):
        x = pd.to_numeric(days, errors="coerce")
        if pd.isna(x): return ""
        if x <= urgent_days: return "🚨 立即下单"
        if x <= warn_days:   return "🟠 关注"
        return "🟢 正常"

    latest_cat = (
        df.sort_values("日期 (Date)")
          .groupby("食材名称 (Item Name)")["分类 (Category)"]
          .agg(lambda s: s.dropna().iloc[-1] if len(s.dropna()) else "")
    )
    stats = stats_all.merge(latest_cat.rename("类型"),
                            left_on="食材名称 (Item Name)", right_index=True, how="left")

    if sel_type != "全部":
        stats = stats[stats["类型"].eq(sel_type)]

    stats["库存预警"] = stats["预计还能用天数"].apply(badge)

    # KPI
    c1, c2, c3, c4 = st.columns(4)
    total_items = int(stats["食材名称 (Item Name)"].nunique()) if not stats.empty else 0
    total_spend = df.loc[df["状态 (Status)"] == "买入", "总价 (Total Cost)"].sum(min_count=1)
    low_days = pd.to_numeric(stats["预计还能用天数"], errors="coerce")
    need_buy = int((low_days <= warn_days).sum()) if not stats.empty else 0
    c1.metric("已记录食材数", value=total_items)
    c2.metric("累计支出", value=f"{(total_spend or 0):.2f}")
    c3.metric(f"≤{warn_days}天即将耗尽", value=need_buy)
    c4.metric("最近14天有使用记录数",
              value=int((pd.to_numeric(stats["平均最近两周使用量"], errors="coerce") > 0).sum()) if not stats.empty else 0)

    # 汇总表
    display_cols = [
        "食材名称 (Item Name)", "类型", "当前库存", "平均最近两周使用量",
        "预计还能用天数", "计算下次采购量", "最近统计剩余日期", "最近采购日期",
        "平均采购间隔(天)", "最近采购数量", "最近采购单价", "累计支出", "库存预警",
    ]
    show = stats[[c for c in display_cols if c in stats.columns]].copy()
    st.dataframe(show, use_container_width=True)

    # Drill-down 详情
    st.markdown("### 🔍 物品详情")
    item_options = ["（不选）"] + list(show["食材名称 (Item Name)"].dropna().unique())
    picked = st.selectbox("选择一个物品查看详情", item_options, index=0)
    if picked and picked != "（不选）":
        item_df = df[df["食材名称 (Item Name)"] == picked].copy().sort_values("日期 (Date)")
        latest_rem = _latest_remainder_row(item_df)
        last_buy   = _last_buy_row(item_df)
        use_14 = _recent_usage_14d_new(item_df)
        cur_stock = float(latest_rem["数量 (Qty)"]) if latest_rem is not None else np.nan
        days_left = (cur_stock / (use_14 / 14.0)) if use_14 and use_14 > 0 and not np.isnan(cur_stock) else np.nan

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("当前库存", f"{cur_stock if cur_stock == cur_stock else 0}")
        k2.metric("最近14天用量", f"{use_14:.2f}")
        k3.metric("预计还能用天数", f"{days_left:.2f}" if days_left == days_left else "—")
        k4.metric("最近采购日期",
                  last_buy["日期 (Date)"].date().isoformat() if last_buy is not None and pd.notna(last_buy["日期 (Date)"]) else "—")

        # 最近60天图
        lookback_start = (pd.Timestamp.today().normalize() - pd.Timedelta(days=60))
        tl = item_df[item_df["日期 (Date)"] >= lookback_start][["日期 (Date)", "状态 (Status)", "数量 (Qty)"]].copy()
        if not tl.empty:
            rem = tl[tl["状态 (Status)"] == "剩余"].copy()
            if not rem.empty:
                rem["dt"] = pd.to_datetime(rem["日期 (Date)"])
                rem_chart = alt.Chart(rem).mark_line(point=True).encode(
                    x=alt.X("dt:T", title="日期"),
                    y=alt.Y("数量 (Qty):Q", title="剩余数量")
                ).properties(title="剩余数量（近60天）")
                st.altair_chart(rem_chart, use_container_width=True)

            tl["dt"] = pd.to_datetime(tl["日期 (Date)"])
            pts = alt.Chart(tl).mark_point(filled=True).encode(
                x=alt.X("dt:T", title="日期"),
                y=alt.Y("数量 (Qty):Q"),
                shape="状态 (Status):N",
                tooltip=["状态 (Status)", "数量 (Qty)", "日期 (Date)"]
            ).properties(title="事件时间线（买入/剩余）")
            st.altair_chart(pts, use_container_width=True)

        st.markdown("#### 最近记录（原始）")
        cols = ["日期 (Date)", "状态 (Status)", "数量 (Qty)", "单价 (Unit Price)",
                "总价 (Total Cost)", "分类 (Category)", "备注 (Notes)"]
        st.dataframe(item_df[cols].sort_values("日期 (Date)", ascending=False).head(10), use_container_width=True)
