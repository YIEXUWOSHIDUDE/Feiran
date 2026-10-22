"use strict";

// Every request carries the per-start token; data is only ever inserted as text.
const I18n = window.WorkbenchI18n;
const t = I18n.text;

const TOKEN = document.querySelector('meta[name="workbench-token"]').content;
// "v2": signed-in users on a shared server; heavy work runs as tasks the page follows.
const MODE = (document.querySelector('meta[name="workbench-mode"]') || {}).content || "local";
const app = document.getElementById("app");
const message = document.getElementById("message");
const TERMINAL = new Set(["succeeded", "failed", "interrupted", "superseded", "cancelled"]);
let pendingWarning = null; // shown once the current action has finished, over its success message

// Page generations reject replies from a route that has already been left, including A→B→A.
let pageGeneration = 0;
function pageGuard() {
  const generation = pageGeneration, hash = location.hash;
  return () => generation === pageGeneration && hash === location.hash;
}

// Unsaved words live only in this tab's memory. Keep their original comparison value: a
// refresh may reveal a newer server line, but must never authorize overwriting it silently.
const editorDrafts = new Map();
const editorOpen = new Map();
function draftField(input, key, value, expected = value) {
  let draft = editorDrafts.get(key);
  if (!draft || !draft.dirty) draft = { value, expected, dirty: false };
  editorDrafts.set(key, draft);
  input.value = draft.value;
  input.setAttribute("data-draft-key", key);
  input.addEventListener("input", () => { draft.value = input.value; draft.dirty = true; });
  input.addEventListener("change", () => { draft.value = input.value; draft.dirty = true; });
  return {
    expected: () => draft.expected,
    saved: (submitted, nextExpected = submitted) => {
      if (draft.value === submitted || !draft.dirty) editorDrafts.delete(key);
      else draft.expected = nextExpected;
    },
  };
}
function draftDetails(key, attributes, ...children) {
  const node = el("details", attributes, ...children);
  if (editorOpen.has(key)) node.open = editorOpen.get(key);
  node.addEventListener("toggle", () => editorOpen.set(key, node.open));
  return node;
}
function editorFocus() {
  const input = document.activeElement;
  const key = input && input.getAttribute("data-draft-key");
  if (!key) return () => {};
  const start = input.selectionStart, end = input.selectionEnd;
  return () => {
    const next = [...app.querySelectorAll("[data-draft-key]")].find(node => node.getAttribute("data-draft-key") === key);
    if (next) {
      next.focus({ preventScroll: true });
      if (typeof start === "number") next.setSelectionRange(start, end);
    }
  };
}

// A request that names the user's action (an Idempotency-Key) can safely be sent again when its
// answer was lost: the server finds the same task instead of starting the work twice.
const RESEND_AFTER_MS = [1000, 3000];

async function request(path, options = {}) {
  const resendable = Boolean(options.headers && options.headers["Idempotency-Key"]);
  for (let attempt = 0; ; attempt += 1) {
    let response;
    try {
      response = await fetch(path, {
        ...options,
        headers: { "Content-Type": "application/json", ...options.headers, "X-Workbench-Token": TOKEN },
      });
    } catch (error) {
      if (!resendable || attempt >= RESEND_AFTER_MS.length) throw error;
      await new Promise((resolve) => setTimeout(resolve, RESEND_AFTER_MS[attempt]));
      continue;
    }
    if (resendable && attempt < RESEND_AFTER_MS.length && (response.status === 502 || response.status === 504)) {
      await new Promise((resolve) => setTimeout(resolve, RESEND_AFTER_MS[attempt]));
      continue;
    }
    return { response, body: await response.json().catch(() => ({})) };
  }
}

function actionKey() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function api(path, options = {}) {
  // V2: each call is one action of the user, named once; resending it (after agreeing to a data
  // flow below, or when the answer was lost) keeps the name, a new click gets a new one.
  if (MODE === "v2" && options.method && options.method !== "GET" && !(options.headers && options.headers["Idempotency-Key"])) {
    options = { ...options, headers: { ...options.headers, "Idempotency-Key": actionKey() } };
  }
  const { response, body } = await request(path, options);
  if (MODE === "v2" && response.status === 401) {
    location.href = `/login?return_to=${encodeURIComponent("/" + location.hash)}`;
    throw new Error(t("Your session ended. Sign in again."));
  }
  if (MODE === "v2" && response.status === 403 && body.code === "consent_required") {
    if (await askConsent(body.stage)) return api(path, options);
    throw new Error(t("Nothing was sent. You can agree whenever you want to use this."));
  }
  if (!response.ok) {
    throw new Error(body.error || body.detail || `HTTP ${response.status}`);
  }
  if (body.task_error) pendingWarning = t`Saved. The CV could not be prepared again now: ${I18n.serverText(body.task_error.error)}`;
  if (body.task) return { ...body, ...(await followTask(body.task)) };
  return body;
}

// Waits for a task the server queued (V2): the page keeps working, and a reload does not start it again.
async function followTask(task) {
  let current = task;
  let polls = 0;
  while (!TERMINAL.has(current.status)) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const { response, body } = await request(`/api/tasks/${encodeURIComponent(task.task_id)}`);
    if (response.status === 401) return api(`/api/tasks/${encodeURIComponent(task.task_id)}`);
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    current = body.task;
    if (++polls % 5 === 0) void refreshUsage();
  }
  await refreshUsage();
  if (current.status !== "succeeded") throw new Error(taskMessage(current));
  return current.result || {};
}

function taskMessage(task) {
  const status = { failed: t("It did not work"), interrupted: t("The service restarted while it ran"),
    superseded: t("A newer version was made meanwhile"), cancelled: t("It was cancelled") }[task.status] || task.status;
  return task.message ? I18n.join([status, ": ", I18n.serverText(task.message)]) : status;
}

// What a model feature sends and to whom, shown before its first use; nothing is sent until the user agrees.
async function askConsent(stage) {
  const { notices } = await api("/api/me");
  const notice = notices[stage];
  if (!notice) return false;
  return new Promise((resolve) => {
    const finish = (agreed) => { box.remove(); resolve(agreed); };
    const agree = el("button", {}, t("I agree"));
    agree.addEventListener("click", async () => {
      agree.disabled = true;
      try {
        await api("/api/consent", { method: "POST", body: JSON.stringify({ stage, version: notice.version }) });
        finish(true);
      } catch (error) {
        show(I18n.serverText(error.message));
        finish(false);
      }
    });
    const box = el("section", { class: "panel consent-panel", role: "dialog", "aria-modal": "true" },
      el("h2", {}, t("Before this is sent to a model")), el("p", {}, I18n.serverText(notice.text)),
      el("div", { class: "toolbar" }, agree, el("button", { class: "secondary", onclick: () => finish(false) }, t("Not now"))));
    app.prepend(box);
    agree.focus();
  });
}

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "onclick") node.addEventListener("click", value);
    else if (value === true) node.setAttribute(name, "");
    else if (value !== false && value !== null && value !== undefined) I18n.attribute(node, name, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : I18n.node(child));
  }
  return node;
}

function show(text, kind = "error") {
  I18n.setText(message, text);
  message.className = `message ${kind}`;
  message.hidden = false;
}

function clearMessage() {
  message.hidden = true;
}

let flash = null; // shown once after the next page change

async function run(action) {
  const current = pageGuard();
  clearMessage();
  try {
    await action();
  } catch (error) {
    if (current()) show(I18n.serverText(error.message));
  } finally {
    await refreshUsage();
  }
  if (pendingWarning) {
    if (current()) show(pendingWarning, "warning");
    pendingWarning = null;
  }
}

function foundEntryHead(entry) {
  const left = [entry.title, entry.subtitle].filter(Boolean).join(" — ");
  const right = [entry.location, entry.dates].filter(Boolean).join(" · ");
  if (!left && !right) return "";
  return el("div", {},
    el("strong", {}, left),
    entry.title_link ? el("span", { class: "muted" }, ` | ${entry.title_link.label}`) : "",
    right ? el("span", { class: "muted" }, `${left ? " · " : ""}${right}`) : "");
}

// What DeepSeek found in an uploaded CV, and the name and contact details read locally for the
// user to check. Nothing is stored until Save.
function uploadReview(proposal, onSaved, onCancel) {
  const found = proposal.private;
  const text = (value) => el("input", { type: "text", value: value || "" });
  const inputs = { name: text(found.name), location: text(found.location), phone: text(found.phone), email: text(found.email) };
  const linkRows = el("div", { class: "link-rows" });
  const addLink = (link = { label: "", url: "" }) => {
    const label = el("input", { type: "text", value: link.label, placeholder: t("Label, e.g. GitHub") });
    const url = el("input", { type: "url", value: link.url, placeholder: "https://" });
    const row = el("div", { class: "link-row" }, label, url);
    row.append(el("button", { class: "secondary", type: "button", onclick: () => row.remove() }, t("Remove")));
    row.link = () => ({ label: label.value.trim(), url: url.value.trim() });
    linkRows.append(row);
  };
  found.links.forEach(addLink);
  const facts = proposal.sections.flatMap((section) => section.entries.flatMap((entry) => entry.facts));
  const known = facts.filter((fact) => fact.known).length;
  const layout = proposal.sections.map((section) => el("div", { class: "found-section" },
    el("h4", {}, section.title || section.kind),
    section.entries.map((entry) => el("div", { class: "found-entry" },
      foundEntryHead(entry),
      entry.facts.length ? el("ul", {}, entry.facts.map((fact) => el("li", {},
        fact.text, fact.known ? el("span", { class: "muted" }, t(" · already in your facts")) : ""))) : ""))));
  const save = el("button", {}, proposal.language === "zh" ? t("Save Chinese CV") : t("Save English CV"));
  save.addEventListener("click", () => run(async () => {
    const body = Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value.trim()]));
    body.links = [...linkRows.children].map((row) => row.link()).filter((link) => link.url);
    const result = await api(`/api/cv/uploads/${proposal.upload_id}/save`, { method: "POST", body: JSON.stringify(body) });
    await onSaved(result);
  }));
  return el("div", { class: "upload-review" },
    el("h3", {}, t("1. Check your name and contact details")),
    el("p", { class: "muted" }, MODE === "v2"
      ? t("Read from the top of your CV on the Feiran server and never sent to DeepSeek. They go on the header of every CV.")
      : t("Read from the top of your CV on this computer and never sent to DeepSeek. They go on the header of every CV.")),
    el("div", { class: "grid" }, field(t("Name *"), inputs.name), field(t("Location"), inputs.location), field(t("Phone"), inputs.phone), field(t("Email"), inputs.email)),
    el("div", { class: "field" }, el("span", {}, t("Links")), linkRows,
      el("div", {}, el("button", { class: "secondary", type: "button", onclick: () => addLink() }, t("Add link")))),
    found.other.length ? el("p", { class: "muted" }, t("Also at the top of your CV, not used: "), found.other.join(" · ")) : "",
    el("h3", {}, t`2. What was found: ${facts.length} lines`),
    el("p", { class: "muted" },
      t("Extracted from your PDF. Save to use these lines for job matching and CV preparation"),
      known ? t`; ${known} already match facts you have.` : "."),
    layout,
    proposal.not_imported.length
      ? el("details", {}, el("summary", {}, t`${proposal.not_imported.length} line(s) not imported`),
        el("ul", {}, proposal.not_imported.map((line) => el("li", {}, line))))
      : "",
    proposal.has_profile ? el("p", { class: "warning" }, t("Saving updates only this CV language. The other language stays available; earlier versions are kept in history.")) : "",
    el("div", { class: "toolbar" }, save, el("button", { class: "secondary", type: "button", onclick: onCancel }, t("Cancel"))));
}

function uploadPanel(languages) {
  const language = el("select", { "aria-label": t("CV upload language") },
    el("option", { value: "zh" }, t("Chinese CV (A4)")),
    el("option", { value: "en" }, t("English CV (US Letter)")));
  const input = el("input", { type: "file", accept: "application/pdf,.pdf" });
  const box = el("div", {});
  const reset = () => { box.replaceChildren(); input.value = ""; };
  const saved = async (result) => {
    await renderFacts();
    if (!result.imported) show(t`Saved your CV. All ${result.reused} lines match facts you already had.`, "ok");
    else show(t`Saved your CV. ${result.imported} new line(s) are ready to use${result.reused ? t`; ${result.reused} matched facts you already had.` : "."}`, "ok");
  };
  input.addEventListener("change", () => run(async () => {
    const file = input.files[0];
    if (!file) return;
    box.replaceChildren(el("p", { class: "muted" }, t`Reading ${file.name}…`));
    try {
      let proposal = await api(`/api/cv/upload?language=${encodeURIComponent(language.value)}`, { method: "POST", body: file, headers: { "Content-Type": "application/pdf" } });
      // V2 reads the CV in a task; its result names the proposal waiting to be checked.
      if (MODE === "v2") proposal = await api(`/api/cv/uploads/${encodeURIComponent(proposal.upload_id)}`);
      const cancel = () => run(async () => {
        await api(`/api/cv/uploads/${proposal.upload_id}`, { method: "DELETE" });
        reset();
      });
      box.replaceChildren(uploadReview(proposal, saved, cancel));
    } catch (error) {
      reset();
      throw error;
    }
  }));
  return el("section", { class: "panel cv-upload-panel" },
    el("h2", {}, t("Your CV")),
    el("p", { class: "muted" },
      t("Upload your CV as a PDF: saved content and its layout become the starting point for each job's CV. "),
      MODE === "v2"
        ? t("Your name, email, phone and links stay on the Feiran server; DeepSeek sees the other lines to tell sections, entries and bullets apart.")
        : t("Your name, email, phone and links stay on this computer; DeepSeek sees the other lines to tell sections, entries and bullets apart.")),
    el("p", { class: "muted" }, languages.length ? I18n.join([t("Available CVs: "),
      I18n.join(languages.map((lang) => t(lang === "zh" ? "Chinese CV (A4)" : "English CV (US Letter)")), " / ")], "") : t("Upload a Chinese or English PDF to begin.")),
    el("div", { class: "toolbar" }, field(t("CV upload language"), language),
      el("label", { class: "file-pick" }, el("span", {}, t("Choose a PDF")), input)),
    box);
}

function factEditor(fact) {
  const text = el("textarea", { class: "fact-edit-text", "aria-label": t("Profile text") }, fact.text);
  const tags = el("input", { type: "text", value: fact.tags.join(", "), "aria-label": t("Skill tags") });
  const details = el("details", { class: "fact-editor" },
    el("summary", {}, t("Edit this line")),
    el("p", { class: "muted" }, t("Saving creates a new usable version. Earlier text stays in history; affected CVs need review again.")),
    field(t("Profile text"), text), field(t("Skill tags"), tags));
  const save = actionButton(t("Save changes"), async () => {
    await api(`/api/facts/${encodeURIComponent(fact.id)}/edit`, { method: "POST", body: JSON.stringify({
      expected_version: fact.version, text: text.value.trim(), tags: tags.value.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean),
    }) });
    await renderFacts();
    show(t("Saved. This content is ready to use."), "ok");
  });
  const cancel = el("button", { class: "secondary", onclick: () => {
    text.value = fact.text; tags.value = fact.tags.join(", "); details.open = false;
  } }, t("Cancel"));
  details.append(el("div", { class: "toolbar" }, save, cancel));
  return details;
}

async function renderFacts() {
  const current = pageGuard();
  const [{ facts, interrupted }, { languages }] = await Promise.all([api("/api/facts"), api("/api/cv/languages")]);
  if (!current()) return;
  const rows = facts.map((fact) => el("tr", { class: "fact-row" },
    el("td", {}, el("code", {}, fact.id), el("div", { class: "muted" }, t`v${fact.version} · ${t(fact.fact_type)}`)),
    el("td", {}, fact.text, el("div", { class: "tags" }, fact.tags.map((tag) => el("span", { class: "tag" }, tag))), factEditor(fact)),
  ));
  // The DOM writes a null child out as the text "null", and there is no account panel outside V2.
  app.replaceChildren(...[
    uploadPanel(languages),
    el("section", { class: "panel facts-panel" },
      el("h2", {}, t("Personal profile")),
      (interrupted || []).map((notice) => unfinishedNote(notice, renderFacts)),
      el("p", { class: "muted" }, t`${facts.length} saved lines, available for matching and CV preparation.`),
      facts.length === 0
        ? el("p", {}, t("No profile content yet. Upload your CV above to begin."))
        : el("div", {},
          el("a", { href: "#find", class: "button" }, t("Find jobs")),
          el("table", {},
            el("thead", {}, el("tr", {}, el("th", {}, t("Profile entry")), el("th", {}, t("Text and tags")))),
            el("tbody", {}, rows))),
    ),
    accountPanel(),
  ].filter(Boolean));
}

function localDate(iso) {
  return iso ? I18n.date(iso) : "";
}

function field(label, input, hint) {
  return el("label", { class: "field" }, el("span", {}, label), input, hint ? el("small", { class: "muted" }, hint) : null);
}

async function renderJobs() {
  const current = pageGuard();
  const { jobs } = await api("/api/jobs");
  if (!current()) return;
  const postingURL = el("input", { type: "url", required: true, placeholder: t("Paste a job posting URL") });
  const importLabel = MODE === "v2" ? t("Read and review") : t("Read and generate");
  const importURL = el("button", { type: "submit" }, importLabel);
  const urlForm = el("form", {},
    field(t("Job posting URL"), postingURL, t("Enter any public job posting URL. If the site blocks reading or the description is incomplete, paste it manually below.")),
    el("div", { class: "toolbar" }, importURL));
  urlForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (importURL.disabled) return;
    run(async () => {
      importURL.disabled = true;
      I18n.setText(importURL, MODE === "v2" ? t("Reading the job…") : t("Reading and preparing…"));
      try {
        const { job_id, existing } = await api("/api/jobs/from-url", {
          method: "POST", body: JSON.stringify({ url: postingURL.value.trim(), ...(MODE === "v2" ? { review_only: true } : {}) }),
        });
        if (existing) flash = [t("This job is already saved. Opened its existing version."), "ok"];
        location.hash = `#job/${job_id}`;
      } finally {
        importURL.disabled = false;
        I18n.setText(importURL, importLabel);
      }
    });
  });
  const inputs = {
    title: el("input", { type: "text", required: true, placeholder: t("Software Engineer Intern") }),
    company: el("input", { type: "text", placeholder: t("Company") }),
    url: el("input", { type: "url", placeholder: t("https://… (official posting)") }),
    location: el("input", { type: "text", placeholder: t("Location") }),
    text: el("textarea", { required: true, placeholder: t("Paste the full job description here") }),
  };
  const create = el("button", {}, MODE === "v2" ? t("Save and review job") : t("Create job"));
  create.addEventListener("click", () => run(async () => {
    const body = Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value.trim() || null]));
    if (MODE === "v2") body.review_only = true;
    const { job_id } = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
    location.hash = `#job/${job_id}`;
  }));
  const list = jobs.length === 0
    ? el("p", { class: "muted" }, t("No jobs yet."))
    : el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, t("Job")), el("th", {}, t("Captured")), el("th", {}, t("Progress")))),
      el("tbody", {}, jobs.map((job) => el("tr", {},
        el("td", {}, el("a", { href: `#job/${job.job_id}` }, job.title || job.job_id), el("div", { class: "muted" }, job.company || "")),
        el("td", { class: "muted" }, localDate(job.captured_at)),
        el("td", {}, progress(job.steps)),
      ))));
  app.replaceChildren(
    el("section", { class: "panel jobs-panel" }, el("h2", {}, t("Jobs")), list),
    el("section", { class: "panel new-job-panel" },
      el("h2", {}, t("New job")),
      el("p", { class: "muted" }, MODE === "v2" ? t("Save a job, check its sponsorship wording and your experience, then decide whether to prepare a CV.") : t("Paste a job link to read its description and prepare a CV draft for review.")),
      urlForm,
      el("details", {}, el("summary", {}, t("Paste the description manually")),
      el("p", { class: "muted" }, MODE === "v2" ? t("Paste the full job description to review it first. A CV is prepared only when you choose to continue.") : t("Paste a job description from any site. Its requirements are found and your CV is prepared for it automatically.")),
      el("div", { class: "grid" }, field(t("Title *"), inputs.title), field(t("Company"), inputs.company), field(t("Official link"), inputs.url, t("Leave empty if unknown; the source is then recorded as unknown.")), field(t("Location"), inputs.location)),
      field(t("Job description *"), inputs.text),
      el("div", { class: "toolbar" }, create)),
    ),
  );
}

function ago(iso) {
  if (!iso) return t("never");
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return t("just now");
  if (minutes < 60) return t`${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return t`${hours} h ago`;
  return t`${Math.round(hours / 24)} days ago`;
}

// Per-browser conveniences only; the page works the same when storage is blocked.
function recall(key, fallback) {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : JSON.parse(value);
  } catch {
    return fallback;
  }
}

function remember(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage blocked: the choice lasts until the page reloads.
  }
}

// Filters survive page changes; a download started on one visit is awaited by the next.
const find = { title: "", location: "", region: recall("find.region", "all"), sponsorship: recall("find.sponsorship", "all"), limit: 50, hideSenior: recall("find.hideSenior", false), refreshing: null };

function findStatus(text) {
  const node = document.getElementById("find-status");
  if (node) I18n.setText(node, text);
}

// Reads companies again a few at a time; one failing company does not stop the others.
async function refreshSources(sources, onProgress) {
  const queue = [...sources];
  const failures = [];
  let done = 0;
  const worker = async () => {
    while (queue.length) {
      const source = queue.shift();
      try {
        await api(`/api/sources/${source.provider}/${encodeURIComponent(source.board)}/refresh`, { method: "POST" });
      } catch (error) {
        failures.push(error.message);
      }
      onProgress(++done, sources.length);
    }
  };
  await Promise.all(Array.from({ length: Math.min(4, sources.length) }, worker));
  return failures;
}

function locationsText(postings) {
  const names = postings.map((posting) => posting.location || t("location unknown"));
  return names.length > 3 ? t`${I18n.join(names.slice(0, 3), "; ")} +${names.length - 3} more` : I18n.join(names, "; ");
}

const SPONSORSHIP_LABELS = {
  supported: t("JD says sponsorship available"),
  not_supported: t("JD says no sponsorship"),
  conditional: t("Sponsorship has conditions"),
  unknown: t("Not stated · verify sponsorship"),
  conflicting: t("Conflicting sponsorship statements"),
  not_assessed: t("Wording not checked yet"),
};

// Source data is untrusted: only web URLs become links, and quoted evidence stays text.
function sourceReference(source) {
  try {
    const url = new URL(source);
    if (url.protocol === "https:" || url.protocol === "http:") {
      return el("a", { href: url.href, target: "_blank", rel: "noopener noreferrer" }, source);
    }
  } catch { /* Unknown or non-web sources remain plain text. */ }
  return source || t("Source: unknown");
}

// `check` (a button) is offered with a saved job whose wording has no answer under the current rules.
function sponsorshipSummary(sponsorship, compact = false, check = null) {
  const data = sponsorship || {};
  // A status this page does not know is never shown as "unknown": that would say the rules found nothing.
  const status = Object.hasOwn(SPONSORSHIP_LABELS, data.status) ? data.status : "not_assessed";
  const evidence = Array.isArray(data.evidence) ? data.evidence : [];
  const label = status === "unknown"
    ? (evidence.length ? t("Policy unclear in JD") : t("No sponsorship wording in JD"))
    : SPONSORSHIP_LABELS[status];
  return el("div", { class: `sponsorship ${compact ? "compact" : ""}` },
    el("span", { class: `sponsor-badge sponsor-${status}` }, t("Visa sponsorship"), ": ", label),
    status === "not_assessed" ? check : null,
    el("details", { class: "sponsor-evidence" },
      el("summary", {}, t("Check the wording and source")),
      status === "not_assessed"
        ? el("p", { class: "muted" }, t("The sponsor wording of this saved description has not been checked with the current rules yet, so nothing is known either way."))
        : evidence.length
          ? evidence.map((quote) => el("blockquote", {}, quote.text))
          : el("p", { class: "muted" }, t("No explicit sponsorship statement was recognized in this description. This does not mean sponsorship is unavailable.")),
      el("p", { class: "muted" }, t("This reads the saved job description, not the employer's sponsorship history. Check the full wording and confirm with the employer.")),
      el("p", { class: "source-reference" }, sourceReference(data.source)),
      el("p", { class: "muted" }, data.captured_at ? t`Description captured ${localDate(data.captured_at)}` : t("Capture date unknown"),
        " · ", t("Capture date is not the posting date; current availability is unverified.")),
      data.assessed_at && status !== "not_assessed"
        ? el("p", { class: "muted" }, t`Wording checked ${localDate(data.assessed_at)} with sponsor rules version ${data.rule_version}`)
        : null));
}

function listingRow(item, rank) {
  const count = new Set(item.matched.map((tag) => tag.toLowerCase())).size;
  // Each location is its own posting: the details, links and action shown always belong to the
  // chosen one. A location already saved as a job is chosen first, so Open opens what is shown.
  const saved = Math.max(0, item.postings.findIndex((posting) => posting.started_job));
  let choose = null;
  if (item.postings.length > 1) {
    choose = el("select", { title: t("Location"), "aria-label": t("Location") }, item.postings.map((posting, index) =>
      el("option", { value: String(index), selected: index === saved }, posting.location || t("location unknown"))));
  }
  const selectedPosting = () => item.postings[choose ? Number(choose.value) : 0];
  const description = el("td", { class: "listing-description" });
  const action = el("span", { class: "listing-action" });
  const draw = () => {
    const posting = selectedPosting();
    const title = el("div", { class: "listing-title" });
    const source = sourceReference(posting.source);
    if (source instanceof Node) {
      I18n.setText(source, item.title);
      title.append(source);
    } else title.append(I18n.node(item.title));
    description.replaceChildren(title,
      el("div", { class: "muted" }, I18n.join([item.company, posting.location || t("location unknown"), posting.posted_at ? t`posted ${localDate(posting.posted_at)}` : null].filter(Boolean), " · ")),
      sponsorshipSummary(posting.sponsorship || { ...item.sponsorship, source: posting.source, captured_at: posting.last_seen_at }, true));
    action.replaceChildren(posting.started_job
      ? el("a", { class: "button", href: `#job/${posting.started_job}` }, t("Open"))
      : actionButton(MODE === "v2" ? t("Review job") : t("Start"), async () => {
        const { job_id } = await api("/api/listings/start", {
          method: "POST",
          body: JSON.stringify({ provider: item.provider, board: item.board, job_id: posting.job_id, ...(MODE === "v2" ? { review_only: true } : {}) }),
        });
        location.hash = `#job/${job_id}`;
      }));
  };
  if (choose) choose.addEventListener("change", draw);
  draw();
  return el("tr", {},
    el("td", { class: "muted listing-rank" }, String(rank)),
    description,
    el("td", { class: "listing-skills" }, el("span", { class: "mobile-label" }, t("Your skills it mentions")), el("strong", {}, String(count)), el("div", { class: "tags" }, item.matched.map((tag) => el("span", { class: "tag" }, tag)))),
    el("td", { class: "choice" }, el("div", { class: "start" }, choose, action)),
  );
}

async function renderFind() {
  const current = pageGuard();
  let listRequest = 0;
  let sourcesRequest = null;
  const readSources = () => {
    if (!sourcesRequest) sourcesRequest = api("/api/sources").catch(error => {
      sourcesRequest = null;
      throw error;
    });
    return sourcesRequest;
  };
  const status = el("p", { class: "muted", id: "find-status" });
  const listBox = el("div", {}, el("p", { class: "muted" }, t("Loading…")));
  const companiesBox = el("div");
  const inputs = {
    title: el("input", { type: "text", placeholder: t("Title contains all words, e.g. software intern"), value: find.title }),
    location: el("input", { type: "text", placeholder: t("Location contains, e.g. New York or Remote"), value: find.location }),
  };
  const region = el("select", { class: "region-select", "aria-label": t("Job region") },
    [["all", "All regions"], ["cn", "Mainland China"], ["other", "Other regions"], ["unknown", "Unknown region"]]
      .map(([value, label]) => el("option", { value, selected: value === find.region }, t(label))));
  region.addEventListener("change", () => run(async () => {
    find.region = region.value;
    remember("find.region", find.region);
    find.limit = 50;
    await drawList();
  }));
  const sponsorship = el("select", { class: "sponsorship-select", "aria-label": t("Visa sponsorship filter") },
    el("option", { value: "all", selected: find.sponsorship === "all" }, t("All sponsorship statuses (including unknown)")),
    Object.entries(SPONSORSHIP_LABELS).map(([value, label]) => el("option", { value, selected: find.sponsorship === value }, label)));
  sponsorship.addEventListener("change", () => run(async () => {
    find.sponsorship = sponsorship.value;
    remember("find.sponsorship", find.sponsorship);
    find.limit = 50;
    await drawList();
  }));
  const apply = () => run(async () => {
    find.title = inputs.title.value.trim();
    find.location = inputs.location.value.trim();
    find.limit = 50;
    await drawList();
  });
  for (const input of Object.values(inputs)) {
    input.addEventListener("keydown", (event) => { if (event.key === "Enter") apply(); });
  }
  const hideSenior = el("input", { type: "checkbox", checked: find.hideSenior });
  hideSenior.addEventListener("change", () => run(async () => {
    find.hideSenior = hideSenior.checked;
    remember("find.hideSenior", find.hideSenior);
    find.limit = 50;
    await drawList();
  }));
  const filterButton = el("button", { class: "secondary" }, t("Filter"));
  filterButton.addEventListener("click", apply);
  const refreshAll = actionButton(t("Refresh all"), async () => {
    const { sources } = await readSources();
    await refreshNow(sources);
  }, true);

  async function drawList() {
    const ticket = ++listRequest;
    const params = new URLSearchParams({
      title: find.title, location: find.location, region: find.region, sponsorship: find.sponsorship, limit: String(find.limit), hide_senior: String(find.hideSenior),
    });
    let result;
    try { result = await Promise.all([api(`/api/listings?${params}`), readSources()]); }
    catch (error) { if (current() && ticket === listRequest) throw error; else return; }
    if (!current() || ticket !== listRequest) return;
    const [data, { sources }] = result;
    const newest = sources.map((source) => source.fetched_at).filter(Boolean).sort().pop();
    if (!find.refreshing) I18n.setText(status, t`${data.total} roles from ${sources.length} companies · updated ${ago(newest)}`);
    const notes = [];
    if (data.skill_count === 0) {
      notes.push(el("p", { class: "warning" }, t("No skill tags in your profile yet. "),
        el("a", { href: "#facts" }, t("Edit your profile."))));
    }
    let body;
    if (sources.length === 0) body = el("p", {}, t("You are not following any company. Add one under Companies below."));
    else if (data.total === 0) body = el("p", { class: "muted" }, find.refreshing ? t("Downloading jobs…") : t("No jobs match these filters."));
    else {
      body = el("table", { class: "listings" },
        el("thead", {}, el("tr", {}, el("th", {}, "#"), el("th", {}, t("Job")), el("th", {}, t("Your skills it mentions")), el("th", {}, ""))),
        el("tbody", {}, data.listings.map((item, index) => listingRow(item, index + 1))));
    }
    const more = data.total > data.listings.length
      ? el("div", { class: "toolbar" }, actionButton(t`Show more (${data.total - data.listings.length} left)`, async () => {
        find.limit = Math.min(find.limit + 50, 200);
        await drawList();
      }, true))
      : null;
    const footer = data.listings.length >= 200 && more
      ? el("p", { class: "muted" }, t("Showing the top 200. Use the filters to narrow the list."))
      : more;
    listBox.replaceChildren(...notes, body, ...(footer ? [footer] : []));
  }

  async function drawCompanies() {
    const { sources } = await readSources();
    if (!current()) return;
    const link = el("input", { type: "text", placeholder: t("Paste a job board link, e.g. https://jobs.lever.co/palantir") });
    const add = actionButton(t("Add company"), async () => {
      const { added } = await api("/api/sources", { method: "POST", body: JSON.stringify({ link: link.value }) });
      show(t`Following ${added.company}: ${added.open} open jobs.`, "ok");
      sourcesRequest = null;
      await Promise.all([drawCompanies(), drawList()]);
    });
    const rows = sources.map((source) => {
      const remove = el("button", { class: "secondary" }, t("Remove"));
      remove.addEventListener("click", () => run(async () => {
        if (!window.confirm(t`Stop following ${source.company}? Its downloaded jobs are removed from this list.`)) return;
        await api(`/api/sources/${source.provider}/${encodeURIComponent(source.board)}`, { method: "DELETE" });
        sourcesRequest = null;
        await Promise.all([drawCompanies(), drawList()]);
      }));
      return el("tr", {},
        el("td", {}, source.company, el("div", { class: "muted" }, `${source.provider} · ${source.board}`)),
        el("td", {}, String(source.open_count)),
        el("td", {}, ago(source.fetched_at), source.error ? el("div", { class: "bad-text" }, t`Last try failed: ${source.error}`) : null),
        el("td", { class: "choice" }, remove));
    });
    companiesBox.replaceChildren(el("details", { open: sources.length === 0 },
      el("summary", {}, t`Companies you follow (${sources.length})`),
      el("p", { class: "muted" }, t("Jobs come from each company's public job board (Greenhouse, Lever or Ashby). No login, and nothing about you is sent.")),
      el("p", { class: "muted" }, t("Tencent's public careers board is also supported: "), "https://careers.tencent.com/zh-cn/search.html"),
      el("div", { class: "toolbar" }, link, add),
      rows.length ? el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, t("Company")), el("th", {}, t("Open jobs")), el("th", {}, t("Updated")), el("th", {}, ""))),
        el("tbody", {}, rows)) : null));
  }

  async function refreshNow(sources) {
    if (!find.refreshing && sources.length) {
      find.refreshing = refreshSources(sources, (done, total) => {
        findStatus(t`Downloading jobs… ${done} of ${total} companies`);
      }).finally(() => { find.refreshing = null; });
    }
    if (!find.refreshing) return;
    const failures = await find.refreshing;
    sourcesRequest = null;
    if (!listBox.isConnected) return;
    findStatus(t("Ranking…"));
    if (failures.length) show(t`${failures.length} company(ies) could not be updated. Details are under Companies.`);
    await Promise.all([drawList(), drawCompanies()]);
  }

  app.replaceChildren(
    el("section", { class: "panel find-panel" },
      el("h2", {}, t("Jobs that match your skills")),
      el("p", { class: "muted" },
        t("Most matched first: how many of your saved skills each posting mentions. "),
        t("It counts words; it is not a fit score or your chance of an offer.")),
      el("div", { class: "toolbar" }, region, sponsorship, inputs.title, inputs.location, filterButton,
        el("label", { class: "check", title: t("Hides roles titled Senior, Staff, Principal, Lead, Manager, Director… (\"Member of Technical Staff\" does not count) and roles whose JD explicitly requires 5+ years of experience. Roles whose level is unclear stay visible.") }, hideSenior, t(" Hide senior roles")),
        refreshAll),
      el("p", { class: "muted sponsor-filter-note" }, t("Unknown sponsorship stays visible by default. These labels describe the JD; they do not establish your eligibility.")),
      status,
      listBox),
    el("section", { class: "panel companies-panel" }, companiesBox),
  );
  await Promise.all([drawList(), drawCompanies()]);
  const { sources } = await readSources();
  if (current()) await refreshNow(sources.filter((source) => source.stale));
}

const STEP_LABELS = [["decided", t("Requirements")], ["cv-approved-en", t("CV (EN)")], ["cv-approved-zh", t("CV (ZH)")]];

function progress(steps) {
  return el("span", {}, STEP_LABELS.map(([step, label]) =>
    el("span", { class: `pill ${steps.includes(step) ? "done" : ""}` }, label)));
}

const STRENGTH_LABELS = { required: t("Required"), preferred: t("Preferred (nice to have)"), unclear: t("Not marked required or preferred") };

function strengthLabel(strength) {
  return STRENGTH_LABELS[strength] || STRENGTH_LABELS.unclear;
}

// Who decided a line counts: the page by itself, or the user.
function decidedLabel(item) {
  if (item.extraction_method === "manual-quote-v1") return t("added by you");
  if (item.decided_by === "user") return t("reviewed by you");
  if (item.decided_by === "auto") return t("counted automatically, not reviewed");
  return "";
}

function requirementsPanel(view, refresh) {
  const choiceKey = `requirements:${view.job_id}`;
  const stored = editorDrafts.get(choiceKey);
  const choices = stored ? new Map(stored.value) : new Map();
  const expectedVersion = stored ? stored.expected : view.requirements_version;
  const decide = (id, status) => {
    choices.set(id, status);
    editorDrafts.set(choiceKey, { value: [...choices], expected: expectedVersion, dirty: true });
  };
  const rows = (view.candidates || []).map((item) => {
    const status = choices.get(item.id) || item.status;
    const confirm = el("input", { type: "radio", name: item.id, checked: status === "confirmed" });
    const exclude = el("input", { type: "radio", name: item.id, checked: status === "excluded" });
    if (status === "confirmed" || status === "excluded") choices.set(item.id, status);
    confirm.addEventListener("change", () => decide(item.id, "confirmed"));
    exclude.addEventListener("change", () => decide(item.id, "excluded"));
    return el("tr", {},
      el("td", {}, item.text, el("div", { class: "muted" }, I18n.join([strengthLabel(item.strength), item.section, decidedLabel(item)].filter(Boolean), " · "))),
      el("td", { class: "choice" }, el("label", {}, confirm, t(" Requirement"))),
      el("td", { class: "choice" }, el("label", {}, exclude, t(" Not a requirement"))),
    );
  });
  const missed = el("input", { type: "text", placeholder: t("Copy a line or phrase exactly from the job description") });
  const missedDraft = draftField(missed, `missed:${view.job_id}`, "", view.requirements_version);
  const add = actionButton(t("Add missed requirement"), async () => {
    const text = missed.value;
    await api(`/api/jobs/${view.job_id}/requirements/add`, { method: "POST", body: JSON.stringify({ text, expected_version: missedDraft.expected() }) });
    missedDraft.saved(text);
    await refresh();
    show(t("Added. It counts, and the CV was prepared again."), "ok");
  }, true);
  const save = actionButton(t("Save"), async () => {
    const submitted = JSON.stringify([...choices]);
    const confirm = [...choices].filter(([, status]) => status === "confirmed").map(([id]) => id);
    const exclude = [...choices].filter(([, status]) => status === "excluded").map(([id]) => id);
    await api(`/api/jobs/${view.job_id}/requirements/decide`, { method: "POST", body: JSON.stringify({ confirm, exclude, expected_version: expectedVersion }) });
    if (JSON.stringify(editorDrafts.get(choiceKey)?.value) === submitted) editorDrafts.delete(choiceKey);
    await refresh();
    show(t`Saved: ${confirm.length} requirement(s). The CV was prepared again for them.`, "ok");
  });
  const findAgain = actionButton(t("Find again with DeepSeek"), async () => {
    await api(`/api/jobs/${view.job_id}/requirements/find`, { method: "POST", body: JSON.stringify({ expected_version: view.requirements_version }) });
    await refresh();
  }, true);
  const extraction = view.extraction || {};
  const foundBy = extraction.method === "deepseek-lines-v1"
    ? t("Found by DeepSeek, which picks lines of the job description; each line is copied word for word.")
    : t`Found by heading rules${extraction.fallback_reason ? t` (DeepSeek not used: ${I18n.serverText(extraction.message || extraction.fallback_reason)})` : ""}.`;
  const body = [
    view.steps.some((name) => name.startsWith("cv-")) ? el("p", { class: "warning" }, t("Saving changes here prepares the CV again; an approved CV is moved to the job's history folder.")) : null,
    rows.length ? el("table", {}, el("tbody", {}, rows)) : el("p", {}, t("No requirement lines were found. Try Find again with DeepSeek, or add lines below by copying exact text from the job description.")),
    el("div", { class: "toolbar" }, missed, add),
    el("div", { class: "toolbar" }, save, findAgain),
  ];
  const counted = (view.selected_requirements || []).length;
  const automatic = (view.selected_requirements || []).filter((item) => item.decided_by === "auto").length;
  const summary = t`${counted} requirement(s)${automatic ? t`, ${automatic} counted automatically and not reviewed by you` : ""} — open to review or change`;
  // Once requirements count, the CV is what matters; the list folds away until needed.
  return el("section", { class: "panel step-panel requirements-panel" },
    el("h2", {}, t("1. What this job asks for")),
    el("p", { class: "muted" }, foundBy, t(" All of them count, and your CV below is adjusted to them. If a line is not really a requirement, choose Not a requirement and Save; saving also marks the list as reviewed by you.")),
    counted ? draftDetails(choiceKey, {}, el("summary", {}, summary), body) : body,
  );
}

function actionButton(label, action, secondary = false) {
  const button = el("button", { class: secondary ? "secondary" : "" }, label);
  button.addEventListener("click", () => run(async () => {
    button.disabled = true;
    I18n.setText(button, t("Working…"));
    try {
      await action();
    } finally {
      button.disabled = false;
      I18n.setText(button, label);
    }
  }));
  return button;
}

const CV_TITLES = { en: t("English CV (US Letter)"), zh: t("Chinese CV (A4)") };
const CHANGE_ICONS = { order: "↕", cut: "✕", reword: "✎" };

function changeRow(view, language, change, refresh, editable) {
  const toggle = editable
    ? actionButton(change.undone ? t("Redo") : t("Undo"), async () => {
      await api(`/api/jobs/${view.job_id}/cv/${language}/change`, {
        method: "POST", body: JSON.stringify({ change_id: change.id, undone: !change.undone }),
      });
      await refresh();
    }, true)
    : null;
  return el("tr", { class: change.undone ? "undone" : "" },
    el("td", { class: "icon" }, CHANGE_ICONS[change.type] || "•"),
    el("td", {}, change.label,
      change.reason ? el("div", { class: "muted" }, change.reason) : null,
      change.undone ? el("div", { class: "muted" }, t("Undone: your usual CV is shown for this part.")) : null),
    el("td", { class: "choice" }, toggle));
}

const STAGE_MARKS = { done: "✓", fallback: "!", failed: "✕", skipped: "–" };

// One editor keeps its request key after an uncertain response. A changed payload is a new
// action; failed requests leave its text in place. This also works in the local file store.
function cvEditAction(view, language, action, body, refresh, saved = () => {}) {
  let previous, key;
  return async () => {
    const payload = JSON.stringify(typeof body === "function" ? body() : body);
    if (payload !== previous) { previous = payload; key = actionKey(); }
    await api(`/api/jobs/${view.job_id}/cv/${language}/${action}`, {
      method: "POST", headers: { "Idempotency-Key": key }, body: payload,
    });
    saved(JSON.parse(payload));
    await refresh();
  };
}

function localEditRow(view, language, edit, refresh) {
  const buttons = [];
  if (edit.can_undo || edit.can_redo) buttons.push(actionButton(edit.can_redo ? t("Redo") : t("Undo"),
    cvEditAction(view, language, `edits/${edit.id}`, { undone: !edit.undone }, refresh), true));
  if (edit.can_save) buttons.push(actionButton(t("Save to my profile"),
    cvEditAction(view, language, `lines/${edit.block}/save`, { expected_text: edit.text }, refresh), true));
  return el("div", { class: edit.undone ? "local-edit undone" : "local-edit" },
    el("p", {}, edit.where ? `${edit.where}: ` : "", edit.text),
    el("p", { class: "muted" }, !edit.placed ? t("Kept in history; its original place is no longer on this CV.")
      : edit.undone ? t("Undone in this CV.") : !edit.latest ? t("An earlier version of this line.")
      : edit.saved ? t("Saved to your profile for reuse.") : t("Saved in this CV only.")),
    el("div", { class: "toolbar" }, buttons));
}

function cvLineEditor(view, language, cv, refresh) {
  const prefix = `cv:${view.job_id}:${language}:`;
  const visible = new Set((cv.lines || []).map(line => prefix + line.block));
  const recover = [...editorDrafts].filter(([key, draft]) => key.startsWith(prefix) && draft.dirty && (cv.stale || !visible.has(key)))
    .map(([key, draft]) => {
      const input = el("textarea", { rows: 3, "aria-label": t("Unsaved text") });
      draftField(input, key, draft.value, draft.expected);
      return el("div", {}, el("p", { class: "warning" }, t("The original line is unavailable. Copy your unsaved text before discarding it.")), input);
    });
  if (cv.stale && !recover.length) return null;
  const lines = (cv.stale ? [] : cv.lines || []).map((line) => {
    const input = el("textarea", { rows: 3, "aria-label": t("CV line") });
    const draft = draftField(input, `cv:${view.job_id}:${language}:${line.block}`, line.text);
    return el("div", { class: "write-own" }, field(line.where || t("CV line"), input),
      actionButton(t("Save in this CV"), cvEditAction(view, language, `lines/${line.block}`,
        () => ({ text: input.value, expected_text: draft.expected() }), refresh, ({ text }) => draft.saved(text)), true),
      actionButton(t("Discard unsaved changes"), async () => {
        editorDrafts.delete(prefix + line.block);
        await refresh();
      }, true));
  });
  return draftDetails(`cv:${view.job_id}:${language}`, { class: "cv-line-editor" }, el("summary", {}, t("Edit this CV")),
    el("p", { class: "muted" }, t("Edits stay in this job's CV. Save a line to your profile separately to reuse it. Changed CVs need approval again.")),
    el("p", { class: "muted" }, t("Unsaved words stay while you navigate within this tab. Reloading or closing the tab clears them. After a conflict, copy them or discard them to load the latest line.")),
    lines, recover);
}

// How the latest preparation went, stage by stage, so a usable CV is never mistaken for a
// tailored one.
function stageList(stages) {
  if (!stages || !stages.length) return null;
  return el("ul", { class: "stages" }, stages.map((stage) => el("li", { class: `stage ${stage.status}` },
    el("span", { class: "stage-mark" }, STAGE_MARKS[stage.status] || "·"), " ", I18n.serverText(stage.message))));
}

function cvBlock(view, language, refresh) {
  const cv = view.cv[language];
  const title = CV_TITLES[language];
  // A failed retry still records why; refresh either way so the page shows it.
  const post = (action, body) => async () => {
    try {
      await api(`/api/jobs/${view.job_id}/cv/${language}/${action}`,
        body ? { method: "POST", body: JSON.stringify(body) } : { method: "POST" });
    } finally {
      await refresh();
    }
  };
  if (!cv.head) {
    return el("div", { class: "cv-block" }, el("h3", {}, title),
      cv.stale ? el("p", { class: "warning" }, t("This CV profile changed. Prepare it again and review the new version.")) : stageList(cv.stages) || el("p", { class: "muted" }, t("Not prepared yet.")),
      el("div", { class: "toolbar" }, actionButton(cv.stages ? t("Try again") : t("Prepare this CV"), post("prepare"))));
  }
  const approved = cv.head === "approved" && !cv.stale;
  const tools = [];
  if (!cv.stale && cv.head === "draft") tools.push(actionButton(t("Reword with DeepSeek"), post("tailor"), true));
  if (!cv.stale && (cv.head === "draft" || cv.head === "tailored")) tools.push(actionButton(t("Adjust for this job"), post("plan")));
  if (!cv.stale && cv.head === "planned") tools.push(actionButton(t("Adjust again"), post("plan"), true));
  tools.push(actionButton(t("Start over"), post("prepare"), true));
  if (approved && !cv.final_pdf) tools.push(actionButton(t("Create final PDF"), post("export")));
  if (cv.final_pdf) {
    tools.push(el("a", { class: "button", href: `/download/${view.job_id}/${language}.pdf${tokenQuery("?")}` }, t("Download final PDF")));
  }
  const notes = [];
  if (cv.stale) notes.push(el("p", { class: "warning" }, t("Your profile or facts changed. This is an old draft. Start over and review the new version.")));
  if (approved) notes.push(el("p", { class: "ok-text" }, t`Approved ${I18n.date(cv.approved_at, true)}. Editing this CV requires approval again.`));
  if (cv.stages) notes.push(stageList(cv.stages));
  else if (cv.head === "draft") notes.push(el("p", { class: "warning" }, t("This is your usual CV: DeepSeek could not reword or adjust it yet. Use the buttons above.")));
  else if (cv.head === "tailored") notes.push(el("p", { class: "warning" }, t("Reworded, but not adjusted for this job yet: DeepSeek could not plan it. Use Adjust for this job.")));
  if ((cv.language_fallbacks || []).length) {
    const missing = language === "zh" ? t("Chinese") : t("English");
    notes.push(el("p", { class: "warning" },
      t`Shown in the other language because your profile has no ${missing} text for — `,
      cv.language_fallbacks.join("; ")));
  }
  const rewrites = (cv.rewrites || []).map((line) => ({
    id: `reword:${line.fact_id}`, type: "reword", undone: line.undone, reason: null,
    label: el("span", {}, el("span", { class: "muted" }, line.from), " → ", line.to),
  }));
  const changes = [...(cv.changes || []), ...rewrites];
  const editable = cv.head === "planned" && !cv.stale;
  if (changes.length) {
    notes.push(el("p", {}, el("strong", {}, t`${changes.length} change(s) from your usual CV`),
      editable ? t(" — undo any you disagree with:") : ":"));
    notes.push(el("table", { class: "changes" }, el("tbody", {}, changes.map((change) => changeRow(view, language, change, refresh, editable)))));
  } else if (cv.head === "planned") {
    notes.push(el("p", { class: "muted" }, t("DeepSeek kept your usual CV unchanged for this job.")));
  }
  if ((cv.rejected || []).length) {
    notes.push(el("details", {}, el("summary", {}, t`${cv.rejected.length} earlier rewording(s) were not applied; previous wording is retained`),
      el("table", {}, el("tbody", {}, cv.rejected.map((line) => el("tr", {},
        el("td", {}, line.rejected_text), el("td", { class: "bad-text" }, line.reasons.join("; "))))))));
  }
  let approval = null;
  if (!approved && !cv.stale) {
    // Only the CV shown below can be approved: if another tab changed it since, the server
    // refuses and the page shows the current one to read again.
    const approve = actionButton(t("Approve this CV"), post("approve", { expected_content_sha256: cv.content_sha256 }));
    approve.disabled = true;
    const read = el("input", { type: "checkbox" });
    read.addEventListener("change", () => { approve.disabled = !read.checked; });
    approval = el("div", { class: "approval" },
      el("label", {}, read, t(" I read the CV below and every change listed above.")), approve);
  }
  const preview = el("iframe", {
    class: "preview", sandbox: "", title: t`${title} preview`,
    src: `/preview/${view.job_id}/${language}?v=${encodeURIComponent(cv.content_sha256)}${tokenQuery("&")}`,
  });
  const edits = (cv.edits || []).length ? el("details", { open: true },
    el("summary", {}, t("Changes saved in this CV")),
    cv.edits.map((edit) => localEditRow(view, language, edit, refresh))) : null;
  return el("div", { class: "cv-block" }, el("h3", {}, title), el("div", { class: "toolbar" }, tools), notes,
    cvLineEditor(view, language, cv, refresh), edits, approval, preview);
}

function cvPanel(view, refresh) {
  const main = view.language || "en";
  const other = main === "en" ? "zh" : "en";
  const language = el("select", { "aria-label": t("CV language for this job") },
    (view.cv_languages || []).map((lang) => el("option", { value: lang, selected: lang === main }, CV_TITLES[lang])));
  language.addEventListener("change", () => run(async () => {
    await api(`/api/jobs/${view.job_id}/language`, { method: "POST", body: JSON.stringify({ language: language.value }) });
    await refresh();
  }));
  return el("section", { class: "panel step-panel cv-panel" },
    el("h2", {}, t("2. Your CV for this job")),
    el("p", { class: "muted" },
      t("Prepared for this job using your profile: relevant content first, with suggested additions "),
      t("and wording that follows the job's requirements. You choose which changes to adopt. "),
      t("DeepSeek sees only your CV lines and this job's requirements, never your name, contact details, schools or employers.")),
    field(t("CV language for this job"), language),
    cvBlock(view, main, refresh),
    // A second language is offered only when the resume itself is written in it.
    (view.cv_languages || []).includes(other)
      ? el("details", { class: "other-language", open: Boolean(view.cv[other].head) },
        el("summary", {}, t`${CV_TITLES[other]} for this job`), cvBlock(view, other, refresh))
      : null,
  );
}

// Which check a gap action is about, as this page shows it: the CV language and that check's
// version. The server refuses an action on a check the page no longer shows (another tab
// switched the language, changed the requirements or checked again) instead of applying it to
// whatever is newest, which could be the other language's CV.
function shownCheck(view) {
  const gaps = view.gaps && !view.gaps.outdated ? view.gaps : null;
  return gaps ? { language: gaps.language, gaps_version: gaps.version } : { language: view.language };
}

function suggestionText(suggestion) {
  if (suggestion.kind === "skill") {
    return el("span", {}, t("Add to your skills line "), el("span", { class: "muted" }, `“${suggestion.where}”`), ": ",
      el("strong", {}, suggestion.items.join(", ")));
  }
  const beside = suggestion.beside || [];
  return el("span", {}, t`New line under ${suggestion.where}: `, el("strong", {}, `“${suggestion.text}”`),
    beside.length ? el("span", { class: "beside muted" }, el("br"), t("Already there: "),
      beside.flatMap((line, index) => [index ? " · " : "", `“${line}”`])) : null);
}

// A suggestion to offer as it is: complete wording, not an empty or template answer.
function completeSuggestion(suggestion) {
  return Boolean(suggestion && (suggestion.text || suggestion.items?.length));
}

// Where a suggestion goes and its words, as the line editor sends them. Adopting sends what the
// page shows, so a tab left open never adopts a newer suggestion it has not shown.
function suggestedLine(suggestion) {
  return suggestion.kind === "skill"
    ? { place: `skill:${suggestion.fact_id}`, text: suggestion.items.join(", ") }
    : { place: `entry:${suggestion.entry_key}`, text: suggestion.text };
}

// What the CV shows for each requirement, most useful to act on first. Worked out on the
// server from the CV as it is shown now, so undoing a cut changes it at once.
const COVERAGE_GROUPS = [
  ["recheck", t("Changed since this check")],
  ["not_shown", t("Your profile includes it, but this CV leaves it out")],
  ["related", t("Related only")],
  ["none", t("Not covered in your profile")],
  ["unchecked", t("Not checked")],
];
const COVERAGE_COUNTS = { shown: t("shown"), recheck: t("to check again"), not_shown: t("left out"), related: t("related only"), none: t("no evidence"), unchecked: t("not checked") };
const LEFT_OUT = {
  cut: t("cut for this job"),
  reworded: t("reworded in a way that no longer shows it"),
  not_on_cv: t("in your profile, but not on your CV"),
  changed: t("as it read when last checked"),
};
const WAY_BACK = { cut: t("Put it back"), reworded: t("Use your own wording") };
// What the last check found, beside a requirement to check again.
const LAST_CHECK = { supported: t("shown"), related: t("related only"), none: t("no evidence") };

// Undoes every change that hides the quoted words: a cut, and a rewording when the line would
// otherwise come back reworded.
function evidenceLine(view, language, line, refresh) {
  const both = line.undo.length > 1;
  const undo = line.undo.length
    ? actionButton(both ? t("Put it back in your own words") : WAY_BACK[line.why] || t("Undo"), async () => {
      try {
        for (const change of line.undo) {
          await api(`/api/jobs/${view.job_id}/cv/${language}/change`, {
            method: "POST", body: JSON.stringify({ change_id: change, undone: true }),
          });
        }
      } finally {
        await refresh();
      }
    }, true)
    : null;
  return el("li", {}, `“${line.text}”`,
    line.shown ? null : el("span", { class: "muted" }, t` — ${LEFT_OUT[line.why] || t("not shown")}`), undo);
}

function suggestionBlock(item, post) {
  if (item.suggestion_adopted) return el("p", { class: "ok-text" }, t("Added to this CV."));
  if (item.suggestion_status === "added") return el("p", { class: "ok-text" }, t("Added to your profile and this CV."));
  if (item.suggestion_status === "declined") return el("p", { class: "muted" }, t("Not adopted — kept off your CV."));
  if (!completeSuggestion(item.suggestion)) return null;
  return el("div", {},
    el("p", {}, suggestionText(item.suggestion)),
    el("div", { class: "toolbar" },
      actionButton(t("Adopt and add to my CV"), post(`/${item.requirement_id}/write`, suggestedLine(item.suggestion))),
      actionButton(t("Skip this suggestion"), post(`/${item.requirement_id}/decline`), true)));
}

// The user's own words for a requirement: skills for one of the skills lines, or a new line under
// an entry, prefilled with DeepSeek's suggestion when there is one. On a refusal the text stays.
function writeOwn(view, item, places, refresh, ai = false) {
  if (item.suggestion_status === "added" || !places.length) return null;
  const suggested = ai && completeSuggestion(item.suggestion) ? suggestedLine(item.suggestion) : { place: null, text: "" };
  const select = el("select", {}, places.map((place) => el("option", { value: place.id, selected: place.id === suggested.place },
    place.kind === "skill" ? t`Add to skills line: ${place.where}` : t`New line under ${place.where}`)));
  const input = el("textarea", { rows: 4, "aria-label": t("CV entry") });
  const key = `gap:${view.job_id}:${view.gaps.language}:${item.requirement_id}:${ai}`;
  const words = draftField(input, key, suggested.text, shownCheck(view));
  const placeDraft = draftField(select, key + ":place", select.value);
  const hint = () => {
    I18n.attribute(input, "placeholder", select.value.startsWith("skill:") ? t("Skills to add, separated by commas") : t("The line as it should read on your CV"));
  };
  select.addEventListener("change", hint);
  hint();
  const add = actionButton(ai ? t("Adopt and add to my CV") : t("Add to my CV"), async () => {
    const text = input.value, place = select.value;
    await api(`/api/jobs/${view.job_id}/gaps/${item.requirement_id}/write`, {
      method: "POST", body: JSON.stringify({ ...words.expected(), place, text }),
    });
    words.saved(text, words.expected()); placeDraft.saved(place);
    await refresh();
  });
  return draftDetails(key, { class: "write-own", open: ai },
    el("summary", {}, ai ? t("Edit the suggested entry") : t("Write your own line")),
    el("p", { class: "muted" }, t("Use the complete suggestion as written, or edit it before adding it to your CV.")),
    el("div", { class: "write-row" }, select, input, add));
}

// Reuse a saved proposal; generate only on an explicit click. Replace this panel alone so
// requesting AI help never erases text the user has already typed into the manual editor.
function aiEntry(view, item, refresh) {
  if (item.suggestion_adopted || item.suggestion_status === "added" || item.suggestion_status === "declined") return suggestionBlock(item, () => {});
  const content = el("div", {});
  let opened = false;
  const result = (currentView, currentItem) => {
    opened = true;
    content.replaceChildren(...[
      el("p", { class: "suggested-entry" }, suggestionText(currentItem.suggestion)),
      writeOwn(currentView, currentItem, currentView.gaps.places || [], refresh, true)
        || el("p", {}, t("No place is available for this entry. Update your CV first.")),
    ].filter(Boolean));
  };
  const button = actionButton(t("AI suggested entry"), async () => {
    if (opened) { content.hidden = !content.hidden; return; }
    if (completeSuggestion(item.suggestion)) {
      result(view, item);
      return;
    }
    const asked = shownCheck(view);
    await api(`/api/jobs/${view.job_id}/gaps/${item.requirement_id}/suggest`, { method: "POST", body: JSON.stringify(asked) });
    const latest = await api(`/api/jobs/${view.job_id}`);
    const current = latest.gaps && !latest.gaps.outdated ? latest.gaps : null;
    // The job reads in the CV language chosen last, which another tab may have switched: then
    // that language's check and places are not this panel's, and nothing of them is offered or
    // adopted here. The panel stays as it is, with whatever the user typed.
    if (latest.language !== asked.language || (current && current.language !== asked.language)) {
      throw new Error(t("This page is out of date; reload it"));
    }
    const found = current && current.requirements.find((row) => row.requirement_id === item.requirement_id);
    if (!found) throw new Error(t("Your CV or facts changed since this check. Choose Check again to update the result."));
    if (!completeSuggestion(found.suggestion)) throw new Error(t("No usable suggestion was returned. Try AI suggested entry again."));
    // Cache only a complete suggestion; historical empty results remain retryable.
    item = found;
    view = latest;
    result(view, item);
  });
  // One this button generated (ai_checked) is shown at once, as it was before a reload or
  // reopen, so a saved suggestion never looks lost behind the button. Nothing is requested.
  if (item.ai_checked && completeSuggestion(item.suggestion)) result(view, item);
  return el("div", { class: "ai-entry" }, button, content);
}

// What a row offers for a requirement a check found missing or only related, before Write your
// own line: its complete suggestion (V1 shows it with Adopt and Skip, V2 behind AI suggested
// entry unless that entry generated it) or AI suggested entry to ask for one; once adopted or
// skipped, only that.
function suggestionArea(view, item, refresh, post) {
  if (MODE !== "v2" && (completeSuggestion(item.suggestion) || ["added", "declined"].includes(item.suggestion_status))) {
    return suggestionBlock(item, post);
  }
  return aiEntry(view, item, refresh);
}

function coverageRow(view, language, item, refresh, post) {
  const strength = item.strength && item.strength !== "required" ? el("span", { class: "muted auto" }, t` · ${strengthLabel(item.strength)}`) : "";
  const lines = item.evidence.length
    ? el("ul", { class: "evidence" }, item.evidence.map((line) => evidenceLine(view, language, line, refresh)))
    : null;
  const notes = [];
  if (item.status === "related") notes.push(el("p", { class: "muted" }, t`Missing: ${item.missing || item.text}`));
  if (item.status === "unchecked") notes.push(el("p", { class: "muted" }, t("DeepSeek gave no usable answer for this requirement, so it does not count as shown. Use Check again.")));
  // A check saved before checks recorded their requirements version is to check again although
  // the CV may be just as it was, so its rows do not say the CV changed.
  const unbound = view.gaps && view.gaps.state === "unbound";
  if (item.status === "recheck") {
    notes.push(el("p", { class: "muted" }, unbound
      ? t("Whether this CV still shows this is not known until it is checked again. Use Check again.")
      : t("Your CV changed after this check, so whether it still shows this is not known yet. Use Check again.")));
    if (LAST_CHECK[item.verdict]) notes.push(el("p", { class: "muted" }, t`Last check: ${LAST_CHECK[item.verdict]}`));
  }
  const gap = item.status === "related" || item.status === "none";
  // Found missing or related by a check now out of date: still to check again, but its
  // suggestions stay usable, so adopting one line never forces a new check before the next.
  const flagged = item.status === "recheck" && (item.verdict === "related" || item.verdict === "none");
  const undecided = !["added", "declined"].includes(item.suggestion_status);
  return el("div", { class: `requirement ${item.status} ${gap ? item.suggestion_status : ""}` },
    el("div", { class: "requirement-text" }, item.status === "none" ? t`Missing: ${item.missing || item.text}` : item.text, strength), lines, notes,
    flagged && undecided ? el("p", { class: "muted" }, unbound
      ? t("Suggested for what the last check found missing. That check may be out of date; adopt only what you still need.")
      : t("Suggested for what the last check found missing. Your CV has changed since; adopt only what you still need.")) : null,
    gap || flagged ? suggestionArea(view, item, refresh, post) : null,
    gap || flagged ? writeOwn(view, item, view.gaps.places || [], refresh) : null,
    (item.adopted || []).map((edit) => localEditRow(view, language, edit, refresh)));
}

function gapsPanel(view, refresh) {
  const gaps = view.gaps && !view.gaps.outdated ? view.gaps : null;
  const acted = shownCheck(view);
  const post = (path, body) => async () => {
    await api(`/api/jobs/${view.job_id}/gaps${path}`, { method: "POST", body: JSON.stringify({ ...acted, ...(body || {}) }) });
    await refresh();
  };
  const check = actionButton(gaps ? t("Check again") : t("Check now"), post(""), Boolean(gaps));
  const heading = el("h2", {}, t("3. What this CV shows for each requirement"));
  const intro = el("p", { class: "muted" },
    t("DeepSeek compares each requirement with your profile and the current CV. "),
    t("Use AI suggested entry to get complete wording for a requirement. "),
    t("Adopting changes this CV only. Save a line to your profile separately to reuse it."));
  if (!gaps) {
    return el("section", { class: "panel step-panel gaps-panel" }, heading, intro,
      el("p", { class: "muted" }, t("Not checked yet. Choose Check now to compare this CV with the requirements.")), el("div", { class: "toolbar" }, check));
  }
  const notes = [];
  // A check of other requirements shows no rows at all: none of its findings is a result now.
  if (gaps.requirements_changed) {
    notes.push(el("p", { class: "warning" }, t("The job's requirements changed after this check, so its results no longer apply. Choose Check again to compare this CV with the current requirements.")));
  } else if (gaps.state === "unbound") {
    notes.push(el("p", { class: "warning" }, t("This check was saved before checks recorded which requirements they used, so it is not known to match the current ones. Choose Check again to update the result.")));
  } else if (gaps.stale) {
    notes.push(el("p", { class: "warning" }, t("Your CV or facts changed since this check. Choose Check again to update the result.")));
  }
  const checking = gaps.requirements_changed ? {} : gaps.evidence_check || {};
  if (checking.fallback_reason) {
    notes.push(el("p", { class: "warning" }, t`Not checked: ${I18n.serverText(checking.message || checking.fallback_reason)} `,
      t("Nothing counts as shown until it is checked. Use Check again.")));
  }
  if (!gaps.requirements_changed && gaps.suggesting && gaps.suggesting.fallback_reason) {
    notes.push(el("p", { class: "warning" }, t`No suggestions this time: ${I18n.serverText(gaps.suggesting.message || gaps.suggesting.fallback_reason)} Use Check again.`));
  }
  const language = gaps.language || view.language;
  const summary = I18n.join(Object.entries(COVERAGE_COUNTS)
    .filter(([status]) => gaps.counts[status])
    .map(([status, label]) => t`${gaps.counts[status]} ${label}`), " · ");
  const groups = COVERAGE_GROUPS.map(([status, title]) => {
    const items = gaps.requirements.filter((item) => item.status === status);
    return items.length
      ? el("div", { class: "coverage-group" }, el("h3", {}, t`${title} (${items.length})`),
        items.map((item) => coverageRow(view, language, item, refresh, post)))
      : null;
  });
  const shown = gaps.requirements.filter((item) => item.status === "shown");
  return el("section", { class: "panel step-panel gaps-panel" }, heading, intro, notes,
    summary ? el("p", {}, el("strong", {}, summary)) : null,
    shown.length === gaps.requirements.length && shown.length
      ? el("p", { class: "ok-text" }, t("This CV shows something for every requirement.")) : null,
    groups,
    shown.length
      ? el("details", { class: "coverage-group" }, el("summary", {}, t`Shown in this CV (${shown.length})`),
        shown.map((item) => coverageRow(view, language, item, refresh, post)))
      : null,
    el("div", { class: "toolbar" }, check));
}

// A change the server was making when it stopped is undone at the next start; say what it was.
function interruptedNote(interrupted) {
  if (!interrupted) return null;
  const step = interrupted.step;
  if (step === "unknown") {
    return el("p", { class: "warning" },
      I18n.join([t("The workbench stopped in the middle of a change to this job and could not tell which one, so it left the files as they were. "), t("If the requirements or your CV look wrong, save the requirements again or use Start over.")]));
  }
  const what = step.startsWith("cv-approved-") ? t("approving your CV")
    : step.startsWith("cv-final-") ? t("creating the final PDF")
      : step.startsWith("cv-") ? t("preparing your CV")
        : step === "gaps" ? t("checking what your CV shows for each requirement")
          : step === "matches" || step === "linked" ? t("finding talking points")
            : t("saving this job's requirements");
  return el("p", { class: "warning" },
    t`The workbench stopped while ${what}, so that change was undone and nothing half-done was kept. Do it again if you still want it.`);
}

// An action that spans the facts, the CV layout and a job did not finish (the workbench stopped,
// or saving failed partway). Nothing is repeated for the user; repeating it is safe.
function unfinishedNote(notice, refresh) {
  const text = notice.kind === "save_cv"
    ? I18n.join([t("Saving an uploaded CV did not finish. Some lines may already have been saved, and "), t("your CV layout may still be the old one. Upload the same PDF again and save: lines already saved are reused, "), t("never added twice.")])
    : notice.kind === "save_line"
      ? t`Saving “${notice.line}” to your profile did not finish. Retry saving the same line to finish without duplicates.`
    : notice.kind === "add_line"
      ? I18n.join([t`Adding “${notice.line}” for “${notice.requirement}” did not finish. If that requirement below still offers `, t("to add it, add it again with the same words: it will not be added twice. If it says the line was added, "), t("prepare the CV again so the CV shows it.")])
      : I18n.join([t("A change did not finish, and its record could not be read. Check your facts and your CV, and repeat what "), t("you were doing.")]);
  const dismiss = async () => {
    await api("/api/notices/dismiss", { method: "POST", body: JSON.stringify({ id: notice.id }) });
    await refresh();
  };
  const tools = [];
  if (notice.kind === "save_line" && notice.job_id && notice.language && notice.block) {
    tools.push(actionButton(t("Retry saving to my profile"), cvEditAction({ job_id: notice.job_id }, notice.language,
      `lines/${notice.block}/save`, { expected_text: notice.line }, refresh), true));
  }
  if (notice.kind === "add_line" && notice.job_id && notice.language) {
    // Once the CV is prepared again with the line recorded, adding it is done: the notice goes.
    tools.push(actionButton(t("Prepare the CV again"), async () => {
      await api(`/api/jobs/${notice.job_id}/cv/${notice.language}/prepare`, { method: "POST" });
      await dismiss();
    }, true));
  }
  tools.push(actionButton(t("Dismiss"), dismiss, true));
  return el("p", { class: "warning" }, text, " ", ...tools);
}

// Review uses saved profile content already held by the server; it does not generate a CV.
function jobReviewPanel(view, refresh, needsPreparation) {
  const review = view.review;
  if (!review) return null;
  const matches = review.skill_matches || [];
  const count = review.fact_count ?? ((review.confirmed_fact_count || 0) + (review.pending_fact_count || 0));
  const ready = count > 0 && (view.cv_languages || []).length > 0;
  const running = (view.tasks || []).some((task) => !TERMINAL.has(task.status));
  let next = null;
  if (needsPreparation) {
    if (ready) {
      const prepare = actionButton(t("Prepare a CV for this job"), async () => {
        try {
          await api(`/api/jobs/${view.job_id}/requirements/find`, { method: "POST", body: JSON.stringify({ expected_version: view.requirements_version }) });
        } finally {
          await refresh();
        }
      });
      prepare.disabled = running;
      next = el("div", {}, el("p", { class: "muted" }, t("Continue to extract job requirements and prepare a draft using your profile. Review the draft before exporting.")),
        el("div", { class: "toolbar" }, prepare));
    } else {
      next = el("p", { class: "warning" }, t("Upload and save your CV profile before preparing a CV. You can keep reviewing this job meanwhile."), " ",
        el("a", { href: "#facts" }, t("Add your profile")));
    }
  }
  return el("section", { class: "panel job-review-panel" },
    el("h2", {}, t("Does this job deserve a closer look?")),
    el("p", { class: "muted" }, t("Compare the job wording with your profile. These are shared skill words, not a qualification check, a fit score or an offer prediction.")),
    el("p", {}, t`${count} saved profile lines`, " · ", el("a", { href: "#facts" }, t("Review or correct your facts"))),
    matches.length ? matches.map((match) => el("article", { class: "skill-evidence" },
      el("h3", {}, match.tag),
      el("div", { class: "skill-evidence-pair" },
        el("div", {}, el("h4", {}, t("In the job description")), el("blockquote", {}, match.job_quote)),
        el("div", {}, el("h4", {}, t("In your profile")), el("ul", {}, (match.facts || []).map((fact) => el("li", {}, fact.text)))))))
      : el("p", { class: "muted" }, t("No shared skill words found in your profile. This alone does not mean the role is unsuitable; review the description and your experience.")),
    next);
}

async function renderJob(jobId) {
  const current = pageGuard();
  let refreshRequest = 0, poll = null;
  // Opening a saved role is read-only in both modes. Paid checks require an explicit action.
  const refresh = async (checked = false) => {
    if (!current()) return;
    clearTimeout(poll);
    const ticket = ++refreshRequest;
    let view;
    try { view = await api(`/api/jobs/${jobId}`); }
    catch (error) { if (current() && ticket === refreshRequest) throw error; else return; }
    if (!current() || ticket !== refreshRequest) return;
    const restoreFocus = editorFocus();
    const jd = view.jd;
    const needsPreparation = MODE === "v2" && !view.requirements_version && !Object.values(view.cv || {}).some((cv) => cv.head);
    app.replaceChildren(
      el("section", { class: "panel job-hero" },
        el("h2", {}, jd.title || jobId),
        el("p", { class: "muted" },
          [jd.company, jd.location].filter(Boolean).join(" · ") || t("Company unknown"), t(" · captured "), localDate(jd.captured_at)),
        el("p", { class: "source-reference muted" }, sourceReference(jd.source)),
        jd.provider === "web" ? el("p", { class: "muted" }, t("Read from a public webpage. Check the description below for completeness; the source and whether the role is still open have not been verified.")) : null,
        view.review ? sponsorshipSummary(view.review.sponsorship, false, MODE === "v2" ? actionButton(t("Check the sponsor wording"), async () => {
          await api(`/api/jobs/${jobId}/sponsorship`, { method: "POST" });
          await refresh(true);
        }, true) : null) : null,
        el("details", {}, el("summary", {}, t("Job description")), el("pre", { class: "jd" }, jd.text)),
        interruptedNote(view.interrupted),
        (view.interrupted_operations || []).map((notice) => unfinishedNote(notice, refresh)),
        (view.tasks || []).map((task) => taskNote(task, refresh)),
      ),
      ...(view.review ? [jobReviewPanel(view, refresh, needsPreparation)] : []),
      ...(needsPreparation ? [] : [requirementsPanel(view, refresh), cvPanel(view, refresh), gapsPanel(view, refresh)]),
    );
    restoreFocus();
    // V2: while this job's work runs on the server, look again shortly; the page stays usable.
    if ((view.tasks || []).some((task) => !TERMINAL.has(task.status))) {
      poll = setTimeout(() => { if (current()) run(() => refresh(true)); }, 2000);
      return view;
    }
    return view;
  };
  await refresh();
}

// V2: work on this job that is waiting, running, or ended without a result.
const TASK_NAMES = {
  read_job_url: t("reading the job for review"), read_listing: t("reading the job for review"),
  prepare_job: t("finding requirements and preparing the CV"), job_from_url: t("reading the job and preparing the CV"),
  start_listing: t("reading the job and preparing the CV"), prepare_cv: t("preparing your CV"),
  tailor_cv: t("rewording your CV"), plan_cv: t("adjusting your CV"), check_gaps: t("checking what your CV shows"),
  suggest_gap: t("suggesting a related experience entry"),
  export_pdf: t("creating the final PDF"), upload_cv: t("reading your CV"),
};

function taskNote(task, refresh) {
  const name = TASK_NAMES[task.operation] || task.operation;
  if (task.status === "queued" || task.status === "running") {
    const cancel = actionButton(t("Cancel"), async () => {
      await api(`/api/tasks/${task.task_id}/cancel`, { method: "POST" });
      await refresh(true);
    }, true);
    return el("p", { class: "muted" }, task.status === "queued" ? t`Waiting to start: ${name}.` : t`Working on it: ${name}.`, " ", cancel);
  }
  if (task.status === "succeeded" || task.status === "superseded") return null;
  const dismiss = actionButton(t("Dismiss"), async () => {
    await api("/api/notices/dismiss", { method: "POST", body: JSON.stringify({ id: task.task_id }) });
    await refresh(true);
  }, true);
  const cost = task.cost === "unknown" ? t(" A model call may have been charged; it is not repeated by itself.") : "";
  return el("p", { class: "warning" }, t`Not finished: ${name}. `, taskMessage(task), ".", cost, " ",
    t("Use the button for it below to try again."), " ", dismiss);
}

// Frames and download links cannot send headers: the single-user page puts its token in their
// address. V2 pages are signed in with a cookie, so the token never goes into an address there.
function tokenQuery(separator) {
  return MODE === "v2" ? "" : `${separator}token=${encodeURIComponent(TOKEN)}`;
}

// V2: sign out (this server's session first, then Cognito's) and delete the account.
// Account-scoped usage is refreshed without api(), which may itself be following a task.
let usageRequest = 0;
async function refreshUsage() {
  if (MODE !== "v2") return;
  const ticket = ++usageRequest;
  const box = document.getElementById("account-usage");
  if (!box) return;
  try {
    const { response, body } = await request("/api/me");
    if (!response.ok || !body.usage || !body.usage.tokens) throw new Error("unavailable");
    if (ticket !== usageRequest) return;
    const usage = body.usage, tokens = usage.tokens;
    const number = (value) => Number(value).toLocaleString();
    const unit = usage.mode === "tokens" ? t("tokens") : t("legacy units");
    const allowance = usage.test_unlimited ? t("Test account · unlimited daily allowance")
      : !usage.configured || usage.limit === null ? t("Daily allowance is not configured")
      : t`Remaining today: ${number(usage.remaining)} / ${number(usage.limit)} ${unit}`;
    const details = el("details", { open: Boolean(box.querySelector("details[open]")) },
      el("summary", {}, allowance),
      el("p", {}, t`Reported tokens: ${number(tokens.total)} · input ${number(tokens.input)} · output ${number(tokens.output)}`),
      el("p", {}, t`Reserved tokens: ${number(tokens.reserved)} · running calls ${tokens.pending_calls} · usage unknown ${tokens.unknown_calls}`),
      el("p", { class: "muted" }, t("Reservations count toward the allowance until usage is reported. Unknown usage keeps its reservation; token counts are not a bill.")),
      usage.mode === "legacy" ? el("p", { class: "muted" }, t("This server still uses task units for its allowance. Token totals include only calls recorded by the new meter.")) : null,
      el("p", { class: "muted" }, t`Daily allowance resets at ${new Date(usage.resets_at).toLocaleString()} (your local time; midnight UTC).`));
    box.replaceChildren(details);
  } catch (_) {
    if (ticket === usageRequest) box.replaceChildren(el("span", {}, t("Usage is temporarily unavailable. This does not mean zero usage.")));
  }
}

function accountTools() {
  if (MODE !== "v2") return;
  const signOut = el("button", { class: "secondary", type: "button" }, t("Sign out"));
  signOut.addEventListener("click", () => run(async () => {
    const { logout_url: next } = await api("/logout", { method: "POST" });
    location.href = next;
  }));
  document.querySelector(".header-actions").append(signOut);
  document.querySelector("header.top").after(el("aside", { id: "account-usage", class: "account-usage", "aria-label": t("Daily model usage") }, t("Loading usage…")));
  window.addEventListener("focus", () => { void refreshUsage(); });
}

function accountPanel() {
  if (MODE !== "v2") return null;
  const remove = el("button", { class: "secondary", type: "button" }, t("Delete my account and data"));
  remove.addEventListener("click", () => run(async () => {
    const typed = window.prompt(t("This deletes your facts, CVs, jobs and PDFs now and cannot be undone. Backups are deleted when they expire. Type DELETE to continue."));
    if (typed === null) return;
    const { logout_url: next } = await api("/api/account/delete", { method: "POST", body: JSON.stringify({ confirm: typed }) });
    location.href = next;
  }));
  return el("section", { class: "panel account-panel" }, el("h2", {}, t("Your account")),
    el("p", { class: "muted" }, t("Only you can see your facts, CVs and jobs. Deleting the account removes them from this server at once.")),
    el("div", { class: "toolbar" }, remove));
}

const routes = { facts: renderFacts, find: renderFind, jobs: renderJobs };

// With no page chosen, any saved profile content can continue to job discovery.
async function openDefault() {
  const current = pageGuard();
  const { facts } = await api("/api/facts");
  if (!current()) return;
  location.replace(facts.length ? "#find" : "#facts");
}

function route() {
  pageGeneration += 1;
  const [name, id] = location.hash.slice(1).split("/");
  for (const link of document.querySelectorAll("nav a")) {
    link.classList.toggle("active", link.dataset.route === (name === "job" ? "jobs" : name));
  }
  if (name === "job" && id) run(() => renderJob(id));
  else run(routes[name] || openDefault);
  if (flash) {
    show(...flash);
    flash = null;
  }
}

I18n.mount();
accountTools();
window.addEventListener("hashchange", route);
route();
