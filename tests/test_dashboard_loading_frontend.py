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
 dashboardShare,formatDashboardShare,renderDashboardShare,renderDepartmentDashboard,renderDepartmentComparisonChart,selectedDashboardDepartmentId,
 applyDashboardDepartmentSelection,initializeDashboardDepartmentSelection,
 useDepartmentReloadSpy(){loadApplicationData=options=>globalThis.onDepartmentReload(options);},
 loadDashboardTotals,retryDashboardTotals,hydrateApplicationMetadata,refreshDashboardAfterMutation,
 loadApplicationData,clearAccountPrivateState,normalizeNotice,
 useBootstrap(rows){fetchNoticePages=async()=>rows;applyRuntimeProfile=()=>{};loadAccountSession=async()=>{};
  setLoading=value=>{state.loading=value;};hideDemoBanner=()=>{};openNoticeFromRoute=()=>{};
  hydrateDepartmentDecisionList=async()=>{};},
};`;
vm.runInContext(source.replace(/\}\)\(\);\s*$/,exported+"\n})();"),context);
const u=context.ui;
function element(){return {value:"",textContent:"",hidden:false,disabled:false,attributes:{},
 classList:{values:new Set(),toggle(name,enabled){if(enabled)this.values.add(name);else this.values.delete(name);},contains(name){return this.values.has(name);}},
 setAttribute(key,value){this.attributes[key]=value;},removeAttribute(key){delete this.attributes[key];},
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
const payload={totals:{notices:800,evaluations:1500,decisions:90},go_count:3,
 work_queue_counts:{fail:4,review:2,urgent:1,result_missing:1,cancelled:0,
 pending_decision:2,in_progress:3,urgent_in_progress:1,result_missing_decided:1},
 work_queue_denominator:450,cancelled_go_notices:[],
 department_statistics:{department_id:"organization",department_name:"전사 공통",total_notice_count:800,
 recommended_count:200,selected_count:80,selected_recommended_count:40,selection_available:true},
 generated_at:"2026-09-08T00:00:00Z"};
const tick=()=>new Promise(setImmediate);
function enableDashboardElements(){
 const nodes=new Map();
 const get=id=>{if(!nodes.has(id))nodes.set(id,{...element(),style:{},parentElement:{classList:element().classList}});return nodes.get(id);};
 context.document.getElementById=get;
 context.document.createElement=()=>({value:"",textContent:"",cloneNode(){return {...this};}});
 u.els.departmentSelect.children=[];
 u.els.departmentSelect.options=[{value:"organization"}];
 u.els.departmentSelect.selectedOptions=[{textContent:"전사 공통"}];
 u.els.departmentSelect.append=function(option){this.children.push(option);this.options.push(option);};
 return get;
}
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
assert.deepEqual(requests.map(r=>r.path),["/dashboard?department_id=organization","/departments/keyword-profiles","/dashboard/departments"]);
for(const id of ["kpiReview","kpiUrgent","kpiGo","kpiResultMissing"]) assert.equal(u.els[id].textContent,"—");
assert.match(u.els.dashboardSummaryTitle.textContent,/집계하고/);
assert.match(u.els.dashboardSummaryDetail.textContent,/0건이 아니라/);
assert.equal(u.els.dashboardRetryButton.hidden,true);
requests[0].reject(Error("SYN timeout"));requests[1].resolve({});requests[2].reject(Error("SYN coverage"));await pending;
assert.equal(u.state.dashboardStatus,"error");
assert.match(u.els.dashboardSummaryTitle.textContent,/확인하지 못/);
assert.equal(u.els.dashboardRetryButton.hidden,false);
assert.equal(u.state.dashboard.totalNotices,null);
const retry=u.retryDashboardTotals();await u.retryDashboardTotals();
assert.equal(requests.length,4);assert.equal(requests[3].path,"/dashboard?department_id=organization");
requests[3].resolve(payload);await retry;
assert.equal(u.state.dashboardStatus,"ready");
assert.equal(u.els.kpiReview.textContent,"2");assert.equal(u.els.kpiResultMissing.textContent,"1");
assert.equal(u.els.kpiGo.textContent,"3");
assert.equal(u.els.dashboardRetryButton.hidden,true);
assert.match(u.els.dashboardSummaryTotals.textContent,/전체 저장 공고 800건/);
assert.match(u.els.dashboardSummaryTotals.textContent,/판정 이력 1,500건/);
assert.match(u.els.dashboardSummaryDetail.textContent,/활성 공고 기준/);
assert.equal(JSON.stringify(u.state.notices),original);
assert.equal(statuses.at(-1),"online");
''')


def test_success_with_missing_aggregates_never_uses_open_list_as_global_counts():
    _run(r'''
const data=u.normalizeDashboard({totals:{notices:800}},u.state.notices);
assert.equal(data.totalNotices,800);
for(const key of ["totalEvaluations","totalDecisions","reviewCount","urgentCount","goCount","failCount","cancelledCount","resultMissingCount"])
 assert.equal(data[key],null,key);
const pending=u.loadDashboardTotals();requests[0].resolve({});await pending;
assert.equal(u.state.dashboardStatus,"partial");
assert.equal(u.state.dashboard.totalNotices,null);
assert.equal(u.els.kpiGo.textContent,"—");
assert.equal(u.els.dashboardRetryButton.hidden,false);
u.els.searchInput.value="SYN";
const scoped=u.normalizeDashboard(payload,u.state.notices);
assert.equal(scoped.totalNotices,800);assert.equal(scoped.totalEvaluations,1500);
assert.equal(scoped.failCount,4);assert.equal(scoped.resultMissingCount,1);
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
requests.find(r=>r.path.startsWith("/dashboard?")).reject(Error("SYN offline"));
requests.find(r=>r.path==="/departments/keyword-profiles").resolve({});await tick();
assert.equal(u.state.dashboardStatus,"error");
assert.equal(u.state.dashboard.totalNotices,800);assert.equal(u.state.dashboard.totalEvaluations,1500);
assert.equal(u.els.kpiReview.textContent,"2");
assert.equal(u.els.kpiReview.attributes["aria-label"],"2건 · 마지막 확인값");
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


def test_search_scope_preserves_global_counts_and_denominators():
    _run(r'''
const whole=u.normalizeDashboard(payload,u.state.notices);
assert.equal(whole.queueScope,"GLOBAL");
u.els.searchInput.value="SYN";
const entering=u.dashboardWithoutGlobalTotals(u.state.notices,whole);
assert.equal(entering.totalNotices,800);assert.equal(entering.failCount,4);
const search=u.normalizeDashboard(payload,u.state.notices);
assert.equal(search.queueScope,"GLOBAL");assert.equal(search.failCount,4);
u.els.searchInput.value="";
const leaving=u.dashboardWithoutGlobalTotals(u.state.notices,search);
assert.equal(leaving.totalNotices,800);assert.equal(leaving.totalEvaluations,1500);
for(const key of ["reviewCount","urgentCount","goCount","failCount","cancelledCount","resultMissingCount"]) assert.equal(leaving[key],whole[key],key);
const sameScope=u.dashboardWithoutGlobalTotals(u.state.notices,whole);
assert.equal(sameScope.failCount,4);assert.equal(sameScope.resultMissingCount,1);
''')


def test_dashboard_renders_global_shares_and_separate_department_denominators():
    _run(r'''
const get=enableDashboardElements();
u.state.dashboard=u.normalizeDashboard(payload,u.state.notices);u.state.dashboardStatus="ready";
u.els.searchInput.value="SYN subset";
u.renderKpis();
assert.equal(get("dashboardTotalNotices").textContent,"450");
assert.equal(u.els.kpiReview.textContent,"2");
assert.equal(u.els.kpiGo.textContent,"3");
assert.equal(u.els.kpiUrgent.textContent,"1");
assert.equal(u.els.kpiResultMissing.textContent,"1");
assert.equal(get("kpiReviewShare").textContent,"");
assert.equal(get("departmentRecommendedCount").textContent,"200");
assert.equal(get("departmentRecommendedShare").textContent,"25%");
assert.equal(get("departmentSelectedShare").textContent,"10%");
assert.equal(get("departmentSelectionRate").textContent,"20%");
assert.equal(get("departmentRecommendedPoint").attributes.cy,"170");
assert.equal(get("departmentSelectedPoint").attributes.cy,"200");
assert.equal(get("departmentRecommendedStem").attributes.y2,"170");
assert.equal(get("departmentSelectedStem").attributes.y2,"200");
assert.match(get("departmentComparisonChart").attributes["aria-label"],/전사 공통.*추천 공고 200건.*25%.*선택한 공고 80건.*10%/);
assert.match(get("departmentSelectionDetail").textContent,/추천 200건 중 40건 선택/);
assert.match(u.els.dashboardSummaryDetail.textContent,/활성 공고 기준/);
const original=JSON.stringify(payload);
u.state.dashboardStatus="loading";u.renderKpis();
assert.equal(u.els.kpiReview.textContent,"2");
assert.match(get("dashboardTotalMeta").textContent,/마지막 확인/);
assert.match(get("departmentDashboardMeta").textContent,/마지막 확인/);
assert.equal(JSON.stringify(payload),original);
''')


def test_zero_or_unknown_denominators_and_unavailable_selection_are_never_zero_percent():
    _run(r'''
const get=enableDashboardElements();
for(const [count,total] of [[0,0],[null,800],[2,null],[2,0],[-1,2],[3,2]])
 assert.equal(u.formatDashboardShare(count,total),"—");
assert.equal(u.formatDashboardShare(0,800),"0%");
assert.equal(u.formatDashboardShare(1,8000),"<0.1%");
u.state.dashboard=u.dashboardWithoutGlobalTotals(u.state.notices);u.state.dashboardStatus="loading";
u.renderKpis();
assert.equal(get("dashboardTotalNotices").textContent,"—");
assert.equal(u.els.kpiReview.textContent,"—");
assert.equal(u.els.kpiReview.attributes["aria-label"],"집계 확인 필요");
u.state.dashboard=u.normalizeDashboard({...payload,department_statistics:{...payload.department_statistics,selection_available:false}},u.state.notices);
u.state.dashboardStatus="ready";u.renderKpis();
assert.equal(get("departmentRecommendedShare").textContent,"25%");
assert.equal(get("departmentSelectedCount").textContent,"—");
assert.equal(get("departmentSelectedShare").textContent,"—");
assert.equal(get("departmentSelectionRate").textContent,"—");
assert.equal(get("departmentRecommendedPoint").attributes.visibility,"visible");
assert.equal(get("departmentSelectedPoint").attributes.visibility,"hidden");
assert.equal(get("departmentSelectedStem").attributes.visibility,"hidden");
assert.match(get("departmentComparisonEmpty").textContent,/선택한 공고 집계 확인 필요/);
assert.match(get("departmentDashboardMeta").textContent,/조회 권한 필요/);
''')


def test_department_switch_discards_old_responses_and_hides_previous_department_metrics():
    _run(r'''
const get=enableDashboardElements();
u.state.departmentSelectionAccountId="SYN-A";
u.els.departmentSelect.value="SYN-A";
const first=u.loadDashboardTotals();
assert.equal(requests[0].path,"/dashboard?department_id=SYN-A");
u.els.departmentSelect.value="SYN-B";
const second=u.loadDashboardTotals();
requests[0].resolve({...payload,department_statistics:{...payload.department_statistics,department_id:"SYN-A"}});await first;
assert.equal(u.state.dashboard.departmentStatistics,undefined);
assert.equal(get("departmentRecommendedCount").textContent,"—");
requests[1].resolve({...payload,department_statistics:{...payload.department_statistics,department_id:"SYN-B",department_name:"SYN B"}});await second;
assert.equal(u.state.dashboardStatus,"ready");
assert.match(get("departmentDashboardTitle").textContent,/SYN B/);
assert.equal(get("dashboardDepartmentSelect").value,"SYN-B");
u.els.departmentSelect.value="SYN-C";u.renderKpis();
assert.equal(get("departmentRecommendedCount").textContent,"—");
assert.equal(get("departmentSelectedShare").textContent,"—");
// Work cards stay on the last confirmed aggregate while the department reloads.
assert.equal(u.els.kpiReview.textContent,"2");
''')


def test_independent_doughnuts_show_zero_full_and_overlapping_notice_shares():
    _run(r'''
const get=enableDashboardElements();
u.renderDashboardShare("kpiReview",0,800);
assert.equal(get("kpiReviewShare").textContent,"0%");
assert.equal(get("kpiReviewProgress").attributes["stroke-dasharray"],"0 100");
assert.equal(get("kpiReviewProgress").attributes.visibility,"hidden");
assert.equal(get("kpiReviewProgress").parentElement.classList.contains("is-unavailable"),false);
u.renderDashboardShare("kpiReview",800,800);
assert.equal(get("kpiReviewProgress").attributes["stroke-dasharray"],"100 0");
assert.equal(get("kpiReviewProgress").attributes.visibility,"visible");
u.renderDashboardShare("kpiGo",600,800);
u.renderDashboardShare("kpiUrgent",700,800);
assert.equal(get("kpiGoProgress").attributes["stroke-dasharray"],"75 25");
assert.equal(get("kpiUrgentProgress").attributes["stroke-dasharray"],"87.5 12.5");
assert.equal(get("kpiGoShare").textContent,"75%");
assert.equal(get("kpiUrgentShare").textContent,"87.5%");
// GO and urgent may overlap. Each ring retains the whole database denominator.
u.renderDashboardShare("kpiReview",null,800);
assert.equal(get("kpiReviewShare").textContent,"—");
assert.equal(get("kpiReviewProgress").attributes["stroke-dasharray"],"0 100");
assert.equal(get("kpiReviewProgress").attributes.visibility,"hidden");
assert.equal(get("kpiReviewProgress").parentElement.classList.contains("is-unavailable"),true);
''')


def test_department_chart_uses_common_zero_to_one_hundred_axis_and_removes_unknown_points():
    _run(r'''
const get=enableDashboardElements();
get("departmentRecommendedPoint").setAttribute("hidden","");
get("departmentRecommendedStem").setAttribute("hidden","");
u.renderDepartmentComparisonChart("SYN 부서",0,800,800);
assert.equal(get("departmentRecommendedPoint").attributes.hidden,undefined);
assert.equal(get("departmentRecommendedStem").attributes.hidden,undefined);
assert.equal(get("departmentRecommendedPoint").attributes.cy,"220");
assert.equal(get("departmentRecommendedPoint").attributes.visibility,"visible");
assert.equal(get("departmentRecommendedStem").attributes.y2,"220");
assert.equal(get("departmentSelectedPoint").attributes.cy,"20");
assert.equal(get("departmentSelectedStem").attributes.y2,"20");
assert.match(get("departmentSelectedPointTitle").textContent,/SYN 부서.*선택한 공고 800건.*100%/);
assert.equal(get("departmentComparisonEmpty").attributes.visibility,"hidden");
u.renderDepartmentComparisonChart("SYN 부서",0,0,0);
for(const key of ["Recommended","Selected"]){
 assert.equal(get(`department${key}Point`).attributes.visibility,"hidden");
 assert.equal(get(`department${key}Point`).attributes.cy,undefined);
 assert.equal(get(`department${key}Stem`).attributes.visibility,"hidden");
 assert.equal(get(`department${key}Stem`).attributes.y2,undefined);
}
assert.equal(get("departmentComparisonEmpty").attributes.visibility,"visible");
assert.match(get("departmentComparisonChart").attributes["aria-label"],/추천 공고 집계 확인 필요.*선택한 공고 집계 확인 필요/);
u.renderDepartmentComparisonChart("SYN 부서",40,null,100);
assert.equal(get("departmentRecommendedPoint").attributes.cy,"140");
assert.equal(get("departmentSelectedPoint").attributes.visibility,"hidden");
assert.equal(get("departmentComparisonEmpty").textContent,"선택한 공고 집계 확인 필요");
''')


def test_dashboard_retains_visible_error_and_bootstrap_retry_without_the_notice_list():
    _run(r'''
const get=enableDashboardElements(),reloads=[];
context.onDepartmentReload=options=>reloads.push(options);u.useDepartmentReloadSpy();
Object.assign(u.state,{source:"error",dashboard:{},dashboardStatus:"error",sourceReason:"SYN connection unavailable"});
u.renderKpis();
assert.equal(u.els.dashboardSummary.hidden,false);
assert.match(u.els.dashboardSummaryTitle.textContent,/실데이터를 불러오지 못/);
assert.match(u.els.dashboardSummaryDetail.textContent,/SYN connection unavailable/);
assert.equal(u.els.dashboardRetryButton.hidden,false);
assert.equal(u.els.dashboardRetryButton.textContent,"서버 연결 다시 시도");
assert.equal(get("departmentRecommendedPoint").attributes.visibility,"hidden");
await u.retryDashboardTotals();
assert.equal(reloads.length,1);assert.equal(reloads[0].forceApi,true);
assert.equal(requests.length,0);
''')


def test_account_department_defaults_once_and_dashboard_selector_updates_list_ranking():
    _run(r'''
const get=enableDashboardElements(),reloads=[];
context.onDepartmentReload=options=>reloads.push(options);u.useDepartmentReloadSpy();
u.state.accountSession.account={id:"SYN-A",department_id:"SYN-DEP-A",department_name:"SYN A"};
u.initializeDashboardDepartmentSelection();
assert.equal(u.els.departmentSelect.value,"SYN-DEP-A");
assert.equal(get("dashboardDepartmentSelect").value,"SYN-DEP-A");
u.applyDashboardDepartmentSelection({target:{value:"organization"}});
assert.equal(u.els.departmentSelect.value,"organization");
assert.equal(u.els.sortSelect.value,"department");
assert.equal(reloads.length,1);assert.equal(reloads[0].forceApi,true);
u.initializeDashboardDepartmentSelection();
assert.equal(u.els.departmentSelect.value,"organization");
u.state.accountSession.account={id:"SYN-B",department_id:"SYN-DEP-B",department_name:"SYN B"};
u.initializeDashboardDepartmentSelection();
assert.equal(u.els.departmentSelect.value,"SYN-DEP-B");
assert.equal(get("dashboardDepartmentSelect").value,"SYN-DEP-B");
''')


def test_explicit_missing_system_recommendation_never_falls_back_to_a_go_risk_band():
    _run(r'''
const raw={notice_key:"SYN-NO-SNAPSHOT",title:"SYN historical band",status:"OPEN",source_kind:"MANUAL",
 analysis_state:"EVALUATED",latest_evaluation:{id:"SYN-EVAL",eligibility:"PASS",risk_band:"GO"},recommendation:null};
const current=u.normalizeNotice(raw);
assert.equal(current.storedSystemRecommendation,"UNKNOWN");
assert.equal(current.recommendation,"UNKNOWN");
assert.equal(raw.latest_evaluation.risk_band,"GO");
const legacy={...raw};delete legacy.recommendation;
assert.equal(u.normalizeNotice(legacy).recommendation,"GO");
const noEvaluation=u.normalizeNotice({notice_key:"SYN-CURRENT-SNAPSHOT",status:"OPEN",recommendation:"GO"});
assert.equal(noEvaluation.storedSystemRecommendation,"GO");
''')
