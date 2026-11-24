import {esc, dateLabel, nowDate, chip, tile, button, linkButton, empty, note, pageHead, panelHead, options, input, textarea, select} from "./ui.js";
import {icon} from "./icons.js";
import {attachmentMeta, comparisonHits, copyrightChip, excerptCards, rightsDisplay, rightsFields, sourceLink, trialFields, trialView} from "./tools.js";
import {agentView, rulingOptions} from "./agent.js";

const main = document.querySelector("#main");
const modal = document.querySelector("#dialog");
const sidebar = document.querySelector("#sidebar");
let data = null;
let current = {page: "home", id: "", params: new URLSearchParams()};
let issueTab = "pending";
let detailTab = "agent";
let agentTimer;
let agentResult;
let listQuery = "";
let evidenceResult = null;
let bookQuery = "";
const expandedIssueSections = new Set();
const can = ability => data?.abilities?.includes(ability);
const reviewerName = () => data?.viewer?.name || "林编辑";
const toneByLevel = {high: "red", normal: "amber", low: "blue"};
const levelNames = {high: "高优先级", normal: "普通", low: "低优先级"};
const statusNames = {active: ["进行中", "blue"], revision: ["待作者补件", "amber"], paused: ["暂停加工", "red"], archived: ["已签核", "green"], rejected: ["已退稿", ""]};
const issueStates = {pending: ["待判断", "purple"], confirmed: ["待处理", "amber"], waiting: ["待补证", "amber"], dismissed: ["已标记例外", ""], resolved: ["已处理", "green"]};
const libraryStates = {approved: ["已审定", "green"], pending: ["待审定", "amber"], inactive: ["停用", ""]};
const referenceStates = {verified: ["身份已核实", "green"], conflict: ["实质冲突", "red"], unverified: ["仍无法核实", "amber"], unavailable: ["接口不可用", ""], noCoverage: ["来源缺覆盖", ""]};
const kinds = {evidence: "专业条款", book: "同类书目", term: "术语表", watch:"预警期刊"};
const labels = {home: "工作台", manuscripts: "稿件管理", issues: "问题清单", workflow: "流程看板",
  library: "知识资料库", archive: "签核归档", trial:"试点验收", trash:"回收站"};
const stageIcons = ["inbox", "book", "shield", "target", "refresh"];
const categories = ["收稿材料", "内容重复", "体例用语", "编辑校对", "编号检查", "数据矛盾", "选题对照",
  "专业审校", "图文核验", "引文核验", "学术评估", "版权核验", "图片授权", "地图审核", "校样复核"];
const primaryDetails = [
  ["agent", "智能审校"], ["docket", "案卷裁决"], ["text", "原文与版本"], ["events", "流转记录"]
];
const manualDetails = [
  ["overview", "节点概览"], ["issues", "问题清单"],
  ["comparison", "同类书对照"], ["verification", "条款与引文"], ["assessment", "学术评估"],
  ["proofs", "校样与图件"], ["copyright", "版权核验"]
];
const searchMatch = (query, ...parts) => parts.join(" ").toLowerCase().includes(query.trim().toLowerCase());
const manuscript = id => data.manuscripts.find(item => item.id === id);
const versionPath = (item, version, suffix) => `/api/manuscripts/${encodeURIComponent(item)}/versions/${encodeURIComponent(version)}/${suffix}`;
const itemHref = (id, tab = "") => `#manuscripts/${encodeURIComponent(id)}${tab ? "?tab=" + tab : ""}`;
const finished = item => ["archived", "rejected"].includes(item.status);
const openIssue = issue => ["pending", "confirmed", "waiting"].includes(issue.state);
const linkedIssues = id => data.issues.filter(issue => issue.manuscript === id);
const pending = () => data.issues.filter(issue => issue.state === "pending" && !finished(manuscript(issue.manuscript)));
const openQueue = () => data.issues.filter(issue => !finished(manuscript(issue.manuscript)));
const repeatKey = issue => Number.isInteger(issue.repeatGroup)
  ? JSON.stringify([issue.manuscript,issue.version,issue.state,issue.repeatGroup]) : null;
const statusChip = value => chip(...statusNames[value], true);
const issueChip = value => chip(...issueStates[value], true);
async function api(path, payload) {
  const response = await fetch(path, payload === undefined ? undefined : {
    method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(payload)
  });
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    if (response.status === 401) showLogin();
    throw Object.assign(new Error(result.error || "本机服务暂时不可用。"),{code:result.code});
  }
  return response.json();
}

function toast(message, error = false) {
  const item = document.createElement("div");
  item.className = "toast" + (error ? " error" : "");
  item.innerHTML = icon(error ? "alert" : "checkCircle", 17) + "<span>" + esc(message) + "</span>";
  document.querySelector("#toasts").append(item);
  setTimeout(() => item.remove(), 3700);
}

async function refresh() {
  data = await api("/api/state");
  render();
}

function parseLocation() {
  const [route, query = ""] = location.hash.slice(1).split("?");
  const [page, id = ""] = route.split("/");
  return {page: labels[page] ? page : "home", id, params: new URLSearchParams(query)};
}

function navigate() {
  const next = parseLocation();
  if (current.page !== next.page) {
    issueTab = "pending";
    listQuery = "";
  }
  if (next.page === "issues") {
    const tab = next.params.get("tab");
    issueTab = ["pending", "waiting", "confirmed", "resolved", "open"].includes(tab)
      ? tab : next.params.get("severity") === "high" ? "open" : "pending";
  }
  if (current.id !== next.id) {
    evidenceResult = null;
    bookQuery = "";
  }
  current = next;
  detailTab = [...primaryDetails, ...manualDetails].some(([key]) => key === current.params.get("tab"))
    ? current.params.get("tab") : "agent";
  sidebar.classList.remove("visible");
  if (data) render();
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", navigate);

function renderShell() {
  const navLink = (key, glyph, count = "") => `<a class="nav-link" href="#${key}" ${current.page === key ? 'aria-current="page"' : ""}>${icon(glyph, 18)}<span>${labels[key]}</span>${count ? `<span class="nav-count">${count}</span>` : ""}</a>`;
  sidebar.innerHTML = `<a href="#home" class="brand"><span class="brand-mark">${icon("logo", 21)}</span><strong>文衡编辑台</strong></a>
    <nav aria-label="主导航"><div class="nav-group"><div class="nav-label">编辑工作</div>
    ${navLink("home", "dashboard")}${navLink("manuscripts", "files")}${navLink("issues", "tasks", issueCount(pending()))}${navLink("workflow", "flow")}
    </div><div class="nav-group"><div class="nav-label">资料与归档</div>
    ${navLink("library", "library")}${navLink("archive", "archive")}${navLink("trial", "list")}</div></nav>
    <div class="sidebar-footer">${icon("lock", 15)} 本机工作区</div>`;
  document.querySelector("#topbar").innerHTML = `<div class="crumb">
    <button type="button" class="icon-button mobile-toggle" data-action="menu" aria-label="打开菜单" aria-expanded="${sidebar.classList.contains("visible")}">${icon("menu", 19)}</button>
    <span>编辑工作区</span>${icon("chevron", 14)}<strong>${labels[current.page]}</strong></div>
    <div class="top-actions"><button class="search-trigger" type="button" data-action="search" aria-label="全局搜索">${icon("search", 17)} <span>搜索稿件或问题</span><kbd>Ctrl K</kbd></button>
    <button type="button" class="icon-button" data-action="notifications" aria-label="工作提醒">${icon("bell", 20)}${pending().some(item => item.severity === "high") ? '<i class="bell-dot"></i>' : ""}</button>
    ${data.viewer?.local ? `<div class="account"><div class="avatar" aria-hidden="true">${esc(reviewerName()[0])}</div><div><strong>${esc(reviewerName())}</strong><small>本机编辑</small></div></div>`
      : `<button class="button compact" type="button" data-action="account">${esc(reviewerName())} ${icon("down",13)}</button>`}</div>`;
  document.querySelector("#footer").innerHTML = "<span>文衡编辑台</span><span>稿件和审校记录保存在本机</span>";
}

function issueCount(items) {
  return new Set(items.map(item => repeatKey(item) ?? item.id)).size;
}

function issueTree(items, {showManuscripts = true, limit = Infinity} = {}) {
  if (!items.length) return empty("没有符合条件的问题", "处理进度会在这里更新。", "checkCircle");
  const byManuscript = new Map();
  for (const issue of items) {
    if (!byManuscript.has(issue.manuscript)) byManuscript.set(issue.manuscript,new Map());
    const types = byManuscript.get(issue.manuscript);
    const key = JSON.stringify([issue.category,issue.title]);
    if (!types.has(key)) types.set(key,{title:issue.title,category:issue.category,locations:new Map()});
    const group = types.get(key);
    const locations = issue.locations?.length ? issue.locations : [{anchor:null,label:"待定位原文"}];
    for (const location of locations) {
      const repeat = repeatKey(issue);
      const identity = JSON.stringify([repeat || issue.id,location.anchor]);
      const previous = group.locations.get(identity);
      if (!previous || (location.anchor === issue.anchor && location.anchor !== previous.issue.anchor)) {
        group.locations.set(identity,{location,issue});
      }
    }
  }
  const details = (key, className, heading, content) => {
    const section = JSON.stringify([current.page,current.id,detailTab,key]);
    const expanded = expandedIssueSections.has(section) || (current.page === "issues" && listQuery.trim());
    return `<details class="${className}" data-issue-section="${esc(section)}"${expanded ? " open" : ""}>
      <summary class="issue-tree-summary">${heading}</summary>${content}</details>`;
  };
  return `<div class="issue-tree">${[...byManuscript].slice(0,limit).map(([ident,types]) => {
    const project = manuscript(ident);
    const versions = new Map(project.versions.map((version,index) => [version.id,{...version,index}]));
    const groups = [...types].map(([key,type]) => {
      const rows = [...type.locations.values()].sort((first,second) =>
        (versions.get(second.issue.version)?.index ?? -1) - (versions.get(first.issue.version)?.index ?? -1)
        || (first.location.anchor ?? Infinity) - (second.location.anchor ?? Infinity));
      const heading = `<span class="issue-tree-title">${esc(type.title)}</span>
        ${chip(type.category)}<span class="issue-tree-count">${rows.length} ${rows.every(row => row.location.anchor === null) ? "项" : "处"}</span>`;
      return details([ident,key],"issue-tree-type",heading,`<ul class="issue-tree-locations">${rows.map(({location,issue}) => {
        const version = versions.get(issue.version);
        const anchorAttribute = Number.isInteger(location.anchor) ? ` data-anchor="${location.anchor}"` : "";
        const source = issue.evidence.find(entry => entry.label === "DOI")?.text
          || (Number.isInteger(issue.repeatGroup) ? issue.evidence[0]?.text : issue.detail) || "";
        const preview = source.replace(/\s+/g," ").trim();
        return `<li class="issue-tree-location"><div class="issue-tree-location-copy">
          <button type="button" class="text-link" data-action="issue" data-id="${issue.id}"${anchorAttribute}>
          ${version ? esc(version.label) + " · " : ""}${esc(location.label)}</button>
          ${preview ? `<span class="row-sub">${esc(preview.slice(0,130))}${preview.length > 130 ? "…" : ""}</span>` : ""}</div>
          <div class="issue-tree-location-status">${chip(levelNames[issue.severity],toneByLevel[issue.severity],true)}${issueChip(issue.state)}</div>
        </li>`;
      }).join("")}</ul>`);
    }).join("");
    if (!showManuscripts) return groups;
    return details(ident,"issue-tree-manuscript",`<span class="issue-tree-title">${esc(project.title)}</span>
      <span class="issue-tree-id">${esc(project.id)}</span><span class="issue-tree-count">${types.size} 类问题</span>`,
      `<div class="issue-tree-types">${groups}</div>`);
  }).join("")}</div>`;
}

document.addEventListener("toggle", event => {
  const key = event.target.dataset?.issueSection;
  if (key) {
    if (event.target.open) expandedIssueSections.add(key);
    else expandedIssueSections.delete(key);
  }
}, true);
function queueTabs(scope) {
  const tabs = [
    ["pending", "需要我判断"], ["waiting", "待补件"], ["confirmed", "待处理"], ["resolved", "已处理"]
  ];
  if (current.page === "issues" && (current.params.get("severity") || issueTab === "open")) {
    tabs.unshift(["open", "全部未结"]);
  }
  return `<div class="tabbar" aria-label="按处理状态筛选">${tabs.map(([key, title]) => {
    const count = issueCount(queueFilter(scope, key));
    return `<button type="button" class="tab ${issueTab === key ? "active" : ""}" aria-pressed="${issueTab === key}" data-action="issue-tab" data-id="${key}">${title}${count ? `<span class="tab-count">${count}</span>` : ""}</button>`;
  }).join("")}</div>`;
}
function queueFilter(scope, key) {
  if (key === "open") return scope.filter(openIssue);
  return scope.filter(item => key === "resolved" ? ["resolved", "dismissed"].includes(item.state) : item.state === key);
}
function dashboard() {
  const items = openQueue();
  const queue = queueFilter(items, issueTab);
  const stats = [
    ["待判断", issueCount(pending()), "#issues", ""],
    ["在编稿件", data.manuscripts.filter(item => !finished(item)).length, "#manuscripts?status=open", ""],
    ["高优先级", issueCount(items.filter(item => item.severity === "high" && openIssue(item))), "#issues?severity=high", "urgent"],
    ["已归档", data.manuscripts.filter(item => item.status === "archived").length, "#archive", ""]
  ];
  const date = new Date().toLocaleDateString("zh-CN", {year:"numeric", month:"long", day:"numeric", weekday:"long", timeZone:"Asia/Shanghai"});
  const stageNotes = ["来稿材料与基础检查", "同类书与选题差异", "条款、图文及引文", "贡献证据与送审判断", "返修比对与校样签核"];
  return `<section class="work-summary" aria-labelledby="summary-title">
    <div class="summary-date"><h1 id="summary-title">编辑工作台</h1><time>${esc(date)}</time></div>
    <div class="summary-metrics" aria-label="稿件与问题统计">${stats.map(([label, value, href, tone]) =>
      `<a class="summary-item ${tone}" href="${esc(href)}">${label} <strong>${value}</strong></a>`).join("")}</div>
    ${can("create") ? button("登记新稿", "create", "plus", "primary") : ""}
    </section>
    <div class="dashboard-grid"><section class="panel" aria-label="审校待办">
      ${panelHead("审校待办", linkButton("筛选问题", "#issues", "filter", "compact"))}
      ${queueTabs(items)}${issueTree(queue,{limit:5})}
      <div class="panel-footer"><span>共 ${new Set(queue.map(issue => issue.manuscript)).size} 件稿件</span><a class="text-link" href="#issues?tab=${issueTab}">查看全部问题 ${icon("chevron", 14)}</a></div>
    </section><aside class="right-stack"><section class="panel">${panelHead("稿件流转", '<a class="text-link" href="#workflow">查看看板</a>')}
      <ol class="pipeline">${data.stages.map(([title], index) => {
        const count = data.manuscripts.filter(item => item.stage === index && !finished(item)).length;
        return `<li><a class="pipeline-link" href="#manuscripts?stage=${index}&status=open">${icon(stageIcons[index], 17)}
          <span><strong class="label">${title}</strong><small>${stageNotes[index]}</small></span><span class="pipeline-count" aria-label="${count} 件稿件">${count}</span></a></li>`;
      }).join("")}</ol></section>
      <a class="quick-strip panel" href="#library">${tile("library")}<span class="fill"><strong>知识资料库</strong><small>条款、同类书目及社内术语</small></span>${icon("chevron", 16)}</a>
    </aside></div>`;
}
function issuePage() {
  const severity = current.params.get("severity") || "";
  const scope = openQueue().filter(item => !severity || severity === item.severity);
  return pageHead("问题清单", "查看证据、确认问题并记录处置依据。") +
    `<section class="panel"><div class="toolbar">${searchField("搜索问题或稿件")}${filterSelect("severity", "优先级", [["", "全部优先级"], ...Object.entries(levelNames)], severity)}
    ${can("decision") ? button("批量处置","batch","tasks","compact") : ""}</div>
    ${queueTabs(scope)}<div data-results></div></section>`;
}
function searchField(placeholder) {
  return `<label class="input-wrap"><span class="visually-hidden">${esc(placeholder)}</span>${icon("search", 16)}<input type="search" value="${esc(listQuery)}" data-list-search placeholder="${esc(placeholder)}"></label>`;
}
function filterSelect(key, label, values, selected) {
  return `<label><span class="visually-hidden">${esc(label)}</span><select data-filter="${key}">${options(values, selected)}</select></label>`;
}
function manuscriptPage(archive) {
  const stage = current.params.get("stage") || "";
  const status = current.params.get("status") || "";
  return pageHead(archive ? "签核归档" : "稿件管理", archive ? "查阅已签核稿件、原始文件及流转记录。" : "登记来稿，管理审校节点、稿件版本和附件。",
    (can("flow") ? linkButton("回收站","#trash","archive") : "")
      + (!archive && can("create") ? button("登记新稿", "create", "plus", "primary") : ""))
    + `<section class="panel"><div class="toolbar">${searchField("搜索稿名、编号或作者")}
      ${archive ? "" : filterSelect("stage", "工作流节点", [["", "全部节点"], ...data.stages.map(([name], index) => [index, name])], stage)}
      ${archive ? "" : filterSelect("status", "稿件状态", [["", "全部状态"], ["open", "在编稿件"], ...Object.entries(statusNames).map(([key, [name]]) => [key, name])], status)}
    </div><div data-results></div></section>`;
}
function trashPage() {
  const rows = (data.trash || []).slice().sort((first,second) => second.trashedAt.localeCompare(first.trashedAt));
  return pageHead("回收站","恢复稿件后，可以继续查看原稿、校样和审校记录。",
    linkButton("返回稿件管理","#manuscripts"))
    + `<section class="panel">${rows.length ? `<div class="table-scroll"><table class="data-table"><thead><tr>
      <th scope="col">稿件名称</th><th scope="col">作者</th><th scope="col">移入前状态</th><th scope="col">移入时间</th><th scope="col">操作</th>
      </tr></thead><tbody>${rows.map(item => `<tr><td><strong>${esc(item.title)}</strong><span class="row-sub">${esc(item.id)}</span></td>
      <td>${esc(item.author)}</td><td>${statusChip(item.status)}</td><td>${dateLabel(item.trashedAt,true)}</td>
      <td><div class="actions">${button("恢复稿件","restore-manuscript","refresh","compact",`data-id="${item.id}"`)}
      ${button("永久删除","delete-manuscript","","compact danger",`data-id="${item.id}"`)}</div></td></tr>`).join("")}</tbody></table></div>`
      : empty("回收站为空","","archive")}</section>`;
}
function manuscriptTable(items) {
  if (!items.length) return empty("没有符合条件的稿件", "可以更改搜索词或筛选条件。");
  return `<div class="table-scroll"><table class="data-table"><thead><tr><th scope="col">稿件名称</th><th scope="col">作者</th><th scope="col">当前节点</th><th scope="col">状态</th><th scope="col">未结事项</th><th scope="col">负责人</th></tr></thead><tbody>${items.map(item =>
    `<tr><td><a class="row-title" href="${itemHref(item.id)}">${esc(item.title)}</a><span class="row-sub">${item.id} · ${esc(item.category)}</span></td><td>${esc(item.author)}</td><td>${data.stages[item.stage][0]}</td><td>${statusChip(item.status)}</td>
    <td>${issueCount(linkedIssues(item.id).filter(openIssue))}</td><td>${esc(item.editor)}</td></tr>`).join("")}</tbody></table></div><div class="panel-footer">共 ${items.length} 件稿件</div>`;
}
function workflowPage() {
  return pageHead("流程看板", "查看在编稿件所处节点和流转状态。", linkButton("稿件管理", "#manuscripts", "files")) +
    `<div class="stage-board">${data.stages.map(([title, description], index) => {
      const items = data.manuscripts.filter(item => !finished(item) && item.stage === index);
      return `<section class="stage-column"><h2>${title} <span class="pipeline-count">${items.length}</span></h2>
        <small style="display:block;margin-bottom:9px">${description}</small>
        ${items.map(item => `<a class="stage-card" href="${itemHref(item.id)}"><strong>${esc(item.title)}</strong><small>${item.id}</small><br>${statusChip(item.status)}</a>`).join("") || '<small>暂无稿件</small>'}</section>`;
    }).join("")}</div>`;
}
function libraryPage() {
  const type = current.params.get("type") || "";
  return pageHead("知识资料库", "登记专业条款、社内术语和经许可使用的同类书目。",
    (can("library") ? button("批量导入","library-import","upload") + button("登记资料", "library-create", "plus", "primary") : "")
      + (can("maintenance") ? button("资料巡检","library-maintenance","clock") : "")) +
    `<section class="panel"><div class="toolbar">${searchField("搜索资料或标识")}
    ${filterSelect("type", "资料类型", [["", "全部类型"], ...Object.entries(kinds)], type)}</div><div data-results></div></section>`;
}
function libraryTable(items) {
  if (!items.length) return empty("没有符合条件的资料", "", "library");
  return `<div class="table-scroll"><table class="data-table"><thead><tr><th scope="col">资料名称</th><th scope="col">类型</th><th scope="col">版本</th><th scope="col">状态</th><th scope="col">核验日期</th><th scope="col">操作</th></tr></thead><tbody>${items.map(item =>
    `<tr><td><button type="button" class="text-link row-title" data-action="library-show" data-id="${item.id}">${esc(item.title)}</button><span class="row-sub">${esc(item.identifier || item.source)}</span></td><td>${kinds[item.type]}</td><td>${esc(item.edition || "—")}</td>
    <td>${chip(...libraryStates[item.status], true)}</td><td>${esc(item.checked || "—")}</td><td>${can("approve") || can("library")
      ? `<button class="text-link" type="button" data-action="library-review" data-id="${item.id}">审定</button>` : "—"}</td></tr>`).join("")}</tbody></table></div><div class="panel-footer">共 ${items.length} 条资料</div>`;
}
function updateResults() {
  const results = main.querySelector("[data-results]");
  if (!results) return;
  const param = key => current.params.get(key) || "";
  if (current.page === "issues") {
    const severity = param("severity");
    const items = openQueue().filter(item => {
      const relevant = queueFilter([item], issueTab).length;
      return (!severity || severity === item.severity) && relevant && searchMatch(listQuery,item.title,item.category,
        manuscript(item.manuscript).title,item.detail,...item.evidence.filter(entry => entry.label === "DOI").map(entry => entry.text));
    });
    results.innerHTML = issueTree(items) + `<div class="panel-footer">共 ${new Set(items.map(issue => issue.manuscript)).size} 件稿件</div>`;
  }
  if (current.page === "manuscripts" || current.page === "archive") {
    const stage = param("stage"), status = param("status");
    results.innerHTML = manuscriptTable(data.manuscripts.filter(item =>
      (current.page !== "archive" || item.status === "archived")
      && (stage === "" || String(item.stage) === stage)
      && (!status || (status === "open" ? !finished(item) : item.status === status))
      && searchMatch(listQuery, item.title, item.id, item.author, item.editor)));
  }
  if (current.page === "library") {
    const type = param("type");
    results.innerHTML = libraryTable(data.library.filter(item => (!type || item.type === type)
      && searchMatch(listQuery, item.title, item.identifier, item.source)));
  }
}

function currentManuscript() {
  return manuscript(current.id);
}
function getVersion(item, id = "") {
  return item.versions.find(version => version.id === id) || item.versions.at(-1);
}
function manuscriptDetail(item) {
  const terminal = finished(item);
  const agentTab = ["agent","docket"].includes(detailTab);
  const actions = (terminal ? "" : (can("upload") && (!agentTab || item.versions.length)
      ? button("上传版本", "upload", "upload", agentTab ? "" : "primary") : "")
    + (!agentTab && can("review") ? button("新增问题", "new-issue", "plus") : ""))
    + (!agentTab && can("flow") ? button("移到回收站","trash-manuscript","archive","danger") : "");
  const stageFlow = `<div class="steps panel" aria-label="稿件流转节点">${data.stages.map(([label], index) => {
    const complete = item.status === "archived" || index < item.stage;
    const isCurrent = index === item.stage && !complete;
    return `<div class="step ${complete ? "complete" : isCurrent ? "current" : ""}" ${isCurrent ? 'aria-current="step"' : ""}>
      <span>${complete ? icon("check", 14) : index + 1}</span><strong>${label}</strong></div>`;
  }).join("")}</div>`;
  const detailLinks = links => links.map(([key,label]) =>
    `<a class="tab ${detailTab === key ? "active" : ""}" href="${itemHref(item.id,key)}"
      ${key === detailTab ? 'aria-current="page"' : ""}>${label}</a>`).join("");
  const manualOpen = manualDetails.some(([key]) => key === detailTab);
  return `<div class="page-head"><div>
    <a class="text-link" href="#manuscripts">返回稿件管理</a><h1 style="margin-top:8px">${esc(item.title)}</h1><div class="detail-meta">
    <span>${item.id}</span><span>${esc(item.category)}</span><span>作者 ${esc(item.author)}</span><span>责任编辑 ${esc(item.editor)}</span>${statusChip(item.status)}${item.example ? chip("演示稿件") : ""}</div>
    </div><div class="head-actions">${actions}</div></div>${detailTab === "overview" ? stageFlow : ""}
    <section class="panel"><nav class="tabbar" aria-label="稿件详情">${detailLinks(primaryDetails)}</nav>
    <details class="manual-tools"${manualOpen ? " open" : ""}><summary>人工编辑工具</summary>
    <nav class="tabbar" aria-label="人工编辑工具">${detailLinks(manualDetails)}</nav></details>
    ${detailTab === "issues" ? issueTree(linkedIssues(item.id), {showManuscripts:false}) : `<div class="detail-content">${detailContent(item)}</div>`}</section>`;
}
function detailContent(item) {
  const access = finished(item) || !can("review");
  if (["agent","docket"].includes(detailTab)) return agentPane(item);
  if (detailTab === "overview") {
    const unresolved = linkedIssues(item.id).filter(openIssue);
    const blocking = unresolved.filter(issue => issue.blocking);
    const rightsPending = linkedIssues(item.id).filter(issue => issue.copyright && issue.copyright.status !== "ready").length;
    const actions = !can("flow") ? "" : item.status === "active" ?
      button(item.stage === 4 ? "签核归档" : "进入下一节点", "transition", "check", "primary", 'data-id="advance"')
      + (item.stage > 0 ? button("退回上一节点", "transition", "", "", 'data-id="return"') : "")
      + button("要求作者补件", "transition", "", "", 'data-id="request"')
      + button("暂停加工", "transition", "", "", 'data-id="pause"') :
      (["paused", "revision"].includes(item.status) ? button("恢复流转", "transition", "refresh", "primary", 'data-id="resume"') : "");
    const reject = !finished(item) && can("flow") && [1, 3].includes(item.stage)
      ? button("登记退稿决定", "transition", "", "danger", 'data-id="reject"') : "";
    return `<div class="content-grid"><section class="content-box"><h3>${data.stages[item.stage][0]}</h3><p>${data.stages[item.stage][1]}</p>
      ${blocking.length ? `<br>${note(`当前有 ${blocking.length} 项阻断事项，核实后可恢复流转。`, true)}` : ""}
      <div class="actions">${actions}${reject}</div></section><section class="content-box"><h3>稿件信息</h3><p>${esc(item.description || "暂无稿件说明")}</p>
      <p style="margin-top:7px">读者对象 · ${esc(item.audience || "未登记")}　适用地区 · ${esc(item.region || "未登记")}</p>
      <div class="actions">${linkButton(`查看未结事项（${issueCount(unresolved)}）`, itemHref(item.id, "issues"), "tasks")}
      ${rightsPending ? linkButton(`待版权核验（${rightsPending}）`,itemHref(item.id,"copyright"),"shield") : ""}
      ${unresolved.some(issue => issue.state === "waiting") ? button("导出补件清单", "export-supplement", "download") : ""}</div></section>
      <section class="content-box"><h3>最新流转</h3>${events(item.events.slice(0, 3))}</section></div>`;
  }
  if (detailTab === "text") {
    const version = getVersion(item, current.params.get("v"));
    if (!version) return empty("还没有稿件文件", "上传 Word 或文本开始初筛。", "file");
    return `<div class="actions" style="margin-bottom:12px">
      <label class="field">稿件版本<select data-filter="v">${options(item.versions.map(ver => [ver.id, ver.label + " · " + ver.type]), version.id)}</select></label>
      ${version.expired ? chip("已到保留期限","amber") : linkButton("下载原件", versionPath(item.id, version.id, "download"), "download")}
      ${!version.expired && version.name.toLowerCase().endsWith(".docx") ? linkButton("导出 Word 批注", versionPath(item.id, version.id, "comments"), "pen") : ""}
      ${version.diffCount ? button(`查看返修差异（${version.diffCount}）`, "show-diff", "grid", "", `data-id="${version.id}"`) : ""}
      ${!finished(item) && can("upload") ? button("重跑检查","recheck","refresh","",`data-id="${version.id}"`) : ""}
      ${data.tasks.some(task=>task.version===version.id) ? button("检查记录","check-tasks","clock","",`data-id="${version.id}"`) : ""}
      ${finished(item) && can("maintenance") && !version.retired ? button("保留期限","retention","clock","",`data-id="${version.id}"`) : ""}
      </div><p class="panel-note" style="margin-bottom:10px">${esc(version.name)} · ${dateLabel(version.created, true)} · ${version.paragraphs} 个结构段落
      ${version.supersededBy ? " · 已被 " + esc(getVersion(item,version.supersededBy)?.label) + " 替换" : ""}
      ${version.retentionUntil ? " · 保留至 "+esc(version.retentionUntil) : ""}</p>
      ${version.expired ? "" : `<div id="version-reader" data-version="${version.id}">${empty("加载原文中…", "", "file")}</div>`}`;
  }
  if (detailTab === "comparison") {
    return `<div class="content-grid"><section class="content-box"><h3>同类书目</h3>
      <p>输入书名、读者或主题查询已审定书目。覆盖范围随结果显示。</p>
      <div class="actions"><form data-inline="books" class="actions">${input("query", "书目关键词", bookQuery, {required:true, placeholder:"例如 质量、制造"})}<button class="button primary" type="submit">${icon("search",16)} 查询</button></form></div>
      ${bookQuery ? catalogResults(item) : ""}</section><section class="content-box"><h3>对照意见</h3>
      ${access ? "" : button("新增对照意见", "comparison", "plus", "compact")}
      ${item.comparisons.length ? item.comparisons.map(result => `<div class="evidence-card"><strong>${esc(result.title)}</strong> ${chip(result.coverage, "blue")}
        <p>重合点 · ${esc(result.overlap)}</p><p>差异 · ${esc(result.difference)}</p><small>来源 · ${esc(result.source)}</small></div>`).join("") : '<p style="margin-top:8px">暂无对照意见</p>'}</section></div>`;
  }
  if (detailTab === "verification") {
    return `<div class="content-grid"><section class="content-box"><h3>专业条款检索</h3><p>按核验时点、项目授权、适用地区和对象筛选已审定条款。</p>
      <form data-inline="evidence" class="field-grid" style="margin-top:10px">${input("query", "主张或条款关键词", evidenceResult?.query || "", {full:true, placeholder:"例如 缺陷，或直接填写段落号"})}
      ${select("version","稿件版本",item.versions.filter(version=>!version.expired).map(version=>[version.id,version.label + " · " + version.type]),evidenceResult?.version || item.versions.filter(version=>!version.expired).at(-1)?.id)}
      ${input("anchor","原文段落号（可选）",Number.isInteger(evidenceResult?.anchor) ? evidenceResult.anchor+1 : "",{type:"number",min:"1"})}
      ${input("date", "适用基准日", evidenceResult?.date || nowDate(), {required:true,type:"date"})}
      <div class="actions"><button type="submit" class="button primary">${icon("search",16)} 检索</button></div></form>
      ${evidenceResult ? evidenceCards(access) : ""}</section>
      <section class="content-box"><h3>引文身份回查</h3><p>登记来源与字段差异。书目身份和原文论据分别核实。</p>
      <div class="actions">${button("正文与文末对照","citation-align","list","compact")}
      ${access ? "" : button("查 Crossref DOI","crossref","search","compact")+button("登记回查记录", "bibliography", "plus", "compact")}</div>
      ${item.bibliography.length ? item.bibliography.slice().sort((a,b) => a.chapter.localeCompare(b.chapter,"zh-CN") || a.number - b.number).map(record =>
        `<div class="evidence-card"><strong>${esc(record.key)}</strong> ${chip(...referenceStates[record.status])}
        <p>${esc(record.chapter)} · 第 ${record.number} 条</p><p>${esc([record.sourceA, record.sourceB].filter(Boolean).join(" / "))}</p>
        <small>${esc(record.note)}</small>${access ? "" : `<div>${button("更新记录", "bibliography", "", "compact", `data-id="${item.bibliography.indexOf(record)}"`)}</div>`}</div>`).join("") : '<p style="margin-top:8px">暂无人工回查记录</p>'}</section></div>`;
  }
  if (detailTab === "assessment") {
    const outcome = item.assessment;
    return `<section class="content-box"><h3>学术评估与送审意见</h3>${outcome.recommendation ? chip(outcome.recommendation, "purple") : ""}
      ${[["question", "研究问题"], ["increment", "相较已有著作的增量"], ["evidence", "依据和适用范围"]].map(([key, label]) =>
        `<div style="margin-top:10px"><strong>${label}</strong><p style="white-space:pre-wrap">${esc(outcome[key] || "尚未填写")}</p></div>`).join("")}
      <div class="actions">${access ? "" : button("编辑送审意见","assessment","pen","primary")}
      ${button("摘录稿件与对照材料","excerpt","book")}</div></section>`;
  }
  if (detailTab === "proofs") return proofPage(item);
  if (detailTab === "copyright") return copyrightPage(item);
  return `<section class="content-box"><h3>流转记录</h3>${events(item.events)}</section>`;
}
function events(items) {
  if (!items.length) return "<p>暂无流转记录</p>";
  return `<ul class="event-list">${items.map(item => `<li><strong>${esc(item.label)}</strong>
    <small>${esc(item.owner)} · ${dateLabel(item.time, true)}</small>${item.note ? `<p>${esc(item.note)}</p>` : ""}</li>`).join("")}</ul>`;
}
function catalogResults(item) {
  const tokens = bookQuery.trim().toLowerCase().split(/[\s，、,]+/).filter(Boolean);
  const canSearch = entry => (entry.projects || "").split(/[\s，,]+/).some(project => project === "*" || project === item.id);
  const internal = data.manuscripts.filter(entry => entry.id !== item.id).map(entry => ({
    id: entry.id, title: entry.title, text: entry.description, source: "社内稿件登记 · " + entry.id,
    coverage: "选题登记", keywords: entry.category + " " + entry.audience
  }));
  const external = data.library.filter(entry => entry.type === "book" && entry.status === "approved"
    && canSearch(entry) && (!entry.approvalUntil || entry.approvalUntil > nowDate()));
  const found = [...internal, ...external].map(entry => ({
    entry, score: tokens.filter(token => searchMatch(token, entry.title, entry.keywords, entry.text)).length
  })).filter(result => result.score).sort((a,b) => b.score - a.score).slice(0, 8).map(result => result.entry);
  if (!found.length) return `<p style="margin-top:10px">没有匹配的授权书目。</p>`;
  return found.map(entry => `<div class="evidence-card"><strong>${esc(entry.title)}</strong> ${chip(entry.coverage || "目录", "blue")}
    <p>${esc(entry.text)}</p><small>${esc(entry.source)}</small>
    <div class="actions">${!finished(item) && can("review") ? button("加入对照", "comparison", "plus", "compact", `data-id="${entry.id}"`) : ""}
    ${entry.coverage === "授权全文" ? button("比对授权全文","compare-book","search","compact",`data-id="${entry.id}"`) : ""}</div></div>`).join("");
}
function evidenceCards(access) {
  const items = evidenceResult.results;
  const located = evidenceResult.block ? `<div class="evidence-card"><a href="${sourceLink(current.id,evidenceResult.version,evidenceResult.anchor)}">查看原文第 ${evidenceResult.anchor+1} 段</a><p><mark>${esc(evidenceResult.block.text)}</mark></p></div>` : "";
  if (!items.length) return `${located}<div style="margin-top:10px">${note("待补证 · 未找到同时满足权限、日期和适用范围的条款。", true)}</div>`;
  return located + `<div style="margin-top:10px">${note("已匹配可回查证据，请编辑逐条判断是否支持主张。")}</div>` +
    items.map(entry => `<div class="evidence-card"><strong>${esc(entry.title)}</strong>
      <small style="display:block">${esc(entry.identifier)} · ${esc(entry.edition)} · ${esc(entry.chapter)} · 第 ${esc(entry.page)} 页</small>
      <p>${esc(entry.excerpt)}</p><small>${esc(entry.source)} · ${esc(entry.region || "通用地区")} · ${esc(entry.audience || "通用对象")}</small>
      ${access ? "" : `<div class="actions">${button("带证据登记问题", "evidence-issue", "link", "compact", `data-id="${entry.id}"`)}</div>`}</div>`).join("");
}
function reader(blocks, highlighted = null, item = null, version = null, figureLinks = {}) {
  if (!blocks.length) return empty("该文件没有在线原文", "请下载原件查看。", "file");
  return `<div class="reader" tabindex="0" aria-label="稿件原文">${blocks.map((block, index) =>
    `<div class="paragraph ${block.kind === "heading" ? "heading" : ""} ${index === highlighted ? "highlight" : ""}" id="para-${index}">
      <em title="${esc(block.location)}">${index + 1}</em><div>${esc(block.text)}${block.kind === "footnote" ? ` <small>〔${esc(block.location)}〕</small>` : ""}
      ${attachmentMeta(block)}${(figureLinks[index] || []).map(link =>
        button(`图 ${link.number}${link.page ? ` · 第 ${link.page} 页` : ""}`,"jump-figure","link","compact",`data-id="${link.anchor}"`)).join(" ")}
      ${block.page && item && version ? button("查看校样页","pdf-page","","compact",`data-id="${index}" data-version="${version.id}" data-project="${item.id}"`) : ""}
      </div></div>`).join("")}</div>`;
}

function render() {
  clearTimeout(agentTimer);
  agentResult = null;
  renderShell();
  const item = currentManuscript();
  const loaders = {home: dashboard, manuscripts: () => current.id
      ? item ? manuscriptDetail(item) : empty("找不到这件稿件") : manuscriptPage(false),
    issues: issuePage, workflow: workflowPage, library: libraryPage, archive: () => manuscriptPage(true),
    trial: () => trialView(data,current.params,can("trial")), trash:trashPage};
  main.innerHTML = loaders[current.page]();
  main.setAttribute("aria-busy", "false");
  document.title = `${item?.title || labels[current.page]} · 文衡编辑台`;
  updateResults();
  const agentTarget = main.querySelector("[data-agent-view][data-run]");
  if (agentTarget && item) loadAgentPane(agentTarget,item);
  const target = main.querySelector("#version-reader");
  if (target && item) {
    api(versionPath(item.id, target.dataset.version, "text"))
      .then(result => {
        const anchor = current.params.has("anchor") ? Number(current.params.get("anchor")) : null;
        target.innerHTML = reader(result.blocks,anchor,item,getVersion(item,target.dataset.version),result.figureLinks);
        if (anchor !== null) target.querySelector(".paragraph.highlight")?.scrollIntoView({block:"center"});
      })
      .catch(error => {target.innerHTML = note(error.message, true);});
  }
}

const agentCaps = () => ({upload:can("upload"),flow:can("flow"),review:can("review")});

function agentPane(item) {
  const run = (data.agentRuns || []).find(entry => entry.manuscript === item.id);
  const content = run ? empty("加载审校记录…","", "clock") : agentView({item,caps:agentCaps(),view:detailTab});
  return `<div class="agent-view" data-agent-view="${detailTab}"${run ? ` data-run="${esc(run.id)}"` : ""}>${content}</div>`;
}

async function loadAgentPane(target, item) {
  if (!target.isConnected) return;
  const id = encodeURIComponent(target.dataset.run);
  try {
    const run = await api(`/api/agent/status/${id}`);
    if (!target.isConnected) return;
    let brief, docket;
    if (run.status === "complete" && !run.stale) {
      [brief,docket] = await Promise.all([api(`/api/agent/brief/${id}`), api(`/api/agent/docket/${id}`)]);
    }
    if (!target.isConnected) return;
    const cases = docket?.cases || brief?.cases || [];
    const model = {item,run,brief,cases,caps:agentCaps(),view:target.dataset.agentView};
    const markup = agentView(model);
    if (agentResult?.markup !== markup) {
      const trace = target.querySelector(".agent-progress .agent-trace");
      const expanded = trace?.open;
      const focused = trace?.querySelector("summary") === document.activeElement;
      target.innerHTML = markup;
      const nextTrace = target.querySelector(".agent-progress .agent-trace");
      if (expanded && nextTrace) nextTrace.open = true;
      if (focused) nextTrace?.querySelector("summary").focus({preventScroll:true});
    }
    agentResult = {run,brief,cases,markup};
    if (["queued","running"].includes(run.status)) {
      agentTimer = setTimeout(() => loadAgentPane(target,item), 1200);
    }
  } catch (error) {
    if (!target.isConnected) return;
    target.innerHTML = note(error.message,true) + button("重新加载记录","agent-refresh","refresh");
    agentResult = null;
  }
}

function agentRulingDialog(runId, caseId) {
  if (agentResult?.run.id !== runId || agentResult.run.stale) return;
  const finding = agentResult.cases.find(entry => entry.id === caseId);
  if (!finding) return;
  const choices = rulingOptions(finding,agentCaps());
  if (!choices.length) return;
  const fields = `<fieldset class="agent-ruling-options field full"><legend>裁决选项</legend>
    ${choices.map(option => `<label><input name="option" type="radio" value="${esc(option.id)}" required>
      <span>${esc(option.label)}</span></label>`).join("")}</fieldset>`;
  formDialog("裁决案卷",fields + textarea("note","裁决依据","",true,1000),async ({option,note}) => {
    await api("/api/agent/ruling",{run:runId,case:caseId,option,note});
    return {message:"裁决已登记。"};
  },{help:finding.title,submit:"确认提交裁决"});
}

function agentApprovalDialog(item, runId) {
  formDialog("批准出版进入印制",textarea("note","终审意见（可选）","",false,1000),async ({note}) => {
    await api("/api/agent/approve-publish",{run:runId,note});
    return {message:"稿件已签发，可下载印制批单。"};
  },{help:`确认《${item.title}》的终审结果并生成印制批单。`,submit:"批准出版进入印制"});
}

function showDialog(title, content, wide = false) {
  modal.className = wide ? "wide" : "";
  modal.innerHTML = `<header class="dialog-head"><h2 id="dialog-title">${esc(title)}</h2>
    <button class="icon-button" type="button" data-action="close" aria-label="关闭">${icon("close", 20)}</button></header><div class="dialog-content">${content}</div>`;
  if (!modal.open) modal.showModal();
}

function formDialog(title, fields, handler, options = {}) {
  const {wide = false, help = "", submit = "保存", danger = false} = options;
  showDialog(title, `<form id="dialog-form">${help ? note(help,danger) : ""}<div class="field-grid">${fields}</div>
    <div class="form-error" role="alert" hidden></div><div class="dialog-actions">
    ${button("取消", "close", "", "", danger ? "autofocus" : "")}
    <button type="submit" class="button ${danger ? "danger" : "primary"}">${esc(submit)}</button></div></form>`, wide);
  const form = modal.querySelector("form");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const submitButton = form.querySelector("[type='submit']");
    const errorElement = form.querySelector(".form-error");
    submitButton.disabled = true;
    errorElement.hidden = true;
    try {
      const fields = Object.fromEntries(new FormData(form));
      const response = await handler(fields, form) || {};
      modal.close();
      await refresh();
      if (response.route) location.hash = response.route;
      if (response.message) toast(response.message);
    } catch (error) {
      errorElement.textContent = error.message;
      errorElement.hidden = false;
      if (!modal.open) toast(error.message, true);
    } finally {
      submitButton.disabled = false;
    }
  });
  return form;
}
function createDialog() {
  formDialog("登记新稿", input("title", "稿件名称", "", {required:true, full:true, limit:160})
    + input("author", "作者", "", {required:true, limit:80}) + input("editor", "责任编辑", reviewerName(), {required:true, limit:80})
    + select("category", "稿件类别", ["工程技术", "人文社科", "艺术设计", "建筑设计", "教育教材", "其他"])
    + input("region", "适用地区", "", {placeholder:"按项目填写", limit:60})
    + input("audience", "读者对象", "", {full:true, placeholder:"例如 工艺工程师", limit:100}) + textarea("description", "稿件说明", "", false, 1000),
    async fields => {
      const result = await api("/api/manuscripts", fields);
      return {route: itemHref(result.id), message: "稿件已登记，可以上传原稿。"};
    }, {submit:"登记稿件"});
}
function uploadDialog(item) {
  const dataFiles = item.versions.filter(version => version.type === "补件" && !version.expired && !version.supersededBy
    && /\.(csv|xlsx)$/i.test(version.name));
  const replacement = dataFiles.length ? '<div data-supplement-replacement hidden>' + select("supersedes","替换旧数据补件",
    [["","新增独立补件"],...dataFiles.map(version => [version.id, `${version.label} · ${version.name}`])]) + "</div>" : "";
  const form = formDialog("上传稿件版本", select("kind", "文件类型", ["原稿", "返修稿", "校样", "补件"], item.versions.length ? "返修稿" : "原稿", true)
    + '<label class="field full">选择文件<input name="file" type="file" accept=".docx,.txt,.pdf,.csv,.xlsx" required><small>支持 Word、文本、PDF，原始数据补件可用 CSV 或 XLSX，单个文件最多 20 MB。</small></label>'
    + replacement,
    async fields => {
      const file = fields.file;
      if (!file.size || file.size > 20 * 1024 * 1024) throw new Error("请选择 20 MB 以内的非空文件。");
      const encoded = await fileBase64(file);
      const result = await api(`/api/manuscripts/${item.id}/upload`, {kind:fields.kind, name:file.name, content:encoded,
        supersedes:fields.supersedes || ""});
      if (fields.kind === "补件") {
        return {route:fields.supersedes && result.agentRun ? itemHref(item.id,"agent") : itemHref(item.id,"text") + "&v=" + result.id,
          message:result.agentRun ? "补件已保存，智能审校已排队。" : `补件已保存，新增 ${result.issues} 条待判断事项。`};
      }
      return {route:itemHref(item.id,"agent"),message:"版本已保存，智能审校已排队。"};
    }, {submit:"上传并检查", help:"PDF 可生成页内定位，有效分辨率按印制参数检查。请人工确认扫描识别和图件授权。"});
  if (dataFiles.length) {
    const updateReplacement = () => {
      const enabled = form.elements.kind.value === "补件" && /\.(csv|xlsx)$/i.test(form.elements.file.files[0]?.name || "");
      form.querySelector("[data-supplement-replacement]").hidden = !enabled;
      form.elements.supersedes.disabled = !enabled;
    };
    form.elements.kind.addEventListener("change",updateReplacement);
    form.elements.file.addEventListener("change",updateReplacement);
    updateReplacement();
  }
}
const actionNames = {advance:"确认进入下一节点", return:"退回上一节点", request:"要求作者补件",
  pause:"暂停加工", resume:"恢复流转", reject:"登记退稿决定"};
function transitionDialog(item, action) {
  const title = action === "advance" && item.stage === 4 ? "签核归档" : actionNames[action];
  const help = {reject:"请在选题会或编辑部作出决定后登记，并填写决定依据。", advance:
    item.stage === 4 ? "签核前应确认当前版本的版权事项、补件及校样均已处理。" : "请确认当前节点必需材料和审校事项均已完成。"};
  formDialog(title, textarea("note", "处理说明", "", true, 1000), async fields => {
    if (action === "request") {
      const rows = linkedIssues(item.id).filter(issue => issue.state === "waiting");
      if (!fields.note && rows.length === 0) throw new Error("请填写需要补齐的材料。");
    }
    await api(`/api/manuscripts/${item.id}/action`, {action, note:fields.note});
    return {message: "流转记录已保存。"};
  }, {help:help[action], submit:"确认"});
}
function newIssueDialog(item, evidence = null, preset = "收稿材料") {
  const version = getVersion(item, evidence ? evidenceResult.version : current.params.get("v"));
  formDialog(evidence ? "登记有证据的问题" : "新增稿件问题",
    input("title","问题标题","",{required:true, full:true, limit:160})
    + select("category","问题类别",categories,evidence ? "专业审校" : preset)
    + select("severity","优先级",Object.entries(levelNames),"normal")
    + textarea("detail","问题说明","",true)
    + (evidence ? `<div class="field full">${note(`${evidence.title} · ${evidence.edition} · 第 ${evidence.page} 页${Number.isInteger(evidenceResult.anchor) ? ` · 原文第 ${evidenceResult.anchor+1} 段` : ""}`)}
      <div class="evidence-card">${esc(evidence.excerpt)}</div></div>` :
      select("version","关联版本",[["","最新版本"], ...item.versions.map(ver => [ver.id,ver.label + " · " + ver.type])],version?.id)
      + input("anchor","原文段落号（可选）","",{type:"number",min:"1"})
      + input("evidenceLabel","证据来源","",{full:true,limit:300}) + textarea("evidence","证据原文","",false,12000))
    + '<label class="field-check"><input type="checkbox" name="blocking"> 未处置时阻断流转</label>',
    async fields => {
      const binding = evidence ? {citationTicket:evidence.citationTicket,version:evidenceResult.version,anchor:evidenceResult.anchor}
        : {anchor:fields.anchor ? Number(fields.anchor)-1 : null};
      await api(`/api/manuscripts/${item.id}/issue`, {...fields,...binding,blocking:fields.blocking === "on"});
      return {route:itemHref(item.id,fields.category === "版权核验" ? "copyright" : "issues"), message:"问题已登记。"};
    });
}

const decisionReasons = [
  "已核对原文，确认问题", "已在修订稿中修正", "属于正常版式或示例标签",
  "已找到对应文献条目", "已核对依据，保留原文", "请作者补充材料", "请专业人员复核"
];

function decisionBasisFields(saved = "") {
  const [first,...rest] = saved.split("\n");
  const preset = decisionReasons.includes(first) ? first : "";
  return select("basis","处置依据",[["","选择常用依据"],...decisionReasons,["other","其他（填写说明）"]],preset,true)
    + textarea("note","补充说明（可选）",preset ? rest.join("\n") : saved,false,1000);
}

function decisionNote(fields) {
  const detail = fields.note.trim();
  if (fields.basis === "other" && !detail) throw new Error("请补充说明其他处置依据。");
  const value = [decisionReasons.includes(fields.basis) ? fields.basis : "",detail].filter(Boolean).join("\n");
  if (value.length > 1000) throw new Error("处置依据和说明合计请控制在 1000 字以内。");
  return value;
}

async function issueDialog(id, requestedAnchor) {
  const issue = data.issues.find(record => record.id === id);
  if (!issue) return;
  const item = manuscript(issue.manuscript);
  const version = item.versions.find(record => record.id === issue.version);
  const selected = Number(requestedAnchor);
  const anchor = issue.locations?.some(location => location.anchor === selected) ? selected : issue.anchor;
  const editable = !finished(item) && can("decision");
  const key = repeatKey(issue);
  const repeats = key === null ? [] : data.issues.filter(record => repeatKey(record) === key);
  const positions = [...new Set(repeats.flatMap(record => record.repeatAnchors))].sort((a,b) => a-b);
  const similar = key !== null ? repeats : issue.group ? data.issues.filter(record => record.manuscript === issue.manuscript
    && record.version === issue.version && record.group === issue.group && record.state === "pending") : [];
  const revisions = item.versions.at(-1);
  const versionChanged = revisions && version && revisions.id !== version.id;
  const decisionForm = editable ? `<form data-decision class="field-grid" style="margin-top:14px">
    ${select("state", "处置结果", [["confirmed","确认问题，待处理"],["resolved","完成处置"],["dismissed","标记例外"],["waiting","退回补证"],["pending","重新打开"]], issue.state === "pending" ? "confirmed" : "resolved", true)}
    ${decisionBasisFields(issue.note)}
    ${similar.length > 1 && (key !== null || issue.state === "pending") ? `<label class="field-check"><input name="batch" type="checkbox" ${key !== null ? "checked" : ""}> ${key !== null ? "同时处置这组重复段落" : `同时处置 ${similar.length} 条同稿同版本的同类问题`}</label>` : ""}
    <div class="form-error field full" role="alert" hidden></div><div class="actions"><button type="submit" class="button primary">保存处置</button></div></form>` : "";
  showDialog(issue.title, `<div class="issue-layout"><div>
    <div class="actions" style="margin-bottom:11px">${chip(issue.category)}${chip(levelNames[issue.severity],toneByLevel[issue.severity],true)}${issueChip(issue.state)}
    ${issue.blocking ? chip("阻断事项","red") : ""}</div>
    <section class="issue-section"><h3>问题说明</h3><p>${esc(issue.detail)}</p><small>${esc(issue.owner)} · ${version ? esc(version.label) : "未关联版本"}</small></section>
    <section class="issue-section"><h3>核验证据</h3>${issue.evidence.length ? issue.evidence.map(evidence =>
    `<div class="evidence-card"><strong>${esc(evidence.label)}</strong><p>${esc(evidence.text)}</p>
    ${Number.isInteger(evidence.anchor) ? button("定位原文", "jump-source", "link", "compact", `data-id="${evidence.anchor}"`) : ""}</div>`).join("") : "<p>暂无附加证据</p>"}</section>
    ${issue.category === "引文核验" && version && !version.expired
      ? button("查看本版引文对照","citation-align-issue","list","compact",`data-id="${issue.id}"`) : ""}
    ${issue.copyright ? `<section class="issue-section"><h3>版权核验</h3>${copyrightChip(issue.copyright)}
      ${issue.copyright.missing?.length ? `<p>尚待覆盖 · ${esc(issue.copyright.missing.join("、"))}</p>` : ""}
      ${rightsDisplay(issue.rights || {},item.versions)}
      ${can("review") && !finished(item) ? button("登记版权核验","rights","pen","compact",`data-id="${issue.id}"`) : ""}</section>` : ""}
    ${decisionForm}</div><section><h3>关联原文</h3>
    <a class="text-link" href="${itemHref(item.id, version ? "text" : "overview")}${version ? "&v=" + version.id : ""}" data-action="close-link">${esc(item.title)} ${icon("external",14)}</a>
    ${positions.length ? '<div id="repeat-positions" style="margin-top:8px"></div>' : ""}
    ${versionChanged ? `<div style="margin-top:7px">${note("此问题关联较早版本，请与最新返修稿对照。")}</div>` : ""}
    <div id="issue-reader" style="margin-top:10px">${empty(version ? "正在加载关联原文…" : "尚未关联原文", "", "file")}</div>
    </section></div>`, true);
  const form = modal.querySelector("[data-decision]");
  if (form) form.addEventListener("submit", async event => {
    event.preventDefault();
    const fields = Object.fromEntries(new FormData(form));
    const errorBox = form.querySelector(".form-error");
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    errorBox.hidden = true;
    try {
      await api("/api/issues/decision", {ids:fields.batch ? similar.map(record => record.id) : [issue.id], state:fields.state, note:decisionNote(fields)});
      modal.close();
      await refresh();
      toast("处置记录已保存。");
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });
  if (version) {
    const container = modal.querySelector("#issue-reader");
    try {
      const result = await api(versionPath(item.id, version.id, "text"));
      container.innerHTML = reader(result.blocks, anchor, item, version, result.figureLinks);
      const selector = container.parentElement.querySelector("#repeat-positions");
      if (selector) {
        selector.innerHTML = select("position",`重复位置（${positions.length} 处）`,
          positions.map(index => [index,`${result.blocks[index]?.page ? `第 ${result.blocks[index].page} 页 · ` : ""}第 ${index+1} 段`]),anchor);
        selector.querySelector("select").addEventListener("change",event =>
          jumpReader(container.querySelector(".reader"),event.target.value,false));
      }
      if (modal.open && Number.isInteger(anchor)) jumpSource(anchor, false);
    } catch (error) { container.innerHTML = note(error.message, true); }
  }
}
function jumpSource(index, takeFocus = true) {
  jumpReader(modal.querySelector("#issue-reader .reader"),index,takeFocus);
}
function jumpReader(viewport,index,takeFocus = true) {
  const target = viewport?.querySelector("#para-" + Number(index));
  if (!target) return;
  viewport.querySelectorAll(".paragraph.highlight").forEach(element => element.classList.remove("highlight"));
  target.classList.add("highlight");
  viewport.scrollTop += target.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 12;
  if (takeFocus) {
    target.tabIndex = -1;
    target.focus({preventScroll:true});
  }
}
async function diffDialog(item, id) {
  const result = await api(versionPath(item.id, id, "text"));
  showDialog("返修差异", result.changes.length ? result.changes.map((change, index) =>
    `<div class="evidence-card"><strong>${index + 1}. ${esc(change.kind)}</strong>
    <div class="diff-row"><div><small>旧版</small><br>${esc(change.before.join("\n\n") || "—")}</div>
    <div><small>本版</small><br>${esc(change.after.join("\n\n") || "—")}</div></div></div>`).join("") : empty("没有段落差异"), true);
}
function comparisonDialog(item, id) {
  const book = data.library.find(entry => entry.id === id);
  const record = manuscript(id);
  const source = book?.source ?? (record ? "社内稿件登记 · " + record.id : "");
  formDialog("新增同类书对照",
    input("title","对照书目",book?.title ?? record?.title ?? "",{required:true,full:true})
    + select("coverage","已查材料",["目录","前言","样章","授权全文","选题登记"],book?.coverage ?? (record ? "选题登记" : "目录"))
    + input("source","来源或定位",source,{required:true})
    + textarea("overlap","重合点","",true) + textarea("difference","可核实的差异","",true),
    async fields => {
      await api(`/api/manuscripts/${item.id}/comparison`, fields);
      return {message:"对照意见已保存。"};
    });
}
function bibliographyDialog(item, id, prefill = {}) {
  const record = item.bibliography[id] || prefill;
  formDialog("登记引文回查",
    input("key","书目题名、DOI 或 ISBN",record.key,{required:true,full:true,limit:500})
    + input("chapter","章节",record.chapter,{required:true})
    + input("number","章内顺序",record.number,{required:true,type:"number",min:"1"})
    + select("status","回查结论",Object.entries(referenceStates).map(([key,[title]]) => [key,title]),record.status || "verified")
    + select("covered","核查来源覆盖该期", [["no","缺覆盖或未知"],["yes","原刊目录覆盖该期"]], record.covered || "no")
    + input("sourceA","来源一",record.sourceA,{full:true,limit:500})
    + input("sourceB","来源二（独立来源）",record.sourceB,{full:true,limit:500})
    + textarea("note","核验依据及字段差异",record.note,false,500),
    async fields => {
      await api(`/api/manuscripts/${item.id}/bibliography`,fields);
      return {message:"引文回查已保存。"};
    }, {help:"同章连续三条不同引文经两种适当来源回查后仍存在实质冲突或有覆盖却无法核实，会暂停加工并提交人工复核。"});
}
function assessmentDialog(item) {
  const record = item.assessment;
  formDialog("编辑送审意见", textarea("question","研究问题",record.question,true,2000)
    + textarea("increment","相较已有著作的增量",record.increment,true,2000) + textarea("evidence","证据与适用范围",record.evidence,true,2000)
    + select("recommendation","责任编辑意见",["建议送审","先补材料","提请讨论退稿"],record.recommendation || "建议送审",true),
    async fields => {
      await api(`/api/manuscripts/${item.id}/assessment`, fields);
      return {message:"送审意见已保存。"};
    });
}
function libraryCreate() {
  showDialog("登记资料", `<div class="actions">${Object.entries(kinds).map(([type, label]) => button(label, "library-type", type === "term" ? "list" : "library", "", `data-id="${type}"`)).join("")}</div>`);
}
function libraryForm(type) {
  const base = input("title","资料名称","",{required:true,full:true})
    + input("source","来源或许可依据","",{required:true,full:true})
    + input("projects","可访问的项目编号","",{required:true,full:true,placeholder:"多个用逗号分隔，* 表示全部项目"});
  const typeFields = {
    evidence: input("identifier","条款资料标识","",{required:true})
      + input("edition","版本","",{required:true})
      + input("chapter","章条路径","",{required:true}) + input("page","页码","",{required:true})
      + input("region","适用地区") + input("audience","适用对象")
      + input("validFrom","生效日","",{type:"date",required:true})
      + input("validUntil","失效首日","",{type:"date"})
      + input("supersedes","替代的旧版资料编号或唯一标识")
      + input("keywords","检索关键词")
      + select("evidenceType","证据对象",[["clause","条文"],["table","表格"],["formula","公式"]],"clause")
      + textarea("text","证据原文","",true,12000)
      + textarea("conditions","限定语、适用条件与例外","",false,12000)
      + textarea("footnotes","相连脚注（含编号和回引条号）","",false,12000)
      + '<label class="field-check"><input name="requiresFootnotes" type="checkbox" value="yes">检索此条需同时返回脚注</label>'
      + textarea("tableRows","表行证据（每行保留完整表头、行名、单位）","",false,12000)
      + textarea("latex","已核对的公式 LaTeX","",false,12000)
      + textarea("symbols","符号释义及单位","",false,12000),
    book: input("identifier","ISBN 或书目编号") + input("edition","版次")
      + input("author","作者") + select("coverage","授权覆盖",["目录","前言","样章","授权全文"])
      + input("keywords","读者、主题、使用场景","",{full:true}) + textarea("text","材料摘要","",true,12000)
      + '<label class="field full">授权全文（可选）<input type="file" name="corpus" accept=".docx,.txt"><small>可上传 2 MB 以内的 DOCX 或 UTF-8 TXT，供段落对照。</small></label>',
    term: input("variant","需规范的用语变体","",{required:true}) + textarea("text","定义和使用说明","",true,12000),
    watch: input("edition","预警名录版本或年份","",{required:true}) + textarea("text","学科范围及说明","",true,12000)
  };
  formDialog(`登记${kinds[type]}`,base + typeFields[type] + input("approvalUntil","授权到期日","",{type:"date"})
    + input("sourceUrl","公开来源地址","",{full:true,limit:400}),
    async fields => {
      const file = fields.corpus;
      if (file?.size>2*1024*1024) throw new Error("请拆分为 2 MB 以内的授权全文。");
      await api("/api/library",{...fields,type,corpusName:file?.name || "",corpus:file?.size ? await fileBase64(file) : ""});
      return {message:"资料已进入待审定区。"};
    }, {wide:true,submit:"登记并提交审定"});
}
function libraryReview(id) {
  const entry = data.library.find(record => record.id === id);
  const states = Object.entries(libraryStates).filter(([key])=>key==="approved" ? can("approve") : can("library"));
  formDialog("审定资料", select("status","资料状态",states.map(([key,[label]]) => [key,label]),entry.status,true)
    + input("reviewer","审定人",reviewerName(),{required:true,full:true,limit:80}),
    async fields => {
      await api(`/api/library/${id}`,fields);
      evidenceResult = null;
      return {message:"资料状态已更新。"};
    }, {help:entry.title + " · 审定时请确认来源、授权和适用范围。"});
}
function libraryShow(id) {
  const entry = data.library.find(record => record.id === id);
  const keys = {source:"来源或许可依据", identifier:"标识", edition:"版本", chapter:"章条路径", page:"页码", projects:"授权项目",
    region:"适用地区", audience:"适用对象", validFrom:"生效日", validUntil:"失效首日", approvalUntil:"授权到期日",
    supersedes:"替代的旧版", coverage:"覆盖范围", variant:"变体", reviewer:"审定人", checked:"最近核验日"};
  showDialog(entry.title, `<div class="actions">${chip(kinds[entry.type],"purple")}${chip(...libraryStates[entry.status])}</div>
    ${entry.restricted ? note("授权已到期，原文已停止展示。", true) : `<div class="evidence-card"><p>${esc(entry.text)}</p></div>`}
    <div class="field-grid">${Object.entries(keys).filter(([key]) => entry[key]).map(([key, label]) =>
    `<p><strong>${label}</strong><br><small>${esc(entry[key])}</small></p>`).join("")}</div>
    <div class="dialog-actions">${can("approve") || can("library") ? button("审定资料", "library-review", "check", "primary", `data-id="${id}"`) : ""}</div>`);
}
function searchDialog() {
  showDialog("全局搜索", `<label class="input-wrap"><span class="visually-hidden">搜索稿件或问题</span>${icon("search", 16)}
    <input type="search" autofocus placeholder="输入稿名、作者、编号或问题关键词" data-global-search></label>
    <div class="search-results" id="search-results"></div>`);
  const field = modal.querySelector("[data-global-search]");
  field.focus();
  showSearch("");
  field.addEventListener("input", () => showSearch(field.value));
}
function showSearch(query) {
  const shelter = modal.querySelector("#search-results");
  const matches = data.manuscripts.filter(item => searchMatch(query, item.title, item.author, item.id)).slice(0, 7);
  const issues = query.trim() ? data.issues.filter(item => searchMatch(query, item.title, item.category)).slice(0, 5) : [];
  shelter.innerHTML = matches.map(item => `<a class="search-result" href="${itemHref(item.id)}" data-action="close-link">
    ${tile("file","blue",16)}<span><strong>${esc(item.title)}</strong><small>${item.id} · ${esc(item.author)}</small></span></a>`).join("")
    + issues.map(item => `<button type="button" class="search-result" data-action="issue" data-id="${item.id}">
      ${tile("tasks","",16)}<span><strong>${esc(item.title)}</strong><small>${esc(manuscript(item.manuscript).title)}</small></span></button>`).join("");
  if (!shelter.innerHTML) shelter.innerHTML = empty("没有匹配结果", "", "search");
}
function notificationsDialog() {
  const items = openQueue().filter(item => openIssue(item) && item.severity === "high");
  showDialog("工作提醒", items.length ? items.map(item =>
    `<button type="button" class="search-result" data-action="issue" data-id="${item.id}">${tile("alert","rose",16)}
    <span><strong>${esc(item.title)}</strong><small>${esc(manuscript(item.manuscript).title)} · ${issueStates[item.state][0]}</small></span></button>`).join("") : empty("没有高优先级提醒", "", "bell"));
}
function exportSupplement(item) {
  const waiting = linkedIssues(item.id).filter(issue => issue.state === "waiting");
  const lines = [item.title + " · 补件清单", item.id + "　责任编辑 " + item.editor, "",
    ...waiting.map((issue,index) => `${index + 1}. ${issue.title}\n   ${issue.note || issue.detail}\n`)];
  downloadText(item.id + "-补件清单.txt", lines);
}
function downloadText(filename, lines) {
  const url = URL.createObjectURL(new Blob(["\uFEFF" + lines.join("\n")], {type:"text/plain;charset=utf-8"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
  toast("补件清单已导出。");
}

document.addEventListener("input", event => {
  if (event.target.matches("[data-list-search]")) {
    listQuery = event.target.value;
    updateResults();
  }
});
document.addEventListener("change", event => {
  if (!event.target.matches("[data-filter]")) return;
  const key = event.target.dataset.filter;
  const params = new URLSearchParams(current.params);
  if (event.target.value) params.set(key, event.target.value);
  else params.delete(key);
  if (key === "v") params.delete("anchor");
  if (key === "severity") params.set("tab", issueTab);
  const route = current.page + (current.id ? "/" + current.id : "");
  location.hash = route + (params.size ? "?" + params : "");
});
document.addEventListener("submit", async event => {
  const type = event.target.dataset.inline;
  if (!type) return;
  event.preventDefault();
  const fields = Object.fromEntries(new FormData(event.target));
  try {
    if (type === "books") {
      bookQuery = fields.query.trim();
      render();
    }
    if (type === "evidence") {
      const id = current.id;
      const query = new URLSearchParams({project:id,q:fields.query,date:fields.date,version:fields.version || ""});
      if (fields.anchor) query.set("anchor",String(Number(fields.anchor)-1));
      const response = await api("/api/evidence?" + query);
      if (current.id === id) {
        evidenceResult = {...response, query:fields.query, date:fields.date};
        render();
      }
    }
  } catch (error) { toast(error.message, true); }
});
document.addEventListener("click", async event => {
  const target = event.target.closest("[data-action]");
  if (!target) {
    if (sidebar.classList.contains("visible") && !sidebar.contains(event.target)) {
      sidebar.classList.remove("visible");
      document.querySelector(".mobile-toggle").setAttribute("aria-expanded","false");
    }
    return;
  }
  const {action,id} = target.dataset;
  const item = currentManuscript();
  try {
    switch (action) {
      case "close":
      case "close-link": modal.close(); break;
      case "menu": target.setAttribute("aria-expanded",sidebar.classList.toggle("visible")); break;
      case "create": createDialog(); break;
      case "agent-trigger":
        target.disabled = true;
        try {
          await api("/api/agent/trigger",{project:item.id});
          await refresh();
          toast("智能审校已排队。");
        } finally { target.disabled = false; }
        break;
      case "agent-refresh": await refresh(); break;
      case "agent-rule": agentRulingDialog(target.dataset.run,id); break;
      case "agent-approve": agentApprovalDialog(item,id); break;
      case "trash-manuscript":
        formDialog("移到回收站","",async () => {
          await api(`/api/manuscripts/${item.id}/trash`,{});
          return {route:"#manuscripts",message:"稿件已移到回收站。"};
        },{help:`《${item.title}》将从稿件列表和问题清单移出。后续可以从回收站恢复。`,submit:"移到回收站"});
        break;
      case "restore-manuscript":
        await api(`/api/manuscripts/${id}/restore`,{});
        await refresh();
        location.hash = itemHref(id);
        toast("稿件已恢复。");
        break;
      case "delete-manuscript": {
        const entry = data.trash.find(record => record.id === id);
        formDialog("永久删除稿件","",async () => {
          await api(`/api/manuscripts/${id}/delete`,{});
          return {message:"稿件已永久删除。"};
        },{help:`将永久删除《${entry.title}》（${entry.id}）及其附件、审校记录和关联任务。删除后无法恢复。`,
          submit:"永久删除",danger:true});
        break;
      }
      case "search": searchDialog(); break;
      case "notifications": notificationsDialog(); break;
      case "issue": await issueDialog(id,target.dataset.anchor); break;
      case "issue-tab": {
        issueTab = id;
        if (current.page === "issues") {
          const params = new URLSearchParams(current.params);
          params.set("tab", id);
          if (params.toString() !== current.params.toString()) location.hash = "issues?" + params;
          else render();
        } else render();
        main.querySelector('.tab.active')?.focus();
        break;
      }
      case "upload": uploadDialog(item); break;
      case "transition": transitionDialog(item, id); break;
      case "new-issue": newIssueDialog(item); break;
      case "evidence-issue": newIssueDialog(item, evidenceResult.results.find(record => record.id === id)); break;
      case "show-diff": await diffDialog(item,id); break;
      case "jump-source": jumpSource(id); break;
      case "jump-figure": jumpReader(target.closest(".reader"),id); break;
      case "comparison": comparisonDialog(item,id); break;
      case "compare-book": await compareBookDialog(item,id); break;
      case "bibliography": bibliographyDialog(item,id); break;
      case "bibliography-crossref": bibliographyDialog(item,null,{key:target.dataset.doi,sourceA:"Crossref · "+target.dataset.doi}); break;
      case "citation-align": await citationDialog(item); break;
      case "citation-align-issue": {
        const issue = data.issues.find(record => record.id === id);
        await citationDialog(manuscript(issue.manuscript),issue.version,issue.anchor);
        break;
      }
      case "crossref": crossrefDialog(target.dataset.doi); break;
      case "excerpt": await excerptDialog(item); break;
      case "assessment": assessmentDialog(item); break;
      case "proof-settings": proofSettings(item); break;
      case "pdf-page": await pdfPageDialog(target.dataset.project,target.dataset.version,id); break;
      case "rights": rightsDialog(id); break;
      case "copyright-add": newIssueDialog(item,null,"版权核验"); break;
      case "copyright-export": exportCopyrightRequests(item); break;
      case "retention": retentionDialog(item,id); break;
      case "trial-create": trialDialog(); break;
      case "batch": batchDialog(); break;
      case "recheck": await api(`/api/manuscripts/${item.id}/recheck`,{version:id}); await checkTasks(id); break;
      case "check-tasks": await checkTasks(id); break;
      case "library-create": libraryCreate(); break;
      case "library-import": libraryImport(); break;
      case "library-maintenance": libraryMaintenance(); break;
      case "source-check": {
        await api(`/api/library/${id}/check`,{});
        await refresh();
        libraryMaintenance();
        break;
      }
      case "library-type": libraryForm(id); break;
      case "library-review": libraryReview(id); break;
      case "library-show": libraryShow(id); break;
      case "export-supplement": exportSupplement(item); break;
      case "account": await accountDialog(); break;
      case "account-create": addAccountDialog(); break;
      case "account-edit": await editAccountDialog(id); break;
      case "audit": await auditDialog(); break;
      case "logout": await api("/api/logout",{}); showLogin(); break;
    }
  } catch (error) { toast(error.message, true); }
});
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k" && data) {
    event.preventDefault();
    searchDialog();
  } else if (event.key === "Escape" && sidebar.classList.contains("visible")) {
    sidebar.classList.remove("visible");
    const trigger = document.querySelector(".mobile-toggle");
    trigger.setAttribute("aria-expanded","false");
    trigger.focus();
  }
});

navigate();
refresh().catch(error => {
  if (error.code==="LOGIN_REQUIRED") return;
  main.setAttribute("aria-busy", "false");
  main.innerHTML = empty("无法连接编辑工作区", error.message, "alert");
});
function fileBase64(file) {
  return new Promise((resolve,reject) => {
    const read = new FileReader();
    read.onload = () => resolve(read.result.slice(read.result.indexOf(",")+1));
    read.onerror = () => reject(new Error("读取文件失败。"));
    read.readAsDataURL(file);
  });
}
function proofPage(item) {
  const choices = linkedIssues(item.id).filter(issue => ["校样复核","图片授权","地图审核","图文核验"].includes(issue.category));
  const settings = item.proofSettings || {};
  return `<div class="content-box"><h3>印制参数</h3>
    <p>照片 ${settings.photoPpi ?? 300} PPI · 线稿 ${settings.linePpi ?? 600} PPI · 首次指代与图件允许相距 ${settings.maxFigurePages ?? 1} 页</p>
    ${!finished(item) && can("upload") ? `<div class="actions">${button("编辑印制参数","proof-settings","pen","compact")}</div>` : ""}
    </div><div style="margin-top:12px">${issueTree(choices,{showManuscripts:false})}</div>`;
}
function proofSettings(item) {
  const settings = item.proofSettings || {};
  formDialog("印制参数",input("photoPpi","照片 PPI",settings.photoPpi ?? 300,{required:true,type:"number",min:"72"})
    + input("linePpi","线稿 PPI",settings.linePpi ?? 600,{required:true,type:"number",min:"72"})
    + input("maxFigurePages","首次指代与图件最大页差",settings.maxFigurePages ?? 1,{required:true,type:"number",min:"0"}),
    async fields => {
      await api(`/api/manuscripts/${item.id}/proof-settings`,fields);
      return {message:"参数已保存。可在原文与版本中重跑检查。"};
    });
}
function rightsDialog(id) {
  const issue = data.issues.find(row=>row.id===id);
  const item = manuscript(issue.manuscript);
  formDialog("登记版权核验",rightsFields(issue.rights,item.versions),async fields => {
    await api(`/api/manuscripts/${item.id}/rights`,{...fields,id});
    return {route:itemHref(item.id,"copyright"),message:"版权核验已登记。"};
  }, {wide:true,help:"拟用与许可范围每行填写一组，例如「纸书 / 内页」。媒介可填纸书、电子书、有声书、网站或社交媒体，用途可填内页、封面或宣传。发现歧义时可登记转审。"});
}

function copyrightPage(item) {
  const rows = linkedIssues(item.id).filter(issue => issue.copyright);
  const editable = !finished(item) && can("review");
  const actions = `<div class="actions">${editable ? button("新增版权事项","copyright-add","plus") : ""}
    ${rows.some(needsCopyrightRequest) ? button("导出版权补件清单","copyright-export","download") : ""}</div>`;
  return `<section class="panel">${panelHead("逐项核验",actions,"记录原作品、拟用范围、许可依据和本次核验版本。")}
    ${rows.length ? `<div class="table-scroll"><table class="data-table"><thead><tr>
    <th scope="col">外来材料</th><th scope="col">原文定位</th><th scope="col">核验状态</th>
    <th scope="col">依据和历次处置</th></tr></thead><tbody>${rows.map(issue => {
      const version = item.versions.find(version => version.id === issue.version && !version.expired);
      return `<tr><td><button type="button" class="text-link row-title" data-action="issue" data-id="${issue.id}">
        ${esc(issue.title)}</button><span class="row-sub">${esc(issue.rights?.source || "")}</span></td>
        <td>${version && Number.isInteger(issue.anchor)
          ? `<a href="${sourceLink(item.id,version.id,issue.anchor)}">${esc(version.label)} · 第 ${issue.anchor+1} 段</a>`
          : esc(version?.label || "待定位")}</td>
        <td>${copyrightChip(issue.copyright)}${issue.copyright.missing?.length
          ? `<span class="row-sub">${esc(issue.copyright.missing.join("、"))}</span>` : ""}</td>
        <td>${issue.rights?.reviewer ? `<span class="row-sub">${esc(issue.rights.reviewer)} · ${dateLabel(issue.rights.reviewedAt,true)}</span>` : ""}
        ${editable ? button("登记核验","rights","pen","compact",`data-id="${issue.id}"`)
          : button("查看事项","issue","","compact",`data-id="${issue.id}"`)}</td></tr>`;
    }).join("")}</tbody></table></div>`
      : empty("尚无版权核验事项","上传稿件可提取来源线索。引文、译文和其他外来素材也可逐项登记。","shield")}</section>`;
}

function needsCopyrightRequest(issue) {
  return ["pending","gap","expired"].includes(issue.copyright?.status);
}

function exportCopyrightRequests(item) {
  const rows = linkedIssues(item.id).filter(needsCopyrightRequest);
  const lines = [item.title + " · 版权补件清单", item.id, ""];
  for (const [index,issue] of rows.entries()) {
    const version = item.versions.find(version => version.id === issue.version);
    const location = version && Number.isInteger(issue.anchor) ? `（${version.label} 第 ${issue.anchor+1} 段）` : "";
    const uses = issue.copyright.missing || issue.rights?.planned || [];
    const request = issue.rights?.request || "请提供原作品名称及出处、权利人和本次使用依据。";
    lines.push(`${index+1}. ${issue.title}${location}`, request);
    if (uses.length) lines.push("需确认的使用范围 · " + uses.join("、"));
    lines.push("");
  }
  downloadText(item.id+"-版权补件清单.txt",lines);
}
async function pdfPageDialog(itemId,versionId,index) {
  const model = await api(versionPath(itemId,versionId,"text"));
  const block = model.blocks[Number(index)];
  if (!block?.page) throw new Error("该处没有校样页定位。");
  const coords = block.bbox && block.pageWidth && block.pageHeight
    ? [block.bbox[0]/block.pageWidth,block.bbox[1]/block.pageHeight,
       (block.bbox[2]-block.bbox[0])/block.pageWidth,(block.bbox[3]-block.bbox[1])/block.pageHeight] : null;
  const box = coords ? `<div class="proof-highlight" style="left:${coords[0]*100}%;top:${coords[1]*100}%;width:${coords[2]*100}%;height:${coords[3]*100}%"></div>` : "";
  showDialog(`校样第 ${block.page} 页`,`<figure class="proof-page">
    <img alt="校样第 ${block.page} 页" src="${versionPath(itemId,versionId,"page")}?number=${block.page}">
    ${box}</figure>`,true);
  modal.querySelector("img").addEventListener("error",event=>{
    event.target.parentElement.insertAdjacentHTML("afterend",note("无法生成此页的预览。可下载原件核对。",true));
  },{once:true});
}
function crossrefDialog(doi="") {
  showDialog("Crossref DOI 核验",`<form data-crossref class="field-grid">
    ${input("doi","待查询的公开 DOI",doi,{required:true,full:true,limit:250})}
    ${input("listedTitle","稿件所列题名（可选）","",{full:true,limit:500})}
    ${input("listedAuthor","稿件所列首位作者（可选）")}
    ${input("listedYear","稿件所列年份（可选）","",{type:"number",min:"1000"})}
    <div class="field full"><small>查询只向 Crossref 提交 DOI。请与稿件所列题名、作者、年份和页码逐项比对。</small></div>
    <div class="actions"><button type="submit" class="button primary">查询 DOI</button></div>
    </form><div id="crossref-result"></div>`);
  const form = modal.querySelector("[data-crossref]");
  form.addEventListener("submit",async event=>{
    event.preventDefault();
    const submit = form.querySelector("button"), result = modal.querySelector("#crossref-result");
    const fields = Object.fromEntries(new FormData(form));
    submit.disabled = true;
    result.textContent = "正在查询…";
    try {
      const record = await api("/api/crossref",{doi:fields.doi});
      const messages = {noCoverage:"Crossref 未收录该 DOI，请另查来源。",throttled:"接口暂时限流，请稍后重试。",
        unavailable:"接口暂不可用，可继续人工回查。"};
      const normalize = value => String(value ?? "").normalize("NFKC").toLowerCase().replace(/[^\p{L}\p{N}]+/gu,"");
      const differences = [["题名",fields.listedTitle,record.title],["首位作者",fields.listedAuthor,record.authors?.[0]],
        ["年份",fields.listedYear,record.year]].filter(([,local,remote])=>local && normalize(local)!==normalize(remote));
      result.innerHTML = record.status === "found" ? `<div class="evidence-card"><strong>${esc(record.title)}</strong>
        <p>${esc(record.authors.join("、"))} · ${esc(record.journal)} · ${esc(record.year || "年份缺失")}</p>
        <small>${esc([record.doi,record.volume,record.issue,record.pages].filter(Boolean).join(" · "))}</small>
        ${differences.length ? note("需核对的字段 · "+differences.map(([name])=>name).join("、"),true) : ""}
        ${record.retracted ? note("发现撤回元数据，请核对正式公告 · "+record.notices.filter(Boolean).join(" / "),true) : ""}
        <div class="actions">${button("登记为回查来源","bibliography-crossref","pen","compact",`data-doi="${esc(record.doi)}"`)}</div></div>`
        : note(messages[record.status] || "回查需要人工处理。",true);
    } catch(error) {result.innerHTML=note(error.message,true);}
    finally {submit.disabled=false;}
  });
}
async function excerptDialog(item) {
  const result = await api("/api/excerpts?" + new URLSearchParams({project:item.id,q:item.assessment.question || item.title}));
  showDialog("稿件与对照材料摘录",excerptCards(result,item.id) || empty("暂无可摘录的段落"),true);
}
async function compareBookDialog(item,book) {
  const result = await api("/api/compare?" + new URLSearchParams({project:item.id,book}));
  showDialog(`授权全文比对 · ${result.title}`,note("以下片段供逐句核对。同一术语、定义、引文与作者前版需要结合上下文判断。")
    + comparisonHits(result,item.id),true);
}
function trialDialog() {
  formDialog("登记验收样本",trialFields(data.manuscripts),async fields=>{
    await api(`/api/manuscripts/${encodeURIComponent(fields.project)}/trial`,fields);
    return {message:"验收记录已保存。"};
  },{wide:true});
}
function safeBatch(issue) {
  return issue.state==="pending" && issue.severity==="low" && !issue.blocking && issue.group
    && (/^(term|unit|typo):/.test(issue.group) || ["punctuation","unit-spacing"].includes(issue.group));
}
function batchDialog() {
  const groups = new Map();
  for (const issue of pending().filter(safeBatch)){
    if (!groups.has(issue.group)) groups.set(issue.group,[]);
    groups.get(issue.group).push(issue);
  }
  const candidates = [...groups].filter(([,issues])=>issues.length>1);
  if (!candidates.length){
    showDialog("批量处置",empty("没有可批量处置的多条意见","范围为同规则、低优先级、非阻断的待判断意见。","tasks"));
    return;
  }
  const prefix = select("group","规则分组",candidates.map(([key,issues])=>[key,`${issues[0].title}（${issues.length}）`]),"",true)
    + '<fieldset class="field full"><legend>勾选要处置的意见</legend><div data-batch-items class="batch-items"></div></fieldset>';
  const form = formDialog("批量处置",prefix
    + select("state","处置结果",[["confirmed","确认问题，待处理"],["resolved","完成处置"],["dismissed","标记例外"]],"",true)
    + decisionBasisFields(),async(fields,form)=>{
      const ids = new FormData(form).getAll("ids");
      if (!ids.length) throw new Error("请勾选待处置项目。");
      const note = decisionNote(fields);
      if (!note) throw new Error("请选择常用处置依据或填写说明。");
      await api("/api/issues/decision",{ids,state:fields.state,note,global:true});
      return {message:`已处置 ${ids.length} 条受选意见。`};
    },{wide:true});
  const update = () => {
    form.querySelector("[data-batch-items]").innerHTML=groups.get(form.elements.group.value).slice(0,100).map(issue=>
      `<label class="field-check"><input type="checkbox" name="ids" value="${issue.id}" checked>${esc(manuscript(issue.manuscript).title)} · ${esc(issue.title)}</label>`).join("");
  };
  update();
  form.elements.group.addEventListener("change",update);
}
function libraryImport() {
  formDialog("批量导入知识资料",select("type","默认资料类型",Object.entries(kinds),"term")
    + input("projects","默认项目编号","*",{required:true,limit:400})
    + '<label class="field full">CSV 或 Excel 资料表<input type="file" name="file" accept=".csv,.xlsx" required></label>',
    async fields=>{
      const file=fields.file;
      if (!file.size || file.size>2*1024*1024) throw new Error("请选择 2 MB 以内的非空资料表。");
      const content=file.name.toLowerCase().endsWith(".xlsx") ? {xlsx:await fileBase64(file)} : {csv:await file.text()};
      const result=await api("/api/library/import",{...content,projects:fields.projects,type:fields.type});
      return {message:`已导入 ${result.count} 条待审定资料。`};
    },{help:"首行使用 title、source、projects、type、text 等字段，或中文列名。术语表还需 variant（变体）；专业条款需标识、版次、章条、页码和生效日。"});
}
async function checkTasks(versionId) {
  await refresh();
  const records = data.tasks.filter(task=>task.version===versionId).slice().reverse();
  const labels = {queued:"待执行",running:"执行中",failed:"检查失败",complete:"完成"};
  showDialog("版本检查记录",`<div>${records.map(task=>`<div class="evidence-card"><strong>${labels[task.status]}</strong>
    <p>${dateLabel(task.updated || task.created,true)}${task.status==="complete" ? " · 新增 " + task.count + " 项" : ""}</p></div>`).join("")}
    ${button("刷新状态","check-tasks","refresh","compact",`data-id="${versionId}"`)}</div>`);
}
function showLogin() {
  data = null;
  sidebar.innerHTML = "";
  document.querySelector("#topbar").innerHTML = "";
  document.querySelector("#footer").innerHTML = "";
  main.setAttribute("aria-busy","false");
  main.innerHTML = `<section class="panel login-panel">${panelHead("登录文衡编辑台")}
    <form id="login-form" class="panel-body field-grid">
    <label class="field full">账号<input name="username" autocomplete="username" maxlength="32" required></label>
    <label class="field full">口令<input name="password" type="password" autocomplete="current-password" required></label>
    <div class="form-error field full" role="alert" hidden></div><div class="actions"><button class="button primary" type="submit">登录</button></div>
    </form></section>`;
  if (modal.open) modal.close();
  main.querySelector("form").addEventListener("submit",async event=>{
    event.preventDefault();
    const form=event.currentTarget, submit=form.querySelector("button"), failure=form.querySelector(".form-error");
    submit.disabled=true;
    try {
      await api("/api/login",Object.fromEntries(new FormData(form)));
      await refresh();
    } catch(error) {failure.textContent=error.message;failure.hidden=false;}
    finally {submit.disabled=false;}
  });
}
async function accountDialog() {
  const canManage=can("accounts");
  const list=canManage ? await api("/api/accounts") : {accounts:[]};
  showDialog("工作区账号",`<p>${esc(reviewerName())} · ${esc(data.roles[data.viewer.role])}</p>
    ${list.accounts.map(account=>`<div class="evidence-card"><strong>${esc(account.name)}</strong>
    <small> · ${esc(account.id)} · ${esc(data.roles[account.role])}</small><p>项目授权 · ${esc(account.projects || "尚未分配")}</p>
    ${button("调整项目授权","account-edit","pen","compact",`data-id="${esc(account.id)}"`)}</div>`).join("")}
    <div class="dialog-actions">${canManage ? button("添加成员","account-create","plus") : ""}
    ${can("audit") ? button("操作审计","audit","list") : ""}${button("退出登录","logout","","danger")}</div>`);
}
function addAccountDialog() {
  formDialog("添加工作区成员",input("username","登录账号","",{required:true,limit:32})
    + input("name","显示姓名","",{required:true,limit:80}) + select("role","角色",Object.entries(data.roles),"editor")
    + input("projects","项目授权","",{placeholder:"项目编号或 *",limit:400})
    + '<label class="field full">初始口令<input type="password" name="password" autocomplete="new-password" minlength="10" required></label>',
    async fields=>{
      await api("/api/accounts",fields);
      return {message:"成员已建立。"};
    });
}
async function auditDialog() {
  const {rows}=await api("/api/audit");
  showDialog("操作审计",rows.map(row=>`<div class="evidence-card">
    <strong>${esc(row.operation)}</strong><small> · ${esc(row.actor)} · ${dateLabel(row.time,true)} · ${esc(row.objectId)}</small>
    <p>HTTP ${row.status} · ${row.duration} ms</p></div>`).join("") || empty("暂无审计记录"),true);
}
function libraryMaintenance() {
  const overdue=new Date(Date.now()-180*86400000).toLocaleDateString("sv-SE");
  const entries=data.library.filter(entry=>entry.restricted || entry.status==="approved" && (!entry.checked || entry.checked<overdue || entry.sourceUrl));
  const linkState = {ok:"可访问",broken:"链接已失效",redirect:"来源已跳转",unavailable:"当前不可用"};
  showDialog("资料巡检",entries.map(entry=>`<div class="evidence-card"><strong>${esc(entry.title)}</strong>
    <p>${entry.restricted ? "授权已到期" : !entry.checked || entry.checked<overdue ? "待复核" : "已核验 " + esc(entry.checked)}</p>
    ${entry.sourceUrl ? `<small>来源地址 · ${esc(entry.sourceUrl)} · ${linkState[entry.linkStatus] || "待检查"}</small>` : ""}
    ${entry.sourceUrl && !entry.restricted ? button("检查公开链接","source-check","refresh","compact",`data-id="${entry.id}"`) : ""}
    ${can("library") || can("approve") ? button("核验资料","library-review","check","compact",`data-id="${entry.id}"`) : ""}</div>`).join("")
    || empty("没有逾期核验或授权到期的资料","按最近核验日期和授权日期更新。","clock"));
}
function retentionDialog(item,id) {
  const version = getVersion(item,id);
  formDialog("登记版本保留期限",input("until","到期首日",version.retentionUntil || "",{type:"date",full:true}),
    async fields=>{
      await api(`/api/manuscripts/${item.id}/retention`,{version:id,...fields});
      return {message:"已更新版本保留期限。"};
    },{help:"到期后停止原文、文件和批注下载。清空可恢复无期限保留，已经执行了本机清除的版本除外。"});
}
async function citationDialog(item, preferredVersion, focusAnchor) {
  const versions = item.versions.filter(version=>version.type!=="补件" && !version.expired);
  if (!versions.length) {
    showDialog("正文与文末引文对照",empty("暂无可核对的稿件版本"));
    return;
  }
  showDialog("正文与文末引文对照",
    select("citationVersion","对照版本",versions.map(version=>[version.id,`${version.label} · ${version.name}`]),
      preferredVersion || versions.at(-1).id,true) + '<div data-citation-results aria-live="polite"></div>',true);
  const chooser = modal.querySelector('[name="citationVersion"]');
  const results = modal.querySelector("[data-citation-results]");
  const load = async () => {
    const version = chooser.value;
    results.textContent = "正在核对引文…";
    try {
      const response = await api("/api/citations?" + new URLSearchParams({project:item.id,version}));
      if (chooser.value !== version) return;
      const link = (anchor,label) => `<a href="${sourceLink(item.id,response.version,anchor)}" data-action="close-link">${esc(label)}</a>`;
      const rows = response.citations.slice().sort((a,b) =>
        Number(b.anchor === focusAnchor && preferredVersion === version) - Number(a.anchor === focusAnchor && preferredVersion === version));
      results.innerHTML = response.total ? `<p>识别到 ${response.referenceCount} 条文末编号条目，核对 ${response.total} 处正文引文${response.total>rows.length ? "，展示前 "+rows.length+" 处" : ""}。</p>`
        + rows.map(row=>`<div class="evidence-card">${link(row.anchor,"正文第 "+(row.anchor+1)+" 段")}
        <p>${esc(row.text)}</p>${row.matches.map(ref=>`<details><summary>[${ref.number}] ${ref.targetCount===0?"缺少条目":ref.targetCount>1?"编号重复":"已对齐"}</summary>
        ${ref.targets.map(target=>`<p>${link(target.anchor,target.page ? `参考文献 · 第 ${target.page} 页` : "文末第 "+(target.anchor+1)+" 段")} · ${esc(target.text)}</p>`).join("") || "<p>请核对文末条目或原件编号。</p>"}</details>`).join("")}</div>`).join("")
        : empty("未识别到方括号编号引文");
    } catch (error) {
      if (chooser.value === version) results.innerHTML = note(error.message,true);
    }
  };
  chooser.addEventListener("change",load);
  await load();
}
async function editAccountDialog(id) {
  const {accounts}=await api("/api/accounts");
  const account=accounts.find(entry=>entry.id===id);
  formDialog("调整项目授权",input("name","显示姓名",account.name,{required:true,full:true,limit:80})
    + select("role","角色",Object.entries(data.roles),account.role)
    + input("projects","项目授权",account.projects,{placeholder:"项目编号或 *",limit:400}),
    async fields=>{
      await api(`/api/accounts/${encodeURIComponent(id)}`,fields);
      return {message:"成员权限已更新。"};
    });
}
