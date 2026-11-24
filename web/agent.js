import {button, chip, dateLabel, empty, esc, linkButton, note} from "./ui.js";
import {icon} from "./icons.js";
import {sourceLink} from "./tools.js";

const runStates = {
  queued: ["等待审校", "blue", "clock"],
  running: ["正在自动审校", "blue", "refresh"],
  failed: ["审校中断", "red", "alert"],
  stale: ["需要重新审校", "amber", "refresh"],
  ready_for_signoff: ["待终审签发", "green", "checkCircle"],
  escalation_required: ["需要编辑裁决", "amber", "alert"],
  approved: ["已批准进入印制", "green", "checkCircle"],
  returned: ["已退回作者补正", "amber", "refresh"],
  rejected: ["已登记终止", "red", "archive"]
};
const stepLabels = {pending:"未开始",running:"进行中",complete:"已完成"};
const checkLabels = {pass:["通过","green"],review:["待核验","amber"],not_applicable:["不适用",""],accepted:["已裁决例外","blue"]};
const severities = {high:["高优先级","red"],normal:["需核验","amber"],low:["一般","blue"]};
const stages = {
  structure:"结构解析",editorial:"文本与体例",citations:"引文",proofs:"图表与数据",
  copyright:"版权与地图",academic:"选题与学术",documents:"修订稿"
};
const isActive = run => ["queued","running"].includes(run?.status);
const isClosed = item => ["archived","rejected"].includes(item.status);
const versionForReview = item => item.versions.filter(version => version.type !== "补件" && !version.expired).at(-1);
const formatCount = value => (value || 0).toLocaleString("zh-CN");

export function rulingOptions(finding, caps) {
  if (finding.ruling) return [];
  return (finding.options || []).filter(option =>
    (option.id === "B" && caps.review) || (["A","C"].includes(option.id) && caps.flow));
}

function artifact(run, name, label) {
  return linkButton(label, `/api/agent/artifacts/${encodeURIComponent(run.id)}/${name}`, "download");
}

function detailHref(item, tab) {
  return `#manuscripts/${encodeURIComponent(item.id)}?tab=${tab}`;
}

function progressPanel(run) {
  const progress = Math.max(0, Math.min(100, Number(run.progress) || 0));
  const recorded = run.trace || [];
  return `<section class="agent-section agent-progress">
    <div class="agent-section-head"><h3>审校进度</h3><strong>${progress}%</strong></div>
    <progress value="${progress}" max="100" aria-label="审校进度"></progress>
    <ol class="agent-steps">${(run.steps || []).map(step =>
      `<li class="agent-step ${step.status === "complete" ? "done" : step.status === "running" ? "current" : ""}"
        ${step.status === "running" ? 'aria-current="step"' : ""}>
        <span>${icon(step.status === "complete" ? "check" : step.status === "running" ? "refresh" : "clock",16)}</span>
        <div><strong>${esc(step.label || stages[step.id])}</strong><small>${esc(stepLabels[step.status] || "待更新")}</small></div>
      </li>`).join("")}</ol>
    ${recorded.length ? `<details class="agent-trace"><summary>执行记录 <span>${recorded.length} 条</span></summary>
      <ul>${recorded.map(entry => `<li><time datetime="${esc(entry.time)}">${dateLabel(entry.time,true)}</time><span>${esc(entry.message)}</span></li>`).join("")}</ul>
    </details>` : ""}
  </section>`;
}

function reportPanel(item, run, brief, cases, state, caps) {
  const fixes = brief.fixes || [];
  const fixCount = fixes.reduce((count,fix) => count + (fix.count || 1),0);
  const citations = brief.citations || [];
  const noActions = run.stale || isClosed(item);
  const approval = brief.approval;
  const awaiting = cases.filter(finding => !finding.ruling).length;
  let actions = run.stale ? "" : artifact(run,"brief","下载审校白皮书");
  if (!run.stale && brief.hasDocx) {
    actions += artifact(run,"tracked","下载 Word 修订稿") + artifact(run,"clean","下载 Word 清稿");
  }
  if (!run.stale && citations.length) actions += artifact(run,"citations","下载引文核验记录");
  if (!run.stale && state === "approved") actions += artifact(run,"publish","下载印制批单");
  if (!run.stale && state === "returned") {
    actions += artifact(run,"supplement","下载作者补正函") + artifact(run,"mail","下载补正邮件草稿");
  }
  if (!run.stale && state === "rejected") actions += artifact(run,"termination","下载终止分析报告");
  if (cases.length && !run.stale) {
    const title = awaiting && state === "escalation_required" ? `处理案卷（${awaiting}）` : "查看案卷";
    actions += linkButton(title,detailHref(item,"docket"),"list",awaiting && !noActions ? "primary" : "");
  }
  if (!noActions && state === "ready_for_signoff" && caps.upload) {
    actions += button("重新审校","agent-trigger","refresh");
  }
  return `<section class="agent-section">
    <div class="agent-section-head"><h3>${["ready_for_signoff","approved"].includes(state) ? "签发白皮书" : "审校报告"}</h3></div>
    <dl class="agent-metrics">
      <div><dt>正文字符</dt><dd>${formatCount(brief.characters)}</dd></div>
      <div><dt>结构段落</dt><dd>${formatCount(brief.paragraphs)}</dd></div>
      <div><dt>自动修订</dt><dd>${formatCount(fixCount)}</dd></div>
      <div><dt>引文核验记录</dt><dd>${formatCount(citations.length)}</dd></div>
    </dl>
    <ul class="agent-checks">${(brief.checks || []).map(check => `<li>
      <strong>${esc(check.label || stages[check.id])}</strong>${chip(...(checkLabels[check.status] || checkLabels.review))}
      <p>${esc(check.detail)}</p></li>`).join("")}</ul>
    ${fixes.length ? `<details class="agent-trace"><summary>查看自动修订 <span>${fixCount} 处</span></summary>
      <ul class="agent-fixes">${fixes.map(fix => `<li><a href="${esc(sourceLink(item.id,run.version,fix.anchor))}">第 ${fix.anchor+1} 段</a>
        <span><del>${esc(fix.before)}</del> → <ins>${esc(fix.after)}</ins>${fix.reason ? `<small>${esc(fix.reason)}</small>` : ""}</span></li>`).join("")}
      </ul></details>` : ""}
    ${approval ? `<p class="agent-record">${esc(approval.actor)} · ${dateLabel(approval.time,true)}${approval.note ? ` · ${esc(approval.note)}` : ""}</p>` : ""}
    ${actions ? `<div class="actions agent-actions">${actions}</div>` : ""}
  </section>`;
}

function evidenceLink(value) {
  if (!value) return "";
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password
      ? `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">查看来源</a>` : "";
  } catch {
    return "";
  }
}

function caseCard(item, run, finding, caps, canDecide) {
  const location = [finding.location, finding.page ? `第 ${finding.page} 页` : "",
    Number.isInteger(finding.anchor) ? `第 ${finding.anchor+1} 段` : ""].filter(Boolean).join(" · ");
  const options = canDecide ? rulingOptions(finding,caps) : [];
  const ruling = finding.ruling;
  const optionLabels = {A:"退回作者补正",B:"采纳为例外",C:"登记终止"};
  return `<article class="agent-case">
    <header>
      <h4>${esc(finding.title)}</h4>
      <div class="agent-case-badges">${stages[finding.stage] ? chip(stages[finding.stage]) : ""}${chip(...(severities[finding.severity] || severities.normal))}</div>
    </header>
    <p class="agent-case-detail">${esc(finding.detail)}</p>
    <div class="agent-evidence-grid">
      <section class="agent-evidence manuscript"><h5>稿件原文</h5>
        ${location ? `<p class="agent-source-place">${esc(location)}</p>` : ""}
        ${finding.excerpt ? `<p><mark>${esc(finding.excerpt)}</mark></p>` : ""}
        ${Number.isInteger(finding.anchor) ? `<a href="${esc(sourceLink(item.id,run.version,finding.anchor))}">定位原文</a>`
          : !finding.excerpt ? "<p>案卷涉及整份稿件。</p>" : ""}
      </section>
      <section class="agent-evidence"><h5>核验依据</h5>
        ${(finding.evidence || []).length ? finding.evidence.map(entry => `<div class="agent-evidence-row">
          ${entry.label ? `<strong>${esc(entry.label)}</strong>` : ""}<p>${esc(entry.text)}</p>
          <div class="actions">${Number.isInteger(entry.anchor) ? `<a href="${esc(sourceLink(item.id,entry.version || run.version,entry.anchor))}">定位证据</a>` : ""}
          ${evidenceLink(entry.url)}</div></div>`).join("") : "<p>暂无可回查的核验依据。</p>"}
      </section>
    </div>
    ${ruling ? `<p class="agent-record"><strong>${esc(finding.options?.find(option => option.id === ruling.option)?.label || optionLabels[ruling.option])}</strong>
      · ${esc(ruling.actor)} · ${dateLabel(ruling.time,true)}${ruling.note ? ` · ${esc(ruling.note)}` : ""}</p>` : ""}
    ${options.length ? `<div class="actions agent-actions">${button("裁决此案","agent-rule","pen","",`data-id="${esc(finding.id)}" data-run="${esc(run.id)}"`)}</div>` : ""}
  </article>`;
}

export function agentView({item, run, brief, cases = [], caps, view}) {
  const version = item.versions.find(version => version.id === run?.version);
  const available = versionForReview(item);
  const canRun = caps.upload && available && !isClosed(item) && !isActive(run);
  const start = label => canRun ? button(label,"agent-trigger","refresh","primary") : "";
  if (!run) return `<section class="agent-section agent-start">
    ${empty(available ? "尚未运行智能审校" : "上传稿件开始审校",
    available ? "可对当前稿件版本启动智能审校。" : "原稿上传后会自动排队审校。","files")}
    <div class="actions">${available ? start("启动智能审校") :
      caps.upload && !isClosed(item) ? button("上传原稿","upload","upload","primary") : ""}</div>
  </section>`;
  if (run.stale && run.publishAvailable) {
    return `<section class="agent-section">
      <h3>已批准进入印制</h3>
      <div class="actions agent-actions">${artifact(run,"publish","下载印制批单")}</div>
    </section>`;
  }

  const complete = run.status === "complete";
  const state = run.stale ? "stale" : complete ? run.verdict || brief?.status || run.status : run.status;
  const [label,tone,glyph] = runStates[state] || runStates[run.status] || ["审校完成","blue","checkCircle"];
  const canDecide = state === "escalation_required" && !isClosed(item);
  const pending = cases.filter(finding => !finding.ruling).length;
  const context = state === "queued" ? "任务将在后台运行。" :
    state === "running" ? "此页会自动更新执行进度。" :
    state === "failed" ? run.error || "执行未完成，可重新审校。" :
    state === "stale" ? "稿件或核验材料已更新，请重新审校。" :
    state === "ready_for_signoff" ? (item.status === "active" ? "终审确认后生成印制批单。"
      : "当前稿件暂停或待补件，恢复流转后可以签发。") :
    state === "escalation_required" ? `${pending} 份案卷待处理。` : "";
  const approve = state === "ready_for_signoff" && item.status === "active" && caps.flow;
  const next = approve ? button("批准出版进入印制","agent-approve","check","primary",`data-id="${esc(run.id)}"`)
    : ["queued","running","approved","rejected"].includes(state) ? "" : start("重新审校");
  const header = `<header class="agent-lead ${tone}">
    <div class="agent-lead-icon">${icon(glyph,27)}</div>
    <div class="agent-lead-copy"><h2>${label}</h2>
      ${context ? `<p>${esc(context)}</p>` : ""}
      ${version ? `<small>${esc(version.label)} · ${esc(version.name)}</small>` : ""}</div>
    ${next}
  </header>`;
  if (view === "docket" && complete && brief && !run.stale) {
    return header + `<section class="agent-section" aria-labelledby="agent-docket-title">
      <div class="agent-section-head"><h3 id="agent-docket-title">案卷裁决</h3>${linkButton("返回审校报告",detailHref(item,"agent"),"file","compact")}</div>
      ${cases.length ? `<div class="agent-case-list">${cases.map(finding => caseCard(item,run,finding,caps,canDecide)).join("")}</div>`
        : empty("没有需要裁决的案卷")}
    </section>`;
  }
  return header + (complete && brief && !run.stale ? reportPanel(item,run,brief,cases,state,caps) : "") + progressPanel(run);
}
