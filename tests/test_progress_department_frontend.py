"""Exercise the real list filter with cross-department decision histories."""
from pathlib import Path
import json
import subprocess


APP = Path(__file__).parents[1] / "src/pai_loop/static/app.js"


def test_progress_department_filter_history_permissions_and_links():
    script = r'''
const assert = require('node:assert/strict'), vm = require('node:vm');
const source = require('node:fs').readFileSync(0, 'utf8');
const nodes = new Map();
const context = vm.createContext({URL, URLSearchParams, Intl,
  document: {documentElement:{dataset:{}}, addEventListener(){}, getElementById(id){return nodes.get(id)||null;}},
  window:{location:new URL('https://syn.invalid/in-progress'), matchMedia(){return {matches:false};}}});
const exported = `
renderNoticeList=()=>{}; renderNoticeSearchScope=()=>{}; renderNoticeFilterTools=()=>{};
globalThis.ui={state,els,normalizeNotice,applyFilters,progressDepartmentDecisions,
operatorDecisionIndicator,renderProgressDepartmentControl,noticeFilterHref,ownDepartmentRecords};`;
vm.runInContext(source.replace(/\}\)\(\);\s*$/, exported+'\n})();'), context);
const u=context.ui;
for(const key of ['searchInput','priorityKeywordInput','eligibilityFilter','recommendationFilter',
  'operatorDecisionFilter','sortSelect','departmentSelect','operatorDecisionFilterHelp','rankingProfileVersion']) {
  u.els[key]={value:['searchInput','priorityKeywordInput'].includes(key)?'':'all',options:[{}],
    attributes:{},setAttribute(k,v){this.attributes[k]=v;}};
}
const common={value:'organization',textContent:'전사 공통 (교육·컨설팅)'};
u.els.departmentSelect.options=[common,{value:'SYN-A'},{value:'SYN-B'}];
u.els.departmentSelect.value='organization';
u.els.sortSelect.value='newest';
nodes.set('departmentSelectLabel',{});
nodes.set('noticeFilterShareHelp',{});
Object.assign(u.state,{source:'api',accessMode:'PUBLIC_READ_ONLY',loading:false,noticeSearchMode:'stored',
  currentView:'in-progress',layout:'table',accountSession:{enabled:true,authenticated:true,
    account:{id:'SYN-ACCOUNT',role:'DEPARTMENT',department_id:'SYN-A'}},
  departmentSelectionAccountId:'SYN-ACCOUNT',departmentCatalog:{departments:[{id:'SYN-A'},{id:'SYN-B'}]}});
const decision=(dept,choice,rev=1,id=dept+rev)=>({id,department_id:dept,department_name:dept==='SYN-A'?'A부서':'B<부서>',
  choice,department_revision:rev,created_at:'2026-09-30T00:00:00Z'});
const row=(key,decisions,extra={})=>u.normalizeNotice({notice_key:key,title:key,deadline:'2099-01-01T00:00:00Z',
  status:'OPEN',decisions,...extra},0,{operatorDecisionsLoaded:true});
u.state.notices=[
  row('SYN-A-ONLY',[decision('SYN-A','GO')]),
  row('SYN-B-ONLY',[decision('SYN-B','CONDITIONAL_GO')]),
  row('SYN-SHARED',[decision('SYN-A','GO'),decision('SYN-B','GO')]),
  row('SYN-CHANGED',[decision('SYN-A','GO'),decision('SYN-A','HOLD',2),decision('SYN-B','GO')]),
  row('SYN-DECLINED',[decision('SYN-B','GO'),decision('SYN-B','NO_GO',2)]),
  row('SYN-CLOSED',[decision('SYN-A','GO')],{status:'CLOSED'}),
  row('SYN-CANCELLED',[decision('SYN-A','GO')],{provider_disposition:'CANCELLED'}),
  row('SYN-RESULT',[decision('SYN-A','GO')],{has_bid_outcome:true}),
  row('SYN-LEGACY',[decision('','GO')]),
  row('SYN-NONE',[]),
];
const keys=()=>Array.from(u.state.filteredNotices,n=>n.noticeKey).sort();
u.applyFilters();
assert.deepEqual(keys(),['SYN-A-ONLY','SYN-B-ONLY','SYN-CHANGED','SYN-SHARED']);
assert.equal(nodes.get('departmentSelectLabel').textContent,'진행 부서');
assert.equal(common.textContent,'전사');
const shared=u.state.notices.find(n=>n.noticeKey==='SYN-SHARED');
assert.match(u.operatorDecisionIndicator(shared),/A부서/);
assert.match(u.operatorDecisionIndicator(shared),/B&lt;부서&gt;/);
u.els.departmentSelect.value='SYN-A';u.applyFilters();
assert.deepEqual(keys(),['SYN-A-ONLY','SYN-SHARED']);
assert.doesNotMatch(u.operatorDecisionIndicator(shared),/B&lt;부서&gt;/);
u.els.departmentSelect.value='SYN-B';u.applyFilters();
assert.deepEqual(keys(),['SYN-B-ONLY','SYN-CHANGED','SYN-SHARED']);
const bOnly=u.state.notices.find(n=>n.noticeKey==='SYN-B-ONLY');
assert.equal(bOnly.decision,'','reading another department must not replace my editable decision');
assert.equal(u.ownDepartmentRecords(bOnly.decisions).length,0);
u.els.operatorDecisionFilter.value='CONDITIONAL_GO';u.applyFilters();
assert.deepEqual(keys(),['SYN-B-ONLY']);
const link=new URL(u.noticeFilterHref());
assert.equal(link.pathname,'/in-progress');
assert.equal(link.searchParams.get('department'),'SYN-B');
assert.equal(link.searchParams.get('decision'),'CONDITIONAL_GO');
u.els.operatorDecisionFilter.value='all';
u.state.accountSession.account.role='ADMIN';u.els.departmentSelect.value='organization';u.applyFilters();
assert.equal(u.els.operatorDecisionFilter.disabled,false);
assert.equal(keys().length,4,'administrators can read all departments');
u.els.searchInput.value='SYN';u.applyFilters();
assert.equal(keys().length,4,'search must not escape the progress queue');
u.els.searchInput.value='';
u.state.notices[0].decisionReadStatus='ERROR';u.applyFilters();
assert.equal(keys().length,0,'failed/partial reads must not appear complete');
u.state.notices[0].decisionReadStatus='KNOWN';
const tie=row('SYN-TIE',[decision('SYN-A','GO',3,'a'),decision('SYN-A','HOLD',3,'z')]);
assert.equal(u.progressDepartmentDecisions(tie).length,0,'latest tie follows the server id ordering');
u.state.currentView='new';u.applyFilters();
assert.equal(nodes.get('departmentSelectLabel').textContent,'추천 부서');
assert.equal(common.textContent,'전사 공통 (교육·컨설팅)');
assert.ok(keys().includes('SYN-NONE'),'recommendation selector must not filter the general list');
u.state.currentView='in-progress';u.state.accountSession.authenticated=false;u.applyFilters();
assert.equal(keys().length,0,'unauthenticated history is never used as a progress list');
'''
    result = subprocess.run(["node", "-e", script], input=APP.read_text(encoding="utf-8"),
                            text=True, capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_progress_lists_match_dashboard_counts_for_each_department(client, monkeypatch):
    from datetime import timedelta
    from pai_loop import api
    from test_dashboard_work_queues import _notice, _decide, NOW, FixedDateTime

    monkeypatch.setattr(api, "datetime", FixedDateTime)
    a, b = "future-ai-education", "future-ai-capability"
    with client.app.state.session_factory() as session:
        shared = _notice(session, "shared-progress", None, complete=False)
        other = _notice(session, "other-progress", None, complete=False, deadline=NOW + timedelta(days=20))
        changed = _notice(session, "changed-progress", None, complete=False)
        result = _notice(session, "result-progress", None, complete=False, outcome=True)
        for notice, dept, choice, revision in [
            (shared, a, "GO", 1), (shared, b, "CONDITIONAL_GO", 1),
            (other, b, "GO", 1), (changed, a, "GO", 1),
            (changed, a, "HOLD", 2), (result, b, "GO", 1),
        ]:
            _decide(session, notice, choice, department_id=dept, revision=revision)
        session.commit()
    rows = client.get("/api/v1/notices").json()
    records = client.post("/api/v1/operator-decisions/batch-read", json={
        "notice_keys": [row["notice_key"] for row in rows],
    }).json()["decisions_by_notice"]
    for row in rows:
        row["decisions"] = records[row["notice_key"]]
    counts = {dept: client.get("/api/v1/dashboard", params={"department_id": dept}).json()["work_queue_counts"]
              for dept in ("organization", a, b)}
    assert [counts[dept]["in_progress"] for dept in ("organization", a, b)] == [2, 1, 2]
    script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8')),source=fs.readFileSync(process.argv[1],'utf8');
const now=Date.parse('2026-09-08T14:30:00Z');
class Clock extends Date {constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
const context=vm.createContext({Date:Clock,Intl,URL,URLSearchParams,
 document:{documentElement:{dataset:{}},addEventListener(){},getElementById(){return null;}},
 window:{location:{search:''},matchMedia(){return {matches:false};}}});
vm.runInContext(source.replace(/\}\)\(\);\s*$/,
 'globalThis.ui={state,els,normalizeNotice,matchesPipelineQueue};\n})();'),context);
const u=context.ui;
Object.assign(u.state,{source:'api',accessMode:'SERVER_AUTHENTICATED',accountSession:{enabled:false}});
const rows=input.rows.map(row=>u.normalizeNotice(row,0,{operatorDecisionsLoaded:true}));
for(const [department,counts] of Object.entries(input.counts)) {
 u.els.departmentSelect={value:department};
 for(const [queue,key] of [['in-progress','in_progress'],['urgent-in-progress','urgent_in_progress']]) {
   assert.equal(rows.filter(row=>u.matchesPipelineQueue(row,queue)).length,counts[key],department+' '+queue);
 }
}
'''
    result = subprocess.run(["node", "-e", script, str(APP)],
                            input=json.dumps({"rows": rows, "counts": counts}),
                            text=True, capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
