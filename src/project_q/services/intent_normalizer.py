from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IntentNormalization:
    original: str
    normalized: str
    corrections: list[dict[str, str]]

    @property
    def changed(self) -> bool:
        return self.original != self.normalized


class IntentNormalizer:
    """Small deterministic repair layer for typo-heavy owner commands.

    This does not replace the LLM. It gives the local router a cleaner intent
    surface so obvious misspellings do not block tools from running.
    """

    _WORD_REPLACEMENTS = {
        "adn": "and",
        "aefificent": "efficient",
        "adeuqtely": "adequately",
        "analzying": "analyzing",
        "aplication": "application",
        "applicaiton": "application",
        "aritcle": "article",
        "automaiton": "automation",
        "auitomatically": "automatically",
        "avergae": "average",
        "bokking": "booking",
        "buid": "build",
        "builld": "build",
        "commnets": "comments",
        "contct": "contact",
        "creat": "create",
        "cusotmize": "customize",
        "dsiabled": "disabled",
        "eveyrthing": "everything",
        "extreemly": "extremely",
        "extermely": "extremely",
        "frist": "first",
        "gallerie": "gallery",
        "goolge": "google",
        "grammer": "grammar",
        "impolemented": "implemented",
        "imporvements": "improvements",
        "imrpove": "improve",
        "intellgent": "intelligent",
        "intellgient": "intelligent",
        "intelligfent": "intelligent",
        "invinicible": "invincible",
        "knwoledge": "knowledge",
        "ladning": "landing",
        "liek": "like",
        "mkae": "make",
        "modfy": "modify",
        "pricng": "pricing",
        "pyhton": "python",
        "resuem": "resume",
        "scipt": "script",
        "scritp": "script",
        "seach": "search",
        "serach": "search",
        "shpo": "shop",
        "smethihng": "something",
        "sumor": "sum or",
        "teh": "the",
        "ther": "the",
        "trhough": "through",
        "turtoial": "tutorial",
        "webiste": "website",
        "wbesite": "website",
        "wth": "with",
        "yurself": "yourself",
    }

    _PHRASE_REPLACEMENTS = {
        "googel search": "google search",
        "google serach": "google search",
        "local llm": "local LLM",
        "lora job": "LoRA job",
        "model weight": "model weight",
        "model weights": "model weights",
        "todo comment": "TODO comment",
        "todo comments": "TODO comments",
    }

    def normalize(self, text: str) -> IntentNormalization:
        normalized = text
        corrections: list[dict[str, str]] = []

        for source, target in self._WORD_REPLACEMENTS.items():
            pattern = re.compile(rf"\b{re.escape(source)}\b", flags=re.IGNORECASE)

            def replace(match: re.Match[str], *, source: str = source, target: str = target) -> str:
                corrections.append({"from": match.group(0), "to": target})
                return target

            normalized = pattern.sub(replace, normalized)

        for source, target in self._PHRASE_REPLACEMENTS.items():
            pattern = re.compile(re.escape(source), flags=re.IGNORECASE)
            if pattern.search(normalized):
                corrections.append({"from": source, "to": target})
                normalized = pattern.sub(target, normalized)

        normalized = re.sub(r"\s+", " ", normalized).strip()
        return IntentNormalization(original=text, normalized=normalized, corrections=corrections)
