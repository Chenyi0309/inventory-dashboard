# 🍱 库存管理 Dashboard（Streamlit + Google Sheets）

目标：在网页里录入【买入/剩余】数据，自动写入 Google 表格的“购入/剩余”工作表；同时在 Dashboard 页面实时计算并展示【库存统计】（最近两周平均使用量、预计还能用天数、建议下次采购量、最近采购信息等）。

---

## 一、准备工作（一次性）

1) **创建 Google 表格**
   - 新建一个表格（或使用你现有的），建议包含工作表：`购入/剩余`。
   - 第一行表头建议如下（可以是中文/英文混排，代码会做兼容）：  
     `日期 (Date) | 食材名称 (Item Name) | 分类 (Category) | 数量 (Qty) | 单位 (Unit) | 单价 (Unit Price) | 总价 (Total Cost) | 状态 (Status) | 备注 (Notes)`  
     其中 **状态** 只有两种：`买入`、`剩余`。

2) **开启 Google Sheets API + 服务账号**
   - 打开 https://console.cloud.google.com/ 新建项目。
   - 在 “API 与服务 → 启用 API 与服务” 中搜索开启 **Google Sheets API**。
   - 在 “凭据” 中创建 **服务账号**（Service Account），并为它生成 **JSON 密钥文件**（下载到本地命名为 `service_account.json`）。
   - 回到你的 Google 表格，点击右上角 “Share”，将该服务账号的邮箱地址（形如 `xxx@xxx.iam.gserviceaccount.com`）加入为 **编辑者**。

3) **把本项目放到本地**
   ```bash
   # Python 3.10+
   pip install -r requirements.txt
   ```

4) **设置环境变量**
   - 在你的终端里设置（或写入 .env / shell 配置）：
     ```bash
     export INVENTORY_SHEET_URL="https://docs.google.com/spreadsheets/d/xxxxxxxxxxxxxxxxxxxxxxxx/edit#gid=0"
     ```
   - 或者在系统里永久配置环境变量。

---

## 二、运行

```bash
streamlit run app.py
```
- 浏览器打开 `http://localhost:8501`（Streamlit 默认端口）。
- 左侧 **设置** 中会显示当前 Sheet URL 是否读取成功。

---

## 三、使用说明

- 在 **“➕ 录入记录”** 页签：
  - 选择日期、状态（买入/剩余），输入品名、分类、数量、单位、（买入时）单价、备注，点击保存即可自动写入你的表格的 `购入/剩余`。
  - 如果下拉里没有你要的值，直接输入新内容即可。

- 在 **“📊 库存统计”** 页签：
  - 会自动读取 `购入/剩余` 并计算：
    - 当前库存 = 最新一条“剩余”的数量（如果有）
    - 平均最近两周使用量 = 统计窗口内，对每个区间（买入→剩余、剩余→剩余且减少）按天均分并加总；对“没有买入却剩余增加”的异常自动忽略
    - 预计还能用天数 = 当前库存 ÷（两周用量/14）
    - 计算下次采购量 = 两周目标用量 − 当前库存（小于 0 记 0）
    - 最近统计剩余日期 / 最近采购日期 / 平均采购间隔（天） / 最近采购数量 / 最近采购单价 / 累计支出

---

## 四、FAQ

- **我想用 Excel (.xlsx) 而不是 Google 表格？**  
  可以，但如果多人同时写入会有文件锁与同步问题。你可以将 `gsheet.py` 换成本地 `excel_backend.py`（自行用 `pandas + openpyxl` 读写），不建议多人并发。

- **端口 3003 打不开？**  
  本项目使用 Streamlit，默认端口是 **8501**，请访问 `http://localhost:8501`。

- **字段名对不上？**  
  代码会尝试把纯中文表头映射到混合表头。若你有自定义列名，请在 `gsheet.py` 的 `HEADERS/aliases` 做适配。

---

## 目录结构
```
app.py
compute.py
gsheet.py
requirements.txt
.streamlit/config.toml
service_account.json    # ← 你自己放（不要上传到Git）
```
