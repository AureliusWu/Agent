from __future__ import annotations


class StreamingSentenceSplitter:
    def __init__(self, max_chars: int = 160) -> None:
        self.max_chars = max(20, max_chars)
        self.buffer = ""
        self.in_code_fence = False
        self._scan_index = 0

    def feed(self, chunk: str) -> list[str]:
        self.buffer += chunk
        sentences: list[str] = []
        cursor = 0
        index = self._scan_index
        while index < len(self.buffer):
            if self.buffer.startswith("```", index) or self.buffer.startswith("~~~", index):
                self.in_code_fence = not self.in_code_fence
                index += 3
                continue
            if not self.in_code_fence:
                current = self.buffer[index]
                boundary = current in "。！？；\n" or (current == "." and _english_period(self.buffer, index))
                # A provider limit is a hard safety boundary, not a hint.
                # Long model output can legitimately have no punctuation;
                # emit a bounded chunk instead of sending an oversized tail
                # that Windows SAPI will reject.
                too_long = index - cursor + 1 >= self.max_chars
                if boundary or too_long:
                    candidate = self.buffer[cursor : index + 1].strip()
                    if candidate:
                        sentences.append(candidate)
                    cursor = index + 1
            index += 1
        self.buffer = self.buffer[cursor:]
        self._scan_index = max(0, index - cursor)
        return sentences

    def flush(self) -> list[str]:
        remaining = self.buffer.strip()
        self.buffer = ""
        self._scan_index = 0
        return [remaining] if remaining and not self.in_code_fence else []


def _english_period(text: str, index: int) -> bool:
    if index + 1 < len(text) and not text[index + 1].isspace():
        return False
    prefix = text[max(0, index - 16) : index]
    return "://" not in prefix and not prefix.endswith(("v", "V"))
