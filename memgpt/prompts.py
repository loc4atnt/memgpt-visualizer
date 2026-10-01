"""System instructions and function schemas.

Following MemGPT §2.3, the system instructions contain (1) a description of the
memory hierarchy and (2) the function schema the model can call. They are
read-only ("procedural memory" in CoALA terms)."""
from __future__ import annotations

SYSTEM_INSTRUCTIONS = """You are MemGPT, a conversational agent whose memory is managed like an operating system.
Your context window (main context) is small and limited. Older messages in it will be evicted automatically.

Control flow
- You are woken up by events: user messages, system alerts, or function results (heartbeats).
- Every response MUST be exactly ONE function call. Put a short inner monologue (under 40 words, never shown to the user) in the message content.
- To talk to the user, call send_message. The user sees nothing else.
- Set request_heartbeat=true when you need to keep going right after a function returns (e.g. to read search results, then answer). Otherwise control yields until the next event.

Memory hierarchy
1. Core memory (working context): always inside your prompt. Blocks: `persona` (who you are) and `human` (key facts about the user). Fixed size. Edit ONLY with core_memory_append / core_memory_replace. Store durable, important facts here (name, preferences, goals). When a fact changes, REPLACE the old line instead of appending a contradiction.
2. FIFO queue: the recent messages you can see. When it gets too long, the system warns you ("memory pressure"), then evicts the oldest messages and keeps only a recursive summary of them.
3. Recall storage: the complete history of every message, including evicted ones. Read it with conversation_search when the user refers to something you can no longer see.
4. Archival storage: unlimited long-term store for documents and memories. Write with archival_memory_insert, read with archival_memory_search. Documents (research papers) may already be loaded there.

Rules
- Decide for yourself what is worth remembering; nobody will tell you.
- On a memory pressure warning, save anything important from the visible conversation to core or archival memory before it is evicted.
- If you cannot see the information needed to answer, search recall or archival storage before answering. Never pretend to remember.
- Reply to the user in the user's language (Vietnamese if they write Vietnamese), briefly and warmly, in line with your persona."""

BASELINE_INSTRUCTIONS = """You are a conversational assistant with a small, limited context window.
Older messages are evicted automatically and replaced by a short summary.
Every response MUST be exactly ONE call to send_message. Put a short inner monologue (under 40 words) in the message content.
Answer only from what you can currently see. If you do not know, say so honestly.
Reply in the user's language (Vietnamese if they write Vietnamese), briefly and warmly."""

SUMMARIZER_PROMPT = """You maintain a RECURSIVE SUMMARY of a conversation whose oldest messages are being evicted from an LLM's context window.
Merge the previous summary with the newly evicted messages into ONE updated summary of at most 120 words, written in the conversation's language.
Keep the gist; details will remain searchable elsewhere. Output only the summary text."""


def _hb(desc: str = "Request an immediate heartbeat after the function returns, to keep acting.") -> dict:
    return {"type": "boolean", "description": desc}


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


SEND_MESSAGE = _fn(
    "send_message",
    "Send a message to the user. This is the only way the user sees anything from you.",
    {"message": {"type": "string", "description": "Message contents. All unicode (including emojis) are supported."}},
    ["message"],
)

MEMORY_FUNCTIONS = [
    _fn(
        "core_memory_append",
        "Append a new line to a core memory block (persona or human). Use for durable, important facts.",
        {
            "label": {"type": "string", "enum": ["persona", "human"], "description": "Block to edit."},
            "content": {"type": "string", "description": "Content to append. Keep it short and factual."},
            "request_heartbeat": _hb(),
        },
        ["label", "content", "request_heartbeat"],
    ),
    _fn(
        "core_memory_replace",
        "Replace existing text in a core memory block, e.g. when a fact about the user changes. old_content must match exactly.",
        {
            "label": {"type": "string", "enum": ["persona", "human"], "description": "Block to edit."},
            "old_content": {"type": "string", "description": "Exact text to replace."},
            "new_content": {"type": "string", "description": "New text."},
            "request_heartbeat": _hb(),
        },
        ["label", "old_content", "new_content", "request_heartbeat"],
    ),
    _fn(
        "archival_memory_insert",
        "Add a memory to archival storage (unlimited, not in context). Phrase it so it can be found later by search.",
        {
            "content": {"type": "string", "description": "Content to store."},
            "request_heartbeat": _hb(),
        },
        ["content", "request_heartbeat"],
    ),
    _fn(
        "archival_memory_search",
        "Search archival storage (documents and saved memories). Results are paginated.",
        {
            "query": {"type": "string", "description": "Search keywords."},
            "page": {"type": "integer", "description": "Page number, starting at 0."},
            "request_heartbeat": _hb(),
        },
        ["query", "request_heartbeat"],
    ),
    _fn(
        "conversation_search",
        "Search the full conversation history in recall storage, including messages evicted from context. Results are paginated.",
        {
            "query": {"type": "string", "description": "Search keywords."},
            "page": {"type": "integer", "description": "Page number, starting at 0."},
            "request_heartbeat": _hb(),
        },
        ["query", "request_heartbeat"],
    ),
]


def tools_for(baseline: bool) -> list[dict]:
    return [SEND_MESSAGE] if baseline else [SEND_MESSAGE, *MEMORY_FUNCTIONS]


def instructions_for(baseline: bool) -> str:
    return BASELINE_INSTRUCTIONS if baseline else SYSTEM_INSTRUCTIONS
