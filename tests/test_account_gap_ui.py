"""Frontend behavior with synthetic responses; real Chrome is checked separately."""
import unittest
from tests import test_sponsor_ui as ui


@unittest.skipUnless(ui.NODE, 'Node is needed for frontend behavior checks')
class AccountGapUITests(unittest.TestCase):
    check_js = ui.SponsorUITests.check_js

    def test_ai_is_first_reuses_saved_answer_and_adopts_edited_content(self):
        self.check_js(r'''
run(`api = async (path, options = {}) => { __calls.push({path, ...options}); return {}; };
__view = {job_id:"j", gaps:{places:[{id:"skill:s",kind:"skill",where:"Tools: Python"}]}};
__item = {requirement_id:"r",text:"SQL",status:"related",evidence:[],suggestion_status:"open",
 suggestion:{kind:"skill",fact_id:"s",items:["SQL"]}};`);
const row = run('coverageRow(__view,"en",__item,async()=>{},()=>{})');
assert.equal(nodes(row).find(n => n.tagName === "button").textContent, "AI suggested entry");
assert(row.textContent.indexOf("AI suggested entry") < row.textContent.indexOf("Write your own line"));
assert.equal(sandbox.__calls.length, 0);
await button(row,"AI suggested entry").fire("click");
assert.equal(sandbox.__calls.length, 0);
const input = find(row,n => n.tagName === "textarea");
assert.equal(input.value,"SQL");
input.value = "SQL, SQLite";
await button(row,"Adopt and add to my CV").fire("click");
assert.equal(sandbox.__calls.length,1);
assert.equal(sandbox.__calls[0].path,"/api/jobs/j/gaps/r/write");
assert.equal(JSON.parse(sandbox.__calls[0].body).text,"SQL, SQLite");
''')

    def test_a_suggestion_ai_entry_generated_stays_shown_after_a_reload(self):
        # Cloud V2 acceptance: after a reload the saved suggestion looked lost, the row offering
        # only AI suggested entry and Write your own line again, as if nothing had been generated.
        self.check_js(r'''
run(`api = async (path, options = {}) => { __calls.push({path, ...options}); return {}; };
__view = {job_id:"j", gaps:{places:[{id:"entry:a",kind:"bullet",where:"Other project"},{id:"entry:k",kind:"bullet",where:"Test project"}]}};
__text = "Diagnosed ambiguous service failures by testing two hypotheses and shipping the fix.";
__item = {requirement_id:"r",text:"Ambiguous challenges",status:"none",evidence:[],suggestion_status:"open",ai_checked:true,
 suggestion:{kind:"bullet",entry_key:"k",where:"Test project",text:__text}};`);
const row = run('coverageRow(__view,"en",__item,async()=>{},()=>{})');
assert(row.textContent.includes(sandbox.__text));
assert.equal(find(row, n => n.tagName === "select").value, "entry:k");
assert.equal(find(row, n => n.tagName === "textarea").value, sandbox.__text);
assert(button(row, "Adopt and add to my CV"));
assert(row.textContent.indexOf("AI suggested entry") < row.textContent.indexOf("Write your own line"));
await button(row, "AI suggested entry").fire("click");
assert.equal(sandbox.__calls.length, 0);
''')

    def test_v1_shows_a_complete_suggestion_and_adopts_exactly_what_it_shows(self):
        self.check_js(r'''
run(`api = async (path, options = {}) => { __calls.push({path, ...options}); return {}; };
__view = {job_id:"j", gaps:{places:[{id:"skill:s",kind:"skill",where:"Tools: Python"}], requirements:[]}};
__item = {requirement_id:"r",text:"SQL",status:"related",evidence:[],suggestion_status:"open",
 suggestion:{kind:"skill",fact_id:"s",items:["SQL","SQLite"],where:"Tools: Python",new_text:"Tools: Python, SQL, SQLite"}};
__post = (path, body) => async () => { await api("/api/jobs/j/gaps" + path, {method:"POST", ...(body ? {body:JSON.stringify(body)} : {})}); };`);
const row = run('coverageRow(__view,"en",__item,async()=>{},__post)');
assert(row.textContent.includes("SQL, SQLite"));
assert(row.textContent.indexOf("Adopt and add to my CV") < row.textContent.indexOf("Write your own line"));
await button(row, "Adopt and add to my CV").fire("click");
assert.deepEqual(sandbox.__calls.map(c => [c.path, JSON.parse(c.body)]),
  [["/api/jobs/j/gaps/r/write", {place:"skill:s", text:"SQL, SQLite"}]]);
await button(row, "Skip this suggestion").fire("click");
assert.equal(sandbox.__calls.at(-1).path, "/api/jobs/j/gaps/r/decline");
''', mode="v1")

    def test_old_empty_answer_regenerates_complete_content_without_erasing_manual_text(self):
        # Dogfood round 3: in V1 a gap with no saved suggestion offered only "Write your own line".
        for mode in ("v1", "v2"):
            with self.subTest(mode=mode):
                self.check_js(r'''
run(`__view={job_id:"j",gaps:{places:[{id:"entry:k",kind:"bullet",where:"Test project"}]}};
__item={requirement_id:"r",text:"3 years",status:"related",evidence:[],suggestion_status:"open",suggestion:null,ai_checked:true};
__text="Developed backend services using Python and SQL, integrating API endpoints, automated tests, and deployment monitoring into a maintainable service workflow.";
api = async (path, options={}) => { __calls.push({path,...options});
return options.method ? {} : {...__view,gaps:{...__view.gaps,requirements:[{...__item,
 suggestion:{kind:"bullet",entry_key:"k",where:"Test project",text:__text}}]}}; };`);
const row=run('coverageRow(__view,"en",__item,async()=>{},()=>{})');
const manual=find(row,n=>n.tagName==="textarea"); manual.value="My unfinished entry";
await button(row,"AI suggested entry").fire("click");
assert.equal(manual.value,"My unfinished entry");
assert(row.textContent.includes(sandbox.__text));
assert(!row.textContent.includes("template"));
const suggested=nodes(row).find(n=>n.tagName==="textarea" && n!==manual);
assert.equal(suggested.value,sandbox.__text);
assert.equal(sandbox.__calls.filter(c=>c.method==="POST").length,1);
assert.equal(sandbox.__calls[0].path,"/api/jobs/j/gaps/r/suggest");
await button(row,"AI suggested entry").fire("click");
assert.equal(sandbox.__calls.length,2);
await button(row,"Adopt and add to my CV").fire("click");
assert.equal(JSON.parse(sandbox.__calls.at(-1).body).text,sandbox.__text);
assert.equal(manual.value,"My unfinished entry");
''', mode=mode)

    def test_empty_answer_stays_retryable_instead_of_opening_a_template(self):
        for mode in ("v1", "v2"):
            with self.subTest(mode=mode):
                self.check_js(r'''
run(`__view={job_id:"j",gaps:{places:[{id:"entry:k",kind:"bullet",where:"Project"}]}};
__item={requirement_id:"r",text:"Python",status:"none",evidence:[],suggestion_status:"open",ai_checked:true,suggestion:null};
api=async(path,options={})=>{__calls.push({path,...options});return options.method?{}:{...__view,gaps:{...__view.gaps,requirements:[__item]}}};`);
const row=run('coverageRow(__view,"en",__item,async()=>{},()=>{})');
assert(row.textContent.indexOf("AI suggested entry") < row.textContent.indexOf("Write your own line"));
await button(row,"AI suggested entry").fire("click");
await button(row,"AI suggested entry").fire("click");
assert.equal(sandbox.__calls.filter(c=>c.method==="POST").length,2);
assert(!row.textContent.includes("template"));
''', mode=mode)

    def test_stale_check_asks_to_check_again_instead_of_claiming_left_out(self):
        for mode in ("v1", "v2"):
            with self.subTest(mode=mode):
                self.check_js(r'''
run(`api = async (path, options = {}) => { __calls.push({path, ...options}); return {}; };
__view = {job_id:"j", language:"en", gaps:{stale:true, language:"en", places:[], evidence_check:{}, suggesting:{},
 counts:{shown:1, recheck:1, not_shown:0, related:0, none:0, unchecked:0},
 requirements:[
  {requirement_id:"a", text:"3+ years of professional software engineering experience.", strength:"required",
   status:"recheck", missing:null, suggestion:null, suggestion_status:"open",
   evidence:[{text:"Shipped Python REST endpoints across 3+ years of backend delivery.", shown:false, why:"changed", undo:[]}]},
  {requirement_id:"b", text:"Python", strength:"required", status:"shown", missing:null, suggestion:null,
   suggestion_status:"open", evidence:[{text:"Languages: Python", shown:true, why:null, undo:[]}]}]}};`);
const panel = run('gapsPanel(__view, async () => {})');
const text = panel.textContent;
assert(text.includes("1 shown · 1 to check again"), text);
assert(text.includes("Changed since this check (1)"));
assert(text.includes("— as it read when last checked"));
assert(text.includes("whether it still shows this is not known yet"));
for (const claim of ["left out", "no longer shows it", "Use your own wording", "Put it back"]) assert(!text.includes(claim), claim);
const labels = nodes(panel).filter(n => n.tagName === "button").map(n => n.textContent);
assert.deepEqual(labels, ["Check again"]);
assert.equal(sandbox.__calls.length, 0);
for (const key of ["Changed since this check", "to check again", "as it read when last checked",
                   "Your CV changed after this check, so whether it still shows this is not known yet. Use Check again."]) {
  assert(sandbox.WorkbenchChinese[key], key);
}
''', mode=mode)

    def test_rows_to_check_again_keep_their_suggestions_marked_as_from_the_last_check(self):
        # Retest: "Missing: 3+ years" sat beside "Added to your profile and this CV". Round 3: hiding
        # every other suggestion after one adoption forced a paid check before the next one.
        for mode in ("v1", "v2"):
            with self.subTest(mode=mode):
                self.check_js(r'''
run(`api = async (path, options = {}) => { __calls.push({path, ...options}); return options.method ? {} : __view; };
__view = {job_id:"j", language:"en", gaps:{stale:true, language:"en", evidence_check:{}, suggesting:{},
 places:[{id:"skill:s", kind:"skill", where:"Frameworks and tools: FastAPI"}, {id:"entry:k", kind:"bullet", where:"Acme Labs"}],
 counts:{shown:0, recheck:5, not_shown:0, related:0, none:0, unchecked:0},
 requirements:[
  {requirement_id:"years", text:"3+ years of professional software engineering experience.", strength:"required",
   status:"recheck", verdict:"none", missing:null, evidence:[], suggestion_status:"added", ai_checked:true,
   suggestion:{kind:"bullet", entry_key:"k", where:"Acme Labs", text:"Delivered Python backend services over 3+ years."}},
  {requirement_id:"docker", text:"Experience deploying services using Docker and Kubernetes.", strength:"required",
   status:"recheck", verdict:"none", missing:null, evidence:[], suggestion_status:"open", ai_checked:true,
   suggestion:{kind:"skill", fact_id:"s", items:["Docker", "Kubernetes"], where:"Frameworks and tools: FastAPI",
               new_text:"Frameworks and tools: FastAPI, Docker, Kubernetes"}},
  {requirement_id:"passion", text:"A passion for tackling complex and ambiguous technical challenges.", strength:"required",
   status:"recheck", verdict:"related", missing:"passion for complex ambiguous challenges", suggestion_status:"declined",
   evidence:[{text:"Added automated tests and investigated API errors.", shown:false, why:"changed", undo:[]}],
   suggestion:{kind:"bullet", entry_key:"k", where:"Acme Labs", text:"Diagnosed ambiguous data-consistency issues."}},
  {requirement_id:"ci", text:"Experience with CI pipelines.", strength:"required",
   status:"recheck", verdict:"none", missing:null, evidence:[], suggestion_status:"open", suggestion:null},
  {requirement_id:"api", text:"Build Python REST APIs.", strength:"required", status:"recheck", verdict:"supported",
   missing:null, suggestion:null, suggestion_status:"open",
   evidence:[{text:"Built REST endpoints.", shown:false, why:"changed", undo:[]}]}]}};`);
const panel = run('gapsPanel(__view, async () => {})');
const text = panel.textContent;
const rowOf = (words) => nodes(panel).find(n => n.attributes?.class?.startsWith("requirement ") && n.textContent.includes(words));
const marked = "Suggested for what the last check found missing. Your CV has changed since; adopt only what you still need.";
assert(text.includes("5 to check again"), text);
assert.equal(text.split("Last check: no evidence").length - 1, 3);
assert(text.includes("Last check: related only") && text.includes("Last check: shown"));
for (const claim of ["Missing:", "Not covered in your profile", "Related only ("]) assert(!text.includes(claim), claim);
// What the user did stays said; open suggestions stay offered, marked as made before the CV changed.
assert(rowOf("3+ years").textContent.includes("Added to your profile and this CV."));
assert(rowOf("A passion").textContent.includes("Not adopted — kept off your CV."));
assert.equal(text.split(marked).length - 1, 2);
for (const words of ["Docker and Kubernetes", "CI pipelines"]) {
  const row = rowOf(words);
  assert(row.textContent.includes(marked), words);
  assert(row.textContent.indexOf(marked) < row.textContent.indexOf("Write your own line"), words);
}
assert(!rowOf("Build Python REST APIs").textContent.includes("AI suggested entry"));
assert(!rowOf("Build Python REST APIs").textContent.includes("Write your own line"));
assert.equal(sandbox.__calls.length, 0);
// Adopting sends the words shown; asking for a missing one works without checking again.
if (MODE === "v1") {
  assert(rowOf("Docker and Kubernetes").textContent.includes("Docker, Kubernetes"));
  await button(rowOf("Docker and Kubernetes"), "Adopt and add to my CV").fire("click");
} else {
  await button(rowOf("Docker and Kubernetes"), "AI suggested entry").fire("click");
  assert.equal(sandbox.__calls.length, 0);  // a saved suggestion is shown, not asked for again
  assert(rowOf("Docker and Kubernetes").textContent.includes("Docker, Kubernetes"));
  await button(rowOf("Docker and Kubernetes"), "Adopt and add to my CV").fire("click");
}
assert.deepEqual([sandbox.__calls[0].path, JSON.parse(sandbox.__calls[0].body)],
  ["/api/jobs/j/gaps/docker/write", {place:"skill:s", text:"Docker, Kubernetes"}]);
const ask = button(rowOf("CI pipelines"), "AI suggested entry");
assert(!ask.disabled);
await ask.fire("click");
assert.equal(sandbox.__calls[1].path, "/api/jobs/j/gaps/ci/suggest");
assert.equal(sandbox.WorkbenchChinese[marked], "以下建议针对上次核验发现的不足；此后简历已更新，请只采用仍需要的内容。");
''', mode=mode)

    def test_profile_page_has_no_literal_null_when_empty_or_after_saving(self):
        # Dogfood 2026-10-20: V1 has no account panel, and its null was written out as "null".
        for mode in ("v1", "v2"):
            with self.subTest(mode=mode):
                self.check_js(r'''
run(`__facts = []; api = async (path) => path === "/api/facts" ? {facts: __facts} : {languages: ["en"]};`);
await run('renderFacts()');
assert(app.textContent.includes("No profile content yet"));
assert(!app.textContent.includes("null"), app.textContent);
assert(app.children.every(child => child.tagName), "only page sections");
run(`__facts = [{id:"f", version:1, status:"pending", fact_type:"experience", text:"Built APIs", tags:["Python"]}];`);
await run('renderFacts()');
assert(app.textContent.includes("Built APIs"));
assert(!app.textContent.includes("null"), app.textContent);
assert.equal(app.children.filter(child => child.attributes?.class?.includes("account-panel")).length, MODE === "v2" ? 1 : 0);
''', mode=mode)

    def test_pending_profile_lines_are_usable_without_confirmation(self):
        self.check_js(r'''
run(`api=async(path)=>path==="/api/facts"?{facts:[{id:"f",version:1,status:"pending",fact_type:"experience",text:"Built APIs",tags:["Python"]}]}:{languages:["en"]};
accountPanel=()=>null;
location.replace=(value)=>{location.hash=value};`);
await run('renderFacts()');
assert(app.textContent.includes("Built APIs"));
assert(app.textContent.includes("available for matching and CV preparation"));
assert(!app.textContent.includes("Confirm selected"));
assert(!nodes(app).some(n=>n.attributes?.["data-ref"]));
await run('openDefault()');
assert.equal(sandbox.location.hash,"#find");
''')

    def test_quota_modes_unknown_usage_and_fetch_failure_are_distinct(self):
        self.check_js(r'''
run(`__usageBox=el("aside"); __usageBox.querySelector=()=>null;
document.getElementById=(id)=>id==="account-usage"?__usageBox:null;
__usage={mode:"tokens",configured:true,test_unlimited:false,limit:200000,remaining:199000,
resets_at:"2026-10-21T00:00:00Z",tokens:{input:700,output:100,total:800,reserved:200,pending_calls:0,unknown_calls:1}};
request=async()=>({response:{ok:true},body:{usage:__usage}});`);
await run('refreshUsage()');
assert(run('__usageBox.textContent').includes('199,000 / 200,000 tokens'));
assert(run('__usageBox.textContent').includes('input 700 · output 100'));
run('__usage.test_unlimited=true; __usage.limit=null');
await run('refreshUsage()');
assert(run('__usageBox.textContent').includes('Test account · unlimited'));
run('__usage.test_unlimited=false; __usage.configured=false');
await run('refreshUsage()');
assert(run('__usageBox.textContent').includes('not configured'));
assert(!run('__usageBox.textContent').includes('unlimited'));
run('request=async()=>{throw new Error("offline")}');
await run('refreshUsage()');
assert(run('__usageBox.textContent').includes('does not mean zero'));
''')
