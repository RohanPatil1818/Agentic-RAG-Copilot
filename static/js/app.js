
/* =========================================================
   Agentic RAG Copilot — frontend
   Talks to your FastAPI backend (agent_graph.ask()).
   ========================================================= */

/* ---------- 1. CONFIG (edit to match your backend) ---------- */
const DEFAULTS = {
  // Same origin when served by FastAPI; localhost:8000 when opening index.html directly.
  apiBase: location.protocol === "file:" ? "http://localhost:8000" : "",
  chatPath: "/api/chat",
  healthPath: "/api/health",
  showTrace: true,
};

// Shape of the JSON body sent to your endpoint.
function buildRequestBody(question) {
  return { question };
}

// Map your backend response to what the UI needs.
// Your graph state has: answer, source_used, citations, trace.
function parseResponse(data) {
  const d = data?.result ?? data ?? {};
  return {
    answer: d.answer ?? d.response ?? "",
    source: d.source_used ?? d.source ?? "",
    citations: Array.isArray(d.citations) ? d.citations : [],
    trace: Array.isArray(d.trace) ? d.trace : [],
    cache: d.cache ?? null,
    latencyMs: d.latency_ms ?? null,
  };
}

/* ---------- 2. Helpers ---------- */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const store = {
  get(k, fallback) {
    try { const v = localStorage.getItem(k); return v === null ? fallback : JSON.parse(v); }
    catch { return fallback; }
  },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

const settings = { ...DEFAULTS, ...store.get("rag.settings", {}) };
if (settings.healthPath === "/health") settings.healthPath = DEFAULTS.healthPath;   // old saved default
const stats = { questions: 0, kb: 0, web: 0, cache: 0 };
let busy = false;

const el = {
  chat: $("#chat"), welcome: $("#welcome"), composer: $("#composer"),
  input: $("#input"), send: $("#sendBtn"), sidebar: $("#sidebar"), scrim: $("#scrim"),
  pipeline: $("#pipeline"), status: $("#status"), statusText: $("#statusText"),
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* Minimal, safe markdown renderer (escapes HTML first). */
function renderMarkdown(src) {
  const blocks = [];
  let text = escapeHtml(src).replace(/```(\w*)\n?([\s\S]*?)```/g, (_, _l, code) => {
    blocks.push(`<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });

  const inline = t => t
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

  const out = [];
  let list = null; // "ul" | "ol"
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^(#{1,4})\s+(.*)/))) {
      closeList(); out.push(`<h4>${inline(m[2])}</h4>`);
    } else if ((m = line.match(/^&gt;\s?(.*)/))) {
      closeList(); out.push(`<blockquote>${inline(m[1])}</blockquote>`);
    } else if (line.trim() === "") {
      closeList();
    } else if (/^\u0000\d+\u0000$/.test(line.trim())) {
      closeList(); out.push(line.trim());
    } else {
      closeList(); out.push(`<p>${inline(line)}</p>`);
    }
  }
  closeList();
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
}

function scrollToBottom() { el.chat.scrollTop = el.chat.scrollHeight; }

/* ---------- 3. Source metadata ---------- */
const SOURCES = {
  private_kb:            { label: "Private KB",            cls: "kb" },
  web_search:            { label: "Web search",            cls: "web" },
  web:                   { label: "Web search",            cls: "web" },
  direct:                { label: "Direct answer",         cls: "direct" },
  insufficient_evidence: { label: "Insufficient evidence", cls: "none" },
};
const sourceInfo = s => SOURCES[s] ?? { label: s || "Answer", cls: "direct" };

/* ---------- 3b. Cache helpers ---------- */
const isHit = c => c?.status === "hit";
const fmtMs = ms => ms == null ? "" : ms >= 1000 ? (ms / 1000).toFixed(1) + " s" : Math.round(ms) + " ms";
function fmtAge(sec) {
  if (sec == null) return "";
  if (sec < 60) return sec + " s";
  if (sec < 3600) return Math.round(sec / 60) + " min";
  if (sec < 86400) return (sec / 3600).toFixed(1) + " h";
  return Math.round(sec / 86400) + " d";
}
// Where the cached answer originally came from, in plain words.
function cacheOrigin(source) {
  const cls = sourceInfo(source).cls;
  return cls === "web" ? "web search" : cls === "kb" ? "company KB" : sourceInfo(source).label.toLowerCase();
}

/* ---------- 4. Pipeline visualiser ---------- */
const NODE_ORDER = ["cache", "router", "retrieve", "gradeKb", "web", "gradeWeb", "rewrite", "generate"];
let simTimer = null;

function setNode(name, state) {
  const li = $(`[data-node="${name}"]`, el.pipeline);
  if (!li) return;
  li.classList.remove("running", "done", "skipped", "fail", "hit", "miss", "cached");
  if (state) li.classList.add(state);
}
function resetPipeline() {
  clearInterval(simTimer);
  NODE_ORDER.forEach(n => setNode(n, null));
  const note = $("#cacheNote"); if (note) note.textContent = "";
}

// While waiting (no streaming), softly animate the first steps.
function simulatePipeline() {
  resetPipeline();
  const steps = ["cache", "router", "retrieve", "gradeKb"];
  let i = 0;
  setNode(steps[0], "running");
  simTimer = setInterval(() => {
    setNode(steps[i], "done");
    i++;
    if (i >= steps.length) { clearInterval(simTimer); return; }
    setNode(steps[i], "running");
  }, 1100);
}

// Final state derived from the real trace the backend returned.
function applyTrace(trace, source, cache) {
  resetPipeline();
  const hit = isHit(cache);
  const seen = new Set();
  let failed = false;
  for (const t of trace) {
    const s = String(t).replace(/^↺\s*/, "");   // cached replays carry a leading arrow
    if (/^cache/i.test(s)) continue;
    if (/^router/i.test(s)) seen.add("router");
    else if (/private kb retrieval/i.test(s)) seen.add("retrieve");
    else if (/kb evidence grade/i.test(s)) { seen.add("gradeKb"); }
    else if (/web fallback/i.test(s)) seen.add("web");
    else if (/web evidence grade/i.test(s)) seen.add("gradeWeb");
    else if (/query rewrite/i.test(s)) seen.add("rewrite");
    else if (/answer generation|direct response/i.test(s)) seen.add("generate");
    else if (/stopped/i.test(s)) failed = true;
  }
  const note = $("#cacheNote");
  if (hit) {
    setNode("cache", "hit");
    if (note) note.textContent = `HIT · ${cache.layer}`;
    NODE_ORDER.slice(1).forEach(n => setNode(n, seen.has(n) ? "cached" : "skipped"));   // steps that originally ran
  } else {
    setNode("cache", cache ? "miss" : "skipped");
    if (note) note.textContent = cache ? "MISS" : "";
    NODE_ORDER.slice(1).forEach(n => setNode(n, seen.has(n) ? "done" : "skipped"));
    if (failed) setNode("gradeWeb", "fail");
  }
  if (source === "direct") ["retrieve", "gradeKb", "web", "gradeWeb", "rewrite"].forEach(n => setNode(n, "skipped"));
}

/* ---------- 4b. Agent traces column ---------- */
function addTraceGroup(question, trace, cache, source, latencyMs) {
  const list = $("#tracesList");
  $("#tracesEmpty")?.remove();
  const g = document.createElement("div");
  g.className = "trace-group";
  const hit = isHit(cache);
  const chip = !cache ? "" : hit
    ? `<div class="trace-cache hit">⚡ Cache hit · ${escapeHtml(cache.layer)} match · originally from ${cacheOrigin(source)} · ${fmtMs(latencyMs)}</div>`
    : '<div class="trace-cache miss">Cache miss · full agent run</div>';
  const steps = (trace.length ? trace : ["No trace returned"]).map((t, i) => {
    const s = String(t);
    const replay = s.startsWith("↺");
    const cls = /^cache/i.test(s) ? "cache-step" : replay ? "replayed" : "";
    const text = escapeHtml(replay ? s.replace(/^↺\s*/, "") : s);
    return `<li class="${cls}"><span class="n">${i + 1}</span><span class="t">${text}${replay ? " <em>(from cache)</em>" : ""}</span></li>`;
  }).join("");
  g.innerHTML = `<div class="trace-q" title="${escapeHtml(question)}">${escapeHtml(question)}</div>
    ${chip}<ol class="trace-steps">${steps}</ol>`;
  list.appendChild(g);
  list.scrollTop = list.scrollHeight;
}
function clearTraces() {
  $("#tracesList").innerHTML = '<p class="traces-empty" id="tracesEmpty">Ask a question to see the agent’s steps here.</p>';
}

/* ---------- 5. Message rendering ---------- */
function addUserMessage(text) {
  el.welcome?.remove();
  const msg = document.createElement("div");
  msg.className = "msg user";
  msg.innerHTML = `<div class="avatar">You</div><div class="msg-body"><div class="bubble">${escapeHtml(text)}</div></div>`;
  el.chat.appendChild(msg);
  scrollToBottom();
}

function addTyping() {
  const msg = document.createElement("div");
  msg.className = "msg bot";
  msg.innerHTML = `
    <div class="avatar">AI</div>
    <div class="msg-body"><div class="bubble">
      <div class="typing"><i></i><i></i><i></i></div>
      <div class="thinking-label">Agent is searching the knowledge base…</div>
    </div></div>`;
  el.chat.appendChild(msg);
  scrollToBottom();
  return msg;
}

function citationHtml(c, cached = false) {
  const title = escapeHtml(c.title || c.url || "Source");
  const kind = c.type === "web" ? "Web" : "KB";
  const icon = c.type === "web"
    ? '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 0 20 15 15 0 0 1 0-20z"/></svg>'
    : '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>';
  const safeUrl = /^https?:\/\//i.test(c.url || "") ? escapeHtml(c.url) : "";
  const inner = `${icon}<span>${title}</span><small>${kind}${cached ? " · cached" : ""}</small>`;
  return safeUrl
    ? `<a class="cite" href="${safeUrl}" target="_blank" rel="noopener noreferrer" title="${title}">${inner}</a>`
    : `<div class="cite" title="${title}">${inner}</div>`;
}

function fillBotMessage(msg, { answer, source, citations, trace, cache, latencyMs }) {
  const info = sourceInfo(source);
  const hit = isHit(cache);
  const origin = cacheOrigin(source);
  const body = $(".msg-body", msg);
  const isWeb = info.cls === "web";

  body.innerHTML = `
    <div class="bubble"><div class="md">${renderMarkdown(answer || "_No answer returned._")}</div></div>
    ${hit ? `<div class="cache-note">⚡ Served from cache (${escapeHtml(cache.layer)} match) · originally answered from ${origin} · stored ${fmtAge(cache.age_seconds)} ago · ${fmtMs(latencyMs)}${cache.saved_latency_ms ? ` instead of ~${fmtMs(cache.saved_latency_ms)}` : ""}</div>` : ""}
    ${isWeb ? `<div class="warn-note">External web information — validate with IT before changing company-managed systems.${hit ? ` This is a cached answer (stored ${fmtAge(cache.age_seconds)} ago).` : ""}</div>` : ""}
    <div class="meta">
      <span class="badge ${info.cls}">${info.label}</span>
      ${hit ? `<span class="badge cache">⚡ Cached · ${escapeHtml(cache.layer)}</span>` : ""}
      <button class="mini-btn" data-copy>Copy</button>
    </div>
    ${citations.length ? `${hit ? `<div class="cite-label">Resources used · from cache (${origin})</div>` : ""}<div class="citations">${citations.map(c => citationHtml(c, hit)).join("")}</div>` : ""}
    ${settings.showTrace && trace.length ? `
      <details class="trace">
        <summary>Agent trace · ${trace.length} steps${hit ? " · cache hit" : ""}</summary>
        <ol>${trace.map(t => `<li>${escapeHtml(t)}</li>`).join("")}</ol>
      </details>` : ""}
  `;

  $("[data-copy]", body).addEventListener("click", async e => {
    try { await navigator.clipboard.writeText(answer); e.target.textContent = "Copied"; }
    catch { e.target.textContent = "Failed"; }
    setTimeout(() => (e.target.textContent = "Copy"), 1500);
  });
}

function fillError(msg, message) {
  $(".msg-body", msg).innerHTML = `
    <div class="bubble error-bubble">
      <b>Something went wrong.</b>
      <div class="md"><p>${escapeHtml(message)}</p></div>
    </div>
    <div class="meta"><button class="mini-btn" data-retry>Retry</button></div>`;
  $("[data-retry]", msg).addEventListener("click", () => {
    const lastQ = msg.dataset.question;
    msg.remove();
    if (lastQ) ask(lastQ, { skipUserBubble: true });
  });
}

/* ---------- 6. API ---------- */
async function callBackend(question) {
  const url = settings.apiBase.replace(/\/$/, "") + settings.chatPath;
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), 90000);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildRequestBody(question)),
      signal: ctrl.signal,
    });
    if (!res.ok) {
      let detail = "";
      try { detail = JSON.stringify((await res.json()).detail ?? ""); } catch {}
      throw new Error(`Server returned ${res.status}${detail ? ` — ${detail}` : ""}`);
    }
    return parseResponse(await res.json());
  } catch (err) {
    if (err.name === "AbortError") throw new Error("The request timed out after 90 seconds.");
    if (err instanceof TypeError) throw new Error(`Cannot reach backend at ${url}. Is it running, and is CORS enabled?`);
    throw err;
  } finally {
    clearTimeout(timeout);
  }
}

async function checkHealth() {
  if (!settings.healthPath) { setStatus("", "Backend not checked"); return; }
  try {
    const res = await fetch(settings.apiBase.replace(/\/$/, "") + settings.healthPath, { cache: "no-store" });
    setStatus(res.ok ? "ok" : "err", res.ok ? "Backend online" : `Backend error ${res.status}`);
  } catch {
    setStatus("err", "Backend offline");
  }
}
function setStatus(cls, text) {
  el.status.className = "status " + cls;
  el.statusText.textContent = text;
}

/* ---------- 7. Ask flow ---------- */
async function ask(question, { skipUserBubble = false } = {}) {
  question = question.trim();
  if (!question || busy) return;
  busy = true;
  el.send.disabled = true;

  if (!skipUserBubble) addUserMessage(question);
  const typing = addTyping();
  typing.dataset.question = question;
  simulatePipeline();

  try {
    const result = await callBackend(question);
    applyTrace(result.trace, result.source, result.cache);
    addTraceGroup(question, result.trace, result.cache, result.source, result.latencyMs);
    fillBotMessage(typing, result);
    stats.questions++;
    if (result.source === "private_kb") stats.kb++;
    if (result.source === "web_search" || result.source === "web") stats.web++;
    if (isHit(result.cache)) stats.cache++;
    renderStats();
  } catch (err) {
    resetPipeline();
    fillError(typing, err.message || "Unknown error");
  } finally {
    busy = false;
    el.send.disabled = !el.input.value.trim();
    scrollToBottom();
    el.input.focus();
  }
}

function renderStats() {
  $("#statQuestions").textContent = stats.questions;
  $("#statKb").textContent = stats.kb;
  $("#statWeb").textContent = stats.web;
  $("#statCache").textContent = stats.cache;
}

/* ---------- 8. UI wiring ---------- */
function autoGrow() {
  el.input.style.height = "auto";
  el.input.style.height = Math.min(el.input.scrollHeight, 160) + "px";
  el.send.disabled = busy || !el.input.value.trim();
}

el.input.addEventListener("input", autoGrow);
el.input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    el.composer.requestSubmit();
  }
});
el.composer.addEventListener("submit", e => {
  e.preventDefault();
  const q = el.input.value;
  el.input.value = "";
  autoGrow();
  ask(q);
});

$("#suggestions")?.addEventListener("click", e => {
  const btn = e.target.closest(".suggestion");
  if (btn) ask(btn.dataset.q);
});

// New chat
$("#newChatBtn").addEventListener("click", () => {
  if (busy) return;
  el.chat.innerHTML = "";
  el.chat.appendChild(el.welcome);
  resetPipeline();
  clearTraces();
  closeSidebar();
  el.input.focus();
});

// Sidebar (mobile)
const openSidebar = () => { el.sidebar.classList.add("open"); el.scrim.classList.add("show"); };
const closeSidebar = () => { el.sidebar.classList.remove("open"); el.scrim.classList.remove("show"); };
$("#menuBtn").addEventListener("click", openSidebar);
el.scrim.addEventListener("click", closeSidebar);

// Theme
function applyTheme(t) { document.documentElement.dataset.theme = t; store.set("rag.theme", t); }
applyTheme(store.get("rag.theme", matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
$("#themeBtn").addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});

// Settings modal
const modal = $("#settingsModal");
$("#settingsBtn").addEventListener("click", () => {
  $("#apiBaseInput").value = settings.apiBase;
  $("#chatPathInput").value = settings.chatPath;
  $("#healthPathInput").value = settings.healthPath;
  $("#showTraceInput").checked = settings.showTrace;
  modal.showModal();
  closeSidebar();
});
$("#settingsCancel").addEventListener("click", () => modal.close());
$("#settingsForm").addEventListener("submit", () => {
  settings.apiBase = $("#apiBaseInput").value.trim();
  settings.chatPath = $("#chatPathInput").value.trim() || DEFAULTS.chatPath;
  settings.healthPath = $("#healthPathInput").value.trim();
  settings.showTrace = $("#showTraceInput").checked;
  store.set("rag.settings", settings);
  checkHealth();
});

// Admin upload: password is verified by the backend before a file can be sent.
const uploadModal = $("#uploadModal");
let adminKey = "";   // kept in memory only
const apiUrl = path => settings.apiBase.replace(/\/$/, "") + path;
function uploadMsg(text, cls = "") { const m = $("#uploadMsg"); m.textContent = text; m.className = "upload-msg " + cls; }
function resetUpload() {
  adminKey = "";
  $("#adminKeyInput").value = "";
  $("#docFileInput").value = "";
  $("#adminStep1").hidden = false;
  $("#adminStep2").hidden = true;
  $("#uploadSubmit").textContent = "Unlock";
  $("#uploadSubmit").disabled = false;
  uploadMsg("");
}
$("#uploadBtn").addEventListener("click", () => { resetUpload(); uploadModal.showModal(); closeSidebar(); });
$("#uploadCancel").addEventListener("click", () => { uploadModal.close(); resetUpload(); });
uploadModal.addEventListener("close", resetUpload);
$("#uploadForm").addEventListener("submit", async e => {
  e.preventDefault();
  const btn = $("#uploadSubmit");
  btn.disabled = true;
  try {
    if (!adminKey) {
      const key = $("#adminKeyInput").value;
      const res = await fetch(apiUrl("/api/admin/verify"), { method: "POST", headers: { "X-Admin-Key": key } });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        uploadMsg(res.status === 401 ? "Incorrect admin password." : (typeof data.detail === "string" ? data.detail : `Server returned ${res.status}`), "err");
        return;
      }
      adminKey = key;
      $("#adminStep1").hidden = true;
      $("#adminStep2").hidden = false;
      btn.textContent = "Upload";
      uploadMsg("Password accepted. Choose a document.", "ok");
    } else {
      const file = $("#docFileInput").files[0];
      if (!file) { uploadMsg("Choose a file first.", "err"); return; }
      uploadMsg("Uploading and indexing…");
      const body = new FormData();
      body.append("file", file);
      const res = await fetch(apiUrl("/api/ingest"), { method: "POST", headers: { "X-Admin-Key": adminKey }, body });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { uploadMsg(typeof data.detail === "string" ? data.detail : `Server returned ${res.status}`, "err"); return; }
      uploadMsg(`Indexed ${data.file}: ${data.chunks} chunks.`, "ok");
      $("#docFileInput").value = "";
    }
  } catch {
    uploadMsg("Cannot reach backend.", "err");
  } finally {
    btn.disabled = false;
  }
});

/* ---------- 9. Init ---------- */
autoGrow();
checkHealth();
setInterval(checkHealth, 30000);
el.input.focus();
 
