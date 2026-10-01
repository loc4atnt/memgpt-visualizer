"""Load the group's research papers (PDF) into archival storage as passages,
mirroring the document-QA setup of MemGPT §3.2 (documents live in archival
storage and are paged in through archival_memory_search)."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .memory import ArchivalStore


def _chunks(text: str, size: int = 900) -> list[str]:
    text = re.sub(r"-\n(?=\w)", "", text)  # de-hyphenate line breaks
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out, cur = [], ""
    for s in sentences:
        if len(cur) + len(s) > size and cur:
            out.append(cur.strip())
            cur = ""
        cur += s + " "
    if cur.strip():
        out.append(cur.strip())
    return [c for c in out if len(c) > 80]


def ingest_papers(store: ArchivalStore, folder: Path, ts: str) -> tuple[int, list[str]]:
    if store.count("paper:") > 0:
        return 0, []
    try:
        from pypdf import PdfReader
    except ImportError:
        return 0, []
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # silence font-parsing warnings
    if any(folder.glob("*.pdf")):
        print(f"  archival: đang nạp PDF từ {folder} (chỉ lần đầu, mất vài giây)…", flush=True)
    rows, names = [], []
    for pdf in sorted(folder.glob("*.pdf")):
        try:
            reader = PdfReader(str(pdf))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception:
            continue
        # stop at the reference list to keep passages relevant
        cut = re.search(r"\n\s*References\s*\n", text)
        if cut and cut.start() > len(text) * 0.4:
            text = text[: cut.start()]
        for chunk in _chunks(text):
            rows.append((ts, f"paper:{pdf.name}", chunk))
        names.append(pdf.name)
    if rows:
        store.insert_many(rows)
    return len(rows), names
