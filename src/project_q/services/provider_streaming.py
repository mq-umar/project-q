from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any
from urllib import error, request


MAX_PROVIDER_DOCUMENT_CHARS = 2 * 1024 * 1024


class ProviderStreamError(RuntimeError):
    pass


class IncrementalJsonReplyExtractor:
    def __init__(self, *, max_document_chars: int = MAX_PROVIDER_DOCUMENT_CHARS) -> None:
        self.max_document_chars = max_document_chars
        self.total_chars = 0
        self.done = False
        self._stack: list[str] = []
        self._top_state = "start"
        self._current_key = ""
        self._in_string = False
        self._string_role = ""
        self._string_raw: list[str] = []
        self._escape = False
        self._unicode_digits = ""
        self._pending_high_surrogate: int | None = None

    def feed(self, chunk: str) -> list[str]:
        if not isinstance(chunk, str):
            raise TypeError("provider text chunks must be strings")
        self.total_chars += len(chunk)
        if self.total_chars > self.max_document_chars:
            raise ProviderStreamError("provider stream exceeded the maximum structured output size")

        emitted: list[str] = []
        for character in chunk:
            if self.done:
                break
            if self._in_string:
                self._consume_string_character(character, emitted)
            else:
                self._consume_structural_character(character)
        return emitted

    def _consume_structural_character(self, character: str) -> None:
        if not self._stack:
            if character.isspace():
                return
            if character != "{":
                return
            self._stack.append("{")
            self._top_state = "key_or_end"
            return

        if len(self._stack) == 1 and self._stack[0] == "{":
            if self._top_state == "key_or_end":
                if character.isspace() or character == ",":
                    return
                if character == "}":
                    self._stack.pop()
                    return
                if character == '"':
                    self._start_string("key")
                    return
            elif self._top_state == "colon":
                if character.isspace():
                    return
                if character == ":":
                    self._top_state = "value"
                return
            elif self._top_state == "value":
                if character.isspace():
                    return
                if character == '"':
                    role = "reply_value" if self._current_key == "reply" else "other_value"
                    self._start_string(role)
                    return
                if character in "[{":
                    self._stack.append(character)
                    self._top_state = "nested_value"
                    return
                if character in ",}":
                    self._finish_top_value(character)
                    return
                self._top_state = "primitive_value"
                return
            elif self._top_state == "primitive_value":
                if character in ",}":
                    self._finish_top_value(character)
                return
            elif self._top_state == "comma_or_end":
                if character.isspace():
                    return
                if character == ",":
                    self._top_state = "key_or_end"
                    self._current_key = ""
                elif character == "}":
                    self._stack.pop()
                return

        if character == '"':
            self._start_string("nested")
            return
        if character in "[{":
            self._stack.append(character)
            return
        if character in "]}":
            if self._stack:
                self._stack.pop()
            if len(self._stack) == 1 and self._top_state == "nested_value":
                self._top_state = "comma_or_end"

    def _consume_string_character(self, character: str, emitted: list[str]) -> None:
        if self._string_role == "reply_value":
            self._consume_reply_character(character, emitted)
            return

        if self._escape:
            self._string_raw.append(character)
            self._escape = False
            return
        if character == "\\":
            self._string_raw.append(character)
            self._escape = True
            return
        if character == '"':
            raw = "".join(self._string_raw)
            role = self._string_role
            self._in_string = False
            self._string_role = ""
            self._string_raw = []
            if role == "key":
                try:
                    self._current_key = json.loads(f'"{raw}"')
                except json.JSONDecodeError:
                    self._current_key = ""
                self._top_state = "colon"
            elif role == "other_value":
                self._top_state = "comma_or_end"
            return
        self._string_raw.append(character)

    def _consume_reply_character(self, character: str, emitted: list[str]) -> None:
        if self._unicode_digits:
            if character.lower() not in "0123456789abcdef":
                raise ProviderStreamError("provider reply contained an invalid Unicode escape")
            self._unicode_digits += character
            if len(self._unicode_digits) == 5:
                code_point = int(self._unicode_digits[1:], 16)
                self._unicode_digits = ""
                self._emit_code_point(code_point, emitted)
            return

        if self._escape:
            self._escape = False
            if character == "u":
                self._unicode_digits = "u"
                return
            escapes = {
                '"': '"',
                "\\": "\\",
                "/": "/",
                "b": "\b",
                "f": "\f",
                "n": "\n",
                "r": "\r",
                "t": "\t",
            }
            if character not in escapes:
                raise ProviderStreamError("provider reply contained an invalid JSON escape")
            self._flush_pending_high_surrogate(emitted)
            self._emit_text(escapes[character], emitted)
            return

        if character == "\\":
            self._escape = True
            return
        if character == '"':
            if self._pending_high_surrogate is not None:
                self._emit_text("\ufffd", emitted)
                self._pending_high_surrogate = None
            self._in_string = False
            self._string_role = ""
            self._top_state = "comma_or_end"
            self.done = True
            return
        self._flush_pending_high_surrogate(emitted)
        self._emit_text(character, emitted)

    def _emit_code_point(self, code_point: int, emitted: list[str]) -> None:
        if 0xD800 <= code_point <= 0xDBFF:
            if self._pending_high_surrogate is not None:
                self._emit_text("\ufffd", emitted)
            self._pending_high_surrogate = code_point
            return
        if 0xDC00 <= code_point <= 0xDFFF and self._pending_high_surrogate is not None:
            high = self._pending_high_surrogate
            self._pending_high_surrogate = None
            combined = 0x10000 + ((high - 0xD800) << 10) + (code_point - 0xDC00)
            self._emit_text(chr(combined), emitted)
            return
        if self._pending_high_surrogate is not None:
            self._emit_text("\ufffd", emitted)
            self._pending_high_surrogate = None
        if 0xDC00 <= code_point <= 0xDFFF:
            self._emit_text("\ufffd", emitted)
        else:
            self._emit_text(chr(code_point), emitted)

    @staticmethod
    def _emit_text(text: str, emitted: list[str]) -> None:
        if text:
            emitted.append(text)

    def _flush_pending_high_surrogate(self, emitted: list[str]) -> None:
        if self._pending_high_surrogate is not None:
            self._emit_text("\ufffd", emitted)
            self._pending_high_surrogate = None

    def _start_string(self, role: str) -> None:
        self._in_string = True
        self._string_role = role
        self._string_raw = []
        self._escape = False
        self._unicode_digits = ""

    def _finish_top_value(self, character: str) -> None:
        if character == ",":
            self._top_state = "key_or_end"
            self._current_key = ""
        elif character == "}":
            self._stack.pop()


def default_stream_transport(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout_seconds: int,
) -> Iterator[str]:
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        response = request.urlopen(req, timeout=timeout_seconds)
    except error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise ProviderStreamError(f"Provider stream request failed: {exc.code} {details}") from exc
    except error.URLError as exc:
        raise ProviderStreamError(f"Provider stream request failed: {exc.reason}") from exc

    try:
        with response:
            for raw_line in response:
                yield raw_line.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProviderStreamError("Provider stream returned invalid UTF-8") from exc


def iter_provider_text(provider_type: str, lines: Iterable[str | bytes]) -> Iterator[str]:
    if provider_type == "ollama":
        yield from _iter_ollama_text(lines)
        return
    if provider_type in {"openai_responses", "anthropic_messages"}:
        yield from _iter_sse_text(provider_type, lines)
        return
    raise ValueError(f"unsupported streaming provider type: {provider_type}")


def _iter_ollama_text(lines: Iterable[str | bytes]) -> Iterator[str]:
    for raw_line in lines:
        line = _decode_line(raw_line).strip()
        if not line:
            continue
        payload = _load_event_json(line)
        if payload.get("error"):
            raise ProviderStreamError(str(payload["error"]))
        message = payload.get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content:
            yield content


def _iter_sse_text(provider_type: str, lines: Iterable[str | bytes]) -> Iterator[str]:
    data_lines: list[str] = []
    for raw_line in lines:
        line = _decode_line(raw_line).rstrip("\r\n")
        if line:
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            continue
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        data_lines = []
        if data == "[DONE]":
            continue
        payload = _load_event_json(data)
        yield from _text_from_sse_payload(provider_type, payload)

    if data_lines:
        payload = _load_event_json("\n".join(data_lines))
        yield from _text_from_sse_payload(provider_type, payload)


def _text_from_sse_payload(provider_type: str, payload: dict[str, Any]) -> Iterator[str]:
    event_type = str(payload.get("type", ""))
    if event_type in {"error", "response.failed", "response.incomplete"}:
        error_payload = payload.get("error") or payload.get("response", {}).get("error") or payload
        if isinstance(error_payload, dict):
            message = error_payload.get("message") or error_payload.get("type") or event_type
        else:
            message = str(error_payload)
        raise ProviderStreamError(str(message))

    if provider_type == "openai_responses":
        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                yield delta
        return

    if event_type == "content_block_delta":
        delta = payload.get("delta") or {}
        if delta.get("type") == "text_delta":
            text = delta.get("text")
            if isinstance(text, str) and text:
                yield text


def _load_event_json(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ProviderStreamError("Provider stream returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderStreamError("Provider stream event must be a JSON object")
    return payload


def _decode_line(value: str | bytes) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderStreamError("Provider stream returned invalid UTF-8") from exc
    if isinstance(value, str):
        return value
    raise TypeError("provider stream lines must be strings or bytes")
