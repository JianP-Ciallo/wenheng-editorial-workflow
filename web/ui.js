import {icon} from "./icons.js";

const escapes = {"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"};
const esc = value => String(value ?? "").replace(/[&<>"']/g, key => escapes[key]);
const dateLabel = (value, withTime = false) => value
  ? new Date(value).toLocaleString("zh-CN", withTime ? {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false} : {year:"numeric", month:"2-digit", day:"2-digit"})
  : "—";
const nowDate = () => new Date().toLocaleDateString("sv-SE");
const chip = (label, tone = "", dot = false) => `<span class="chip ${tone}">${dot ? '<span class="dot"></span>' : ""}${esc(label)}</span>`;
const tile = (name, color = "", size = 18) => `<span class="tile ${color}">${icon(name, size)}</span>`;
const button = (label, action, name = "", classes = "", attrs = "") =>
  `<button type="button" class="button ${classes}" data-action="${action}" ${attrs}>${name ? icon(name, 16) : ""}${esc(label)}</button>`;
const linkButton = (label, href, name = "", classes = "") =>
  `<a class="button ${classes}" href="${esc(href)}">${name ? icon(name, 16) : ""}${esc(label)}</a>`;
const empty = (title, message = "", name = "inbox") =>
  `<div class="empty">${tile(name)}<strong>${esc(title)}</strong>${message ? `<small>${esc(message)}</small>` : ""}</div>`;
const note = (text, warning = false) =>
  `<div class="note${warning ? " warning" : ""}">${icon(warning ? "alert" : "info", 16)}<span>${esc(text)}</span></div>`;
const pageHead = (title, description, actions = "") =>
  `<div class="page-head"><div><h1>${esc(title)}</h1><p>${esc(description)}</p></div>${actions ? `<div class="head-actions">${actions}</div>` : ""}</div>`;
const panelHead = (title, actions = "", description = "") =>
  `<div class="panel-head"><div><h2>${esc(title)}</h2>${description ? `<p class="panel-note">${esc(description)}</p>` : ""}</div>${actions}</div>`;
const option = (value, label, selected) => `<option value="${esc(value)}"${String(value) === String(selected) ? " selected" : ""}>${esc(label)}</option>`;
const options = (values, selected) => values.map(value => Array.isArray(value)
  ? option(value[0], value[1], selected) : option(value, value, selected)).join("");
const input = (name, label, value = "", config = {}) => {
  const {type = "text", required = false, full = false, placeholder = "", limit = 200, min = "", step = ""} = config;
  return `<label class="field ${full ? "full" : ""}">${esc(label)}<input name="${name}" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder)}"${required ? " required" : ""}${type === "text" ? ` maxlength="${limit}"` : ""}${min ? ` min="${min}"` : ""}${step ? ` step="${step}"` : ""}></label>`;
};
const textarea = (name, label, value = "", required = false, limit = 1500) =>
  `<label class="field full">${esc(label)}<textarea name="${name}" maxlength="${limit}"${required ? " required" : ""}>${esc(value)}</textarea></label>`;
const select = (name, label, values, selected = "", full = false) =>
  `<label class="field ${full ? "full" : ""}">${esc(label)}<select name="${name}">${options(values, selected)}</select></label>`;

export {esc, dateLabel, nowDate, chip, tile, button, linkButton, empty, note, pageHead, panelHead, option, options, input, textarea, select};
