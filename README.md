# GA4 自動稽核 Agent

自動化稽核 GA4 設定，使用 Claude AI 分析並產生專業 PDF 報告。

## 稽核項目

- 資料串流設定（Stream 數量、Measurement ID）
- 轉換事件（是否設定 purchase、lead 等關鍵事件）
- 流量資料品質（過去 30 天是否有斷點）
- 事件命名規範（snake_case 檢查）
- 流量渠道分佈（Self-referral 偵測）

---

## 安裝步驟

### 1. 安裝套件

```bash
pip install -r requirements.txt
```

### 2. 設定 Google Service Account

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立或選擇專案
3. 啟用以下 API：
   - **Google Analytics Admin API**
   - **Google Analytics Data API**
4. 建立 Service Account：
   - IAM & Admin > Service Accounts > 建立
   - 下載 JSON 金鑰，命名為 `service_account.json`，放在專案根目錄
5. 在 GA4 後台將此 Service Account Email 加為「檢視者」

### 3. 設定環境變數

建立 `.env` 檔案：

```env
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxx
```

或直接 export：

```bash
export ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxx
```

### 4. 設定 Property ID

在 `ga4_audit_agent.py` 第 30 行修改：

```python
PROPERTY_ID = "properties/123456789"  # 你的 GA4 Property ID
```

Property ID 可在 GA4 後台 > 管理 > 資源設定 中找到。

---

## 執行方式

### 基本執行

```bash
python ga4_audit_agent.py
```

### 程式中呼叫

```python
from ga4_audit_agent import run_audit

# 使用預設設定
report_path = run_audit()

# 指定 Property 和輸出路徑
report_path = run_audit(
    property_id="properties/123456789",
    output_path="client_A_audit.pdf"
)
```

### 排程自動執行（每週一早上 9 點）

使用 cron：
```bash
0 9 * * 1 cd /path/to/project && python ga4_audit_agent.py
```

使用 GitHub Actions（`.github/workflows/weekly_audit.yml`）：
```yaml
on:
  schedule:
    - cron: '0 1 * * 1'  # UTC 每週一 01:00
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - run: pip install -r requirements.txt
      - run: python ga4_audit_agent.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

---

## 報告範例

產生的 PDF 包含：
1. **AI 稽核摘要**：整體健康評分 + 關鍵問題 + 修復建議
2. **資料串流設定**：串流清單與異常偵測
3. **轉換事件設定**：已設定的轉換與缺漏項目
4. **流量資料品質**：30 天 Sessions 趨勢與斷點偵測
5. **事件品質**：Top 20 事件清單與命名規範檢查
6. **流量渠道分佈**：各渠道佔比與 Self-referral 警告

---

## 接案應用建議

### 定價參考
- 單次稽核報告：$100–$200 USD
- 月訂閱（每月自動稽核）：$150–$300 USD/月
- 搭配修復服務：稽核 + 修復 $300–$600 USD

### 交付流程
1. 客戶提供 GA4 存取權（加 Service Account 為檢視者）
2. 執行 Agent，5 分鐘產生報告
3. 寄送 PDF + 修復建議
4. 選配：提供修復執行服務

---

## 常見問題

**Q: 需要什麼 GA4 權限？**
A: 「檢視者」權限即可，不需要編輯權限，安全無風險。

**Q: 一次可以稽核多個 Property 嗎？**
A: 可以，在 `run_audit()` 中傳入不同的 `property_id` 並迴圈執行。

**Q: Claude API 費用大概多少？**
A: 每次稽核約消耗 1,500 tokens，費用約 $0.002 USD，幾乎可忽略。
