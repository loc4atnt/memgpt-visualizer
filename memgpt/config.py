"""Runtime configuration, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader (KEY=VALUE per line) so no extra dependency is needed."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0]  # allow trailing comments
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    # --- Virtual context (MemGPT §2.2) -------------------------------------
    context_limit: int = field(default_factory=lambda: _i("CONTEXT_LIMIT", 4000))
    warn_frac: float = field(default_factory=lambda: _f("WARN_FRAC", 0.70))
    flush_frac: float = field(default_factory=lambda: _f("FLUSH_FRAC", 1.00))
    evict_frac: float = field(default_factory=lambda: _f("EVICT_FRAC", 0.50))
    keep_tail: int = 3  # never evict the newest N messages (keeps a tool call + result pair intact)

    # --- Working context / core memory -------------------------------------
    persona_limit: int = field(default_factory=lambda: _i("PERSONA_LIMIT", 600))
    human_limit: int = field(default_factory=lambda: _i("HUMAN_LIMIT", 800))

    # --- Retrieval -----------------------------------------------------------
    page_size: int = field(default_factory=lambda: _i("PAGE_SIZE", 5))
    max_chain: int = field(default_factory=lambda: _i("MAX_CHAIN", 6))

    # --- LLM (any OpenAI-compatible endpoint) ------------------------------
    api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.environ.get("OPENAI_BASE_URL", ""))
    model: str = field(default_factory=lambda: os.environ.get("MODEL", "gpt-4o-mini"))
    temperature: float = field(default_factory=lambda: _f("TEMPERATURE", 0.0))
    scripted_delay: float = field(default_factory=lambda: _f("SCRIPTED_DELAY", 0.7))

    # --- Files ---------------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", ROOT / "data")))
    runs_dir: Path = field(default_factory=lambda: Path(os.environ.get("RUNS_DIR", ROOT / "runs")))
    papers_dir: Path = field(default_factory=lambda: Path(os.environ.get("PAPERS_DIR", ROOT.parent)))

    @property
    def warn_tokens(self) -> int:
        return int(self.warn_frac * self.context_limit)

    @property
    def flush_tokens(self) -> int:
        return int(self.flush_frac * self.context_limit)
