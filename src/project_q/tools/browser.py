from __future__ import annotations

import json
import ipaddress
import os
import re
import secrets
import socket
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse, urlunparse

from project_q.runtime import discover_node_executable, discover_node_modules_path
from project_q.tools.access import resolve_allowed_path
from project_q.tools.base import ToolDefinition
from project_q.tools.windows import WindowsOpenUrlTool


_BROWSER_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BROWSER_PROFILE_LOCK = threading.RLock()
_BROWSER_ACTION_TYPES = {
    "goto",
    "click",
    "fill",
    "press",
    "wait_for_selector",
    "wait_for_timeout",
    "extract_text",
    "screenshot",
    "select_option",
    "check",
    "uncheck",
    "hover",
    "new_tab",
    "switch_tab",
    "close_tab",
    "download",
    "upload",
    "pause_for_owner",
}


def _validated_web_url(raw_url: str) -> str:
    url = str(raw_url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("browser tools only support http and https URLs")
    if _is_private_network_host(parsed.hostname or ""):
        raise PermissionError("browser tools do not inspect localhost or private network URLs")
    if _hostname_resolves_to_private_network(parsed.hostname or ""):
        raise PermissionError(
            "browser tools do not inspect hostnames that resolve to private network addresses"
        )
    return url


def _is_private_network_host(hostname: str) -> bool:
    host = str(hostname or "").strip().strip("[]").rstrip(".").lower()
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
        or address.is_multicast
    )


def _hostname_resolves_to_private_network(hostname: str) -> bool:
    host = str(hostname or "").strip().strip("[]").rstrip(".")
    if not host:
        return True
    try:
        addresses = socket.getaddrinfo(
            host,
            None,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return False
    return any(
        _is_private_network_host(str(sockaddr[0]))
        for _family, _socktype, _proto, _canonname, sockaddr in addresses
        if sockaddr
    )


def _validated_browser_actions(raw_actions: Any) -> list[dict[str, Any]]:
    if raw_actions is None:
        return []
    if not isinstance(raw_actions, list):
        raise ValueError("browser actions must be a list")
    if len(raw_actions) > 50:
        raise ValueError("browser actions are limited to 50 steps")

    validated: list[dict[str, Any]] = []
    for index, raw_action in enumerate(raw_actions):
        if not isinstance(raw_action, dict):
            raise ValueError(f"browser action {index + 1} must be an object")
        action = dict(raw_action)
        action_type = str(action.get("type", "")).strip()
        if action_type not in _BROWSER_ACTION_TYPES:
            raise ValueError(f"unsupported browser action type: {action_type or '(missing)'}")
        action["type"] = action_type

        if action_type in {"goto", "new_tab"}:
            action["url"] = _validated_web_url(action.get("url", ""))
        if "selector" in action and len(str(action["selector"])) > 2000:
            raise ValueError("browser action selector is too long")
        if "value" in action and len(str(action["value"])) > 100_000:
            raise ValueError("browser action value is too long")
        if action_type in {"wait_for_timeout", "pause_for_owner"}:
            timeout_ms = int(action.get("timeout_ms", 1000))
            if timeout_ms < 0 or timeout_ms > 300_000:
                raise ValueError("browser action timeout_ms must be between 0 and 300000")
            action["timeout_ms"] = timeout_ms
        if action_type == "switch_tab":
            tab_index = int(action.get("index", -1))
            if tab_index < 0 or tab_index > 49:
                raise ValueError("browser tab index must be between 0 and 49")
            action["index"] = tab_index
        validated.append(action)
    return validated


class BrowserInspectTool:
    definition = ToolDefinition(
        tool_id="browser.inspect_page",
        name="Inspect Browser Page",
        description="Open a page with Playwright and extract title, text, and links",
        tier=1,
    )

    def __init__(self, data_root: Path, settings_service=None) -> None:
        self.data_root = data_root
        self.settings_service = settings_service
        self.node_path = discover_node_executable()
        self.node_modules_path = discover_node_modules_path()
        self.worker_script = Path(__file__).resolve().parent.parent / "workers" / "browser_worker.js"

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings_service.get_all() if self.settings_service is not None else {}
        return self._run_worker(
            {
                "mode": "inspect",
                "url": _validated_web_url(payload["url"]),
                "browser_type": payload.get("browser_type", "chromium"),
                "headless": bool(payload.get("headless", True)),
                "channel": payload.get("channel", settings.get("browser_channel", "msedge")),
                "executable_path": payload.get("executable_path", settings.get("browser_executable_path", "")),
                "timeout_seconds": int(payload.get("timeout_seconds", 25)),
                "include_links": bool(payload.get("include_links", False)),
                "screenshot_path": self._build_screenshot_path(payload) if payload.get("screenshot") else "",
            }
        )

    def self_test(self) -> dict[str, Any]:
        settings = self.settings_service.get_all() if self.settings_service is not None else {}
        probe_token = secrets.token_urlsafe(18)
        profile_dir = (
            self.data_root / "browser_profiles" / "self-test"
        ).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        download_dir = (
            self.data_root / "browser_artifacts" / "downloads"
        ).resolve()
        download_dir.mkdir(parents=True, exist_ok=True)
        upload_probe = (
            self.data_root / "browser_artifacts" / "browser-self-test-upload.txt"
        ).resolve()
        upload_probe.write_text("Project Q upload self-test", encoding="utf-8")
        job = {
            "mode": "self_test",
            "browser_type": "chromium",
            "headless": True,
            "channel": settings.get("browser_channel", "msedge"),
            "executable_path": settings.get("browser_executable_path", ""),
            "timeout_seconds": 30,
            "screenshot_path": self._build_screenshot_path(
                {"screenshot_name": "browser-self-test.png"}
            ),
            "user_data_dir": str(profile_dir),
            "profile_probe": probe_token,
            "download_dir": str(download_dir),
            "self_test_upload_path": str(upload_probe),
        }
        self._run_worker(job)
        result = self._run_worker(job)
        checks = dict(result.get("checks", {}))
        checks["persistence"] = (
            result.get("previous_profile_probe") == probe_token
        )
        result["checks"] = checks
        return result

    def _build_screenshot_path(self, payload: dict[str, Any]) -> str:
        artifacts = self.data_root / "browser_artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        filename = str(payload.get("screenshot_name", "page.png") or "page.png")
        if Path(filename).is_absolute() or Path(filename).name != filename:
            raise ValueError("screenshot_name must be a simple file name")
        target = (artifacts / filename).resolve()
        if not target.is_relative_to(artifacts.resolve()):
            raise ValueError("screenshot path is outside browser artifacts")
        return str(target)

    def _run_worker(self, job: dict[str, Any]) -> dict[str, Any]:
        if not self.node_path:
            raise RuntimeError("Node.js runtime not found for browser worker")
        if not self.node_modules_path:
            raise RuntimeError("Node modules path not found for browser worker")

        env = os.environ.copy()
        module_paths = [self.node_modules_path]
        pnpm_modules = Path(self.node_modules_path) / ".pnpm" / "node_modules"
        if pnpm_modules.is_dir():
            module_paths.append(str(pnpm_modules))
        existing_node_path = env.get("NODE_PATH", "")
        if existing_node_path:
            module_paths.extend(
                path
                for path in existing_node_path.split(os.pathsep)
                if path and path not in module_paths
            )
        env["NODE_PATH"] = os.pathsep.join(module_paths)

        completed = subprocess.run(
            [self.node_path, str(self.worker_script), json.dumps(job)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(int(job.get("timeout_seconds", 25)) + 10, 330),
            check=False,
            env=env,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip()
            if "Executable doesn't exist" in stderr or "Failed to launch" in stderr:
                stderr += (
                    " | Configure browser_channel or browser_executable_path in Settings "
                    "if Playwright cannot find a usable browser."
                )
            raise RuntimeError(f"Browser worker failed: {stderr}")
        return json.loads(completed.stdout)


class BrowserActionsTool(BrowserInspectTool):
    definition = ToolDefinition(
        tool_id="browser.run_actions",
        name="Run Browser Actions",
        description="Navigate and perform click/fill/extract automation with Playwright",
        tier=2,
    )

    def __init__(
        self,
        data_root: Path,
        settings_service=None,
        *,
        workspace_root: Path | None = None,
    ) -> None:
        super().__init__(data_root, settings_service)
        self.workspace_root = (workspace_root or data_root.parent).resolve()

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings_service.get_all() if self.settings_service is not None else {}
        actions = _validated_browser_actions(payload.get("actions", []))
        actions = self._resolve_upload_actions(actions)
        requested_timeout = int(payload.get("timeout_seconds", 30))
        if requested_timeout < 5 or requested_timeout > 300:
            raise ValueError("browser timeout_seconds must be between 5 and 300")
        longest_action_seconds = max(
            (
                int(action.get("timeout_ms", 0)) / 1000 + 5
                for action in actions
                if action["type"] in {"wait_for_timeout", "pause_for_owner"}
            ),
            default=0,
        )
        job = {
            "mode": "actions",
            "url": _validated_web_url(payload["url"]),
            "actions": actions,
            "browser_type": payload.get("browser_type", "chromium"),
            "headless": bool(payload.get("headless", settings.get("browser_headless", False))),
            "channel": payload.get("channel", settings.get("browser_channel", "msedge")),
            "executable_path": payload.get(
                "executable_path",
                settings.get("browser_executable_path", ""),
            ),
            "timeout_seconds": min(
                300,
                max(requested_timeout, int(longest_action_seconds)),
            ),
            "screenshot_path": self._build_screenshot_path(payload)
            if payload.get("screenshot")
            else "",
            "user_data_dir": self._build_profile_dir(payload),
            "download_dir": self._build_download_dir(),
        }
        with _BROWSER_PROFILE_LOCK:
            return self._run_worker(job)

    def _build_profile_dir(self, payload: dict[str, Any]) -> str:
        profile_name = str(payload.get("profile", "default") or "default").strip()
        if not _BROWSER_PROFILE_NAME.fullmatch(profile_name) or profile_name in {".", ".."}:
            raise ValueError(
                "browser profile must use 1-64 letters, numbers, dots, dashes, or underscores"
            )
        profiles_root = (self.data_root / "browser_profiles").resolve()
        profiles_root.mkdir(parents=True, exist_ok=True)
        target = (profiles_root / profile_name).resolve()
        if not target.is_relative_to(profiles_root):
            raise ValueError("browser profile path is outside browser profiles")
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    def _build_download_dir(self) -> str:
        downloads = (self.data_root / "browser_artifacts" / "downloads").resolve()
        downloads.mkdir(parents=True, exist_ok=True)
        return str(downloads)

    def _resolve_upload_actions(
        self,
        actions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        resolved_actions: list[dict[str, Any]] = []
        for action in actions:
            resolved_action = dict(action)
            if action["type"] == "upload":
                upload_path = resolve_allowed_path(
                    action.get("path", ""),
                    workspace_root=self.workspace_root,
                    data_root=self.data_root,
                    settings_service=self.settings_service,
                    must_exist=True,
                )
                if not upload_path.is_file():
                    raise ValueError("browser upload path must be a file")
                if upload_path.stat().st_size > 100 * 1024 * 1024:
                    raise ValueError("browser uploads are limited to 100 MiB")
                resolved_action["path"] = str(upload_path)
            resolved_actions.append(resolved_action)
        return resolved_actions


@dataclass(slots=True)
class BrowserGoalIntent:
    mode: str
    query: str
    normalized_instruction: str
    wants_high_quality: bool = False
    prefers_youtube: bool = False


class BrowserGoalTool:
    definition = ToolDefinition(
        tool_id="browser.complete_goal",
        name="Complete Browser Goal",
        description="Interpret a browser-related owner goal, search intelligently, and open the best visible result",
        tier=1,
    )

    COMMON_TYPO_CORRECTIONS = {
        "than": "then",
        "serach": "search",
        "googel": "google",
        "gooogle": "google",
        "yotube": "youtube",
        "youutbe": "youtube",
        "vidoes": "videos",
        "vedios": "videos",
        "imgaes": "images",
        "imges": "images",
        "turtoial": "tutorial",
        "toturial": "tutorial",
        "guied": "guide",
        "invinicible": "invincible",
    }

    def __init__(
        self,
        data_root: Path,
        settings_service=None,
        *,
        open_url_tool=None,
        inspect_tool=None,
    ) -> None:
        self.data_root = data_root
        self.settings_service = settings_service
        self.open_url_tool = open_url_tool or WindowsOpenUrlTool()
        self.inspect_tool = inspect_tool or BrowserInspectTool(data_root, settings_service)

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        instruction = str(payload.get("instruction") or payload.get("goal") or "").strip()
        if not instruction:
            raise ValueError("instruction is required")

        intent = self._parse_intent(instruction)
        if intent.mode == "image_search":
            final_url = self._google_images_url(intent.query, large=intent.wants_high_quality)
            opened = self.open_url_tool.execute({"url": final_url})
            return {
                "instruction": instruction,
                "intent": asdict(intent),
                "opened_url": final_url,
                "selected_result_url": "",
                "search_url": final_url,
                "opened": opened.get("opened", False),
            }

        if intent.mode in {"tutorial_video", "youtube_video"}:
            search_query = intent.query
            if intent.mode == "tutorial_video" and "tutorial" not in search_query.lower() and "guide" not in search_query.lower():
                search_query = f"{search_query} tutorial".strip()
            search_url = self._youtube_search_url(search_query)
            selected_url, inspected = self._select_best_video_result(search_url, prefer_tutorial=intent.mode == "tutorial_video")
            final_url = self._with_youtube_autoplay(selected_url or search_url)
            opened = self.open_url_tool.execute({"url": final_url})
            return {
                "instruction": instruction,
                "intent": asdict(intent),
                "opened_url": final_url,
                "selected_result_url": selected_url or "",
                "search_url": search_url,
                "inspection_url": inspected.get("url", "") if inspected else "",
                "opened": opened.get("opened", False),
            }

        if intent.mode == "video_search":
            final_url = self._google_video_search_url(intent.query)
            opened = self.open_url_tool.execute({"url": final_url})
            return {
                "instruction": instruction,
                "intent": asdict(intent),
                "opened_url": final_url,
                "selected_result_url": "",
                "search_url": final_url,
                "opened": opened.get("opened", False),
            }

        final_url = self._google_search_url(intent.query)
        opened = self.open_url_tool.execute({"url": final_url})
        return {
            "instruction": instruction,
            "intent": asdict(intent),
            "opened_url": final_url,
            "selected_result_url": "",
            "search_url": final_url,
            "opened": opened.get("opened", False),
        }

    def _parse_intent(self, instruction: str) -> BrowserGoalIntent:
        normalized = self._normalize_instruction_text(instruction)
        lowered = normalized.lower()
        wants_high_quality = any(phrase in lowered for phrase in ("high quality", "high-quality", "hd", "4k"))
        prefers_youtube = "youtube" in lowered or "tutorial" in lowered or "guide" in lowered

        if "image" in lowered or "images" in lowered or "picture" in lowered or "photo" in lowered:
            mode = "image_search"
        elif "tutorial" in lowered or "guide" in lowered:
            mode = "tutorial_video"
        elif "youtube" in lowered or "watch" in lowered or "play" in lowered or "press a youtube link" in lowered:
            mode = "youtube_video"
        elif "video" in lowered or "videos" in lowered:
            mode = "video_search"
        else:
            mode = "web_search"

        query = self._extract_query(normalized, mode=mode)
        return BrowserGoalIntent(
            mode=mode,
            query=query,
            normalized_instruction=normalized,
            wants_high_quality=wants_high_quality,
            prefers_youtube=prefers_youtube,
        )

    def _normalize_instruction_text(self, instruction: str) -> str:
        normalized = re.sub(r"\s+", " ", instruction.strip())
        normalized = normalized.replace("’", "'").replace("`", "'")
        for typo, correction in self.COMMON_TYPO_CORRECTIONS.items():
            normalized = re.sub(rf"\b{re.escape(typo)}\b", correction, normalized, flags=re.IGNORECASE)
        return normalized

    def _extract_query(self, instruction: str, *, mode: str) -> str:
        lowered = instruction.lower()
        query = instruction

        search_match = re.search(r"search(?:\s+(?:google|youtube))?(?:\s+for)?\s+(.+)$", instruction, flags=re.IGNORECASE)
        find_match = re.search(r"find me\s+(.+)$", instruction, flags=re.IGNORECASE)
        if search_match:
            query = search_match.group(1)
        elif find_match:
            query = find_match.group(1)

        query = re.split(r"\b(?:then|after that|and then)\b", query, maxsplit=1, flags=re.IGNORECASE)[0]
        query = re.split(r"\bgo to\b", query, maxsplit=1, flags=re.IGNORECASE)[0]
        query = re.split(r"\b(?:select|click|press|open|play)\b", query, maxsplit=1, flags=re.IGNORECASE)[0]
        query = query.strip(" ,.")

        query = re.sub(
            r"^(?:a|an|the)?\s*(?:very\s+good\s+)?(?:high\s+quality\s+)?(?:tutorial|guide|video|videos|image|images|photo|photos|picture|pictures)\s*(?:on|for|of)?\s*",
            "",
            query,
            flags=re.IGNORECASE,
        )

        if mode == "image_search":
            query = re.sub(r"\b(?:find|show)\s+(?:a|an|the)?\s*(?:very\s+good\s+)?(?:high\s+quality\s+)?(?:image|images|photo|picture)\b.*$", "", query, flags=re.IGNORECASE).strip(" ,.")

        return query.strip() or lowered.strip()

    def _select_best_video_result(self, search_url: str, *, prefer_tutorial: bool) -> tuple[str | None, dict[str, Any] | None]:
        try:
            inspected = self.inspect_tool.execute({"url": search_url, "include_links": True, "timeout_seconds": 25})
        except Exception:
            return None, None

        candidates: list[tuple[int, str]] = []
        seen: set[str] = set()
        for link in inspected.get("links", []):
            href = str(link.get("href", "")).strip()
            text = str(link.get("text", "")).strip().lower()
            canonical = self._canonical_youtube_watch_url(href)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            score = 0
            if prefer_tutorial and any(token in text for token in ("tutorial", "guide", "how to", "setup", "set up")):
                score += 5
            if "shorts" in href:
                score -= 4
            if any(token in text for token in ("full", "step by step", "beginner", "walkthrough")):
                score += 2
            candidates.append((score, canonical))

        if not candidates:
            return None, inspected

        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1], inspected

    def _canonical_youtube_watch_url(self, href: str) -> str | None:
        if "youtube.com/watch" in href:
            parsed = urlparse(href)
            video_id = parse_qs(parsed.query).get("v", [""])[0]
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"
        if "youtu.be/" in href:
            parsed = urlparse(href)
            video_id = parsed.path.strip("/").split("/", 1)[0]
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"
        return None

    def _with_youtube_autoplay(self, url: str) -> str:
        if "youtube.com/watch" not in url:
            return url
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        query["autoplay"] = ["1"]
        serialized = "&".join(f"{key}={quote(value[0])}" for key, value in query.items() if value)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", serialized, ""))

    def _google_search_url(self, query: str) -> str:
        return "https://www.google.com/search?q=" + quote(query.strip() or "Google")

    def _google_images_url(self, query: str, *, large: bool) -> str:
        tbs = "&tbs=isz:l" if large else ""
        return "https://www.google.com/search?tbm=isch" + tbs + "&q=" + quote(query.strip() or "images")

    def _google_video_search_url(self, query: str) -> str:
        return "https://www.google.com/search?tbm=vid&q=" + quote(query.strip() or "videos")

    def _youtube_search_url(self, query: str) -> str:
        return "https://www.youtube.com/results?search_query=" + quote(query.strip() or "tutorial")
