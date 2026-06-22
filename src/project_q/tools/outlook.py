from __future__ import annotations

import base64
import json
import subprocess
from typing import Any

from project_q.tools.base import ToolDefinition


def _run_powershell_json(script: str, *, timeout_seconds: int = 30) -> Any:
    """Encode a PowerShell script and run it, returning parsed JSON output."""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "PowerShell Outlook command failed")
    stdout = completed.stdout.strip()
    return json.loads(stdout) if stdout else {}


def _require_outlook_enabled(settings_service) -> None:
    if settings_service is None:
        return
    if not settings_service.get_all().get("outlook_enabled", False):
        raise PermissionError("Outlook integration is disabled in settings")


# ---------------------------------------------------------------------------
# 1. OutlookEmailListTool
# ---------------------------------------------------------------------------

class OutlookEmailListTool:
    definition = ToolDefinition(
        tool_id="outlook.email_list",
        name="Outlook Email List",
        description="List recent emails from an Outlook inbox folder",
        tier=1,
    )

    def __init__(self, settings_service=None) -> None:
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require_outlook_enabled(self.settings_service)
        limit = max(1, min(int(payload.get("limit", 20)), 200))
        folder = str(payload.get("folder", "Inbox") or "Inbox")
        folder_literal = json.dumps(folder)

        script = f"""
        try {{
            $outlook = New-Object -ComObject Outlook.Application
            $namespace = $outlook.GetNamespace('MAPI')
            # olFolderInbox = 6
            $inbox = $namespace.GetDefaultFolder(6)
            $folderName = {folder_literal}
            if ($folderName -ne 'Inbox') {{
                $inbox = $inbox.Folders | Where-Object {{ $_.Name -eq $folderName }} | Select-Object -First 1
                if ($null -eq $inbox) {{
                    @{{ error = "folder not found: $folderName" }} | ConvertTo-Json -Compress
                    exit 0
                }}
            }}
            $items = $inbox.Items
            $items.Sort('[ReceivedTime]', $true)
            $limit = {limit}
            $emails = @()
            $count = 0
            foreach ($item in $items) {{
                if ($count -ge $limit) {{ break }}
                $emails += @{{
                    subject    = [string]$item.Subject
                    from       = [string]$item.SenderEmailAddress
                    received   = [string]$item.ReceivedTime
                    read       = [bool]$item.UnRead -eq $false
                    entry_id   = [string]$item.EntryID
                }}
                $count++
            }}
            @{{ emails = $emails; count = $emails.Count }} | ConvertTo-Json -Compress -Depth 3
        }} catch {{
            @{{ error = $_.Exception.Message }} | ConvertTo-Json -Compress
        }}
        """
        result = _run_powershell_json(script, timeout_seconds=30)
        if "error" in result:
            raise RuntimeError(result["error"])
        emails = result.get("emails") or []
        if not isinstance(emails, list):
            emails = [emails]
        return {"emails": emails, "count": len(emails)}


# ---------------------------------------------------------------------------
# 2. OutlookEmailReadTool
# ---------------------------------------------------------------------------

class OutlookEmailReadTool:
    definition = ToolDefinition(
        tool_id="outlook.email_read",
        name="Outlook Email Read",
        description="Read the full body of an Outlook email by EntryID",
        tier=1,
    )

    def __init__(self, settings_service=None) -> None:
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require_outlook_enabled(self.settings_service)
        entry_id = str(payload.get("entry_id", "")).strip()
        if not entry_id:
            raise ValueError("entry_id is required")
        entry_id_literal = json.dumps(entry_id)

        script = f"""
        try {{
            $outlook = New-Object -ComObject Outlook.Application
            $namespace = $outlook.GetNamespace('MAPI')
            $item = $namespace.GetItemFromID({entry_id_literal})
            @{{
                subject  = [string]$item.Subject
                from     = [string]$item.SenderEmailAddress
                body     = [string]$item.Body
                received = [string]$item.ReceivedTime
            }} | ConvertTo-Json -Compress -Depth 2
        }} catch {{
            @{{ error = $_.Exception.Message }} | ConvertTo-Json -Compress
        }}
        """
        result = _run_powershell_json(script, timeout_seconds=30)
        if "error" in result:
            raise RuntimeError(result["error"])
        return {
            "subject": result.get("subject", ""),
            "from": result.get("from", ""),
            "body": result.get("body", ""),
            "received": result.get("received", ""),
        }


# ---------------------------------------------------------------------------
# 3. OutlookEmailSendTool
# ---------------------------------------------------------------------------

class OutlookEmailSendTool:
    definition = ToolDefinition(
        tool_id="outlook.email_send",
        name="Outlook Email Send",
        description="Send an email via Outlook",
        tier=3,
    )

    def __init__(self, settings_service=None) -> None:
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require_outlook_enabled(self.settings_service)
        to_field = payload.get("to")
        if not to_field:
            raise ValueError("to is required")
        if isinstance(to_field, str):
            to_list = [to_field]
        else:
            to_list = list(to_field)

        subject = str(payload.get("subject", "")).strip()
        body = str(payload.get("body", "") or "")
        cc_field = payload.get("cc") or []
        if isinstance(cc_field, str):
            cc_list = [cc_field] if cc_field.strip() else []
        else:
            cc_list = list(cc_field)

        to_joined = "; ".join(to_list)
        cc_joined = "; ".join(cc_list)
        to_literal = json.dumps(to_joined)
        cc_literal = json.dumps(cc_joined)
        subject_literal = json.dumps(subject)
        body_literal = json.dumps(body)

        script = f"""
        try {{
            $outlook = New-Object -ComObject Outlook.Application
            $mail = $outlook.CreateItem(0)
            $mail.To = {to_literal}
            $mail.CC = {cc_literal}
            $mail.Subject = {subject_literal}
            $mail.Body = {body_literal}
            $mail.Send()
            @{{ sent = $true; to = {to_literal}; subject = {subject_literal} }} | ConvertTo-Json -Compress
        }} catch {{
            @{{ error = $_.Exception.Message }} | ConvertTo-Json -Compress
        }}
        """
        result = _run_powershell_json(script, timeout_seconds=30)
        if "error" in result:
            raise RuntimeError(result["error"])
        return {
            "sent": True,
            "to": to_list,
            "subject": subject,
        }


# ---------------------------------------------------------------------------
# 4. OutlookCalendarListTool
# ---------------------------------------------------------------------------

class OutlookCalendarListTool:
    definition = ToolDefinition(
        tool_id="outlook.calendar_list",
        name="Outlook Calendar List",
        description="List upcoming Outlook calendar events within a date window",
        tier=1,
    )

    def __init__(self, settings_service=None) -> None:
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require_outlook_enabled(self.settings_service)
        days_ahead = max(1, min(int(payload.get("days_ahead", 7)), 365))
        limit = max(1, min(int(payload.get("limit", 20)), 500))

        script = f"""
        try {{
            $outlook = New-Object -ComObject Outlook.Application
            $namespace = $outlook.GetNamespace('MAPI')
            # olFolderCalendar = 9
            $calendar = $namespace.GetDefaultFolder(9)
            $items = $calendar.Items
            $items.IncludeRecurrences = $true
            $items.Sort('[Start]')
            $now = [DateTime]::Now
            $end = $now.AddDays({days_ahead})
            $filter = "[Start] >= '$($now.ToString('MM/dd/yyyy HH:mm'))' AND [Start] <= '$($end.ToString('MM/dd/yyyy HH:mm'))'"
            $filtered = $items.Restrict($filter)
            $limit = {limit}
            $events = @()
            $count = 0
            foreach ($item in $filtered) {{
                if ($count -ge $limit) {{ break }}
                $events += @{{
                    subject   = [string]$item.Subject
                    start     = [string]$item.Start
                    end       = [string]$item.End
                    location  = [string]$item.Location
                    organizer = [string]$item.Organizer
                }}
                $count++
            }}
            @{{ events = $events; count = $events.Count }} | ConvertTo-Json -Compress -Depth 3
        }} catch {{
            @{{ error = $_.Exception.Message }} | ConvertTo-Json -Compress
        }}
        """
        result = _run_powershell_json(script, timeout_seconds=30)
        if "error" in result:
            raise RuntimeError(result["error"])
        events = result.get("events") or []
        if not isinstance(events, list):
            events = [events]
        return {"events": events, "count": len(events)}


# ---------------------------------------------------------------------------
# 5. OutlookCalendarCreateTool
# ---------------------------------------------------------------------------

class OutlookCalendarCreateTool:
    definition = ToolDefinition(
        tool_id="outlook.calendar_create",
        name="Outlook Calendar Create",
        description="Create a new calendar appointment in Outlook",
        tier=2,
    )

    def __init__(self, settings_service=None) -> None:
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require_outlook_enabled(self.settings_service)
        title = str(payload.get("title", "")).strip()
        if not title:
            raise ValueError("title is required")
        start = str(payload.get("start", "")).strip()
        if not start:
            raise ValueError("start is required")
        end = str(payload.get("end", "")).strip()
        if not end:
            raise ValueError("end is required")
        body = str(payload.get("body", "") or "")
        location = str(payload.get("location", "") or "")

        title_literal = json.dumps(title)
        start_literal = json.dumps(start)
        end_literal = json.dumps(end)
        body_literal = json.dumps(body)
        location_literal = json.dumps(location)

        script = f"""
        try {{
            $outlook = New-Object -ComObject Outlook.Application
            # olAppointmentItem = 1
            $appt = $outlook.CreateItem(1)
            $appt.Subject = {title_literal}
            $appt.Start = [DateTime]::Parse({start_literal})
            $appt.End = [DateTime]::Parse({end_literal})
            $appt.Body = {body_literal}
            $appt.Location = {location_literal}
            $appt.Save()
            @{{
                created  = $true
                title    = {title_literal}
                start    = [string]$appt.Start
                end      = [string]$appt.End
            }} | ConvertTo-Json -Compress
        }} catch {{
            @{{ error = $_.Exception.Message }} | ConvertTo-Json -Compress
        }}
        """
        result = _run_powershell_json(script, timeout_seconds=30)
        if "error" in result:
            raise RuntimeError(result["error"])
        return {
            "created": True,
            "title": title,
            "start": start,
            "end": end,
        }
