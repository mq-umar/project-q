from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from project_q.tools.base import ToolDefinition


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _safe_filename_fragment(value: str, fallback: str) -> str:
    fragment = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())[:60].strip("-")
    return fragment or fallback


def _reject_header_injection(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    if "\r" in text or "\n" in text:
        raise ValueError(f"{field_name} must not contain newline characters")
    return text


def _draft_root(data_root: Path, kind: str) -> Path:
    root = (data_root / "drafts" / kind).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


class EmailDraftTool:
    definition = ToolDefinition(
        tool_id="communications.email_draft",
        name="Create Email Draft",
        description="Create a local RFC 822 email draft file for owner review",
        tier=1,
    )

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        recipients = payload.get("to") or payload.get("recipients")
        if isinstance(recipients, str):
            recipients = [recipients]
        recipients = [_reject_header_injection(item, "recipient") for item in recipients or []]
        if not recipients:
            raise ValueError("at least one recipient is required")

        subject = _reject_header_injection(payload.get("subject", ""), "subject")
        body = str(payload.get("body", ""))
        sender = str(payload.get("from", "")).strip()
        if sender:
            sender = _reject_header_injection(sender, "from")

        message = EmailMessage()
        if sender:
            message["From"] = sender
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message["X-Project-Q-Draft"] = "true"
        message.set_content(body)

        root = _draft_root(self.data_root, "email")
        filename = f"{_utc_stamp()}-{uuid.uuid4().hex[:8]}-{_safe_filename_fragment(subject, 'email')}.eml"
        target = (root / filename).resolve()
        if not target.is_relative_to(root):
            raise ValueError("email draft path escaped draft directory")
        target.write_text(message.as_string(), encoding="utf-8")
        return {
            "path": str(target),
            "to": recipients,
            "subject": subject,
            "bytes_written": target.stat().st_size,
        }


class CalendarInviteTool:
    definition = ToolDefinition(
        tool_id="calendar.create_invite",
        name="Create Calendar Invite",
        description="Create a local ICS calendar invite file for owner review",
        tier=1,
    )

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = _reject_header_injection(payload.get("title", ""), "title")
        start = _format_ics_datetime(payload.get("start", ""))
        end = _format_ics_datetime(payload.get("end", ""))
        description = _ics_escape(str(payload.get("description", "")))
        location = _ics_escape(str(payload.get("location", "")))
        uid = f"{uuid.uuid4().hex}@project-q.local"
        now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Project Q//Local Calendar Draft//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART:{start}",
            f"DTEND:{end}",
            f"SUMMARY:{_ics_escape(title)}",
        ]
        if description:
            lines.append(f"DESCRIPTION:{description}")
        if location:
            lines.append(f"LOCATION:{location}")
        lines.extend(["END:VEVENT", "END:VCALENDAR", ""])

        root = _draft_root(self.data_root, "calendar")
        filename = f"{_utc_stamp()}-{uuid.uuid4().hex[:8]}-{_safe_filename_fragment(title, 'event')}.ics"
        target = (root / filename).resolve()
        if not target.is_relative_to(root):
            raise ValueError("calendar invite path escaped draft directory")
        target.write_text("\r\n".join(lines), encoding="utf-8")
        return {
            "path": str(target),
            "title": title,
            "start": start,
            "end": end,
            "uid": uid,
        }


def _format_ics_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("start and end are required")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )
