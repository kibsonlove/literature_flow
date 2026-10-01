// -*- coding: utf-8 -*-
// agent.js —— 侧栏导航 / 全局「大脑」开关 / 智能体指挥台。
// 与 app.js 同页加载：复用其全局 $() 与 postJSON()；整体包在 IIFE 里避免命名冲突。
(function () {
  "use strict";

  var agentBackend = "webchat";   // 与侧栏「大脑」开关联动
  var agState = {
    id: null, status: null, poll: null, t0: 0, tick: null, tools: [],
    openHist: false, openCap: false,
    last: null,        // 最近一次拿到的任务对象（编辑/取消时用来重渲染）
    editing: null,     // 正在编辑哪一步的参数（非空时不重建 DOM，免得冲掉输入）
    renderedEditing: null   // 页面上当前已经画出来的编辑态（用来判断要不要重建）
  };

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
      var live = (t.status === "planning" || t.status === "running")
        || (t.status === "aborted" && t.busy);   // 中止后后台还在收尾，继续盯到它停稳
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

  // 任务处于这些状态时，计划还是"可改"的（已经跑完的不许改）
  var EDITABLE = { "planned": 1, "aborted": 1, "failed": 1, "awaiting_confirm": 1 };
  // 引用形如 $s2.results（后端已把漏掉 $ 的写法归一过；这里再宽一点兜底）
  var REF_RE = /^\$?\s*s(\d+)((?:\.\w+)*)$/;

  function toolSpec(name) {
    for (var i = 0; i < agState.tools.length; i++) {
      if (agState.tools[i].name === name) return agState.tools[i];
    }
    return null;
  }

  function typeOf(v) { return Object.prototype.toString.call(v); }

  // 把 "$s2.results" 翻成人话：「第 2 步「检索文献」的结果（results）」——用户看不懂 $s2 是什么
  function refLabel(val, t) {
    var m = REF_RE.exec(String(val).trim());
    if (!m) return "";
    var n = parseInt(m[1], 10);
    var field = (m[2] || "").replace(/^\./, "");
    var steps = (t && t.steps) || [];
    var src = steps[n - 1];
    var who = src ? ('第 ' + n + ' 步「' + (src.title || src.tool) + '」') : ("第 " + n + " 步");
    return field ? (who + " 运行结果里的 " + field) : (who + " 的完整结果");
  }

  // 每个参数渲染一个控件；引用型（$sN…）与数组型对象只读展示——那些是上一步自动带进来的
  function fieldHtml(tool, key, p, val, t) {
    var id = "agf_" + esc(tool) + "_" + esc(key);
    var label = '<span class="agf-label">' + esc(key) + "</span>";
    var hintTxt = p.desc ? '<span class="agf-hint">' + esc(p.desc) + "</span>" : "";
    if (p.required) hintTxt = '<span class="agf-hint">必填</span>' + hintTxt;
    if (typeof val === "string" && REF_RE.test(val.trim())) {
      return '<label class="agf-row is-ref" for="' + id + '">' + label
        + '<input id="' + id + '" type="text" value="' + esc(val) + '" data-ref="1" readonly>'
        + '<span class="agf-hint">自动引用：' + esc(refLabel(val, t)) + "（" + esc(val.trim())
        + "）——由前面那一步算完自动带过来，不用手改</span></label>";
    }
    var t = (p.type || "").toLowerCase();
    if (t === "bool" || typeof val === "boolean") {
      return '<label class="agf-row is-check" for="' + id + '">'
        + '<input id="' + id + '" type="checkbox" data-arg="' + esc(key) + '" data-kind="bool"'
        + (val ? " checked" : "") + ">" + label + hintTxt + "</label>";
    }
    if (t === "int") {
      return '<label class="agf-row" for="' + id + '">' + label
        + '<input id="' + id + '" type="number" data-arg="' + esc(key) + '" data-kind="int" value="'
        + esc(val == null ? "" : val) + '">' + hintTxt + "</label>";
    }
    if (t.indexOf("array<string") === 0 || typeOf(val) === "[object Array]") {
      var arr = (typeOf(val) === "[object Array]") ? val : [];
      var onlyStr = arr.every(function (x) { return typeof x === "string"; });
      if (!onlyStr) {
        return '<div class="agf-row is-ref">' + label
          + '<div class="agf-readonly">' + esc(JSON.stringify(val)) + "</div>"
          + '<span class="agf-hint">由前面步骤的结果自动带入，不能手改；想换内容就改用前面那一步</span></div>';
      }
      return '<label class="agf-row" for="' + id + '">' + label
        + '<textarea id="' + id + '" rows="4" data-arg="' + esc(key) + '" data-kind="array">'
        + esc(arr.join("\n")) + "</textarea>"
        + '<span class="agf-hint">一行一条</span></label>';
    }
    return '<label class="agf-row" for="' + id + '">' + label
      + '<input id="' + id + '" type="text" data-arg="' + esc(key) + '" data-kind="string" value="'
      + esc(val == null ? "" : val) + '">' + hintTxt + "</label>";
  }

  function formHtml(s, t) {
    var spec = toolSpec(s.tool) || { params: {} };
    var params = spec.params || {};
    var keys = Object.keys(params);
    // 参数表里没有、但 args 里有的（比如模型多塞的），也让人看得见，免得"参数不见了"的困惑
    Object.keys(s.args || {}).forEach(function (k) {
      if (keys.indexOf(k) === -1) keys.push(k);
    });
    var rows = keys.map(function (k) {
      return fieldHtml(s.tool, k, params[k] || {}, s.args ? s.args[k] : undefined, t);
    }).join("");
    if (!rows) rows = '<div class="muted">这一步没有可改的参数。</div>';
    return '<div class="ps-form">'
      + '<div class="agf-head">改「' + esc(s.title) + '」的参数'
      + '<span class="muted">（' + esc(s.tool) + '）</span></div>'
      + rows
      + '<div class="ps-form-actions">'
      + '<button class="primary btn-sm" data-act="save" data-step="' + esc(s.id) + '">保存</button>'
      + '<button class="ghost btn-sm" data-act="cancel" data-step="' + esc(s.id) + '">取消</button>'
      + "</div></div>";
  }

  function stepHtml(s, t) {
    var danger = !!s.danger;
    var cls = "plan-step is-" + s.status + (danger ? " is-danger" : "");
    var h = ['<li class="' + cls + '" data-step="' + esc(s.id) + '">'];
    h.push('<span class="ps-badge"><i data-lucide="' + (STEP_ICON[s.status] || "circle") + '" aria-hidden="true"></i></span>');
    h.push('<div class="ps-body">');
    h.push('<div class="ps-line"><span class="ps-title">' + esc(s.title) + "</span>"
      + '<span class="ps-tool">' + esc(s.tool) + "</span>"
      + (danger ? '<span class="ps-tag">需确认</span>' : "") + "</div>");
    if (s.why) h.push('<div class="ps-why">' + esc(s.why) + "</div>");
    if (s.warning) {
      h.push('<div class="ps-warn"><i data-lucide="triangle-alert" aria-hidden="true"></i>'
        + esc(s.warning) + "　建议点「改参数」补上再执行</div>");
    }
    if (s.summary) h.push('<div class="ps-result">' + esc(s.summary) + "</div>");
    if (s.error) h.push('<div class="ps-error">' + esc(s.error) + "</div>");

    if (agState.editing === s.id) {
      h.push(formHtml(s, t));                    // 正在改这一步的参数
    } else if (s.status === "awaiting_confirm") {
      h.push('<div class="ps-gate">');
      h.push('<div class="ps-gate-impact"><i data-lucide="triangle-alert" aria-hidden="true"></i>'
        + esc(s.impact || "这一步会修改本地数据") + "</div>");
      h.push('<div class="ps-gate-actions">'
        + '<button class="primary btn-sm" data-act="confirm" data-step="' + esc(s.id) + '">确认并执行</button>'
        + '<button class="ghost btn-sm" data-act="edit" data-step="' + esc(s.id) + '">先改参数</button>'
        + '<button class="ghost btn-sm" data-act="skip" data-step="' + esc(s.id) + '">跳过这步</button>'
        + "</div></div>");
    } else if (s.status === "pending" && EDITABLE[t.status] && t.status !== "running") {
      h.push('<div class="ps-edit-row">'
        + '<button class="ghost btn-sm" data-act="edit" data-step="' + esc(s.id) + '">'
        + '<i data-lucide="pencil" aria-hidden="true"></i>改参数</button>'
        + '<button class="ghost btn-sm" data-act="skip" data-step="' + esc(s.id) + '">跳过这步</button>'
        + "</div>");
    }
    h.push("</div></li>");
    return h.join("");
  }

  function planActions(t) {
    var b = [];
    if (t.status === "planned") b.push(['run', "primary", "play", "开始执行"]);
    if (t.status === "aborted") b.push(['run', "primary", "play", "继续执行"]);
    if (t.status === "failed") b.push(['run', "primary", "rotate-cw", "再试一次"]);
    if (t.status === "planned" || t.status === "aborted" || t.status === "failed"
      || t.status === "awaiting_confirm") {
      b.push(["replan", "ghost", "wand-sparkles", "重新生成计划"]);
    }
    // 终态给个删除入口，免得失败/中止的残值一直堆在历史里
    if (t.status === "done" || t.status === "failed" || t.status === "aborted") {
      b.push(["delete", "ghost", "trash-2", "删除任务"]);
    }
    if (!b.length) {
      if (t.status === "planning") return "";      // 出计划时别写"再发一个新任务"这种无关的话
      return '<div class="plan-actions"><span class="muted">需要改动数据的话，再发一个新任务即可。</span></div>';
    }
    var hint = "";
    if (t.status === "planned" || t.status === "aborted") {
      hint = '<span class="muted">不满意可以直接改单步参数，或整份重排</span>';
    } else if (t.status === "awaiting_confirm") {
      hint = '<span class="muted">也可以先中止，改完再继续</span>';
    }
    return '<div class="plan-actions">' + b.map(function (x) {
      return '<button class="' + x[1] + '" data-act="' + x[0] + '">'
        + '<i data-lucide="' + x[2] + '" aria-hidden="true"></i>' + x[3] + "</button>";
    }).join("") + hint + "</div>";
  }

  function renderTask(t) {
    var card = el("agentPlanCard");
    var wrap = el("agentLogWrap");
    var log = el("agentLog");
    var sum = el("agentSummary");
    if (!card) return;

    card.classList.remove("hidden");
    agState.last = t;
    busyUI(!!t.busy);          // 串行通道：有任务在跑时先别再提新任务
    var ex = el("agentExamples");
    if (ex) ex.classList.add("hidden");   // 已经有任务在视图里了，示例就别占地方

    // 正在编辑参数时不要重建 DOM（否则轮询会把用户刚敲的字冲掉）；
    // 但"刚点开编辑"这一次必须重建，否则表单根本画不出来。
    if (!agState.editing || agState.renderedEditing !== agState.editing) {
      var steps = t.steps || [];
      var nDanger = steps.filter(function (s) { return s.danger; }).length;
      var head = '<div class="plan-head">'
        + '<div class="plan-note">' + esc(t.note || ("任务：" + t.task)) + "</div>"
        + '<div class="plan-meta"><span class="pill is-' + esc(t.status) + '">'
        + esc(STATUS_TEXT[t.status] || t.status) + "</span>"
        + '<span class="muted">' + steps.length + " 步"
        + (nDanger ? " · " + nDanger + " 步需确认" : "") + "</span></div></div>";
      var empty = t.status === "planning"
        ? "正在生成计划，请稍候（会打开一个浏览器窗口，约 30-60 秒）…"
        : (t.error || "还没有步骤");
      var list = steps.length
        ? '<ol class="plan-steps">' + steps.map(function (s) { return stepHtml(s, t); }).join("") + "</ol>"
        : '<div class="ps-empty">' + esc(empty) + "</div>";
      card.innerHTML = head + list + planActions(t);
      agState.renderedEditing = agState.editing;
      card.classList.toggle("is-editing", !!agState.editing);
    } else {
      card.classList.add("is-editing");
      icons();
      return;   // 编辑中且表单已在页面上：这一轮不重建
    }

    // 日志
    var logs = t.log || [];
    if (logs.length) {
      wrap.classList.remove("hidden");
      if (log) { log.textContent = logs.join("\n"); log.scrollTop = log.scrollHeight; }
    }

    // 总结
    if (t.summary) { sum.classList.remove("hidden"); sum.innerHTML = "<h3>总结</h3><pre>" + esc(t.summary) + "</pre>"; }
    else sum.classList.add("hidden");

    // 顶部按钮：按状态给不同动作，**任何状态都至少留一个能点的按钮**
    var runBtn = el("agentRunBtn");
    var abortBtn = el("agentAbortBtn");
    var RUN_LABEL = { planned: "开始执行", aborted: "继续执行", failed: "再试一次" };
    if (runBtn) {
      var canRun = !!RUN_LABEL[t.status] && !t.busy;
      runBtn.classList.toggle("hidden", !canRun);
      if (canRun) runBtn.innerHTML = '<i data-lucide="play" aria-hidden="true"></i>' + RUN_LABEL[t.status];
    }
    if (abortBtn) {
      abortBtn.classList.toggle("hidden",
        t.status !== "running" && t.status !== "awaiting_confirm"
        && t.status !== "planned" && t.status !== "planning");
    }

    // 顶部提示
    if (agState.editing) say("正在编辑参数，保存或取消后继续");
    else if (t.status === "planning") {
      // 计划阶段也要给出路：卡住时告诉用户能中止、能重排
      var secs = agState.t0 ? Math.round((Date.now() - agState.t0) / 1000) : 0;
      if (secs > 150) {
        say("已经等了 " + secs + " 秒还没出计划，多半是卡住了：点「中止」，再用「重新生成计划」重试", "warn");
      } else {
        say("正在生成计划，会打开一个浏览器窗口（约 30-60 秒），请勿关闭它…");
      }
    }
    else if (t.status === "aborted") {
      say(t.busy ? "正在停止上一个步骤，稍等再点「继续执行」" : "已中止；未执行的步骤都留着，可以改完继续跑", "warn");
    }
    else if (t.status === "awaiting_confirm") say("有步骤要写库，请确认后继续", "warn");
    else if (t.status === "running") say("执行中，请稍候…");
    else if (t.status === "planned") say("计划已就绪：可直接执行，也可以先改某一步的参数");
    else if (t.status === "done") say("任务完成", "ok");
    else if (t.status === "failed") say(t.error || "任务失败", "err");

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
    agState.editing = null;
    el("agentSummary").classList.add("hidden");
    el("agentPlanCard").classList.add("hidden");
    el("agentLogWrap").classList.add("hidden");
    try {
      var r = await postJSON("/api/agent/plan", { task: text, backend: agentBackend });
      agState.id = r.id;
      agState.last = r.task;
      renderTask(r.task);
      beginPoll();
    } catch (err) {
      say(err.message, "err");
      busyUI(false);
    }
  }

  async function onRun() {
    if (!agState.id) return;
    var label = (agState.last && agState.last.status === "aborted") ? "继续执行…"
      : (agState.last && agState.last.status === "failed") ? "再试一次…" : "开始执行…";
    try {
      agState.editing = null;
      say(label);
      await postJSON("/api/agent/run", { id: agState.id });
      agState.t0 = Date.now();
      beginPoll();
    } catch (e) { say(e.message, "err"); }
  }

  async function onAbort() {
    if (!agState.id) return;
    try {
      agState.editing = null;
      var r = await postJSON("/api/agent/abort", { id: agState.id });
      agState.last = r.task;
      renderTask(r.task);
      stopPoll(); startTickStop(r.task);
      // 后台线程可能还在把当前这一步跑完，继续轮询到它真正停下（busy 变 false）
      if (r.task && r.task.busy) agState.poll = setTimeout(pollOnce, 1500);
    } catch (e) { say(e.message, "err"); }
  }

  async function onGate(act, stepId) {
    if (!agState.id) return;
    try {
      agState.editing = null;
      say(act === "confirm" ? "已确认，继续执行…" : "已跳过该步骤…");
      await postJSON("/api/agent/" + (act === "confirm" ? "confirm" : "skip"),
        { id: agState.id, step_id: stepId });
      beginPoll();
    } catch (e) { say(e.message, "err"); }
  }

  // ---- 改参数 ----
  function collectArgs(stepId) {
    var li = document.querySelector('.plan-step[data-step="' + stepId + '"]');
    if (!li) return {};
    var args = {};
    li.querySelectorAll("[data-arg]").forEach(function (inp) {
      var k = inp.dataset.arg, kind = inp.dataset.kind;
      if (kind === "bool") args[k] = !!inp.checked;
      else if (kind === "int") {
        var n = parseInt(inp.value, 10);
        args[k] = isNaN(n) ? inp.value : n;
      } else if (kind === "array") {
        args[k] = inp.value.split("\n").map(function (x) { return x.trim(); })
          .filter(function (x) { return x.length > 0; });
      } else args[k] = inp.value;
    });
    return args;
  }

  async function onEdit(stepId) {
    if (!agState.id) return;
    try {
      var t = await getJSON("/api/agent/status?id=" + encodeURIComponent(agState.id));
      agState.last = t;
      agState.editing = stepId;
      renderTask(t);
      var form = document.querySelector('.plan-step[data-step="' + stepId + '"] .ps-form');
      if (form) form.scrollIntoView({ behavior: "smooth", block: "center" });
      var first = form && form.querySelector("input,textarea");
      if (first) first.focus();
    } catch (e) { say(e.message, "err"); }
  }

  async function onSave(stepId) {
    if (!agState.id) return;
    var args = collectArgs(stepId);
    try {
      var r = await postJSON("/api/agent/update", {
        id: agState.id, steps: [{ id: stepId, args: args }]
      });
      agState.editing = null;
      agState.last = r.task;
      renderTask(r.task);
      say("已保存该步的参数", "ok");
    } catch (e) { say("保存失败：" + e.message, "err"); }
  }

  function onCancelEdit() {
    agState.editing = null;
    if (agState.last) renderTask(agState.last);
  }

  async function onReplan() {
    if (!agState.id) return;
    try {
      agState.editing = null;
      say("正在重新生成计划…");
      await postJSON("/api/agent/replan", { id: agState.id });
      beginPoll();
    } catch (e) { say(e.message, "err"); }
  }

  async function onDelete() {
    if (!agState.id) return;
    try {
      await postJSON("/api/agent/delete", { id: agState.id });
      agState.id = null; agState.last = null; agState.editing = null;
      stopPoll();
      el("agentPlanCard").classList.add("hidden");
      el("agentLogWrap").classList.add("hidden");
      el("agentSummary").classList.add("hidden");
      el("agentExamples").classList.remove("hidden");
      say("已删除该任务记录", "ok");
      if (!el("agentHistory").classList.contains("hidden")) renderHistory();
    } catch (e) { say(e.message, "err"); }
  }

  // 一键清掉失败 / 已中止的残留任务，免得历史里堆一堆没用的
  async function onPurge() {
    var d;
    try { d = await getJSON("/api/agent/status"); } catch (e) { say(e.message, "err"); return; }
    var junk = (d.tasks || []).filter(function (r) {
      return r.status === "failed" || r.status === "aborted";
    });
    if (!junk.length) { say("没有需要清理的任务", "ok"); return; }
    if (!confirm("要删除 " + junk.length + " 个「失败 / 已中止」的任务记录吗？\n（只是清掉任务记录，不影响 Zotero 里的文献）")) return;
    try {
      for (var i = 0; i < junk.length; i++) {
        await postJSON("/api/agent/delete", { id: junk[i].id });
      }
      if (agState.id && junk.some(function (r) { return r.id === agState.id; })) {
        agState.id = null; agState.last = null;
        el("agentPlanCard").classList.add("hidden");
        el("agentLogWrap").classList.add("hidden");
        el("agentExamples").classList.remove("hidden");
      }
      say("已清理 " + junk.length + " 个任务记录", "ok");
      renderHistory();
    } catch (e) { say(e.message, "err"); }
  }

  document.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest("[data-act]") : null;
    if (!b) return;
    var act = b.dataset.act;
    if (act === "confirm" || act === "skip") onGate(act, b.dataset.step);
    else if (act === "edit") onEdit(b.dataset.step);
    else if (act === "save") onSave(b.dataset.step);
    else if (act === "cancel") onCancelEdit();
    else if (act === "replan") onReplan();
    else if (act === "delete") onDelete();
    else if (act === "purge") onPurge();
    else if (act === "run" || act === "retry") onRun();
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
    var nJunk = rows.filter(function (r) { return r.status === "failed" || r.status === "aborted"; }).length;
    var h = ['<div class="cap-head"><h3>历史任务</h3><span class="muted">共 ' + rows.length + " 个</span>"
      + '<span class="hist-tools">'
      + (nJunk ? '<button class="ghost btn-sm" data-act="purge">清理失败/已中止（' + nJunk + '）</button>' : "")
      + "</span></div>"];
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
    agState.editing = null;
    agState.t0 = 0;
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
