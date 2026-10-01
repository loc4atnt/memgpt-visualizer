"""The MemGPT agent: main context + queue manager + function executor + event loop.

Paper mapping (Packer et al., 2024):
  main context  = system instructions + working context (core memory) + FIFO queue   (§2.1)
  queue manager = append / write-all to recall / 70% warning / 100% flush + summary  (§2.2)
  function exec = parse & run the LLM's function call, feed results/errors back      (§2.3)
  control flow  = events trigger inference; request_heartbeat chains calls           (§2.4)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import Config
from .memory import ArchivalStore, CoreMemory, CoreMemoryError, RecallStore
from .prompts import instructions_for, tools_for
from .tokens import TOKENIZER_NAME, count_tokens

Emitter = Callable[[str, dict], Awaitable[None]]


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Msg:
    kind: str  # user | alert | summary | assistant | tool
    content: str = ""
    id: int = 0
    ts: str = ""
    tokens: int = 0
    tool_call: dict | None = None  # assistant: {"id","name","arguments"}
    tool_call_id: str | None = None  # tool
    meta: dict = field(default_factory=dict)

    # How the message appears in the prompt (OpenAI chat format). System events
    # are wrapped as JSON user messages, as in the original MemGPT.
    def api(self) -> dict:
        if self.kind == "user":
            return {"role": "user", "content": json.dumps({"type": "user_message", "message": self.content, "time": self.ts}, ensure_ascii=False)}
        if self.kind == "alert":
            return {"role": "user", "content": json.dumps({"type": "system_alert", "message": self.content, "time": self.ts}, ensure_ascii=False)}
        if self.kind == "summary":
            note = (
                "Note: prior messages have been hidden from view due to conversation memory constraints. "
                "The following is a summary of the previous messages:\n" + self.content
            )
            return {"role": "user", "content": json.dumps({"type": "system_alert", "message": note, "time": self.ts}, ensure_ascii=False)}
        if self.kind == "assistant" and not self.tool_call:
            return {"role": "assistant", "content": self.content or "…"}
        if self.kind == "assistant":
            tc = self.tool_call or {}
            return {
                "role": "assistant",
                "content": self.content or None,
                "tool_calls": [{"id": tc.get("id"), "type": "function", "function": {"name": tc.get("name"), "arguments": tc.get("arguments", "{}")}}],
            }
        return {"role": "tool", "tool_call_id": self.tool_call_id, "content": self.content}

    def fn_name(self) -> str | None:
        if self.kind == "assistant" and self.tool_call:
            return self.tool_call.get("name")
        return self.meta.get("name")

    def args(self) -> dict:
        try:
            return json.loads((self.tool_call or {}).get("arguments") or "{}")
        except Exception:
            return {}

    def display_text(self) -> str:
        if self.kind == "assistant" and not self.tool_call:
            return self.content
        if self.kind == "assistant":
            a = self.args()
            if self.fn_name() == "send_message":
                return a.get("message", "")
            return f"{self.fn_name()}({json.dumps({k: v for k, v in a.items() if k != 'request_heartbeat'}, ensure_ascii=False)})"
        return self.content

    def full_text(self) -> str:
        """Readable, untruncated content of this message (for the UI tooltip)."""
        if self.kind == "assistant":
            head = f"💭 {self.content}\n\n" if self.content else ""
            if not self.tool_call:
                return head.strip()
            args = self.args()
            body = "\n".join(f"  {k}: {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}" for k, v in args.items())
            return f"{head}{self.fn_name()}(\n{body}\n)"
        if self.kind == "tool":
            try:
                payload = json.loads(self.content)
                msg = payload.get("message")
                return f"[{payload.get('status')}] " + (msg if isinstance(msg, str) else "(không có nội dung trả về)")
            except Exception:
                return self.content
        return self.content

    def view(self) -> dict:
        label = {"user": "user", "alert": "system alert", "summary": "recursive summary", "tool": "function result"}.get(self.kind, "assistant")
        if self.kind == "assistant":
            label = f"→ {self.fn_name()}" if self.tool_call else "→ (chỉ suy nghĩ)"
        elif self.kind == "tool":
            label = f"← {self.meta.get('name', '')}"
        return {
            "id": self.id,
            "kind": self.kind,
            "tokens": self.tokens,
            "label": label,
            "preview": self.display_text()[:240],
            "full": self.full_text(),
            "role": self.api()["role"],
            "thought": self.content if self.kind == "assistant" else None,
            "ok": self.meta.get("ok", True),
            "ts": self.ts,
        }


class Agent:
    def __init__(self, cfg: Config, llm, emit: Emitter):
        self.cfg = cfg
        self.llm = llm
        self._emit = emit
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.core = CoreMemory(cfg.data_dir / "core_memory.json", cfg.persona_limit, cfg.human_limit)
        self.recall = RecallStore(cfg.data_dir / "recall.sqlite")
        self.archival = ArchivalStore(cfg.data_dir / "archival.sqlite")
        self.queue: list[Msg] = []
        self._next_id = int(time.time()) % 100000 * 10
        self.session = 1
        self.warned = False
        self.unseen_alert = False
        self.step_mode = False
        self.baseline = False
        self.paused: dict | None = None
        self.busy = False
        self.status = "idle"
        self.last_prompt: list[dict] = []
        self._next = asyncio.Event()
        self.events: asyncio.Queue = asyncio.Queue()
        self.step_counter = 0
        self.in_chain = False

    # ------------------------------------------------------------------ state
    def _instr(self) -> str:
        return instructions_for(self.baseline)

    def _tools(self) -> list[dict]:
        return tools_for(self.baseline)

    def _stats_text(self) -> str:
        return (
            "### Memory statistics\n"
            f"{self.recall.count()} previous messages between you and the user are stored in recall storage (use conversation_search).\n"
            f"{self.archival.count()} passages are stored in archival storage (use archival_memory_search)."
            if not self.baseline
            else "### Memory statistics\n(no external memory available)"
        )

    def _core_text(self) -> str:
        return "" if self.baseline else self.core.render()

    def system_message(self) -> str:
        return "\n\n".join(p for p in (self._instr(), self._core_text(), self._stats_text()) if p)

    def segments(self) -> dict:
        system = count_tokens(self._instr()) + count_tokens(json.dumps(self._tools(), ensure_ascii=False)) + count_tokens(self._stats_text())
        working = count_tokens(self._core_text())
        queue = sum(m.tokens for m in self.queue)
        return {"system": system, "working": working, "queue": queue}

    def used_tokens(self) -> int:
        s = self.segments()
        return s["system"] + s["working"] + s["queue"]

    def snapshot(self) -> dict:
        seg = self.segments()
        used = seg["system"] + seg["working"] + seg["queue"]
        return {
            "mode": self.llm.kind,
            "model": self.llm.model,
            "tokenizer": TOKENIZER_NAME,
            "session": self.session,
            "step_mode": self.step_mode,
            "baseline": self.baseline,
            "paused": self.paused,
            "busy": self.busy,
            "status": self.status,
            "L": self.cfg.context_limit,
            "warn": self.cfg.warn_frac,
            "flush": self.cfg.flush_frac,
            "segments": seg,
            "used": used,
            "pct": round(100 * used / self.cfg.context_limit, 1),
            "queue": [m.view() for m in self.queue],
            "core": self.core.snapshot(),
            "recall": {"count": self.recall.count(), "recent": _clip(self.recall.recent(30))},
            "archival": {
                "count": self.archival.count(),
                "papers": self.archival.count("paper:"),
                "agent": self.archival.count("agent"),
                "recent": _clip(self.archival.recent_agent(20)),
            },
        }

    async def emit(self, etype: str, data: dict | None = None) -> None:
        await self._emit(etype, data or {})

    # ---------------------------------------------------------- step gate
    async def gate(self, label: str, info: dict) -> None:
        """In Step mode, pause right before an action so the presenter can explain it."""
        if not self.step_mode:
            return
        self.paused = {"label": label, **info}
        self._next.clear()
        await self.emit("paused", self.paused)
        await self._next.wait()
        self.paused = None
        await self.emit("resumed", {"label": label})

    def next(self) -> None:
        self._next.set()

    def set_step_mode(self, on: bool) -> None:
        self.step_mode = on
        if not on:
            self._next.set()

    # ------------------------------------------------------ queue manager
    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _measure(self, m: Msg) -> None:
        m.tokens = count_tokens(json.dumps(m.api(), ensure_ascii=False)) + 4

    async def append(self, m: Msg, event: str = "queue_append", extra: dict | None = None) -> Msg:
        """Queue manager: append to FIFO, write to recall storage, then check pressure (§2.2)."""
        m.id = self._new_id()
        m.ts = m.ts or now()
        self._measure(m)
        self.queue.append(m)
        kind = m.kind
        text = m.display_text()
        if m.kind == "assistant" and m.fn_name() == "send_message":
            kind = "assistant_message"
        self.recall.insert(m.id, m.ts, kind, text)
        await self.emit(event, {"msg": m.view(), **(extra or {})})
        await self.check_pressure()
        return m

    async def check_pressure(self) -> None:
        used = self.used_tokens()
        if used >= self.cfg.flush_tokens:
            await self.flush(used)
        elif used >= self.cfg.warn_tokens and not self.warned and not self.in_chain:
            # The warning is inserted when the agent yields, so a function call and its
            # result stay adjacent and a pending user message is answered first.
            self.warned = True
            pct = round(100 * used / self.cfg.context_limit)
            alert = Msg(
                kind="alert",
                content=(
                    f"Memory pressure: {pct}% of the context window is in use ({used}/{self.cfg.context_limit} tokens). "
                    "The oldest messages will soon be evicted from context. Save any important information "
                    "to core memory or archival memory now."
                ),
            )
            self.unseen_alert = True
            await self.append(alert, event="alert", extra={"pct": pct, "used": used})

    async def flush(self, used: int) -> None:
        start = 1 if self.queue and self.queue[0].kind == "summary" else 0
        target = int(self.cfg.evict_frac * self.cfg.context_limit)
        plan: list[Msg] = []
        tokens = 0
        i = start
        while len(self.queue) - (i + 1) >= self.cfg.keep_tail and tokens < target:
            plan.append(self.queue[i])
            tokens += self.queue[i].tokens
            i += 1
        while i < len(self.queue) and self.queue[i].kind == "tool":
            plan.append(self.queue[i])  # never leave a function result without its call
            tokens += self.queue[i].tokens
            i += 1
        if not plan:
            return
        await self.gate("flush", {"used": used, "evict_ids": [m.id for m in plan], "evict_tokens": tokens})
        await self.emit("flush_start", {"evict_ids": [m.id for m in plan], "evict_tokens": tokens, "used": used})
        self.status = "summarizing"
        previous = self.queue[0].content if start == 1 else ""
        evicted = self.queue[start:start + len(plan)]
        del self.queue[start:start + len(plan)]
        try:
            summary_text = await self.llm.summarize(previous, evicted)
        except Exception as e:  # keep the demo alive even if the summarizer fails
            summary_text = (previous + f" [{len(evicted)} tin cũ đã bị evict; tóm tắt lỗi: {e}]").strip()
        total_evicted = (self.queue[0].meta.get("evicted_total", 0) if start == 1 else 0) + len(evicted)
        summary = Msg(kind="summary", content=summary_text, meta={"evicted_total": total_evicted})
        summary.id = self._new_id()
        summary.ts = now()
        self._measure(summary)
        if start == 1:
            self.queue[0] = summary
        else:
            self.queue.insert(0, summary)
        self.warned = False
        self.status = "thinking" if self.busy else "idle"
        await self.emit(
            "flush",
            {
                "evicted_ids": [m.id for m in evicted],
                "evicted_tokens": tokens,
                "evicted_total": total_evicted,
                "summary": summary.view(),
                "used_before": used,
            },
        )

    # --------------------------------------------------------- event loop
    async def run(self) -> None:
        while True:
            ev = await self.events.get()
            try:
                await self.handle(ev)
            except Exception as e:  # never kill the loop during a talk
                self.busy = False
                self.in_chain = False
                self.status = "idle"
                await self.emit("error", {"message": f"{type(e).__name__}: {e}"})

    async def handle(self, ev: dict) -> None:
        if ev["type"] == "user_message":
            self.busy = True
            self.status = "thinking"
            await self.emit("chat", {"role": "user", "text": ev["text"]})
            self.in_chain = True
            await self.append(Msg(kind="user", content=ev["text"]))
            await self.run_chain()
            self.busy = False
            self.status = "idle"
            await self.emit("idle", {})

    async def run_chain(self, depth: int = 0) -> None:
        self.in_chain = True
        for _ in range(self.cfg.max_chain):
            self.step_counter += 1
            messages = [{"role": "system", "content": self.system_message()}] + [m.api() for m in self.queue]
            self.last_prompt = messages
            self.unseen_alert = False
            self.status = "thinking"
            await self.emit("llm_call", {"step": self.step_counter, "n_messages": len(messages), "prompt": messages, "tools": [t["function"]["name"] + ": " + t["function"]["description"] for t in self._tools()]})
            out = await self.llm.complete(messages, self._tools(), ctx=self)
            warn = None
            call = out.call
            if call is None and self.queue and self.queue[-1].kind == "alert":
                # the model chose to do nothing about the alert: record its thought and yield
                note = Msg(kind="assistant", content=out.thought or "(không có gì cần lưu)")
                await self.append(note, event="thought_only", extra={"step": self.step_counter, "thought": note.content})
                break
            if call is None:
                warn = "Model trả lời bằng text thay vì gọi hàm — tự chuyển thành send_message."
                call = {"id": f"call_auto_{self.step_counter}", "name": "send_message", "arguments": json.dumps({"message": out.thought or "…"}, ensure_ascii=False)}
                out.thought = None
            asst = Msg(kind="assistant", content=out.thought or "", tool_call=call)
            args = asst.args()
            heartbeat = _truthy(args.get("request_heartbeat", False))
            await self.append(
                asst,
                event="function_call",
                extra={"step": self.step_counter, "name": call["name"], "args": args, "heartbeat": heartbeat, "thought": out.thought, "warn": warn, "usage": out.usage},
            )
            self.status = "waiting" if self.step_mode else "executing"
            await self.gate("function_call", {"name": call["name"], "args": args, "thought": out.thought, "msg_id": asst.id})
            self.status = "executing"
            result, ok = await self.execute(call["name"], call.get("arguments") or "{}")
            payload = {"status": "OK" if ok else "Failed", "message": result, "time": now()}
            tool = Msg(kind="tool", content=json.dumps(payload, ensure_ascii=False), tool_call_id=call["id"], meta={"name": call["name"], "ok": ok})
            await self.append(tool, event="function_result", extra={"name": call["name"], "ok": ok, "result": result, "call_id": asst.id})
            if not ok:
                heartbeat = True  # let the model see the error and fix it (§2.3)
            if call["name"] == "send_message" and ok:
                heartbeat = False
            if not heartbeat:
                break
        self.in_chain = False
        await self.check_pressure()  # agent yielded: now the memory-pressure warning may be inserted
        if self.unseen_alert and depth < 2:
            await self.run_chain(depth + 1)

    # ------------------------------------------------------ function executor
    async def execute(self, name: str, raw_args: str) -> tuple[Any, bool]:
        allowed = {t["function"]["name"] for t in self._tools()}
        if name not in allowed:
            return f"Function '{name}' is not available.", False
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError as e:
            return f"Invalid JSON arguments: {e}", False
        try:
            if name == "send_message":
                text = str(args.get("message", "")).strip()
                if not text:
                    return "message must not be empty", False
                await self.emit("chat", {"role": "assistant", "text": text})
                return None, True
            if name == "core_memory_append":
                label = args.get("label", "human")
                before, after = self.core.append(label, str(args.get("content", "")))
                await self.emit("memory_update", {"label": label, "op": "append", "before": before, "after": after})
                return None, True
            if name == "core_memory_replace":
                label = args.get("label", "human")
                before, after = self.core.replace(label, str(args.get("old_content", "")), str(args.get("new_content", "")))
                await self.emit("memory_update", {"label": label, "op": "replace", "before": before, "after": after})
                return None, True
            if name == "archival_memory_insert":
                content = str(args.get("content", "")).strip()
                if not content:
                    return "content must not be empty", False
                pid = self.archival.insert(now(), "agent", content)
                await self.emit("archival_insert", {"id": pid, "content": content})
                return None, True
            if name in ("archival_memory_search", "conversation_search"):
                query = str(args.get("query", ""))
                page = int(args.get("page") or 0)
                store = "archival" if name == "archival_memory_search" else "recall"
                if store == "archival":
                    res = self.archival.search(query, page, self.cfg.page_size)
                else:  # search out-of-context history (Fig. 2): skip what is already visible
                    res = self.recall.search(query, page, self.cfg.page_size, exclude={m.id for m in self.queue})
                lines = []
                for r in res.results:
                    txt = r["text"].replace("\n", " ")
                    txt = txt[:400] + ("…" if len(txt) > 400 else "")
                    who = r.get("kind") or r.get("source")
                    lines.append(f"[{r['ts']}] ({who}) {txt}")
                header = f"Showing {len(res.results)} of {res.total} results (page {res.page + 1}/{res.pages})"
                if res.total == 0:
                    header = "No results found."
                await self.emit("page_in", {"store": store, "query": query, "page": res.page, "pages": res.pages, "total": res.total, "shown": len(res.results)})
                return header + (":\n" + "\n".join(lines) if lines else ""), True
        except CoreMemoryError as e:
            return str(e), False
        except Exception as e:
            return f"{type(e).__name__}: {e}", False
        return f"Unknown function {name}", False

    # ------------------------------------------------------- session ops
    async def new_session(self) -> None:
        self.queue.clear()
        self.warned = False
        self.session += 1
        await self.emit("session", {"op": "new_session", "session": self.session})

    async def reset(self) -> None:
        self.queue.clear()
        self.warned = False
        self.core.reset()
        self.recall.clear()
        self.archival.clear_agent()
        if hasattr(self.llm, "reset"):
            self.llm.reset()  # scripted stand-in keeps a little state
        self.session = 1
        await self.emit("session", {"op": "reset", "session": self.session})


def _clip(rows: list[dict], n: int = 900) -> list[dict]:
    for r in rows:
        if len(r.get("text") or "") > n:
            r["text"] = r["text"][:n] + "…"
    return rows


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)
