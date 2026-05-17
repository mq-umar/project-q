from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse, urlunparse

from project_q.runtime import discover_node_executable, discover_node_modules_path
from project_q.tools.base import ToolDefinition
from project_q.tools.windows import WindowsOpenUrlTool


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
                "url": payload["url"],
                "browser_type": payload.get("browser_type", "chromium"),
                "headless": bool(payload.get("headless", settings.get("browser_headless", True))),
                "channel": payload.get("channel", settings.get("browser_channel", "msedge")),
                "executable_path": payload.get("executable_path", settings.get("browser_executable_path", "")),
                "timeout_seconds": int(payload.get("timeout_seconds", 25)),
                "include_links": bool(payload.get("include_links", False)),
                "screenshot_path": self._build_screenshot_path(payload) if payload.get("screenshot") else "",
            }
        )

    def _build_screenshot_path(self, payload: dict[str, Any]) -> str:
        artifacts = self.data_root / "browser_artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        filename = payload.get("screenshot_name", "page.png")
        return str((artifacts / filename).resolve())

    def _run_worker(self, job: dict[str, Any]) -> dict[str, Any]:
        if not self.node_path:
            raise RuntimeError("Node.js runtime not found for browser worker")
        if not self.node_modules_path:
            raise RuntimeError("Node modules path not found for browser worker")

        env = os.environ.copy()
        env["NODE_PATH"] = self.node_modules_path

        completed = subprocess.run(
            [self.node_path, str(self.worker_script), json.dumps(job)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(int(job.get("timeout_seconds", 25)) + 10, 60),
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

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings_service.get_all() if self.settings_service is not None else {}
        return self._run_worker(
            {
                "mode": "actions",
                "url": payload["url"],
                "actions": payload.get("actions", []),
                "browser_type": payload.get("browser_type", "chromium"),
                "headless": bool(payload.get("headless", settings.get("browser_headless", True))),
                "channel": payload.get("channel", settings.get("browser_channel", "msedge")),
                "executable_path": payload.get("executable_path", settings.get("browser_executable_path", "")),
                "timeout_seconds": int(payload.get("timeout_seconds", 30)),
                "screenshot_path": self._build_screenshot_path(payload) if payload.get("screenshot") else "",
            }
        )


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
