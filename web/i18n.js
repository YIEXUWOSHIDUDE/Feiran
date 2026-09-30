"use strict";

// Only explicitly marked interface messages are bound. Switching language updates those
// nodes in place: form values, selections, previews and in-flight requests are left alone.
window.WorkbenchI18n = (() => {
  const translations = window.WorkbenchChinese;
  const storageKey = "workbench.language";
  let language = /^zh\b/i.test(navigator.language) ? "zh" : "en";
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved === "zh" || saved === "en") language = saved;
  } catch { /* A blocked storage area must not prevent using the page. */ }
  const bindings = new WeakMap();

  class Message {
    constructor(render) { this.render = render; }
    toString() { return this.render(); }
  }

  function text(key, ...values) {
    if (Array.isArray(key)) key = key.reduce((result, part, i) => result + (i ? `{${i - 1}}` : "") + part, "");
    return new Message(() => {
      const template = language === "zh" && Object.hasOwn(translations, key) ? translations[key] : key;
      return String(template).replace(/\{(\d+)\}/g, (whole, index) => index < values.length ? String(values[index]) : whole);
    });
  }

  function join(parts, separator = "") {
    return new Message(() => parts.map(String).join(separator));
  }

  function date(iso, withTime = false) {
    return new Message(() => {
      const value = new Date(iso);
      return withTime ? value.toLocaleString(language === "zh" ? "zh-CN" : "en-US")
        : value.toLocaleDateString(language === "zh" ? "zh-CN" : "en-US");
    });
  }

  // Translate the server's known status sentences, without changing its stored records.
  // Unrecognized diagnostics remain verbatim so useful failure details are never hidden.
  function serverText(value) {
    if (Object.hasOwn(translations, value)) return text(value);
    let match = /^Reworded for this job: (\d+) line\(s\) changed(?:; (\d+) kept as confirmed because the rewording failed the fact check)?\.$/.exec(value);
    if (match) return text`Reworded for this job: ${match[1]} line(s) changed${match[2] ? text`; ${match[2]} kept as confirmed because the rewording failed the fact check` : ""}.`;
    match = /^Adjusted for this job: (\d+) change\(s\), listed below\.$/.exec(value);
    if (match) return text`Adjusted for this job: ${match[1]} change(s), listed below.`;
    match = /^(Not prepared|Not reworded|Not adjusted for this job)( again)?: (.*)$/.exec(value);
    if (match) return match[2] ? text`${text(match[1])} again: ${serverText(match[3])}`
      : text`${text(match[1])}: ${serverText(match[3])}`;
    for (const suffix of ["Your confirmed wording is used.", "Your usual layout is used.",
      "The earlier rewording below is kept.", "The earlier adjusted layout below is kept.",
      " since the last change; use the button below.", ": the CV could not be drafted.",
      " The CV below is from the last time it could be prepared."]) {
      if (value.endsWith(suffix)) return join([serverText(value.slice(0, -suffix.length).trimEnd()), text(suffix)], " ");
    }
    return value;
  }

  function node(value) {
    const result = document.createTextNode(String(value));
    if (value instanceof Message) bindings.set(result, new Map([["text", value]]));
    return result;
  }

  function setText(target, value) { target.replaceChildren(node(value)); }

  function attribute(target, name, value) {
    target.setAttribute(name, String(value));
    if (value instanceof Message) {
      if (!bindings.has(target)) bindings.set(target, new Map());
      bindings.get(target).set(name, value);
    } else {
      bindings.get(target)?.delete(name);
    }
  }

  function setLanguage(value) {
    if (value !== "zh" && value !== "en") return;
    language = value;
    try { localStorage.setItem(storageKey, value); } catch { /* Keep the choice for this visit. */ }
    document.documentElement.lang = value === "zh" ? "zh-CN" : "en";
    // Walk only live nodes; detached pages have no retained references in the WeakMap.
    const walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let target;
    while ((target = walker.nextNode())) {
      for (const [name, message] of bindings.get(target) || []) {
        if (name === "text") target.nodeValue = String(message);
        else target.setAttribute(name, String(message));
      }
    }
    const select = document.getElementById("interface-language");
    if (select) select.value = value;
  }

  function mount() {
    for (const target of document.querySelectorAll("[data-i18n]")) setText(target, text(target.dataset.i18n));
    const select = document.getElementById("interface-language");
    attribute(select, "aria-label", text("Interface language"));
    select.addEventListener("change", () => setLanguage(select.value));
    setLanguage(language);
  }

  return { text, join, date, serverText, node, setText, attribute, setLanguage, mount };
})();
