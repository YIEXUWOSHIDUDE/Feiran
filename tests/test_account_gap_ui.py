"""Frontend behavior with synthetic responses; real Chrome is checked separately."""
import unittest
from tests import test_sponsor_ui as ui


@unittest.skipUnless(ui.NODE, 'Node is needed for frontend behavior checks')
class AccountGapUITests(unittest.TestCase):
    check_js = ui.SponsorUITests.check_js

    def test_ai_is_first_reuses_saved_answer_and_waits_for_human_confirmation(self):
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
const input = find(row,n => n.tagName === "input");
assert.equal(input.value,"SQL");
input.value = "SQL, SQLite";
await button(row,"Confirm this is true — add to my CV").fire("click");
assert.equal(sandbox.__calls.length,1);
assert.equal(sandbox.__calls[0].path,"/api/jobs/j/gaps/r/write");
assert.equal(JSON.parse(sandbox.__calls[0].body).text,"SQL, SQLite");
''')

    def test_missing_facts_still_give_an_editable_template_without_erasing_manual_text(self):
        self.check_js(r'''
run(`__view={job_id:"j",gaps:{places:[{id:"entry:k",kind:"bullet",where:"Test project"}]}};
__item={requirement_id:"r",text:"3 years",status:"related",evidence:[],suggestion_status:"open",suggestion:null};
api = async (path, options={}) => { __calls.push({path,...options});
return options.method ? {} : {...__view,gaps:{...__view.gaps,requirements:[{...__item,ai_checked:true,ai_needs_details:true,
 guidance:{source:"reference",questions:["What were the dates of your relevant roles?"],template:"[[start date]]–[[end date]]: [[role]]."}}]}}; };`);
const row=run('coverageRow(__view,"en",__item,async()=>{},()=>{})');
const manual=find(row,n=>n.tagName==="input"); manual.value="My unfinished entry";
await button(row,"AI suggested entry").fire("click");
assert.equal(manual.value,"My unfinished entry");
assert(row.textContent.includes("What were the dates of your relevant roles?"));
const template=nodes(row).find(n=>n.tagName==="input" && n!==manual);
assert.equal(template.value,"[[start date]]–[[end date]]: [[role]].");
await button(row,"Confirm this is true — add to my CV").fire("click");
assert(!sandbox.__calls.some(c=>c.path.endsWith("/write")));
assert.equal(sandbox.__calls.filter(c=>c.method==="POST").length,1);
await button(row,"AI suggested entry").fire("click");
assert.equal(sandbox.__calls.length,2);
template.value="June–August: backend intern, writing service tests.";
await button(row,"Confirm this is true — add to my CV").fire("click");
assert.equal(JSON.parse(sandbox.__calls.at(-1).body).text,template.value);
assert.equal(manual.value,"My unfinished entry");
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
