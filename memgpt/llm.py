"""LLM processors.

* OpenAILLM   – any OpenAI-compatible chat-completions endpoint with tool calling
                (OpenAI, OpenRouter, Ollama, LM Studio, vLLM, ...).
* ScriptedLLM – a deterministic, rule-based stand-in used to rehearse the demo
                without an API key. Its "decisions" are hand-written rules, so
                the UI labels this mode SCRIPTED. It still goes through exactly
                the same queue manager, functions and memory stores.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .prompts import SUMMARIZER_PROMPT

if TYPE_CHECKING:  # pragma: no cover
    from .agent import Agent, Msg


@dataclass
class LLMOutput:
    thought: str | None
    call: dict | None  # {"id", "name", "arguments"(json str)}
    usage: dict = field(default_factory=dict)


def _call(name: str, **args: Any) -> dict:
    return {"id": "call_" + uuid.uuid4().hex[:12], "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


# --------------------------------------------------------------------------- #
# Real LLM
# --------------------------------------------------------------------------- #
class OpenAILLM:
    kind = "live"

    def __init__(self, cfg):
        from openai import AsyncOpenAI

        self.cfg = cfg
        self.model = cfg.model
        self.client = AsyncOpenAI(api_key=cfg.api_key or "not-needed", base_url=cfg.base_url or None)
        # optional params some providers reject; dropped one by one on 400 errors
        self._optional = ["parallel_tool_calls", "tool_choice", "temperature"]
        self._disabled: set[str] = set()

    async def _create(self, **kwargs):
        from openai import BadRequestError

        while True:
            args = {k: v for k, v in kwargs.items() if k not in self._disabled}
            try:
                return await self.client.chat.completions.create(**args)
            except BadRequestError as e:
                candidates = [p for p in self._optional if p in args]
                if not candidates:
                    raise
                # drop the first optional parameter mentioned in the error, else the first one present
                msg = str(e)
                drop = next((p for p in candidates if p in msg), candidates[0])
                self._disabled.add(drop)

    async def complete(self, messages: list[dict], tools: list[dict], ctx: "Agent | None" = None) -> LLMOutput:
        resp = await self._create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="required",
            parallel_tool_calls=False,
            temperature=self.cfg.temperature,
        )
        msg = resp.choices[0].message
        call = None
        if msg.tool_calls:
            tc = msg.tool_calls[0]
            call = {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or "{}"}
        usage = {}
        if getattr(resp, "usage", None):
            usage = {"prompt_tokens": resp.usage.prompt_tokens, "completion_tokens": resp.usage.completion_tokens}
        return LLMOutput((msg.content or "").strip() or None, call, usage)

    async def summarize(self, previous: str, evicted: list["Msg"]) -> str:
        transcript = "\n".join(f"{m.kind}: {m.display_text()}" for m in evicted)
        resp = await self._create(
            model=self.model,
            messages=[
                {"role": "system", "content": SUMMARIZER_PROMPT},
                {"role": "user", "content": f"Previous summary:\n{previous or '(none)'}\n\nNewly evicted messages:\n{transcript}"},
            ],
            temperature=self.cfg.temperature,
        )
        return (resp.choices[0].message.content or "").strip()


# --------------------------------------------------------------------------- #
# Scripted stand-in (rehearsal mode)
# --------------------------------------------------------------------------- #
def cap_phrase_after(text: str, marker: str = "tên là ") -> str | None:
    """'... một quán tên là Sương Sớm, nằm ...' -> 'Sương Sớm'."""
    i = text.find(marker)
    if i < 0:
        return None
    out: list[str] = []
    for w in text[i + len(marker):].split():
        core = w.strip(",.!?;:\"“”()")
        if not core or not core[0].isupper():
            break
        out.append(core)
        if w and w[-1] in ",.!?;:":
            break
    return " ".join(out) or None


def first_words(text: str, n: int) -> str:
    words = text.split()
    return " ".join(words[:n]) + ("…" if len(words) > n else "")


STORY_REPLIES = [
    "Nghe thích quá! Kể tiếp đi, mình đang nghe nè 😊",
    "Trời, nghe mà muốn đi Đà Lạt liền. Rồi sao nữa?",
    "Chuyến đi có vẻ thật sự đáng nhớ đó!",
    "Nghe ấm áp ghê. Mình hình dung được luôn khung cảnh đó.",
    "Hay quá! Hai bạn chắc thân nhau lắm.",
    "Một chuyến đi thong thả như vậy đúng là quý thật.",
]


class ScriptedLLM:
    kind = "scripted"
    model = "scripted-rules"

    def __init__(self, cfg):
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        self.plan: list[tuple[str | None, dict]] = []
        self.pending_reply: str | None = None
        self._story_i = 0
        self.saved: set[str] = set()

    # -- helpers ----------------------------------------------------------------
    @staticmethod
    def _parse_event(content: str) -> tuple[str, str]:
        try:
            data = json.loads(content)
            return data.get("type", "user_message"), data.get("message", "")
        except Exception:
            return "user_message", content

    @staticmethod
    def _names(tools: list[dict]) -> set[str]:
        return {t["function"]["name"] for t in tools}

    # -- main entry -------------------------------------------------------------
    async def complete(self, messages: list[dict], tools: list[dict], ctx: "Agent | None" = None) -> LLMOutput:
        await asyncio.sleep(self.cfg.scripted_delay)
        names = self._names(tools)
        last = messages[-1]
        if last["role"] == "tool":
            prev = messages[-2]["tool_calls"][0]["function"]
            return self._after_tool(prev["name"], json.loads(prev["arguments"] or "{}"), last["content"], ctx, names)
        etype, text = self._parse_event(last.get("content") or "")
        if etype == "system_alert" and "Memory pressure" in text:
            return self._on_pressure(ctx, names)
        return self._on_user(text, ctx, names)

    # -- after a function returned (heartbeat) -------------------------------------
    def _after_tool(self, name: str, args: dict, result: str, ctx, names) -> LLMOutput:
        if self.plan:
            thought, call = self.plan.pop(0)
            return LLMOutput(thought, call)
        try:
            payload = json.loads(result)
        except Exception:
            payload = {"message": result}
        body = str(payload.get("message", ""))
        if payload.get("status") == "Failed":
            return LLMOutput("Lệnh trước bị lỗi; báo cho người dùng.", _call("send_message", message="Xin lỗi, mình gặp chút trục trặc khi cập nhật bộ nhớ."))
        if name == "conversation_search":
            place = cap_phrase_after(body)
            if place:
                return LLMOutput(
                    "Tìm thấy trong recall storage: tên quán nằm trong tin nhắn cũ.",
                    _call("send_message", message=f"Quán đó tên là {place} nè! Mình vừa lục lại lịch sử trò chuyện: hôm đó trời mưa, hai bạn trú mưa ở {place} và ngồi nguyên buổi chiều với cà phê trứng ☕"),
                )
            snippet = body[:220]
            return LLMOutput("Không thấy câu trả lời rõ ràng; trả lời trung thực.", _call("send_message", message=f"Mình tìm lại được đoạn này: «{snippet}»"))
        if name == "archival_memory_search":
            m = re.search(r"[^.\n]{0,160}70%[^.\n]{0,160}", body)
            snippet = (m.group(0) if m else body[:260]).strip()
            return LLMOutput(
                "Kết quả archival có đoạn nói về ngưỡng cảnh báo.",
                _call("send_message", message=f"Theo paper MemGPT (§2.2), khi prompt vượt khoảng 70% context window thì queue manager chèn cảnh báo \"memory pressure\"; tới 100% thì flush. Đoạn mình tìm thấy trong archival: «{snippet}»"),
            )
        if self.pending_reply:
            reply, self.pending_reply = self.pending_reply, None
            return LLMOutput("Đã lưu xong, giờ trả lời người dùng.", _call("send_message", message=reply))
        return LLMOutput(None, _call("send_message", message="Mình đã ghi nhớ rồi nhé!"))

    # -- system alert: memory pressure ---------------------------------------------
    def _on_pressure(self, ctx, names) -> LLMOutput:
        if "archival_memory_insert" not in names or ctx is None:
            return LLMOutput("Bộ nhớ sắp đầy nhưng mình không có hàm để lưu.", _call("send_message", message="(Bộ nhớ đang đầy dần…)"))
        stories = [m.content for m in ctx.queue if m.kind == "user" and len(m.content) > 200 and m.content not in self.saved]
        if not stories:
            return LLMOutput("Không có thông tin quan trọng mới cần lưu — bỏ qua cảnh báo này.", None)
        self.saved.update(stories)
        facts = [first_words(s.split(". ")[0], 18) for s in stories] or ["(không có chuyện dài nào)"]
        content = "Chuyện Minh kể (lưu trước khi bị evict): " + " | ".join(facts)
        friend = None
        for s in stories:
            m = re.search(r"với (\w+)\s*[–-]\s*bạn thân", s)
            if m:
                friend = m.group(1)
                break
        thought = "Cảnh báo memory pressure! Lưu ý chính của chuyện đang kể vào archival trước khi tin cũ bị evict."
        if friend:
            self.plan.append((
                f"Thêm fact bền vững về {friend} vào core memory (human).",
                _call("core_memory_append", label="human", content=f"Có bạn thân tên {friend} (từng đi Đà Lạt cùng nhau)", request_heartbeat=False),
            ))
        return LLMOutput(thought, _call("archival_memory_insert", content=content, request_heartbeat=bool(friend)))

    # -- user messages --------------------------------------------------------------
    def _context_text(self, ctx) -> str:
        return "\n".join(m.content for m in ctx.queue) if ctx else ""

    def _on_user(self, text: str, ctx, names) -> LLMOutput:
        t = text.lower()
        has_memory = "core_memory_append" in names
        human = ctx.core.blocks.get("human", "") if ctx else ""

        # 1) Self-introduction -> save durable facts to core memory
        if re.search(r"\b(mình|tôi) là\b", t) and any(k in t for k in ("sinh nhật", "luận văn", "học")):
            name = re.search(r"(?:[Mm]ình|[Tt]ôi) là (\w+)", text)
            who = name.group(1) if name else "bạn"
            reply = f"Rất vui được làm quen, {who}! Chúc luận văn suôn sẻ nha — có gì cần mình hỗ trợ cứ nói nhé ☕"
            if not has_memory:
                return LLMOutput("Chỉ có thể trả lời, không lưu được.", _call("send_message", message=reply))
            parts = [p.strip(" .!") for p in re.split(r"[,.!]| và ", text) if p.strip(" .!")]
            facts = []
            for p in parts:
                pl = p.lower()
                if pl.startswith("chào"):
                    continue
                m = re.match(r"(?:[Mm]ình|[Tt]ôi) là (\w+)$", p)
                if m:
                    facts.append(f"Tên: {m.group(1)}")
                    continue
                p = re.sub(r"^[Ss]inh nhật (?:mình|tôi) là", "Sinh nhật:", p)
                p = re.sub(r"^(?:[Mm]ình|[Tt]ôi) ", "", p)
                facts.append(p[:1].upper() + p[1:])
            self.pending_reply = reply
            return LLMOutput(
                "Người dùng vừa chia sẻ thông tin cá nhân quan trọng — nên lưu vào core memory (human).",
                _call("core_memory_append", label="human", content="\n".join(facts), request_heartbeat=True),
            )

        # 2) A fact changed -> replace, don't append a contradiction
        if "đổi" in t and "đề tài" in t:
            m = re.search(r"sang (.+?)(?: rồi| nhé|[.!]|$)", text)
            topic = (m.group(1) if m else text).strip()
            reply = f"Đã cập nhật! Đề tài mới: {topic}. Nghe thú vị đó 👍"
            if not has_memory:
                return LLMOutput("Không có core memory để sửa.", _call("send_message", message=reply))
            old = next((ln for ln in human.splitlines() if "luận văn" in ln), None)
            self.pending_reply = reply
            if old:
                return LLMOutput(
                    "Fact cũ về đề tài luận văn đã lỗi thời — phải REPLACE chứ không append.",
                    _call("core_memory_replace", label="human", old_content=old, new_content=f"Đang làm luận văn về {topic} (trước đây: agent memory)", request_heartbeat=True),
                )
            return LLMOutput("Chưa có dòng nào về luận văn; thêm mới.", _call("core_memory_append", label="human", content=f"Đang làm luận văn về {topic}", request_heartbeat=True))

        # 3) What do you remember about me?
        if "nhớ những gì" in t or "biết gì về mình" in t:
            if has_memory and human and human != "Chưa biết gì về người dùng.":
                lines = "\n".join(f"• {ln}" for ln in human.splitlines())
                return LLMOutput("Trả lời từ core memory (human) — luôn có trong context.", _call("send_message", message=f"Mình nhớ nè:\n{lines}"))
            summary = next((m.content for m in ctx.queue if m.kind == "summary"), "") if ctx else ""
            msg = "Mình chỉ còn nhớ loáng thoáng thôi…" + (f" ({first_words(summary, 25)})" if summary else " Xin lỗi nha 😅")
            return LLMOutput("Không có core memory; chỉ còn những gì thấy trong context.", _call("send_message", message=msg))

        # 4) Question about the research papers -> archival search
        if "paper" in t or "memgpt" in t:
            if "archival_memory_search" in names:
                q = "memory pressure warning token count context window" if ("ngưỡng" in t or "cảnh báo" in t) else text
                return LLMOutput("Câu hỏi về tài liệu — tìm trong archival storage.", _call("archival_memory_search", query=q, page=0, request_heartbeat=True))
            return LLMOutput("Không truy cập được tài liệu.", _call("send_message", message="Xin lỗi, mình không có tài liệu đó trong đầu."))

        # 5) Question about an earlier detail
        if any(k in t for k in ("hồi nãy", "tên gì", "còn nhớ", "nhớ không")):
            visible = self._context_text(ctx)
            place = None
            for line in visible.splitlines():
                if "quán" in line and "tên là" in line:
                    place = cap_phrase_after(line)
                    break
            if place:
                return LLMOutput("Chi tiết này vẫn còn trong context — trả lời trực tiếp.", _call("send_message", message=f"Quán {place} nè! 😊"))
            if "conversation_search" in names:
                q = "quán cà phê buổi chiều" if "quán" in t else text
                return LLMOutput(
                    "Chi tiết này không còn trong context (đã bị evict) — tìm trong recall storage.",
                    _call("conversation_search", query=q, page=0, request_heartbeat=True),
                )
            return LLMOutput("Không thấy trong context, không có cách tra cứu.", _call("send_message", message="Xin lỗi, mình không còn nhớ chi tiết đó 😅"))

        # 6) Long story -> just listen
        if len(text) > 200:
            reply = STORY_REPLIES[self._story_i % len(STORY_REPLIES)]
            self._story_i += 1
            return LLMOutput("Người dùng đang kể chuyện; phản hồi ngắn, chưa cần lưu gì.", _call("send_message", message=reply))

        return LLMOutput(None, _call("send_message", message="Mình hiểu rồi! Bạn muốn nói thêm gì không?"))

    async def summarize(self, previous: str, evicted: list["Msg"]) -> str:
        await asyncio.sleep(self.cfg.scripted_delay / 2)
        users = [first_words(m.content, 9) for m in evicted if m.kind == "user"]
        n_asst = sum(1 for m in evicted if m.kind == "assistant")
        new = f"{len(evicted)} tin cũ: người dùng nói \"" + "\"; \"".join(users) + f"\"; trợ lý đã phản hồi {n_asst} lần." if users else f"{len(evicted)} tin cũ (trao đổi ngắn)."
        text = (previous + " " + new).strip() if previous else new
        return text[-700:]
