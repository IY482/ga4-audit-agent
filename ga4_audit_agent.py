"""
GA4 自動稽核 Agent
使用 GA4 Admin API + Data API + Claude API 產生 PDF 稽核報告
"""

import os
import json
from datetime import datetime, timedelta
from typing import Any

import anthropic
from google.analytics.admin import AnalyticsAdminServiceClient
from google.analytics.admin.types import (
    ListPropertiesRequest,
    ListDataStreamsRequest,
    ListConversionEventsRequest,
    ListCustomEventsRequest,
)
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest,
    Dimension,
    Metric,
    DateRange,
    FilterExpression,
)
from google.oauth2 import service_account

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER


# ─────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────

SERVICE_ACCOUNT_FILE = "service_account.json"   # Google Service Account JSON
PROPERTY_ID = "properties/XXXXXXXXX"            # 替換成你的 GA4 Property ID
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ─────────────────────────────────────────────
# 1. GA4 資料抓取
# ─────────────────────────────────────────────

def get_admin_client(sa_file: str) -> AnalyticsAdminServiceClient:
    credentials = service_account.Credentials.from_service_account_file(
        sa_file,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    return AnalyticsAdminServiceClient(credentials=credentials)


def get_data_client(sa_file: str) -> BetaAnalyticsDataClient:
    credentials = service_account.Credentials.from_service_account_file(
        sa_file,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    return BetaAnalyticsDataClient(credentials=credentials)


def audit_data_streams(admin_client, property_id: str) -> dict:
    """稽核資料串流設定"""
    results = {"streams": [], "issues": []}
    try:
        streams = admin_client.list_data_streams(parent=property_id)
        for stream in streams:
            stream_info = {
                "name": stream.display_name,
                "type": str(stream.type_).split(".")[-1],
                "create_time": str(stream.create_time),
            }
            # 檢查是否有網站串流
            if hasattr(stream, "web_stream_data"):
                stream_info["measurement_id"] = stream.web_stream_data.measurement_id
                stream_info["default_uri"] = stream.web_stream_data.default_uri
            results["streams"].append(stream_info)

        if not results["streams"]:
            results["issues"].append("⚠️ 未找到任何資料串流，GA4 可能未正確設定")
        if len(results["streams"]) > 5:
            results["issues"].append("⚠️ 資料串流數量過多，建議確認是否有重複串流")

    except Exception as e:
        results["issues"].append(f"❌ 無法讀取資料串流：{e}")
    return results


def audit_conversion_events(admin_client, property_id: str) -> dict:
    """稽核轉換事件設定"""
    results = {"conversions": [], "issues": []}
    critical_conversions = {"purchase", "lead", "sign_up", "contact", "submit_form"}

    try:
        events = admin_client.list_conversion_events(parent=property_id)
        for event in events:
            results["conversions"].append({
                "name": event.event_name,
                "create_time": str(event.create_time),
                "deletable": event.deletable,
            })

        found_names = {c["name"] for c in results["conversions"]}
        if not found_names:
            results["issues"].append("❌ 未設定任何轉換事件，無法追蹤業務目標")
        else:
            missing = critical_conversions - found_names
            if missing:
                results["issues"].append(
                    f"⚠️ 建議追蹤的轉換事件尚未設定：{', '.join(missing)}"
                )

    except Exception as e:
        results["issues"].append(f"❌ 無法讀取轉換事件：{e}")
    return results


def audit_traffic_data(data_client, property_id: str) -> dict:
    """稽核過去 30 天流量資料品質"""
    results = {"daily_sessions": [], "issues": [], "summary": {}}
    end_date = datetime.today()
    start_date = end_date - timedelta(days=30)

    try:
        request = RunReportRequest(
            property=property_id,
            dimensions=[Dimension(name="date")],
            metrics=[
                Metric(name="sessions"),
                Metric(name="totalUsers"),
            ],
            date_ranges=[DateRange(
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
            )],
            order_bys=[{"dimension": {"dimension_name": "date"}}],
        )
        response = data_client.run_report(request)

        zero_days = []
        for row in response.rows:
            date = row.dimension_values[0].value
            sessions = int(row.metric_values[0].value)
            users = int(row.metric_values[1].value)
            results["daily_sessions"].append({
                "date": date, "sessions": sessions, "users": users
            })
            if sessions == 0:
                zero_days.append(date)

        if zero_days:
            results["issues"].append(
                f"❌ 以下日期 sessions 為 0，可能有追蹤中斷：{', '.join(zero_days[:5])}"
                + ("..." if len(zero_days) > 5 else "")
            )

        if results["daily_sessions"]:
            total_sessions = sum(d["sessions"] for d in results["daily_sessions"])
            avg_sessions = total_sessions / len(results["daily_sessions"])
            results["summary"] = {
                "total_sessions_30d": total_sessions,
                "avg_daily_sessions": round(avg_sessions, 1),
                "data_days": len(results["daily_sessions"]),
                "zero_session_days": len(zero_days),
            }

    except Exception as e:
        results["issues"].append(f"❌ 無法讀取流量資料：{e}")
    return results


def audit_top_events(data_client, property_id: str) -> dict:
    """稽核事件命名與重要事件"""
    results = {"events": [], "issues": []}
    end_date = datetime.today()
    start_date = end_date - timedelta(days=30)

    naming_issues = []

    try:
        request = RunReportRequest(
            property=property_id,
            dimensions=[Dimension(name="eventName")],
            metrics=[Metric(name="eventCount")],
            date_ranges=[DateRange(
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
            )],
            order_bys=[{"metric": {"metric_name": "eventCount"}, "desc": True}],
            limit=30,
        )
        response = data_client.run_report(request)

        for row in response.rows:
            name = row.dimension_values[0].value
            count = int(row.metric_values[0].value)
            results["events"].append({"name": name, "count": count})

            # 命名規範檢查：應為 snake_case，不含大寫或空格
            if any(c.isupper() for c in name) or " " in name:
                naming_issues.append(name)

        if naming_issues:
            results["issues"].append(
                f"⚠️ 以下事件命名不符合 snake_case 規範：{', '.join(naming_issues[:5])}"
            )

        # 檢查必要事件是否存在
        found = {e["name"] for e in results["events"]}
        must_have = {"page_view", "session_start", "first_visit"}
        missing = must_have - found
        if missing:
            results["issues"].append(f"⚠️ 基礎事件缺失：{', '.join(missing)}")

    except Exception as e:
        results["issues"].append(f"❌ 無法讀取事件資料：{e}")
    return results


def audit_channel_grouping(data_client, property_id: str) -> dict:
    """稽核流量來源與 Self-referral"""
    results = {"channels": [], "issues": []}
    end_date = datetime.today()
    start_date = end_date - timedelta(days=30)

    try:
        request = RunReportRequest(
            property=property_id,
            dimensions=[Dimension(name="sessionDefaultChannelGrouping")],
            metrics=[Metric(name="sessions")],
            date_ranges=[DateRange(
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
            )],
            order_bys=[{"metric": {"metric_name": "sessions"}, "desc": True}],
        )
        response = data_client.run_report(request)
        total_sessions = 0
        referral_sessions = 0

        for row in response.rows:
            channel = row.dimension_values[0].value
            sessions = int(row.metric_values[0].value)
            results["channels"].append({"channel": channel, "sessions": sessions})
            total_sessions += sessions
            if channel.lower() in ["referral", "(other)"]:
                referral_sessions += sessions

        if total_sessions > 0:
            referral_pct = referral_sessions / total_sessions * 100
            if referral_pct > 20:
                results["issues"].append(
                    f"⚠️ Referral / (Other) 流量佔 {referral_pct:.1f}%，"
                    "可能有 Self-referral 或內部流量未排除"
                )

    except Exception as e:
        results["issues"].append(f"❌ 無法讀取渠道資料：{e}")
    return results


# ─────────────────────────────────────────────
# 2. Claude AI 分析
# ─────────────────────────────────────────────

def generate_ai_analysis(audit_data: dict) -> str:
    """使用 Claude 產生稽核摘要與建議"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    all_issues = []
    for section in ["streams", "conversions", "traffic", "events", "channels"]:
        section_data = audit_data.get(section, {})
        all_issues.extend(section_data.get("issues", []))

    prompt = f"""
你是一位資深的 GA4 數位分析顧問，請根據以下稽核資料，用繁體中文撰寫一份專業的稽核摘要報告。

【稽核資料】
{json.dumps(audit_data, ensure_ascii=False, indent=2)}

請提供：
1. 整體健康評分（0-100分）並說明原因
2. 最關鍵的 3 個問題（如果有）
3. 優先修復建議（按重要性排序）
4. 正面評價（哪些設定做得好）
5. 一段給客戶的總結語（專業且友善）

格式請使用清楚的段落，不要使用 Markdown 符號（因為輸出到 PDF）。
"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


# ─────────────────────────────────────────────
# 3. PDF 報告產生
# ─────────────────────────────────────────────

def build_pdf_report(audit_data: dict, ai_analysis: str, output_path: str):
    """產生專業 PDF 稽核報告"""
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm
    )

    styles = getSampleStyleSheet()
    # 自訂樣式
    title_style = ParagraphStyle(
        "CustomTitle", parent=styles["Title"],
        fontSize=22, textColor=colors.HexColor("#1a1a2e"),
        spaceAfter=6, alignment=TA_CENTER
    )
    subtitle_style = ParagraphStyle(
        "Subtitle", parent=styles["Normal"],
        fontSize=11, textColor=colors.HexColor("#555555"),
        alignment=TA_CENTER, spaceAfter=20
    )
    h1_style = ParagraphStyle(
        "H1", parent=styles["Heading1"],
        fontSize=14, textColor=colors.HexColor("#16213e"),
        spaceBefore=16, spaceAfter=8,
        borderPad=4,
    )
    h2_style = ParagraphStyle(
        "H2", parent=styles["Heading2"],
        fontSize=12, textColor=colors.HexColor("#0f3460"),
        spaceBefore=10, spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "Body", parent=styles["Normal"],
        fontSize=10, leading=15,
        textColor=colors.HexColor("#333333"),
    )
    issue_style = ParagraphStyle(
        "Issue", parent=styles["Normal"],
        fontSize=10, leading=14,
        textColor=colors.HexColor("#cc0000"),
        leftIndent=12,
    )
    ok_style = ParagraphStyle(
        "OK", parent=styles["Normal"],
        fontSize=10, leading=14,
        textColor=colors.HexColor("#007700"),
        leftIndent=12,
    )

    story = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── 封面 ──
    story.append(Spacer(1, 1.5*cm))
    story.append(Paragraph("GA4 自動稽核報告", title_style))
    story.append(Paragraph(f"產生時間：{now}", subtitle_style))
    story.append(Paragraph(f"Property：{PROPERTY_ID}", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=2,
                             color=colors.HexColor("#0f3460"), spaceAfter=20))

    # ── AI 摘要 ──
    story.append(Paragraph("AI 稽核摘要", h1_style))
    for line in ai_analysis.split("\n"):
        if line.strip():
            story.append(Paragraph(line.strip(), body_style))
            story.append(Spacer(1, 4))
    story.append(PageBreak())

    # ── 資料串流 ──
    story.append(Paragraph("1. 資料串流設定", h1_style))
    streams_data = audit_data.get("streams", {})
    streams = streams_data.get("streams", [])
    if streams:
        table_data = [["串流名稱", "類型", "Measurement ID"]]
        for s in streams:
            table_data.append([
                s.get("name", "-"),
                s.get("type", "-"),
                s.get("measurement_id", "-"),
            ])
        t = Table(table_data, colWidths=[6*cm, 4*cm, 6*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f3460")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.HexColor("#f0f4ff"), colors.white]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("PADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(t)
        story.append(Spacer(1, 10))
    _append_issues(story, streams_data.get("issues", []), issue_style, ok_style)

    # ── 轉換事件 ──
    story.append(Paragraph("2. 轉換事件設定", h1_style))
    conv_data = audit_data.get("conversions", {})
    conversions = conv_data.get("conversions", [])
    if conversions:
        table_data = [["事件名稱", "可刪除"]]
        for c in conversions:
            table_data.append([c.get("name", "-"), "是" if c.get("deletable") else "否"])
        t = Table(table_data, colWidths=[10*cm, 6*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f3460")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.HexColor("#f0f4ff"), colors.white]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("PADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(t)
        story.append(Spacer(1, 10))
    _append_issues(story, conv_data.get("issues", []), issue_style, ok_style)

    # ── 流量品質 ──
    story.append(Paragraph("3. 流量資料品質（過去 30 天）", h1_style))
    traffic_data = audit_data.get("traffic", {})
    summary = traffic_data.get("summary", {})
    if summary:
        metrics = [
            ["總 Sessions", str(summary.get("total_sessions_30d", 0))],
            ["平均每日 Sessions", str(summary.get("avg_daily_sessions", 0))],
            ["有資料的天數", str(summary.get("data_days", 0))],
            ["Sessions 為 0 的天數", str(summary.get("zero_session_days", 0))],
        ]
        t = Table(metrics, colWidths=[8*cm, 8*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e8eaf6")),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("PADDING", (0, 0), (-1, -1), 7),
        ]))
        story.append(t)
        story.append(Spacer(1, 10))
    _append_issues(story, traffic_data.get("issues", []), issue_style, ok_style)

    # ── 事件品質 ──
    story.append(Paragraph("4. 事件品質（前 20 筆）", h1_style))
    events_data = audit_data.get("events", {})
    events = events_data.get("events", [])[:20]
    if events:
        table_data = [["事件名稱", "次數（30天）"]]
        for e in events:
            table_data.append([e["name"], f"{e['count']:,}"])
        t = Table(table_data, colWidths=[10*cm, 6*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f3460")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.HexColor("#f0f4ff"), colors.white]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("PADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(t)
        story.append(Spacer(1, 10))
    _append_issues(story, events_data.get("issues", []), issue_style, ok_style)

    # ── 流量渠道 ──
    story.append(Paragraph("5. 流量渠道分佈", h1_style))
    channels_data = audit_data.get("channels", {})
    channels = channels_data.get("channels", [])
    if channels:
        total = sum(c["sessions"] for c in channels)
        table_data = [["渠道", "Sessions", "佔比"]]
        for c in channels:
            pct = c["sessions"] / total * 100 if total else 0
            table_data.append([c["channel"], f"{c['sessions']:,}", f"{pct:.1f}%"])
        t = Table(table_data, colWidths=[7*cm, 5*cm, 4*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f3460")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.HexColor("#f0f4ff"), colors.white]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("PADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(t)
        story.append(Spacer(1, 10))
    _append_issues(story, channels_data.get("issues", []), issue_style, ok_style)

    # ── 頁尾 ──
    story.append(Spacer(1, 1*cm))
    story.append(HRFlowable(width="100%", thickness=1,
                             color=colors.HexColor("#cccccc")))
    story.append(Paragraph(
        "本報告由 GA4 自動稽核 Agent 產生｜僅供參考，建議搭配人工複核",
        ParagraphStyle("Footer", parent=styles["Normal"],
                       fontSize=8, textColor=colors.grey, alignment=TA_CENTER)
    ))

    doc.build(story)
    print(f"✅ PDF 報告已產生：{output_path}")


def _append_issues(story, issues: list, issue_style, ok_style):
    if issues:
        for issue in issues:
            story.append(Paragraph(issue, issue_style))
    else:
        story.append(Paragraph("✅ 此項目設定正常，未發現問題", ok_style))
    story.append(Spacer(1, 8))


# ─────────────────────────────────────────────
# 4. 主流程
# ─────────────────────────────────────────────

def run_audit(property_id: str = None, output_path: str = None) -> str:
    """執行完整 GA4 稽核並產生 PDF 報告"""
    prop_id = property_id or PROPERTY_ID
    out_path = output_path or f"ga4_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

    print("🔍 正在連接 GA4 API...")
    admin_client = get_admin_client(SERVICE_ACCOUNT_FILE)
    data_client = get_data_client(SERVICE_ACCOUNT_FILE)

    print("📊 正在執行稽核檢查...")
    audit_data = {
        "property_id": prop_id,
        "audit_time": datetime.now().isoformat(),
        "streams": audit_data_streams(admin_client, prop_id),
        "conversions": audit_conversion_events(admin_client, prop_id),
        "traffic": audit_traffic_data(data_client, prop_id),
        "events": audit_top_events(data_client, prop_id),
        "channels": audit_channel_grouping(data_client, prop_id),
    }

    print("🤖 Claude AI 正在分析結果...")
    ai_analysis = generate_ai_analysis(audit_data)

    print("📄 正在產生 PDF 報告...")
    build_pdf_report(audit_data, ai_analysis, out_path)

    return out_path


if __name__ == "__main__":
    report_path = run_audit()
    print(f"\n🎉 稽核完成！報告儲存於：{report_path}")
