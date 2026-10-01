"""MemGPT Visualizer — FastAPI + WebSocket server.

    python server.py              # LIVE if OPENAI_API_KEY is set, otherwise SCRIPTED
    python server.py --scripted   # rule-based stand-in LLM (no API key, rehearsal)
    python server.py --replay runs/sample_scripted.jsonl   # replay a recorded run

Then open http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import warnings

warnings.filterwarnings("ignore", message=".*ARC4.*")
import asyncio
import datetime as dt
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from memgpt.agent import Agent, now
from memgpt.config import ROOT, Config
from memgpt.ingest import ingest_papers
from memgpt.llm import OpenAILLM, ScriptedLLM
from memgpt.scenario import BURST_MESSAGES, QUICK_PROMPTS

PAUSE_TYPES_REPLAY = {"function_call", "flush_start", "alert"}


class Hub:
    """Broadcasts events to every open browser tab, keeps a history for page
    reloads, and records the run to JSONL for replay."""

    def __init__(self, log_path: Path | None):
        self.clients: set[WebSocket] = set()
        self.history: list[dict] = []
        self.seq = 0
        self.log = open(log_path, "a", encoding="utf-8") if log_path else None
        self.log_path = log_path

    async def publish(self, etype: str, data: dict, state: dict, record: bool = True) -> None:
        self.seq += 1
        event = {"seq": self.seq, "t": time.time(), "type": etype, "data": data, "state": state}
        self.history.append(event)
        if len(self.history) > 4000:
            self.history = self.history[-4000:]
        if self.log and record:
            self.log.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.log.flush()
        text = json.dumps(event, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                self.clients.discard(ws)


# --------------------------------------------------------------------------- #
class ReplayDriver:
    """Plays a recorded JSONL run through the same UI. Step mode pauses before
    every function call, alert and flush, exactly like the live agent."""

    def __init__(self, path: Path, hub: Hub):
        self.path = path
        self.hub = hub
        self.events = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.events = [e for e in self.events if e["type"] not in ("paused", "resumed")]
        self.step_mode = True
        self.speed = 1.0
        self._next = asyncio.Event()
        self.paused: dict | None = None
        self.last_state: dict = self._decorate(self.events[0]["state"] if self.events else {})
        self.restart_flag = False

    def _decorate(self, state: dict) -> dict:
        s = dict(state)
        s["mode"] = "replay"
        s["model"] = f"{state.get('model', '?')} · {self.path.name}"
        s["step_mode"] = self.step_mode
        s["paused"] = self.paused
        return s

    def next(self) -> None:
        self._next.set()

    def set_step_mode(self, on: bool) -> None:
        self.step_mode = on
        if not on:
            self._next.set()

    def snapshot(self) -> dict:
        return self._decorate(self.last_state)

    async def run(self) -> None:
        while True:
            self.restart_flag = False
            self.hub.history.clear()
            await self.hub.publish("replay_reset", {}, self._decorate(self.events[0]["state"] if self.events else {}), record=False)
            prev_t = None
            for ev in self.events:
                if self.restart_flag:
                    break
                if ev["type"] in PAUSE_TYPES_REPLAY and self.step_mode:
                    info = {"label": ev["type"], **{k: ev["data"].get(k) for k in ("name", "args", "thought") if k in ev["data"]}}
                    if ev["type"] == "flush_start":
                        info["label"] = "flush"
                        info["evict_ids"] = ev["data"].get("evict_ids")
                    self.paused = info
                    await self.hub.publish("paused", info, self._decorate(self.last_state), record=False)
                    self._next.clear()
                    await self._next.wait()
                    self.paused = None
                    if self.restart_flag:
                        break
                elif prev_t is not None:
                    await asyncio.sleep(min(max(ev["t"] - prev_t, 0.02), 0.3) / self.speed)  # the browser paces the playback
                prev_t = ev["t"]
                self.last_state = ev["state"]
                await self.hub.publish(ev["type"], ev["data"], self._decorate(ev["state"]), record=False)
            if not self.restart_flag:
                await self.hub.publish("replay_end", {}, self._decorate(self.last_state), record=False)
                self._next.clear()
                while not self.restart_flag:
                    await asyncio.sleep(0.2)


# --------------------------------------------------------------------------- #
def build_app(args) -> FastAPI:
    cfg = Config()
    runtime: dict = {}

    @asynccontextmanager
    async def lifespan(_app):
        runtime["task"] = asyncio.create_task(runtime["driver"].run() if runtime["driver"] else runtime["agent"].run())
        yield
        runtime["task"].cancel()

    app = FastAPI(title="MemGPT Visualizer", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def no_cache(request, call_next):
        # the UI is edited often while rehearsing: never serve a stale copy from the browser cache
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    replay_path = Path(args.replay) if args.replay else None
    mode = "replay" if replay_path else ("scripted" if (args.scripted or not cfg.api_key) else "live")

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    log_path = None if mode == "replay" else cfg.runs_dir / f"run-{dt.datetime.now():%Y%m%d-%H%M%S}-{mode}.jsonl"
    hub = Hub(log_path)
    state = {"burst_i": 0}

    if mode == "replay":
        driver = ReplayDriver(replay_path, hub)
        agent = None
    else:
        llm = ScriptedLLM(cfg) if mode == "scripted" else OpenAILLM(cfg)
        agent_holder: dict = {}

        async def emit(etype: str, data: dict) -> None:
            await hub.publish(etype, data, agent_holder["a"].snapshot())

        agent = Agent(cfg, llm, emit)
        agent_holder["a"] = agent
        driver = None
        if not args.no_ingest:
            n, names = ingest_papers(agent.archival, cfg.papers_dir, now())
            if n:
                print(f"  archival: nạp {n} đoạn từ {', '.join(names)}")

    print(f"\n  MemGPT Visualizer — chế độ {mode.upper()}"
          + (f" · model {cfg.model}" if mode == "live" else "")
          + f" · context {cfg.context_limit} token")
    if log_path:
        try:
            shown = log_path.relative_to(ROOT)
        except ValueError:
            shown = log_path
        print(f"  ghi log: {shown}")
    print(f"  mở trình duyệt: http://{args.host}:{args.port}\n")

    runtime["driver"], runtime["agent"] = driver, agent

    @app.get("/api/system")
    async def system_prompt():
        """Static part of the prompt (instructions + function schemas), for the UI tooltip."""
        from memgpt.prompts import instructions_for, tools_for

        def pack(baseline: bool) -> dict:
            tools = tools_for(baseline)
            return {
                "instructions": instructions_for(baseline),
                "tools": [f"{t['function']['name']}: {t['function']['description']}" for t in tools],
                "tools_json": json.dumps(tools, ensure_ascii=False, indent=1),
            }

        return {"memgpt": pack(False), "baseline": pack(True)}

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "static" / "index.html")

    def current_state() -> dict:
        return driver.snapshot() if driver else agent.snapshot()

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        hub.clients.add(ws)
        await ws.send_text(json.dumps({
            "type": "hello",
            "state": current_state(),
            "history": hub.history[-1500:],
            "quick_prompts": QUICK_PROMPTS,
            "burst_total": len(BURST_MESSAGES),
            "burst_next": state["burst_i"],
        }, ensure_ascii=False))
        try:
            while True:
                cmd = json.loads(await ws.receive_text())
                t = cmd.get("type")
                target = driver or agent
                if t == "next":
                    target.next()
                elif t == "set_step":
                    target.set_step_mode(bool(cmd.get("value")))
                    await hub.publish("config", {"step_mode": target.step_mode}, current_state(), record=False)
                elif driver:
                    if t == "restart_replay":
                        driver.restart_flag = True
                        driver.next()
                    elif t == "speed":
                        driver.speed = float(cmd.get("value", 1.0))
                    continue
                elif t == "user_message" and cmd.get("text", "").strip():
                    await agent.events.put({"type": "user_message", "text": cmd["text"].strip()})
                elif t == "burst":
                    for _ in range(max(1, min(int(cmd.get("n", 1)), 6))):
                        msg = BURST_MESSAGES[state["burst_i"] % len(BURST_MESSAGES)]
                        state["burst_i"] += 1
                        await agent.events.put({"type": "user_message", "text": msg})
                    await hub.publish("config", {"burst_next": state["burst_i"]}, current_state(), record=False)
                elif t == "set_baseline":
                    agent.baseline = bool(cmd.get("value"))
                    await hub.publish("config", {"baseline": agent.baseline}, current_state())
                elif t in ("new_session", "reset"):
                    if agent.busy:
                        await ws.send_text(json.dumps({"type": "toast", "data": {"text": "Agent đang chạy — đợi xong lượt này đã."}}))
                        continue
                    if t == "reset":
                        state["burst_i"] = 0
                        await agent.reset()
                    else:
                        await agent.new_session()
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(ws)

    return app


def main() -> None:
    p = argparse.ArgumentParser(description="MemGPT Visualizer")
    p.add_argument("--scripted", action="store_true", help="dùng LLM mô phỏng bằng luật (không cần API key)")
    p.add_argument("--replay", metavar="FILE", help="phát lại một file log .jsonl")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-ingest", action="store_true", help="không nạp các file PDF vào archival")
    args = p.parse_args()
    uvicorn.run(build_app(args), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
