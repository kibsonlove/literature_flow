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
let _settingsLoaded = false;

async function loadZoteroWriteStatus() {
  try {
    const s = await (await fetch("/api/zotero/write_status")).json();
    const el = $("zoteroWriteStatus");
    el.textContent = s.authorized ? "已授权，可正常写入" : "未授权 —— 写入会被 Zotero 拒绝";
    el.style.color = s.authorized ? "#1f7a37" : "#b3244b";
  } catch (e) { $("zoteroWriteStatus").textContent = "状态读取失败"; }
}

$("zoteroAuthorize").onclick = async () => {
  $("settingsHint").textContent = "已在 Zotero 弹出授权窗口，请选择「Always Allow」…";
  $("zoteroAuthorize").disabled = true;
  try {
    await postJSON("/api/zotero/authorize", { app_name: "literature_flow" });
    $("settingsHint").textContent = "授权成功，写入密钥已保存 ✓";
    loadZoteroWriteStatus();
  } catch (e) {
    $("settingsHint").textContent = "授权失败：" + e.message;
  } finally {
    $("zoteroAuthorize").disabled = false;
  }
};

$("settingsBtn").onclick = async () => {
  const p = $("settingsPanel");
  p.classList.toggle("hidden");
  if (!p.classList.contains("hidden")) {
    loadZoteroWriteStatus();
    if (_settingsLoaded) return;
    _settingsLoaded = true;
    try {
      const s = await (await fetch("/api/settings")).json();
      $("apiKey").value = s.api_key || "";
      $("model").value = s.model || "";
      $("baseUrl").value = s.base_url || "";
      $("openalexKey").value = s.openalex_key || "";
      await loadPathsInto();
      await loadEmbeddingInto();
      await loadMineruInto();
    } catch (e) { _settingsLoaded = false; }
  }
};

// 本机路径：未配置时用自动探测结果预填，但仍需点「保存」才写盘，避免误以为已经配好
async function loadPathsInto() {
  const d = await (await fetch("/api/paths")).json();
  const dir = d.zotero_data_dir || "";
  const exe = d.zotero_exe || "";
  const det = d.detected || {};
  const guessDir = (det.data_dir || {}).path || "";
  const guessExe = (det.zotero_exe || {}).path || "";
  $("zoteroDataDir").value = dir || guessDir;
  $("zoteroExe").value = exe || guessExe;
  if (!dir && guessDir) _paintPathsHint("已自动探测到路径，点「保存」生效");
  else if (!dir) _paintPathsHint("没探测到 Zotero 数据目录，请手填或用「浏览数据目录…」");
  else _paintPathsHint("");
  // 数据目录与高级目录：只回填"用户显式填过的值"，留空时靠 placeholder 说明默认位置
  $("dataDir").value = d.data_dir || "";
  $("pwBrowsersDir").value = d.playwright_browsers_dir || "";
  $("webchatProfileDir").value = d.webchat_profile_dir || "";
  const eff = d.effective || {};
  _paintDataDirHint(eff.data_dir ? "当前生效：" + eff.data_dir : "");
  _pollPw();   // 顺便刷新浏览器内核的安装状态
}

async function loadEmbeddingInto() {
  const e = await (await fetch("/api/embedding")).json();
  $("embKey").value = e.api_key || "";
  $("embBaseUrl").value = e.base_url || "";
  $("embModel").value = e.model || "";
}

async function loadMineruInto() {
  const m = await (await fetch("/api/mineru/key")).json();
  $("mineruKey").value = m.api_key || "";
}

$("saveSettings").onclick = async () => {
  const h = $("settingsHint");
  h.textContent = "保存中…";
  try {
    await postJSON("/api/settings", {
      api_key: $("apiKey").value.trim(),
      model: $("model").value.trim(),
      base_url: $("baseUrl").value.trim(),
      openalex_key: $("openalexKey").value.trim(),
    });
    const p = await postJSON("/api/paths", {
      zotero_data_dir: $("zoteroDataDir").value.trim(),
      zotero_exe: $("zoteroExe").value.trim(),
      data_dir: $("dataDir").value.trim(),
      playwright_browsers_dir: $("pwBrowsersDir").value.trim(),
      webchat_profile_dir: $("webchatProfileDir").value.trim(),
    });
    await postJSON("/api/embedding", {
      api_key: $("embKey").value.trim(),
      base_url: $("embBaseUrl").value.trim(),
      model: $("embModel").value.trim(),
    });
    await postJSON("/api/mineru/key", { api_key: $("mineruKey").value.trim() });

    if (p.zotero_data_dir && !p.zotero_data_dir_ok) {
      h.textContent = "已保存，但该目录里没找到 storage 或 zotero.sqlite —— 请核对";
    } else if (p.zotero_exe && !p.exe_ok) {
      h.textContent = "已保存，但 zotero.exe 路径不存在 —— 请核对";
    } else if (p.data_dir && !p.data_dir_ok) {
      h.textContent = "已保存，但数据目录有问题：" + (p.data_dir_msg || "请核对");
    } else {
      h.textContent = "已保存 ✓";
    }
    if (p.data_dir_effective) _paintDataDirHint("当前生效：" + p.data_dir_effective);
    await loadMineruInto();
    refreshSetupBanner();
  } catch (e) {
    h.textContent = "保存失败：" + e.message;
  }
};

// ---- 本机路径：自动探测 + 系统选择框（网页拿不到完整本地路径，故由服务端弹窗代选）----
function _paintPathsHint(t) { $("pathsHint").textContent = t || ""; }
function _paintDataDirHint(t) { $("dataDirHint").textContent = t || ""; }

$("detectPaths").onclick = async () => {
  _paintPathsHint("探测中…");
  try {
    const det = await postJSON("/api/paths/detect", {});
    const dir = (det.data_dir || {}).path || "";
    const exe = (det.zotero_exe || {}).path || "";
    if (dir) $("zoteroDataDir").value = dir;
    if (exe) $("zoteroExe").value = exe;
    _paintPathsHint(dir || exe ? "探测到路径，点「保存」生效" : "没探测到，请手填或用「浏览…」选择");
  } catch (e) { _paintPathsHint("探测失败：" + e.message); }
};

async function pickPath(kind, inputId, hint) {
  const paint = hint || _paintPathsHint;
  paint("已弹出系统选择框，若没看到请检查任务栏…");
  try {
    const r = await postJSON("/api/paths/pick", { kind });
    if (r.path) {
      $(inputId).value = r.path;
      paint("已选择，点「保存」生效");
    } else {
      paint("未选择");
    }
  } catch (e) { paint("打开选择框失败：" + e.message); }
}
$("pickDataDir").onclick = () => pickPath("zotero_dir", "zoteroDataDir");
$("pickExe").onclick = () => pickPath("exe", "zoteroExe");
$("pickDataRoot").onclick = () => pickPath("data_dir", "dataDir", _paintDataDirHint);
$("pickBrowsers").onclick = () => pickPath("browsers", "pwBrowsersDir", _paintDataDirHint);
$("pickProfile").onclick = () => pickPath("webchat_profile", "webchatProfileDir", _paintDataDirHint);

// ---- 浏览器内核：一键下载（后台跑，前端轮询进度）----
let _pwTimer = null;

function _paintPwLog(lines) {
  const el = $("pwLog");
  if (!lines || !lines.length) { el.classList.add("hidden"); el.textContent = ""; return; }
  el.classList.remove("hidden");
  el.textContent = lines.join("\n");
  el.scrollTop = el.scrollHeight;
}

async function _pollPw() {
  try {
    const s = await (await fetch("/api/playwright/status")).json();
    if (s.log && s.log.length) _paintPwLog(s.log);
    $("pwHint").textContent = s.installed ? "已装好" : "尚未安装";
    if (!s.running) {
      if (_pwTimer) { clearInterval(_pwTimer); _pwTimer = null; }
      $("installChromium").disabled = false;
      if (s.ok === true) $("pwHint").textContent = "已装好 → 可直接用";
      if (s.ok === false) $("pwHint").textContent = "下载失败，详见下方日志";
    }
  } catch (e) { /* 拿不到状态不影响其它功能 */ }
}

$("installChromium").onclick = async () => {
  $("installChromium").disabled = true;
  $("pwHint").textContent = "正在下载…";
  _paintPwLog(["正在启动下载…"]);
  try {
    await postJSON("/api/playwright/install", {});
    if (_pwTimer) clearInterval(_pwTimer);
    _pwTimer = setInterval(_pollPw, 2000);
    _pollPw();
  } catch (e) {
    $("installChromium").disabled = false;
    $("pwHint").textContent = "启动失败：" + e.message;
  }
};

// ---- 首次运行引导：缺必填配置时给横幅，而不是等用户撞报错 ----
async function refreshSetupBanner() {
  const el = $("setupBanner");
  try {
    const s = await (await fetch("/api/setup/status")).json();
    if (!s.needs_setup) { el.classList.add("hidden"); return; }
    $("setupList").innerHTML = (s.items || []).filter((i) => !i.ok).map((i) =>
      `<li><b>${i.label}</b>：${i.hint} <span class="muted">（${i.where}）</span></li>`).join("");
    el.classList.remove("hidden");
  } catch (e) { el.classList.add("hidden"); }
}

$("setupOpenBtn").onclick = () => {
  const p = $("settingsPanel");
  if (p.classList.contains("hidden")) $("settingsBtn").click();
  p.scrollIntoView({ behavior: "smooth", block: "start" });
};
$("setupLaterBtn").onclick = () => $("setupBanner").classList.add("hidden");
refreshSetupBanner();

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
      $("genStatus").textContent = `已选「${it.title}」，点「生成笔记」开始。`;
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
      `已选择 <b>${f.name}</b>（${(f.size / 1024).toFixed(0)} KB）——点「生成笔记」：<br>` +
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
    $("psStatus").textContent = `共 ${PS_ROWS.length} 篇（已解析维度 ${ok} 篇）`;
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
  if (!confirm(`将对 ${BATCH_KEYS.length} 篇批量生成笔记（网页端、逐篇、耗时较长）。继续？`)) return;
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
    let line = `已入库 ${st.items} 篇 / ${st.chunks} 块（表格 ${(st.by_kind || {}).table || 0} 块）· 模型 ${st.model || "未配置"}`;
    if (st.pending > 0) {
      line += `　⚠ 另有 ${st.pending} 篇已用 MinerU 解析、但还没入库 —— 点「更新索引」补齐后才能被检索到`;
    }
    $("kbStats").textContent = line;
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

// ================= 文献检索（OpenAlex + arXiv，含引文追踪与批量入库） =================
let litResults = [];   // 当前结果集（检索 + 引文追踪合并后）
let litSlug = "";      // 当前载入的主题 slug，保存时用于覆盖同名主题
let litAutoLoaded = false;   // 首次打开面板自动载入最近主题，只做一次
let litRestored = false;     // 首次打开面板从本地恢复上次结果，只做一次
let litRestoredPicks = null; // 待恢复的勾选（存 data-i 字符串），在 litDraw() 之后再套用

// 结果存浏览器本地：刷新/关标签/重启后仍能接着看与勾选。
// 注意：点「检索」永远是实时重查并覆盖这份存档，存档只用于"不丢上一次的进度"。
const LIT_STORE = "litflow_search_v1";

function litSave() {
  try {
    localStorage.setItem(LIT_STORE, JSON.stringify({
      ts: new Date().toISOString(),
      results: litResults,
      picked: Array.prototype.slice.call(document.querySelectorAll(".lit-pick:checked"))
        .map((c) => c.dataset.i),
    }));
  } catch (e) { /* 隐私模式 / 配额满：静默降级，不影响检索本身 */ }
}

function litRestore() {
  try {
    const raw = localStorage.getItem(LIT_STORE);
    if (!raw) return null;
    const d = JSON.parse(raw);
    if (!d || !Array.isArray(d.results) || !d.results.length) return null;
    litResults = d.results;
    litRestoredPicks = d.picked || [];   // 勾选状态延后恢复：必须在最后一次 litDraw() 之后
    return d.ts || "";
  } catch (e) { return null; }
}

$("litBtn").onclick = async () => {
  $("litPanel").classList.remove("hidden");
  let restoredTs = null;
  if (!litRestored) { litRestored = true; restoredTs = litRestore(); }

  await loadLitTopics();
  if (!litAutoLoaded) {
    litAutoLoaded = true;
    const list = JSON.parse($("litTopicSel").dataset.list || "[]");
    if (list.length && litApplyTopic(list[0])) {
      $("litTopicSel").value = "0";
    }
  }
  litDraw();   // 画表（空结果时显示说明文字）

  // 恢复勾选只能放在 litDraw() 之后 —— 重画会清空所有勾选框
  if (restoredTs) {
    (litRestoredPicks || []).forEach((i) => {
      const c = document.querySelector(`.lit-pick[data-i="${i}"]`);
      if (c) c.checked = true;
    });
    litRestoredPicks = null;
    const when = restoredTs.replace("T", " ").slice(5, 16);
    $("litStatus").textContent = `已恢复上次检索的 ${litResults.length} 条结果（${when}，含勾选）`
      + `，可直接接着入库；点「检索」则实时重跑并覆盖`;
  } else if (litResults.length) {
    $("litStatus").textContent = `当前 ${litResults.length} 条结果`;
  } else {
    const t = litPickedTopic();
    if (t) $("litStatus").textContent = `已载入主题「${t.name}」，点「检索」开始`;
  }
};
$("litClose").onclick = () => $("litPanel").classList.add("hidden");

function litForm() {
  const sources = [];
  if ($("litSrcOA").checked) sources.push("openalex");
  if ($("litSrcAX").checked) sources.push("arxiv");
  return {
    name: $("litName").value.trim(),
    slug: litSlug,
    queries: $("litQueries").value.split("\n").map((s) => s.trim()).filter(Boolean),
    since: $("litSince").value.trim(),
    sources: sources,
    per_page: parseInt($("litPer").value, 10) || 100,
    sort: $("litSort").value,
    collection: $("litCollection").value.trim(),
  };
}

async function loadLitTopics() {
  try {
    const list = await (await fetch("/api/lit/topics")).json();
    const arr = list || [];
    $("litTopicSel").innerHTML = '<option value="">（未选择主题）</option>' +
      arr.map((t, i) => `<option value="${i}">${escapeHtml(t.name)}</option>`).join("");
    $("litTopicSel").dataset.list = JSON.stringify(arr);
  } catch (e) { /* 主题列表读不到不影响检索功能 */ }
}

function litPickedTopic() {
  const list = JSON.parse($("litTopicSel").dataset.list || "[]");
  return list[parseInt($("litTopicSel").value, 10)];
}

// 把一个主题配置填进表单（载入主题 / 打开面板自动载入 共用）
function litApplyTopic(t) {
  if (!t) return false;
  litSlug = t.slug || "";
  $("litName").value = t.name || "";
  $("litSince").value = t.since || "";
  $("litPer").value = t.per_page || 100;
  $("litCollection").value = t.collection || "";
  $("litQueries").value = (t.queries || []).join("\n");
  const src = t.sources || ["openalex"];
  $("litSrcOA").checked = src.indexOf("openalex") >= 0;
  $("litSrcAX").checked = src.indexOf("arxiv") >= 0;
  if (t.sort && $("litSort").querySelector(`option[value="${t.sort}"]`)) $("litSort").value = t.sort;
  return true;
}

$("litTopicLoad").onclick = () => {
  const t = litPickedTopic();
  if (!t) { $("litStatus").textContent = "请先在下拉框里选一个主题"; return; }
  litApplyTopic(t);
  $("litStatus").textContent = `已载入主题「${t.name}」（${(t.queries || []).length} 条检索式），点「检索」开始`;
};

$("litTopicSave").onclick = async () => {
  const form = litForm();
  if (!form.name) { $("litStatus").textContent = "请先填写主题名"; return; }
  if (!form.queries.length) { $("litStatus").textContent = "请先填写至少一条检索式"; return; }
  try {
    const t = await postJSON("/api/lit/topics", form);
    litSlug = t.slug || "";
    $("litStatus").textContent = `主题「${t.name}」已保存到本机`;
    loadLitTopics();
  } catch (e) { $("litStatus").textContent = "保存失败：" + e.message; }
};

$("litTopicDel").onclick = async () => {
  const t = litPickedTopic();
  if (!t) { $("litStatus").textContent = "请先在下拉框里选一个主题"; return; }
  if (!confirm(`删除检索主题「${t.name}」？\n只删检索配置，不影响 Zotero 里已入库的文献。`)) return;
  try {
    await postJSON("/api/lit/topics/delete", { slug: t.slug });
    if (litSlug === t.slug) litSlug = "";
    $("litStatus").textContent = "主题已删除";
    loadLitTopics();
  } catch (e) { $("litStatus").textContent = "删除失败：" + e.message; }
};

// 数据源异常告警：有源挂了就必须说出来，不能让用户以为拿到的是全量结果
function litShowWarn(msgs) {
  const w = $("litWarn");
  if (!w) return;
  w.classList.remove("lit-info");
  if (!msgs || !msgs.length) { w.classList.add("hidden"); w.innerHTML = ""; return; }
  w.innerHTML = "<b>数据源异常，本次结果不完整：</b>" + msgs.map(escapeHtml).join("；")
    + "<br>额度恢复后重新点「检索」即可；也可以临时只勾 arXiv 先跑（OpenAlex 侧暂时拿不到）。";
  w.classList.remove("hidden");
}

// 中性提示（非故障）：例如"未配 Key，可以去免费注册"
function litShowInfo(html) {
  const w = $("litWarn");
  if (!w) return;
  if (!html) { w.classList.add("hidden"); w.innerHTML = ""; return; }
  w.innerHTML = html;
  w.classList.add("lit-info");
  w.classList.remove("hidden");
}

// 通用错误展示（写入失败之类），红色报警样式
function litShowError(html) {
  const w = $("litWarn");
  if (!w) return;
  w.classList.remove("lit-info");
  w.innerHTML = html;
  w.classList.remove("hidden");
}

$("litRun").onclick = async () => {
  const form = litForm();
  if (!form.queries.length) { $("litStatus").textContent = "请先填写至少一条检索式"; return; }
  if (!form.sources.length) { $("litStatus").textContent = "请至少选择一个数据源"; return; }
  $("litRun").disabled = true;
  litShowWarn(null);
  $("litStatus").textContent = "检索中…（多检索式 × 多数据源，约需 30–60 秒；arXiv 官方要求 3 秒间隔，属正常）";
  $("litLog").textContent = "检索中…";
  try {
    const r = await postJSON("/api/lit/search", form);
    litResults = r.results || [];
    litDraw();
    litSave();
    litShowWarn(r.degraded);
    $("litLog").textContent = (r.log || []).concat(r.errors || []).join("\n") || "（无日志）";
    $("litStatus").textContent = (r.degraded || []).length
      ? `检索完成，命中 ${litResults.length} 条 —— 有数据源未响应，结果不完整`
      : `检索完成，命中 ${litResults.length} 条`;
  } catch (e) {
    $("litStatus").textContent = "检索失败：" + e.message;
  } finally {
    $("litRun").disabled = false;
  }
};

// 可见行：勾了「隐藏已在库」就过滤掉已入库的。
// 注意 data-i 仍写原始下标，这样勾选/勾全选与实际数组始终对得上。
function litVisibleIdx() {
  const hide = $("litHideLib").checked;
  const out = [];
  litResults.forEach((x, i) => { if (!(hide && x.in_library)) out.push(i); });
  return out;
}

// 重画表格。keepSel=true 时保留当前勾选（切换筛选时用）；
// 新检索/引文追踪后不要保留——结果集换了、下标会错位。
function litDraw(keepSel) {
  const keep = keepSel
    ? Array.prototype.slice.call(document.querySelectorAll(".lit-pick:checked")).map((c) => c.dataset.i)
    : null;

  const oa = litResults.filter((x) => x.oa_url).length;
  const lib = litResults.filter((x) => x.in_library).length;
  const byOa = litResults.filter((x) => x.source === "openalex").length;
  const idx = litVisibleIdx();
  const split = litResults.length ? `（OpenAlex ${byOa} · arXiv ${litResults.length - byOa}）` : "";
  $("litCount").textContent = litResults.length
    ? `共 ${litResults.length} 条${split} · 可下载全文 ${oa} 条 · 已在库 ${lib} 条`
      + (idx.length < litResults.length ? `（已隐藏 ${litResults.length - idx.length} 条已在库）` : "")
    : "尚未检索";

  if (!litResults.length) {
    $("litTable").innerHTML = `<tbody><tr><td colspan="5" class="lit-empty">
      结果还没出来 —— 上方填好检索式后点「检索」，命中的文献会列在这里。<br>
      每行<b>最左侧的方框</b>就是勾选框：勾好再点「入库选中」，就会写进 Zotero 的对应分类。
    </td></tr></tbody>`;
  } else if (!idx.length) {
    $("litTable").innerHTML = `<tbody><tr><td colspan="5" class="lit-empty">
      这一批 ${litResults.length} 条<b>全部已在库中</b>，被「隐藏已在库」过滤掉了。<br>
      想核对就把左上角那个勾去掉；想找新文献，建议改检索式、调时间窗，或用引文追踪往外扩。
    </td></tr></tbody>`;
  } else {
    $("litTable").innerHTML =
      `<thead><tr>
        <th class="lit-pickcell"></th>
        <th>文献</th>
        <th style="width:122px">来源 / 状态</th>
        <th style="width:56px">被引</th>
        <th style="width:172px">操作</th>
      </tr></thead><tbody>${idx.map((i) => litRowHtml(litResults[i], i)).join("")}</tbody>`;
  }

  if (keep) {
    keep.forEach((i) => {
      const c = document.querySelector(`.lit-pick[data-i="${i}"]`);
      if (c) c.checked = true;
    });
  } else {
    $("litSelAll").checked = false;
  }
}

function litRowHtml(x, i) {
  const au = (x.authors || []).slice(0, 3).join(", ") + ((x.authors || []).length > 3 ? " 等" : "");
  const chips = [`<span class="lit-chip src">${x.source === "arxiv" ? "arXiv" : "OpenAlex"}</span>`];
  if ((x.also || []).length) chips.push(`<span class="lit-chip src">+${(x.also || []).join("/")}</span>`);
  chips.push(x.oa_url
    ? '<span class="lit-chip oa">可下载全文</span>'
    : '<span class="lit-chip nooa">无全文</span>');
  if (x.in_library) chips.push('<span class="lit-chip lib">已在库</span>');
  const doiLink = x.doi
    ? ` · <a href="https://doi.org/${encodeURIComponent(x.doi)}" target="_blank" rel="noopener">DOI</a>`
    : "";
  const abs = x.abstract
    ? `<div class="lit-abs hidden" id="litAbs${i}">${escapeHtml(x.abstract)}</div>` : "";
  return `<tr>
    <td class="lit-pickcell"><input type="checkbox" class="lit-pick" data-i="${i}"></td>
    <td>
      <div class="lit-title"><a href="${escapeHtml(x.url || "#")}" target="_blank" rel="noopener">${escapeHtml(x.title)}</a></div>
      <div class="lit-meta">${escapeHtml(au)} · ${x.year || "—"}${x.venue ? " · " + escapeHtml(x.venue) : ""}${doiLink}</div>
      ${abs}
    </td>
    <td>${chips.join("")}</td>
    <td>${x.cited_by || 0}</td>
    <td class="lit-acts">
      ${x.abstract ? `<button class="ghost lit-mini lit-absbtn" data-i="${i}">摘要</button>` : ""}
      <button class="ghost lit-mini lit-cite" data-i="${i}">追引用</button>
      <button class="ghost lit-mini lit-ref" data-i="${i}">参考文献</button>
    </td>
  </tr>`;
}

$("litTable").addEventListener("click", (ev) => {
  const b = ev.target.closest("button");
  if (!b) return;
  const i = parseInt(b.dataset.i, 10);
  if (b.classList.contains("lit-absbtn")) {
    const el = $("litAbs" + i);
    if (!el) return;
    el.classList.toggle("hidden");
    b.textContent = el.classList.contains("hidden") ? "摘要" : "收起";
  } else if (b.classList.contains("lit-cite")) {
    litExpand(i, "citing");
  } else if (b.classList.contains("lit-ref")) {
    litExpand(i, "referenced");
  }
});

$("litSelAll").onchange = () => {
  document.querySelectorAll(".lit-pick").forEach((c) => { c.checked = $("litSelAll").checked; });
  litSave();
};

$("litHideLib").onchange = () => { litDraw(true); litSave(); };

$("litClearCache").onclick = () => {
  if (litResults.length
      && !confirm(`清空当前这 ${litResults.length} 条结果？\n只清浏览器里的这份快照，不影响 Zotero 里已入库的文献。`)) return;
  litResults = [];
  try { localStorage.removeItem(LIT_STORE); } catch (e) {}
  litShowWarn(null);
  litDraw();
  $("litStatus").textContent = "已清空结果（点「检索」可重新跑）";
};

$("litQuota").onclick = async () => {
  $("litStatus").textContent = "正在查询 OpenAlex 额度…";
  try {
    const q = await (await fetch("/api/lit/quota")).json();
    if (!q.has_key) {
      $("litStatus").textContent = "未配置 OpenAlex API Key（当前用匿名额度）";
      litShowInfo("<b>还没配 OpenAlex API Key。</b>当前匿名额度：每天 $0.1（约 100 次请求 ≈ 20 轮检索），"
        + "每天北京时间早 8 点重置。<br>免费注册约 30 秒，额度提升到 $1/天（10 倍）且能查看用量："
        + "<a href=\"https://openalex.org/settings/api\" target=\"_blank\" rel=\"noopener\">openalex.org/settings/api</a>"
        + " —— 登录后复制 key，粘到「设置」里的 <b>OpenAlex API Key</b> 即可。");
      return;
    }
    if (q.error) { $("litStatus").textContent = "额度查询失败：" + q.error; return; }
    const hrs = q.resets_in_seconds ? (q.resets_in_seconds / 3600).toFixed(1) : "?";
    const cost = Math.max(1, Math.round((q.credits_remaining || 0) / 10));
    $("litStatus").textContent =
      `OpenAlex 额度：剩余 ${q.credits_remaining}/${q.credits_limit} credits`
      + `（约 ${cost} 次检索请求）· 已用 ${q.credits_used} · ${hrs} 小时后重置（UTC 午夜 = 北京早 8 点）`;
  } catch (e) {
    $("litStatus").textContent = "额度查询失败：" + e.message;
  }
};

$("litTable").addEventListener("change", (ev) => {
  if (ev.target.classList && ev.target.classList.contains("lit-pick")) litSave();
});

// 引文追踪：citing = 找引用它的后续工作；referenced = 找它的奠基文献
async function litExpand(i, direction) {
  const rec = litResults[i];
  if (!rec) return;
  $("litStatus").textContent = direction === "citing" ? "正在追踪引用它的后续工作…" : "正在追踪它的参考文献…";
  try {
    const r = await postJSON("/api/lit/expand", {
      record: rec, direction: direction, limit: 40, since: $("litSince").value.trim(),
    });
    const known = {};
    litResults.forEach((x) => { known[x.uid] = true; });
    const tag = `${direction === "citing" ? "引用了" : "参考文献于"}「${(rec.title || "").slice(0, 34)}」`;
    const added = (r.results || []).filter((x) => !known[x.uid]);
    added.forEach((x) => { x.via = tag; });
    litResults = added.concat(litResults);
    litDraw();
    litSave();
    $("litStatus").textContent = added.length
      ? `「${(rec.title || "").slice(0, 28)}」引文追踪新增 ${added.length} 条，当前共 ${litResults.length} 条`
      : "没有新增（追踪到的文献都已在结果里）";
  } catch (e) {
    $("litStatus").textContent = "引文追踪失败：" + e.message;
  }
}

$("litImport").onclick = async () => {
  const picks = Array.prototype.slice.call(document.querySelectorAll(".lit-pick:checked"))
    .map((c) => litResults[parseInt(c.dataset.i, 10)]).filter(Boolean);
  if (!picks.length) { $("litStatus").textContent = "请先在结果表里勾选要入库的文献"; return; }
  const col = $("litCollection").value.trim();
  const doPdf = $("litFetchPdf").checked;
  const dup = picks.filter((x) => x.in_library).length;
  let msg = `将把 ${picks.length} 条写入 Zotero`;
  msg += col ? `，归入分类「${col}」（不存在则新建）` : "（不指定分类）";
  msg += doPdf ? "，并抓取可下载的开放获取 PDF 挂为附件" : "";
  if (dup) msg += `。\n其中 ${dup} 条已在库中，会自动跳过。`;
  msg += "\n\n确认执行？";
  if (!confirm(msg)) return;
  $("litImport").disabled = true;
  $("litStatus").textContent = "入库中…（逐条建条目并抓取全文，可能需要一两分钟）";
  try {
    const r = await postJSON("/api/lit/import", { records: picks, collection: col, fetch_pdf: doPdf });
    $("litLog").textContent = (r.log || []).join("\n") || "（无日志）";
    $("litStatus").textContent = `入库完成：新建 ${r.created} 条 · 跳过 ${r.skipped} 条 · 抓到全文 ${r.pdf_ok} 篇`
      + (r.collection ? ` · 分类「${r.collection.name}」` : "");
    picks.forEach((x) => { x.in_library = true; });
    litDraw(true);   // 入库后保留其余勾选；刚入库的会被「隐藏已在库」收走
    litSave();
  } catch (e) {
    $("litStatus").textContent = "入库失败";
    litShowError("<b>入库失败：</b>" + escapeHtml(e.message).replace(/\n/g, "<br>"));
  } finally {
    $("litImport").disabled = false;
  }
};

$("litExport").onclick = () => {
  if (!litResults.length) { $("litStatus").textContent = "还没有检索结果可导出"; return; }
  const cell = (v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`;
  const head = ["年份", "标题", "作者", "载体", "来源", "被引", "DOI", "OA链接", "已在库", "命中检索式"];
  const rows = litResults.map((x) => [
    x.year || "", x.title || "", (x.authors || []).join("; "), x.venue || "",
    x.source === "arxiv" ? "arXiv" : "OpenAlex", x.cited_by || 0, x.doi || "",
    x.oa_url || "", x.in_library ? "是" : "否", x.via || "",
  ].map(cell).join(","));
  const csv = "\ufeff" + [head.map(cell).join(",")].concat(rows).join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  a.download = `文献检索_${$("litName").value.trim() || "结果"}_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
};
