# -*- coding: utf-8 -*-
import os
import json
import pandas as pd
import numpy as np
import streamlit as st

# ========== Secrets ==========
if "service_account" in st.secrets:
    with open("service_account.json", "w") as f:
        json.dump(dict(st.secrets["service_account"]), f)

sheet_url = st.secrets.get("INVENTORY_SHEET_URL", None) or os.getenv("INVENTORY_SHEET_URL", None)
if sheet_url:
    os.environ["INVENTORY_SHEET_URL"] = sheet_url  # 供 gsheet.py 使用

# ========== Backend ==========
from gsheet import append_record
try:
    from gsheet import read_records_cached as read_records_fn, read_catalog_cached as read_catalog_fn, bust_cache
except Exception:
    from gsheet import read_records as read_records_fn, read_catalog as read_catalog_fn
    def bust_cache(): pass

# compute 模块（含新的两周用量算法）
try:
    from compute import compute_stats, _recent_usage_14d_robust as _recent_usage_14d_new
except Exception:
    from compute import compute_stats, _recent_usage_14d_new

# ========== UI ==========
st.set_page_config(page_title="库存管理 Dashboard", layout="wide")
st.title("🍱 库存管理 Dashboard")
st.caption("录入‘买入/剩余’，自动保存到表格，并实时生成‘库存统计’分析")

tabs = st.tabs(["➕ 录入记录", "📊 库存统计"])

# ================== 录入记录 ==================
with tabs[0]:
    st.subheader("录入新记录")

    # 主数据
    try:
        catalog = read_catalog_fn()
    except Exception as e:
        st.error(f"读取物品清单失败：{e}")
        st.stop()

    if catalog.empty or not {"物品名", "类型"}.issubset(set(catalog.columns)):
        st.warning("未找到物品清单工作表，或缺少‘物品名/类型’列。")
        st.stop()

    c1, c2, c3 = st.columns(3)
    sel_date   = c1.date_input("日期 (Date)", pd.Timestamp.today())
    sel_type   = c2.selectbox("类型", ["食物类", "清洁类", "消耗品", "饮品类"])
    sel_status = c3.selectbox("状态 (Status)", ["买入", "剩余"])

    items_df = catalog[catalog["类型"] == sel_type].copy().reset_index(drop=True)
    if items_df.empty:
        st.info("该类型下暂无物品。")
        st.stop()

    st.markdown("**在下表中填写数量（必填），单价仅在买入时填写**")
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

    if st.button("✅ 批量保存到『购入/剩余』"):
        rows = edited.copy()
        rows = rows[pd.to_numeric(rows["数量"], errors="coerce").fillna(0) > 0]
        if rows.empty:
            st.warning("请至少填写一个物品的数量")
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
                "分类 (Category)": sel_type,
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

        if ok and not fail:
            st.success(f"已成功写入 {ok} 条记录！")
        elif ok and fail:
            st.warning(f"部分成功：{ok} 条成功，{fail} 条失败。")
        else:
            st.error("保存失败，请检查表格权限与 Secrets 配置。")

# ================== 库存统计 ==================
with tabs[1]:
    st.subheader("库存统计（最近 14 天用量估算）")

    top_l, top_r = st.columns([1, 3])
    if top_l.button("🔄 刷新数据", help="清空缓存并重新读取 Google Sheet"):
        try: bust_cache()
        except: pass
        st.rerun()

    # 读明细
    try:
        df = read_records_fn()
    except Exception as e:
        st.error(f"读取表格失败：{e}")
        st.stop()

    # 计算整体统计
    stats_all = compute_stats(df)

    # 每个物品的“最近分类”（用于筛选）
    latest_cat = (
        df.sort_values("日期 (Date)")
          .groupby("食材名称 (Item Name)")["分类 (Category)"]
          .agg(lambda s: s.dropna().iloc[-1] if len(s.dropna()) else "")
    )
    stats_all = stats_all.merge(latest_cat.rename("类型"),
                                left_on="食材名称 (Item Name)", right_index=True, how="left")

    # 选择类别 + 阈值
    ctl1, ctl2, ctl3 = st.columns([1.2, 1, 1])
    sel_type = ctl1.selectbox("选择类别", ["食物类", "清洁类", "消耗品", "饮品类"], index=0)
    warn_days   = ctl2.number_input("关注阈值（天）", min_value=1, max_value=60, value=7, step=1)
    urgent_days = ctl3.number_input("紧急阈值（天）", min_value=1, max_value=60, value=3, step=1)

    stats = stats_all[stats_all["类型"].eq(sel_type)].copy()

    # 预警标签
    def badge(days):
        x = pd.to_numeric(days, errors="coerce")
        if pd.isna(x): return ""
        if x <= urgent_days: return "🚨 立即下单"
        if x <= warn_days:   return "🟠 关注"
        return "🟢 正常"
    stats["库存预警"] = stats["预计还能用天数"].apply(badge)

    # KPI（只针对选中类别）
    c1, c2, c3, c4 = st.columns(4)
    total_items = int(stats["食材名称 (Item Name)"].nunique()) if not stats.empty else 0
    total_spend = df.loc[df["状态 (Status)"] == "买入", "总价 (Total Cost)"].sum(min_count=1)
    low_days = pd.to_numeric(stats["预计还能用天数"], errors="coerce")
    need_buy = int((low_days <= warn_days).sum()) if not stats.empty else 0
    c1.metric(f"{sel_type} — 记录食材数", value=total_items)
    c2.metric("累计支出", value=f"{(total_spend or 0):.2f}")
    c3.metric(f"≤{warn_days}天即将耗尽", value=need_buy)
    c4.metric("最近14天有使用记录数",
              value=int((pd.to_numeric(stats["平均最近两周使用量"], errors="coerce") > 0).sum())
              if not stats.empty else 0)

    # 展示列（只展示统计结果）
    display_cols = [
        "食材名称 (Item Name)", "当前库存", "平均最近两周使用量",
        "预计还能用天数", "计算下次采购量", "最近统计剩余日期",
        "最近采购日期", "最近采购数量", "最近采购单价",
        "平均采购间隔(天)", "累计支出", "库存预警"
    ]
    show = stats[[c for c in display_cols if c in stats.columns]].copy()

    # 按预警严重程度排序：🚨 > 🟠 > 🟢 > 空
    severity = {"🚨 立即下单": 0, "🟠 关注": 1, "🟢 正常": 2, "": 3}
    show["__sev__"] = show["库存预警"].map(severity).fillna(3)
    show = show.sort_values(["__sev__", "预计还能用天数"], ascending=[True, True]).drop(columns="__sev__")

    st.dataframe(show, use_container_width=True)

    # 导出
    csv = show.to_csv(index=False).encode("utf-8-sig")
    st.download_button("⬇️ 导出统计结果（CSV）", data=csv,
                       file_name=f"inventory_stats_{sel_type}.csv", mime="text/csv")

# ====== 下钻：物品详情 ======
st.markdown("### 🔍 物品详情")
detail_items = ["（不选）"] + list(show["食材名称 (Item Name)"].dropna().unique())
picked = st.selectbox("选择一个物品查看详情", detail_items, index=0)

if picked and picked != "（不选）":
    # 取该物品的原始记录
    item_df = (
        df[df["食材名称 (Item Name)"] == picked]
        .copy()
        .sort_values("日期 (Date)")
    )

    # 最近一次“剩余”→ 当前库存
    rem = item_df[item_df["状态 (Status)"] == "剩余"].copy()
    latest_rem = rem.iloc[-1] if len(rem) else None
    cur_stock = float(latest_rem["数量 (Qty)"]) if latest_rem is not None else np.nan

    # 最近“买入”
    buy = item_df[item_df["状态 (Status)"] == "买入"].copy()
    last_buy = buy.iloc[-1] if len(buy) else None
    last_buy_date = (last_buy["日期 (Date)"].date().isoformat()
                     if last_buy is not None and pd.notna(last_buy["日期 (Date)"]) else "—")
    last_buy_qty  = float(last_buy["数量 (Qty)"]) if last_buy is not None else np.nan
    last_buy_price = float(last_buy["单价 (Unit Price)"]) if last_buy is not None else np.nan

    # 最近14天用量（稳健算法）
    try:
        from compute import _recent_usage_14d_robust as recent14
    except Exception:
        from compute import _recent_usage_14d_new as recent14
    use14 = recent14(item_df)

    # 预计还能用天数 & 缺货日期
    days_left = (cur_stock / (use14/14.0)) if (use14 and use14>0 and not np.isnan(cur_stock)) else np.nan
    stockout_date = (pd.Timestamp.today().normalize() + pd.Timedelta(days=float(days_left))).date().isoformat() \
                    if days_left == days_left else "—"

    # 平均采购间隔
    if len(buy) >= 2:
        avg_interval = (buy["日期 (Date)"].diff().dt.days.dropna().mean())
    else:
        avg_interval = np.nan

    # 近14天的总采购额 / 日均花费
    end = pd.Timestamp.today().normalize()
    start = end - pd.Timedelta(days=14)
    recent_buys = buy[(buy["日期 (Date)"] >= start) & (buy["日期 (Date)"] <= end)]
    spend14 = recent_buys["总价 (Total Cost)"].sum(min_count=1)
    spend14_perday = spend14 / 14.0 if spend14 == spend14 else np.nan

    # 异常：最近14天内是否存在“剩余上涨但中间无买入”
    recent_rem = rem[rem["日期 (Date)"] >= start].copy()
    anomaly = ""
    if len(recent_rem) >= 2:
        recent_rem["diff"] = recent_rem["数量 (Qty)"].diff()
        if (recent_rem["diff"] > 0).any():  # 出现上涨
            # 检查上涨区间内是否有买入
            # 简化判断：有上涨就提示可能漏记
            anomaly = "⚠️ 最近14天内出现‘剩余上涨’且未检测到对应‘买入’，可能漏记。"

    # KPI
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("当前库存", f"{0 if np.isnan(cur_stock) else cur_stock}")
    k2.metric("最近14天用量", f"{0 if not use14 else round(use14,2)}")
    k3.metric("预计还能用天数", "—" if np.isnan(days_left) else f"{days_left:.2f}")
    k4.metric("预计缺货日期", stockout_date)
    k5.metric("最近采购日期", last_buy_date)
    k6.metric("平均采购间隔(天)", "—" if np.isnan(avg_interval) else f"{avg_interval:.1f}")

    if anomaly:
        st.warning(anomaly)

    # ---------- 图表 ----------
    # 1) 库存轨迹（近60天，仅“剩余”）
    lookback = pd.Timestamp.today().normalize() - pd.Timedelta(days=60)
    rem60 = rem[rem["日期 (Date)"] >= lookback].copy()
    if not rem60.empty:
        import altair as alt
        rem60["dt"] = pd.to_datetime(rem60["日期 (Date)"])
        chart_stock = alt.Chart(rem60).mark_line(point=True).encode(
            x=alt.X("dt:T", title="日期"),
            y=alt.Y("数量 (Qty):Q", title="剩余数量")
        ).properties(title=f"{picked} — 剩余数量（近60天）")
        st.altair_chart(chart_stock, use_container_width=True)

    # 2) 买入/剩余事件时间线（近60天）
    ev = item_df[item_df["日期 (Date)"] >= lookback][["日期 (Date)","状态 (Status)","数量 (Qty)","单价 (Unit Price)"]].copy()
    if not ev.empty:
        ev["dt"] = pd.to_datetime(ev["日期 (Date)"])
        chart_ev = alt.Chart(ev).mark_point(filled=True).encode(
            x=alt.X("dt:T", title="日期"),
            y=alt.Y("数量 (Qty):Q"),
            shape="状态 (Status):N",
            tooltip=["状态 (Status)","数量 (Qty)","单价 (Unit Price)","日期 (Date)"]
        ).properties(title=f"{picked} — 事件时间线（近60天）")
        st.altair_chart(chart_ev, use_container_width=True)

    # 3) 最近记录（原始）- 最近10条
    st.markdown("#### 最近记录（原始）")
    cols = ["日期 (Date)","状态 (Status)","数量 (Qty)","单价 (Unit Price)","总价 (Total Cost)","分类 (Category)","备注 (Notes)"]
    st.dataframe(item_df[cols].sort_values("日期 (Date)", ascending=False).head(10), use_container_width=True)
