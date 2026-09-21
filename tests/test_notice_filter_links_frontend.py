"""Exercise shared notice filters using the application's actual JavaScript."""

from pathlib import Path
import subprocess


APP = Path(__file__).parents[1] / "src/pai_loop/static/app.js"

HARNESS = r'''
const assert=require("node:assert/strict"),vm=require("node:vm");
const source=require("node:fs").readFileSync(0,"utf8");
const nodes=new Map(),reloads=[],cancelledTimers=[],toasts=[],requests=[];
const document={documentElement:{dataset:{}},activeElement:null,
 addEventListener(){},getElementById(id){return nodes.get(id)||null;},
 createElement:tag=>element(tag),createDocumentFragment:()=>element("fragment")};
function element(tag="div") {
 const node={tag,children:[],dataset:{},attributes:{},hidden:false,disabled:false,
  value:"",textContent:"",selected:false,
  append(...items){for(const item of items)this.children.push(...(item.tag==="fragment"?item.children:[item]));},
  replaceChildren(...items){this.children=[];this.append(...items);},
  setAttribute(name,value){this.attributes[name]=String(value);},
  focus(){document.activeElement=this;},select(){this.selected=true;},
  closest(selector){return selector==="[data-remove-notice-filter]"&&this.dataset.removeNoticeFilter?this:null;}};
 if(tag==="select"){
  let selected="";
  Object.defineProperty(node,"options",{get(){return this.children.flatMap(child=>child.tag==="optgroup"?child.children:[child]);}});
  Object.defineProperty(node,"value",{get(){return selected;},set(value){selected=this.options.some(option=>option.value===value)?value:"";}});
 }
 return node;
}
const window={location:new URL("https://syn.invalid/notices"),
 matchMedia(){return {matches:false};},setTimeout,
 clearTimeout(id){cancelledTimers.push(id);}};
const navigator={clipboard:{async writeText(){}}};
const history={state:null,replaceState(state,_,url){this.state=state;window.location=new URL(url);}};
const context=vm.createContext({URL,URLSearchParams,Intl,console,document,window,navigator,history});
context.reload=options=>reloads.push(options);
context.toast=args=>toasts.push(args);
context.request=(path,options)=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject}));
const exported=`
const originalLoadApplicationData=loadApplicationData;
loadApplicationData=options=>globalThis.reload(options);
apiRequest=(...args)=>globalThis.request(...args);
showToast=(...args)=>globalThis.toast(args);
renderNoticeSearchScope=()=>{};renderNoticeList=()=>{};
populatePerformanceDivisionOptions=()=>{};
globalThis.ui={state,els,NOTICE_FILTER_FIELDS,noticeFilterHref,restoreNoticeFiltersFromRoute,
 routeViewFromLocation,prepareNoticeFilterDepartments,removeNoticeFilter,copyNoticeFilterLink,
 renderNoticeFilterTools,applyFilters,
 useRealLoader(rows){
  loadApplicationData=originalLoadApplicationData;fetchNoticePages=async()=>rows;
  applyRuntimeProfile=()=>{};loadAccountSession=async()=>{};
  setLoading=value=>{state.loading=value;};setSystemStatus=()=>{};hideDemoBanner=()=>{};
  renderAll=applyFilters;openNoticeFromRoute=()=>{};hydrateApplicationMetadata=async()=>{};
 },
 loadApplicationData:(...args)=>loadApplicationData(...args),
 useRealInit(){
  cacheElements=()=>{};clearManualAnalysisToken=()=>{};
  applyAccountSession=payload=>{state.accountSession=payload;};renderAccountSession=()=>{};
  formatAwardBusinessNumberInput=initializeExternalSearchDates=configurePaiBotTeamsAccess=detectTeamsContext=bindEvents=()=>{};
  closeDetail=renderNoticeSearchMode=updateActiveNavigationGroup=closeMobileMenu=renderDataSource=()=>{};
  hideDemoBanner=showDemoBanner=()=>{};loadTeamsFollowups=async()=>{};
  loadApplicationData=()=>globalThis.observeBootstrapRequest(buildNoticeRequestPath());
 },init};`;
vm.runInContext(source.replace(/\}\)\(\);\s*$/,exported+"\n})();"),context);
const u=context.ui;
const choices={
 eligibility:["all","PASS","PASS_EXCEPTION","PASS_CURRENT","REVIEW","FAIL"],
 recommendation:["all","GO","CONDITIONAL_GO","DEFERRED","NO_GO"],
 decision:["all","GO","CONDITIONAL_GO","HOLD","NO_GO","UNDECIDED"],
 sort:["judgement","department","readiness","risk","newest"],
 department:["organization","SYN-DEPT"]};
for(const [key,[id,fallback]] of Object.entries(u.NOTICE_FILTER_FIELDS)){
 const field=element(choices[key]?"select":"input");
 for(const value of choices[key]||[]){const option=element("option");option.value=value;option.textContent="표시 "+value;field.append(option);}
 field.value=fallback;u.els[id]=field;nodes.set(id,field);
}
for(const id of ["noticeFilterTools","noticeFilterChips","noticeFilterLink","operatorDecisionFilterHelp","rankingProfileVersion"]){
 u.els[id]=element(id==="noticeFilterLink"?"input":"div");nodes.set(id,u.els[id]);
}
u.els.noticeFilterLink.hidden=true;
Object.assign(u.state,{source:"api",currentView:"new",noticeSearchMode:"stored",layout:"table",loading:false,
 accountSession:{enabled:true,authenticated:true,account:{id:"SYN-ACCOUNT",role:"DEPARTMENT",department_id:"SYN-DEPT"}},
 departmentSelectionAccountId:"SYN-ACCOUNT",notices:[]});
function location(href){window.location=new URL(href,"https://syn.invalid");}
function resetFields(){for(const [, [id,fallback]] of Object.entries(u.NOTICE_FILTER_FIELDS))u.els[id].value=fallback;u.state.pendingNoticeDecisionFilter=null;}
function remove(key){const chip=element("button");chip.dataset.removeNoticeFilter=key;u.removeNoticeFilter({target:chip});}
function notice(key,decision,readStatus="KNOWN"){
 return {noticeKey:key,title:key,agency:"",noticeNumber:key,noticeStatus:"OPEN",sourceKind:"MANUAL",
 deadline:"2099-12-31T00:00:00Z",decision,decisionReadStatus:readStatus,
 qualificationStatus:"PASS",eligibilityStatus:"PASS",analysisState:"EVALUATED",recommendation:"GO",
 topDepartmentRankings:[],departmentReviewCandidates:[]};
}
'''


def _run(script: str) -> None:
    result = subprocess.run(
        ["node", "-e", HARNESS + "\n(async()=>{\n" + script
         + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
        input=APP.read_text(encoding="utf-8"), capture_output=True,
        text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_round_trip_all_filters_and_alias_views_without_private_url_parameters() -> None:
    _run(r'''
const expected={q:'한글 <검토> "A&B" / + #',eligibility:"PASS_EXCEPTION",recommendation:"CONDITIONAL_GO",
 decision:"HOLD",sort:"risk",department:"SYN-DEPT",keywords:"교육, 컨설팅 & 연구"};
for(const view of ["new","review","undecided","collected","go","ended"]){
 location("/notices?token=SYN-PRIVATE&api_key=SYN-KEY&notice=SYN-N&host=teams&demo=1#SYN-fragment");
 u.state.currentView=view;u.state.layout="cards";
 for(const [key,value] of Object.entries(expected))u.els[u.NOTICE_FILTER_FIELDS[key][0]].value=value;
 const href=u.noticeFilterHref(),url=new URL(href);
 assert.equal(url.hash,"");
 for(const key of ["token","api_key","notice","host","demo"])assert.equal(url.searchParams.has(key),false,key);
 assert.equal(href.includes("SYN-PRIVATE"),false);
 resetFields();u.state.layout="table";location(href);
 u.state.currentView=u.routeViewFromLocation();assert.equal(u.state.currentView,view);
 u.restoreNoticeFiltersFromRoute();
 for(const [key,value] of Object.entries(expected))assert.equal(u.els[u.NOTICE_FILTER_FIELDS[key][0]].value,value,key);
 assert.equal(u.state.layout,"cards");assert.equal(u.state.pendingNoticeDecisionFilter,"HOLD");
 assert.equal(u.state.departmentSelectionAccountId,"SYN-ACCOUNT");
 u.renderNoticeFilterTools();
 assert.equal(u.els.noticeFilterChips.children.length,7);
 const chip=u.els.noticeFilterChips.children.find(item=>item.dataset.removeNoticeFilter==="q");
 assert.equal(chip.textContent,`검색: ${expected.q} ×`);
 assert.equal(chip.attributes["aria-label"],`검색: ${expected.q} 조건 지우기`);
 assert.equal(chip.children.length,0,"query is text, not HTML");
}
location("/notices?filters=1&view=ended");
assert.equal(u.routeViewFromLocation(),"new","a query must not override an unrelated path");
''')


def test_restoration_validates_enums_and_bounds_text_and_requires_marker() -> None:
    _run(r'''
const params=new URLSearchParams({filters:"1",q:"한".repeat(220),keywords:"키".repeat(550),
 eligibility:"BAD",recommendation:"BAD",decision:"BAD",sort:"BAD",department:"BAD",layout:"BAD"});
location("/notices?"+params);u.restoreNoticeFiltersFromRoute();
assert.equal(u.els.searchInput.value.length,200);assert.equal(u.els.priorityKeywordInput.value.length,500);
for(const key of ["eligibility","recommendation","decision","sort","department"]){
 const [id,fallback]=u.NOTICE_FILTER_FIELDS[key];assert.equal(u.els[id].value,fallback,key);
}
assert.equal(u.state.pendingNoticeDecisionFilter,null);assert.equal(u.state.layout,"table");
location("/notices?filters=1&q=%20%20한글%20%20검색%0A테스트%20");u.restoreNoticeFiltersFromRoute();
assert.equal(u.els.searchInput.value,"한글 검색 테스트");
resetFields();location("/notices?q=ignored&decision=GO");u.restoreNoticeFiltersFromRoute();
assert.equal(u.els.searchInput.value,"");assert.equal(u.state.pendingNoticeDecisionFilter,null);
u.state.noticeSearchMode="pps";u.renderNoticeFilterTools();assert.equal(u.els.noticeFilterTools.hidden,true);
''')


def test_department_catalog_loads_before_select_restoration_and_failure_falls_back() -> None:
    _run(r'''
location("/notices?filters=1&department=SYN-OTHER");
const pending=u.prepareNoticeFilterDepartments();
assert.equal(requests.length,1);assert.equal(requests[0].path,"/departments/keyword-profiles");
requests[0].resolve({version:"SYN-V1",departments:[{id:"SYN-OTHER",name:"SYN 다른 부서",group:"SYN"}]});
await pending;u.restoreNoticeFiltersFromRoute();
assert.equal(u.els.departmentSelect.value,"SYN-OTHER");assert.equal(u.state.keywordProfilesAvailable,true);
await u.prepareNoticeFilterDepartments();assert.equal(requests.length,1,"existing option needs no refetch");
location("/notices?filters=1&department=SYN-MISSING");
const failed=u.prepareNoticeFilterDepartments();requests[1].reject(Error("SYN offline"));await failed;
u.restoreNoticeFiltersFromRoute();assert.equal(u.els.departmentSelect.value,"organization");
assert.equal(toasts.at(-1)[2],"error");
''')


def test_chip_removal_reloads_only_server_conditions_and_cancels_pending_search() -> None:
    _run(r'''
location("/notices?notice=SYN-N&host=teams#detail");
u.els.eligibilityFilter.value="REVIEW";u.els.searchInput.value="기존 검색";
remove("eligibility");assert.equal(reloads.length,0);assert.equal(u.els.eligibilityFilter.value,"all");
assert.equal(u.els.searchInput.value,"기존 검색");assert.equal(document.activeElement,u.els.eligibilityFilter);
for(const [index,key] of ["q","department","keywords"].entries()){
 const [id]=u.NOTICE_FILTER_FIELDS[key];u.els[id].value=key==="department"?"SYN-DEPT":"SYN text";
 u.state.noticeSearchTimer="SYN-TIMER-"+key;remove(key);
 assert.equal(reloads.length,index+1);assert.equal(reloads.at(-1).forceApi,true);
 assert.equal(cancelledTimers.at(-1),"SYN-TIMER-"+key);assert.equal(u.state.noticeSearchTimer,null);
 assert.equal(u.els[id].value,u.NOTICE_FILTER_FIELDS[key][1]);
}
assert.equal(window.location.search.includes("notice=SYN-N"),true,"editing local filters preserves detail context");
assert.equal(window.location.search.includes("host=teams"),true);
u.state.pendingNoticeDecisionFilter="HOLD";remove("decision");assert.equal(u.state.pendingNoticeDecisionFilter,null);
assert.equal(reloads.length,3);remove("not-a-filter");assert.equal(reloads.length,3);
''')


def test_clipboard_failure_selects_safe_link_for_manual_copy() -> None:
    _run(r'''
location("/notices?secret=SYN-PRIVATE&notice=SYN-N#private");u.els.searchInput.value="한글 검색";
let copied;navigator.clipboard.writeText=async value=>{copied=value;throw Error("SYN denied");};
await u.copyNoticeFilterLink();
assert.equal(u.els.noticeFilterLink.hidden,false);assert.equal(u.els.noticeFilterLink.selected,true);
assert.equal(document.activeElement,u.els.noticeFilterLink);assert.equal(u.els.noticeFilterLink.value,copied);
assert.equal(new URL(copied).searchParams.get("q"),"한글 검색");
assert.equal(new URL(copied).searchParams.has("secret"),false);assert.equal(new URL(copied).searchParams.has("notice"),false);
assert.equal(new URL(copied).hash,"");assert.equal(toasts.at(-1)[2],"info");
navigator.clipboard.writeText=async value=>{copied=value;};await u.copyNoticeFilterLink();
assert.equal(toasts.at(-1)[2],"success");
''')


def test_decision_filter_waits_for_complete_reads_then_applies_and_admin_does_not_claim_it() -> None:
    _run(r'''
location("/notices?filters=1&decision=HOLD");u.restoreNoticeFiltersFromRoute();
u.state.notices=[notice("SYN-HOLD","HOLD"),notice("SYN-GO","GO","UNKNOWN")];
u.applyFilters();assert.equal(u.state.pendingNoticeDecisionFilter,"HOLD");
assert.equal(u.els.operatorDecisionFilter.disabled,true);assert.equal(u.state.filteredNotices.length,0);
assert.equal(new URL(u.noticeFilterHref()).searchParams.get("decision"),"HOLD");
u.state.notices[1].decisionReadStatus="ERROR";u.applyFilters();assert.equal(u.state.filteredNotices.length,0);
u.state.notices[1].decisionReadStatus="KNOWN";u.applyFilters();
assert.equal(u.state.pendingNoticeDecisionFilter,null);assert.equal(u.els.operatorDecisionFilter.value,"HOLD");
assert.equal(u.els.operatorDecisionFilter.disabled,false);
assert.deepEqual(Array.from(u.state.filteredNotices,notice=>notice.noticeKey),["SYN-HOLD"]);
u.restoreNoticeFiltersFromRoute();u.state.accountSession.account.role="ADMIN";u.applyFilters();
assert.equal(u.state.pendingNoticeDecisionFilter,null);assert.equal(u.els.operatorDecisionFilter.value,"all");
assert.equal(u.els.operatorDecisionFilter.disabled,true);assert.equal(u.state.filteredNotices.length,2);
''')


def test_first_init_serializes_login_department_before_first_notice_request() -> None:
    _run(r'''
location("/notices");
u.els.departmentSelect.children=u.els.departmentSelect.children.filter(option=>option.value==="organization");
u.els.departmentSelect.value="organization";
u.state.departmentSelectionAccountId=null;u.state.source="loading";u.state.currentView="all";
document.body=element("body");window.addEventListener=()=>{};
history.pushState=(state,unused,url)=>history.replaceState(state,unused,url);
Object.setPrototypeOf(u.els,new Proxy({}, {get(_,id){
 if(!nodes.has(id)){
  const node=element();node.classList={toggle(){},contains(){return false;}};nodes.set(id,node);
 }
 return nodes.get(id);
}}));
u.els.navItems=[];u.els.kpiViewButtons=[];u.els.layoutButtons=[];
const initialRequests=[];context.observeBootstrapRequest=path=>initialRequests.push(path);
u.useRealInit();const pending=u.init();
assert.equal(requests[0].path,"/accounts/me");
requests[0].resolve({enabled:true,authenticated:true,account:{id:"SYN-ACCOUNT",role:"DEPARTMENT",
 department_id:"SYN-DEPT",department_name:"SYN 로그인 부서"}});
await pending;
assert.equal(u.els.departmentSelect.value,"SYN-DEPT");
assert.equal(new URL(window.location.href).searchParams.get("department"),"SYN-DEPT");
assert.equal(initialRequests.length,1);
assert.equal(new URL(initialRequests[0],window.location.origin).searchParams.get("department_id"),"SYN-DEPT");
assert.equal(document.body.hidden,false);
''')


def test_real_reload_preserves_applied_decision_until_batch_hydration_finishes() -> None:
    _run(r'''
location("/notices?filters=1&decision=HOLD");u.restoreNoticeFiltersFromRoute();
u.state.notices=[notice("SYN-HOLD","HOLD"),notice("SYN-GO","GO")];u.applyFilters();
assert.equal(u.state.pendingNoticeDecisionFilter,null);assert.equal(u.els.operatorDecisionFilter.value,"HOLD");
window.setTimeout=()=>"SYN-LOADING-TIMER";
u.state.accountSession.csrfToken="SYN-CSRF";
u.useRealLoader(["SYN-HOLD","SYN-GO"].map(key=>({notice_key:key,title:key,source_kind:"MANUAL",
 status:"OPEN",deadline:"2099-12-31T00:00:00Z"})));
const pending=u.loadApplicationData({forceApi:true});
assert.equal(u.state.pendingNoticeDecisionFilter,"HOLD");
assert.equal(requests[0].path,"/runtime-profile");requests[0].resolve({});await pending;
assert.equal(u.state.pendingNoticeDecisionFilter,"HOLD");assert.equal(u.state.filteredNotices.length,0);
assert.equal(new URL(u.noticeFilterHref()).searchParams.get("decision"),"HOLD");
assert.equal(requests[1].path,"/operator-decisions/batch-read");
requests[1].resolve({decisions_by_notice:{
 "SYN-HOLD":[{id:"SYN-D1",department_id:"SYN-DEPT",department_revision:1,choice:"HOLD",rationale:"SYN review"}],
 "SYN-GO":[{id:"SYN-D2",department_id:"SYN-DEPT",department_revision:1,choice:"GO",rationale:"SYN proceed"}]
}});
await new Promise(setImmediate);
assert.equal(u.state.pendingNoticeDecisionFilter,null);assert.equal(u.els.operatorDecisionFilter.value,"HOLD");
assert.deepEqual(Array.from(u.state.filteredNotices,item=>item.noticeKey),["SYN-HOLD"]);
''')
