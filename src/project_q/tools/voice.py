from __future__ import annotations

import base64
import json
import subprocess
from typing import Any

from project_q.tools.base import ToolDefinition


def _run_powershell_json(script: str, *, timeout_seconds: int = 30) -> Any:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "PowerShell voice command failed")
    stdout = completed.stdout.strip()
    return json.loads(stdout) if stdout else {}


class VoiceSpeakTool:
    definition = ToolDefinition(
        tool_id="voice.speak",
        name="Speak Text",
        description="Speak owner-approved text through local Windows text-to-speech",
        tier=1,
    )

    def __init__(self, settings_service) -> None:
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings_service.get_all()
        if not settings.get("voice_enabled", False):
            raise PermissionError("voice output is disabled in settings")

        text = str(payload.get("text", "")).strip()
        if not text:
            raise ValueError("text is required")
        if len(text) > 4000:
            raise ValueError("voice text must be 4000 characters or fewer")

        rate = min(max(int(payload.get("rate", 0)), -10), 10)
        volume = min(max(int(payload.get("volume", 85)), 0), 100)
        voice = str(payload.get("voice", "")).strip()
        text_literal = json.dumps(text)
        voice_literal = json.dumps(voice)
        script = f"""
        Add-Type -AssemblyName System.Speech
        $speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
        $speaker.Rate = {rate}
        $speaker.Volume = {volume}
        $voice = {voice_literal}
        if ($voice -and $voice.Trim().Length -gt 0) {{
            $speaker.SelectVoice($voice)
        }}
        $text = {text_literal}
        try {{
            $speaker.Speak($text)
        }} finally {{
            $speaker.Dispose()
        }}
        @{{ spoken = $true; text_length = $text.Length; voice = $voice; rate = {rate}; volume = {volume} }} |
            ConvertTo-Json -Compress
        """
        return _run_powershell_json(script, timeout_seconds=60)


class VoiceListenOnceTool:
    definition = ToolDefinition(
        tool_id="voice.listen_once",
        name="Listen Once",
        description="Capture one local Windows microphone utterance as text when voice is enabled",
        tier=1,
    )

    def __init__(self, settings_service) -> None:
        self.settings_service = settings_service

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings_service.get_all()
        if not settings.get("voice_enabled", False):
            raise PermissionError("voice input is disabled in settings")

        timeout_seconds = min(max(int(payload.get("timeout_seconds", 6)), 1), 30)
        source = str(payload.get("source", "desktop_microphone")).strip()[:100] or "desktop_microphone"
        source_literal = json.dumps(source)
        script = f"""
        Add-Type -AssemblyName System.Speech
        $timeoutSeconds = {timeout_seconds}
        $source = {source_literal}
        $recognizer = New-Object System.Speech.Recognition.SpeechRecognitionEngine
        try {{
            $grammar = New-Object System.Speech.Recognition.DictationGrammar
            $recognizer.LoadGrammar($grammar)
            $recognizer.SetInputToDefaultAudioDevice()
            $result = $recognizer.Recognize([TimeSpan]::FromSeconds($timeoutSeconds))
            if ($null -eq $result) {{
                @{{
                    recognized = $false
                    transcript = ""
                    confidence = 0
                    source = $source
                    timeout_seconds = $timeoutSeconds
                }} | ConvertTo-Json -Compress
            }} else {{
                @{{
                    recognized = $true
                    transcript = $result.Text
                    confidence = [double]$result.Confidence
                    source = $source
                    timeout_seconds = $timeoutSeconds
                }} | ConvertTo-Json -Compress
            }}
        }} finally {{
            $recognizer.Dispose()
        }}
        """
        return _run_powershell_json(script, timeout_seconds=timeout_seconds + 15)
