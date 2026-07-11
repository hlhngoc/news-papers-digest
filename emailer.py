"""
emailer.py — build the two-section HTML digest and send via Gmail SMTP.

Layout:  overview banner  ->  📄 Papers section  ->  📰 News & Industry section

Env vars needed:
  GMAIL_ADDRESS        -> the sending Gmail (also the default recipient)
  GMAIL_APP_PASSWORD   -> a Google App Password (NOT your normal password)
  DIGEST_TO            -> optional; recipient if different from GMAIL_ADDRESS
"""

from __future__ import annotations

import html
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _esc(s: str) -> str:
    return html.escape(str(s) if s is not None else "")


def _card(it: dict, language: str) -> str:
    why_label = "💡 Vì sao liên quan tới mày" if language == "vi" \
        else "💡 Why this matters for you"
    read_label = "Đọc bài gốc →" if language == "vi" else "Read it →"

    title_vi = _esc(it.get("title_vi", it["title"]))
    title_og = _esc(it.get("title_original", it["title"]))
    show_original = title_og and title_og != title_vi

    badges = []
    if it.get("score") is not None:
        badges.append(
            f'<span style="background:#eef2ff;color:#4338ca;font-size:11px;'
            f'font-weight:600;padding:2px 8px;border-radius:10px;'
            f'margin-left:6px;">{it["score"]}/100</span>')
    if it.get("citations"):
        badges.append(
            f'<span style="background:#ecfdf5;color:#047857;font-size:11px;'
            f'font-weight:600;padding:2px 8px;border-radius:10px;'
            f'margin-left:6px;">📈 {it["citations"]} cites</span>')

    return f"""
    <div style="border:1px solid #e5e7eb;border-radius:12px;padding:18px 20px;
                margin:0 0 14px 0;background:#ffffff;">
      <div style="margin-bottom:6px;">
        <span style="font-size:11px;font-weight:700;letter-spacing:.04em;
                     text-transform:uppercase;color:#6b7280;">
          {_esc(it['source'])}</span>
        {''.join(badges)}
      </div>
      <div style="font-size:17px;font-weight:700;color:#111827;
                  line-height:1.35;">{title_vi}</div>
      {f'<div style="font-size:13px;color:#9ca3af;margin-top:2px;">{title_og}</div>'
         if show_original else ''}
      <p style="font-size:14px;color:#374151;line-height:1.6;margin:10px 0;">
        {_esc(it.get('summary', ''))}</p>
      <div style="background:#f9fafb;border-left:3px solid #6366f1;
                  padding:8px 12px;border-radius:0 6px 6px 0;margin:10px 0;">
        <span style="font-size:13px;color:#4338ca;font-weight:600;">
          {why_label}:</span>
        <span style="font-size:13px;color:#374151;">
          {_esc(it.get('why', ''))}</span>
      </div>
      <a href="{_esc(it.get('url', '#'))}"
         style="display:inline-block;font-size:13px;font-weight:600;
                color:#6366f1;text-decoration:none;">{read_label}</a>
    </div>"""


def _section(header: str, items: list[dict], language: str) -> str:
    if not items:
        return ""
    cards = "".join(_card(it, language) for it in items)
    return f"""
    <div style="font-size:16px;font-weight:800;color:#111827;
                margin:24px 0 12px 0;padding-bottom:6px;
                border-bottom:2px solid #e5e7eb;">{header}</div>
    {cards}"""


def build_html(digest: dict, language: str) -> str:
    """digest = {"overview": str, "items": [ enriched items with pool tags ]}"""
    overview = _esc(digest.get("overview", ""))
    items = digest.get("items", [])
    papers = [i for i in items if i.get("pool") == "papers"]
    news = [i for i in items if i.get("pool") == "news"]

    intro_label = "Tổng quan hôm nay" if language == "vi" else "Today at a glance"
    papers_hdr = "📄 Papers"
    news_hdr = "📰 News & Industry" if language == "en" else "📰 Tin tức & Ngành"

    today = datetime.now().strftime("%A, %d %B %Y")

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f3f4f6;
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:24px 16px;">
    <div style="text-align:center;margin-bottom:20px;">
      <div style="font-size:22px;font-weight:800;color:#111827;">
        📰 Daily Papers Digest</div>
      <div style="font-size:13px;color:#6b7280;margin-top:4px;">{today}</div>
    </div>
    <div style="background:linear-gradient(135deg,#6366f1,#8b5cf6);
                border-radius:12px;padding:18px 20px;margin-bottom:8px;">
      <div style="font-size:13px;font-weight:700;color:#e0e7ff;
                  text-transform:uppercase;letter-spacing:.04em;
                  margin-bottom:6px;">{intro_label}</div>
      <p style="font-size:14px;color:#ffffff;line-height:1.6;margin:0;">
        {overview}</p>
    </div>
    {_section(papers_hdr, papers, language)}
    {_section(news_hdr, news, language)}
    <div style="text-align:center;font-size:12px;color:#9ca3af;
                margin-top:16px;">
      Auto-generated · arXiv · Semantic Scholar · Hacker News · RSS · Gemini
    </div>
  </div>
</body></html>"""


def send(subject: str, html_body: str) -> None:
    """Send the digest via Gmail SMTP (TLS on 587)."""
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ.get("DIGEST_TO", sender)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())
    print(f"[email] sent to {recipient}")
