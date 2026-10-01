/* MemGPT Visualizer — front-end.
 *
 * Every server event carries a full state snapshot, so rendering is
 * idempotent. Events are NOT applied immediately: a small "director" queues
 * them and plays them one by one, holding each step on screen long enough to
 * follow (Tốc độ: Chậm / Vừa / Nhanh / Tức thì), zooming into the component
 * that is acting and narrating what happens in one sentence.
 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const C = {};
function loadColors() {
  for (const k of ["system", "working", "user", "assistant", "tool", "alert", "summary", "recall", "archival", "danger", "ok"]) C[k] = css(`--${k}`);
}

const S = {
  ws: null,
  state: null,
  animate: true,
  lastPrompt: null,
  quick: [],
  knownIds: new Set(),
  doomed: new Set(),
  diffs: {},
  freshArch: new Set(),
  lastFnCard: null,
  wireTimers: {},
  burstNext: 0,
  burstTotal: 6,
};

/* ---------------------------------------------------------------- director */
const D = { q: [], busy: false, pace: 1, skip: false };
try {
  const saved = localStorage.getItem("memgpt-pace");
  if (saved !== null) D.pace = parseFloat(saved);
} catch (_) {}

// how long (ms, at pace 1) each step stays on screen before the next one plays
const DWELL = {
  chat_user: 1900,
  chat_assistant: 2200,
  queue_append: 350,
  llm_call: 1700,
  function_call: 2800,
  function_result: 1500,
  memory_update: 3400,
  archival_insert: 2800,
  page_in: 3200,
  alert: 3600,
  flush_start: 1800,
  flush: 3800,
  thought_only: 2400,
  session: 2200,
  error: 2000,
};
function dwellOf(ev) {
  if (ev.type === "chat") return DWELL[`chat_${ev.data.role}`] || 0;
  return DWELL[ev.type] || 0;
}
function enqueue(ev) {
  D.q.push(ev);
  updateQueueBadge();
  if (!D.busy) pump();
}
async function pump() {
  D.busy = true;
  while (D.q.length) {
    const ev = D.q.shift();
    updateQueueBadge();
    handle(ev);
    const ms = D.skip ? 0 : dwellOf(ev) * D.pace;
    if (ms > 0) await sleep(ms);
  }
  D.busy = false;
  D.skip = false;
  updateQueueBadge();
  if (!(S.state && S.state.paused)) setTimeout(() => { if (!D.busy) unfocus(); }, 1600 * Math.max(D.pace, 0.3));
}
function updateQueueBadge() {
  const n = D.q.length;
  $("#nQueue").textContent = n ? `còn ${n} bước đang chờ phát` : "";
  $("#skipBtn").disabled = n === 0;
}

/* ------------------------------------------------------------------ socket */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  S.ws = ws;
  ws.onopen = () => $("#connDot").classList.add("on");
  ws.onclose = () => {
    $("#connDot").classList.remove("on");
    setTimeout(connect, 1500);
  };
  ws.onmessage = (m) => onEvent(JSON.parse(m.data));
}
function send(obj) {
  if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(obj));
}

function onEvent(ev) {
  if (ev.type === "hello") {
    D.q = [];
    clearUI();
    S.quick = ev.quick_prompts || [];
    S.burstTotal = ev.burst_total || 6;
    S.burstNext = ev.burst_next || 0;
    renderQuick();
    S.animate = false;
    for (const e of ev.history || []) handle(e);
    S.animate = true;
    unfocus();
    render(ev.state);
    narrate("●", "Sẵn sàng. Gửi một tin nhắn hoặc bấm một câu gợi ý ở khung hội thoại.", "#94a3b8");
    return;
  }
  if (ev.type === "toast") {
    toast(ev.data.text, "info");
    return;
  }
  if (ev.type === "config" && ev.data.step_mode !== undefined && S.state) {
    // toggling Step must feel instant, not wait behind the animation queue
    S.state.step_mode = ev.data.step_mode;
    $("#stepToggle").checked = !!ev.data.step_mode;
  }
  enqueue(ev);
}

/* ------------------------------------------------------------ focus & narration */
let focusEl = null;
function focus(sel, color, tag) {
  if (!S.animate) return;
  const el = typeof sel === "string" ? $(sel) : sel;
  if (!el) return;
  if (focusEl && focusEl !== el) clearFocus(focusEl);
  focusEl = el;
  el.style.setProperty("--fc", color);
  el.classList.add("focus");
  el.querySelectorAll(":scope > .focus-tag").forEach((t) => t.remove());
  if (tag) {
    const t = document.createElement("div");
    t.className = "focus-tag";
    t.textContent = tag;
    el.appendChild(t);
  }
  document.body.classList.add("focusing");
  scrollIntoViewIfNeeded(el);
}
function clearFocus(el) {
  el.classList.remove("focus");
  el.querySelectorAll(":scope > .focus-tag").forEach((t) => t.remove());
}
function unfocus() {
  if (focusEl) clearFocus(focusEl);
  focusEl = null;
  document.body.classList.remove("focusing");
  $("#chatPanel").classList.remove("focus-chat");
}
function scrollIntoViewIfNeeded(el) {
  // visible stage = below the sticky header, left of the fixed chat panel
  if (el.closest(".chat")) return;
  const r = el.getBoundingClientRect();
  const top = $(".sticky-top").getBoundingClientRect().bottom + 16;
  const bottom = window.innerHeight - 16;
  const left = 16;
  const right = window.innerWidth - $("#chatPanel").offsetWidth - 16;
  let dx = 0, dy = 0;
  if (r.top < top || r.bottom > bottom) dy = r.top - top - Math.max(0, (bottom - top - r.height) / 2);
  if (r.left < left || r.right > right) dx = r.left - left - Math.max(0, (right - left - r.width) / 2);
  if (dx || dy) window.scrollBy({ left: dx, top: dy, behavior: "smooth" });
}
function narrate(icon, html, color, cls = "") {
  if (!S.animate) return;
  const n = $("#narrator");
  n.className = `narrator ${cls}`;
  n.style.setProperty("--nc", color);
  $("#nIcon").textContent = icon;
  $("#nText").innerHTML = html;
  void n.offsetWidth;
  n.classList.add("enter");
}
function chatFocus() {
  if (!S.animate) return;
  $("#chatPanel").classList.add("focus-chat");
  setTimeout(() => $("#chatPanel").classList.remove("focus-chat"), 2000 * Math.max(D.pace, 0.4));
}

/* ------------------------------------------------------------------ events */
function handle(ev) {
  const d = ev.data || {};
  const st = ev.state;
  const kind = (name) => fnKind(name);
  switch (ev.type) {
    case "replay_reset":
      clearUI();
      break;
    case "chat":
      addChat(d.role, d.text);
      if (d.role === "user") {
        addTL({ title: "👤 user", sub: d.text, color: C.user, ev });
        narrate("👤", "Người dùng gửi tin nhắn. Queue manager sẽ thêm nó vào cuối <b>FIFO queue</b> và ghi vào <b>recall storage</b>.", C.user);
      } else {
        chatFocus();
        narrate("💬", "Tin nhắn hiện ra cho người dùng — đây là thứ <b>duy nhất</b> người dùng thấy; mọi thao tác bộ nhớ ở trên đều ẩn.", C.assistant);
      }
      break;
    case "queue_append":
      pulse("qm-bar", C.tool, false, 900);
      pulse("qm-recall", C.recall, false, 900);
      if (d.msg && d.msg.kind === "user") {
        if (st) render(st);
        focus("#ctxBar", C.user, "FIFO += tin nhắn mới");
        hlSeg(d.msg.id);
        return;
      }
      break;
    case "llm_call": {
      S.lastPrompt = { prompt: d.prompt, step: d.step, tools: d.tools || [] };
      pulse("bar-llm", "#334155", false, 1800);
      focus("#llmCard", "#334155", `Bước #${d.step} · suy luận`);
      const used = st ? `${st.used}/${st.L} token (${st.pct}%)` : "";
      narrate("🧠", `LLM đọc toàn bộ <b>main context</b> — ${d.n_messages} message, ${used} — rồi quyết định gọi hàm nào.`, "#334155");
      if (!$("#drawer").classList.contains("hidden")) renderDrawer();
      break;
    }
    case "function_call":
      setThought(d.thought, d.step, d.heartbeat, d.name);
      setFE(d);
      pulse("llm-fe", fnColor(d.name), false, 2400);
      pulse("qm-recall", C.recall, false, 900);
      S.lastFnCard = addTL({ top: `#${d.step}`, title: d.name, sub: d.thought || argsLine(d.args), color: fnColor(d.name), ev });
      focus("#feCard", fnColor(d.name), `LLM gọi ${d.name}()`);
      narrate("ƒ", explainCall(d), fnColor(d.name));
      if (d.warn) toast(d.warn, "info");
      break;
    case "thought_only":
      setThought(d.thought, d.step, false, null);
      addTL({ top: `#${d.step}`, title: "💭 chỉ suy nghĩ", sub: d.thought, color: "#64748b", ev });
      focus("#llmCard", "#64748b", "Không làm gì");
      narrate("💭", "LLM thấy không có gì quan trọng cần lưu thêm nên bỏ qua cảnh báo này và nhường lượt.", "#64748b");
      break;
    case "paused":
      showPause(d);
      if (d.label === "flush") (d.evict_ids || []).forEach((id) => S.doomed.add(id));
      break;
    case "resumed":
      $("#feCard").classList.remove("waiting");
      if (S.lastPause && S.lastPause.label === "function_call") {
        const nm = S.lastPause.name || "";
        narrate("▶", `Function executor thực thi <code>${esc(nm)}()</code>…`, fnColor(nm));
        $("#feStatus").textContent = "đang chạy";
        $("#feStatus").className = "pill";
      } else if (S.lastPause && S.lastPause.label === "flush") {
        narrate("▶", "Queue manager đang evict và tóm tắt các tin cũ…", C.danger, "flushing");
      }
      break;
    case "function_result":
      setFEResult(d);
      if (S.lastFnCard) {
        const extra = d.ok ? (typeof d.result === "string" ? "✓ " + d.result.split("\n")[0] : "✓ OK") : "✖ " + String(d.result);
        S.lastFnCard.querySelector(".tl-sub").textContent = extra;
        if (!d.ok) S.lastFnCard.style.setProperty("--c", C.danger);
      }
      if (!d.ok) {
        addNote(`✖ ${d.name} lỗi: ${d.result}`, "red");
        focus("#feCard", C.danger, "Lỗi được trả lại cho LLM");
        narrate("✖", `Hàm <code>${esc(d.name)}</code> lỗi: ${esc(d.result)}. Lỗi được đưa lại vào context để LLM tự sửa (§2.3).`, C.danger);
      }
      break;
    case "memory_update": {
      S.diffs[d.label] = { before: d.before, after: d.after, until: Date.now() + 12000 * Math.max(D.pace, 0.5) };
      pulse("fe-work", C.working, false, 2600);
      chip("#feCard", "#workCard", `core_memory_${d.op}`, C.working);
      focus("#workCard", C.working, d.op === "replace" ? "Sửa fact cũ (replace)" : "Ghi fact mới (append)");
      const what = d.op === "replace" ? "<b>thay</b> một dòng đã lỗi thời (dòng đỏ → dòng xanh)" : "<b>ghi thêm</b> fact mới (dòng xanh)";
      narrate("✍", `Working context (block <code>${esc(d.label)}</code>) vừa được ${what}. Không ai ra lệnh — chính LLM quyết định điều gì đáng nhớ.`, C.working);
      addNote(`🧠 Working context (${d.label}) được ${d.op === "replace" ? "sửa (replace)" : "ghi thêm (append)"}`, "blue");
      setTimeout(() => S.state && render(S.state), 12100 * Math.max(D.pace, 0.5));
      break;
    }
    case "archival_insert":
      S.freshArch.add(d.id);
      pulse("fe-arch", C.archival, false, 2600);
      chip("#feCard", "#archCard", "archival_memory_insert", C.archival);
      focus("#archCard", C.archival, "Ghi vào archival");
      narrate("📦", "LLM ghi một memory vào <b>archival storage</b> — kho không giới hạn nằm ngoài context; muốn dùng lại phải search.", C.archival);
      addNote("📦 LLM ghi một memory vào archival storage", "orange");
      break;
    case "page_in": {
      const isRecall = d.store === "recall";
      const store = isRecall ? "#recallCard" : "#archCard";
      const color = isRecall ? C.recall : C.archival;
      pulse(isRecall ? "fe-recall" : "fe-arch", color, true, 3000);
      chip(store, "#ctxBar", `page-in: ${d.shown}/${d.total} kết quả`, color, "bottom");
      focus(store, color, `Search: "${d.query}"`);
      narrate("🔎", `<b>Page-in:</b> tìm <code>${esc(d.query)}</code> trong ${isRecall ? "recall storage" : "archival storage"} → ${d.total} kết quả; trang ${d.page + 1}/${d.pages} được đưa vào cuối FIFO để LLM đọc.`, color);
      addNote(`🔎 ${isRecall ? "conversation_search" : "archival_memory_search"}("${d.query}") → ${d.total} kết quả, trang ${d.page + 1}/${d.pages} được đưa vào context`, isRecall ? "sky" : "orange");
      break;
    }
    case "alert":
      pulse("qm-bar", C.alert, false, 3200);
      restartAnim("#tWarn", "pulse");
      setQM(`<span class="warn">⚠ Đã chèn cảnh báo memory pressure</span> — prompt đang dùng <b>${d.pct}%</b> context (${d.used} token). LLM có một cơ hội để lưu thông tin quan trọng.`);
      focus("#qmCard", C.alert, `Vượt ${Math.round((st ? st.warn : 0.7) * 100)}% → cảnh báo`);
      narrate("⚠", `Prompt đã dùng <b>${d.pct}%</b> context (vượt ngưỡng 70%). Queue manager chèn cảnh báo <b>memory pressure</b> — LLM được một cơ hội lưu thông tin trước khi tin cũ bị evict.`, C.alert);
      addNote(`⚠ Hệ thống → LLM: memory pressure ${d.pct}% (người dùng không thấy)`, "warn");
      addTL({ title: "⚠ memory pressure", sub: `${d.pct}% context · ${d.used} token`, color: C.alert, ev });
      break;
    case "flush_start":
      (d.evict_ids || []).forEach((id) => S.doomed.add(id));
      if (st) render(st);
      focus("#ctxBar", C.danger, "Sắp evict (viền đỏ)");
      narrate("⟲", `Prompt chạm <b>100%</b>. Queue manager chuẩn bị evict <b>${(d.evict_ids || []).length}</b> tin cũ nhất (viền đỏ) theo FIFO.`, C.danger, "flushing");
      return;
    case "flush":
      flyEvicted(d.evicted_ids || []);
      S.doomed.clear();
      pulse("qm-bar", C.danger, true, 3400);
      pulse("qm-recall", C.recall, false, 3400);
      restartAnim("#tFlush", "pulse");
      setQM(`<span class="red">⟲ Flush</span> — evict <b>${d.evicted_ids.length}</b> tin cũ nhất (${d.evicted_tokens} token, FIFO). Chúng vẫn nằm trong recall storage; recursive summary được cập nhật.`);
      focus("#qmCard", C.danger, "Flush xong");
      narrate("⟲", `Đã evict <b>${d.evicted_ids.length}</b> tin (${d.evicted_tokens} token). Chúng <b>không mất</b> — vẫn nằm trong recall storage; phần context còn lại mở đầu bằng <b>recursive summary</b> (tím).`, C.danger);
      addNote(`⟲ Flush: ${d.evicted_ids.length} tin cũ bị đẩy khỏi context → vẫn còn trong recall storage`, "red");
      addTL({ title: "⟲ flush", sub: `evict ${d.evicted_ids.length} tin · ${d.evicted_tokens} token → summary`, color: C.danger, ev });
      break;
    case "session":
      if (d.op === "reset") {
        clearUI();
        addNote("⌫ Reset: đã xóa toàn bộ bộ nhớ của agent", "red");
        narrate("⌫", "Reset: bộ nhớ của agent đã được xóa sạch để diễn lại từ đầu.", C.danger);
      } else {
        addNote("↻ Phiên mới: main context (FIFO) đã bị xóa — core / recall / archival vẫn giữ nguyên", "violet");
        addTL({ title: "↻ phiên mới", sub: "context trống, bộ nhớ dài hạn còn nguyên", color: C.summary, ev });
        setQM(`<span style="color:var(--summary);font-weight:700">↻ Phiên mới</span> — FIFO queue trống. Working context, recall và archival vẫn còn nguyên.`);
        if (st) render(st);
        focus("#workCard", C.summary, "Vẫn còn sau phiên mới");
        narrate("↻", "<b>Phiên mới:</b> toàn bộ tin nhắn trong context đã biến mất, nhưng working context, recall và archival vẫn còn nguyên — đó là bộ nhớ dài hạn.", C.summary);
      }
      setFE(null);
      setThought(null);
      break;
    case "config":
      if (d.burst_next !== undefined) S.burstNext = d.burst_next;
      break;
    case "error":
      toast(d.message);
      addTL({ title: "✖ lỗi", sub: d.message, color: C.danger, ev });
      narrate("✖", `Lỗi: ${esc(d.message)}`, C.danger);
      break;
    case "idle":
      break;
    case "replay_end":
      addNote("■ Hết bản ghi — bấm “Phát lại từ đầu” để xem lại", "violet");
      narrate("■", "Hết bản ghi. Bấm <b>Phát lại từ đầu</b> để xem lại.", C.summary);
      break;
  }
  if (st) render(st);
}

function explainCall(d) {
  const hb = d.heartbeat ? ` <span style="color:#db2777">♥ heartbeat</span>: chạy tiếp ngay sau khi hàm trả về.` : "";
  const th = d.thought ? `<br><span style="font-weight:400;color:var(--muted);font-style:italic">“${esc(d.thought)}”</span>` : "";
  const a = d.args || {};
  let what;
  switch (d.name) {
    case "send_message": what = "LLM gọi <code>send_message</code> — cách duy nhất để nói với người dùng."; break;
    case "core_memory_append": what = `LLM tự quyết định <b>ghi thêm</b> vào working context (block <code>${esc(a.label)}</code>).`; break;
    case "core_memory_replace": what = `LLM tự quyết định <b>sửa</b> một fact trong working context (block <code>${esc(a.label)}</code>).`; break;
    case "archival_memory_insert": what = "LLM tự quyết định <b>lưu</b> thông tin vào archival storage."; break;
    case "archival_memory_search": what = `LLM cần thông tin không có trong context → <b>tìm</b> <code>${esc(a.query)}</code> trong archival storage.`; break;
    case "conversation_search": what = `Thông tin đã bị evict khỏi context → LLM <b>tìm</b> <code>${esc(a.query)}</code> trong recall storage.`; break;
    default: what = `LLM gọi <code>${esc(d.name)}</code>.`;
  }
  return what + hb + th;
}

/* ------------------------------------------------------------------ render */
function render(st) {
  S.state = st;
  const mode = st.mode || "scripted";
  document.body.classList.toggle("mode-replay", mode === "replay");
  const badge = $("#modeBadge");
  badge.className = `badge ${mode}`;
  badge.textContent = { live: "LIVE LLM", scripted: "SCRIPTED", replay: "REPLAY" }[mode] || mode;
  badge.title = {
    live: "Agent gọi LLM thật qua API",
    scripted: "Hành vi LLM được mô phỏng bằng luật để tập dượt — queue manager, hàm và bộ nhớ vẫn chạy thật",
    replay: "Đang phát lại một bản ghi",
  }[mode];
  $("#modelName").textContent = `${st.model || ""} · ${st.tokenizer || ""}`;
  $("#stepToggle").checked = !!st.step_mode;
  $("#baselineToggle").checked = !!st.baseline;
  $("#nextBtn").disabled = !st.paused;
  $("#burstBtn").textContent = `Burst +1 (${Math.min(S.burstNext, S.burstTotal)}/${S.burstTotal})`;

  const status = st.paused ? "waiting" : st.status || "idle";
  const labels = { idle: "idle", thinking: "đang suy luận…", executing: "đang thực thi", waiting: "chờ Next", summarizing: "đang tóm tắt" };
  $("#llmStatus").textContent = labels[status] || status;
  $("#llmCard").classList.toggle("thinking", status === "thinking" || status === "summarizing");

  renderBar(st);
  renderCore(st);
  renderStores(st);

  const summary = (st.queue || []).find((m) => m.kind === "summary");
  $("#qmSummary").innerHTML = summary ? `<b>Recursive summary</b> (queue[0]): ${esc(summary.preview)}` : "";
  $("#workCard").classList.toggle("dim", !!st.baseline);
  layoutWires();
  refreshTip();
}

function renderBar(st) {
  const bar = $("#ctxBar");
  const H = bar.clientHeight - 10;
  const L = st.L || 4000;
  const px = (t) => Math.max(4, (t / L) * H - 2);
  const seg = st.segments || { system: 0, working: 0, queue: 0 };
  const parts = [];
  parts.push(`<div class="seg system" data-tip="seg:system" style="height:${px(seg.system)}px"><span>System + functions · ${seg.system}</span></div>`);
  if (seg.working > 0)
    parts.push(`<div class="seg working" data-tip="seg:working" style="height:${px(seg.working)}px"><span>Working context · ${seg.working}</span></div>`);
  const nowIds = new Set();
  for (const m of st.queue || []) {
    nowIds.add(m.id);
    const cls = ["seg", m.kind];
    if (m.kind === "tool" && m.ok === false) cls.push("fail");
    if (!S.knownIds.has(m.id) && S.animate) cls.push("new");
    if (S.doomed.has(m.id)) cls.push("doomed");
    const h = px(m.tokens);
    const label = h >= 15 ? `<span>${esc(m.label)} · ${m.tokens}</span>` : "";
    if (TIP.key === `seg:${m.id}`) cls.push("tip-on");
    parts.push(`<div class="${cls.join(" ")}" data-id="${m.id}" data-tip="seg:${m.id}" style="height:${h}px">${label}</div>`);
  }
  $("#ctxFill").innerHTML = parts.join("");
  S.knownIds = nowIds;

  const pct = st.pct || 0;
  $("#usedTxt").textContent = st.used;
  $("#limitTxt").textContent = L;
  $("#pctTxt").textContent = `${pct}%`;
  $("#pctTxt").style.color = pct >= 100 ? C.danger : pct >= st.warn * 100 ? "#b45309" : "";
  bar.classList.toggle("warn", pct >= st.warn * 100 && pct < 100);
  bar.classList.toggle("full", pct >= 100);
  $("#tWarn").style.top = `${5 + H * st.warn}px`;
  $("#tWarn").querySelector("span").textContent = `${Math.round(st.warn * 100)}% · cảnh báo`;
  $("#tFlush").style.top = `${5 + H * st.flush}px`;
}
function hlSeg(id) {
  if (!S.animate) return;
  const seg = document.querySelector(`.seg[data-id="${id}"]`);
  if (!seg) return;
  seg.classList.add("hl");
  setTimeout(() => seg.classList.remove("hl"), 3200);
}

function renderCore(st) {
  for (const label of ["persona", "human"]) {
    const blk = (st.core || {})[label] || { value: "", limit: 1 };
    const value = blk.value || "";
    const ratio = value.length / blk.limit;
    $(`#chars-${label}`).textContent = `${value.length}/${blk.limit} ký tự`;
    const meter = $(`#meter-${label}`);
    meter.style.width = `${Math.min(100, ratio * 100)}%`;
    meter.classList.toggle("high", ratio > 0.85);
    const diff = S.diffs[label];
    const lines = value.split("\n").filter((l) => l.trim());
    let html = "";
    if (diff && diff.until > Date.now()) {
      const beforeLines = new Set(diff.before.split("\n"));
      const afterLines = new Set(diff.after.split("\n"));
      const removed = diff.before.split("\n").filter((l) => l.trim() && !afterLines.has(l));
      for (const l of lines) {
        if (!beforeLines.has(l) && removed.length) html += removed.splice(0).map((r) => `<div class="ln removed">${esc(r)}</div>`).join("");
        html += `<div class="ln ${beforeLines.has(l) ? "" : "added"}">${esc(l)}</div>`;
      }
      html += removed.map((r) => `<div class="ln removed">${esc(r)}</div>`).join("");
    } else {
      for (const l of lines) html += `<div class="ln">${esc(l)}</div>`;
    }
    $(`#body-${label}`).innerHTML = html || `<span class="muted">(trống)</span>`;
  }
}

const KIND_LABEL = { user: "user", assistant_message: "send_message", assistant: "function call", tool: "function result", alert: "system alert", summary: "summary" };
function recallText(r) {
  if (r.kind === "tool") {
    try {
      const j = JSON.parse(r.text.replace(/…$/, ""));
      return `[${j.status}] ${typeof j.message === "string" ? j.message : "(không có nội dung trả về)"}`;
    } catch (_) {}
  }
  return r.text;
}
function shortTime(ts) {
  return (ts || "").slice(11, 19);
}
function renderStores(st) {
  const rc = st.recall || { count: 0, recent: [] };
  $("#recallCount").textContent = rc.count;
  const inCtx = new Set((st.queue || []).map((m) => m.id));
  $("#recallList").innerHTML = (rc.recent || [])
    .map((r, i) => `<div class="store-item ${inCtx.has(r.msg_id) ? "" : "evicted"}" data-tip="recall:${i}"><em>${esc(KIND_LABEL[r.kind] || r.kind)} · ${shortTime(r.ts)}${inCtx.has(r.msg_id) ? "" : " · đã evict"}</em>${esc(recallText(r))}</div>`)
    .join("") + (rc.count > (rc.recent || []).length ? `<div class="store-more">… và ${rc.count - rc.recent.length} tin cũ hơn</div>` : "");
  const ar = st.archival || { count: 0, papers: 0, agent: 0, recent: [] };
  $("#archCount").textContent = ar.count;
  $("#archSub").innerHTML = `${ar.papers} đoạn từ paper · <b style="color:var(--archival)">${ar.agent}</b> do agent tự ghi`;
  $("#archList").innerHTML =
    (ar.recent || []).map((r, i) => `<div class="store-item ${S.freshArch.has(r.id) ? "fresh" : ""}" data-tip="arch:${i}"><em>agent · ${shortTime(r.ts)}</em>${esc(r.text)}</div>`).join("") +
    `<div class="store-item static"><em>paper</em>${ar.papers} đoạn trích từ MemGPT, CoALA, Generative Agents (nạp sẵn)</div>`;
  setTimeout(() => S.freshArch.clear(), 3000);
}

/* ---------------------------------------------------------------- tooltip */
const TIP = { key: null, pinned: false, hideTimer: null, system: null };
fetch("/api/system").then((r) => r.json()).then((j) => { TIP.system = j; }).catch(() => {});

const ROLE_NOTE = {
  user: "Trong prompt: message role=user, bọc JSON {type: \"user_message\", time}.",
  alert: "Trong prompt: message role=user, bọc JSON {type: \"system_alert\"} — do queue manager chèn, người dùng không thấy.",
  summary: "Trong prompt: FIFO[0], role=user, JSON system_alert — tóm tắt các tin đã bị evict.",
  assistant: "Trong prompt: message role=assistant — inner monologue + một tool call.",
  tool: "Trong prompt: message role=tool — kết quả hàm trả lại cho LLM.",
};
const KIND_COLOR = () => ({ system: C.system, working: C.working, user: C.user, assistant: C.assistant, tool: C.tool, alert: C.alert, summary: C.summary });

function systemParts(st) {
  const base = st && st.baseline ? "baseline" : "memgpt";
  if (TIP.system && TIP.system[base]) return TIP.system[base];
  const sys = S.lastPrompt && S.lastPrompt.prompt[0] && S.lastPrompt.prompt[0].content;
  if (sys) {
    const i = sys.indexOf("### Core memory");
    return { instructions: i >= 0 ? sys.slice(0, i) : sys, tools: S.lastPrompt.tools || [] };
  }
  return { instructions: "(chưa tải được system prompt)", tools: [] };
}
function tipData(key) {
  const st = S.state;
  if (!st || !key) return null;
  const [kind, id] = key.split(":");
  const col = KIND_COLOR();
  if (key === "seg:system") {
    const p = systemParts(st);
    const stats = st.baseline
      ? "### Memory statistics\n(no external memory available)"
      : `### Memory statistics\n${st.recall.count} previous messages between you and the user are stored in recall storage (use conversation_search).\n${st.archival.count} passages are stored in archival storage (use archival_memory_search).`;
    return {
      badge: "system", color: col.system, title: "System instructions + functions + memory stats",
      meta: `${st.segments.system} token · chỉ đọc (read-only)`,
      body: `${p.instructions.trim()}\n\n── Function schemas (${p.tools.length} hàm, gửi qua tham số tools) ──\n${p.tools.map((t) => "• " + t).join("\n")}\n\n${stats}`,
      mono: true,
      foot: "Người thiết kế viết phần này (procedural memory theo CoALA). Queue manager không bao giờ evict nó.",
    };
  }
  if (key === "seg:working") {
    const core = st.core || {};
    const blk = (l) => `<${l} characters="${(core[l].value || "").length}/${core[l].limit}">\n${core[l].value}\n</${l}>`;
    return {
      badge: "working", color: col.working, title: "Working context (core memory)",
      meta: `${st.segments.working} token · luôn nằm trong prompt`,
      body: `### Core memory (working context) — always visible, edit only via functions\n${blk("persona")}\n${blk("human")}`,
      mono: true,
      foot: "Chỉ LLM được sửa, qua core_memory_append / core_memory_replace. Không bị evict.",
    };
  }
  if (kind === "seg") {
    const m = (st.queue || []).find((x) => String(x.id) === id);
    if (!m) return { badge: "evicted", color: C.danger, title: "Tin này đã bị evict khỏi context", meta: "", body: "Nó vẫn nằm nguyên văn trong recall storage.", foot: "" };
    return {
      badge: m.kind, color: col[m.kind] || C.tool, title: m.label,
      meta: `${m.tokens} token · ${shortTime(m.ts)} · #${m.id}`,
      body: m.full || m.preview, mono: m.kind === "assistant" || m.kind === "tool",
      foot: (ROLE_NOTE[m.kind] || "") + " Bấm để ghim.",
    };
  }
  if (kind === "recall") {
    const r = ((st.recall || {}).recent || [])[+id];
    if (!r) return null;
    const inCtx = (st.queue || []).some((m) => m.id === r.msg_id);
    return {
      badge: KIND_LABEL[r.kind] || r.kind, color: C.recall, title: "Recall storage",
      meta: `${r.ts} · #${r.msg_id}`, body: recallText(r), mono: r.kind === "assistant" || r.kind === "tool",
      foot: (inCtx ? "Tin này vẫn đang nằm trong main context." : "Tin này đã ra khỏi main context — LLM chỉ đọc lại được bằng conversation_search.") + " Bấm để ghim.",
    };
  }
  if (kind === "arch") {
    const r = ((st.archival || {}).recent || [])[+id];
    if (!r) return null;
    return { badge: "agent memory", color: C.archival, title: "Archival storage", meta: `${r.ts} · #${r.id}`, body: r.text, foot: "Do LLM tự ghi bằng archival_memory_insert; đọc lại bằng archival_memory_search. Bấm để ghim." };
  }
  return null;
}
function showTip(key, anchor, pin = false) {
  const d = tipData(key);
  if (!d) return;
  clearTimeout(TIP.hideTimer);
  document.querySelectorAll(".tip-on").forEach((e) => e.classList.remove("tip-on"));
  TIP.key = key;
  TIP.pinned = pin;
  const tip = $("#tip");
  tip.style.setProperty("--tc", d.color);
  tip.classList.toggle("pinned", pin);
  tip.innerHTML = `<div class="tip-head"><span class="tip-badge">${esc(d.badge)}</span><b>${esc(d.title)}</b><span class="tip-meta">${esc(d.meta)}</span>${pin ? '<button class="ghost icon tip-close" aria-label="Đóng">✕</button>' : ""}</div>
    <div class="tip-body ${d.mono ? "mono" : ""}">${esc(d.body)}</div>${d.foot ? `<div class="tip-foot">${esc(d.foot)}</div>` : ""}`;
  const x = tip.querySelector(".tip-close");
  if (x) x.addEventListener("click", () => hideTip(true));
  tip.classList.remove("hidden");
  if (anchor) {
    anchor.classList.add("tip-on");
    placeTip(anchor);
  }
}
function placeTip(anchor) {
  const tip = $("#tip");
  const r = anchor.getBoundingClientRect();
  const w = tip.offsetWidth, h = tip.offsetHeight;
  const top0 = $(".sticky-top").getBoundingClientRect().bottom + 8;
  const rightLimit = window.innerWidth - $("#chatPanel").offsetWidth - 8;
  let left = r.right + 14;
  if (left + w > rightLimit) left = Math.max(8, r.left - w - 14);
  let top = Math.min(Math.max(top0, r.top + r.height / 2 - h / 2), window.innerHeight - h - 8);
  tip.style.left = `${left}px`;
  tip.style.top = `${Math.max(top0, top)}px`;
}
function hideTip(force = false) {
  if (TIP.pinned && !force) return;
  TIP.key = null;
  TIP.pinned = false;
  $("#tip").classList.add("hidden");
  document.querySelectorAll(".tip-on").forEach((e) => e.classList.remove("tip-on"));
}
function refreshTip() {
  if (!TIP.key) return;
  const anchor = document.querySelector(`[data-tip="${TIP.key}"]`);
  if (!anchor && !TIP.pinned) return hideTip();
  const d = tipData(TIP.key);
  if (!d) return hideTip(true);
  const body = $("#tip .tip-body");
  if (body) body.textContent = d.body;
  const meta = $("#tip .tip-meta");
  if (meta) meta.textContent = d.meta;
  if (anchor) anchor.classList.add("tip-on");
}
function initTooltip() {
  for (const sel of ["#ctxFill", "#recallList", "#archList"]) {
    const box = $(sel);
    box.addEventListener("mouseover", (e) => {
      const a = e.target.closest("[data-tip]");
      if (!a || TIP.pinned) return;
      showTip(a.dataset.tip, a, false);
    });
    box.addEventListener("mouseout", (e) => {
      const a = e.target.closest("[data-tip]");
      if (!a || TIP.pinned) return;
      if (e.relatedTarget && (a.contains(e.relatedTarget) || $("#tip").contains(e.relatedTarget))) return;
      TIP.hideTimer = setTimeout(() => hideTip(), 280);
    });
    box.addEventListener("click", (e) => {
      const a = e.target.closest("[data-tip]");
      if (!a) return;
      e.stopPropagation();
      if (TIP.pinned && TIP.key === a.dataset.tip) return hideTip(true);
      showTip(a.dataset.tip, a, true);
    });
  }
  const tip = $("#tip");
  tip.addEventListener("mouseenter", () => clearTimeout(TIP.hideTimer));
  tip.addEventListener("mouseleave", () => { if (!TIP.pinned) TIP.hideTimer = setTimeout(() => hideTip(), 280); });
  document.addEventListener("click", (e) => { if (TIP.pinned && !tip.contains(e.target)) hideTip(true); });
  window.addEventListener("scroll", () => { if (!TIP.pinned) hideTip(); }, { passive: true });
}

/* --------------------------------------------------------------- cards */
function fnKind(name) {
  if (!name) return "other";
  if (name === "send_message") return "send";
  if (name.startsWith("core_memory")) return "core";
  if (name.startsWith("archival")) return "archival";
  if (name === "conversation_search") return "recall";
  return "other";
}
function fnColor(name) {
  return { send: C.assistant, core: C.working, archival: C.archival, recall: C.recall, other: "#64748b" }[fnKind(name)];
}
function argsLine(args) {
  const a = { ...(args || {}) };
  delete a.request_heartbeat;
  return Object.entries(a).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
}
function setThought(thought, step, heartbeat, name) {
  $("#llmThought").innerHTML = thought ? `“${esc(thought)}”` : `<span class="muted">(không có inner monologue)</span>`;
  $("#llmMeta").textContent = step ? `bước #${step}${name ? " · quyết định gọi " + name : ""}` : "";
}
function setFE(d) {
  const card = $("#feCard");
  card.classList.remove("failed", "waiting");
  if (!d) {
    $("#feFn").innerHTML = `<span class="muted">Chưa có function call.</span>`;
    $("#feArgs").textContent = "";
    $("#feHint").textContent = "";
    $("#feStatus").textContent = "—";
    $("#feStatus").className = "pill";
    return;
  }
  const hb = d.heartbeat ? `<span class="hb" title="request_heartbeat=true: chạy tiếp ngay sau khi hàm trả về">♥ heartbeat</span>` : "";
  $("#feFn").innerHTML = `${esc(d.name)}()${hb}`;
  $("#feFn").style.color = fnColor(d.name);
  const a = { ...(d.args || {}) };
  delete a.request_heartbeat;
  $("#feArgs").textContent = Object.entries(a).map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join("\n");
  $("#feStatus").textContent = "đang chạy";
  $("#feStatus").className = "pill";
  $("#feHint").textContent = "";
}
function setFEResult(d) {
  const card = $("#feCard");
  card.classList.remove("waiting");
  card.classList.toggle("failed", !d.ok);
  $("#feStatus").textContent = d.ok ? "OK" : "Failed";
  $("#feStatus").className = `pill ${d.ok ? "ok" : "fail"}`;
  let hint = "";
  if (!d.ok) hint = String(d.result);
  else if (d.name === "send_message") hint = "→ tin nhắn đã hiện cho người dùng";
  else if (typeof d.result === "string") hint = d.result.split("\n")[0];
  else hint = "→ bộ nhớ đã được cập nhật";
  $("#feHint").textContent = hint;
}
function setQM(html) {
  $("#qmLast").innerHTML = html;
}
function showPause(p) {
  S.lastPause = p;
  $("#nextBtn").disabled = false;
  if (S.state) S.state.paused = p;
  if (p.label === "flush") {
    const n = (p.evict_ids || []).length;
    if (S.state) render(S.state);
    focus("#ctxBar", C.danger, `Sắp evict ${n} tin`);
    narrate("⏸", `Queue manager sắp <b>flush</b>: evict ${n} tin cũ nhất (viền đỏ, FIFO). Nhấn <kbd>→</kbd> hoặc <b>Next</b> để tiếp tục.`, C.danger, "paused flushing");
  } else if (p.label === "alert") {
    focus("#qmCard", C.alert, "Sắp chèn cảnh báo");
    narrate("⏸", `Queue manager sắp chèn cảnh báo <b>memory pressure</b>. Nhấn <kbd>→</kbd> để tiếp tục.`, C.alert, "paused");
  } else {
    // in replay the pause arrives before the call itself: show the pending call now
    const args = p.args || {};
    setFE({ name: p.name, args, heartbeat: args.request_heartbeat === true || args.request_heartbeat === "true" });
    if (p.thought !== undefined) setThought(p.thought, null, false, p.name);
    $("#feCard").classList.add("waiting");
    $("#feStatus").textContent = "chờ Next";
    $("#feStatus").className = "pill wait";
    $("#feHint").textContent = "⏸ Đang chờ bạn bấm Next (→)";
    focus("#feCard", "#2563eb", `Chờ Next: ${p.name || ""}()`);
    const th = p.thought ? `<br><span style="font-weight:400;color:var(--muted);font-style:italic">“${esc(p.thought)}”</span>` : "";
    narrate("⏸", `LLM muốn gọi <code>${esc(p.name || "")}</code>. Giải thích xong thì nhấn <kbd>→</kbd> hoặc <b>Next</b> để thực thi.${th}`, "#2563eb", "paused");
  }
}
function restartAnim(sel, cls) {
  const el = $(sel);
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
}

/* ------------------------------------------------------------------- chat */
function addChat(role, text) {
  const div = document.createElement("div");
  div.className = `msg ${role}${S.animate ? " fresh" : ""}`;
  div.textContent = text;
  if (role === "user" && text.length > 260) {
    div.classList.add("long");
    div.addEventListener("click", () => div.classList.toggle("open"));
  }
  appendChat(div);
}
function addNote(text, tone = "") {
  const div = document.createElement("div");
  div.className = `note ${tone}`;
  div.textContent = text;
  appendChat(div);
}
function appendChat(el) {
  const log = $("#chatLog");
  log.appendChild(el);
  while (log.children.length > 400) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}
function renderQuick() {
  const box = $("#quick");
  box.innerHTML = "";
  for (const q of S.quick) {
    const b = document.createElement("button");
    b.type = "button";
    b.innerHTML = `<b>${esc(q.scene)}</b>${esc(q.label)}`;
    b.title = q.text + "\n\n(bấm để điền vào ô nhập, Shift+bấm để gửi luôn)";
    b.addEventListener("click", (e) => {
      if (e.shiftKey) {
        send({ type: "user_message", text: q.text });
      } else {
        $("#chatInput").value = q.text;
        $("#chatInput").focus();
      }
    });
    box.appendChild(b);
  }
}

/* --------------------------------------------------------------- timeline */
function addTL({ top = "", title, sub = "", color, ev }) {
  const card = document.createElement("div");
  card.className = "tl-card";
  card.style.setProperty("--c", color || "#94a3b8");
  const time = ev && ev.t ? new Date(ev.t * 1000).toLocaleTimeString("vi-VN", { hour12: false }) : "";
  card.innerHTML = `<div class="tl-top"><span>${esc(top)}</span><span>${time}</span></div><div class="tl-title">${esc(title)}</div><div class="tl-sub">${esc(sub)}</div>`;
  card.addEventListener("click", () => openDetail(title, ev));
  const track = $("#tl");
  track.querySelectorAll(".tl-card.current").forEach((c) => c.classList.remove("current"));
  card.classList.add("current");
  track.appendChild(card);
  while (track.children.length > 300) track.removeChild(track.firstChild);
  track.scrollLeft = track.scrollWidth;
  return card;
}
function openDetail(title, ev) {
  if (!ev) return;
  const d = { ...(ev.data || {}) };
  delete d.prompt;
  const rows = [];
  if (d.thought) rows.push(["inner monologue", esc(d.thought)]);
  if (d.name) rows.push(["function", esc(d.name)]);
  if (d.args) rows.push(["arguments", `<pre>${esc(JSON.stringify(d.args, null, 2))}</pre>`]);
  if (d.text) rows.push(["text", esc(d.text)]);
  if (ev.state) rows.push(["context lúc đó", `${ev.state.used}/${ev.state.L} token (${ev.state.pct}%) · ${ev.state.queue.length} tin trong FIFO`]);
  const table = rows.length ? `<table>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</table>` : "";
  openModal(title, `${table}<p class="muted">Dữ liệu sự kiện <code>${esc(ev.type)}</code>:</p><pre>${esc(JSON.stringify(d, null, 2))}</pre>`);
}

/* ------------------------------------------------------------------ wires */
const WIRES = [
  { id: "bar-llm", from: ["#ctxBar", "right", 0.08], to: ["#llmCard", "left", 0.45], label: "prompt" },
  { id: "llm-fe", from: ["#llmCard", "right", 0.5], to: ["#feCard", "left", 0.5], label: "call" },
  { id: "fe-work", from: ["#feCard", "bottom", 0.5], to: ["#workCard", "top", 0.75], label: "core_memory_*" },
  { id: "fe-arch", from: ["#feCard", "right", 0.35], to: ["#archCard", "left", 0.3], label: "archival" },
  { id: "fe-recall", from: ["#feCard", "right", 0.8], to: ["#recallCard", "left", 0.25], label: "recall search" },
  { id: "qm-bar", from: ["#qmCard", "left", 0.5], to: ["#ctxBar", "right", 0.9], label: "append / evict" },
  { id: "qm-recall", from: ["#qmCard", "right", 0.5], to: ["#recallCard", "left", 0.8], label: "ghi mọi tin" },
];
// layout position relative to #arch, ignoring CSS transforms (so zooming a card does not move wires)
function layoutRect(el) {
  const arch = $("#arch");
  let x = 0, y = 0, n = el;
  while (n && n !== arch) {
    x += n.offsetLeft;
    y += n.offsetTop;
    n = n.offsetParent;
  }
  return { left: x, top: y, width: el.offsetWidth, height: el.offsetHeight };
}
function anchor(sel, side, t) {
  const r = layoutRect($(sel));
  if (side === "right") return [r.left + r.width, r.top + r.height * t];
  if (side === "left") return [r.left, r.top + r.height * t];
  if (side === "top") return [r.left + r.width * t, r.top];
  return [r.left + r.width * t, r.top + r.height];
}
const DIRS = { right: [1, 0], left: [-1, 0], top: [0, -1], bottom: [0, 1] };
function layoutWires() {
  const svg = $("#wires");
  if (!svg.querySelector("marker")) {
    let html = `<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z"/></marker></defs>`;
    for (const w of WIRES) html += `<path id="w-${w.id}" class="wire" marker-end="url(#arrow)"/><text id="l-${w.id}" class="wire-label" text-anchor="middle">${esc(w.label)}</text>`;
    svg.innerHTML = html;
  }
  for (const w of WIRES) {
    const [x1, y1] = anchor(w.from[0], w.from[1], w.from[2]);
    const [x2, y2] = anchor(w.to[0], w.to[1], w.to[2]);
    const dist = Math.max(30, Math.hypot(x2 - x1, y2 - y1) * 0.4);
    const [a, b] = DIRS[w.from[1]], [c, d] = DIRS[w.to[1]];
    const p = document.getElementById(`w-${w.id}`);
    p.setAttribute("d", `M${x1.toFixed(1)},${y1.toFixed(1)} C${(x1 + a * dist).toFixed(1)},${(y1 + b * dist).toFixed(1)} ${(x2 + c * dist).toFixed(1)},${(y2 + d * dist).toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}`);
    const mid = p.getPointAtLength(p.getTotalLength() / 2);
    const lbl = document.getElementById(`l-${w.id}`);
    lbl.setAttribute("x", mid.x.toFixed(1));
    lbl.setAttribute("y", (mid.y - 7).toFixed(1));
  }
}
function pulse(id, color, reverse = false, ms = 1500) {
  if (!S.animate) return;
  const p = document.getElementById(`w-${id}`);
  const l = document.getElementById(`l-${id}`);
  if (!p) return;
  for (const el of [p, l]) {
    el.style.setProperty("--wc", color);
    el.classList.add("active");
  }
  p.classList.toggle("rev", reverse);
  clearTimeout(S.wireTimers[id]);
  S.wireTimers[id] = setTimeout(() => {
    p.classList.remove("active", "rev");
    l.classList.remove("active");
  }, ms * Math.max(D.pace, 0.4));
}

/* ------------------------------------------------------------- animations */
function centerOf(sel, where = "center") {
  const r = $(sel).getBoundingClientRect();
  if (where === "bottom") {
    const fill = $("#ctxFill").getBoundingClientRect();
    return [r.left + r.width / 2, Math.min(fill.bottom, r.bottom - 12)];
  }
  return [r.left + r.width / 2, r.top + r.height / 2];
}
function animMs(base) {
  return base * Math.max(D.pace, 0.35);
}
function chip(fromSel, toSel, text, color, toWhere = "center") {
  if (!S.animate || D.skip || D.pace === 0) return;
  const [x1, y1] = centerOf(fromSel);
  const [x2, y2] = centerOf(toSel, toWhere);
  const el = document.createElement("div");
  el.className = "chip";
  el.textContent = text;
  el.style.background = color;
  document.body.appendChild(el);
  const w = el.offsetWidth / 2, h = el.offsetHeight / 2;
  el.animate(
    [
      { transform: `translate(${x1 - w}px, ${y1 - h}px) scale(.7)`, opacity: 0 },
      { transform: `translate(${x1 - w}px, ${y1 - h}px) scale(1.08)`, opacity: 1, offset: 0.18 },
      { transform: `translate(${x2 - w}px, ${y2 - h}px) scale(1)`, opacity: 1, offset: 0.82 },
      { transform: `translate(${x2 - w}px, ${y2 - h}px) scale(.85)`, opacity: 0 },
    ],
    { duration: animMs(2300), easing: "cubic-bezier(.45,.05,.3,1)" }
  ).onfinish = () => el.remove();
}
function flyEvicted(ids) {
  if (!S.animate || D.skip || D.pace === 0) return;
  const target = $("#recallCard").getBoundingClientRect();
  const tx = target.left + 40, ty = target.top + target.height / 2;
  ids.forEach((id, i) => {
    const seg = document.querySelector(`.seg[data-id="${id}"]`);
    if (!seg) return;
    const r = seg.getBoundingClientRect();
    const g = document.createElement("div");
    g.className = "ghost-seg";
    g.style.cssText = `width:${r.width}px;height:${r.height}px;background:${getComputedStyle(seg).backgroundColor}`;
    document.body.appendChild(g);
    g.animate(
      [
        { transform: `translate(${r.left}px, ${r.top}px)`, opacity: 1 },
        { transform: `translate(${r.left + 60}px, ${r.top}px)`, opacity: 1, offset: 0.25 },
        { transform: `translate(${tx}px, ${ty}px) scale(.3)`, opacity: 0.15 },
      ],
      { duration: animMs(2600), delay: animMs(110) * i, easing: "cubic-bezier(.5,0,.3,1)", fill: "backwards" }
    ).onfinish = () => g.remove();
  });
}

/* ---------------------------------------------------------- raw prompt */
function renderDrawer() {
  const body = $("#drawerBody");
  if (!S.lastPrompt) {
    body.innerHTML = `<p class="muted">Chưa có lời gọi LLM nào. Gửi một tin nhắn trước đã.</p>`;
    $("#drawerMeta").textContent = "";
    return;
  }
  const { prompt, step } = S.lastPrompt;
  $("#drawerMeta").textContent = `lần gọi LLM #${step} · ${prompt.length} message · đúng chuỗi gửi đi`;
  const blocks = [];
  for (const m of prompt) {
    if (m.role === "system") {
      const text = m.content;
      const iCore = text.indexOf("### Core memory");
      const iStats = text.indexOf("### Memory statistics");
      const instr = text.slice(0, iCore >= 0 ? iCore : iStats >= 0 ? iStats : text.length);
      blocks.push(pm("system · instructions (read-only)", C.system, instr));
      if (iCore >= 0) blocks.push(pm("system · working context", C.working, text.slice(iCore, iStats >= 0 ? iStats : text.length)));
      if (iStats >= 0) blocks.push(pm("system · memory stats", C.system, text.slice(iStats)));
      continue;
    }
    if (m.role === "user") {
      let color = C.user, label = "user";
      try {
        const j = JSON.parse(m.content);
        if (j.type === "system_alert") {
          const isSum = (j.message || "").includes("summary of the previous");
          color = isSum ? C.summary : C.alert;
          label = isSum ? "FIFO[0] · recursive summary" : "system alert";
        } else label = "user_message";
      } catch (_) {}
      blocks.push(pm(label, color, m.content));
    } else if (m.role === "assistant") {
      const calls = (m.tool_calls || []).map((c) => `${c.function.name}(${c.function.arguments})`).join("\n");
      blocks.push(pm("assistant", C.assistant, (m.content ? `inner monologue: ${m.content}\n` : "") + calls));
    } else if (m.role === "tool") {
      blocks.push(pm("tool (function result)", C.tool, m.content));
    }
  }
  if ((S.lastPrompt.tools || []).length)
    blocks.splice(1, 0, pm("tools · function schemas (gửi kèm, cũng chiếm token của phần System)", C.system, S.lastPrompt.tools.join("\n")));
  body.innerHTML = blocks.join("");
  body.scrollTop = body.scrollHeight;
}
function pm(label, color, text) {
  return `<div class="pm" style="--c:${color}"><div class="pm-head"><span>${esc(label)}</span><span>${text.length} ký tự</span></div><pre>${esc(text.trim())}</pre></div>`;
}

/* ---------------------------------------------------------------- misc UI */
function clearUI() {
  $("#chatLog").innerHTML = "";
  $("#tl").innerHTML = "";
  S.knownIds = new Set();
  S.doomed.clear();
  S.diffs = {};
  S.lastFnCard = null;
  setQM(`<span class="muted">Chưa có sự kiện.</span>`);
  setFE(null);
  setThought(null);
  unfocus();
}
let toastTimer = null;
function toast(text, kind = "") {
  const t = $("#toast");
  t.className = `toast ${kind}`;
  t.textContent = text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 4500);
}
function openModal(title, html) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = html;
  $("#modal").classList.remove("hidden");
}
function closeOverlays() {
  $("#modal").classList.add("hidden");
  $("#drawer").classList.add("hidden");
}
function toggleDrawer() {
  const d = $("#drawer");
  d.classList.toggle("hidden");
  if (!d.classList.contains("hidden")) renderDrawer();
}
function setPace(v) {
  D.pace = v;
  $("#paceSel").value = String(v);
  try { localStorage.setItem("memgpt-pace", String(v)); } catch (_) {}
}
function showHelp() {
  openModal(
    "Phím tắt & cách dùng",
    `<table>
      <tr><td>→ · Space · PageDown</td><td>Next: thực thi bước đang chờ (khi bật Step). Bút trình chiếu thường gửi phím này.</td></tr>
      <tr><td>S</td><td>Bật/tắt chế độ Step (dừng trước mỗi function call và trước flush)</td></tr>
      <tr><td>1 · 2 · 3 · 4</td><td>Tốc độ hiệu ứng: Chậm · Vừa · Nhanh · Tức thì</td></tr>
      <tr><td>K</td><td>Bỏ qua: phát nhanh các bước đang chờ</td></tr>
      <tr><td>B</td><td>Burst: gửi 1 tin nhắn dài soạn sẵn để làm đầy context</td></tr>
      <tr><td>N</td><td>Phiên mới: xóa main context, giữ core / recall / archival</td></tr>
      <tr><td>P</td><td>Mở/đóng Raw prompt (đúng chuỗi gửi cho LLM)</td></tr>
      <tr><td>Esc</td><td>Đóng cửa sổ</td></tr>
    </table>
    <p><b>Tốc độ:</b> mỗi bước được giữ trên màn hình một lúc, thành phần đang chạy được phóng to và dòng giải thích phía trên mô tả chuyện gì đang xảy ra. Agent có thể chạy nhanh hơn giao diện; các bước chưa phát được xếp hàng (xem “còn N bước”).</p>
    <p><b>Chế độ:</b> <span class="badge live">LIVE LLM</span> gọi LLM thật · <span class="badge scripted">SCRIPTED</span> LLM được mô phỏng bằng luật để tập dượt (queue manager, hàm, bộ nhớ vẫn chạy thật) · <span class="badge replay">REPLAY</span> phát lại bản ghi.</p>
    <p><b>Kịch bản gợi ý:</b> Cảnh 1 (giới thiệu) → Burst +2 (vượt 70%, cảnh báo) → Burst +2 (vượt 100%, flush) → Cảnh 4 (hỏi lại tên quán) → Phiên mới → Cảnh 5 (đổi đề tài, hỏi agent nhớ gì). Bật Baseline + Reset để so sánh.</p>`
  );
}
function syncStickyHeight() {
  const h = $(".sticky-top").getBoundingClientRect().height;
  document.documentElement.style.setProperty("--sticky-h", `${Math.round(h)}px`);
}

/* --------------------------------------------------------------- bindings */
function bind() {
  $("#stepToggle").addEventListener("change", (e) => send({ type: "set_step", value: e.target.checked }));
  $("#baselineToggle").addEventListener("change", (e) => send({ type: "set_baseline", value: e.target.checked }));
  $("#nextBtn").addEventListener("click", () => send({ type: "next" }));
  $("#paceSel").addEventListener("change", (e) => setPace(parseFloat(e.target.value)));
  $("#skipBtn").addEventListener("click", () => { D.skip = true; });
  $("#burstBtn").addEventListener("click", () => send({ type: "burst", n: 1 }));
  $("#burst2Btn").addEventListener("click", () => send({ type: "burst", n: 2 }));
  $("#newSessionBtn").addEventListener("click", () => send({ type: "new_session" }));
  let resetArmed = null;
  $("#resetBtn").addEventListener("click", (e) => {
    if (resetArmed) {
      clearTimeout(resetArmed);
      resetArmed = null;
      e.target.textContent = "Reset";
      send({ type: "reset" });
    } else {
      e.target.textContent = "Bấm lần nữa để xóa";
      resetArmed = setTimeout(() => { e.target.textContent = "Reset"; resetArmed = null; }, 3000);
    }
  });
  $("#restartReplayBtn").addEventListener("click", () => { D.q = []; send({ type: "restart_replay" }); });
  $("#promptBtn").addEventListener("click", toggleDrawer);
  $("#drawerClose").addEventListener("click", toggleDrawer);
  $("#helpBtn").addEventListener("click", showHelp);
  $("#modalClose").addEventListener("click", closeOverlays);
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeOverlays(); });
  $("#chatForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("#chatInput").value.trim();
    if (!v) return;
    send({ type: "user_message", text: v });
    $("#chatInput").value = "";
  });
  $("#chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("#chatForm").requestSubmit();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("textarea, input, select")) {
      if (e.key === "Escape") e.target.blur();
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key;
    const replay = document.body.classList.contains("mode-replay");
    if (k === "ArrowRight" || k === "PageDown" || k === " ") {
      if (!$("#nextBtn").disabled) send({ type: "next" });
      e.preventDefault();
    } else if (k === "s" || k === "S") {
      send({ type: "set_step", value: !(S.state && S.state.step_mode) });
    } else if (k === "1" || k === "2" || k === "3" || k === "4") {
      setPace([1.7, 1, 0.5, 0][parseInt(k, 10) - 1]);
    } else if (k === "k" || k === "K") {
      D.skip = true;
    } else if ((k === "b" || k === "B") && !replay) {
      send({ type: "burst", n: 1 });
    } else if ((k === "n" || k === "N") && !replay) {
      send({ type: "new_session" });
    } else if (k === "p" || k === "P") {
      toggleDrawer();
    } else if (k === "?") {
      showHelp();
    } else if (k === "Escape") {
      closeOverlays();
      hideTip(true);
    }
  });
  let rt = null;
  window.addEventListener("resize", () => {
    clearTimeout(rt);
    rt = setTimeout(() => { syncStickyHeight(); layoutWires(); if (S.state) render(S.state); }, 120);
  });
}

loadColors();
bind();
initTooltip();
setPace(D.pace);
syncStickyHeight();
connect();
