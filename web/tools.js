import {button, chip, dateLabel, empty, esc, input, linkButton, nowDate, options, pageHead, select, textarea} from "./ui.js";

const number = value => new Intl.NumberFormat("zh-CN",{maximumFractionDigits:2}).format(value);
const format = (value, unit = "") => value == null ? "—" : number(value) + unit;
const ratio = value => value == null ? "—" : format(value * 100, "%");
const trialNumbers = [
  ["words", "稿件核验字数", "10000", "1"], ["volume", "核算期预计稿量", "300", "1"],
  ["truePositive", "准确命中数", "0", "0"], ["falsePositive", "误报数", "0", "0"],
  ["falseNegative", "漏报数", "0", "0"], ["traditionalMinutes", "传统流程工时（分钟）", "1200", "0"],
  ["assistedMinutes", "辅助流程工时（分钟）", "840", "0"], ["hourly", "综合工时成本（元/小时）", "120", "0"],
  ["runtimeCost","每稿新增经常费用（元）","180","0"], ["fixedCost","核算期固定投入（元）","54000","0"]
];
export const trialFields = manuscripts => select("project","关联稿件",manuscripts.map(item=>[item.id,item.title]),"",true)
  + input("date","记录日期",nowDate(),{type:"date",required:true})
  + select("mode","验收方式",["盲测","交叉处理","普通试点"])
  + trialNumbers.map(([key,label,value,min]) => input(key,label,value,{type:"number",min,required:true,
    step: ["hourly","runtimeCost","fixedCost","traditionalMinutes","assistedMinutes"].includes(key) ? "any" : "1"})).join("")
  + textarea("notes","抽样、审核和交叉处理说明","",false,500);

function trialValues(rows) {
  const sum = key => rows.reduce((total,row) => total + row[key],0);
  const trueCount = sum("truePositive"), falseCount = sum("falsePositive"), missCount = sum("falseNegative");
  const words = sum("words"), minutes = sum("traditionalMinutes") - sum("assistedMinutes");
  const contribution = rows.map(row => (row.traditionalMinutes-row.assistedMinutes)/60*row.hourly-row.runtimeCost);
  const net = rows.map((row,index) => contribution[index] - row.fixedCost/row.volume);
  return {
    precision: trueCount+falseCount ? trueCount/(trueCount+falseCount) : null,
    recall: trueCount+missCount ? trueCount/(trueCount+missCount) : null,
    missShare: trueCount+missCount ? missCount/(trueCount+missCount) : null,
    falseShare: trueCount+falseCount ? falseCount/(trueCount+falseCount) : null,
    savedPer10k: words ? minutes*10000/words : null,
    averageNet: net.length ? net.reduce((total,value)=>total+value,0)/net.length : null,
    net, contribution
  };
}
export function trialView(data,params,canEdit) {
  const {trials, manuscripts} = data;
  const filter = key => params.get(key) || "";
  const rows = trials.filter(row => (!filter("project") || row.manuscript===filter("project"))
    && (!filter("editor") || row.editor===filter("editor"))
    && (!filter("from") || row.date>=filter("from")) && (!filter("to") || row.date<=filter("to")));
  const projection = trialValues(rows);
  const metrics = [["验收样本",rows.length," 条"],["准确率",ratio(projection.precision)],["误报占比",ratio(projection.falseShare)],
    ["召回率",ratio(projection.recall)],["漏报率",ratio(projection.missShare)],
    ["每万字节省",format(projection.savedPer10k," 分钟")],["平均每稿净节省",format(projection.averageNet," 元")]];
  return pageHead("试点验收","按盲测和交叉处理记录核算命中、工时及成本。",
    canEdit && manuscripts.length ? button("登记验收样本","trial-create","plus","primary") : "")
    + `<section class="panel"><div class="toolbar">
    <label class="field">稿件<select data-filter="project">${options([["","全部稿件"],...manuscripts.map(item=>[item.id,item.title])],filter("project"))}</select></label>
    <label class="field">编辑<select data-filter="editor">${options([["","全部编辑"],...new Set(trials.map(row=>row.editor))],filter("editor"))}</select></label>
    <label class="field">从<input type="date" data-filter="from" value="${esc(filter("from"))}"></label>
    <label class="field">至<input type="date" data-filter="to" value="${esc(filter("to"))}"></label></div>
    <div class="metric-strip">${metrics.map(([title,value,unit])=>`<div><small>${title}</small><strong>${value}${unit || ""}</strong></div>`).join("")}</div>
    ${rows.length ? `<div class="table-scroll"><table class="data-table"><thead><tr>
    <th>稿件与日期</th><th>验收方式</th><th>编辑</th><th>命中 / 误报 / 漏报</th><th>工时†</th><th>净节省</th><th>盈亏平衡量</th>
    </tr></thead><tbody>${rows.map((row,index)=> {
      const breakEven = projection.contribution[index]>0 ? Math.ceil(row.fixedCost/projection.contribution[index]) : null;
      const item = manuscripts.find(item=>item.id===row.manuscript);
      return `<tr><td><a class="row-title" href="#manuscripts/${encodeURIComponent(row.manuscript)}">${esc(item.title)}</a><small>${row.date}</small></td>
      <td>${esc(row.mode)}</td><td>${esc(row.editor)}</td><td>${row.truePositive} / ${row.falsePositive} / ${row.falseNegative}</td>
      <td>${row.traditionalMinutes} / ${row.assistedMinutes} 分钟</td><td>${format(projection.net[index]," 元")}</td><td>${format(breakEven," 稿")}</td></tr>`;
    }).join("")}</tbody></table></div><div class="panel-footer">† 传统流程 / 辅助流程。净节省已分摊固定投入。</div>`
    : empty("暂无符合条件的验收记录","登记一份样本后自动计算指标。","list")}</section>`;
}

export function comparisonHits(result, manuscriptId) {
  const original = anchor => `#manuscripts/${encodeURIComponent(manuscriptId)}?tab=text&v=${encodeURIComponent(result.version)}&anchor=${anchor}`;
  if (!result.matches.length) return empty("未发现符合阈值的重合线索");
  return result.matches.map(hit => `<div class="evidence-card">${chip(hit.kind,"blue")} <a href="${original(hit.anchor)}" data-action="close-link">
    ${esc(hit.location)} · 第 ${hit.anchor+1} 段</a><small> / 对照材料 ${esc(hit.sourceLocation || "第 "+hit.target+" 段")}</small>
    <div class="diff-row"><div><strong>本稿</strong><br>${esc(hit.original)}</div><div><strong>对照材料</strong><br>${esc(hit.matched)}</div></div>
    ${hit.alignment.length ? `<details><summary>逐句对齐（${hit.alignment.length}）</summary>${hit.alignment.map(entry=>`<div class="diff-row"><div>${esc(entry.original)}</div><div>${esc(entry.matched)}</div></div>`).join("")}</details>` : ""}</div>`).join("");
}

export function sourceLink(id,version,anchor) {
  return `#manuscripts/${encodeURIComponent(id)}?tab=text&v=${encodeURIComponent(version)}&anchor=${anchor}`;
}
export function excerptCards(result,id) {
  return result.items.map(entry => `<div class="evidence-card"><a href="${sourceLink(id,result.version,entry.anchor)}" data-action="close-link">
    ${esc(entry.location)} · 第 ${entry.anchor+1} 段</a><p>${esc(entry.text)}</p></div>`).join("")
    + (result.comparisons || []).map(entry=>`<div class="evidence-card"><strong>${esc(entry.title)}</strong>
    <small> ${esc(entry.coverage)} · ${esc(entry.source)}</small><p>${esc(entry.difference)}</p></div>`).join("");
}

const rightsDecisions = {pending:"待补证",referral:"转专业复核",licensed:"按许可使用",
  quotation:"按适当引用保留",original:"核实为自有或可用素材",replaced:"已核实替换材料",removed:"已删除材料"};
const rightsLabels = {source:"原作品或替换素材出处",rightsOwner:"权利人",media:"原登记媒介",scope:"原登记范围",
  basis:"依据及具体位置",expiry:"授权截止日",credit:"署名",mapNumber:"审图号",referTo:"复核人或部门",
  request:"需作者补交",note:"核验说明"};
const rightsStates = {pending:["待补证","amber"],referral:["待专业复核","amber"],stale:["待核对最新版本","amber"],
  gap:["许可范围不足","red"],expired:["许可已到期","red"],unavailable:["关联资料已到保留期限","amber"],
  ready:["已核验","green"]};

export function copyrightChip(review) {
  return chip(...rightsStates[review.status]);
}

export function rightsFields(record = {}, versions = []) {
  const available = versions.filter(version => !version.expired);
  const reviewable = available.filter(version => ["原稿","返修稿","校样"].includes(version.type));
  const attachments = available.filter(version => version.type === "补件");
  const versionOptions = list => list.map(version => [version.id,`${version.label} · ${version.name}`]);
  return select("version","核验的稿件版本",[["","待确认"],...versionOptions(reviewable)],reviewable.at(-1)?.id)
    + select("decision","人工处置结论",Object.entries(rightsDecisions),record.decision || "pending")
    + input("source",rightsLabels.source,record.source,{full:true,limit:1200})
    + input("rightsOwner",rightsLabels.rightsOwner,record.rightsOwner,{limit:1200})
    + select("evidenceVersion","关联依据补件",[["","未关联"],...versionOptions(attachments)],record.evidenceVersion || "")
    + textarea("planned","实际拟用的媒介 / 用途",record.planned?.join("\n"),false,1200)
    + textarea("allowed","许可覆盖的媒介 / 用途",record.allowed?.join("\n"),false,1200)
    + select("term","授权期限",[["","待确认"],["dated","有截止日期"],["indefinite","依据明确无截止日期"]],record.term || (record.expiry ? "dated" : ""))
    + input("expiry","授权截止日（含当日）",record.expiry,{type:"date"})
    + textarea("basis","依据及具体位置",record.basis,false,1200)
    + input("credit","署名或注明出处要求",record.credit,{limit:1200})
    + input("mapNumber","审图号（适用时）",record.mapNumber,{limit:1200})
    + input("referTo",rightsLabels.referTo,record.referTo,{limit:1200})
    + textarea("request",rightsLabels.request,record.request,false,1200)
    + textarea("note",rightsLabels.note,record.note,false,1200);
}

export function rightsDisplay(record, versions) {
  const label = id => versions.find(version => version.id === id)?.label || id;
  const details = data => {
    const fields = {
      decision: rightsDecisions[data.decision],
      version: data.version ? label(data.version) : "",
      evidenceVersion: data.evidenceVersion ? label(data.evidenceVersion) : "",
      planned: data.planned?.join("、"),
      allowed: data.allowed?.join("、"),
      reviewer: data.reviewer && `${data.reviewer} · ${dateLabel(data.reviewedAt,true)}`,
    };
    return Object.entries({decision:"人工处置",version:"核验版本",evidenceVersion:"依据补件",planned:"拟用范围",
      allowed:"许可范围",reviewer:"核验人",...rightsLabels}).filter(([key])=>fields[key] || data[key])
      .map(([key,title])=>`<p>${title} · ${esc(fields[key] || data[key])}</p>`).join("");
  };
  return details(record) + (record.history?.length
    ? `<details><summary>历次处置（${record.history.length}）</summary>${record.history.map(entry =>
      `<div class="evidence-card">${details(entry)}</div>`).join("")}</details>` : "");
}

export function attachmentMeta(block) {
  const jump = [];
  if (block.page) jump.push("第 " + block.page + " 页");
  if (block.bbox) jump.push("坐标 " + block.bbox.map(value=>number(value)).join(", ") + " pt");
  if (block.ocr) jump.push("OCR 结果");
  if (block.latex) jump.push((block.formulaCheck ? "公式待校对 · " : "LaTeX · ") + block.latex);
  for (const image of block.images || []) {
    jump.push(`${image.width} × ${image.height} px · ${number(image.xPpi)} × ${number(image.yPpi)} PPI`);
    if (image.numericLabels?.length) jump.push("读数候选 " + image.numericLabels.map(label=>label.value).join(" / "));
  }
  for (const entry of block.calculations || []) {
    jump.push(`${entry.cell} · ${entry.formula} → ${entry.expected}（记录 ${entry.observed}）`);
  }
  if (block.notes?.length) jump.push("脚注 " + block.notes.join(", "));
  if (block.backLinks?.length) jump.push("回引段落 " + block.backLinks.map(index=>index+1).join(", "));
  if (block.numericLabels?.length) jump.push("图表读数候选 " + block.numericLabels.map(entry=>entry.value).join(" / "));
  return jump.map(line=>`<small class="reader-meta">${esc(line)}</small>`).join("");
}
