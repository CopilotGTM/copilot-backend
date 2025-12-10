from __future__ import annotations


class JsonExtractor:
    """Utility to robustly extract a JSON object substring from text."""

    def extract(self, text: str) -> str | None:
        if not text:
            return None

        json_str = self._extract_from_sentinel(text)
        if json_str:
            return json_str

        json_str = self._extract_from_fence(text)
        if json_str:
            return json_str

        return self._extract_from_brackets(text)

    def _extract_from_sentinel(self, text: str) -> str | None:
        """Extract JSON from sentinel block ⟦AEP:1⟧ ... ⟦/AEP⟧."""
        sentinel_start = "⟦AEP:1⟧"
        sentinel_end = "⟦/AEP⟧"

        if sentinel_start not in text or sentinel_end not in text:
            return None

        try:
            segment = text.split(sentinel_start, 1)[1].split(sentinel_end, 1)[0]

            if "```" in segment:
                return self._extract_from_fence(segment)

            return self._extract_from_brackets(segment)
        except Exception:
            return None

    def _extract_from_fence(self, text: str) -> str | None:
        """Extract JSON from fenced code block."""
        if "```" not in text:
            return None

        parts = text.split("```")
        if len(parts) < 3:
            return None

        candidate = parts[1]
        return self._clean_language_hint(candidate)

    def _clean_language_hint(self, content: str) -> str:
        """Remove language hint from fenced content if present."""
        if "\n" not in content:
            return content.strip()

        first_line, rest = content.split("\n", 1)
        language_hints = {"json", "javascript", "ts", "yaml"}

        if first_line.strip().lower() in language_hints:
            return rest.strip()

        return content.strip()

    def _extract_from_brackets(self, text: str) -> str | None:
        """Extract JSON using bracket matching."""
        start = text.find("{")
        if start == -1:
            return None

        return self._match_json_brackets(text, start)

    def _match_json_brackets(self, text: str, start: int) -> str | None:
        """Match JSON brackets starting from given position."""
        depth = 0
        state = {"in_str": False, "escaped": False}

        for i, ch in enumerate(text[start:], start=start):
            if self._process_character(ch, state):
                continue

            if ch == "{":
                depth += 1
            elif ch == "}" and depth > 0:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]

        return None

    def _process_character(self, ch: str, state: dict) -> bool:
        """Process character for string/escape state. Returns True if handled."""
        if state["in_str"]:
            if state["escaped"]:
                state["escaped"] = False
                return True
            if ch == "\\":
                state["escaped"] = True
                return True
            if ch == '"':
                state["in_str"] = False
                return True
            return True

        if ch == '"':
            state["in_str"] = True
            return True

        return False
