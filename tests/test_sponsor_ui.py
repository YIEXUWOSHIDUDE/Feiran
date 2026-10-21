"""Exercise the shipped frontend with a small DOM double, no browser/model/network calls.

These tests cover decisions and unsafe text, not CSS layout. Responsive layout still
needs the real browser acceptance check.
"""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

HARNESS = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class TextNode {
  constructor(value) { this.nodeValue = String(value); this.children = []; }
  get textContent() { return this.nodeValue; }
}
class Element extends TextNode {
  constructor(tag) { super(""); this.tagName = tag; this.attributes = {}; this.listeners = {}; this.disabled = false; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  append(...children) { this.children.push(...children.map(x => x instanceof TextNode ? x : new TextNode(x))); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  addEventListener(name, action) { this.listeners[name] = action; }
  get textContent() { return this.children.map(x => x.textContent).join(""); }
  get value() { return this._value ?? this.attributes.value ?? (this.tagName === "select" ? (this.children.find(x => "selected" in x.attributes) || this.children[0])?.value : ""); }
  set value(value) { this._value = String(value); }
  get isConnected() { return true; }
  async fire(name) { if (!this.disabled) await this.listeners[name]?.({ preventDefault() {} }); }
}
const app = new Element("main");
const message = new Element("div");
const sandbox = {
  console, URL, URLSearchParams, Node: TextNode, setTimeout, clearTimeout,
  navigator: { language: "en" },
  location: { hash: "#jobs" },
  localStorage: { getItem() { return null; }, setItem() {} },
  document: {
    querySelector(selector) { return { content: selector.includes("mode") ? MODE : "synthetic-token" }; },
    getElementById(id) { return id === "app" ? app : id === "message" ? message : null; },
    createElement(tag) { return new Element(tag); },
    createTextNode(value) { return new TextNode(value); },
  },
  __app: app,
  __calls: [],
};
sandbox.window = sandbox;
vm.createContext(sandbox);
for (const filename of ["zh.js", "i18n.js", "app.js"]) {
  let source = fs.readFileSync("web/" + filename, "utf8");
  if (filename === "app.js") source = source.slice(0, source.lastIndexOf("\nI18n.mount();"));
  vm.runInContext(source, sandbox, {filename});
}
const run = (source) => vm.runInContext(source, sandbox);
function nodes(node) { return [node, ...node.children.flatMap(nodes)]; }
function find(node, predicate) { return nodes(node).find(predicate); }
function button(node, label) { return find(node, n => n.tagName === "button" && n.textContent === label); }
'''


@unittest.skipUnless(NODE, "Node is needed for frontend behavior checks")
class SponsorUITests(unittest.TestCase):
    def check_js(self, script, mode="v2"):
        result = subprocess.run(
            [NODE, "-e", "const MODE = " + json.dumps(mode) + ";\n" + HARNESS
             + "\n(async () => {\n" + script + "\n})().catch(error => { console.error(error); process.exitCode = 1; });"],
            cwd=REPO, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_evidence_is_plain_text_and_unknown_is_not_a_negative(self):
        self.check_js(r'''
const injected = '<img src=x onerror="alert(1)">Visa sponsorship available';
sandbox.__evidence = injected;
const card = run('sponsorshipSummary({status:"supported", evidence:[{text:__evidence}], source:"javascript:alert(1)"})');
assert(card.textContent.includes(injected));
assert(!nodes(card).some(n => n.tagName === "img" || n.tagName === "script" || n.tagName === "a"));
const unknown = run('sponsorshipSummary({status:"unknown", evidence:[]})');
assert(unknown.textContent.includes("This does not mean sponsorship is unavailable."));
for (const status of ["supported", "not_supported", "conditional", "unknown", "conflicting"]) {
  const card = run('sponsorshipSummary({status:' + JSON.stringify(status) + '})');
  assert(find(card, n => n.attributes?.class?.includes("sponsor-" + status)));
}
assert.equal(run('find.sponsorship'), "all");
''')

    def test_not_assessed_is_neither_unknown_nor_a_negative_and_answers_show_when_checked(self):
        self.check_js(r'''
const waiting = run('sponsorshipSummary({status:"not_assessed", evidence:[], source:"https://example.com/1"})');
assert(waiting.textContent.includes("Wording not checked yet"));
assert(waiting.textContent.includes("has not been checked with the current rules yet"));
assert(!waiting.textContent.includes("This does not mean sponsorship is unavailable."));
assert(find(waiting, n => n.attributes?.class?.includes("sponsor-not_assessed")));
// A status this page does not know never reads as "the rules found nothing".
const strange = run('sponsorshipSummary({status:"probably", evidence:[]})');
assert(find(strange, n => n.attributes?.class?.includes("sponsor-not_assessed")));
assert(!strange.textContent.includes("Not stated"));
assert.match(run('sponsorshipSummary({status:"unknown", evidence:[]})').textContent, /No sponsorship wording in JD/);
assert.match(run('sponsorshipSummary({status:"unknown", evidence:[{text:"Discuss visa arrangements"}]})').textContent, /Policy unclear in JD/);
const checked = run('sponsorshipSummary({status:"supported", evidence:[{text:"Visa sponsorship available"}], assessed_at:"2026-10-18T12:00:00Z", rule_version:1})');
assert(checked.textContent.includes("Wording checked"));
assert(checked.textContent.includes("sponsor rules version 1"));
assert(!run('sponsorshipSummary({status:"supported", evidence:[]})').textContent.includes("Wording checked"));
''')

    def test_explicit_sponsor_check_is_offered_only_for_an_unchecked_v2_job(self):
        for mode in ("v2", "local"):
            with self.subTest(mode=mode):
                self.check_js(r'''
run(`__view = {job_id:"saved-job",jd:{title:"Engineer",text:"Python",captured_at:"2026-10-18"},
 requirements_version:0,cv:{en:{head:null},zh:{head:null}},cv_languages:["en"],tasks:[],
 review:{sponsorship:{status:"not_assessed",evidence:[]},confirmed_fact_count:0,pending_fact_count:0,skill_matches:[]}};
 api = async (path, options = {}) => { __calls.push({path, ...options});
   if (options.method === "POST") __view.review.sponsorship = {status:"supported",evidence:[{text:"We sponsor visas."}]};
   return __view; };
 requirementsPanel = () => el("section",{},"requirements");
 cvPanel = () => el("section",{},"cv");
 gapsPanel = () => el("section",{},"gaps");`);
await run('renderJob("saved-job")');
const check = button(app, "Check the sponsor wording");
if (MODE !== "v2") { assert(!check); return; }
assert(check);
await check.fire("click");
const posts = sandbox.__calls.filter(call => call.method === "POST");
assert.equal(posts.length, 1);
assert.equal(posts[0].path, "/api/jobs/saved-job/sponsorship");
assert(app.textContent.includes("JD says sponsorship available"));
assert(!button(app, "Check the sponsor wording"));
''', mode=mode)

    def test_a_failed_sponsor_check_says_so_and_stays_unchecked(self):
        # The real api() and request() run against a fetch double answering as the server does.
        self.check_js(r'''
run(`__view = {job_id:"saved-job",jd:{title:"Engineer",text:"Python",captured_at:"2026-10-18"},
 requirements_version:0,cv:{en:{head:null},zh:{head:null}},cv_languages:["en"],tasks:[],
 review:{sponsorship:{status:"not_assessed",evidence:[]},confirmed_fact_count:0,pending_fact_count:0,skill_matches:[]}};
 globalThis.crypto = { randomUUID: () => "synthetic-action" };
 globalThis.fetch = async (path, options = {}) => { __calls.push({path, ...options});
   if (options.method === "POST") return { ok: false, status: 503, json: async () => (
     { error: "The sponsor wording could not be checked; nothing was saved. Try again later.", code: "sponsor_check_failed" }) };
   return { ok: true, status: 200, json: async () => __view }; };`);
await run('renderJob("saved-job")');
await button(app, "Check the sponsor wording").fire("click");
const posts = sandbox.__calls.filter(call => call.method === "POST");
assert.equal(posts.length, 1);
assert.equal(posts[0].path, "/api/jobs/saved-job/sponsorship");
assert(message.textContent.includes("could not be checked; nothing was saved"));
assert(app.textContent.includes("Wording not checked yet"));
assert(!app.textContent.includes("Not stated"));
assert(!app.textContent.includes("JD says sponsorship available"));
assert(button(app, "Check the sponsor wording"));
''')

    def test_each_location_shows_links_and_opens_its_own_posting(self):
        for mode in ("v2", "local"):
            with self.subTest(mode=mode):
                self.check_js(r'''
run(`api = async (path, options = {}) => { __calls.push({path, ...options}); return {job_id:"saved-boston"}; }`);
const row = run(`listingRow({provider:"lever",board:"example",company:"Example",title:"Engineer",matched:[],started_job:"saved-seattle",
 sponsorship:{status:"supported",evidence:[{text:"Boston statement"}]},postings:[
  {job_id:"boston",location:"Boston",source:"https://example.com/boston",posted_at:"2026-01-01",last_seen_at:"2026-02-01T12:00:00Z",started_job:null,
   sponsorship:{status:"supported",evidence:[{text:"Boston statement"}],source:"https://example.com/boston",captured_at:"2026-02-01T12:00:00Z"}},
  {job_id:"seattle",location:"Seattle",source:"https://example.com/seattle",posted_at:"2026-03-01",last_seen_at:"2026-04-01T12:00:00Z",started_job:"saved-seattle",
   sponsorship:{status:"conditional",evidence:[{text:"Seattle statement"}],source:"https://example.com/seattle",captured_at:"2026-04-01T12:00:00Z"}}
]}, 1)`);
const links = () => nodes(row).filter(n => n.tagName === "a");
const open = () => links().find(n => n.textContent === "Open");
const shows = (place) => {
  assert(row.textContent.includes(place + " statement"));
  assert(!row.textContent.includes((place === "Boston" ? "Seattle" : "Boston") + " statement"));
  assert(links().filter(n => n !== open()).every(n => n.attributes.href === "https://example.com/" + place.toLowerCase()));
};
const select = find(row, n => n.tagName === "select");
assert(select);  // a saved location still lets the other one be chosen
assert.equal(select.value, "1");  // the saved one is chosen first: what is shown is what opens
shows("Seattle");
assert.equal(open().attributes.href, "#job/saved-seattle");
select.value = "0";
await select.fire("change");
shows("Boston");
assert(!open());
await button(row, MODE === "v2" ? "Review job" : "Start").fire("click");
assert.equal(JSON.parse(sandbox.__calls.at(-1).body).job_id, "boston");
select.value = "1";
await select.fire("change");
shows("Seattle");
assert.equal(open().attributes.href, "#job/saved-seattle");
''', mode=mode)

    def test_changing_location_updates_evidence_source_date_and_review_request(self):
        self.check_js(r'''
run(`api = async (path, options = {}) => { __calls.push({path, ...options}); return {job_id:"saved-job"}; }`);
const row = run(`listingRow({provider:"lever",board:"example",company:"Example",title:"Engineer",matched:["Python"],postings:[
  {job_id:"one",location:"Boston",source:"https://example.com/one",posted_at:"2026-01-01",sponsorship:{status:"supported",evidence:[{text:"First statement"}],source:"https://example.com/one",captured_at:"2026-02-01T12:00:00Z"}},
  {job_id:"two",location:"Seattle",source:"https://example.com/two",posted_at:"2026-03-01",sponsorship:{status:"conditional",evidence:[{text:"Second statement"}],source:"https://example.com/two",captured_at:"2026-04-01T12:00:00Z"}}
]}, 1)`);
assert.equal(sandbox.__calls.length, 0);
const select = find(row, n => n.tagName === "select");
select.value = "1";
await select.fire("change");
assert(row.textContent.includes("Second statement"));
assert(!row.textContent.includes("First statement"));
assert(row.textContent.includes("4/1/2026"));
assert(!row.textContent.includes("2/1/2026"));
assert(nodes(row).filter(n => n.tagName === "a").every(n => n.attributes.href === "https://example.com/two"));
await button(row, "Review job").fire("click");
assert.equal(sandbox.__calls.length, 1);
const submitted = JSON.parse(sandbox.__calls[0].body);
assert.equal(submitted.job_id, "two");
assert.equal(submitted.review_only, true);
''')

    def test_job_opens_without_model_action_and_prepares_only_after_click(self):
        self.check_js(r'''
run(`__view = {job_id:"saved-job",jd:{title:"Engineer",text:"Python and sponsorship",captured_at:"2026-10-18"},
 requirements_version:0,cv:{en:{head:null},zh:{head:null}},cv_languages:["en"],tasks:[],
 review:{sponsorship:{status:"unknown",evidence:[]},confirmed_fact_count:1,pending_fact_count:2,
 skill_matches:[{tag:"Python",job_quote:"Python services",facts:[{id:"f1",version:1,text:"Built a Python service"}]}]}};
 api = async (path, options = {}) => { __calls.push({path, ...options}); return __view; };`);
await run('renderJob("saved-job")');
assert.equal(sandbox.__calls.length, 1);
assert.equal(sandbox.__calls[0].method, undefined);
assert(app.textContent.includes("Built a Python service"));
assert(app.textContent.includes("Python services"));
assert(!app.textContent.includes("1. What this job asks for"));
await button(app, "Prepare a CV for this job").fire("click");
const post = sandbox.__calls.find(call => call.method === "POST");
assert.equal(post.path, "/api/jobs/saved-job/requirements/find");
assert.equal(JSON.parse(post.body).expected_version, 0);
run('__view.review.confirmed_fact_count = 0');
await run('renderJob("saved-job")');
assert(button(app, "Prepare a CV for this job")); // legacy pending lines are usable
run('__view.review.pending_fact_count = 0');
await run('renderJob("saved-job")');
assert(!button(app, "Prepare a CV for this job"));
assert(find(app, n => n.tagName === "a" && n.attributes.href === "#facts"));
run('__view.review.confirmed_fact_count = 1; __view.cv_languages = []');
await run('renderJob("saved-job")');
assert(!button(app, "Prepare a CV for this job"));
''')

    def test_filters_reuse_company_metadata_and_refresh_invalidates_it(self):
        self.check_js(r'''
run(`api = async (path, options = {}) => {
 __calls.push({path, ...options});
 if (path === "/api/sources") return {sources:[{provider:"greenhouse",board:"acme",company:"Acme",open_count:0,stale:false}]};
 if (path.startsWith("/api/listings")) return {listings:[],total:0,skill_count:1};
 return {};
};`);
await run('renderFind()');
assert.equal(sandbox.__calls.filter(c => c.path === "/api/sources").length, 1);
await button(app, "Filter").fire("click");
assert.equal(sandbox.__calls.filter(c => c.path === "/api/sources").length, 1);
await button(app, "Refresh all").fire("click");
assert.equal(sandbox.__calls.filter(c => c.path === "/api/sources").length, 2);
assert.equal(sandbox.__calls.filter(c => c.method === "POST").length, 1);
''')

    def test_existing_cv_does_not_trigger_automatic_paid_gap_check(self):
        self.check_js(r'''
run(`__view = {job_id:"saved-job",jd:{title:"Engineer",text:"Python"},language:"en",
 requirements_version:1,cv:{en:{head:"draft"}},selected_requirements:[{text:"Python"}],tasks:[]};
 api = async (path, options = {}) => { __calls.push({path, ...options}); return __view; };
 requirementsPanel = () => el("section",{},"requirements");
 cvPanel = () => el("section",{},"cv");`);
await run('renderJob("saved-job")');
assert.equal(sandbox.__calls.length, 1);
assert(app.textContent.includes("Not checked yet."));
assert(button(app, "Check now"));
await button(app, "Check now").fire("click");
assert.equal(sandbox.__calls.filter(c => c.method === "POST" && c.path.endsWith("/gaps")).length, 1);
''')

    def test_local_existing_cv_does_not_trigger_automatic_paid_gap_check(self):
        self.check_js(r'''
run(`__view = {job_id:"saved-job",jd:{title:"Engineer",text:"Python"},language:"en",
 requirements_version:1,cv:{en:{head:"draft"}},selected_requirements:[{text:"Python"}],tasks:[]};
 api = async (path, options = {}) => { __calls.push({path, ...options}); return __view; };
 requirementsPanel = () => el("section",{},"requirements");
 cvPanel = () => el("section",{},"cv");`);
await run('renderJob("saved-job")');
assert.equal(sandbox.__calls.length, 1);
assert(app.textContent.includes("Not checked yet."));
assert(button(app, "Check now"));
await button(app, "Check now").fire("click");
assert.equal(sandbox.__calls.filter(c => c.method === "POST" && c.path.endsWith("/gaps")).length, 1);
''', mode="local")

    def test_paste_and_url_review_only_is_v2_specific(self):
        for mode in ("v2", "local"):
            with self.subTest(mode=mode):
                self.check_js(r'''
run(`api = async (path, options = {}) => { __calls.push({path, ...options}); return path === "/api/jobs" && !options.method ? {jobs:[]} : {job_id:"saved-job"}; }`);
await run('renderJobs()');
const create = button(app, MODE === "v2" ? "Save and review job" : "Create job");
await create.fire("click");
let post = sandbox.__calls.find(c => c.method === "POST");
assert.equal(JSON.parse(post.body).review_only, MODE === "v2" ? true : undefined);
sandbox.__calls.length = 0;
const form = find(app, n => n.tagName === "form");
const url = find(form, n => n.tagName === "input");
url.value = "https://example.com/one";
await form.fire("submit");
// The form submit listener starts run() without returning it, as native form handlers do.
await new Promise(resolve => setTimeout(resolve, 0));
post = sandbox.__calls.find(c => c.method === "POST");
assert.equal(post.path, "/api/jobs/from-url");
assert.equal(JSON.parse(post.body).review_only, MODE === "v2" ? true : undefined);
''', mode=mode)


if __name__ == "__main__":
    unittest.main()
