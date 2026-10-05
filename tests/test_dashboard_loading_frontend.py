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
 departmentComparisonRows,selectDepartmentComparison,initializeDashboardDepartmentSelection,loadDepartmentCoverage,
 useDepartmentReloadSpy(){loadApplicationData=options=>globalThis.onDepartmentReload(options);},
 loadDashboardTotals,retryDashboardTotals,hydrateApplicationMetadata,refreshDashboardAfterMutation,
 loadApplicationData,clearAccountPrivateState,normalizeNotice,
 useBootstrap(rows){fetchNoticePages=async()=>rows;applyRuntimeProfile=()=>{};loadAccountSession=async()=>{};
  setLoading=value=>{state.loading=value;};hideDemoBanner=()=>{};openNoticeFromRoute=()=>{};
  hydrateDepartmentDecisionList=async()=>{};},
 useNoticePages(fetcher){fetchNoticePages=fetcher;},
};`;
vm.runInContext(source.replace(/\}\)\(\);\s*$/,exported+"\n})();"),context);
const u=context.ui;
function element(){return {value:"",textContent:"",hidden:false,disabled:false,attributes:{},style:{},dataset:{},children:[],
 classList:{values:new Set(),toggle(name,enabled){if(enabled)this.values.add(name);else this.values.delete(name);},contains(name){return this.values.has(name);}},
 setAttribute(key,value){this.attributes[key]=value;},removeAttribute(key){delete this.attributes[key];},
 append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},querySelector(){return null;},focus(){},reset(){},close(){}};}
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
 context.document.createElement=()=>({...element(),cloneNode(){return {...this};}});
 context.CSS={escape:value=>value};
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
assert.match(u.els.dashboardSummaryDetail.textContent,/마감 전 공고, 결과 입력은 개찰 후 공고 기준/);
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
const refresh=u.refreshDashboardAfterMutation();
requests.find(r=>r.path.startsWith("/dashboard?")).reject(Error("SYN aggregate failure"));
requests.find(r=>r.path==="/dashboard/departments").reject(Error("SYN comparison failure"));await refresh;
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
assert.equal(get("departmentComparisonChart").children.length,0);
assert.match(get("departmentSelectionDetail").textContent,/추천 200건 중 40건 선택/);
assert.match(u.els.dashboardSummaryDetail.textContent,/마감 전 공고, 결과 입력은 개찰 후 공고 기준/);
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
assert.equal(get("departmentComparisonChart").children.length,0);
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
assert.equal(get("dashboardDepartmentAccount").textContent,"전사 계정");
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


def test_department_bars_pin_own_and_top_four_with_independent_click_details():
    _run(r'''
const get=enableDashboardElements();
u.state.accountSession.account={id:"SYN-A",department_id:"SYN-own",department_name:"SYN 우리 부서"};
u.els.departmentSelect.value="organization";
u.state.dashboard=u.normalizeDashboard(payload,u.state.notices);
const row=(id,rec,sel)=>({department_id:id,department_name:id,recommended_count:rec,selected_count:sel,
 selected_recommended_count:Math.min(rec,sel),selection_available:true});
const rows=[row("SYN-own",27,1),row("SYN-a",34,8),row("SYN-b",24,7),row("SYN-c",18,6),row("SYN-d",0,4),row("SYN-e",30,3)];
u.state.departmentCoverage={status:"ready",data:{total_notice_count:100,departments:rows,generated_at:payload.generated_at}};
u.renderDepartmentDashboard(u.state.dashboard.departmentStatistics);
const groups=()=>get("departmentComparisonChart").children[1].children;
assert.deepEqual(Array.from(groups(),b=>b.dataset.departmentId),["SYN-own","SYN-a","SYN-b","SYN-c","SYN-d"]);
assert.equal(get("departmentRecommendedCount").textContent,"27");
assert.equal(groups()[0].attributes["aria-pressed"],"true");
const listBefore=u.els.departmentSelect.value;
groups()[3].onclick();
assert.equal(get("departmentRecommendedCount").textContent,"18");
assert.equal(get("departmentSelectedCount").textContent,"6");
assert.equal(get("departmentSelectionRate").textContent,"33.3%");
assert.equal(groups()[3].attributes["aria-pressed"],"true");
assert.equal(groups()[0].dataset.departmentId,"SYN-own");
assert.equal(u.els.departmentSelect.value,listBefore);
assert.equal(requests.length,0);
// Zero stays zero; independent bars must not clamp selections to recommendations.
const pair=groups()[4].children[0].children;
assert.equal(pair[0].style.height,"0%");
assert.ok(parseFloat(pair[1].style.height)>0);
const max=Math.max(...groups().flatMap(b=>b.children[0].children.map(v=>parseFloat(v.style.height))));
assert.ok(max<=100);
rows[0].selected_count=100;u.renderDepartmentDashboard(u.state.dashboard.departmentStatistics);
assert.equal(groups().filter(b=>b.dataset.departmentId==="SYN-own").length,1);
assert.equal(groups().length,5);
// A missing own selection is visibly unknown, never zero or a fabricated ranking.
rows[0].selection_available=false;u.selectDepartmentComparison("SYN-own");
assert.equal(get("departmentSelectedCount").textContent,"—");
assert.equal(groups()[0].children[0].children[1].children[0].textContent,"—");
u.state.departmentCoverage.status="error";u.renderDepartmentDashboard(u.state.dashboard.departmentStatistics);
assert.equal(get("departmentComparisonRetry").hidden,false);
assert.match(get("departmentComparisonStatus").textContent,/마지막 확인/);
u.clearAccountPrivateState();
assert.equal(u.state.departmentComparisonId,null);
assert.equal(u.state.departmentCoverage.data,null);
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
assert.equal(get("departmentComparisonChart").children.length,0);
await u.retryDashboardTotals();
assert.equal(reloads.length,1);assert.equal(reloads[0].forceApi,true);
assert.equal(requests.length,0);
''')


def test_account_department_defaults_once_and_account_label_stays_fixed():
    _run(r'''
const get=enableDashboardElements(),reloads=[];
context.onDepartmentReload=options=>reloads.push(options);u.useDepartmentReloadSpy();
u.state.accountSession.account={id:"SYN-A",department_id:"SYN-DEP-A",department_name:"SYN A"};
u.initializeDashboardDepartmentSelection();
assert.equal(u.els.departmentSelect.value,"SYN-DEP-A");
assert.equal(get("dashboardDepartmentAccount").textContent,"SYN A");
u.els.departmentSelect.value="organization";
assert.equal(u.els.departmentSelect.value,"organization");
assert.equal(reloads.length,0);
u.initializeDashboardDepartmentSelection();
assert.equal(u.els.departmentSelect.value,"organization");
u.state.accountSession.account={id:"SYN-B",department_id:"SYN-DEP-B",department_name:"SYN B"};
u.initializeDashboardDepartmentSelection();
assert.equal(u.els.departmentSelect.value,"SYN-DEP-B");
assert.equal(get("dashboardDepartmentAccount").textContent,"SYN B");
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


def test_first_notice_page_renders_before_remaining_pages_and_dashboard():
    _run(r"""
const row=i=>({notice_key:"SYN-P"+i,status:"OPEN",title:"SYN page "+i,source_kind:"MANUAL"});
const first=Array.from({length:200},(_,i)=>row(i)),all=Array.from({length:260},(_,i)=>row(i));
let release;const remaining=new Promise(resolve=>{release=resolve;});
u.useBootstrap([]);
u.useNoticePages(async({onFirstPage})=>{onFirstPage(first);await remaining;return all;});
const loading=u.loadApplicationData();await tick();
assert.equal(u.state.notices.length,200);assert.equal(u.state.loading,false);
assert.equal(u.state.noticeListPartial,true);
assert.equal(requests.some(r=>r.path.startsWith("/dashboard?")),false);
release();requests[0].resolve({});await loading;
assert.equal(u.state.notices.length,260);assert.equal(u.state.noticeListPartial,false);
assert.equal(requests.some(r=>r.path.startsWith("/dashboard?")),true);
""")


def test_stale_first_notice_page_is_ignored_after_a_newer_request():
    _run(r"""
u.useBootstrap([]);
u.useNoticePages(async({onFirstPage})=>{u.state.requestSequence++;onFirstPage([{notice_key:"SYN-STALE",status:"OPEN",title:"SYN stale"}]);return [];});
const before=u.state.notices.map(n=>n.noticeKey);
const loading=u.loadApplicationData();await tick();requests[0]?.resolve({});await loading;
assert.deepEqual(u.state.notices.map(n=>n.noticeKey),before);
assert.equal(u.state.noticeListPartial,false);
""")
