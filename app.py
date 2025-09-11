# -*- coding: utf-8 -*-
import os
import json
import pandas as pd
import numpy as np
import streamlit as st

# ========== Secrets / ENV ==========
if "service_account" in st.secrets:
    with open("service_account.json", "w") as f:
        json.dump(dict(st.secrets["service_account"]), f)

sheet_url = st.secrets.get("INVENTORY_SHEET_URL", None) or os.getenv("INVENTORY_SHEET_URL", None)
if sheet_url:
    os.environ["INVENTORY_SHEET_URL"] = sheet_url  # 供 gsheet.py 使用

# ========== Backend ==========
from gsheet import append_record
try:
    from gsheet import read_records_cached as read_records_fn, bust_cache
except Exception:
    from gsheet import read_records as read_records_fn
    def bust_cache(): pass

# 统计计算（含“最近14天用量”的稳健算法）
try:
    from compute import compute_stats, _recent_usage_14d_robust as _recent_usage_14d_new
except Exception:
    from compute import compute_stats, _recent_usage_14d_new

# ========== UI ==========
st.set_page_config(page_title="库存管理 Dashboard", layout="wide")
st.title("🍱 库存管理 Dashboard")
st.caption("仅使用『购入/剩余』工作表：录入买入/剩余，自动生成统计。")

tabs = st.tabs(["➕ 录入记录", "📊 库存统计"])

# ================== 录入记录（不依赖 Content_tracker） ==================
with tabs[0]:
    st.subheader("录入新记录（直接填写，不依赖『Content_tracker』）")

    # 读取历史，用于提示
    try:
        df_hist = read_records_fn()
    except Exception as e:
        st.error(f"读取表格失败：{e}")
        st.stop()

    # 历史物品名/单位用于提示
    known_items = sorted(df_hist["食材名称 (Item Name)"].dropna().unique().tolist()) if not df_hist.empty else []
    # 物品最近一次单位
    last_unit = {}
    if not df_hist.empty:
        _tmp = (df_hist.sort_values("日期 (Date)")
                      .dropna(subset=["食材名称 (Item Name)"])
                      .groupby("食材名称 (Item Name)")["单位 (Unit)"]
                      .agg(lambda s: s.dropna().iloc[-1] if len(s.dropna()) else ""))
        last_unit = _tmp.to_dict()

    c1, c2, c3 = st.columns(3)
    sel_date   = c1.date_input("日期 (Date)", pd.Timestamp.today())
    sel_type   = c2.selectbox("分类 (Category)", ["食物类", "清洁类", "消耗品", "饮品类"])
    sel_status = c3.selectbox("状态 (Status)", ["买入", "剩余"])

    st.markdown("**在下表中填写需要录入的记录（数量必填；单价仅买入时需要）**")
    # 初始5行，支持动态增删
    template = pd.DataFrame({
        "物品名": ["" for _ in range(5)],
        "单位": ["" for _ in range(5)],
        "数量": [0.0 for _ in range(5)],
        "单价": [0.0 if sel_status == "买入" else None for _ in range(5)],
        "备注": ["" for _ in range(5)]
    })

    # 为了提示，把物品名列变成下拉（不过 data_editor 目前不支持原生下拉，只能文字提示）
    st.caption("提示：可以直接输入物品名；若之前录入过，单位会自动带出（可修改）")
    edited = st.data_editor(
        template,
        use_container_width=True,
        num_rows="dynamic",
        key="free_editor"
    )

    # 自动带单位（提交时兜底）
    def infer_unit(name: str, unit_now: str) -> str:
        if unit_now:  # 用户手填优先
            return unit_now
        return last_unit.get(name, "")

    if st.button("✅ 批量保存到『购入/剩余』"):
        rows = edited.copy()
        # 过滤有效行：物品名非空且数量>0
        rows = rows[(rows["物品名"].astype(str).str.strip() != "")
                    & (pd.to_numeric(rows["数量"], errors="coerce").fillna(0) > 0)]
        if rows.empty:
            st.warning("请至少填写一个有效的物品（物品名非空且数量>0）。")
            st.stop()

        ok, fail = 0, 0
        for _, r in rows.iterrows():
            name  = str(r["物品名"]).strip()
            qty   = float(r["数量"])
            unit  = infer_unit(name, str(r.get("单位","")).strip())

            # 单价/总价仅买入
            price = float(r["单价"]) if sel_status == "买入" and pd.notna(r.get("单价", None)) else None
            total = (qty * price) if (sel_status == "买入" and price is not None) else None

            record = {
                "日期 (Date)": pd.to_datetime(sel_date).strftime("%Y-%m-%d"),
                "食材名称 (Item Name)": name,
                "分类 (Category)": sel_type,
                "数量 (Qty)": qty,
                "单位 (Unit)": unit,
                "单价 (Unit Price)": price if sel_status == "买入" else "",
                "总价 (Total Cost)": total if sel_status == "买入" else "",
                "状态 (Status)": sel_status,
                "备注 (Notes)": str(r.get("备注","")).strip(),
            }
            try:
                append_record(record)
                ok += 1
            except Exception as e:
                fail += 1
                st.error(f"保存失败：{name} → {e}")

        if ok and not fail:
            st.success(f"已成功写入 {ok} 条记录！")
        elif ok and fail:
            st.warning(f"部分成功：{ok} 条成功，{fail} 条失败。")
        else:
            st.error("保存失败，请检查表格权限与 Secrets 配置。")

# ================== 库存统计（基于购入/剩余） ==================
with tabs[1]:
    st.subheader("库存统计（最近 14 天用量估算）")

    if st.button("🔄 刷新数据"):
        try: bust_cache()
        except: pass
        st.rerun()

    try:
        df = read_records_fn()
    except Exception as e:
        st.error(f"读取表格失败：{e}")
        st.stop()

    # 计算全量统计
    stats_all = compute_stats(df)

    # 类别来自「购入/剩余」中填写的 分类 (Category)
    all_types = sorted(df["分类 (Category)"].dropna().unique().tolist()) if not df.empty else []
    ctl1, ctl2, ctl3 = st.columns([1.2, 1, 1])
    sel_type = ctl1.selectbox("选择类别", ["全部"] + all_types, index=0)
    warn_days   = ctl2.number_input("关注阈值（天）", min_value=1, max_value=60, value=7, step=1)
    urgent_days = ctl3.number_input("紧急阈值（天）", min_value=1, max_value=60, value=3, step=1)

    stats = stats_all.copy()
    if sel_type != "全部":
        # 这里直接用统计表里的「分类 (Category)」列筛选
        if "分类 (Category)" in stats.columns:
            stats = stats[stats["分类 (Category)"] == sel_type]
        else:
            st.info("该类别下暂无统计结果（可能是分类列为空或未被识别）。")

    # 预警标签
    def badge(days):
        x = pd.to_numeric(days, errors="coerce")
        if pd.isna(x): return ""
        if x <= urgent_days: return "🚨 立即下单"
        if x <= warn_days:   return "🟠 关注"
        return "🟢 正常"
    if "预计还能用天数" in stats.columns:
        stats["库存预警"] = stats["预计还能用天数"].apply(badge)

    # KPI
    c1, c2, c3, c4 = st.columns(4)
    total_items = int(stats["食材名称 (Item Name)"].nunique()) if (not stats.empty and "食材名称 (Item Name)" in stats.columns) else 0
    total_spend = df.loc[df["状态 (Status)"] == "买入", "总价 (Total Cost)"].sum(min_count=1) if not df.empty else 0
    c1.metric("记录食材数", value=total_items)
    c2.metric("累计支出", value=f"{(total_spend or 0):.2f}")
    if not stats.empty and "预计还能用天数" in stats.columns and "平均最近两周使用量" in stats.columns:
        low_days = pd.to_numeric(stats["预计还能用天数"], errors="coerce")
        need_buy = int((low_days <= warn_days).sum())
        c3.metric(f"≤{warn_days}天即将耗尽", value=need_buy)
        recent_used_cnt = int((pd.to_numeric(stats["平均最近两周使用量"], errors="coerce") > 0).sum())
        c4.metric("最近14天有使用记录数", value=recent_used_cnt)
    else:
        c3.metric(f"≤{warn_days}天即将耗尽", value=0)
        c4.metric("最近14天有使用记录数", value=0)

    # 只展示统计结果列
    display_cols = [
        "食材名称 (Item Name)", "分类 (Category)",
        "当前库存", "平均最近两周使用量",
        "预计还能用天数", "计算下次采购量",
        "最近统计剩余日期", "最近采购日期",
        "最近采购数量", "最近采购单价",
        "平均采购间隔(天)", "累计支出", "库存预警"
    ]
    show_cols = [c for c in display_cols if c in stats.columns]
    show = stats[show_cols].copy() if show_cols else pd.DataFrame(columns=display_cols)

    if show.empty:
        st.info("该类别下暂无统计结果（可能是分类列为空或未被识别）。请检查『购入/剩余』表中的【分类 (Category)】是否填写正确。")
    else:
        # 按预警严重程度排序
        severity = {"🚨 立即下单": 0, "🟠 关注": 1, "🟢 正常": 2, "": 3}
        if "库存预警" in show.columns:
            show["__sev__"] = show["库存预警"].map(severity).fillna(3)
            if "预计还能用天数" in show.columns:
                show = show.sort_values(["__sev__", "预计还能用天数"], ascending=[True, True]).drop(columns="__sev__")
            else:
                show = show.sort_values(["__sev__"], ascending=[True]).drop(columns="__sev__")
        st.dataframe(show, use_container_width=True)

        # 导出
        csv = show.to_csv(index=False).encode("utf-8-sig")
        st.download_button("⬇️ 导出统计结果（CSV）", data=csv,
                           file_name=f"inventory_stats_{sel_type if sel_type!='全部' else 'all'}.csv",
                           mime="text/csv")

    # ===== 物品详情 =====
    st.markdown("### 🔍 物品详情")
    if show.empty:
        detail_options = ["（不选）"]
    else:
        detail_options = ["（不选）"] + list(show["食材名称 (Item Name)"].dropna().unique())

    picked = st.selectbox("选择一个物品查看详情", detail_options, index=0)

    if picked and picked != "（不选）":
        # 原始记录（仅该物品）
        item_df = df[df["食材名称 (Item Name)"] == picked].copy().sort_values("日期 (Date)")

        # 当前库存（最近一次剩余）
        rem = item_df[item_df["状态 (Status)"] == "剩余"].copy()
        latest_rem = rem.iloc[-1] if len(rem) else None
        cur_stock = float(latest_rem["数量 (Qty)"]) if latest_rem is not None else np.nan

        # 最近买入信息
        buy = item_df[item_df["状态 (Status)"] == "买入"].copy()
        last_buy = buy.iloc[-1] if len(buy) else None
        last_buy_date = (last_buy["日期 (Date)"].date().isoformat()
                         if last_buy is not None and pd.notna(last_buy["日期 (Date)"]) else "—")
        last_buy_qty   = float(last_buy["数量 (Qty)"]) if last_buy is not None else np.nan
        last_buy_price = float(last_buy["单价 (Unit Price)"]) if last_buy is not None else np.nan

        # 最近14天用量
        use14 = _recent_usage_14d_new(item_df)
        days_left = (cur_stock / (use14/14.0)) if (use14 and use14>0 and not np.isnan(cur_stock)) else np.nan
        stockout_date = (pd.Timestamp.today().normalize() + pd.Timedelta(days=float(days_left))).date().isoformat() \
                        if days_left == days_left else "—"

        # 平均采购间隔
        if len(buy) >= 2:
            avg_interval = (buy["日期 (Date)"].diff().dt.days.dropna().mean())
        else:
            avg_interval = np.nan

        # KPI
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("当前库存", f"{0 if np.isnan(cur_stock) else cur_stock}")
        k2.metric("最近14天用量", f"{0 if not use14 else round(use14,2)}")
        k3.metric("预计还能用天数", "—" if np.isnan(days_left) else f"{days_left:.2f}")
        k4.metric("最近采购日期", last_buy_date)

        # 最近60天：库存轨迹 + 事件点
        import altair as alt
        lookback = pd.Timestamp.today().normalize() - pd.Timedelta(days=60)
        rem60 = rem[rem["日期 (Date)"] >= lookback].copy()
        if not rem60.empty:
            rem60["dt"] = pd.to_datetime(rem60["日期 (Date)"])
            st.altair_chart(
                alt.Chart(rem60).mark_line(point=True).encode(
                    x=alt.X("dt:T", title="日期"),
                    y=alt.Y("数量 (Qty):Q", title="剩余数量")
                ).properties(title=f"{picked} — 剩余数量（近60天）"),
                use_container_width=True
            )

        ev = item_df[item_df["日期 (Date)"] >= lookback][["日期 (Date)","状态 (Status)","数量 (Qty)","单价 (Unit Price)"]].copy()
        if not ev.empty:
            ev["dt"] = pd.to_datetime(ev["日期 (Date)"])
            st.altair_chart(
                alt.Chart(ev).mark_point(filled=True).encode(
                    x=alt.X("dt:T", title="日期"),
                    y=alt.Y("数量 (Qty):Q"),
                    shape="状态 (Status):N",
                    tooltip=["状态 (Status)","数量 (Qty)","单价 (Unit Price)","日期 (Date)"]
                ).properties(title=f"{picked} — 事件时间线（近60天）"),
                use_container_width=True
            )

        # 最近记录（原始）
        st.markdown("#### 最近记录（原始）")
        cols = ["日期 (Date)","状态 (Status)","数量 (Qty)","单价 (Unit Price)","总价 (Total Cost)","分类 (Category)","备注 (Notes)"]
        show_raw = item_df[cols] if all(c in item_df.columns for c in cols) else item_df
        st.dataframe(show_raw.sort_values("日期 (Date)", ascending=False).head(10), use_container_width=True)
