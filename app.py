# -*- coding: utf-8 -*-
import os
import pandas as pd
import streamlit as st
from compute import compute_stats
import altair as alt

# Choose backend (Google Sheets recommended)
from gsheet import read_records, append_record, STATUS_VALUES

# ====== 【替换开始】Secrets -> 写临时密钥文件 + 同步 SHEET_URL ======
import json

# 1) 如果 Secrets 里有 service_account，就写成临时文件给 gsheet.py 用
if "service_account" in st.secrets:
    with open("service_account.json", "w") as f:
        json.dump(dict(st.secrets["service_account"]), f)

# 2) 读取 INVENTORY_SHEET_URL（优先 Secrets，其次环境变量），并同步到环境变量
sheet_url = st.secrets.get("INVENTORY_SHEET_URL", None) or os.getenv("INVENTORY_SHEET_URL", None)
if sheet_url:
    os.environ["INVENTORY_SHEET_URL"] = sheet_url  # 供 gsheet.py 使用
# ====== 【替换结束】======

st.set_page_config(page_title="库存管理 Dashboard", layout="wide")

st.title("🍱 库存管理 Dashboard")
st.caption("录入‘买入/剩余’，自动保存到表格，并实时生成‘库存统计’分析")

with st.sidebar:
    st.header("⚙️ 设置 / Setup")
    st.write("请先在项目根目录放置 `service_account.json`（部署时由 Secrets 自动生成），并设置/填好 `INVENTORY_SHEET_URL`。")
    # ====== 【替换开始】显示我们刚同步的 sheet_url，而不是只读环境变量 ======
    st.code(f"INVENTORY_SHEET_URL={sheet_url or '(未设置)'}")
    # ====== 【替换结束】======
    if not sheet_url:
        st.error("未检测到 INVENTORY_SHEET_URL。请在 Streamlit Cloud 的 App → Settings → Secrets 中设置。")

    st.markdown("---")
    st.write("**如何找到 URL?** 打开你的目标表格 → 浏览器地址栏完整 URL。")

tabs = st.tabs(["➕ 录入记录", "📊 库存统计"])

# ===================== 录入 =====================
with tabs[0]:
    st.subheader("录入新记录（三步：选择 → 批量填写 → 保存）")

    # 1) 读取主数据（物品清单）
    from gsheet import read_catalog
    try:
        catalog = read_catalog()
    except Exception as e:
        st.error(f"读取物品清单失败：{e}")
        st.stop()

    if catalog.empty or not {"物品名","类型"}.issubset(set(catalog.columns)):
        st.warning("未找到“库存产品/Content_tracker/物品清单”工作表，或缺少‘物品名/类型’列。请在你的表格增加主数据表。")
        st.stop()

    # -------- Step A：三项选择 --------
    c1, c2, c3 = st.columns(3)
    sel_date   = c1.date_input("日期 (Date)", pd.Timestamp.today())
    sel_type   = c2.selectbox("类型", ["食物类","清洁类","消耗品","饮品类"])
    sel_status = c3.selectbox("状态 (Status)", ["买入","剩余"])

    # 2) 根据类型过滤物品清单
    items_df = catalog[catalog["类型"] == sel_type].copy().reset_index(drop=True)
    if items_df.empty:
        st.info("该类型下暂无物品。请先到主数据表中补充。")
        st.stop()

    # 生成可填写表格（数量、单价、备注）
    st.markdown("**在下表中为需要录入的物品填写数量（必填）与单价（仅买入时）**")
    edit_df = items_df[["物品名","单位"]].copy()
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
            "单价": st.column_config.NumberColumn(step=0.01, min_value=0.0) if sel_status=="买入" else None,
        },
        key="bulk_editor",
    )

    # 只保留填写了数量>0 的行
    try_submit = st.button("✅ 批量保存到『购入/剩余』")
    if try_submit:
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
            price = float(r["单价"]) if sel_status=="买入" and "单价" in r else None
            total = (qty * price) if (sel_status=="买入" and price is not None) else None
            unit  = str(r.get("单位","") or "")

            record = {
                "日期 (Date)": pd.to_datetime(sel_date).strftime("%Y-%m-%d"),
                "食材名称 (Item Name)": str(r["物品名"]).strip(),
                "分类 (Category)": sel_type,             # 用类型作为分类
                "数量 (Qty)": qty,
                "单位 (Unit)": unit,
                "单价 (Unit Price)": price if sel_status=="买入" else "",
                "总价 (Total Cost)": total if sel_status=="买入" else "",
                "状态 (Status)": sel_status,
                "备注 (Notes)": str(r.get("备注","")).strip()
            }

            try:
                append_record(record)
                ok += 1
            except Exception:
                fail += 1

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

    # --- 刷新按钮，清除缓存，避免429 ---
    from gsheet import read_records_cached, bust_cache
    colR1, colR2 = st.columns([1,3])
    if colR1.button("🔄 刷新数据", help="清空缓存并重新读取 Google Sheet"):
        bust_cache()
        st.experimental_rerun()

    # --- 阈值设置（可调） ---
    with st.expander("⚙️ 预警阈值设置", expanded=False):
        warn_days = st.number_input("关注阈值（天）", min_value=1, max_value=60, value=7, step=1)
        urgent_days = st.number_input("紧急阈值（天）", min_value=1, max_value=60, value=3, step=1)

    try:
        df = read_records_cached()
    except Exception as e:
        st.error(f"读取表格失败：{e}")
        st.stop()

    stats = compute_stats(df)  # 仍然沿用你的 compute.py 逻辑

    # === 组装列顺序，名称与 Google 表一致 ===
    # 将 NaN 转空串，方便显示
    show = stats.copy()
    for c in show.columns:
        show[c] = show[c].astype("object")

    # 计算“库存预警”
    import pandas as pd
    def badge(days):
        x = pd.to_numeric(days, errors="coerce")
        if pd.isna(x):
            return ""
        if x <= urgent_days:
            return "🚨 立即下单"
        if x <= warn_days:
            return "🟠 关注"
        return "🟢 正常"

    show["库存预警"] = show["预计还能用天数"].apply(badge)

    # 重命名/排序，尽量对齐你表上的列
    col_order = [
        "食材名称 (Item Name)",   # A
        "当前库存",               # B
        "平均最近两周使用量",     # C
        "预计还能用天数",         # D
        "计算下次采购量",         # E
        "最近统计剩余日期",       # F
        "最近采购日期",           # G
        "平均采购间隔(天)",       # H
        "最近采购数量",           # I
        "最近采购单价",           # J
        "累计支出",               # K
        "库存预警",               # L  (新增)
    ]
    show = show.reindex(columns=[c for c in col_order if c in show.columns])

    # KPI
    c1, c2, c3, c4 = st.columns(4)
    total_items = int(show["食材名称 (Item Name)"].nunique()) if not show.empty else 0
    total_spend = df.loc[df["状态 (Status)"]=="买入", "总价 (Total Cost)"].sum(min_count=1)
    low_days = pd.to_numeric(show["预计还能用天数"], errors="coerce")
    need_buy = int((low_days <= warn_days).sum()) if not show.empty else 0

    c1.metric("已记录食材数", value=total_items)
    c2.metric("累计支出", value=f"{total_spend:.2f}")
    c3.metric(f"≤{warn_days}天即将耗尽", value=need_buy)
    c4.metric("最近14天有使用记录数", value=int((pd.to_numeric(show["平均最近两周使用量"], errors="coerce")>0).sum()) if not show.empty else 0)

    st.dataframe(show, use_container_width=True)

    # 图表（保留）
    if not show.empty:
        st.markdown("#### Top 使用量（最近14天）")
        top_use = show.assign(**{
            "平均最近两周使用量": pd.to_numeric(show["平均最近两周使用量"], errors="coerce").fillna(0)
        }).sort_values("平均最近两周使用量", ascending=False).head(15)
        chart1 = alt.Chart(top_use).mark_bar().encode(
            x=alt.X("平均最近两周使用量:Q"),
            y=alt.Y("食材名称 (Item Name):N", sort="-x")
        )
        st.altair_chart(chart1, use_container_width=True)

        st.markdown("#### 预计还能用天数（越短越靠前）")
        tmp = show.assign(**{
            "预计还能用天数_num": pd.to_numeric(show["预计还能用天数"], errors="coerce")
        }).dropna(subset=["预计还能用天数_num"]).sort_values("预计还能用天数_num").head(15)
        if not tmp.empty:
            chart2 = alt.Chart(tmp).mark_bar().encode(
                x=alt.X("预计还能用天数_num:Q", title="预计还能用天数"),
                y=alt.Y("食材名称 (Item Name):N", sort="-x")
            )
            st.altair_chart(chart2, use_container_width=True)

    st.caption("说明：本页大部分列已实现。若与你的 Excel 口径不同，告诉我每列的精确计算方式，我立刻按你的规则更新。")
