"""Deterministic response ordering and transient draft regressions, alongside browser QA."""
import json
import subprocess
import unittest
from tests.test_sponsor_ui import HARNESS, NODE, REPO


@unittest.skipUnless(NODE, "Node needed")
class PageStateUITests(unittest.TestCase):
    def check_js(self, script):
        result = subprocess.run([NODE, "-e", 'const MODE="v2";\n' + HARNESS + '\n(async()=>{\n' + script
                                 + '\n})().catch(e=>{console.error(e);process.exitCode=1});'],
                                cwd=REPO, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_poll_preserves_words_original_guard_and_failed_save(self):
        self.check_js(r'''
run(`crypto={randomUUID:()=>"key"}; __view={job_id:"one"};
__cv={lines:[{block:"b",text:"old",where:"Project"}]};
api=async(path,options)=>{__calls.push(JSON.parse(options.body));throw Error("conflict")};`);
let editor=run('cvLineEditor(__view,"en",__cv,async()=>{})');
let input=find(editor,n=>n.tagName==="textarea");
input.value="unsaved"; await input.fire("input");
editor.open=true; await editor.fire("toggle");
run('__cv.lines[0].text="new from another tab"');
editor=run('cvLineEditor(__view,"en",__cv,async()=>{})');
assert.equal(editor.open,true);
input=find(editor,n=>n.tagName==="textarea");
assert.equal(input.value,"unsaved");
await button(editor,"Save in this CV").fire("click");
assert.deepEqual(JSON.parse(JSON.stringify(sandbox.__calls[0])),{text:"unsaved",expected_text:"old"});
assert.equal(input.value,"unsaved"); assert(message.textContent.includes("conflict"));
// A second language/job must not borrow this draft.
const other=run('cvLineEditor(__view,"zh",__cv,async()=>{})');
assert.equal(find(other,n=>n.tagName==="textarea").value,"new from another tab");
run('__cv.lines=[]');
const missing=run('cvLineEditor(__view,"en",__cv,async()=>{})');
assert.equal(find(missing,n=>n.tagName==="textarea").value,"unsaved");
assert(missing.textContent.includes("original line is unavailable"));
''')

    def test_manual_entry_keeps_its_check_binding_across_refresh(self):
        self.check_js(r'''
run(`crypto={randomUUID:()=>"key"};
__view={job_id:"one",gaps:{language:"en",version:1}};
__item={requirement_id:"req"}; __places=[{id:"skill:x",kind:"skill",where:"Tools"}];
api=async(path,options)=>{__calls.push(JSON.parse(options.body));throw Error("stale check")};`);
let editor=run('writeOwn(__view,__item,__places,async()=>{})');
let input=find(editor,n=>n.tagName==="textarea");
input.value="PostgreSQL"; await input.fire("input");
run('__view.gaps.version=2');
editor=run('writeOwn(__view,__item,__places,async()=>{})');
assert.equal(find(editor,n=>n.tagName==="textarea").value,"PostgreSQL");
await button(editor,"Add to my CV").fire("click");
assert.equal(sandbox.__calls[0].gaps_version,1);
assert(message.textContent.includes("stale check"));
''')

    def test_typing_while_save_waits_survives_success(self):
        self.check_js(r'''
run(`crypto={randomUUID:()=>"key"}; __view={job_id:"one"};
__cv={lines:[{block:"b",text:"old"}]}; api=()=>new Promise(resolve=>{__resolve=resolve});`);
let editor=run('cvLineEditor(__view,"en",__cv,async()=>{})');
let input=find(editor,n=>n.tagName==="textarea");
input.value="first"; await input.fire("input");
const pending=button(editor,"Save in this CV").fire("click");
input.value="continued typing"; await input.fire("input");
run('__resolve({}); __cv.lines[0].text="first"'); await pending;
editor=run('cvLineEditor(__view,"en",__cv,async()=>{})');
input=find(editor,n=>n.tagName==="textarea");
assert.equal(input.value,"continued typing");
assert.equal(run('editorDrafts.get("cv:one:en:b").expected'),"first");
''')

    def test_late_job_response_does_not_overwrite_new_route_even_after_return(self):
        self.check_js(r'''
run(`location.hash="#job/one";
api=()=>new Promise(resolve=>{__resolve=resolve});`);
const pending=run('renderJob("one")');
run('location.hash="#jobs"; pageGeneration++; app.replaceChildren(el("h2",{},"Latest page"))');
run('__resolve({jd:{title:"old"}})'); await pending;
assert.equal(app.textContent,"Latest page");
const pending2=run('renderJob("one")');
run('pageGeneration++; location.hash="#job/one"; pageGeneration++; location.hash="#jobs"');
run('__resolve({jd:{title:"old"}})'); await pending2;
assert.equal(app.textContent,"Latest page");
''')

    def test_find_latest_filter_wins_with_reversed_responses(self):
        self.check_js(r'''
run(`location.hash="#find"; __reads=[];
api=async(path)=>{
 if(path==="/api/sources")return {sources:[{company:"Synthetic",provider:"lever",board:"test",open_count:0}]};
 if(path.startsWith("/api/listings"))return new Promise(resolve=>__reads.push({path,resolve}));
 return {};
};`);
const initial=run('renderFind()');
await Promise.resolve();
const filter=button(app,"Filter"); assert(filter);
const title=find(app,n=>n.attributes?.placeholder?.startsWith("Title contains"));
title.value="latest";
const next=filter.fire("click");
run('__reads[1].resolve({total:0,skill_count:1,listings:[]})'); await next;
run('__reads[0].resolve({total:99,skill_count:1,listings:[]})'); await initial;
assert(app.textContent.includes("0 roles")); assert(!app.textContent.includes("99 roles"));
''')
