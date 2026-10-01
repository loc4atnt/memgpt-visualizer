"""Token counting. Uses tiktoken (cl100k_base) when available, otherwise a
character-based estimate so the demo still runs fully offline."""
from __future__ import annotations

_encoder = None
try:  # tiktoken may need to download its BPE file on first use
    import tiktoken

    _encoder = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover - offline fallback
    _encoder = None

TOKENIZER_NAME = "tiktoken cl100k_base" if _encoder else "ước lượng ký tự/3"


def count_tokens(text: str) -> int:
    if not text:
        return 0
    if _encoder is not None:
        return len(_encoder.encode(text, disallowed_special=()))
    return max(1, len(text) // 3)
