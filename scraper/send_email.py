"""
新 IOSCO 報告 Email 通知
- 讀 scraper/new_iosco.json（scraper.py 本次執行產生），有新報告才寄信
- 需在 GitHub Secrets 設定 MAIL_USERNAME、MAIL_PASSWORD、MAIL_TO
  （MAIL_TO 可用逗號分隔多個收件人）；未設定就略過，不影響抓取
- 預設用 Gmail SMTP（smtp.gmail.com:465），可用 MAIL_SERVER / MAIL_PORT 改
"""

import os
import json
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from pathlib import Path

NOTIFY_PATH = Path(__file__).parent / "new_iosco.json"
SITE_URL    = os.environ.get("SITE_URL", "https://shelly-awkward.github.io/regulatory-reports/")


def build_body(reports: list[dict]) -> str:
    lines = [f"本週抓到 {len(reports)} 份新的 IOSCO 報告：", ""]
    for i, r in enumerate(reports, 1):
        lines.append(f"{i}. {r.get('title_zh') or r.get('title_en', '')}")
        lines.append(f"   英文標題：{r.get('title_en', '')}")
        lines.append(f"   發布日期：{r.get('date') or '未知'}")
        points = r.get("key_points_zh") or []
        if points:
            lines.append("   重點：")
            for p in points:
                lines.append(f"   ・{p}")
        else:
            lines.append("   重點：（尚未產生，請直接看原文）")
        lines.append(f"   原文：{r.get('url', '')}")
        lines.append("")
    lines.append(f"完整清單與一鍵複製呈報文字：{SITE_URL}")
    lines.append("")
    lines.append("※ 重點由 AI 依報告 PDF 前幾頁整理，呈報前請對照原文確認。")
    return "\n".join(lines)


def main():
    user = os.environ.get("MAIL_USERNAME", "")
    pwd  = os.environ.get("MAIL_PASSWORD", "")
    to   = [a.strip() for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()]
    if not (user and pwd and to):
        print("未設定 MAIL_USERNAME / MAIL_PASSWORD / MAIL_TO，略過 Email 通知")
        return

    if not NOTIFY_PATH.exists():
        print("沒有通知檔，略過")
        return
    reports = json.loads(NOTIFY_PATH.read_text(encoding="utf-8"))
    if not reports:
        print("本次沒有新 IOSCO 報告，不寄信")
        return

    msg = MIMEText(build_body(reports), "plain", "utf-8")
    msg["Subject"] = Header(f"【IOSCO 新報告】{len(reports)} 份待呈報", "utf-8")
    msg["From"]    = user
    msg["To"]      = ", ".join(to)

    server = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    port   = int(os.environ.get("MAIL_PORT", "465"))
    with smtplib.SMTP_SSL(server, port, timeout=30) as s:
        s.login(user, pwd)
        s.sendmail(user, to, msg.as_string())
    print(f"📧 已寄出通知給 {len(to)} 位收件人（{len(reports)} 份報告）")


if __name__ == "__main__":
    main()
