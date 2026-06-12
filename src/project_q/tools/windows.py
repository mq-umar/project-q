from __future__ import annotations

import base64
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from project_q.tools.base import ToolDefinition


REGISTRY_READ_ROOTS = (
    "HKCU:\\Software",
    "HKCU:\\Environment",
    "HKLM:\\Software",
)
REGISTRY_WRITE_ROOTS = ("HKCU:\\Software\\ProjectQ",)
REGISTRY_VALUE_KINDS = {"String", "ExpandString", "DWord", "QWord"}


def _run_powershell_json(script: str, *, timeout_seconds: int = 10, sta: bool = False) -> Any:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    command = ["powershell", "-NoProfile"]
    if sta:
        command.append("-STA")
    command.extend(["-EncodedCommand", encoded])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "PowerShell command failed")
    stdout = completed.stdout.strip()
    return json.loads(stdout) if stdout else {}


def _ps_json_literal(value: Any) -> str:
    encoded = json.dumps(value).replace("'", "''")
    return f"(ConvertFrom-Json -InputObject '{encoded}')"


def _safe_artifact_path(data_root: Path, filename: str, *, suffix: str = ".png") -> Path:
    clean = str(filename or "").strip()
    if not clean:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        clean = f"screenshot-{stamp}{suffix}"
    candidate_name = Path(clean).name
    if candidate_name != clean or candidate_name in {"", ".", ".."}:
        raise ValueError("artifact name must be a simple filename")
    if Path(candidate_name).suffix.lower() != suffix:
        candidate_name += suffix
    artifacts_root = (data_root / "windows_artifacts").resolve()
    target = (artifacts_root / candidate_name).resolve()
    if not target.is_relative_to(artifacts_root):
        raise ValueError("artifact path must remain inside the windows artifact directory")
    return target


def _resolve_existing_artifact_path(data_root: Path, value: str, *, suffix: str = ".png") -> Path:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError("image_path is required")
    artifacts_root = (data_root / "windows_artifacts").resolve()
    raw_path = Path(clean)
    if raw_path.is_absolute():
        target = raw_path.resolve()
    else:
        candidate_name = raw_path.name
        if candidate_name != clean or candidate_name in {"", ".", ".."}:
            raise PermissionError("image_path must remain inside the windows artifact directory")
        if Path(candidate_name).suffix.lower() != suffix:
            candidate_name += suffix
        target = (artifacts_root / candidate_name).resolve()
    if not target.is_relative_to(artifacts_root):
        raise PermissionError("image_path must remain inside the windows artifact directory")
    if target.suffix.lower() != suffix:
        raise ValueError(f"image_path must reference a {suffix} artifact")
    if not target.exists():
        raise FileNotFoundError(target)
    return target


def _normalize_registry_path(raw_path: str) -> str:
    text = str(raw_path or "").strip().replace("/", "\\")
    replacements = {
        "HKEY_CURRENT_USER\\": "HKCU:\\",
        "HKEY_LOCAL_MACHINE\\": "HKLM:\\",
        "HKCU\\": "HKCU:\\",
        "HKLM\\": "HKLM:\\",
    }
    for prefix, replacement in replacements.items():
        if text.upper().startswith(prefix.upper()):
            text = replacement + text[len(prefix) :]
            break
    text = text.rstrip("\\")
    if not text:
        raise ValueError("registry path is required")
    return text


def _registry_path_is_under(path: str, roots: tuple[str, ...]) -> bool:
    normalized = path.lower()
    for root in roots:
        root_normalized = root.lower().rstrip("\\")
        if normalized == root_normalized or normalized.startswith(root_normalized + "\\"):
            return True
    return False


def _normalize_registry_value_name(raw_name: str) -> str:
    name = str(raw_name or "").strip()
    if not name:
        raise ValueError("registry value name is required")
    if "\\" in name or "/" in name:
        raise ValueError("registry value name must not contain path separators")
    return name


def _normalize_registry_value_kind(raw_kind: str) -> str:
    kind = str(raw_kind or "String").strip() or "String"
    for allowed in REGISTRY_VALUE_KINDS:
        if allowed.lower() == kind.lower():
            return allowed
    raise ValueError(f"registry value_kind must be one of: {', '.join(sorted(REGISTRY_VALUE_KINDS))}")


class WindowsListWindowsTool:
    definition = ToolDefinition(
        tool_id="windows.list_windows",
        name="List Windows",
        description="List visible desktop windows and owning processes",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit = min(max(int(payload.get("limit", 40)), 1), 120)
        script = f"""
        $windows = Get-Process |
            Where-Object {{ $_.MainWindowTitle -and $_.MainWindowTitle.Trim().Length -gt 0 }} |
            Sort-Object ProcessName |
            Select-Object -First {limit} Id, ProcessName, MainWindowTitle
        @{{ windows = @($windows) }} | ConvertTo-Json -Depth 4 -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10)


class WindowsLaunchApplicationTool:
    definition = ToolDefinition(
        tool_id="windows.launch_application",
        name="Launch Application",
        description="Launch a Windows application or executable",
        tier=2,
    )

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        command = payload.get("command") or payload.get("path")
        if not command:
            raise ValueError("command is required")
        args = [str(item) for item in payload.get("args", [])]
        workdir_value = payload.get("workdir")
        cwd = self._resolve_workdir(workdir_value)
        process = subprocess.Popen(
            [command, *args],
            cwd=cwd,
        )
        return {
            "command": command,
            "args": args,
            "workdir": str(cwd) if cwd else None,
            "pid": process.pid,
        }

    def _resolve_workdir(self, value: str | None) -> Path | None:
        if not value:
            return None
        raw = Path(value)
        if raw.is_absolute():
            return raw
        return (self.workspace_root / raw).resolve()


class WindowsActivateWindowTool:
    definition = ToolDefinition(
        tool_id="windows.activate_window",
        name="Activate Window",
        description="Bring a target window to the foreground by title or process id",
        tier=1,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        window_title = payload.get("window_title")
        process_id = payload.get("process_id")
        if not window_title and process_id is None:
            raise ValueError("window_title or process_id is required")
        title_literal = json.dumps(window_title or "")
        process_id_literal = "null" if process_id is None else json.dumps(int(process_id))
        script = f"""
        $windowTitle = {title_literal}
        $processId = {process_id_literal}
        $shell = New-Object -ComObject WScript.Shell
        if ($processId -ne $null) {{
            $process = Get-Process -Id $processId -ErrorAction Stop
            $activated = $shell.AppActivate($process.Id)
            $target = $process.MainWindowTitle
        }} else {{
            $activated = $shell.AppActivate($windowTitle)
            $target = $windowTitle
        }}
        @{{ activated = [bool]$activated; target = $target }} | ConvertTo-Json -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10)


class WindowsSendKeysTool:
    definition = ToolDefinition(
        tool_id="windows.send_keys",
        name="Send Keys",
        description="Send keystrokes to the active window or a target window",
        tier=2,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        keys = payload.get("keys")
        if not keys:
            raise ValueError("keys is required")
        window_title = payload.get("window_title")
        delay_ms = min(max(int(payload.get("delay_ms", 300)), 0), 5000)
        title_literal = json.dumps(window_title or "")
        keys_literal = json.dumps(str(keys))
        script = f"""
        $windowTitle = {title_literal}
        $keys = {keys_literal}
        $delayMs = {delay_ms}
        $shell = New-Object -ComObject WScript.Shell
        $activated = $true
        if ($windowTitle -and $windowTitle.Trim().Length -gt 0) {{
            $activated = $shell.AppActivate($windowTitle)
            Start-Sleep -Milliseconds $delayMs
        }}
        if (-not $activated) {{
            throw "Unable to activate target window."
        }}
        $shell.SendKeys($keys)
        @{{ sent = $true; target = $windowTitle; keys = $keys }} | ConvertTo-Json -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10)


class WindowsOpenUrlTool:
    definition = ToolDefinition(
        tool_id="windows.open_url",
        name="Open URL",
        description="Open a URL in the default browser and leave it visible",
        tier=1,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url", "")).strip()
        if not url:
            raise ValueError("url is required")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("windows.open_url only supports http and https URLs")
        script = f"""
        $url = {json.dumps(url)}
        Start-Process $url | Out-Null
        @{{ opened = $true; url = $url }} | ConvertTo-Json -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10)


class WindowsNotificationTool:
    definition = ToolDefinition(
        tool_id="windows.notify",
        name="Send Windows Notification",
        description="Dispatch a local Windows toast notification when notifications are enabled",
        tier=1,
    )

    def __init__(self, settings_service=None) -> None:
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.settings_service is not None:
            settings = self.settings_service.get_all()
            if not settings.get("notifications_enabled", True):
                raise PermissionError("notifications are disabled in settings")
        title = str(payload.get("title", "")).strip()
        message = str(payload.get("message", payload.get("body", ""))).strip()
        if not title:
            raise ValueError("title is required")
        if not message:
            raise ValueError("message is required")
        if len(title) > 120:
            raise ValueError("title must be 120 characters or fewer")
        if len(message) > 1000:
            raise ValueError("message must be 1000 characters or fewer")
        app_id = str(payload.get("app_id", "Project Q")).strip()[:120] or "Project Q"
        title_literal = json.dumps(title)
        message_literal = json.dumps(message)
        app_id_literal = json.dumps(app_id)
        script = f"""
        $title = {title_literal}
        $message = {message_literal}
        $appId = {app_id_literal}
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
        $escapedTitle = [System.Security.SecurityElement]::Escape($title)
        $escapedMessage = [System.Security.SecurityElement]::Escape($message)
        $template = "<toast><visual><binding template=`"ToastGeneric`"><text>$escapedTitle</text><text>$escapedMessage</text></binding></visual></toast>"
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml($template)
        $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId)
        $notifier.Show($toast)
        @{{
            delivered = $true
            title = $title
            message_length = $message.Length
            app_id = $appId
        }} | ConvertTo-Json -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10, sta=True)


class WindowsClipboardReadTool:
    definition = ToolDefinition(
        tool_id="windows.clipboard_read",
        name="Read Clipboard",
        description="Read text from the Windows clipboard with origin metadata",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        max_chars = min(max(int(payload.get("max_chars", 5000)), 1), 50000)
        script = f"""
        Add-Type -AssemblyName System.Windows.Forms
        $text = [System.Windows.Forms.Clipboard]::GetText()
        $truncated = $false
        if ($text.Length -gt {max_chars}) {{
            $text = $text.Substring(0, {max_chars})
            $truncated = $true
        }}
        @{{ text = $text; length = $text.Length; truncated = $truncated; origin = "windows_clipboard" }} |
            ConvertTo-Json -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10, sta=True)


class WindowsClipboardWriteTool:
    definition = ToolDefinition(
        tool_id="windows.clipboard_write",
        name="Write Clipboard",
        description="Write owner-provided text to the Windows clipboard",
        tier=2,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text", ""))
        if text == "":
            raise ValueError("text is required")
        text_literal = json.dumps(text)
        script = f"""
        Add-Type -AssemblyName System.Windows.Forms
        $text = {text_literal}
        [System.Windows.Forms.Clipboard]::SetText($text)
        @{{ written = $true; length = $text.Length; target = "windows_clipboard" }} |
            ConvertTo-Json -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10, sta=True)


class WindowsCaptureScreenshotTool:
    definition = ToolDefinition(
        tool_id="windows.capture_screenshot",
        name="Capture Screenshot",
        description="Capture the Windows virtual screen to a Project Q artifact file",
        tier=1,
    )

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = _safe_artifact_path(self.data_root, str(payload.get("name", "")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target_literal = json.dumps(str(target))
        script = f"""
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $target = {target_literal}
        $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
        $bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        try {{
            $graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bounds.Size)
            $bitmap.Save($target, [System.Drawing.Imaging.ImageFormat]::Png)
        }} finally {{
            $graphics.Dispose()
            $bitmap.Dispose()
        }}
        @{{ captured = $true; path = $target; width = $bounds.Width; height = $bounds.Height; origin = "windows_screen" }} |
            ConvertTo-Json -Compress
        """
        return _run_powershell_json(script, timeout_seconds=20)


class WindowsOcrScreenshotTool:
    definition = ToolDefinition(
        tool_id="windows.ocr_screenshot",
        name="OCR Screenshot",
        description="Extract text from a Project Q Windows screenshot artifact",
        tier=1,
    )

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        image_path = str(payload.get("image_path", "")).strip()
        if image_path:
            target = _resolve_existing_artifact_path(self.data_root, image_path)
            capture_first = False
        else:
            target = _safe_artifact_path(self.data_root, str(payload.get("name", "")))
            target.parent.mkdir(parents=True, exist_ok=True)
            capture_first = True
        target_literal = json.dumps(str(target))
        capture_literal = "$true" if capture_first else "$false"
        script = f"""
        $target = {target_literal}
        $captureFirst = {capture_literal}

        if ($captureFirst) {{
            Add-Type -AssemblyName System.Windows.Forms
            Add-Type -AssemblyName System.Drawing
            $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
            $bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
            $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
            try {{
                $graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bounds.Size)
                $bitmap.Save($target, [System.Drawing.Imaging.ImageFormat]::Png)
            }} finally {{
                $graphics.Dispose()
                $bitmap.Dispose()
            }}
        }}

        Add-Type -AssemblyName System.Runtime.WindowsRuntime
        [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
        [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime] | Out-Null
        [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime] | Out-Null

        function Await-Operation($operation) {{
            $task = [System.WindowsRuntimeSystemExtensions]::AsTask($operation)
            $task.Wait()
            return $task.Result
        }}

        $file = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($target))
        $stream = Await-Operation ($file.OpenReadAsync())
        $decoder = Await-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream))
        $softwareBitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync())
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
        if ($null -eq $engine) {{
            throw "Windows OCR engine is unavailable."
        }}
        $ocrResult = Await-Operation ($engine.RecognizeAsync($softwareBitmap))
        $lines = @()
        foreach ($line in $ocrResult.Lines) {{
            $words = @($line.Words | ForEach-Object {{ $_.Text }})
            $lines += @{{ text = $line.Text; words = $words }}
        }}
        @{{
            path = $target
            text = $ocrResult.Text
            line_count = $lines.Count
            lines = @($lines)
            origin = "windows_screen_ocr"
        }} | ConvertTo-Json -Depth 8 -Compress
        """
        return _run_powershell_json(script, timeout_seconds=30, sta=True)


class WindowsAppStateTool:
    definition = ToolDefinition(
        tool_id="windows.app_state",
        name="Inspect App State",
        description="Query running Windows application state by process id, process name, or window title",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        process_id = payload.get("process_id")
        process_name = str(payload.get("process_name", "")).strip()
        window_title = str(payload.get("window_title", "")).strip()
        if process_id is None and not process_name and not window_title:
            raise ValueError("process_id, process_name, or window_title is required")
        limit = min(max(int(payload.get("limit", 20)), 1), 100)
        query = {
            "process_id": int(process_id) if process_id is not None else None,
            "process_name": process_name,
            "window_title": window_title,
        }
        query_literal = _ps_json_literal(query)
        script = f"""
        $query = {query_literal}
        $limit = {limit}
        $all = Get-Process -ErrorAction SilentlyContinue
        if ($query.process_id -ne $null) {{
            $matches = @($all | Where-Object {{ $_.Id -eq [int]$query.process_id }})
        }} elseif ($query.process_name) {{
            $name = [string]$query.process_name
            $matches = @($all | Where-Object {{ $_.ProcessName -like "*$name*" }})
        }} else {{
            $title = [string]$query.window_title
            $matches = @($all | Where-Object {{ $_.MainWindowTitle -like "*$title*" }})
        }}
        $items = @()
        foreach ($process in ($matches | Select-Object -First $limit)) {{
            $path = ""
            $startTime = ""
            $cpu = $null
            try {{ $path = [string]$process.Path }} catch {{ $path = "" }}
            try {{ $startTime = $process.StartTime.ToUniversalTime().ToString("o") }} catch {{ $startTime = "" }}
            try {{ $cpu = [double]$process.CPU }} catch {{ $cpu = $null }}
            $items += @{{
                id = [int]$process.Id
                process_name = [string]$process.ProcessName
                main_window_title = [string]$process.MainWindowTitle
                has_main_window = [bool]($process.MainWindowHandle -ne 0)
                responding = [bool]$process.Responding
                path = $path
                start_time = $startTime
                cpu_seconds = $cpu
                working_set_bytes = [int64]$process.WorkingSet64
            }}
        }}
        @{{
            running = [bool]($items.Count -gt 0)
            query = $query
            count = [int]$items.Count
            matches = @($items)
        }} | ConvertTo-Json -Depth 6 -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10)


class WindowsFocusFollowTool:
    definition = ToolDefinition(
        tool_id="windows.focus_follow",
        name="Focus Window By Heuristic",
        description="Find and activate the best matching visible Windows application window",
        tier=1,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query", "")).strip()
        process_name = str(payload.get("process_name", "")).strip()
        window_title = str(payload.get("window_title", "")).strip()
        candidates = payload.get("candidates") or []
        if not isinstance(candidates, list):
            raise ValueError("candidates must be a list")
        normalized_candidates: list[dict[str, str]] = []
        for candidate in candidates[:20]:
            if isinstance(candidate, str):
                text = candidate.strip()
                if text:
                    normalized_candidates.append({"query": text})
            elif isinstance(candidate, dict):
                normalized_candidates.append(
                    {
                        "query": str(candidate.get("query", "")).strip(),
                        "process_name": str(candidate.get("process_name", "")).strip(),
                        "window_title": str(
                            candidate.get("window_title", candidate.get("title_contains", ""))
                        ).strip(),
                    }
                )
        if query:
            normalized_candidates.insert(0, {"query": query})
        if process_name or window_title:
            normalized_candidates.insert(
                0,
                {"query": "", "process_name": process_name, "window_title": window_title},
            )
        normalized_candidates = [
            item for item in normalized_candidates if item.get("query") or item.get("process_name") or item.get("window_title")
        ]
        if not normalized_candidates:
            raise ValueError("query, process_name, window_title, or candidates is required")
        candidates_literal = _ps_json_literal(normalized_candidates)
        script = f"""
        $candidates = @({candidates_literal})
        $windows = @(Get-Process -ErrorAction SilentlyContinue |
            Where-Object {{ $_.MainWindowTitle -and $_.MainWindowTitle.Trim().Length -gt 0 }})
        $ranked = @()
        foreach ($window in $windows) {{
            $bestScore = 0
            $bestMatchedBy = ""
            foreach ($candidate in $candidates) {{
                $score = 0
                $matchedBy = ""
                $query = [string]$candidate.query
                $processName = [string]$candidate.process_name
                $windowTitle = [string]$candidate.window_title
                if ($processName -and $window.ProcessName -like "*$processName*") {{ $score += 4; $matchedBy = "process_name" }}
                if ($windowTitle -and $window.MainWindowTitle -like "*$windowTitle*") {{ $score += 4; $matchedBy = "window_title" }}
                if ($query -and $window.MainWindowTitle -like "*$query*") {{ $score += 2; $matchedBy = "query_title" }}
                if ($query -and $window.ProcessName -like "*$query*") {{ $score += 1; $matchedBy = "query_process" }}
                if ($score -gt $bestScore) {{
                    $bestScore = $score
                    $bestMatchedBy = $matchedBy
                }}
            }}
            if ($bestScore -gt 0) {{
                $ranked += [pscustomobject]@{{
                    id = [int]$window.Id
                    process_name = [string]$window.ProcessName
                    main_window_title = [string]$window.MainWindowTitle
                    score = [int]$bestScore
                    matched_by = $bestMatchedBy
                }}
            }}
        }}
        $matched = @($ranked | Sort-Object score -Descending | Select-Object -First 1)
        if ($matched.Count -eq 0) {{
            throw "No visible window matched the focus heuristic."
        }}
        $shell = New-Object -ComObject WScript.Shell
        $activated = $shell.AppActivate([int]$matched[0].id)
        @{{
            activated = [bool]$activated
            matched = $matched[0]
            candidate_count = [int]$candidates.Count
        }} | ConvertTo-Json -Depth 6 -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10, sta=True)


class WindowsScreenshotDiffTool:
    definition = ToolDefinition(
        tool_id="windows.screenshot_diff",
        name="Compare Screenshots",
        description="Compare two Project Q Windows screenshot artifacts with bounded pixel sampling",
        tier=0,
    )

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        before_raw = str(payload.get("before_image_path", payload.get("before", ""))).strip()
        after_raw = str(payload.get("after_image_path", payload.get("after", ""))).strip()
        before = _resolve_existing_artifact_path(self.data_root, before_raw)
        after = _resolve_existing_artifact_path(self.data_root, after_raw)
        max_samples = min(max(int(payload.get("max_samples", 5000)), 100), 100000)
        before_literal = _ps_json_literal(str(before))
        after_literal = _ps_json_literal(str(after))
        script = f"""
        Add-Type -AssemblyName System.Drawing
        $beforePath = {before_literal}
        $afterPath = {after_literal}
        $maxSamples = {max_samples}
        $beforeBitmap = [System.Drawing.Bitmap]::FromFile($beforePath)
        $afterBitmap = [System.Drawing.Bitmap]::FromFile($afterPath)
        try {{
            $sameDimensions = ($beforeBitmap.Width -eq $afterBitmap.Width -and $beforeBitmap.Height -eq $afterBitmap.Height)
            $sampled = 0
            $mismatched = 0
            if ($sameDimensions) {{
                $root = [Math]::Sqrt([double]$maxSamples)
                $stepX = [Math]::Max([int][Math]::Ceiling($beforeBitmap.Width / $root), 1)
                $stepY = [Math]::Max([int][Math]::Ceiling($beforeBitmap.Height / $root), 1)
                for ($y = 0; $y -lt $beforeBitmap.Height; $y += $stepY) {{
                    for ($x = 0; $x -lt $beforeBitmap.Width; $x += $stepX) {{
                        $sampled += 1
                        if ($beforeBitmap.GetPixel($x, $y).ToArgb() -ne $afterBitmap.GetPixel($x, $y).ToArgb()) {{
                            $mismatched += 1
                        }}
                    }}
                }}
            }}
            $ratio = if ($sampled -gt 0) {{ [Math]::Round($mismatched / [double]$sampled, 6) }} else {{ 1.0 }}
            @{{
                different = [bool]((-not $sameDimensions) -or $mismatched -gt 0)
                before = $beforePath
                after = $afterPath
                before_dimensions = @{{ width = [int]$beforeBitmap.Width; height = [int]$beforeBitmap.Height }}
                after_dimensions = @{{ width = [int]$afterBitmap.Width; height = [int]$afterBitmap.Height }}
                same_dimensions = [bool]$sameDimensions
                sampled_pixels = [int]$sampled
                mismatched_pixels = [int]$mismatched
                mismatch_ratio = [double]$ratio
                origin = "windows_screenshot_diff"
            }} | ConvertTo-Json -Depth 6 -Compress
        }} finally {{
            $beforeBitmap.Dispose()
            $afterBitmap.Dispose()
        }}
        """
        return _run_powershell_json(script, timeout_seconds=30)


class WindowsInspectUITreeTool:
    definition = ToolDefinition(
        tool_id="windows.inspect_ui_tree",
        name="Inspect UI Tree",
        description="Inspect Windows UI Automation elements for a target window",
        tier=0,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        window_title = str(payload.get("window_title", "")).strip()
        process_id = payload.get("process_id")
        if not window_title and process_id is None:
            raise ValueError("window_title or process_id is required")
        max_elements = min(max(int(payload.get("max_elements", 80)), 1), 250)
        include_offscreen = bool(payload.get("include_offscreen", False))
        title_literal = json.dumps(window_title)
        process_id_literal = "null" if process_id is None else json.dumps(int(process_id))
        include_offscreen_literal = "$true" if include_offscreen else "$false"
        script = f"""
        Add-Type -AssemblyName UIAutomationClient
        Add-Type -AssemblyName UIAutomationTypes
        $windowTitle = {title_literal}
        $processId = {process_id_literal}
        $maxElements = {max_elements}
        $includeOffscreen = {include_offscreen_literal}

        function Convert-Element($element) {{
            $rect = $element.Current.BoundingRectangle
            @{{
                name = $element.Current.Name
                automation_id = $element.Current.AutomationId
                control_type = $element.Current.ControlType.ProgrammaticName.Replace("ControlType.", "")
                class_name = $element.Current.ClassName
                process_id = $element.Current.ProcessId
                is_enabled = [bool]$element.Current.IsEnabled
                is_offscreen = [bool]$element.Current.IsOffscreen
                bounding_rectangle = @{{
                    left = [int]$rect.Left
                    top = [int]$rect.Top
                    width = [int]$rect.Width
                    height = [int]$rect.Height
                }}
            }}
        }}

        $root = [System.Windows.Automation.AutomationElement]::RootElement
        $condition = $null
        if ($processId -ne $null) {{
            $condition = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
                [int]$processId
            )
        }} else {{
            $condition = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty,
                $windowTitle
            )
        }}
        $target = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $condition)
        if ($target -eq $null -and $windowTitle) {{
            $allWindows = $root.FindAll(
                [System.Windows.Automation.TreeScope]::Children,
                [System.Windows.Automation.Condition]::TrueCondition
            )
            foreach ($candidate in $allWindows) {{
                if ($candidate.Current.Name -like "*$windowTitle*") {{
                    $target = $candidate
                    break
                }}
            }}
        }}
        if ($target -eq $null) {{
            throw "Target window was not found."
        }}

        $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
        $queue = New-Object System.Collections.Queue
        $queue.Enqueue($target)
        $elements = New-Object System.Collections.ArrayList
        while ($queue.Count -gt 0 -and $elements.Count -lt $maxElements) {{
            $current = $queue.Dequeue()
            if ($current -ne $target) {{
                if ($includeOffscreen -or -not $current.Current.IsOffscreen) {{
                    [void]$elements.Add((Convert-Element $current))
                }}
            }}
            $child = $walker.GetFirstChild($current)
            while ($child -ne $null -and ($queue.Count + $elements.Count) -lt ($maxElements * 3)) {{
                $queue.Enqueue($child)
                $child = $walker.GetNextSibling($child)
            }}
        }}

        @{{
            root = Convert-Element $target
            elements = @($elements)
            max_elements = $maxElements
            include_offscreen = [bool]$includeOffscreen
        }} | ConvertTo-Json -Depth 8 -Compress
        """
        return _run_powershell_json(script, timeout_seconds=20, sta=True)


class WindowsInvokeUIElementTool:
    definition = ToolDefinition(
        tool_id="windows.invoke_ui_element",
        name="Invoke UI Element",
        description="Invoke a Windows UI Automation element by automation id, name, or control type",
        tier=2,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        window_title = str(payload.get("window_title", "")).strip()
        process_id = payload.get("process_id")
        automation_id = str(payload.get("automation_id", "")).strip()
        name = str(payload.get("name", "")).strip()
        control_type = str(payload.get("control_type", "")).strip()
        if not window_title and process_id is None:
            raise ValueError("window_title or process_id is required")
        if not automation_id and not name and not control_type:
            raise ValueError("automation_id, name, or control_type is required")
        title_literal = json.dumps(window_title)
        process_id_literal = "null" if process_id is None else json.dumps(int(process_id))
        automation_id_literal = json.dumps(automation_id)
        name_literal = json.dumps(name)
        control_type_literal = json.dumps(control_type)
        script = f"""
        Add-Type -AssemblyName UIAutomationClient
        Add-Type -AssemblyName UIAutomationTypes
        $windowTitle = {title_literal}
        $processId = {process_id_literal}
        $automationId = {automation_id_literal}
        $name = {name_literal}
        $controlType = {control_type_literal}

        function Convert-Element($element) {{
            @{{
                name = $element.Current.Name
                automation_id = $element.Current.AutomationId
                control_type = $element.Current.ControlType.ProgrammaticName.Replace("ControlType.", "")
                class_name = $element.Current.ClassName
                process_id = $element.Current.ProcessId
            }}
        }}

        $root = [System.Windows.Automation.AutomationElement]::RootElement
        if ($processId -ne $null) {{
            $windowCondition = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
                [int]$processId
            )
        }} else {{
            $windowCondition = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty,
                $windowTitle
            )
        }}
        $target = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $windowCondition)
        if ($target -eq $null -and $windowTitle) {{
            $allWindows = $root.FindAll(
                [System.Windows.Automation.TreeScope]::Children,
                [System.Windows.Automation.Condition]::TrueCondition
            )
            foreach ($candidate in $allWindows) {{
                if ($candidate.Current.Name -like "*$windowTitle*") {{
                    $target = $candidate
                    break
                }}
            }}
        }}
        if ($target -eq $null) {{
            throw "Target window was not found."
        }}

        $conditions = New-Object System.Collections.ArrayList
        if ($automationId) {{
            [void]$conditions.Add((New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
                $automationId
            )))
        }}
        if ($name) {{
            [void]$conditions.Add((New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty,
                $name
            )))
        }}
        $condition = if ($conditions.Count -eq 1) {{
            $conditions[0]
        }} elseif ($conditions.Count -gt 1) {{
            New-Object System.Windows.Automation.AndCondition @($conditions.ToArray())
        }} else {{
            [System.Windows.Automation.Condition]::TrueCondition
        }}

        $matches = $target.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
        $matched = $null
        foreach ($candidate in $matches) {{
            if ($controlType -and $candidate.Current.ControlType.ProgrammaticName.Replace("ControlType.", "") -ne $controlType) {{
                continue
            }}
            $matched = $candidate
            break
        }}
        if ($matched -eq $null) {{
            throw "Matching UI element was not found."
        }}

        $pattern = $null
        if (-not $matched.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {{
            throw "Matching UI element does not support InvokePattern."
        }}
        $pattern.Invoke()
        @{{
            invoked = $true
            matched = Convert-Element $matched
        }} | ConvertTo-Json -Depth 6 -Compress
        """
        return _run_powershell_json(script, timeout_seconds=20, sta=True)


class WindowsRegistryReadTool:
    definition = ToolDefinition(
        tool_id="windows.registry_read",
        name="Read Registry Value",
        description="Read a scoped Windows registry value from approved software/environment roots",
        tier=1,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = _normalize_registry_path(str(payload.get("path", "")))
        name = _normalize_registry_value_name(str(payload.get("name", "")))
        if not _registry_path_is_under(path, REGISTRY_READ_ROOTS):
            raise PermissionError("registry read path is outside approved roots")
        path_literal = json.dumps(path)
        name_literal = json.dumps(name)
        script = f"""
        $path = {path_literal}
        $name = {name_literal}
        $key = Get-Item -LiteralPath $path -ErrorAction Stop
        $value = $key.GetValue($name, $null)
        if ($null -eq $value) {{
            throw "Registry value was not found."
        }}
        $kind = $key.GetValueKind($name).ToString()
        @{{
            path = $path
            name = $name
            value = $value
            value_kind = $kind
        }} | ConvertTo-Json -Depth 5 -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10)


class WindowsRegistryWriteTool:
    definition = ToolDefinition(
        tool_id="windows.registry_write",
        name="Write Registry Value",
        description="Write a Windows registry value under the Project Q HKCU scope",
        tier=3,
    )

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = _normalize_registry_path(str(payload.get("path", "")))
        name = _normalize_registry_value_name(str(payload.get("name", "")))
        kind = _normalize_registry_value_kind(str(payload.get("value_kind", "String")))
        if not _registry_path_is_under(path, REGISTRY_WRITE_ROOTS):
            raise PermissionError("registry write path is outside the Project Q HKCU scope")
        value = payload.get("value", "")
        if kind in {"DWord", "QWord"}:
            value = int(value)
        else:
            value = str(value)

        path_literal = json.dumps(path)
        name_literal = json.dumps(name)
        value_literal = json.dumps(value)
        kind_literal = json.dumps(kind)
        script = f"""
        $path = {path_literal}
        $name = {name_literal}
        $value = {value_literal}
        $kind = {kind_literal}
        if ($kind -eq "DWord") {{
            $value = [int]$value
        }} elseif ($kind -eq "QWord") {{
            $value = [long]$value
        }}
        New-Item -Path $path -Force | Out-Null
        New-ItemProperty -LiteralPath $path -Name $name -Value $value -PropertyType $kind -Force | Out-Null
        @{{
            written = $true
            path = $path
            name = $name
            value = $value
            value_kind = $kind
        }} | ConvertTo-Json -Depth 5 -Compress
        """
        return _run_powershell_json(script, timeout_seconds=10)
