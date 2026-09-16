// -*- coding: utf-8 -*-
// 前端逻辑：输入(key/PDF) → 生成 → 展示目标条目 → 确认回写。
const $ = (id) => document.getElementById(id);

let current = null; // { markdown, target }

async function postJSON(url, body, isForm = false) {
  const opt = isForm
    ? { method: "POST", body }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(url, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `请求失败 ${r.status}`);
  return data;
}

// ---- 设置 ----
$("settingsBtn").onclick = async () => {
  const p = $("settingsPanel");
  p.classList.toggle("hidden");
  if (!p.classList.contains("hidden") && !$("apiKey").value) {
    try {
      const s = await (await fetch("/api/settings")).json();
      $("apiKey").value = s.api_key || "";
      $("model").value = s.model || "";
      $("baseUrl").value = s.base_url || "";
    } catch (e) {}
  }
};
$("saveSettings").onclick = async () => {
  try {
    await postJSON("/api/settings", {
      api_key: $("apiKey").value.trim(),
      model: $("model").value.trim(),
      base_url: $("baseUrl").value.trim(),
    });
    $("settingsHint").textContent = "已保存 ✓";
  } catch (e) {
    $("settingsHint").textContent = "保存失败：" + e.message;
  }
};

// ---- 后端切换：显示/隐藏网页端地址 ----
document.querySelectorAll("input[name=backend]").forEach((r) => {
  r.addEventListener("change", () => {
    const v = (document.querySelector("input[name=backend]:checked") || {}).value;
    $("webchatRow").classList.toggle("hidden", v !== "webchat");
  });
});

// ---- 从 Zotero 选择 ----
const ZP_WARN_MSG =
  "⚠️ 未检测到 Zotero。请先打开 <b>Zotero 桌面端</b>，并在「编辑 → 设置 → 高级」中勾选" +
  "<b>“允许其他应用与 Zotero 通信”</b>，然后点「最近」重试。";

function showZpWarn(msg) {
  const w = $("zpWarn");
  if (!w) return;
  w.innerHTML = msg;
  w.classList.remove("hidden");
}
function hideZpWarn() {
  const w = $("zpWarn");
  if (w) w.classList.add("hidden");
}
async function checkZoteroAlive() {
  try {
    const r = await (await fetch("/api/zotero_ping")).json();
    return !!r.ok;
  } catch (e) {
    return false;
  }
}
async function ensureZotero() {
  hideZpWarn();
  $("zpStatus").textContent = "检查 Zotero 连接…";
  if (!(await checkZoteroAlive())) {
    $("zpStatus").textContent = "";
    $("zpTree").innerHTML = "";
    $("zpList").innerHTML = "";
    showZpWarn(ZP_WARN_MSG);
    return;
  }
  loadZoteroTree();
  loadZoteroRecent();
}

// 从 Zotero 选择：各面板共用同一弹窗，用回调区分选中后的去向
let _zpOnPick = null;
let _zpRequirePdf = false;

function openZoteroPicker(onPick, opts) {
  _zpOnPick = onPick || null;
  _zpRequirePdf = !!((opts || {}).requirePdf);
  $("zoteroPicker").classList.remove("hidden");
  ensureZotero();
}

$("pickZoteroBtn").onclick = () => openZoteroPicker(null);
$("zpClose").onclick = () => $("zoteroPicker").classList.add("hidden");
$("zpRecentBtn").onclick = ensureZotero;
$("zpSearchBtn").onclick = async () => {
  const q = $("zpSearch").value.trim();
  if (!q) { ensureZotero(); return; }
  if (!(await checkZoteroAlive())) { showZpWarn(ZP_WARN_MSG); return; }
  hideZpWarn();
  loadZoteroSearch(q);
};
$("zpSearch").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("zpSearchBtn").click();
});

async function loadZoteroRecent() {
  $("zpStatus").textContent = "读取最近条目…";
  try {
    const items = await (await fetch("/api/zotero_items?limit=80")).json();
    renderZoteroList(items);
  } catch (e) {
    $("zpStatus").textContent = "读取失败：" + e.message;
  }
}

async function loadZoteroSearch(q) {
  $("zpStatus").textContent = "搜索中…";
  try {
    const items = await (await fetch("/api/zotero_search?q=" + encodeURIComponent(q))).json();
    renderZoteroList(items);
  } catch (e) {
    $("zpStatus").textContent = "搜索失败：" + e.message;
  }
}

// ---- 分类树 ----
async function loadZoteroTree() {
  const box = $("zpTree");
  box.innerHTML = '<div class="zp-status muted">读取分类…</div>';
  try {
    const tree = await (await fetch("/api/zotero_collections")).json();
    box.innerHTML = "";
    if (!tree || !tree.length) {
      box.innerHTML = '<div class="zp-status muted">无分类</div>';
      return;
    }
    renderTree(tree, box, 0);
  } catch (e) {
    box.innerHTML = '<div class="zp-status">读取分类失败</div>';
  }
}

function renderTree(nodes, container, depth) {
  nodes.forEach((n) => {
    const row = document.createElement("div");
    row.className = "zp-node";
    row.style.paddingLeft = 6 + depth * 14 + "px";
    const hasCh = n.children && n.children.length;
    row.innerHTML =
      `<span class="zp-caret">${hasCh ? "▾" : "·"}</span>` +
      `<span class="zp-name">${escapeHtml(n.name)}</span>`;
    const childWrap = document.createElement("div");
    childWrap.className = "zp-children";
    container.appendChild(row);
    container.appendChild(childWrap);
    row.onclick = (e) => {
      if (e.target.classList.contains("zp-caret") && hasCh) {
        const hidden = childWrap.classList.toggle("hidden");
        e.target.textContent = hidden ? "▸" : "▾";
        return;
      }
      $("zpTree").querySelectorAll(".zp-node").forEach((x) => x.classList.remove("sel"));
      row.classList.add("sel");
      loadCollectionItems(n.key, n.name);
    };
    if (hasCh) renderTree(n.children, childWrap, depth + 1);
  });
}

async function loadCollectionItems(key, name) {
  $("zpStatus").textContent = `读取「${name}」…`;
  try {
    const items = await (
      await fetch("/api/zotero_collection_items?key=" + encodeURIComponent(key))
    ).json();
    renderZoteroList(items);
  } catch (e) {
    $("zpStatus").textContent = "读取失败：" + e.message;
  }
}

function renderZoteroList(items, onPick) {
  const ul = $("zpList");
  ul.innerHTML = "";
  if (!items || !items.length) {
    $("zpStatus").textContent = "没有匹配的文献条目";
    return;
  }
  $("zpStatus").textContent = `共 ${items.length} 条`;
  items.forEach((it) => {
    const li = document.createElement("li");
    li.className = "zp-item" + (it.has_pdf ? "" : " no-pdf");
    const pdfTag = it.has_pdf
      ? '<span class="zp-pdf ok">含 PDF</span>'
      : '<span class="zp-pdf warn">无 PDF</span>';
    li.innerHTML =
      `<div class="zp-title">${escapeHtml(it.title)}</div>` +
      `<div class="zp-meta">${pdfTag} <span class="muted">${escapeHtml(it.itemType || "")} · ${escapeHtml(it.key)}</span></div>`;
    li.onclick = () => {
      const cb = onPick || _zpOnPick;
      if (_zpRequirePdf && !it.has_pdf) {
        $("zpStatus").textContent = `「${it.title}」没有 PDF 附件，无法做长文精读（请先走 PDF 入库或用上传入口）。`;
        return;
      }
      $("zoteroPicker").classList.add("hidden");
      _zpRequirePdf = false;
      if (cb) { cb(it); _zpOnPick = null; return; }
      $("zoteroKey").value = it.title;          // 显示人话（标题），key 存在 dataset
      $("zoteroKey").dataset.key = it.key;
      // 清掉上一次的旧结果，避免误把旧笔记当本次生成
      $("result").classList.add("hidden");
      $("noteEditor").value = "";
      $("noteTitle").textContent = "";
      if ($("writeStatus")) $("writeStatus").textContent = "";
      $("genStatus").textContent = `已选「${it.title}」，点「生成六维笔记」开始。`;
    };
    ul.appendChild(li);
  });
}

// ---- 主页上传：选择文件后立即反馈并引导下一步 ----
$("pdfFile").addEventListener("change", () => {
  const f = $("pdfFile").files[0];
  $("result").classList.add("hidden");
  if (f) {
    $("genStatus").innerHTML =
      `已选择 <b>${f.name}</b>（${(f.size / 1024).toFixed(0)} KB）——点「生成六维笔记」：<br>` +
      `<span class="muted">自动存入 Zotero（建条目+挂附件）→ 表格解析（首次约 1-3 分钟）→ 生成笔记 → 确认回写。</span>`;
  } else {
    $("genStatus").textContent = "";
  }
});

// ---- 生成 ----
$("zoteroKey").addEventListener("dblclick", () => {   // 双击清空，便于重选
  $("zoteroKey").value = "";
  $("zoteroKey").dataset.key = "";
  $("genStatus").textContent = "";
});

$("genBtn").onclick = async () => {
  const key = ($("zoteroKey").dataset.key || "").trim();
  const file = $("pdfFile").files[0];
  if (!key && !file) {
    $("genStatus").textContent = "请先「从 Zotero 选择」挑一篇，或上传 PDF 文件";
    return;
  }
  $("genStatus").textContent = "生成中…";
  try {
    let data;
    const backend = (document.querySelector('input[name=backend]:checked') || {}).value || "webchat";
    if (file) {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("backend", backend);
      if ($("webchatUrl")) fd.append("webchat_url", $("webchatUrl").value.trim());
      data = await postJSON("/api/process", fd, true);
    } else {
      data = await postJSON("/api/process_key", {
        zotero_key: key,
        backend,
        webchat_url: $("webchatUrl") ? $("webchatUrl").value.trim() : "",
      });
    }
    current = { markdown: data.markdown, format: data.note_format || "markdown", target: data.target };
    $("noteTitle").textContent = data.title || "";
    $("notePages").textContent = data.pages ? `${data.pages} 页` : "";
    $("noteEditor").value = data.markdown;
    renderPreview(data.markdown, current.format);
    renderTarget(data.target);
    $("result").classList.remove("hidden");
    $("genStatus").textContent = (data.warning ? data.warning + " " : "") + "完成。请确认下方条目后回写。";
    $("writeStatus").textContent = "";
  } catch (e) {
    $("genStatus").textContent = "失败：" + e.message;
  }
};

function renderTarget(t) {
  if (!t) return;
  const badge =
    t.action === "create_new" ? '<span class="badge new">将新建条目</span>'
    : t.action === "created_parent" ? '<span class="badge fix">已自动建父条目</span>'
    : '<span class="badge ok">已有条目</span>';
  const keyLine = t.target_key ? `（key: <code>${t.target_key}</code>）` : "";
  $("targetCard").innerHTML =
    `<div class="tc-title">将挂到：${escapeHtml(t.target_title || "（未命名）")} ${keyLine}</div>` +
    `<div class="tc-meta">${badge} <span class="muted">${escapeHtml(t.detail || "")}</span></div>`;
}

function renderPreview(content, format) {
  if (format === "html") {
    $("preview").innerHTML = content || "";
  } else {
    $("preview").innerHTML = marked.parse(content || "");
  }
}

// 编辑时实时预览
$("noteEditor").addEventListener("input", (e) => {
  if (current) current.markdown = e.target.value;
  renderPreview(e.target.value, current ? current.format : "markdown");
});

// ---- 确认回写 ----
$("writeBtn").onclick = async () => {
  if (!current) return;
  const md = $("noteEditor").value.trim();
  if (!md) {
    $("writeStatus").textContent = "笔记为空，无法回写";
    return;
  }
  $("writeStatus").textContent = "回写中…";
  try {
    const tags = $("tagsInput").value.split(/\s+/).filter((x) => x.trim());
    const res = await postJSON("/api/writeback", {
      markdown: md,
      note_format: current.format || "markdown",
      target_key: current.target.target_key || "",
      target_title: current.target.target_title || "",
      action: current.target.action || "attach_existing",
      tags,
    });
    $("writeStatus").textContent =
      `已回写 ✓ 挂到「${res.target_title || ""}」(key: ${res.target_key})`;
  } catch (e) {
    $("writeStatus").textContent = "回写失败：" + e.message;
  }
};

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ---- 论文对比（paper-set） ----
let PS_DIMS = ["对象", "方法", "材料", "核心发现", "量化", "对话"];
let PS_ROWS = [];

$("papersetBtn").onclick = () => {
  $("papersetPanel").classList.remove("hidden");
  loadPaperset();
};
$("psClose").onclick = () => $("papersetPanel").classList.add("hidden");
$("psFilter").addEventListener("input", renderPaperset);
$("psExport").onclick = exportPapersetCsv;

async function loadPaperset() {
  $("psStatus").textContent = "读取 Zotero 笔记…";
  try {
    PS_ROWS = await (await fetch("/api/paperset")).json();
    if (!Array.isArray(PS_ROWS)) PS_ROWS = [];
    const ok = PS_ROWS.filter((r) => r.parsed).length;
    $("psStatus").textContent = `共 ${PS_ROWS.length} 篇（已解析六维 ${ok} 篇）`;
    renderPaperset();
  } catch (e) {
    $("psStatus").textContent = "读取失败：" + e.message;
  }
}

function _psFiltered() {
  const q = ($("psFilter").value || "").trim().toLowerCase();
  if (!q) return PS_ROWS;
  return PS_ROWS.filter((r) => {
    const hay = [
      r.title, r.year, (r.tags || []).join(" "),
      ...PS_DIMS.map((d) => (r.dims || {})[d] || ""),
    ].join(" ").toLowerCase();
    return hay.includes(q);
  });
}

function renderPaperset() {
  const rows = _psFiltered();
  const head = ["年份", "标题", ...PS_DIMS, "标签"];
  let html = "<thead><tr>" + head.map((h) => `<th>${escapeHtml(h)}</th>`).join("") + "</tr></thead><tbody>";
  rows.forEach((r) => {
    html += "<tr>";
    html += `<td>${escapeHtml(r.year || "")}</td>`;
    html += `<td class="ps-title">${escapeHtml(r.title || "")}</td>`;
    PS_DIMS.forEach((d) => {
      const v = (r.dims || {})[d] || "";
      html += `<td class="ps-dim">${v ? escapeHtml(v) : '<span class="muted">—</span>'}</td>`;
    });
    html += `<td>${(r.tags || []).map((t) => `<span class="ps-tag">${escapeHtml(t)}</span>`).join(" ")}</td>`;
    html += "</tr>";
  });
  html += "</tbody>";
  $("psTable").innerHTML = html;
}

function exportPapersetCsv() {
  const rows = _psFiltered();
  const head = ["年份", "标题", ...PS_DIMS, "标签"];
  const esc = (s) => `"${String(s == null ? "" : s).replace(/"/g, '""')}"`;
  const lines = [head.map(esc).join(",")];
  rows.forEach((r) => {
    const vals = [
      r.year || "", r.title || "",
      ...PS_DIMS.map((d) => (r.dims || {})[d] || ""),
      (r.tags || []).join(" "),
    ];
    lines.push(vals.map(esc).join(","));
  });
  const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "paper-set.csv";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
}

// ---- 批量笔记 ----
let BATCH_KEYS = [];
let BATCH_CANDS = [];
let BATCH_TIMER = null;

$("batchBtn").onclick = () => {
  $("batchPanel").classList.remove("hidden");
  loadBatchScopes();
  pollBatchOnce();
};
$("batchClose").onclick = () => $("batchPanel").classList.add("hidden");
$("batchLoad").onclick = loadBatchCandidates;
$("batchStart").onclick = startBatch;
$("batchStop").onclick = stopBatch;

async function loadBatchScopes() {
  const sel = $("batchScope");
  if (sel.options.length) return;
  sel.innerHTML = '<option value="">全部（含未分类）</option>';
  try {
    const tree = await (await fetch("/api/zotero_collections")).json();
    const walk = (ns, d) => (ns || []).forEach((n) => {
      const o = document.createElement("option");
      o.value = n.key;
      o.textContent = "　".repeat(d) + n.name;
      sel.appendChild(o);
      walk(n.children, d + 1);
    });
    walk(tree, 0);
  } catch (e) {}
}

async function loadBatchCandidates() {
  const col = $("batchScope").value;
  const skip = $("optSkipThesis").checked ? 1 : 0;
  $("batchCandCount").textContent = "读取中…";
  try {
    const items = await (await fetch(
      `/api/batch/candidates?collection=${encodeURIComponent(col)}&skip_thesis=${skip}`
    )).json();
    BATCH_KEYS = (items || []).map((x) => x.key);
    BATCH_CANDS = items || [];
    $("batchCandCount").textContent = `候选 ${BATCH_KEYS.length} 篇`;
    renderBatchResults([]);  // 读取候选即预填整表，状态列空白待处理
  } catch (e) {
    $("batchCandCount").textContent = "读取失败：" + e.message;
  }
}

async function startBatch() {
  if (!BATCH_KEYS.length) {
    $("batchProg").textContent = "请先点「读取候选」";
    return;
  }
  if (!confirm(`将对 ${BATCH_KEYS.length} 篇批量生成六维笔记（网页端、逐篇、耗时较长）。继续？`)) return;
  try {
    await postJSON("/api/batch/start", {
      keys: BATCH_KEYS,
      options: {
        skip_thesis: $("optSkipThesis").checked,
        auto_classify: $("optClassify").checked,
        writeback: $("optWriteback").checked,
      },
    });
    $("batchProg").textContent = "已启动…";
    pollBatch();
  } catch (e) {
    $("batchProg").textContent = "启动失败：" + e.message;
  }
}

async function stopBatch() {
  try { await postJSON("/api/batch/stop", {}); } catch (e) {}
}

async function pollBatchOnce() {
  try {
    const s = await (await fetch("/api/batch/status")).json();
    applyBatchStatus(s);
  } catch (e) {}
}

function pollBatch() {
  if (BATCH_TIMER) return;
  BATCH_TIMER = setInterval(async () => {
    try {
      const s = await (await fetch("/api/batch/status")).json();
      applyBatchStatus(s);
      if (!s.running) { clearInterval(BATCH_TIMER); BATCH_TIMER = null; }
    } catch (e) {}
  }, 3000);
}

function applyBatchStatus(s) {
  let eta = "";
  const okSecs = (s.results || []).filter((r) => r.secs).map((r) => r.secs);
  if (s.running && okSecs.length) {
    const avg = okSecs.reduce((a, b) => a + b, 0) / okSecs.length;
    const remain = Math.max(0, (s.total || 0) - (s.done || 0));
    eta = `　均 ${fmtSecs(avg)}/篇，预计剩余约 ${Math.ceil(remain * avg / 60)} 分钟`;
  }
  $("batchProg").textContent = `${s.done || 0}/${s.total || 0}　${s.current || ""}${eta}`;
  const box = $("batchLog");
  box.textContent = (s.log || []).slice(-60).join("\n");
  box.scrollTop = box.scrollHeight;
  renderBatchResults(s.results || []);
}

function fmtSecs(sec) {
  sec = Math.round(sec);
  return `${Math.floor(sec / 60)}′${String(sec % 60).padStart(2, "0")}″`;
}

function renderBatchResults(rows) {
  // 以候选清单为主视图：待处理=空白，完成=✓，失败=✗，跳过=⤼
  const done = {};
  (rows || []).forEach((r) => { done[r.key] = r; });
  const list = BATCH_CANDS.length
    ? BATCH_CANDS.map((c) => ({ ...c, status: "pending", ...(done[c.key] || {}) }))
    : (rows || []);
  let html = "<thead><tr><th>状态</th><th>标题</th><th>分类</th><th>用时</th><th>标签</th></tr></thead><tbody>";
  const SKIPPED_LABEL = { skipped: "学位论文", already_noted: "已有笔记", no_pdf: "无 PDF" };
  list.forEach((r) => {
    let badge = "";
    if (r.status === "ok") badge = '<span class="badge ok">✓</span>';
    else if (r.status === "error") badge = '<span class="badge new">✗</span>';
    else if (r.status !== "pending") badge = `<span class="muted" title="${SKIPPED_LABEL[r.status] || "跳过"}">⤼</span>`;
    const catCell = r.status === "already_noted" ? '<span class="muted">已有笔记，跳过</span>'
      : escapeHtml(r.category || "");
    const title = r.title || "";
    html += `<tr><td>${badge}</td><td class="ps-title">${escapeHtml(title)}</td>`
      + `<td>${catCell}</td>`
      + `<td>${r.secs ? fmtSecs(r.secs) : ""}</td>`
      + `<td>${(r.tags || []).map((t) => "#" + escapeHtml(t)).join(" ")}</td></tr>`;
  });
  html += "</tbody>";
  $("batchResult").innerHTML = html;
}

// ================= PDF 迁移入库 =================
$("pdfimpBtn").onclick = () => { $("pdfimpPanel").classList.remove("hidden"); scanPdfDownloads(); };
$("pdfimpClose").onclick = () => $("pdfimpPanel").classList.add("hidden");

$("pdfimpUpload").onclick = async () => {
  const files = $("pdfimpFiles").files;
  if (!files.length) { $("pdfimpResult").textContent = "请先选择 PDF 文件（可多选）。"; return; }
  $("pdfimpUpload").disabled = true;
  $("pdfimpResult").textContent = "上传中…";
  const fd = new FormData();
  [...files].forEach((f) => fd.append("files", f));
  try {
    const res = await (await fetch("/api/pdf_import/upload", { method: "POST", body: fd })).json();
    let ok = 0, html = "";
    (res.results || []).forEach((r) => {
      html += r.ok ? `<div>✓ ${escapeHtml(r.file)} → 条目 ${r.item}（${escapeHtml(r.title || "")}）</div>`
                   : `<div style="color:#b00">✗ ${escapeHtml(r.file)}：${escapeHtml(r.error || "")}</div>`;
      if (r.ok) ok++;
    });
    $("pdfimpResult").innerHTML = `入库成功 ${ok}/${res.results.length}<br>` + html;
    $("pdfimpFiles").value = "";
    scanPdfDownloads();
  } catch (e) {
    $("pdfimpResult").textContent = "上传失败：" + e.message;
  }
  $("pdfimpUpload").disabled = false;
};

let pdfDirHandle = null;   // File System Access API 文件夹句柄（会话内有效）

$("pdfimpPickDir").onclick = async () => {
  if (!window.showDirectoryPicker) {
    alert("当前浏览器不支持文件夹选择器，请使用 Chrome / Edge。");
    return;
  }
  try {
    pdfDirHandle = await window.showDirectoryPicker({ mode: "readwrite" });
    $("pdfimpDirName").textContent = "已选择：" + pdfDirHandle.name;
    await scanPdfDownloads();
  } catch (e) { /* 用户取消选择 */ }
};

async function scanPdfDownloads() {
  const box = $("pdfimpDownloads");
  if (!pdfDirHandle) {
    box.innerHTML = '<li class="muted">点「选择下载文件夹」后列出其中的 PDF。</li>';
    return;
  }
  let importedSet = new Set();
  try {
    const lst = await (await fetch("/api/pdf_import/imported")).json();
    importedSet = new Set(lst.map((e) => e.name + "|" + e.size));
  } catch (e) {}
  const rows = [];
  try {
    for await (const [name, handle] of pdfDirHandle.entries()) {
      if (handle.kind !== "file" || !name.toLowerCase().endsWith(".pdf")) continue;
      const f = await handle.getFile();
      const imported = importedSet.has(name + "|" + f.size);
      rows.push(`<li>${imported ? '<span class="badge ok">已入库</span> ' : ""}` +
        `${escapeHtml(name)} <span class="muted">${(f.size / 1024).toFixed(0)}KB · ` +
        `${new Date(f.lastModified).toLocaleDateString()}</span>` +
        (imported ? ` <button class="ghost" data-name="${escapeHtml(name)}" data-size="${f.size}" onclick="deletePdfDownloaded(this)">清理原文件</button>` : "") +
        `</li>`);
    }
  } catch (e) {
    rows.push('<li class="muted">读取文件夹失败：' + escapeHtml(e.message) + "</li>");
  }
  box.innerHTML = rows.join("") || '<li class="muted">该文件夹中没有 PDF。</li>';
}

async function deletePdfDownloaded(btn) {
  const name = btn.dataset.name, size = parseInt(btn.dataset.size, 10);
  if (!confirm(`确认从下载文件夹删除原文件？\n${name}\n（Zotero 中已入库，删除的是下载残留副本）`)) return;
  btn.disabled = true;
  try {
    await pdfDirHandle.removeEntry(name);   // 客户端直接删除原文件
    await postJSON("/api/pdf_import/mark_deleted", { name, size });
    scanPdfDownloads();
  } catch (e) {
    alert("删除失败：" + e.message); btn.disabled = false;
  }
}


// PDF 入库：选择器交互增强
$("pdfimpPick").onclick = () => $("pdfimpFiles").click();
$("pdfimpFiles").onchange = () => {
  const fs = [...$("pdfimpFiles").files];
  $("pdfimpPicked").textContent = fs.length
    ? `已选择 ${fs.length} 个文件：` + fs.map((f) => f.name).join("、")
    : "尚未选择文件。";
};

// 领域包：顶栏标题由 config/domain.json 驱动
(async () => {
  try {
    const d = await (await fetch("/api/domain")).json();
    if (d.app_title) $("appTitle").textContent = d.app_title;
    if (d.dimensions && d.dimensions.length) PS_DIMS = d.dimensions;
    document.title = d.app_title || document.title;
  } catch (e) {}
})();

// ================= 工具箱 =================
$("toolsBtn").onclick = () => { $("toolsPanel").classList.remove("hidden"); loadToolReports(); };
$("toolsClose").onclick = () => $("toolsPanel").classList.add("hidden");

const TOOL_LABELS = {
  lit_watch: "订阅清单", coverage: "覆盖矩阵", digest: "素材库", pack: "写作素材包",
  tag_analysis: "标签扫描", verify_tags: "标签核验", dedup: "重复扫描", tag_merge: "标签归并预览",
};

async function runTool(tool, apply = false, confirmText = "") {
  if (apply && !confirm(confirmText || "此操作会实际修改 Zotero 文库，确认执行？")) return;
  const out = $("toolOutput");
  out.textContent = `⏳ 运行 ${TOOL_LABELS[tool] || tool}${apply ? "（apply）" : "（dry-run）"} …`;
  try {
    const res = await postJSON("/api/tool/run", { tool, apply });
    let text = res.output || "(无输出)";
    if (res.ok && res.output_file) text += `\n\n✔ 产物：${res.output_file}`;
    out.textContent = text;
    if (tool === "coverage" || tool === "lit_watch" || tool === "pack") loadToolReports();
  } catch (e) {
    out.textContent = "✗ 运行失败：" + e.message;
  }
}

$("toolLitWatch").onclick = () => runTool("lit_watch");
$("toolCoverage").onclick = () => runTool("coverage");
$("toolDigest").onclick = () => runTool("digest");
$("toolPack").onclick = () => runTool("pack");
$("toolSnapshot").onclick = async () => {
  $("toolOutput").textContent = "正在为已有解析的文献补建 MinerU 子笔记…";
  try {
    const r = await postJSON("/api/mineru/snapshot_all", {});
    $("toolOutput").textContent = `完成：新建 ${r.created} 篇 / 更新 ${r.updated} 篇 / 失败 ${r.failed} 篇`;
  } catch (e) { $("toolOutput").textContent = "失败：" + e.message; }
};
$("toolTagScan").onclick = () => runTool("tag_analysis");
$("toolTagVerify").onclick = () => runTool("verify_tags");
$("toolDedup").onclick = () => runTool("dedup");
$("toolTagMerge").onclick = () => runTool("tag_merge");
$("toolDedupApply").onclick = () => runTool("dedup", true,
  "将按去重方案实际删除 Zotero 中的重复条目（走回收站）。\n确认已审阅过清单并要执行？");
$("toolTagMergeApply").onclick = () => runTool("tag_merge", true,
  "将按归并方案实际修改 Zotero 中的标签。\n确认已审阅过清单并要执行？");

async function loadToolReports() {
  try {
    const rows = await (await fetch("/api/tool/reports")).json();
    $("toolReports").innerHTML = rows.map((r) =>
      `<li><a href="/reports/${encodeURIComponent(r.name)}" target="_blank">${escapeHtml(r.name)}</a> <span class="muted">${(r.size / 1024).toFixed(0)}KB</span></li>`).join("");
  } catch (e) {
    $("toolReports").innerHTML = '<li class="muted">列表加载失败</li>';
  }
}

// ================= 语义检索（知识库） =================
$("kbBtn").onclick = async () => { $("kbPanel").classList.remove("hidden"); loadKbStats(); };
$("kbClose").onclick = () => $("kbPanel").classList.add("hidden");

async function loadKbStats() {
  try {
    const st = await (await fetch("/api/kb/stats")).json();
    $("kbStats").textContent = `已索引 ${st.items} 篇 / ${st.chunks} 块（表格 ${(st.by_kind || {}).table || 0} 块）· 模型 ${st.model || "未配置"}`;
  } catch (e) { $("kbStats").textContent = "统计失败：" + e.message; }
}

async function runKbSearch() {
  const q = $("kbQuery").value.trim();
  if (!q) { $("kbResults").innerHTML = '<li class="muted">请输入检索内容。</li>'; return; }
  $("kbResults").innerHTML = '<li class="muted">检索中…</li>';
  try {
    const res = await postJSON("/api/kb/search", { query: q, top_k: 10 });
    const rows = res.results || [];
    if (!rows.length) { $("kbResults").innerHTML = '<li class="muted">没有命中（可能这些文献还没建索引）。</li>'; return; }

    // 按文献聚合：一篇一组，组内按相似度列出命中块
    const groups = [];
    const idx = {};
    rows.forEach((r) => {
      const k = r.item_key;
      if (idx[k] === undefined) { idx[k] = groups.length; groups.push({ title: r.title || k, key: k, hits: [] }); }
      groups[idx[k]].hits.push(r);
    });

    const html = groups.map((g) => {
      const head = `<div style="margin:.5em 0 .2em"><b>${escapeHtml(g.title)}</b> ` +
        `<span class="muted">命中 ${g.hits.length} 处</span> · ` +
        `<a href="/api/mineru/${g.key}" target="_blank">查看解析</a></div>`;
      const hits = g.hits.map((r) => {
        const tag = r.kind === "table"
          ? '<span class="badge fix">表格</span>'
          : '<span class="badge ok">正文</span>';
        let body;
        if (r.kind === "table") {
          body = `<div class="kb-table">${cleanTableHtml(r.snippet)}</div>`;
        } else {
          body = `<div style="font-size:.9em;line-height:1.5">${escapeHtml(r.snippet)}</div>`;
        }
        return `<li style="margin:.35em 0;padding-left:.6em;border-left:3px solid #e3ded4">` +
          `<div>${tag} <span class="muted" style="font-size:.85em">${escapeHtml(r.section || "")}` +
          ` · ${r.score}${r.source ? "（" + escapeHtml(r.source) + "）" : ""}</span></div>${body}</li>`;
      }).join("");
      return `<li style="margin:.7em 0">${head}<ul style="list-style:none;padding-left:0;margin:.2em 0">${hits}</ul></li>`;
    }).join("");

    $("kbResults").innerHTML = `<li class="muted" style="list-style:none">共命中 ${groups.length} 篇 / ${rows.length} 处</li>` + html;
  } catch (e) {
    $("kbResults").innerHTML = '<li class="muted">检索失败：' + escapeHtml(e.message) + "</li>";
  }
}

// 把 MinerU 的 HTML 表格片段清理成可读表格（去掉 class/rowspan 冗余，保留 colspan）
function cleanTableHtml(html) {
  let t = html || "";
  t = t.replace(/\s+class="[^"]*"/g, "").replace(/\s+rowspan=1/g, "").replace(/\s+colspan=1/g, "");
  t = t.replace(/<table/g, '<table style="border-collapse:collapse;font-size:.82em;margin:.3em 0"');
  t = t.replace(/<t[dh]/g, (m) => '<span></span>' + m).replace(/<span><\/span>/g, "");
  t = t.replace(/<td/g, '<td style="border:1px solid #d8d3c8;padding:2px 6px"');
  t = t.replace(/<th/g, '<th style="border:1px solid #d8d3c8;padding:2px 6px;background:#f6f3ec"');
  return `<div style="max-height:220px;overflow:auto">${t}</div>`;
}

$("kbSearchBtn").onclick = runKbSearch;
$("kbQuery").addEventListener("keydown", (e) => { if (e.key === "Enter") runKbSearch(); });
$("kbIndexBtn").onclick = async () => {
  $("kbResults").innerHTML = '<li class="muted">正在为已有解析的文献建索引…</li>';
  try {
    const res = await postJSON("/api/kb/index", {});
    $("kbResults").innerHTML = (res.details || []).map((d) => `<li>${escapeHtml(d)}</li>`).join("")
      || '<li class="muted">没有待索引的文献（新文献入库解析后再点这里）。</li>';
    loadKbStats();
  } catch (e) { $("kbResults").innerHTML = '<li class="muted">建索引失败：' + escapeHtml(e.message) + "</li>"; }
};

// ================= 长文精读（学位论文 / 专著） =================
let _longdocKey = "";      // 从 Zotero 选择器选中的条目（不再要求手填 key）

$("longdocBtn").onclick = () => $("longdocPanel").classList.remove("hidden");

$("longdocPick").onclick = () => openZoteroPicker((it) => {
  _longdocKey = it.key;
  $("longdocPickedItem").innerHTML =
    `已选「<b>${escapeHtml(it.title)}</b>」` +
    (it.has_pdf ? "" : ' <span class="muted">（已解析过则可直接用）</span>');
}, { requirePdf: true });
$("longdocClose").onclick = () => $("longdocPanel").classList.add("hidden");

async function refreshLongdoc() {
  try {
    const r = await (await fetch("/api/longdoc/status")).json();
    $("longdocLog").textContent = (r.log || []).join("\n") || "（尚无进度）";
    if (r.result && !r.running) {
      $("longdocLog").textContent += "\n\n" + (r.result.ok
        ? `✔ 完成：${r.result.title}｜${r.result.chapters} 章｜笔记 ${r.result.note_chars} 字符`
        : "✗ 失败：" + (r.result.error || ""));
    }
  } catch (e) { $("longdocLog").textContent = "读取失败：" + e.message; }
}

$("longdocFile").addEventListener("change", () => {
  const f = $("longdocFile").files[0];
  $("longdocPicked").textContent = f ? `已选择 ${f.name}（${(f.size / 1048576).toFixed(1)} MB）——点「开始精读」` : "尚未选择文件。";
});

$("longdocUpload").onclick = async () => {
  const f = $("longdocFile").files[0];
  if (!f) { alert("请先选择 PDF 文件"); return; }
  const maxCh = $("longdocMax").value.trim();
  const fd = new FormData();
  fd.append("file", f);
  if (maxCh) fd.append("max_chapters", maxCh);
  fd.append("backend", $("longdocBackend") ? $("longdocBackend").value : "webchat");
  $("longdocLog").textContent = "已开始：正在入库 → MinerU 解析 → 分章精读…";
  try {
    await postJSON("/api/longdoc/upload", fd, true);
    setInterval(refreshLongdoc, 5000);
  } catch (e) {
    $("longdocLog").textContent = "启动失败：" + e.message;
  }
};

$("longdocStart").onclick = async () => {
  const key = _longdocKey;
  if (!key) { alert("请先点「从 Zotero 选择」挑一篇文献"); return; }
  const maxCh = parseInt($("longdocMax").value, 10);
  try {
    await postJSON("/api/longdoc/start", { item_key: key, max_chapters: isNaN(maxCh) ? null : maxCh,
      backend: $("longdocBackend") ? $("longdocBackend").value : "webchat" });
    $("longdocLog").textContent = "已启动，正在分章精读…（每章约 20–60 秒，可点「刷新进度」查看）";
    setInterval(refreshLongdoc, 5000);
  } catch (e) {
    $("longdocLog").textContent = "启动失败：" + e.message;
  }
};
$("longdocStatusBtn").onclick = refreshLongdoc;
