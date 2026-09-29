"use strict";

// Every request carries the per-start token; data is only ever inserted as text.
const TOKEN = document.querySelector('meta[name="workbench-token"]').content;
const app = document.getElementById("app");
const message = document.getElementById("message");

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers, "X-Workbench-Token": TOKEN },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error || body.detail || `HTTP ${response.status}`);
  }
  return body;
}

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "onclick") node.addEventListener("click", value);
    else if (value === true) node.setAttribute(name, "");
    else if (value !== false && value !== null && value !== undefined) node.setAttribute(name, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function show(text, kind = "error") {
  message.textContent = text;
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
    show(error.message);
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
    const label = el("input", { type: "text", value: link.label, placeholder: "Label, e.g. GitHub" });
    const url = el("input", { type: "url", value: link.url, placeholder: "https://" });
    const row = el("div", { class: "link-row" }, label, url);
    row.append(el("button", { class: "secondary", type: "button", onclick: () => row.remove() }, "Remove"));
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
        fact.text, fact.known ? el("span", { class: "muted" }, " · already in your facts") : ""))) : ""))));
  const save = el("button", {}, proposal.has_profile ? "Replace my CV with this" : "Save as my CV");
  save.addEventListener("click", () => run(async () => {
    const body = Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value.trim()]));
    body.links = [...linkRows.children].map((row) => row.link()).filter((link) => link.url);
    const result = await api(`/api/cv/uploads/${proposal.upload_id}/save`, { method: "POST", body: JSON.stringify(body) });
    await onSaved(result);
  }));
  return el("div", { class: "upload-review" },
    el("h3", {}, "1. Check your name and contact details"),
    el("p", { class: "muted" }, "Read from the top of your CV on this computer and never sent to DeepSeek. They go on the header of every CV."),
    el("div", { class: "grid" }, field("Name *", inputs.name), field("Location", inputs.location), field("Phone", inputs.phone), field("Email", inputs.email)),
    el("div", { class: "field" }, el("span", {}, "Links"), linkRows,
      el("div", {}, el("button", { class: "secondary", type: "button", onclick: () => addLink() }, "Add link"))),
    found.other.length ? el("p", { class: "muted" }, "Also at the top of your CV, not used: ", found.other.join(" · ")) : "",
    el("h3", {}, `2. What was found: ${facts.length} lines`),
    el("p", { class: "muted" },
      "Copied word for word from your PDF. After saving, each line waits in the list below until you confirm it",
      known ? `; ${known} already match facts you have.` : "."),
    layout,
    proposal.not_imported.length
      ? el("details", {}, el("summary", {}, `${proposal.not_imported.length} line(s) not imported`),
        el("ul", {}, proposal.not_imported.map((line) => el("li", {}, line))))
      : "",
    proposal.has_profile ? el("p", { class: "warning" }, "Saving replaces your current CV layout; the old one is kept in profile-history.") : "",
    el("div", { class: "toolbar" }, save, el("button", { class: "secondary", type: "button", onclick: onCancel }, "Cancel")));
}

function uploadPanel() {
  const input = el("input", { type: "file", accept: "application/pdf,.pdf" });
  const box = el("div", {});
  const reset = () => { box.replaceChildren(); input.value = ""; };
  const saved = async (result) => {
    await renderFacts();
    if (!result.imported) show(`Saved your CV. All ${result.reused} lines match facts you already had.`, "ok");
    else show(`Saved your CV. ${result.imported} new line(s) wait below for you to confirm`
      + (result.reused ? `; ${result.reused} matched facts you already had.` : "."), "ok");
  };
  input.addEventListener("change", () => run(async () => {
    const file = input.files[0];
    if (!file) return;
    box.replaceChildren(el("p", { class: "muted" }, `Reading ${file.name}…`));
    try {
      const proposal = await api("/api/cv/upload", { method: "POST", body: file, headers: { "Content-Type": "application/pdf" } });
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
    el("h2", {}, "Your CV"),
    el("p", { class: "muted" },
      "Upload your CV as a PDF: each line becomes a fact for you to confirm, and its layout is the base of every job's CV. ",
      "Your name, email, phone and links stay on this computer; DeepSeek sees the other lines to tell sections, entries and bullets apart."),
    el("label", { class: "file-pick" }, el("span", {}, "Choose a PDF"), input),
    box);
}

async function renderFacts() {
  const { facts } = await api("/api/facts");
  const pending = facts.filter((fact) => fact.status !== "confirmed");
  const selected = new Set();
  const confirmButton = el("button", { disabled: true }, "Confirm selected");
  const updateButton = () => {
    confirmButton.disabled = selected.size === 0;
    confirmButton.textContent = selected.size ? `Confirm ${selected.size} selected` : "Confirm selected";
  };
  confirmButton.addEventListener("click", () => run(async () => {
    const result = await api("/api/facts/confirm", {
      method: "POST",
      body: JSON.stringify({ refs: [...selected] }),
    });
    const { facts: after } = await api("/api/facts");
    if (after.some((fact) => fact.status !== "confirmed")) {
      await renderFacts();
      show(`Confirmed ${result.changed_count} fact(s).`, "ok");
    } else {
      flash = [`Confirmed ${result.changed_count} fact(s). These are the jobs that mention your skills.`, "ok"];
      location.hash = "#find";
    }
  }));
  const selectAll = el("input", { type: "checkbox", title: "Select all pending" });
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
      el("td", {}, el("code", {}, fact.id), el("div", { class: "muted" }, `v${fact.version} · ${fact.fact_type}`)),
      el("td", {}, fact.text, el("div", { class: "tags" }, fact.tags.map((tag) => el("span", { class: "tag" }, tag)))),
      el("td", {}, el("span", { class: `status ${fact.status}` }, fact.status)),
    );
  });
  app.replaceChildren(
    uploadPanel(),
    el("section", { class: "panel facts-panel" },
      el("h2", {}, "Facts"),
      el("p", { class: "muted" },
        `${facts.length} facts, ${pending.length} pending. Only confirmed facts can be matched or used in a CV. `,
        "Read each one carefully before confirming."),
      facts.length === 0
        ? el("p", {}, "No facts yet. Upload your CV above: each of its lines becomes a fact to confirm here.")
        : el("div", {},
          el("div", { class: "toolbar" }, confirmButton),
          el("table", {},
            el("thead", {}, el("tr", {}, el("th", {}, pending.length ? selectAll : ""), el("th", {}, "Fact"), el("th", {}, "Text and tags"), el("th", {}, "Status"))),
            el("tbody", {}, rows))),
    ),
  );
}

function localDate(iso) {
  return iso ? new Date(iso).toLocaleDateString() : "";
}

function field(label, input, hint) {
  return el("label", { class: "field" }, el("span", {}, label), input, hint ? el("small", { class: "muted" }, hint) : null);
}

async function renderJobs() {
  const { jobs } = await api("/api/jobs");
  const inputs = {
    title: el("input", { type: "text", required: true, placeholder: "Software Engineer Intern" }),
    company: el("input", { type: "text", placeholder: "Company" }),
    url: el("input", { type: "url", placeholder: "https://… (official posting)" }),
    location: el("input", { type: "text", placeholder: "Location" }),
    text: el("textarea", { required: true, placeholder: "Paste the full job description here" }),
  };
  const create = el("button", {}, "Create job");
  create.addEventListener("click", () => run(async () => {
    const body = Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value.trim() || null]));
    const { job_id } = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
    location.hash = `#job/${job_id}`;
  }));
  const list = jobs.length === 0
    ? el("p", { class: "muted" }, "No jobs yet.")
    : el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Job"), el("th", {}, "Captured"), el("th", {}, "Progress"))),
      el("tbody", {}, jobs.map((job) => el("tr", {},
        el("td", {}, el("a", { href: `#job/${job.job_id}` }, job.title || job.job_id), el("div", { class: "muted" }, job.company || "")),
        el("td", { class: "muted" }, localDate(job.captured_at)),
        el("td", {}, progress(job.steps)),
      ))));
  app.replaceChildren(
    el("section", { class: "panel jobs-panel" }, el("h2", {}, "Jobs"), list),
    el("section", { class: "panel new-job-panel" },
      el("h2", {}, "New job"),
      el("p", { class: "muted" }, "Paste a job description from any site. Its requirements are found and your CV is prepared for it automatically."),
      el("div", { class: "grid" }, field("Title *", inputs.title), field("Company", inputs.company), field("Official link", inputs.url, "Leave empty if unknown; the source is then recorded as unknown."), field("Location", inputs.location)),
      field("Job description *", inputs.text),
      el("div", { class: "toolbar" }, create),
    ),
  );
}

function ago(iso) {
  if (!iso) return "never";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
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
const find = { title: "", location: "", limit: 50, hideSenior: recall("find.hideSenior", false), refreshing: null };

function findStatus(text) {
  const node = document.getElementById("find-status");
  if (node) node.textContent = text;
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
  const names = postings.map((posting) => posting.location || "location unknown");
  return names.length > 3 ? `${names.slice(0, 3).join("; ")} +${names.length - 3} more` : names.join("; ");
}

function listingRow(item, rank) {
  const first = item.postings[0];
  const count = new Set(item.matched.map((tag) => tag.toLowerCase())).size;
  let choose = null;
  if (item.postings.length > 1 && !item.started_job) {
    choose = el("select", { title: "Location" }, item.postings.map((posting, index) =>
      el("option", { value: String(index) }, posting.location || "location unknown")));
  }
  const action = item.started_job
    ? el("a", { class: "button", href: `#job/${item.started_job}` }, "Open")
    : actionButton("Start", async () => {
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
      el("div", { class: "muted" }, [item.company, locationsText(item.postings), item.posted_at ? `posted ${localDate(item.posted_at)}` : null].filter(Boolean).join(" · "))),
    el("td", {}, el("strong", {}, String(count)), el("div", { class: "tags" }, item.matched.map((tag) => el("span", { class: "tag" }, tag)))),
    el("td", { class: "choice" }, el("div", { class: "start" }, choose, action)),
  );
}

async function renderFind() {
  const status = el("p", { class: "muted", id: "find-status" });
  const listBox = el("div", {}, el("p", { class: "muted" }, "Loading…"));
  const companiesBox = el("div");
  const inputs = {
    title: el("input", { type: "text", placeholder: "Title contains, e.g. engineer or intern", value: find.title }),
    location: el("input", { type: "text", placeholder: "Location contains, e.g. New York or Remote", value: find.location }),
  };
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
  const filterButton = el("button", { class: "secondary" }, "Filter");
  filterButton.addEventListener("click", apply);
  const refreshAll = actionButton("Refresh all", async () => {
    const { sources } = await api("/api/sources");
    await refreshNow(sources);
  }, true);

  async function drawList() {
    const params = new URLSearchParams({
      title: find.title, location: find.location, limit: String(find.limit), hide_senior: String(find.hideSenior),
    });
    const [data, { sources }] = await Promise.all([api(`/api/listings?${params}`), api("/api/sources")]);
    const newest = sources.map((source) => source.fetched_at).filter(Boolean).sort().pop();
    status.textContent = find.refreshing ? status.textContent
      : `${data.total} roles from ${sources.length} companies · updated ${ago(newest)}`;
    const notes = [];
    if (data.skill_count === 0) {
      notes.push(el("p", { class: "warning" }, "No confirmed skills yet, so nothing can be matched. ",
        el("a", { href: "#facts" }, "Confirm your facts first.")));
    }
    let body;
    if (sources.length === 0) body = el("p", {}, "You are not following any company. Add one under Companies below.");
    else if (data.total === 0) body = el("p", { class: "muted" }, find.refreshing ? "Downloading jobs…" : "No jobs match these filters.");
    else {
      body = el("table", { class: "listings" },
        el("thead", {}, el("tr", {}, el("th", {}, "#"), el("th", {}, "Job"), el("th", {}, "Your skills it mentions"), el("th", {}, ""))),
        el("tbody", {}, data.listings.map((item, index) => listingRow(item, index + 1))));
    }
    const more = data.total > data.listings.length
      ? el("div", { class: "toolbar" }, actionButton(`Show more (${data.total - data.listings.length} left)`, async () => {
        find.limit = Math.min(find.limit + 50, 200);
        await drawList();
      }, true))
      : null;
    listBox.replaceChildren(...notes, body, data.listings.length >= 200 && more ? el("p", { class: "muted" }, "Showing the top 200. Use the filters to narrow the list.") : more);
  }

  async function drawCompanies() {
    const { sources } = await api("/api/sources");
    const link = el("input", { type: "text", placeholder: "Paste a job board link, e.g. https://jobs.lever.co/palantir" });
    const add = actionButton("Add company", async () => {
      const { added } = await api("/api/sources", { method: "POST", body: JSON.stringify({ link: link.value }) });
      show(`Following ${added.company}: ${added.open} open jobs.`, "ok");
      await Promise.all([drawCompanies(), drawList()]);
    });
    const rows = sources.map((source) => {
      const remove = el("button", { class: "secondary" }, "Remove");
      remove.addEventListener("click", () => run(async () => {
        if (!window.confirm(`Stop following ${source.company}? Its downloaded jobs are removed from this list.`)) return;
        await api(`/api/sources/${source.provider}/${encodeURIComponent(source.board)}`, { method: "DELETE" });
        await Promise.all([drawCompanies(), drawList()]);
      }));
      return el("tr", {},
        el("td", {}, source.company, el("div", { class: "muted" }, `${source.provider} · ${source.board}`)),
        el("td", {}, String(source.open_count)),
        el("td", {}, ago(source.fetched_at), source.error ? el("div", { class: "bad-text" }, `Last try failed: ${source.error}`) : null),
        el("td", { class: "choice" }, remove));
    });
    companiesBox.replaceChildren(el("details", { open: sources.length === 0 },
      el("summary", {}, `Companies you follow (${sources.length})`),
      el("p", { class: "muted" }, "Jobs come from each company's public job board (Greenhouse, Lever or Ashby). No login, and nothing about you is sent."),
      el("div", { class: "toolbar" }, link, add),
      rows.length ? el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "Company"), el("th", {}, "Open jobs"), el("th", {}, "Updated"), el("th", {}, ""))),
        el("tbody", {}, rows)) : null));
  }

  async function refreshNow(sources) {
    if (!find.refreshing && sources.length) {
      find.refreshing = refreshSources(sources, (done, total) => {
        findStatus(`Downloading jobs… ${done} of ${total} companies`);
      }).finally(() => { find.refreshing = null; });
    }
    if (!find.refreshing) return;
    const failures = await find.refreshing;
    if (!listBox.isConnected) return;
    findStatus("Ranking…");
    if (failures.length) show(`${failures.length} company(ies) could not be updated. Details are under Companies.`);
    await Promise.all([drawList(), drawCompanies()]);
  }

  app.replaceChildren(
    el("section", { class: "panel find-panel" },
      el("h2", {}, "Jobs that match your skills"),
      el("p", { class: "muted" },
        "Most matched first: how many of your confirmed skills each posting mentions. ",
        "It counts words; it is not a fit score or your chance of an offer. Pending facts never count."),
      el("div", { class: "toolbar" }, inputs.title, inputs.location, filterButton,
        el("label", { class: "check", title: "Hides Senior, Staff, Principal, Lead, Manager, Director… titles. \"Member of Technical Staff\" stays." }, hideSenior, " Hide senior roles"),
        refreshAll),
      status,
      listBox),
    el("section", { class: "panel companies-panel" }, companiesBox),
  );
  await Promise.all([drawList(), drawCompanies()]);
  const { sources } = await api("/api/sources");
  await refreshNow(sources.filter((source) => source.stale));
}

const STEP_LABELS = [["decided", "Requirements"], ["cv-approved-en", "CV (EN)"], ["cv-approved-zh", "CV (ZH)"]];

function progress(steps) {
  return el("span", {}, STEP_LABELS.map(([step, label]) =>
    el("span", { class: `pill ${steps.includes(step) ? "done" : ""}` }, label)));
}

const STRENGTH_LABELS = { required: "Required", preferred: "Preferred (nice to have)", unclear: "Not marked required or preferred" };

function strengthLabel(strength) {
  return STRENGTH_LABELS[strength] || STRENGTH_LABELS.unclear;
}

// Who decided a line counts: the page by itself, or the user.
function decidedLabel(item) {
  if (item.extraction_method === "manual-quote-v1") return "added by you";
  if (item.decided_by === "user") return "reviewed by you";
  if (item.decided_by === "auto") return "counted automatically, not reviewed";
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
      el("td", {}, item.text, el("div", { class: "muted" }, [strengthLabel(item.strength), item.section, decidedLabel(item)].filter(Boolean).join(" · "))),
      el("td", { class: "choice" }, el("label", {}, confirm, " Requirement")),
      el("td", { class: "choice" }, el("label", {}, exclude, " Not a requirement")),
    );
  });
  const missed = el("input", { type: "text", placeholder: "Copy a line or phrase exactly from the job description" });
  const add = actionButton("Add missed requirement", async () => {
    await api(`/api/jobs/${view.job_id}/requirements/add`, { method: "POST", body: JSON.stringify({ text: missed.value }) });
    await refresh();
    show("Added. It counts, and the CV was prepared again.", "ok");
  }, true);
  const save = actionButton("Save", async () => {
    const confirm = [...choices].filter(([, status]) => status === "confirmed").map(([id]) => id);
    const exclude = [...choices].filter(([, status]) => status === "excluded").map(([id]) => id);
    await api(`/api/jobs/${view.job_id}/requirements/decide`, { method: "POST", body: JSON.stringify({ confirm, exclude }) });
    await refresh();
    show(`Saved: ${confirm.length} requirement(s). The CV was prepared again for them.`, "ok");
  });
  const findAgain = actionButton("Find again with DeepSeek", async () => {
    await api(`/api/jobs/${view.job_id}/requirements/find`, { method: "POST" });
    await refresh();
  }, true);
  const extraction = view.extraction || {};
  const foundBy = extraction.method === "deepseek-lines-v1"
    ? "Found by DeepSeek, which picks lines of the job description; each line is copied word for word."
    : `Found by heading rules${extraction.fallback_reason ? ` (DeepSeek not used: ${extraction.message || extraction.fallback_reason})` : ""}.`;
  const body = [
    view.steps.some((name) => name.startsWith("cv-")) ? el("p", { class: "warning" }, "Saving changes here prepares the CV again; an approved CV is moved to the job's history folder.") : null,
    rows.length ? el("table", {}, el("tbody", {}, rows)) : el("p", {}, "No requirement lines were found. Try Find again with DeepSeek, or add lines below by copying exact text from the job description."),
    el("div", { class: "toolbar" }, missed, add),
    el("div", { class: "toolbar" }, save, findAgain),
  ];
  const counted = (view.selected_requirements || []).length;
  const automatic = (view.selected_requirements || []).filter((item) => item.decided_by === "auto").length;
  const summary = `${counted} requirement(s)${automatic ? `, ${automatic} counted automatically and not reviewed by you` : ""} — open to review or change`;
  // Once requirements count, the CV is what matters; the list folds away until needed.
  return el("section", { class: "panel step-panel requirements-panel" },
    el("h2", {}, "1. What this job asks for"),
    el("p", { class: "muted" }, foundBy, " All of them count, and your CV below is adjusted to them. If a line is not really a requirement, choose Not a requirement and Save; saving also marks the list as reviewed by you."),
    counted ? el("details", {}, el("summary", {}, summary), body) : body,
  );
}

function actionButton(label, action, secondary = false) {
  const button = el("button", { class: secondary ? "secondary" : "" }, label);
  button.addEventListener("click", () => run(async () => {
    button.disabled = true;
    button.textContent = "Working…";
    try {
      await action();
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }));
  return button;
}

const CV_TITLES = { en: "English CV (US Letter)", zh: "Chinese CV (A4)" };
const CHANGE_ICONS = { order: "↕", cut: "✕", reword: "✎" };

function changeRow(view, language, change, refresh, editable) {
  const toggle = editable
    ? actionButton(change.undone ? "Redo" : "Undo", async () => {
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
      change.undone ? el("div", { class: "muted" }, "Undone: your usual CV is shown for this part.") : null),
    el("td", { class: "choice" }, toggle));
}

const STAGE_MARKS = { done: "✓", fallback: "!", failed: "✕", skipped: "–" };

// How the latest preparation went, stage by stage, so a usable CV is never mistaken for a
// tailored one.
function stageList(stages) {
  if (!stages || !stages.length) return null;
  return el("ul", { class: "stages" }, stages.map((stage) => el("li", { class: `stage ${stage.status}` },
    el("span", { class: "stage-mark" }, STAGE_MARKS[stage.status] || "·"), " ", stage.message)));
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
      stageList(cv.stages) || el("p", { class: "muted" }, "Not prepared yet."),
      el("div", { class: "toolbar" }, actionButton(cv.stages ? "Try again" : "Prepare this CV", post("prepare"))));
  }
  const approved = cv.head === "approved";
  const tools = [];
  if (cv.head === "draft") tools.push(actionButton("Reword with DeepSeek", post("tailor"), true));
  if (cv.head === "draft" || cv.head === "tailored") tools.push(actionButton("Adjust for this job", post("plan")));
  if (cv.head === "planned") tools.push(actionButton("Adjust again", post("plan"), true));
  tools.push(actionButton("Start over", post("prepare"), true));
  if (approved && !cv.final_pdf) tools.push(actionButton("Create final PDF", post("export")));
  if (cv.final_pdf) {
    tools.push(el("a", { class: "button", href: `/download/${view.job_id}/${language}.pdf?token=${encodeURIComponent(TOKEN)}` }, "Download final PDF"));
  }
  const notes = [];
  if (approved) notes.push(el("p", { class: "ok-text" }, `Approved ${new Date(cv.approved_at).toLocaleString()}. To change anything, use Start over and approve again.`));
  if (cv.stages) notes.push(stageList(cv.stages));
  else if (cv.head === "draft") notes.push(el("p", { class: "warning" }, "This is your usual CV: DeepSeek could not reword or adjust it yet. Use the buttons above."));
  else if (cv.head === "tailored") notes.push(el("p", { class: "warning" }, "Reworded, but not adjusted for this job yet: DeepSeek could not plan it. Use Adjust for this job."));
  if ((cv.language_fallbacks || []).length) {
    const missing = language === "zh" ? "Chinese" : "English";
    notes.push(el("p", { class: "warning" },
      `Shown in the other language because your profile has no ${missing} text for — `,
      cv.language_fallbacks.join("; ")));
  }
  const rewrites = (cv.rewrites || []).map((line) => ({
    id: `reword:${line.fact_id}`, type: "reword", undone: line.undone, reason: null,
    label: el("span", {}, el("span", { class: "muted" }, line.from), " → ", line.to),
  }));
  const changes = [...(cv.changes || []), ...rewrites];
  const editable = cv.head === "planned";
  if (changes.length) {
    notes.push(el("p", {}, el("strong", {}, `${changes.length} change(s) from your usual CV`),
      editable ? " — undo any you disagree with:" : ":"));
    notes.push(el("table", { class: "changes" }, el("tbody", {}, changes.map((change) => changeRow(view, language, change, refresh, editable)))));
  } else if (cv.head === "planned") {
    notes.push(el("p", { class: "muted" }, "DeepSeek kept your usual CV unchanged for this job."));
  }
  if ((cv.rejected || []).length) {
    notes.push(el("details", {}, el("summary", {}, `${cv.rejected.length} rewording(s) failed the fact check, so your own wording is kept`),
      el("table", {}, el("tbody", {}, cv.rejected.map((line) => el("tr", {},
        el("td", {}, line.rejected_text), el("td", { class: "bad-text" }, line.reasons.join("; "))))))));
  }
  let approval = null;
  if (!approved) {
    // Only the CV shown below can be approved: if another tab changed it since, the server
    // refuses and the page shows the current one to read again.
    const approve = actionButton("Approve this CV", post("approve", { expected_content_sha256: cv.content_sha256 }));
    approve.disabled = true;
    const read = el("input", { type: "checkbox" });
    read.addEventListener("change", () => { approve.disabled = !read.checked; });
    approval = el("div", { class: "approval" },
      el("label", {}, read, " I read the CV below and every change listed above."), approve);
  }
  const preview = el("iframe", {
    class: "preview", sandbox: "", title: `${title} preview`,
    src: `/preview/${view.job_id}/${language}?token=${encodeURIComponent(TOKEN)}&v=${encodeURIComponent(cv.content_sha256)}`,
  });
  return el("div", { class: "cv-block" }, el("h3", {}, title), el("div", { class: "toolbar" }, tools), notes, approval, preview);
}

function cvPanel(view, refresh) {
  const main = view.language || "en";
  const other = main === "en" ? "zh" : "en";
  return el("section", { class: "panel step-panel cv-panel" },
    el("h2", {}, "2. Your CV for this job"),
    el("p", { class: "muted" },
      "Made from your confirmed facts and adjusted for this job: the most relevant parts first, what does not help cut, ",
      "and wording that follows the job's requirements. Nothing is added, and every reworded line is fact-checked. ",
      "DeepSeek sees only your CV lines and this job's requirements, never your name, contact details, schools or employers."),
    cvBlock(view, main, refresh),
    // A second language is offered only when the resume itself is written in it.
    (view.cv_languages || []).includes(other)
      ? el("details", { class: "other-language", open: Boolean(view.cv[other].head) },
        el("summary", {}, `${CV_TITLES[other]} for this job`), cvBlock(view, other, refresh))
      : null,
  );
}

function suggestionText(suggestion) {
  if (suggestion.kind === "skill") {
    return el("span", {}, "Add to your skills line ", el("span", { class: "muted" }, `“${suggestion.where}”`), ": ",
      el("strong", {}, suggestion.items.join(", ")));
  }
  const beside = suggestion.beside || [];
  return el("span", {}, `New line under ${suggestion.where}: `, el("strong", {}, `“${suggestion.text}”`),
    beside.length ? el("span", { class: "beside muted" }, el("br"), "Already there: ",
      beside.flatMap((line, index) => [index ? " · " : "", `“${line}”`])) : null);
}

// What the CV shows for each requirement, most useful to act on first. Worked out on the
// server from the CV as it is shown now, so undoing a cut changes it at once.
const COVERAGE_GROUPS = [
  ["not_shown", "Your facts show it, but this CV leaves it out"],
  ["related", "Related only"],
  ["none", "No evidence in your confirmed facts"],
  ["unchecked", "Not checked"],
];
const COVERAGE_COUNTS = { shown: "shown", not_shown: "left out", related: "related only", none: "no evidence", unchecked: "not checked" };
const LEFT_OUT = {
  cut: "cut for this job",
  reworded: "reworded in a way that no longer shows it",
  not_on_cv: "in your confirmed facts, but not on your CV",
};
const WAY_BACK = { cut: "Put it back", reworded: "Use your own wording" };

// Undoes every change that hides the quoted words: a cut, and a rewording when the line would
// otherwise come back reworded.
function evidenceLine(view, language, line, refresh) {
  const both = line.undo.length > 1;
  const undo = line.undo.length
    ? actionButton(both ? "Put it back in your own words" : WAY_BACK[line.why] || "Undo", async () => {
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
    line.shown ? null : el("span", { class: "muted" }, ` — ${LEFT_OUT[line.why] || "not shown"}`), undo);
}

function suggestionBlock(item, post) {
  if (item.suggestion_status === "added") return el("p", { class: "ok-text" }, "Added to your facts and to this CV.");
  if (item.suggestion_status === "declined") return el("p", { class: "muted" }, "Not true for you — kept off your CV.");
  if (!item.suggestion) return el("p", { class: "muted" }, "No honest line to suggest (for example years, seniority or a degree).");
  return el("div", {},
    el("p", {}, suggestionText(item.suggestion)),
    el("div", { class: "toolbar" },
      actionButton("True for me — add it", post(`/${item.requirement_id}/accept`)),
      actionButton("Not true", post(`/${item.requirement_id}/decline`), true)));
}

// The user's own words for a requirement: skills for one of the skills lines, or a new line under
// an entry, prefilled with DeepSeek's suggestion when there is one. On a refusal the text stays.
function writeOwn(view, item, places, refresh) {
  if (item.suggestion_status === "added" || !places.length) return null;
  const suggestion = item.suggestion || {};
  const chosen = suggestion.kind === "skill" ? `skill:${suggestion.fact_id}`
    : suggestion.kind === "bullet" ? `entry:${suggestion.entry_key}` : null;
  const select = el("select", {}, places.map((place) => el("option", { value: place.id, selected: place.id === chosen },
    place.kind === "skill" ? `Add to skills line: ${place.where}` : `New line under ${place.where}`)));
  const input = el("input", { type: "text", value: suggestion.kind === "skill" ? suggestion.items.join(", ") : suggestion.text || "" });
  const hint = () => {
    input.placeholder = select.value.startsWith("skill:") ? "Skills to add, separated by commas" : "The line as it should read on your CV";
  };
  select.addEventListener("change", hint);
  hint();
  const add = actionButton("Add to my CV", async () => {
    await api(`/api/jobs/${view.job_id}/gaps/${item.requirement_id}/write`, {
      method: "POST", body: JSON.stringify({ place: select.value, text: input.value }),
    });
    await refresh();
  });
  return el("details", { class: "write-own" },
    el("summary", {}, suggestion.kind ? "Edit it, or write your own" : "Write your own line"),
    el("p", { class: "muted" }, "Only what is true for you. It is added as you write it, as a confirmed fact, and the CV is prepared again."),
    el("div", { class: "write-row" }, select, input, add));
}

function coverageRow(view, language, item, refresh, post) {
  const strength = item.strength && item.strength !== "required" ? el("span", { class: "muted auto" }, ` · ${strengthLabel(item.strength)}`) : "";
  const lines = item.evidence.length
    ? el("ul", { class: "evidence" }, item.evidence.map((line) => evidenceLine(view, language, line, refresh)))
    : null;
  const notes = [];
  if (item.status === "related" && item.missing) notes.push(el("p", { class: "muted" }, `No line shows: ${item.missing}`));
  if (item.status === "none") notes.push(el("p", { class: "muted" }, "None of your confirmed facts states this; that does not mean you lack it."));
  if (item.status === "unchecked") notes.push(el("p", { class: "muted" }, "DeepSeek gave no usable answer for this requirement, so it does not count as shown. Use Check again."));
  const gap = item.status === "related" || item.status === "none";
  return el("div", { class: `requirement ${item.status} ${gap ? item.suggestion_status : ""}` },
    el("div", { class: "requirement-text" }, item.text, strength), lines, notes,
    gap ? suggestionBlock(item, post) : null, gap ? writeOwn(view, item, view.gaps.places || [], refresh) : null);
}

function gapsPanel(view, refresh) {
  const gaps = view.gaps && !view.gaps.outdated ? view.gaps : null;
  const post = (path) => async () => {
    await api(`/api/jobs/${view.job_id}/gaps${path}`, { method: "POST" });
    await refresh();
  };
  const check = actionButton(gaps ? "Check again" : "Check now", post(""), Boolean(gaps));
  const heading = el("h2", {}, "3. What this CV shows for each requirement");
  const intro = el("p", { class: "muted" },
    "DeepSeek compares each requirement with your confirmed facts and with this CV as it is shown now, after cuts and rewording. ",
    "A line that is only about the same thing does not count as showing it. Nothing is added until you click True for me; ",
    "then it becomes a confirmed fact and the CV is prepared again.");
  if (!gaps) {
    return el("section", { class: "panel step-panel gaps-panel" }, heading, intro,
      el("p", { class: "muted" }, "Checking…"), el("div", { class: "toolbar" }, check));
  }
  const notes = [];
  if (gaps.stale) notes.push(el("p", { class: "warning" }, "Your CV or facts changed since this check. Checking again…"));
  const checking = gaps.evidence_check || {};
  if (checking.fallback_reason) {
    notes.push(el("p", { class: "warning" }, `Not checked: ${checking.message || checking.fallback_reason} `,
      "Nothing counts as shown until it is checked. Use Check again."));
  }
  if (gaps.suggesting && gaps.suggesting.fallback_reason) {
    notes.push(el("p", { class: "warning" }, `No suggestions this time: ${gaps.suggesting.message || gaps.suggesting.fallback_reason} Use Check again.`));
  }
  const language = gaps.language || view.language;
  const summary = Object.entries(COVERAGE_COUNTS)
    .filter(([status]) => gaps.counts[status])
    .map(([status, label]) => `${gaps.counts[status]} ${label}`).join(" · ");
  const groups = COVERAGE_GROUPS.map(([status, title]) => {
    const items = gaps.requirements.filter((item) => item.status === status);
    return items.length
      ? el("div", { class: "coverage-group" }, el("h3", {}, `${title} (${items.length})`),
        items.map((item) => coverageRow(view, language, item, refresh, post)))
      : null;
  });
  const shown = gaps.requirements.filter((item) => item.status === "shown");
  return el("section", { class: "panel step-panel gaps-panel" }, heading, intro, notes,
    summary ? el("p", {}, el("strong", {}, summary)) : null,
    shown.length === gaps.requirements.length && shown.length
      ? el("p", { class: "ok-text" }, "This CV shows something for every requirement.") : null,
    groups,
    shown.length
      ? el("details", { class: "coverage-group" }, el("summary", {}, `Shown in this CV (${shown.length})`),
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
      "The workbench stopped in the middle of a change to this job and could not tell which one, so it left the files as they were. "
      + "If the requirements or your CV look wrong, save the requirements again or use Start over.");
  }
  const what = step.startsWith("cv-approved-") ? "approving your CV"
    : step.startsWith("cv-final-") ? "creating the final PDF"
      : step.startsWith("cv-") ? "preparing your CV"
        : step === "gaps" ? "checking what your CV shows for each requirement"
          : step === "matches" || step === "linked" ? "finding talking points"
            : "saving this job's requirements";
  return el("p", { class: "warning" },
    `The workbench stopped while ${what}, so that change was undone and nothing half-done was kept. Do it again if you still want it.`);
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
          [jd.company, jd.location].filter(Boolean).join(" · ") || "Company unknown", " · captured ", localDate(jd.captured_at)),
        jd.source ? el("p", {}, el("a", { href: jd.source, target: "_blank", rel: "noopener noreferrer" }, jd.source)) : el("p", { class: "muted" }, "Source: unknown"),
        el("details", {}, el("summary", {}, "Job description"), el("pre", { class: "jd" }, jd.text)),
        interruptedNote(view.interrupted),
      ),
      requirementsPanel(view, refresh),
      cvPanel(view, refresh),
      gapsPanel(view, refresh),
    );
    const due = !view.gaps || view.gaps.outdated || view.gaps.stale;
    if (!checked && due && (view.selected_requirements || []).length && view.cv[view.language].head) {
      await api(`/api/jobs/${jobId}/gaps`, { method: "POST" });
      return refresh(true);
    }
    return view;
  };
  await refresh();
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

window.addEventListener("hashchange", route);
route();
