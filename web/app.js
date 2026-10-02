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
  while (!TERMINAL.has(current.status)) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const { response, body } = await request(`/api/tasks/${encodeURIComponent(task.task_id)}`);
    if (response.status === 401) return api(`/api/tasks/${encodeURIComponent(task.task_id)}`);
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    current = body.task;
  }
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
  clearMessage();
  try {
    await action();
  } catch (error) {
    show(I18n.serverText(error.message));
  }
  if (pendingWarning) {
    show(pendingWarning, "warning");
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
      t("Copied word for word from your PDF. After saving, each line waits in the list below until you confirm it"),
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
    else show(t`Saved your CV. ${result.imported} new line(s) wait below for you to confirm${result.reused ? t`; ${result.reused} matched facts you already had.` : "."}`, "ok");
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
      t("Upload your CV as a PDF: each line becomes a fact for you to confirm, and its layout is the base of every job's CV. "),
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
  const text = el("textarea", { class: "fact-edit-text", "aria-label": t("Fact text") }, fact.text);
  const tags = el("input", { type: "text", value: fact.tags.join(", "), "aria-label": t("Skill tags") });
  const details = el("details", { class: "fact-editor" },
    el("summary", {}, t("Edit this line")),
    el("p", { class: "muted" }, t("Saving creates a new version pending confirmation. Earlier text stays in history; affected CVs need review again.")),
    field(t("Fact text"), text), field(t("Skill tags"), tags));
  const save = actionButton(t("Save changes"), async () => {
    await api(`/api/facts/${encodeURIComponent(fact.id)}/edit`, { method: "POST", body: JSON.stringify({
      expected_version: fact.version, text: text.value.trim(), tags: tags.value.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean),
    }) });
    await renderFacts();
    show(t("Saved. Confirm the changed line before preparing the CV again."), "ok");
  });
  const cancel = el("button", { class: "secondary", onclick: () => {
    text.value = fact.text; tags.value = fact.tags.join(", "); details.open = false;
  } }, t("Cancel"));
  details.append(el("div", { class: "toolbar" }, save, cancel));
  return details;
}

async function renderFacts() {
  const [{ facts, interrupted }, { languages }] = await Promise.all([api("/api/facts"), api("/api/cv/languages")]);
  const pending = facts.filter((fact) => fact.status !== "confirmed");
  const selected = new Set();
  const confirmButton = el("button", { disabled: true }, t("Confirm selected"));
  const updateButton = () => {
    confirmButton.disabled = selected.size === 0;
    I18n.setText(confirmButton, selected.size ? t`Confirm ${selected.size} selected` : t("Confirm selected"));
  };
  confirmButton.addEventListener("click", () => run(async () => {
    const result = await api("/api/facts/confirm", {
      method: "POST",
      body: JSON.stringify({ refs: [...selected] }),
    });
    const { facts: after } = await api("/api/facts");
    if (after.some((fact) => fact.status !== "confirmed")) {
      await renderFacts();
      show(t`Confirmed ${result.changed_count} fact(s).`, "ok");
    } else {
      flash = [t`Confirmed ${result.changed_count} fact(s). These are the jobs that mention your skills.`, "ok"];
      location.hash = "#find";
    }
  }));
  const selectAll = el("input", { type: "checkbox", title: t("Select all pending") });
  selectAll.addEventListener("change", () => {
    for (const box of app.querySelectorAll("input[data-ref]")) {
      box.checked = selectAll.checked;
      box.dispatchEvent(new Event("change"));
    }
  });
  const rows = facts.map((fact) => {
    const ref = `${fact.id}@${fact.version}`;
    let box = "";
    if (fact.status !== "confirmed") {
      box = el("input", { type: "checkbox", "data-ref": ref });
      box.addEventListener("change", () => {
        if (box.checked) selected.add(ref); else selected.delete(ref);
        updateButton();
      });
    }
    return el("tr", { class: `fact-row ${fact.status}` },
      el("td", {}, box),
      el("td", {}, el("code", {}, fact.id), el("div", { class: "muted" }, t`v${fact.version} · ${t(fact.fact_type)}`)),
      el("td", {}, fact.text, el("div", { class: "tags" }, fact.tags.map((tag) => el("span", { class: "tag" }, tag))), factEditor(fact)),
      el("td", {}, el("span", { class: `status ${fact.status}` }, t(fact.status))),
    );
  });
  app.replaceChildren(
    uploadPanel(languages),
    el("section", { class: "panel facts-panel" },
      el("h2", {}, t("Facts")),
      (interrupted || []).map((notice) => unfinishedNote(notice, renderFacts)),
      el("p", { class: "muted" },
        t`${facts.length} facts, ${pending.length} pending. Only confirmed facts can be matched or used in a CV. `,
        t("Read each one carefully before confirming.")),
      facts.length === 0
        ? el("p", {}, t("No facts yet. Upload your CV above: each of its lines becomes a fact to confirm here."))
        : el("div", {},
          el("div", { class: "toolbar" }, confirmButton),
          el("table", {},
            el("thead", {}, el("tr", {}, el("th", {}, pending.length ? selectAll : ""), el("th", {}, t("Fact")), el("th", {}, t("Text and tags")), el("th", {}, t("Status")))),
            el("tbody", {}, rows))),
    ),
    accountPanel(),
  );
}

function localDate(iso) {
  return iso ? I18n.date(iso) : "";
}

function field(label, input, hint) {
  return el("label", { class: "field" }, el("span", {}, label), input, hint ? el("small", { class: "muted" }, hint) : null);
}

async function renderJobs() {
  const { jobs } = await api("/api/jobs");
  const postingURL = el("input", { type: "url", required: true, placeholder: t("Paste a job posting URL") });
  const importURL = el("button", { type: "submit" }, t("Read and generate"));
  const urlForm = el("form", {},
    field(t("Job posting URL"), postingURL, t("Enter any public job posting URL. If the site blocks reading or the description is incomplete, paste it manually below.")),
    el("div", { class: "toolbar" }, importURL));
  urlForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (importURL.disabled) return;
    run(async () => {
      importURL.disabled = true;
      I18n.setText(importURL, t("Reading and preparing…"));
      try {
        const { job_id, existing } = await api("/api/jobs/from-url", {
          method: "POST", body: JSON.stringify({ url: postingURL.value.trim() }),
        });
        if (existing) flash = [t("This job is already saved. Opened its existing version."), "ok"];
        location.hash = `#job/${job_id}`;
      } finally {
        importURL.disabled = false;
        I18n.setText(importURL, t("Read and generate"));
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
  const create = el("button", {}, t("Create job"));
  create.addEventListener("click", () => run(async () => {
    const body = Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value.trim() || null]));
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
      el("p", { class: "muted" }, t("Paste a job link to read its description and prepare a CV draft for review.")),
      urlForm,
      el("details", {}, el("summary", {}, t("Paste the description manually")),
      el("p", { class: "muted" }, t("Paste a job description from any site. Its requirements are found and your CV is prepared for it automatically.")),
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
const find = { title: "", location: "", region: recall("find.region", "all"), limit: 50, hideSenior: recall("find.hideSenior", false), refreshing: null };

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

function listingRow(item, rank) {
  const first = item.postings[0];
  const count = new Set(item.matched.map((tag) => tag.toLowerCase())).size;
  let choose = null;
  if (item.postings.length > 1 && !item.started_job) {
    choose = el("select", { title: t("Location") }, item.postings.map((posting, index) =>
      el("option", { value: String(index) }, posting.location || t("location unknown"))));
  }
  const action = item.started_job
    ? el("a", { class: "button", href: `#job/${item.started_job}` }, t("Open"))
    : actionButton(t("Start"), async () => {
      const posting = item.postings[choose ? Number(choose.value) : 0];
      const { job_id } = await api("/api/listings/start", {
        method: "POST",
        body: JSON.stringify({ provider: item.provider, board: item.board, job_id: posting.job_id }),
      });
      location.hash = `#job/${job_id}`;
    });
  const title = first.source.startsWith("https://")
    ? el("a", { href: first.source, target: "_blank", rel: "noopener noreferrer" }, item.title)
    : item.title;
  return el("tr", {},
    el("td", { class: "muted" }, String(rank)),
    el("td", {}, title,
      el("div", { class: "muted" }, I18n.join([item.company, locationsText(item.postings), item.posted_at ? t`posted ${localDate(item.posted_at)}` : null].filter(Boolean), " · "))),
    el("td", {}, el("strong", {}, String(count)), el("div", { class: "tags" }, item.matched.map((tag) => el("span", { class: "tag" }, tag)))),
    el("td", { class: "choice" }, el("div", { class: "start" }, choose, action)),
  );
}

async function renderFind() {
  const status = el("p", { class: "muted", id: "find-status" });
  const listBox = el("div", {}, el("p", { class: "muted" }, t("Loading…")));
  const companiesBox = el("div");
  const inputs = {
    title: el("input", { type: "text", placeholder: t("Title contains, e.g. engineer or intern"), value: find.title }),
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
    const { sources } = await api("/api/sources");
    await refreshNow(sources);
  }, true);

  async function drawList() {
    const params = new URLSearchParams({
      title: find.title, location: find.location, region: find.region, limit: String(find.limit), hide_senior: String(find.hideSenior),
    });
    const [data, { sources }] = await Promise.all([api(`/api/listings?${params}`), api("/api/sources")]);
    const newest = sources.map((source) => source.fetched_at).filter(Boolean).sort().pop();
    if (!find.refreshing) I18n.setText(status, t`${data.total} roles from ${sources.length} companies · updated ${ago(newest)}`);
    const notes = [];
    if (data.skill_count === 0) {
      notes.push(el("p", { class: "warning" }, t("No confirmed skills yet, so nothing can be matched. "),
        el("a", { href: "#facts" }, t("Confirm your facts first."))));
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
    listBox.replaceChildren(...notes, body, data.listings.length >= 200 && more ? el("p", { class: "muted" }, t("Showing the top 200. Use the filters to narrow the list.")) : more);
  }

  async function drawCompanies() {
    const { sources } = await api("/api/sources");
    const link = el("input", { type: "text", placeholder: t("Paste a job board link, e.g. https://jobs.lever.co/palantir") });
    const add = actionButton(t("Add company"), async () => {
      const { added } = await api("/api/sources", { method: "POST", body: JSON.stringify({ link: link.value }) });
      show(t`Following ${added.company}: ${added.open} open jobs.`, "ok");
      await Promise.all([drawCompanies(), drawList()]);
    });
    const rows = sources.map((source) => {
      const remove = el("button", { class: "secondary" }, t("Remove"));
      remove.addEventListener("click", () => run(async () => {
        if (!window.confirm(t`Stop following ${source.company}? Its downloaded jobs are removed from this list.`)) return;
        await api(`/api/sources/${source.provider}/${encodeURIComponent(source.board)}`, { method: "DELETE" });
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
    if (!listBox.isConnected) return;
    findStatus(t("Ranking…"));
    if (failures.length) show(t`${failures.length} company(ies) could not be updated. Details are under Companies.`);
    await Promise.all([drawList(), drawCompanies()]);
  }

  app.replaceChildren(
    el("section", { class: "panel find-panel" },
      el("h2", {}, t("Jobs that match your skills")),
      el("p", { class: "muted" },
        t("Most matched first: how many of your confirmed skills each posting mentions. "),
        t("It counts words; it is not a fit score or your chance of an offer. Pending facts never count.")),
      el("div", { class: "toolbar" }, region, inputs.title, inputs.location, filterButton,
        el("label", { class: "check", title: t("Hides Senior, Staff, Principal, Lead, Manager, Director… titles. \"Member of Technical Staff\" stays.") }, hideSenior, t(" Hide senior roles")),
        refreshAll),
      status,
      listBox),
    el("section", { class: "panel companies-panel" }, companiesBox),
  );
  await Promise.all([drawList(), drawCompanies()]);
  const { sources } = await api("/api/sources");
  await refreshNow(sources.filter((source) => source.stale));
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
  const choices = new Map();
  const rows = (view.candidates || []).map((item) => {
    const confirm = el("input", { type: "radio", name: item.id, checked: item.status === "confirmed" });
    const exclude = el("input", { type: "radio", name: item.id, checked: item.status === "excluded" });
    if (item.status === "confirmed" || item.status === "excluded") choices.set(item.id, item.status);
    confirm.addEventListener("change", () => choices.set(item.id, "confirmed"));
    exclude.addEventListener("change", () => choices.set(item.id, "excluded"));
    return el("tr", {},
      el("td", {}, item.text, el("div", { class: "muted" }, I18n.join([strengthLabel(item.strength), item.section, decidedLabel(item)].filter(Boolean), " · "))),
      el("td", { class: "choice" }, el("label", {}, confirm, t(" Requirement"))),
      el("td", { class: "choice" }, el("label", {}, exclude, t(" Not a requirement"))),
    );
  });
  const missed = el("input", { type: "text", placeholder: t("Copy a line or phrase exactly from the job description") });
  const add = actionButton(t("Add missed requirement"), async () => {
    await api(`/api/jobs/${view.job_id}/requirements/add`, { method: "POST", body: JSON.stringify({ text: missed.value, expected_version: view.requirements_version }) });
    await refresh();
    show(t("Added. It counts, and the CV was prepared again."), "ok");
  }, true);
  const save = actionButton(t("Save"), async () => {
    const confirm = [...choices].filter(([, status]) => status === "confirmed").map(([id]) => id);
    const exclude = [...choices].filter(([, status]) => status === "excluded").map(([id]) => id);
    await api(`/api/jobs/${view.job_id}/requirements/decide`, { method: "POST", body: JSON.stringify({ confirm, exclude, expected_version: view.requirements_version }) });
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
    counted ? el("details", {}, el("summary", {}, summary), body) : body,
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
  if (approved) notes.push(el("p", { class: "ok-text" }, t`Approved ${I18n.date(cv.approved_at, true)}. To change anything, use Start over and approve again.`));
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
    notes.push(el("details", {}, el("summary", {}, t`${cv.rejected.length} rewording(s) failed the fact check, so your own wording is kept`),
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
  return el("div", { class: "cv-block" }, el("h3", {}, title), el("div", { class: "toolbar" }, tools), notes, approval, preview);
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
      t("Made from your confirmed facts and adjusted for this job: the most relevant parts first, what does not help cut, "),
      t("and wording that follows the job's requirements. Nothing is added, and every reworded line is fact-checked. "),
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

// What the CV shows for each requirement, most useful to act on first. Worked out on the
// server from the CV as it is shown now, so undoing a cut changes it at once.
const COVERAGE_GROUPS = [
  ["not_shown", t("Your facts show it, but this CV leaves it out")],
  ["related", t("Related only")],
  ["none", t("No evidence in your confirmed facts")],
  ["unchecked", t("Not checked")],
];
const COVERAGE_COUNTS = { shown: t("shown"), not_shown: t("left out"), related: t("related only"), none: t("no evidence"), unchecked: t("not checked") };
const LEFT_OUT = {
  cut: t("cut for this job"),
  reworded: t("reworded in a way that no longer shows it"),
  not_on_cv: t("in your confirmed facts, but not on your CV"),
};
const WAY_BACK = { cut: t("Put it back"), reworded: t("Use your own wording") };

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
  if (item.suggestion_status === "added") return el("p", { class: "ok-text" }, t("Added to your facts and to this CV."));
  if (item.suggestion_status === "declined") return el("p", { class: "muted" }, t("Not true for you — kept off your CV."));
  if (!item.suggestion) return null;
  return el("div", {},
    el("p", {}, suggestionText(item.suggestion)),
    el("div", { class: "toolbar" },
      actionButton(t("True for me — add it"), post(`/${item.requirement_id}/accept`)),
      actionButton(t("Not true"), post(`/${item.requirement_id}/decline`), true)));
}

// The user's own words for a requirement: skills for one of the skills lines, or a new line under
// an entry, prefilled with DeepSeek's suggestion when there is one. On a refusal the text stays.
function writeOwn(view, item, places, refresh) {
  if (item.suggestion_status === "added" || !places.length) return null;
  const suggestion = item.suggestion || {};
  const chosen = suggestion.kind === "skill" ? `skill:${suggestion.fact_id}`
    : suggestion.kind === "bullet" ? `entry:${suggestion.entry_key}` : null;
  const select = el("select", {}, places.map((place) => el("option", { value: place.id, selected: place.id === chosen },
    place.kind === "skill" ? t`Add to skills line: ${place.where}` : t`New line under ${place.where}`)));
  const input = el("input", { type: "text", value: suggestion.kind === "skill" ? suggestion.items.join(", ") : suggestion.text || "" });
  const hint = () => {
    I18n.attribute(input, "placeholder", select.value.startsWith("skill:") ? t("Skills to add, separated by commas") : t("The line as it should read on your CV"));
  };
  select.addEventListener("change", hint);
  hint();
  const add = actionButton(t("Add to my CV"), async () => {
    await api(`/api/jobs/${view.job_id}/gaps/${item.requirement_id}/write`, {
      method: "POST", body: JSON.stringify({ place: select.value, text: input.value }),
    });
    await refresh();
  });
  return el("details", { class: "write-own" },
    el("summary", {}, suggestion.kind ? t("Edit it, or write your own") : t("Write your own line")),
    el("p", { class: "muted" }, t("Only what is true for you. It is added as you write it, as a confirmed fact, and the CV is prepared again.")),
    el("div", { class: "write-row" }, select, input, add));
}

function coverageRow(view, language, item, refresh, post) {
  const strength = item.strength && item.strength !== "required" ? el("span", { class: "muted auto" }, t` · ${strengthLabel(item.strength)}`) : "";
  const lines = item.evidence.length
    ? el("ul", { class: "evidence" }, item.evidence.map((line) => evidenceLine(view, language, line, refresh)))
    : null;
  const notes = [];
  if (item.status === "related") notes.push(el("p", { class: "muted" }, t`Missing: ${item.missing || item.text}`));
  if (item.status === "unchecked") notes.push(el("p", { class: "muted" }, t("DeepSeek gave no usable answer for this requirement, so it does not count as shown. Use Check again.")));
  const gap = item.status === "related" || item.status === "none";
  return el("div", { class: `requirement ${item.status} ${gap ? item.suggestion_status : ""}` },
    el("div", { class: "requirement-text" }, item.status === "none" ? t`Missing: ${item.missing || item.text}` : item.text, strength), lines, notes,
    gap ? suggestionBlock(item, post) : null, gap ? writeOwn(view, item, view.gaps.places || [], refresh) : null);
}

function gapsPanel(view, refresh) {
  const gaps = view.gaps && !view.gaps.outdated ? view.gaps : null;
  const post = (path) => async () => {
    await api(`/api/jobs/${view.job_id}/gaps${path}`, { method: "POST" });
    await refresh();
  };
  const check = actionButton(gaps ? t("Check again") : t("Check now"), post(""), Boolean(gaps));
  const heading = el("h2", {}, t("3. What this CV shows for each requirement"));
  const intro = el("p", { class: "muted" },
    t("DeepSeek compares each requirement with your confirmed facts and with this CV as it is shown now, after cuts and rewording. "),
    t("A line that is only about the same thing does not count as showing it. Nothing is added until you click True for me; "),
    t("then it becomes a confirmed fact and the CV is prepared again."));
  if (!gaps) {
    return el("section", { class: "panel step-panel gaps-panel" }, heading, intro,
      el("p", { class: "muted" }, t("Checking…")), el("div", { class: "toolbar" }, check));
  }
  const notes = [];
  if (gaps.stale) notes.push(el("p", { class: "warning" }, t("Your CV or facts changed since this check. Checking again…")));
  const checking = gaps.evidence_check || {};
  if (checking.fallback_reason) {
    notes.push(el("p", { class: "warning" }, t`Not checked: ${I18n.serverText(checking.message || checking.fallback_reason)} `,
      t("Nothing counts as shown until it is checked. Use Check again.")));
  }
  if (gaps.suggesting && gaps.suggesting.fallback_reason) {
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
    ? I18n.join([t("Saving an uploaded CV did not finish. Some of its lines may already be here, waiting for confirmation, and "), t("your CV layout may still be the old one. Upload the same PDF again and save: lines already saved are reused, "), t("never added twice.")])
    : notice.kind === "add_line"
      ? I18n.join([t`Adding “${notice.line}” for “${notice.requirement}” did not finish. If that requirement below still offers `, t("to add it, add it again with the same words: it will not be added twice. If it says the line was added, "), t("prepare the CV again so the CV shows it.")])
      : I18n.join([t("A change did not finish, and its record could not be read. Check your facts and your CV, and repeat what "), t("you were doing.")]);
  const dismiss = async () => {
    await api("/api/notices/dismiss", { method: "POST", body: JSON.stringify({ id: notice.id }) });
    await refresh();
  };
  const tools = [];
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

async function renderJob(jobId) {
  // Requirements are checked against the CV once it exists, after the page shows, so Start
  // stays quick; and again, at most once per refresh, whenever the facts or the CV's wording
  // changed since, for example after True for me or Start over.
  const refresh = async (checked = false) => {
    const view = await api(`/api/jobs/${jobId}`);
    const jd = view.jd;
    app.replaceChildren(
      el("section", { class: "panel job-hero" },
        el("h2", {}, jd.title || jobId),
        el("p", { class: "muted" },
          [jd.company, jd.location].filter(Boolean).join(" · ") || t("Company unknown"), t(" · captured "), localDate(jd.captured_at)),
        jd.source ? el("p", {}, el("a", { href: jd.source, target: "_blank", rel: "noopener noreferrer" }, jd.source)) : el("p", { class: "muted" }, t("Source: unknown")),
        jd.provider === "web" ? el("p", { class: "muted" }, t("Read from a public webpage. Check the description below for completeness; the source and whether the role is still open have not been verified.")) : null,
        el("details", {}, el("summary", {}, t("Job description")), el("pre", { class: "jd" }, jd.text)),
        interruptedNote(view.interrupted),
        (view.interrupted_operations || []).map((notice) => unfinishedNote(notice, refresh)),
        (view.tasks || []).map((task) => taskNote(task, refresh)),
      ),
      requirementsPanel(view, refresh),
      cvPanel(view, refresh),
      gapsPanel(view, refresh),
    );
    // V2: while this job's work runs on the server, look again shortly; the page stays usable.
    if ((view.tasks || []).some((task) => !TERMINAL.has(task.status))) {
      setTimeout(() => { if (location.hash === `#job/${jobId}`) run(() => refresh(true)); }, 2000);
      return view;
    }
    const due = !view.gaps || view.gaps.outdated || view.gaps.stale;
    if (!checked && due && (view.selected_requirements || []).length && view.cv[view.language].head) {
      await api(`/api/jobs/${jobId}/gaps`, { method: "POST" });
      return refresh(true);
    }
    return view;
  };
  await refresh();
}

// V2: work on this job that is waiting, running, or ended without a result.
const TASK_NAMES = {
  prepare_job: t("finding requirements and preparing the CV"), job_from_url: t("reading the job and preparing the CV"),
  start_listing: t("reading the job and preparing the CV"), prepare_cv: t("preparing your CV"),
  tailor_cv: t("rewording your CV"), plan_cv: t("adjusting your CV"), check_gaps: t("checking what your CV shows"),
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
function accountTools() {
  if (MODE !== "v2") return;
  const signOut = el("button", { class: "secondary", type: "button" }, t("Sign out"));
  signOut.addEventListener("click", () => run(async () => {
    const { logout_url: next } = await api("/logout", { method: "POST" });
    location.href = next;
  }));
  document.querySelector(".header-actions").append(signOut);
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

// With no page chosen: facts while any wait for confirmation, otherwise the matched jobs.
async function openDefault() {
  const { facts } = await api("/api/facts");
  location.replace(facts.length && facts.every((fact) => fact.status === "confirmed") ? "#find" : "#facts");
}

function route() {
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
