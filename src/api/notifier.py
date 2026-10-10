"""Notify by email after summaries have been published to WPS.

This is deliberately **not** the old digest emailer: the full summaries now live
in WPS, so this only reports *what was generated* together with the document
links. That keeps the notification short and the mailbox free of large HTML.

It reuses the same QQ SMTP settings the old ``Emailer`` used
(``config.SMTP_*``). If SMTP is unconfigured, or ``NOTIFY_EMAIL`` is disabled,
the notifier is inert -- the pipeline never depends on it.
"""
from __future__ import annotations

import os
import smtplib
import time
from collections import OrderedDict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape

from src.runtime import config

_CSS = """\
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 15px;
    line-height: 1.7;
    color: #1a1a1a;
    max-width: 720px;
    margin: 0 auto;
    padding: 20px;
}
h2 {
    color: #2c3e50;
    border-bottom: 2px solid #3498db;
    padding-bottom: 8px;
    margin-top: 26px;
    font-size: 18px;
}
ul { padding-left: 22px; }
li { margin-bottom: 6px; }
a { color: #3498db; text-decoration: none; }
a:hover { text-decoration: underline; }
.meta { color: #7f8c8d; font-size: 13px; }
.tip {
    background: #f8f9fa;
    border-left: 4px solid #3498db;
    padding: 10px 14px;
    margin-top: 22px;
    color: #555;
    font-size: 13px;
}
"""


class Notifier:
    """Sends a short "documents generated" email."""

    def __init__(self, enabled: bool | None = None):
        self.host = config.SMTP_HOST
        self.port = config.SMTP_PORT
        self.sender = config.SMTP_EMAIL
        self.password = config.SMTP_PASSWORD
        self.receiver = config.RECEIVER_EMAIL

        if enabled is None:
            # Off unless explicitly disabled, and only when SMTP is usable.
            flag = os.environ.get("NOTIFY_EMAIL", "1").strip().lower()
            wanted = flag not in ("0", "false", "no", "off")
            enabled = wanted and bool(self.sender and self.password and self.receiver)
        self.enabled = enabled

    # ------------------------------------------------------------------ public
    def notify_published(self, rows: list[dict]) -> bool:
        """Email a list of freshly created documents.

        Args:
            rows: each ``{course_title, sub_title, date, link}``.

        Returns:
            True when an email was sent, False when skipped or failed.
        """
        if not self.enabled or not rows:
            return False

        courses: "OrderedDict[str, list[dict]]" = OrderedDict()
        for r in rows:
            courses.setdefault(r.get("course_title") or "(未命名课程)", []).append(r)

        n = len(rows)
        subject = "[FiCS] 已生成 %d 份智能文档" % n

        # ---- plain text ----
        plain_lines = ["iCourse 摘要已生成 WPS 智能文档，共 %d 份。" % n, ""]
        for course, items in courses.items():
            plain_lines.append("【%s】" % course)
            for it in items:
                date = (it.get("date") or "").strip()
                suffix = " (%s)" % date if date else ""
                plain_lines.append("  - %s%s" % (it.get("sub_title") or "", suffix))
                if it.get("link"):
                    plain_lines.append("    %s" % it["link"])
            plain_lines.append("")
        plain_lines.append("（正文已归档在 WPS 智能文档中，本邮件仅作生成通知。）")
        plain = "\n".join(plain_lines)

        # ---- html ----
        parts = [
            "<p>iCourse 摘要已生成 <strong>%d</strong> 份 WPS 智能文档：</p>" % n
        ]
        for course, items in courses.items():
            parts.append("<h2>%s</h2><ul>" % escape(course))
            for it in items:
                sub = escape(it.get("sub_title") or "")
                date = escape((it.get("date") or "").strip())
                meta = ' <span class="meta">(%s)</span>' % date if date else ""
                link = it.get("link")
                if link:
                    parts.append(
                        '<li>%s%s<br><a href="%s">%s</a></li>'
                        % (sub, meta, escape(link, quote=True), escape(link))
                    )
                else:
                    parts.append("<li>%s%s</li>" % (sub, meta))
            parts.append("</ul>")
        parts.append(
            '<div class="tip">正文已归档在 WPS 智能文档中，本邮件仅作生成通知。</div>'
        )
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<style>%s</style></head><body>%s</body></html>"
            % (_CSS, "\n".join(parts))
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = formataddr(("iCourse Subscriber", self.sender))
        msg["To"] = self.receiver
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        for attempt in range(3):
            try:
                with smtplib.SMTP_SSL(self.host, self.port) as server:
                    server.login(self.sender, self.password)
                    server.sendmail(self.sender, self.receiver, msg.as_string())
                print("[Notifier] Sent: %s" % subject, flush=True)
                return True
            except Exception as e:
                print("[Notifier] Attempt %d/3 failed: %s" % (attempt + 1, e), flush=True)
                if attempt < 2:
                    time.sleep(2 ** attempt)

        print("[Notifier] All send attempts failed.", flush=True)
        return False
