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

export const rightsLabels = {rightsOwner:"权利人或图件出处",media:"许可媒介",scope:"许可范围",expiry:"授权截止日",
  credit:"署名",mapNumber:"审图号",note:"核验说明"};

export function rightsFields(record = {}) {
  return Object.entries(rightsLabels).map(([key,label]) => input(key,label,record[key],
    {type:key==="expiry"?"date":"text", required:key==="rightsOwner",limit:400})).join("");
}
export function rightsDisplay(record) {
  return Object.entries(rightsLabels).filter(([key])=>record[key]).map(([key,label])=>`<p>${label} · ${esc(record[key])}</p>`).join("");
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
