"""Behavioral regression tests for partial dashboard reads and aggregate retries."""

from pathlib import Path
import subprocess


APP = Path(__file__).parents[1] / "src/pai_loop/static/app.js"

HARNESS = r'''
const assert=require("node:assert/strict"),vm=require("node:vm");
const source=require("node:fs").readFileSync(0,"utf8");
const requests=[],statuses=[];
const now=Date.parse("2026-09-08T01:00:00Z");
class FixedDate extends Date { constructor(...args){super(...(args.length?args:[now]));} static now(){return now;} }
const context=vm.createContext({URL,URLSearchParams,Intl,Date:FixedDate,console,
 document:{documentElement:{dataset:{}},getElementById(){return null;},addEventListener(){}},
 window:{location:{search:"",href:"https://example.test/"},matchMedia(){return {matches:false};},setTimeout,clearTimeout}});
context.request=(path,options)=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject}));
context.status=value=>statuses.push(value);
const exported=`
apiRequest=(...args)=>globalThis.request(...args);
renderAll=renderKpis;renderAnalysisProgress=()=>{};
setSystemStatus=value=>globalThis.status(value);populateDepartmentProfiles=()=>{};
globalThis.ui={state,els,normalizeDashboard,dashboardWithoutGlobalTotals,renderKpis,
 loadDashboardTotals,retryDashboardTotals,hydrateApplicationMetadata,refreshDashboardAfterMutation,
 loadApplicationData,clearAccountPrivateState,normalizeNotice,
 useBootstrap(rows){fetchNoticePages=async()=>rows;applyRuntimeProfile=()=>{};loadAccountSession=async()=>{};
  setLoading=value=>{state.loading=value;};hideDemoBanner=()=>{};openNoticeFromRoute=()=>{};
  hydrateDepartmentDecisionList=async()=>{};},
};`;
vm.runInContext(source.replace(/\}\)\(\);\s*$/,exported+"\n})();"),context);
const u=context.ui;
function element(){return {value:"",textContent:"",hidden:false,disabled:false,attributes:{},
 classList:{toggle(){}},setAttribute(key,value){this.attributes[key]=value;},
 replaceChildren(){},reset(){},close(){}};}
const fields=new Map();
Object.setPrototypeOf(u.els,new Proxy({}, {get(_,key){if(!fields.has(key))fields.set(key,element());return fields.get(key);}}));
u.els.decisionInputs=[];
Object.assign(u.state,{source:"api",currentView:"all",noticeStatusScope:"OPEN",requestSequence:1,
 runtimeProfileAvailable:true,keywordProfilesAvailable:true,
 accountSession:{enabled:true,authenticated:true,account:{id:"SYN-A",role:"DEPARTMENT"},capabilities:{}}});
const notice=(id,days)=>({noticeKey:id,title:id,noticeStatus:"OPEN",sourceKind:"MANUAL",
 deadline:new FixedDate(now+days*86400000).toISOString(),qualificationStatus:"REVIEW",eligibilityStatus:"REVIEW",
 analysisState:"EVALUATED",recommendation:"UNKNOWN",requirements:[],hasBidOutcome:false});
u.state.notices=[notice("SYN-NEAR",1),notice("SYN-LATER",8)];
const payload={totals:{notices:800,evaluations:1500,decisions:90},
 work_queue_counts:{fail:4,review:2,urgent:1,result_missing:1,cancelled:0},
 generated_at:"2026-09-08T00:00:00Z"};
const tick=()=>new Promise(setImmediate);
'''


def _run(script):
    result = subprocess.run(
        ["node", "-e", HARNESS + "\n(async()=>{\n" + script
         + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"],
        input=APP.read_text(encoding="utf-8"),
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_pending_failed_counts_are_unknown_and_retry_only_reads_dashboard():
    _run(r'''
u.state.dashboard=u.dashboardWithoutGlobalTotals(u.state.notices);
const original=JSON.stringify(u.state.notices);
const pending=u.hydrateApplicationMetadata({sequence:1,requestedStatusScope:"OPEN"});
assert.deepEqual(requests.map(r=>r.path),["/dashboard","/departments/keyword-profiles"]);
assert.equal(u.els.kpiReview.textContent,"2");
assert.equal(u.els.kpiUrgent.textContent,"1");
assert.equal(u.els.kpiGo.textContent,"0");
for(const id of ["kpiNew","kpiResultMissing","kpiEnded"]) assert.equal(u.els[id].textContent,"—");
assert.match(u.els.dashboardSummaryTitle.textContent,/집계하고/);
assert.match(u.els.dashboardSummaryDetail.textContent,/0건이 아니라/);
assert.equal(u.els.dashboardRetryButton.hidden,true);
requests[0].reject(Error("SYN timeout"));requests[1].resolve({});await pending;
assert.equal(u.state.dashboardStatus,"error");
assert.match(u.els.dashboardSummaryTitle.textContent,/확인하지 못/);
assert.equal(u.els.dashboardRetryButton.hidden,false);
assert.equal(u.state.dashboard.totalNotices,null);
const retry=u.retryDashboardTotals();await u.retryDashboardTotals();
assert.equal(requests.length,3);assert.equal(requests[2].path,"/dashboard");
requests[2].resolve(payload);await retry;
assert.equal(u.state.dashboardStatus,"ready");
assert.equal(u.els.kpiNew.textContent,"4");assert.equal(u.els.kpiResultMissing.textContent,"1");
assert.equal(u.els.kpiEnded.textContent,"0");
assert.equal(u.els.dashboardRetryButton.hidden,true);
assert.match(u.els.dashboardSummaryTotals.textContent,/전체 저장 공고 800건/);
assert.match(u.els.dashboardSummaryTotals.textContent,/판정 이력 1,500건/);
assert.match(u.els.dashboardSummaryDetail.textContent,/PASS·REVIEW/);
assert.equal(JSON.stringify(u.state.notices),original);
assert.equal(statuses.at(-1),"online");
''')


def test_success_with_missing_aggregates_never_uses_open_list_as_global_counts():
    _run(r'''
const data=u.normalizeDashboard({totals:{notices:800}},u.state.notices);
assert.equal(data.reviewCount,2);assert.equal(data.totalNotices,800);
for(const key of ["totalEvaluations","totalDecisions","failCount","cancelledCount","resultMissingCount"])
 assert.equal(data[key],null,key);
const pending=u.loadDashboardTotals();requests[0].resolve({});await pending;
assert.equal(u.state.dashboardStatus,"partial");
assert.equal(u.state.dashboard.totalNotices,null);
assert.equal(u.els.kpiEnded.textContent,"—");
assert.equal(u.els.dashboardRetryButton.hidden,false);
u.els.searchInput.value="SYN";
const scoped=u.normalizeDashboard(payload,u.state.notices);
assert.equal(scoped.totalNotices,800);assert.equal(scoped.totalEvaluations,1500);
assert.equal(scoped.failCount,0);assert.equal(scoped.resultMissingCount,0);
''')


def test_full_refresh_retains_last_known_totals_and_labels_their_time():
    _run(r'''
u.state.dashboard=u.normalizeDashboard(payload,u.state.notices);u.state.dashboardStatus="ready";
u.useBootstrap([{notice_key:"SYN-REFRESHED",status:"OPEN",title:"SYN refreshed",source_kind:"MANUAL"}]);
const loading=u.loadApplicationData();
requests[0].resolve({});await loading;
assert.equal(u.state.dashboardStatus,"loading");
assert.equal(u.state.notices.length,1);assert.equal(u.state.dashboard.totalNotices,800);
assert.equal(u.state.dashboard.resultMissingCount,1);
assert.match(u.els.dashboardSummaryTotals.textContent,/마지막 확인/);
assert.match(u.els.dashboardSummaryTotals.textContent,/2026\. 09\. 08\./);
assert.match(u.els.dashboardSummaryDetail.textContent,/마지막 확인값/);
requests.find(r=>r.path==="/dashboard").reject(Error("SYN offline"));
requests.find(r=>r.path==="/departments/keyword-profiles").resolve({});await tick();
assert.equal(u.state.dashboardStatus,"error");
assert.equal(u.state.dashboard.totalNotices,800);assert.equal(u.state.dashboard.totalEvaluations,1500);
assert.equal(u.els.kpiNew.textContent,"4");
assert.equal(u.els.kpiNew.attributes["aria-label"],"4건 · 마지막 확인값");
''')


def test_dashboard_retry_discards_responses_from_old_page_or_account():
    _run(r'''
u.state.dashboard=u.dashboardWithoutGlobalTotals(u.state.notices);
const first=u.loadDashboardTotals();
u.state.requestSequence++;
const second=u.loadDashboardTotals();
requests[1].resolve(payload);await second;
requests[0].reject(Error("SYN old failure"));await first;
assert.equal(u.state.dashboardStatus,"ready");assert.equal(u.state.dashboard.totalNotices,800);
const oldAccount=u.loadDashboardTotals();
u.clearAccountPrivateState();
requests[2].resolve(payload);await oldAccount;
assert.equal(u.state.dashboardStatus,"idle");
assert.equal(u.state.dashboard.totalNotices,undefined);
assert.equal(u.state.notices.length,0);
u.state.accountSession.authenticated=false;await u.retryDashboardTotals();
assert.equal(requests.length,3);
''')


def test_mutation_refresh_failure_keeps_known_totals_and_exposes_retry():
    _run(r'''
u.state.dashboard=u.normalizeDashboard(payload,u.state.notices);u.state.dashboardStatus="ready";
const refresh=u.refreshDashboardAfterMutation();requests[0].reject(Error("SYN aggregate failure"));await refresh;
assert.equal(u.state.dashboardStatus,"error");
assert.equal(u.state.dashboard.totalNotices,800);
assert.equal(u.state.dashboard.resultMissingCount,1);
assert.equal(u.els.dashboardRetryButton.hidden,false);
assert.match(u.els.dashboardSummaryDetail.textContent,/마지막 확인값/);
''')


def test_search_scope_never_retains_local_queue_counts_as_global_totals():
    _run(r'''
const whole=u.normalizeDashboard(payload,u.state.notices);
assert.equal(whole.queueScope,"GLOBAL");
u.els.searchInput.value="SYN";
const entering=u.dashboardWithoutGlobalTotals(u.state.notices,whole);
assert.equal(entering.totalNotices,800);assert.equal(entering.failCount,null);
const search=u.normalizeDashboard(payload,u.state.notices);
assert.equal(search.queueScope,"LOCAL");assert.equal(search.failCount,0);
u.els.searchInput.value="";
const leaving=u.dashboardWithoutGlobalTotals(u.state.notices,search);
assert.equal(leaving.totalNotices,800);assert.equal(leaving.totalEvaluations,1500);
for(const key of ["failCount","cancelledCount","resultMissingCount"]) assert.equal(leaving[key],null,key);
const sameScope=u.dashboardWithoutGlobalTotals(u.state.notices,whole);
assert.equal(sameScope.failCount,4);assert.equal(sameScope.resultMissingCount,1);
''')
