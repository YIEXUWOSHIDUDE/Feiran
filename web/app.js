"use strict";

// Every request carries the per-start token; data is only ever inserted as text.
const TOKEN = document.querySelector('meta[name="workbench-token"]').content;
const app = document.getElementById("app");
const message = document.getElementById("message");

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-Workbench-Token": TOKEN },
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
    return el("tr", { class: fact.status },
      el("td", {}, box),
      el("td", {}, el("code", {}, fact.id), el("div", { class: "muted" }, `v${fact.version} · ${fact.fact_type}`)),
      el("td", {}, fact.text, el("div", { class: "tags" }, fact.tags.map((tag) => el("span", { class: "tag" }, tag)))),
      el("td", {}, el("span", { class: `status ${fact.status}` }, fact.status)),
    );
  });
  app.replaceChildren(
    el("section", { class: "panel" },
      el("h2", {}, "Facts"),
      el("p", { class: "muted" },
        `${facts.length} facts, ${pending.length} pending. Only confirmed facts can be matched or used in a CV. `,
        "Read each one carefully before confirming."),
      facts.length === 0
        ? el("p", {}, "No facts yet. Import them with: python3 facts.py import .local/my-facts.json")
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
    el("section", { class: "panel" }, el("h2", {}, "Jobs"), list),
    el("section", { class: "panel" },
      el("h2", {}, "New job"),
      el("p", { class: "muted" }, "Paste a job description from any site. Requirements are found automatically; you confirm them next."),
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
    el("section", { class: "panel" },
      el("h2", {}, "Jobs that match your skills"),
      el("p", { class: "muted" },
        "Most matched first: how many of your confirmed skills each posting mentions. ",
        "It counts words; it is not a fit score or your chance of an offer. Pending facts never count."),
      el("div", { class: "toolbar" }, inputs.title, inputs.location, filterButton,
        el("label", { class: "check", title: "Hides Senior, Staff, Principal, Lead, Manager, Director… titles. \"Member of Technical Staff\" stays." }, hideSenior, " Hide senior roles"),
        refreshAll),
      status,
      listBox),
    el("section", { class: "panel" }, companiesBox),
  );
  await Promise.all([drawList(), drawCompanies()]);
  const { sources } = await api("/api/sources");
  await refreshNow(sources.filter((source) => source.stale));
}

const STEP_LABELS = [
  ["decided", "Requirements"], ["linked", "Matching"],
  ["cv-approved-en", "CV (EN)"], ["cv-approved-zh", "CV (ZH)"],
];

function progress(steps) {
  return el("span", {}, STEP_LABELS.map(([step, label]) =>
    el("span", { class: `pill ${steps.includes(step) ? "done" : ""}` }, label)));
}

function laterStepsExist(view, step) {
  const order = ["candidates", "decided", "matches", "linked"];
  return view.steps.some((name) => name.startsWith("cv-") || order.indexOf(name) > order.indexOf(step));
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
      el("td", {}, item.text, el("div", { class: "muted" }, item.extraction_method === "manual-quote-v1" ? "added by you" : (item.section || ""))),
      el("td", { class: "choice" }, el("label", {}, confirm, " Requirement")),
      el("td", { class: "choice" }, el("label", {}, exclude, " Not a requirement")),
    );
  });
  const missed = el("input", { type: "text", placeholder: "Copy a line or phrase exactly from the job description" });
  const add = el("button", { class: "secondary" }, "Add missed requirement");
  add.addEventListener("click", () => run(async () => {
    await api(`/api/jobs/${view.job_id}/requirements/add`, { method: "POST", body: JSON.stringify({ text: missed.value }) });
    await refresh();
    show("Added. It starts undecided; choose Requirement or Not a requirement.", "ok");
  }));
  const save = el("button", {}, "Save decisions");
  save.addEventListener("click", () => run(async () => {
    const confirm = [...choices].filter(([, status]) => status === "confirmed").map(([id]) => id);
    const exclude = [...choices].filter(([, status]) => status === "excluded").map(([id]) => id);
    await api(`/api/jobs/${view.job_id}/requirements/decide`, { method: "POST", body: JSON.stringify({ confirm, exclude }) });
    await refresh();
    show(`Saved: ${confirm.length} requirement(s) confirmed.`, "ok");
  }));
  const selected = view.selected_requirements || [];
  return el("section", { class: "panel" },
    el("h2", {}, "1. Requirements"),
    el("p", { class: "muted" }, "Lines found under requirement headings, copied word for word. Decide which are real requirements."),
    laterStepsExist(view, "decided") ? el("p", { class: "warning" }, "Changing requirements resets matching and CV steps for this job. Old versions are kept in the job's history folder.") : null,
    rows.length ? el("table", {}, el("tbody", {}, rows)) : el("p", {}, "No requirement lines were found. Add them below by copying exact text from the job description."),
    el("div", { class: "toolbar" }, missed, add),
    el("div", { class: "toolbar" }, save),
    selected.length ? el("p", { class: "ok-text" }, `${selected.length} requirement(s) confirmed.`) : null,
  );
}

function matchingPanel(view, refresh) {
  const title = el("h2", {}, "2. Matching");
  const selected = view.selected_requirements || [];
  if (selected.length === 0) {
    return el("section", { class: "panel" }, title, el("p", { class: "muted" }, "Confirm at least one requirement first."));
  }
  const propose = el("button", { class: view.matching ? "secondary" : "" }, view.matching ? "Search again" : "Find matching facts");
  propose.addEventListener("click", () => run(async () => {
    await api(`/api/jobs/${view.job_id}/matches/propose`, { method: "POST" });
    await refresh();
  }));
  if (!view.matching) {
    return el("section", { class: "panel" }, title,
      el("p", { class: "muted" }, "Looks up your confirmed facts whose tags appear in each requirement. Pending facts are never offered."),
      el("div", { class: "toolbar" }, propose));
  }
  const choices = new Map();
  const blocks = view.matching.requirements.map((requirement) => {
    const decision = requirement.decision;
    if (decision) choices.set(requirement.requirement_id, decision.status === "linked" ? decision.fact_id : "__none__");
    const option = (value, label, detail) => {
      const input = el("input", { type: "radio", name: `match-${requirement.requirement_id}`, checked: choices.get(requirement.requirement_id) === value });
      input.addEventListener("change", () => choices.set(requirement.requirement_id, value));
      return el("label", { class: "option" }, input, el("span", {}, label, detail ? el("div", { class: "muted" }, detail) : null));
    };
    return el("div", { class: "requirement" },
      el("div", { class: "requirement-text" }, requirement.text),
      requirement.candidates.length === 0 ? el("p", { class: "muted" }, "No confirmed fact has a matching tag.") : null,
      requirement.candidates.map((fact) => option(fact.fact_id, fact.fact_text,
        `${fact.fact_id} · ${fact.fact_type} · matched: ${(fact.retrieval_basis.matched_tags || []).join(", ") || "type only"}`)),
      option("__none__", "No matching fact", "Recorded as unknown; nothing is invented."),
    );
  });
  const save = el("button", {}, "Save matches");
  save.addEventListener("click", () => run(async () => {
    const links = {};
    const noMatch = [];
    for (const [requirementId, value] of choices) {
      if (value === "__none__") noMatch.push(requirementId); else links[requirementId] = value;
    }
    await api(`/api/jobs/${view.job_id}/matches/decide`, { method: "POST", body: JSON.stringify({ links, no_match: noMatch }) });
    await refresh();
    show("Matches saved.", "ok");
  }));
  const report = view.report ? el("div", {},
    el("h3", {}, "Evidence"),
    el("table", {}, el("tbody", {}, view.report.items.map((item) => el("tr", {},
      el("td", {}, item.requirement_quote),
      el("td", {}, item.fact_quote || el("span", { class: "muted" }, "Unknown — no fact linked")),
    ))))) : null;
  return el("section", { class: "panel" }, title,
    view.steps.some((name) => name.startsWith("cv-")) ? el("p", { class: "warning" }, "Changing matches resets this job's CV steps. Old versions stay in history.") : null,
    blocks,
    el("div", { class: "toolbar" }, save, propose),
    report,
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

function cvBlock(view, language, title, refresh) {
  const cv = view.cv[language];
  const step = (action) => async () => {
    await api(`/api/jobs/${view.job_id}/cv/${language}/${action}`, { method: "POST" });
    await refresh();
  };
  const tools = [actionButton(cv.head ? "Rebuild draft" : "Build draft", step("draft"), Boolean(cv.head))];
  if (cv.head === "draft") {
    tools.push(actionButton("Tailor with DeepSeek", step("tailor"), true));
  }
  let approval = null;
  if (cv.head === "draft" || cv.head === "tailored") {
    const approve = actionButton("Approve this version", step("approve"));
    approve.disabled = true;
    const read = el("input", { type: "checkbox" });
    read.addEventListener("change", () => { approve.disabled = !read.checked; });
    approval = el("div", { class: "approval" },
      el("label", {}, read, " I read every line of the preview below, including the DeepSeek changes."),
      approve);
  }
  if (cv.head === "approved" && !cv.final_pdf) tools.push(actionButton("Create final PDF", step("export")));
  if (cv.final_pdf) {
    tools.push(el("a", { class: "button", href: `/download/${view.job_id}/${language}.pdf?token=${encodeURIComponent(TOKEN)}` }, "Download final PDF"));
  }
  const notes = [];
  if (cv.head === "approved") notes.push(el("p", { class: "ok-text" }, `Approved ${new Date(cv.approved_at).toLocaleString()}. Any later change needs a new draft and a new approval.`));
  if ((cv.language_fallbacks || []).length) {
    const missing = language === "zh" ? "Chinese" : "English";
    notes.push(el("p", { class: "warning" },
      `Shown in the other language because your profile has no ${missing} text for — `,
      cv.language_fallbacks.join("; ")));
  }
  if (cv.tailoring) {
    notes.push(el("p", { class: "muted" }, `DeepSeek (${cv.tailoring.model}): ${cv.tailoring.accepted} line(s) passed the checks, ${cv.tailoring.rejected} rejected and kept as your original text.`));
    if (cv.rewrites.length) {
      notes.push(el("h3", {}, "Lines DeepSeek changed — check each one"),
        el("table", {}, el("tbody", {}, cv.rewrites.map((line) => el("tr", {},
          el("td", { class: "muted" }, line.from), el("td", {}, "→"), el("td", {}, line.to))))));
    }
    if (cv.rejected.length) {
      notes.push(el("h3", {}, "Rejected rewrites (your original line is used)"),
        el("table", {}, el("tbody", {}, cv.rejected.map((line) => el("tr", {},
          el("td", {}, line.rejected_text), el("td", { class: "bad-text" }, line.reasons.join("; ")))))));
    }
  }
  const preview = cv.head ? el("iframe", {
    class: "preview", sandbox: "", title: `${title} preview`,
    src: `/preview/${view.job_id}/${language}?token=${encodeURIComponent(TOKEN)}&v=${Date.now()}`,
  }) : el("p", { class: "muted" }, "No draft yet.");
  return el("div", { class: "cv-block" },
    el("h3", {}, title),
    el("div", { class: "toolbar" }, tools),
    notes, approval, preview);
}

function cvPanel(view, refresh) {
  return el("section", { class: "panel" },
    el("h2", {}, "3. CV"),
    el("p", { class: "muted" },
      "Built only from your confirmed facts; facts matched to this job come first. ",
      "DeepSeek receives only the bullet lines and this job's requirements, never your name or contact details."),
    cvBlock(view, "en", "English CV (US Letter)", refresh),
    cvBlock(view, "zh", "Chinese CV (A4)", refresh),
  );
}

async function renderJob(jobId) {
  const refresh = async () => {
    const view = await api(`/api/jobs/${jobId}`);
    const jd = view.jd;
    app.replaceChildren(
      el("section", { class: "panel" },
        el("h2", {}, jd.title || jobId),
        el("p", { class: "muted" },
          [jd.company, jd.location].filter(Boolean).join(" · ") || "Company unknown", " · captured ", localDate(jd.captured_at)),
        jd.source ? el("p", {}, el("a", { href: jd.source, target: "_blank", rel: "noopener noreferrer" }, jd.source)) : el("p", { class: "muted" }, "Source: unknown"),
        el("details", {}, el("summary", {}, "Job description"), el("pre", { class: "jd" }, jd.text)),
      ),
      requirementsPanel(view, refresh),
      matchingPanel(view, refresh),
      cvPanel(view, refresh),
    );
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
