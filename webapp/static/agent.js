// -*- coding: utf-8 -*-
// agent.js —— 侧栏导航 / 全局「大脑」开关 / 智能体指挥台。
// 与 app.js 同页加载：复用其全局 $() 与 postJSON()；整体包在 IIFE 里避免命名冲突。
(function () {
  "use strict";

  var agentBackend = "webchat";   // 与侧栏「大脑」开关联动
  var agState = { id: null, status: null, poll: null, t0: 0, tick: null, tools: [], openHist: false, openCap: false };

  function el(id) { return document.getElementById(id); }
  function icons() { try { if (window.lucide && window.lucide.createIcons) window.lucide.createIcons(); } catch (e) {} }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
    });
  }
  async function getJSON(url) {
    var r = await fetch(url);
    var d = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(d.detail || ("请求失败 " + r.status));
    return d;
  }

  // ================= 侧栏导航 =================
  function switchView(name) {
    ["agent", "single"].forEach(function (v) {
      var sec = el("view" + v.charAt(0).toUpperCase() + v.slice(1));
      if (sec) sec.classList.toggle("hidden", v !== name);
    });
    document.querySelectorAll(".nav-item[data-view]").forEach(function (b) {
      b.classList.toggle("is-active", b.dataset.view === name);
    });
    try { history.replaceState(null, "", "#" + name); } catch (e) {}
    window.scrollTo({ top: 0 });
  }

  document.querySelectorAll(".nav-item[data-view]").forEach(function (b) {
    b.addEventListener("click", function () { switchView(b.dataset.view); });
  });

  // ================= 全局「大脑」开关 =================
  // 一套界面、两种大脑：这里选一次，单篇精读 / 长文精读 / 智能体都跟着走。
  function normBrain(v) { return (v === "api") ? "api" : "webchat"; }

  function paintBrain(v) {
    var seg = el("brainSeg");
    if (seg) {
      seg.querySelectorAll(".seg-btn").forEach(function (b) {
        var on = b.dataset.brain === v;
        b.classList.toggle("is-on", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }
    var hint = el("brainHint");
    if (hint) hint.textContent = (v === "api") ? "API · 按量计费" : "网页端 · 零费用";
  }

  // 侧栏切换 → 写进面板里的后端选择（单篇精读 / 长文精读 都跟着）
  function setBrain(v) {
    agentBackend = normBrain(v);
    document.querySelectorAll("input[name=backend]").forEach(function (r) {
      r.checked = (r.value === agentBackend);
      r.dispatchEvent(new Event("change"));      // 让既有逻辑（webchatRow 显隐）跟着更新
    });
    var lb = el("longdocBackend");
    if (lb) lb.value = agentBackend;
    paintBrain(agentBackend);
  }

  // 反向同步：用户在面板里自己改了后端，侧栏也要跟着变，避免两处状态不一致
  function syncFromPanel(v) {
    v = normBrain(v);
    if (v === agentBackend) return;
    agentBackend = v;
    var lb = el("longdocBackend");
    if (lb) lb.value = v;
    paintBrain(v);
  }
  document.querySelectorAll("input[name=backend]").forEach(function (r) {
    r.addEventListener("change", function () {
      syncFromPanel((document.querySelector("input[name=backend]:checked") || {}).value || "webchat");
    });
  });
  var lbSel = el("longdocBackend");
  if (lbSel) lbSel.addEventListener("change", function () { syncFromPanel(lbSel.value); });

  (function () {
    var seg = el("brainSeg");
    if (!seg) return;
    seg.querySelectorAll(".seg-btn").forEach(function (b) {
      b.addEventListener("click", function () { setBrain(b.dataset.brain); });
    });
    var checked = document.querySelector("input[name=backend]:checked");
    setBrain(checked ? checked.value : "webchat");
  })();

  // ================= Zotero 状态灯 =================
  async function pollZoteroState() {
    var box = el("zoteroState");
    if (!box) return;
    try {
      var d = await getJSON("/api/zotero_ping");
      var ok = !!d.ok;
      box.classList.toggle("is-ok", ok);
      box.classList.toggle("is-off", !ok);
      box.innerHTML = '<i data-lucide="' + (ok ? "circle-check" : "circle-alert")
        + '" aria-hidden="true"></i><span>' + (ok ? "Zotero 已连接" : "Zotero 未连接") + "</span>";
    } catch (e) {
      box.classList.add("is-off");
      box.innerHTML = '<i data-lucide="circle-alert" aria-hidden="true"></i><span>Zotero 状态未知</span>';
    }
    icons();
  }
  pollZoteroState();
  setInterval(pollZoteroState, 20000);

  // ================= 浮层通用：关闭键 / ESC =================
  var OVERLAYS = ["settingsPanel", "zoteroPicker", "papersetPanel", "batchPanel",
    "pdfimpPanel", "longdocPanel", "kbPanel", "litPanel", "toolsPanel"];
  OVERLAYS.forEach(function (id) {
    var o = el(id);
    if (!o) return;
    o.addEventListener("mousedown", function (e) {
      if (e.target === o) o.classList.add("hidden");   // 点遮罩关闭
    });
  });
  var sClose = el("settingsClose");
  if (sClose) sClose.addEventListener("click", function () { el("settingsPanel").classList.add("hidden"); });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    for (var i = OVERLAYS.length - 1; i >= 0; i--) {
      var o = el(OVERLAYS[i]);
      if (o && !o.classList.contains("hidden")) { o.classList.add("hidden"); return; }
    }
  });

  // ================= 智能体：状态与轮询 =================
  var STATUS_TEXT = {
    planning: "正在生成计划…",
    planned: "计划已生成，等你确认后开始执行",
    running: "执行中…",
    awaiting_confirm: "有步骤需要你确认",
    done: "已完成",
    failed: "失败",
    aborted: "已中止"
  };

  function say(msg, kind) {
    var h = el("agentHint");
    if (!h) return;
    h.textContent = msg || "";
    h.className = "agent-hint" + (kind ? " is-" + kind : "");
  }

  function busyUI(busy) {
    var pb = el("agentPlanBtn");
    if (pb) pb.disabled = !!busy;
  }

  function stopPoll() {
    if (agState.poll) { clearTimeout(agState.poll); agState.poll = null; }
    if (agState.tick) { clearInterval(agState.tick); agState.tick = null; }
  }

  function startTick() {
    if (agState.tick) return;
    agState.t0 = agState.t0 || Date.now();
    agState.tick = setInterval(function () {
      var s = Math.round((Date.now() - agState.t0) / 1000);
      var e2 = el("agentElapsed");
      if (e2) e2.textContent = "已运行 " + s + " 秒";
    }, 1000);
  }

  function pollOnce() {
    if (!agState.id) return;
    getJSON("/api/agent/status?id=" + encodeURIComponent(agState.id)).then(function (t) {
      agState.status = t.status;
      renderTask(t);
      var live = (t.status === "planning" || t.status === "running");
      if (live) {
        agState.poll = setTimeout(pollOnce, 1200);
      } else {
        stopPoll();
        startTickStop(t);
      }
    }).catch(function (e) {
      say("状态读取失败：" + e.message, "err");
      agState.poll = setTimeout(pollOnce, 2500);
    });
  }

  function startTickStop(t) {
    if (agState.tick) { clearInterval(agState.tick); agState.tick = null; }
    var e2 = el("agentElapsed");
    if (e2 && t && t.status !== "done" && t.status !== "failed") e2.textContent = "";
  }

  function beginPoll() {
    stopPoll();
    agState.t0 = Date.now();
    startTick();
    agState.poll = setTimeout(pollOnce, 400);
  }

  // ================= 智能体：渲染 =================
  var STEP_ICON = {
    pending: "circle", running: "loader", done: "circle-check", failed: "circle-x",
    skipped: "skip-forward", awaiting_confirm: "triangle-alert"
  };

  function stepHtml(s) {
    var danger = !!s.danger;
    var cls = "plan-step is-" + s.status + (danger ? " is-danger" : "");
    var h = ['<li class="' + cls + '" data-step="' + esc(s.id) + '">'];
    h.push('<span class="ps-badge"><i data-lucide="' + (STEP_ICON[s.status] || "circle") + '" aria-hidden="true"></i></span>');
    h.push('<div class="ps-body">');
    h.push('<div class="ps-line"><span class="ps-title">' + esc(s.title) + "</span>"
      + '<span class="ps-tool">' + esc(s.tool) + "</span>"
      + (danger ? '<span class="ps-tag">需确认</span>' : "") + "</div>");
    if (s.why) h.push('<div class="ps-why">' + esc(s.why) + "</div>");
    if (s.summary) h.push('<div class="ps-result">' + esc(s.summary) + "</div>");
    if (s.error) h.push('<div class="ps-error">' + esc(s.error) + "</div>");
    if (s.status === "awaiting_confirm") {
      h.push('<div class="ps-gate">');
      h.push('<div class="ps-gate-impact"><i data-lucide="triangle-alert" aria-hidden="true"></i>'
        + esc(s.impact || "这一步会修改本地数据") + "</div>");
      h.push('<div class="ps-gate-actions">'
        + '<button class="primary btn-sm" data-act="confirm" data-step="' + esc(s.id) + '">确认并执行</button>'
        + '<button class="ghost btn-sm" data-act="skip" data-step="' + esc(s.id) + '">跳过这步</button>'
        + "</div></div>");
    }
    h.push("</div></li>");
    return h.join("");
  }

  function renderTask(t) {
    var card = el("agentPlanCard");
    var wrap = el("agentLogWrap");
    var log = el("agentLog");
    var sum = el("agentSummary");
    if (!card) return;

    card.classList.remove("hidden");
    var ex = el("agentExamples");
    if (ex) ex.classList.add("hidden");   // 已经有任务在视图里了，示例就别占地方
    var steps = t.steps || [];
    var nDanger = steps.filter(function (s) { return s.danger; }).length;
    var head = '<div class="plan-head">'
      + '<div class="plan-note">' + esc(t.note || ("任务：" + t.task)) + "</div>"
      + '<div class="plan-meta"><span class="pill is-' + esc(t.status) + '">'
      + esc(STATUS_TEXT[t.status] || t.status) + "</span>"
      + '<span class="muted">' + steps.length + " 步"
      + (nDanger ? " · " + nDanger + " 步需确认" : "") + "</span></div></div>";
    var list = steps.length
      ? '<ol class="plan-steps">' + steps.map(stepHtml).join("") + "</ol>"
      : '<div class="ps-empty">' + esc(t.error || "还没有步骤") + "</div>";
    var acts = "";
    if (t.status === "planned") {
      acts = '<div class="plan-actions"><button class="primary" data-act="run">'
        + '<i data-lucide="play" aria-hidden="true"></i>开始执行</button>'
        + '<span class="muted">写库步骤会先停下来等你确认</span></div>';
    } else if (t.status === "failed") {
      acts = '<div class="plan-actions"><button class="ghost" data-act="retry">'
        + '<i data-lucide="rotate-cw" aria-hidden="true"></i>再试一次</button></div>';
    } else if (t.status === "done") {
      acts = '<div class="plan-actions"><span class="muted">需要改动数据的话，再发一个新任务即可。</span></div>';
    }
    card.innerHTML = head + list + acts;

    // 日志
    var logs = t.log || [];
    if (logs.length) {
      wrap.classList.remove("hidden");
      if (log) { log.textContent = logs.join("\n"); log.scrollTop = log.scrollHeight; }
    }

    // 总结
    if (t.summary) { sum.classList.remove("hidden"); sum.innerHTML = "<h3>总结</h3><pre>" + esc(t.summary) + "</pre>"; }
    else sum.classList.add("hidden");

    // 按钮态
    var runBtn = el("agentRunBtn");
    var abortBtn = el("agentAbortBtn");
    if (runBtn) runBtn.classList.toggle("hidden", t.status !== "planned");
    if (abortBtn) abortBtn.classList.toggle("hidden",
      !(t.status === "running" || t.status === "awaiting_confirm" || t.status === "planned"));

    // 顶部提示
    if (t.status === "planning") say("正在生成计划，会打开一个浏览器窗口（约 30-60 秒），请勿关闭它…");
    else if (t.status === "awaiting_confirm") say("有步骤要写库，请确认后继续", "warn");
    else if (t.status === "running") say("执行中，请稍候…");
    else if (t.status === "planned") say("计划已就绪，点「开始执行」");
    else if (t.status === "done") say("任务完成", "ok");
    else if (t.status === "failed") say(t.error || "任务失败", "err");
    else if (t.status === "aborted") say("已中止", "warn");

    icons();
  }

  // ================= 智能体：动作 =================
  async function onPlan(e) {
    if (e) e.preventDefault();
    var ta = el("agentTask");
    var text = (ta && ta.value || "").trim();
    if (!text) { say("请先写下你想做什么", "err"); if (ta) ta.focus(); return; }
    say("正在提交任务…");
    busyUI(true);
    el("agentSummary").classList.add("hidden");
    try {
      var r = await postJSON("/api/agent/plan", { task: text, backend: agentBackend });
      agState.id = r.id;
      renderTask(r.task);
      beginPoll();
    } catch (err) {
      say(err.message, "err");
      busyUI(false);
    }
  }

  async function onRun() {
    if (!agState.id) return;
    try {
      say("开始执行…");
      await postJSON("/api/agent/run", { id: agState.id });
      agState.t0 = Date.now();
      beginPoll();
    } catch (e) { say(e.message, "err"); }
  }

  async function onAbort() {
    if (!agState.id) return;
    try {
      await postJSON("/api/agent/abort", { id: agState.id });
      var t = await getJSON("/api/agent/status?id=" + encodeURIComponent(agState.id));
      renderTask(t); stopPoll(); startTickStop(t);
    } catch (e) { say(e.message, "err"); }
  }

  async function onGate(act, stepId) {
    if (!agState.id) return;
    try {
      say(act === "confirm" ? "已确认，继续执行…" : "已跳过该步骤…");
      await postJSON("/api/agent/" + (act === "confirm" ? "confirm" : "skip"),
        { id: agState.id, step_id: stepId });
      beginPoll();
    } catch (e) { say(e.message, "err"); }
  }

  document.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest("[data-act]") : null;
    if (!b) return;
    var act = b.dataset.act;
    if (act === "confirm" || act === "skip") onGate(act, b.dataset.step);
    else if (act === "run") onRun();
    else if (act === "retry") { if (agState.id) onRun(); }
  });

  var form = el("agentForm");
  if (form) form.addEventListener("submit", onPlan);
  var exBox = el("agentExamples");
  if (exBox) exBox.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest(".example") : null;
    if (!b) return;
    var ta2 = el("agentTask");
    if (ta2) { ta2.value = b.dataset.example || ""; ta2.focus(); }
  });
  var runBtn = el("agentRunBtn");
  if (runBtn) runBtn.addEventListener("click", onRun);
  var abortBtn = el("agentAbortBtn");
  if (abortBtn) abortBtn.addEventListener("click", onAbort);

  // Ctrl/Cmd + Enter 快捷提交
  var ta = el("agentTask");
  if (ta) ta.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") onPlan(e);
  });

  // ================= 可用工具 =================
  async function renderTools() {
    var box = el("agentCapList");
    if (!box) return;
    if (!agState.tools.length) {
      try { agState.tools = (await getJSON("/api/agent/tools")).tools || []; } catch (e) { }
    }
    var cnt = el("agentToolCount");
    if (cnt) cnt.textContent = agState.tools.length ? String(agState.tools.length) : "—";
    var groups = {};
    agState.tools.forEach(function (t) { (groups[t.category || "其他"] = groups[t.category || "其他"] || []).push(t); });
    var h = ['<div class="cap-head"><h3>智能体能调用的工具</h3>'
      + '<span class="muted">「只读」可直接跑；「写库」每一步都要你确认；「预览/可写」默认只预览，应用改动时才要确认</span></div>'];
    Object.keys(groups).forEach(function (g) {
      h.push('<div class="cap-group"><div class="cap-group-title">' + esc(g) + "</div><ul>");
      groups[g].forEach(function (t) {
        var kind = t.danger_kind || (t.danger ? "always" : "never");
        var label = kind === "always" ? "写库" : (kind === "conditional" ? "预览/可写" : "只读");
        var cls = kind === "never" ? "is-read" : (kind === "conditional" ? "is-warn" : "is-danger");
        h.push('<li><div class="cap-line"><span class="cap-name">' + esc(t.title) + "</span>"
          + '<span class="cap-flag ' + cls + '">' + label + "</span></div>"
          + '<div class="cap-desc">' + esc(t.description) + "</div></li>");
      });
      h.push("</ul></div>");
    });
    box.innerHTML = h.join("");
  }

  // ================= 历史任务 =================
  async function renderHistory() {
    var box = el("agentHistory");
    if (!box) return;
    var d;
    try { d = await getJSON("/api/agent/status"); } catch (e) { box.innerHTML = '<div class="muted">读取失败</div>'; return; }
    var rows = d.tasks || [];
    var h = ['<div class="cap-head"><h3>历史任务</h3><span class="muted">共 ' + rows.length + " 个</span></div>"];
    if (!rows.length) h.push('<div class="muted">还没有任务记录。上面发一条指令试试。</div>');
    else {
      h.push('<ul class="hist-list">');
      rows.forEach(function (r) {
        h.push('<li class="hist-item" data-id="' + esc(r.id) + '">'
          + '<span class="pill is-' + esc(r.status) + '">' + esc(STATUS_TEXT[r.status] || r.status) + "</span>"
          + '<span class="hist-task">' + esc(r.task) + "</span>"
          + '<span class="muted hist-time">' + esc((r.created_at || "").slice(5, 16)) + "</span></li>");
      });
      h.push("</ul>");
    }
    box.innerHTML = h.join("");
    icons();
  }

  document.addEventListener("click", function (e) {
    var it = e.target.closest ? e.target.closest(".hist-item") : null;
    if (!it) return;
    agState.id = it.dataset.id;
    getJSON("/api/agent/status?id=" + encodeURIComponent(agState.id)).then(function (t) {
      renderTask(t);
      if (t.status === "running" || t.status === "planning") beginPoll();
      el("agentPlanCard").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  var histBtn = el("agentHistoryBtn");
  if (histBtn) histBtn.addEventListener("click", function () {
    var box = el("agentHistory");
    box.classList.toggle("hidden");
    if (!box.classList.contains("hidden")) renderHistory();
  });
  var capBtn = el("agentCapBtn");
  if (capBtn) capBtn.addEventListener("click", function () {
    var box = el("agentCapList");
    box.classList.toggle("hidden");
    if (!box.classList.contains("hidden")) renderTools();
  });

  // ================= 启动 =================
  (function boot() {
    var hash = (location.hash || "").replace("#", "");
    switchView(hash === "single" ? "single" : "agent");
    icons();
    getJSON("/api/agent/tools").then(function (d) {
      agState.tools = d.tools || [];
      var c = el("agentToolCount");
      if (c) c.textContent = agState.tools.length ? String(agState.tools.length) : "—";
    }).catch(function () { });
  })();
})();
