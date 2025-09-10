# -*- coding: utf-8 -*-
import os
import pandas as pd
import streamlit as st
from compute import compute_stats
import altair as alt

# Choose backend (Google Sheets recommended)
from gsheet import read_records, append_record, STATUS_VALUES

import json, os
# 如果放在 Secrets，就写一个临时文件给 gsheet.py 用
if "service_account" in st.secrets:
    with open("service_account.json", "w") as f:
        json.dump(dict(st.secrets["service_account"]), f)


st.set_page_config(page_title="库存管理 Dashboard", layout="wide")

st.title("🍱 库存管理 Dashboard")
st.caption("录入‘买入/剩余’，自动保存到表格，并实时生成‘库存统计’分析")

with st.sidebar:
    st.header("⚙️ 设置 / Setup")
    st.write("请先在项目根目录放置 `service_account.json`，并设置环境变量 `INVENTORY_SHEET_URL` 指向你的 Google 表格 URL。")
    sheet_url = os.getenv("INVENTORY_SHEET_URL", "(未设置)")
    st.code(f"INVENTORY_SHEET_URL={sheet_url}")

    st.markdown("---")
    st.write("**如何找到 URL?** 打开你的目标表格 → 浏览器地址栏完整 URL。")

tabs = st.tabs(["➕ 录入记录", "📊 库存统计"])

# ===================== 录入 =====================
with tabs[0]:
    st.subheader("录入新记录（保存到 ‘购入/剩余’ 工作表）")

    # Load existing for dropdowns
    try:
        df_all = read_records()
    except Exception as e:
        st.error(f"读取表格失败：{e}")
        st.stop()

    items = sorted([x for x in df_all["食材名称 (Item Name)"].dropna().unique() if x])
    cats  = sorted([x for x in df_all["分类 (Category)"].dropna().unique() if x])
    units = sorted([x for x in df_all["单位 (Unit)"].dropna().unique() if x])

    with st.form("entry_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        date = c1.date_input("日期 (Date)", pd.Timestamp.today())
        status = c2.selectbox("状态 (Status)", STATUS_VALUES, index=0)
        cat = c3.selectbox("分类 (Category)", options=[""] + cats, index=0, placeholder="可直接输入新分类")

        item = st.selectbox("食材名称 (Item Name)", options=[""] + items, index=0, placeholder="可直接输入新名称")
        if not item:
            item = st.text_input("或手动输入新‘食材名称’")

        c4, c5, c6 = st.columns(3)
        qty = c4.number_input("数量 (Qty)", min_value=0.0, step=0.1)
        unit = c5.selectbox("单位 (Unit)", options=[""] + units, index=0, placeholder="可直接输入新单位")
        price = c6.number_input("单价 (Unit Price) — 仅‘买入’需要", min_value=0.0, step=0.01) if status == "买入" else 0.0

        notes = st.text_input("备注 (Notes)", "")

        total = qty * price if status == "买入" else 0.0
        st.caption(f"总价 (Total Cost): {total:.2f}" if status == "买入" else "总价 (Total Cost): （剩余无需填写）")

        submitted = st.form_submit_button("✅ 保存到 ‘购入/剩余’")
        if submitted:
            record = {
                "日期 (Date)": pd.to_datetime(date).strftime("%Y-%m-%d"),
                "食材名称 (Item Name)": item.strip(),
                "分类 (Category)": (cat or "").strip(),
                "数量 (Qty)": qty,
                "单位 (Unit)": (unit or "").strip(),
                "单价 (Unit Price)": price if status == "买入" else "",
                "总价 (Total Cost)": total if status == "买入" else "",
                "状态 (Status)": status,
                "备注 (Notes)": notes.strip()
            }
            try:
                append_record(record)
                st.success("已保存！请到右侧‘库存统计’查看效果。")
            except Exception as e:
                st.error(f"保存失败：{e}")

    st.markdown("—")
    st.caption("提示：下拉框里没有想要的内容？直接在框里输入新值即可。")

# ===================== 统计 =====================
with tabs[1]:
    st.subheader("库存统计（自动根据最近 14 天使用量估算）")
    try:
        df = read_records()
    except Exception as e:
        st.error(f"读取表格失败：{e}")
        st.stop()

    stats = compute_stats(df)

    # KPI bar
    c1, c2, c3, c4 = st.columns(4)
    total_items = (stats["食材名称 (Item Name)"].nunique()) if not stats.empty else 0
    total_spend = df.loc[df["状态 (Status)"]=="买入", "总价 (Total Cost)"].sum(min_count=1)
    low_days = stats["预计还能用天数"].apply(lambda x: pd.to_numeric(x, errors="coerce")).fillna(9999)
    need_buy = int((low_days <= 7).sum()) if not stats.empty else 0

    c1.metric("已记录食材数", value=total_items)
    c2.metric("累计支出", value=f"{total_spend:.2f}")
    c3.metric("≤7天即将耗尽", value=need_buy)
    c4.metric("最近 14 天有使用记录数", value=int((stats["平均最近两周使用量"]>0).sum()) if not stats.empty else 0)

    st.dataframe(stats, use_container_width=True)

    # Charts
    if not stats.empty:
        st.markdown("#### Top 使用量（最近14天）")
        top_use = stats.sort_values("平均最近两周使用量", ascending=False).head(15)
        chart1 = alt.Chart(top_use).mark_bar().encode(
            x=alt.X("平均最近两周使用量:Q"),
            y=alt.Y("食材名称 (Item Name):N", sort="-x")
        )
        st.altair_chart(chart1, use_container_width=True)

        st.markdown("#### 预计还能用天数（越短越靠前）")
        tmp = stats.copy()
        tmp["预计还能用天数_num"] = pd.to_numeric(tmp["预计还能用天数"], errors="coerce")
        tmp = tmp.dropna(subset=["预计还能用天数_num"]).sort_values("预计还能用天数_num").head(15)
        if not tmp.empty:
            chart2 = alt.Chart(tmp).mark_bar().encode(
                x=alt.X("预计还能用天数_num:Q", title="预计还能用天数"),
                y=alt.Y("食材名称 (Item Name):N", sort="-x")
            )
            st.altair_chart(chart2, use_container_width=True)

    st.caption("说明：‘平均最近两周使用量’= 统计窗口内各区间（买入→剩余、剩余→剩余且减少）的使用量按天均分并加总，自动忽略无买入情况下剩余增加的异常记录。")
