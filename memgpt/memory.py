"""Memory tiers of MemGPT.

* CoreMemory     -> "working context" in the paper (§2.1): fixed-size, read/write
                    only through functions, always inside the prompt.
* RecallStore    -> "recall storage" (§2.2): every message, written automatically
                    by the queue manager, read through conversation_search.
* ArchivalStore  -> "archival storage" (§2.1, §3.2): arbitrary-length text written
                    and read by the LLM through functions.

Search uses BM25 (pure Python) so the demo has no vector-DB dependency; the
paper used PostgreSQL + pgvector for archival storage (§3.2.1).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PERSONA = (
    "Tôi là Mem, trợ lý học tập thân thiện. Tôi trò chuyện bằng tiếng Việt, "
    "ngắn gọn, ấm áp, và luôn cố nhớ những điều quan trọng về người dùng."
)
DEFAULT_HUMAN = "Chưa biết gì về người dùng."


# --------------------------------------------------------------------------- #
# Text utilities
# --------------------------------------------------------------------------- #
_WORD = re.compile(r"\w+", re.UNICODE)


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text or "")


def tokenize(text: str) -> list[str]:
    return [w for w in _WORD.findall(normalize(text).lower()) if len(w) > 1 or w.isdigit()]


def bm25_rank(query: str, docs: list[str], k1: float = 1.4, b: float = 0.75) -> list[tuple[int, float]]:
    """Return (index, score) pairs with score > 0, best first."""
    q_terms = tokenize(query)
    if not q_terms or not docs:
        return []
    tokenized = [tokenize(d) for d in docs]
    n = len(tokenized)
    avgdl = sum(len(t) for t in tokenized) / max(n, 1)
    df: Counter = Counter()
    for toks in tokenized:
        df.update(set(toks))
    scores: list[tuple[int, float]] = []
    for i, toks in enumerate(tokenized):
        if not toks:
            continue
        tf = Counter(toks)
        dl = len(toks)
        s = 0.0
        for term in q_terms:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * dl / avgdl))
        if s > 0:
            scores.append((i, s))
    scores.sort(key=lambda x: -x[1])
    return scores


# --------------------------------------------------------------------------- #
# Core memory (= working context)
# --------------------------------------------------------------------------- #
class CoreMemoryError(Exception):
    pass


class CoreMemory:
    def __init__(self, path: Path, persona_limit: int, human_limit: int):
        self.path = path
        self.limits = {"persona": persona_limit, "human": human_limit}
        self.blocks: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.blocks = json.loads(self.path.read_text(encoding="utf-8"))
                return
            except Exception:
                pass
        self.reset()

    def reset(self) -> None:
        self.blocks = {"persona": DEFAULT_PERSONA, "human": DEFAULT_HUMAN}
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.blocks, ensure_ascii=False, indent=2), encoding="utf-8")

    def _check(self, label: str, value: str) -> None:
        if label not in self.limits:
            raise CoreMemoryError(f"Block '{label}' không tồn tại. Chỉ có: persona, human.")
        if len(value) > self.limits[label]:
            raise CoreMemoryError(
                f"Edit failed: exceeds {self.limits[label]} character limit "
                f"(requested {len(value)}). Consider summarizing or moving content to archival memory."
            )

    def append(self, label: str, content: str) -> tuple[str, str]:
        before = self.blocks.get(label, "")
        base = "" if before.strip() == DEFAULT_HUMAN else before
        after = (base.rstrip() + "\n" + content.strip()).strip()
        self._check(label, after)
        self.blocks[label] = after
        self.save()
        return before, after

    def replace(self, label: str, old: str, new: str) -> tuple[str, str]:
        before = self.blocks.get(label, "")
        if label not in self.limits:
            raise CoreMemoryError(f"Block '{label}' không tồn tại. Chỉ có: persona, human.")
        if not old or old not in before:
            raise CoreMemoryError(f"Old content '{old}' not found in {label} block.")
        after = before.replace(old, new).strip()
        self._check(label, after)
        self.blocks[label] = after
        self.save()
        return before, after

    def render(self) -> str:
        parts = ["### Core memory (working context) — always visible, edit only via functions"]
        for label in ("persona", "human"):
            value = self.blocks.get(label, "")
            parts.append(f'<{label} characters="{len(value)}/{self.limits[label]}">\n{value}\n</{label}>')
        return "\n".join(parts)

    def snapshot(self) -> dict:
        return {k: {"value": self.blocks.get(k, ""), "limit": v} for k, v in self.limits.items()}


# --------------------------------------------------------------------------- #
# SQLite-backed stores
# --------------------------------------------------------------------------- #
@dataclass
class SearchPage:
    results: list[dict]
    total: int
    page: int
    pages: int


class _Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row


class RecallStore(_Store):
    """Every message ever seen (§2.2: 'stored indefinitely in recall storage')."""

    SEARCHABLE = ("user", "assistant_message")

    def __init__(self, path: Path):
        super().__init__(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            " id INTEGER PRIMARY KEY, msg_id INTEGER, ts TEXT, kind TEXT, text TEXT)"
        )
        self.db.commit()

    def insert(self, msg_id: int, ts: str, kind: str, text: str) -> None:
        self.db.execute(
            "INSERT INTO messages(msg_id, ts, kind, text) VALUES (?,?,?,?)", (msg_id, ts, kind, text)
        )
        self.db.commit()

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def recent(self, n: int = 4) -> list[dict]:
        rows = self.db.execute(
            "SELECT msg_id, ts, kind, text FROM messages ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in rows]

    def search(self, query: str, page: int, page_size: int, exclude: set[int] | None = None) -> SearchPage:
        rows = [
            dict(r)
            for r in self.db.execute(
                "SELECT msg_id, ts, kind, text FROM messages WHERE kind IN (?,?) ORDER BY id",
                self.SEARCHABLE,
            ).fetchall()
            if not exclude or r["msg_id"] not in exclude
        ]
        ranked = bm25_rank(query, [r["text"] for r in rows])
        hits = [rows[i] for i, _ in ranked]
        return _paginate(hits, page, page_size)

    def clear(self) -> None:
        self.db.execute("DELETE FROM messages")
        self.db.commit()


class ArchivalStore(_Store):
    def __init__(self, path: Path):
        super().__init__(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS passages ("
            " id INTEGER PRIMARY KEY, ts TEXT, source TEXT, text TEXT)"
        )
        self.db.commit()

    def insert(self, ts: str, source: str, text: str) -> int:
        cur = self.db.execute("INSERT INTO passages(ts, source, text) VALUES (?,?,?)", (ts, source, text))
        self.db.commit()
        return cur.lastrowid

    def insert_many(self, rows: list[tuple[str, str, str]]) -> None:
        self.db.executemany("INSERT INTO passages(ts, source, text) VALUES (?,?,?)", rows)
        self.db.commit()

    def count(self, source_prefix: str | None = None) -> int:
        if source_prefix is None:
            return self.db.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
        return self.db.execute(
            "SELECT COUNT(*) FROM passages WHERE source LIKE ?", (source_prefix + "%",)
        ).fetchone()[0]

    def recent_agent(self, n: int = 3) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, ts, source, text FROM passages WHERE source='agent' ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in rows]

    def search(self, query: str, page: int, page_size: int) -> SearchPage:
        rows = [dict(r) for r in self.db.execute("SELECT id, ts, source, text FROM passages").fetchall()]
        ranked = bm25_rank(query, [r["text"] for r in rows])
        return _paginate([rows[i] for i, _ in ranked], page, page_size)

    def clear_agent(self) -> None:
        self.db.execute("DELETE FROM passages WHERE source='agent'")
        self.db.commit()


def _paginate(hits: list[dict], page: int, page_size: int) -> SearchPage:
    total = len(hits)
    pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, pages - 1))
    return SearchPage(hits[page * page_size:(page + 1) * page_size], total, page, pages)
