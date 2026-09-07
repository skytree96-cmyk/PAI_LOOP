from pathlib import Path
import subprocess

APP = Path(__file__).parents[1] / 'src/pai_loop/static/app.js'


BEHAVIOR_HARNESS = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const source=require('node:fs').readFileSync(0,'utf8');
const requests=[],toasts=[],rendered=[];
const context=vm.createContext({URL,URLSearchParams,Intl,Headers,AbortController,
 fetch(path,options){return new Promise(resolve=>requests.push({path,options,resolve}));},
 document:{documentElement:{dataset:{}},getElementById(){return null;},addEventListener(){}},
 window:{matchMedia(){return {matches:false}},setTimeout,clearTimeout,requestAnimationFrame(f){f();},
 sessionStorage:{getItem(){return null;},removeItem(){},setItem(){throw Error('PIN storage must not be used');}}}});
const exported=`
const originalRenderExistingDecision=renderExistingDecision,originalUpdateDecisionButton=updateDecisionButton;
renderAll=()=>{};renderDataSource=()=>{};renderResultLearning=()=>{};
closeDetail=()=>{state.selectedNotice=null;};
renderExistingDecision=n=>globalThis.onRender(n);renderPipelineIntoExisting=()=>{};
renderDetail=n=>globalThis.onRender(n);applyFilters=()=>{};
updateDecisionButton=()=>{};setDecisionDockExpanded=()=>{};
showToast=(...args)=>globalThis.onToast(args);
refreshDashboardAfterMutation=async()=>{};loadApplicationData=async()=>{};
globalThis.ui={state,els,apiRequest,applyAccountSession,loadAccountSession,loginDepartmentAccount,logoutDepartmentAccount,
 hydrateDepartmentDecisionList,hydrateOperatorDecisions,normalizeNotice,normalizeResultLearningNotice,
 loadResultLearning,openResultLearningDialog,saveResultLearning,saveDecision,
 renderDecision:originalRenderExistingDecision,updateDecisionButton:originalUpdateDecisionButton,
 setOpenDetail(fn){openDetail=fn;},
 setRefresh(fn){refreshDashboardAfterMutation=fn;}};`;
context.onToast=args=>toasts.push(args);context.onRender=n=>rendered.push(n);
vm.runInContext(source.replace(/\}\)\(\);\s*$/,exported+'\n})();'),context);
const u=context.ui;
const fields=new Map();
function field(){return {value:'',textContent:'',innerHTML:'',hidden:false,disabled:false,open:false,
 dataset:{},classList:{contains(){return false;},toggle(){},add(){},remove(){}},setAttribute(){},
 replaceChildren(){this.innerHTML='';this.textContent='';},reset(){},focus(){},closest(){return null;},
 showModal(){this.open=true;},close(){this.open=false;}};}
Object.setPrototypeOf(u.els,new Proxy({}, {get(_,key){if(!fields.has(key))fields.set(key,field());return fields.get(key);}}));
u.els.decisionInputs=[{value:'GO',checked:false},{value:'HOLD',checked:true},{value:'NO_GO',checked:false}];
u.state.source='api';u.state.accessMode='PUBLIC_READ_ONLY';u.state.requestSequence=1;
function payload(id){return {enabled:true,authenticated:true,account:{id,role:'DEPARTMENT',department_id:id,
 department_name:id,username:id},csrf_token:'SYN-CSRF-'+id,
 capabilities:{read_department_records:true,write_decisions:true,write_results:true}};}
function login(id='SYN-A'){u.applyAccountSession(payload(id));}
function respond(request,status,payload){request.resolve({status,ok:status<400,
 headers:new Headers({'content-type':'application/json'}),async json(){return payload;}});}
const tick=()=>new Promise(setImmediate);
const noticeRaw={notice_key:'SYN-N',title:'SYN notice',status:'OPEN',source_kind:'PPS',decisions:[]};
function notice(decisions=[]){return u.normalizeNotice({...noticeRaw,decisions},0,{operatorDecisionsLoaded:true});}
login();
'''


def _run_behavior(script):
    result = subprocess.run(
        ['node', '-e', BEHAVIOR_HARNESS + '\n(async()=>{\n' + script + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'],
        input=APP.read_text(encoding='utf-8'), capture_output=True, text=True, encoding='utf-8',
    )
    assert result.returncode == 0, result.stderr


def test_old_me_and_old_401_cannot_restore_or_expire_another_session():
    _run_behavior(r'''
const me=u.loadAccountSession();
const expired=u.apiRequest('/operator-decisions/notices/SYN-N').catch(e=>e);
const logout=u.logoutDepartmentAccount();
assert.equal(requests[2].options.headers.get('X-CSRF-Token'),'SYN-CSRF-SYN-A');
respond(requests[2],204,null);await logout;
assert.equal(u.state.accountSession.authenticated,false);
login('SYN-B');
respond(requests[0],200,payload('SYN-A'));
respond(requests[1],401,{detail:'SYN expired'});
await me;const error=await expired;
assert.equal(error.code,'ACCOUNT_CHANGED');
assert.equal(u.state.accountSession.account.id,'SYN-B');
assert.equal(u.state.accountSession.status,'ready');
assert.equal(u.state.notices.length,0);
''')


def test_login_uses_same_origin_and_clears_password_after_success_or_failure():
    _run_behavior(r'''
u.applyAccountSession({enabled:true,authenticated:false});
u.els.accountUsername.value='  SYN-DEPARTMENT  ';u.els.accountPassword.value='SYN-invalid-password';
let loggingIn=u.loginDepartmentAccount({preventDefault(){}});await tick();
assert.equal(requests[0].options.credentials,'same-origin');
assert.equal(requests[0].options.headers.has('X-PAI-Manual-Token'),false);
assert.equal(JSON.parse(requests[0].options.body).username,'SYN-DEPARTMENT');
respond(requests[0],401,{detail:'SYN invalid'});await loggingIn;
assert.equal(u.els.accountPassword.value,'');assert.equal(u.els.accountSubmitButton.disabled,false);
assert.equal(u.state.accountSession.authenticated,false);
u.els.accountPassword.value='SYN-valid-password';
loggingIn=u.loginDepartmentAccount({preventDefault(){}});await tick();
respond(requests[1],200,payload('SYN-A'));await loggingIn;
assert.equal(u.state.accountSession.account.id,'SYN-A');
assert.equal(u.els.accountPassword.value,'');assert.equal(u.els.accountDialog.open,false);
''')


def test_result_reads_expire_cleanly_and_do_not_block_or_overwrite_new_account():
    _run_behavior(r'''
u.state.resultLearning.offset=80;
const old=u.loadResultLearning();await tick();
assert.equal(u.state.resultLearning.loading,true);
login('SYN-B');
assert.equal(u.state.resultLearning.loading,false);
assert.equal(u.state.resultLearning.offset,0);
const current=u.loadResultLearning();await tick();
assert.equal(requests.length,2);
respond(requests[0],200,{records:[{notice_key:'SYN-A-PRIVATE'}],total:1});await old;
assert.equal(u.state.resultLearning.loading,true,'old finally cannot unlock a current request');
assert.equal(u.state.resultLearning.records.length,0);
respond(requests[1],200,{records:[{notice_key:'SYN-B-CURRENT'}],total:1});await current;
assert.equal(u.state.resultLearning.records[0].noticeKey,'SYN-B-CURRENT');
const invalid=u.loadResultLearning({force:true});await tick();
respond(requests[2],401,{detail:'SYN expired'});await invalid;
assert.equal(u.state.accountSession.authenticated,false);
assert.equal(u.state.resultLearning.records.length,0);
assert.equal(u.state.resultLearning.loaded,false);
assert.equal(u.els.resultLearningUnlockButton.disabled,false);
assert.match(u.els.resultLearningState.innerHTML,/부서 로그인이 필요/);
assert.doesNotMatch(u.els.resultLearningState.innerHTML,/PIN/);
''')


def test_anonymous_decision_action_opens_login_and_returns_to_same_notice():
    _run_behavior(r'''
u.applyAccountSession({enabled:true,authenticated:false});
const original=notice();u.state.selectedNotice=original;
u.renderDecision(original);u.updateDecisionButton();
assert.equal(u.els.saveDecisionButton.disabled,false,'login entry does not require a choice or rationale');
assert.equal(u.els.saveDecisionButton.textContent,'로그인 후 판단 기록');
assert.equal(u.els.decisionInputs.every(input=>input.disabled),true);
await u.saveDecision({preventDefault(){}});
assert.equal(u.els.accountDialog.open,true);
assert.equal(requests.length,0,'anonymous action cannot send a decision write');
assert.equal(u.state.selectedNotice.noticeKey,original.noticeKey);
const opened=[];
u.setOpenDetail(async key=>{opened.push(key);u.state.selectedNotice=notice();});
u.els.accountUsername.value='SYN-A';u.els.accountPassword.value='SYN-login-password';
const loggingIn=u.loginDepartmentAccount({preventDefault(){}});await tick();
respond(requests[0],200,payload('SYN-A'));await tick();
assert.deepEqual(opened,[original.noticeKey]);
assert.equal(u.els.accountDialog.open,false);
assert.match(requests[1].path,/\/operator-decisions\/notices\/SYN-N$/);
respond(requests[1],200,[{id:'SYN-A1',department_id:'SYN-A',department_revision:1,
 choice:'HOLD',rationale:'SYN restored own decision'}]);
await loggingIn;
assert.equal(u.state.selectedNotice.noticeKey,original.noticeKey);
assert.equal(u.state.selectedNotice.decisionComment,'SYN restored own decision');
assert.equal(rendered.at(-1).noticeKey,original.noticeKey);
assert.equal(requests.filter(request=>request.options.method==='POST').length,1,'login is the only mutation');
''')


def test_anonymous_cancelled_notice_and_admin_cannot_enter_decision_write():
    _run_behavior(r'''
u.applyAccountSession({enabled:true,authenticated:false});
u.state.selectedNotice=u.normalizeNotice({...noticeRaw,status:'CLOSED',provider_disposition:'CANCELLED'});
u.updateDecisionButton();assert.equal(u.els.saveDecisionButton.disabled,true);
await u.saveDecision({preventDefault(){}});
assert.equal(u.els.accountDialog.open,false);assert.equal(requests.length,0);
u.applyAccountSession({...payload('SYN-ADMIN'),account:{id:'SYN-ADMIN',role:'ADMIN'},
 capabilities:{read_department_records:true,write_decisions:false}});
u.state.selectedNotice=notice([{id:'SYN-A1',department_id:'SYN-A',department_revision:1,
 choice:'HOLD',rationale:'SYN readable department record'}]);u.updateDecisionButton();
assert.equal(u.els.saveDecisionButton.disabled,true);
assert.equal(u.els.saveDecisionButton.textContent,'부서 판단 조회 전용');
u.renderDecision(u.state.selectedNotice);
assert.match(u.els.departmentDecisionState.textContent,/조회만 가능합니다/);
assert.doesNotMatch(u.els.departmentDecisionState.textContent,/작성하세요/);
await u.saveDecision({preventDefault(){}});
assert.equal(u.els.accountDialog.open,false);assert.equal(requests.length,0);
''')


def test_department_card_shows_latest_revision_per_department_and_clears_on_logout():
    _run_behavior(r'''
const current=notice([
 {id:'SYN-A1',department_id:'SYN-A',department_name:'SYN A',department_revision:1,
  choice:'GO',rationale:'SYN old own',created_at:'2099-01-01T00:00:00Z'},
 {id:'SYN-A2',department_id:'SYN-A',department_name:'SYN A',department_revision:2,
  choice:'HOLD',rationale:'SYN latest own',created_at:'2026-01-01T00:00:00Z'},
 {id:'SYN-B1',department_id:'SYN-B',department_name:'SYN <B>',department_revision:1,
  choice:'NO_GO',rationale:'SYN <script>other rationale</script>'},
 {id:'SYN-LEGACY',actor_label:'SYN A',choice:'GO',rationale:'SYN unassigned record'}]);
u.state.selectedNotice=current;u.renderDecision(current);
const html=u.els.departmentDecisionList.innerHTML;
assert.equal(u.els.departmentDecisionCard.hidden,false);
assert.equal((html.match(/<li>/g)||[]).length,3,'one own, one other, and separate legacy row');
assert.match(html,/SYN latest own/);assert.doesNotMatch(html,/SYN old own/);
assert.match(html,/SYN &lt;B&gt;/);assert.match(html,/&lt;script&gt;other rationale&lt;\/script&gt;/);
assert.doesNotMatch(html,/<script>/);
assert.equal(u.els.decisionComment.value,'SYN latest own','other departments stay outside the own form');
assert.doesNotMatch(u.els.decisionExisting.textContent,/other rationale|SYN <B>/);
u.applyAccountSession({enabled:true,authenticated:false});
assert.equal(u.els.departmentDecisionList.innerHTML,'','logout clears the private DOM immediately');
assert.equal(u.els.decisionComment.value,'');assert.equal(u.state.selectedNotice,null);
u.renderDecision(current);
assert.equal(u.els.departmentDecisionCard.hidden,true);
assert.equal(u.els.departmentDecisionList.innerHTML,'','a stale known notice cannot repopulate the hidden department card');
const publicNotice=u.normalizeNotice(noticeRaw);u.renderDecision(publicNotice);
assert.equal(u.els.departmentDecisionCard.hidden,true);
assert.equal(u.els.departmentDecisionList.innerHTML,'');
''')


def test_department_decision_batches_cover_every_page_and_keep_missing_reads_unknown():
    _run_behavior(r'''
u.state.notices=Array.from({length:401},(_,i)=>u.normalizeNotice({...noticeRaw,notice_key:'SYN-N-'+i}));
const loading=u.hydrateDepartmentDecisionList(1);
for(let page=0;page<3;page++){
 await tick();const request=requests[page];const keys=JSON.parse(request.options.body).notice_keys;
 assert.equal(keys.length,page<2?200:1);
 assert.equal(keys[0],'SYN-N-'+page*200);
 assert.equal(request.options.headers.get('X-CSRF-Token'),'SYN-CSRF-SYN-A');
 assert.equal(request.options.credentials,'same-origin');
 const rows=Object.fromEntries(keys.filter(key=>key!=='SYN-N-400').map(key=>[key,[]]));
 respond(request,200,{decisions_by_notice:rows,missing_notice_keys:page===2?['SYN-N-400']:[]});
}
await loading;
assert.equal(u.state.notices.filter(n=>n.decisionReadStatus==='KNOWN').length,400);
assert.equal(u.state.notices[400].decisionReadStatus,'ERROR');
assert.equal(u.state.notices[400].decision,'');
''')


def test_decision_save_uses_own_revision_and_refreshes_conflict_without_retry():
    _run_behavior(r'''
const decisions=[
 {id:'SYN-B99',department_id:'SYN-B',department_revision:99,choice:'GO',rationale:'SYN other'},
 {id:'SYN-A2',department_id:'SYN-A',department_revision:2,choice:'HOLD',rationale:'SYN own'},
 {id:'SYN-A1',department_id:'SYN-A',department_revision:1,choice:'GO',rationale:'SYN old',created_at:'2099-01-01'}];
const current=notice(decisions);u.state.notices=[current];u.state.selectedNotice=current;
u.els.decisionInputs[1].checked=true;u.els.decisionComment.value='SYN own reason';
const saving=u.saveDecision({preventDefault(){}});await tick();
const sent=JSON.parse(requests[0].options.body);
assert.equal(sent.expected_decision_id,'SYN-A2');assert.equal(sent.actor_label,'SYN-A');
assert.equal(sent.choice,'HOLD');assert.equal(sent.rationale,'SYN own reason');
respond(requests[0],409,{detail:'SYN department decision changed'});await tick();
assert.match(requests[1].path,/\/notices\/SYN-N$/);
respond(requests[1],200,noticeRaw);await tick();
assert.match(requests[2].path,/\/operator-decisions\/notices\/SYN-N$/);
respond(requests[2],200,[...decisions,{id:'SYN-A3',department_id:'SYN-A',department_revision:3,choice:'NO_GO',rationale:'SYN new'}]);
await saving;
assert.equal(u.state.selectedNotice.decision,'NO_GO');
assert.equal(requests.filter(r=>r.options.method==='POST').length,1);
assert.equal(rendered.at(-1).decisionComment,'SYN new');
''')


def test_decision_save_cannot_render_previous_account_after_dashboard_wait():
    _run_behavior(r'''
const current=notice();u.state.notices=[current];u.state.selectedNotice=current;
u.els.decisionInputs[1].checked=true;u.els.decisionComment.value='SYN private reason';
let finishRefresh;u.setRefresh(()=>new Promise(resolve=>{finishRefresh=resolve;}));
const saving=u.saveDecision({preventDefault(){}});await tick();
respond(requests[0],200,{id:'SYN-A1',department_id:'SYN-A',department_revision:1,choice:'HOLD',rationale:'SYN private reason'});
await tick();assert.equal(typeof finishRefresh,'function');
login('SYN-B');finishRefresh();await saving;
assert.equal(rendered.length,0);assert.equal(u.state.selectedNotice,null);
assert.equal(u.state.notices.length,0);
''')


def test_result_form_uses_own_record_and_safe_create_patch_versions():
    _run_behavior(r'''
const other={id:'SYN-B99',department_id:'SYN-B',department_revision:99,source:'MANUAL_UI',status:'WON',operator_note:'SYN other private form'};
const own={id:'SYN-A2',department_id:'SYN-A',department_revision:2,source:'MANUAL_UI',status:'LOST',operator_note:'SYN own note',updated_at:'2026-09-01T01:00:00Z'};
let n=u.normalizeResultLearningNotice({notice_key:'SYN-N',outcomes:[other,own],latest_outcome:other});
u.openResultLearningDialog(n);assert.equal(u.els.resultLearningOperatorNote.value,'SYN own note');
let saving=u.saveResultLearning({preventDefault(){}});await tick();
assert.match(requests[0].path,/\/result-learning\/SYN-A2$/);assert.equal(requests[0].options.method,'PATCH');
assert.equal(JSON.parse(requests[0].options.body).expected_updated_at,own.updated_at);
respond(requests[0],409,{detail:'SYN stale result'});await tick();
assert.equal(u.els.resultLearningDialog.open,false);
respond(requests[1],200,{records:[],total:0});await saving;
n=u.normalizeResultLearningNotice({notice_key:'SYN-N',outcomes:[other],latest_outcome:other});
u.openResultLearningDialog(n);assert.equal(u.els.resultLearningOperatorNote.value,'');
u.els.resultLearningOperatorNote.value='SYN first own note';
saving=u.saveResultLearning({preventDefault(){}});await tick();
assert.equal(requests[2].options.method,'POST');
const sent=JSON.parse(requests[2].options.body);
assert.equal(sent.expected_outcome_id,null);assert.equal(sent.basis_outcome_id,undefined);
assert.equal(sent.operator_note,'SYN first own note');
respond(requests[2],200,{id:'SYN-A1'});await tick();
respond(requests[3],200,{records:[],total:0});await saving;
''')


def test_unrelated_operator_routes_do_not_use_department_auth_or_pin_fallback():
    _run_behavior(r'''
for(const path of ['/pps-discovery/search','/prespec-discovery/save','/performance-records?limit=200',
 '/pre-specifications/SYN-P/analysis','/company-awards/search']){
 await assert.rejects(u.apiRequest(path,{method:'POST'}),error=>error.status===403);
}
assert.equal(requests.length,0);assert.equal(u.state.accountSession.authenticated,true);
''')


def test_department_frontend_ownership_versions_and_permissions():
    script = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const source=require('node:fs').readFileSync(0,'utf8');
const context=vm.createContext({URL,URLSearchParams,Intl,
 document:{documentElement:{dataset:{}},getElementById(){return null;},addEventListener(){}},
 window:{matchMedia(){return {matches:false}}}});
const exported=`globalThis.ui={state,els,normalizeNotice,normalizeDecisionRecord,normalizeResultLearningNotice,
 ownDepartmentRecords,compareDepartmentRevision,canWriteDecision,canWriteResults,manualAnalysisAvailability,
 accountMutationHeaders,operatorDecisionListAvailable};`;
vm.runInContext(source.replace(/\}\)\(\);\s*$/,exported+'\n})();'),context);
const u=context.ui;
u.state.source='api';u.state.manualAnalysisEnabled=true;u.state.accessMode='PUBLIC_READ_ONLY';
u.state.accountSession={enabled:true,authenticated:true,account:{id:'SYN-A',role:'DEPARTMENT',department_id:'SYN-A',department_name:'SYN A'},
 capabilities:{write_decisions:true,write_results:true,recompute_analysis:true,request_paid_analysis:false},csrfToken:'SYN-CSRF'};
const decisions=[
 {id:'SYN-legacy',actor_label:'SYN A',choice:'GO',rationale:'legacy is unassigned',created_at:'2099-01-01T00:00:00Z'},
 {id:'SYN-B1',department_id:'SYN-B',department_revision:99,choice:'NO_GO',rationale:'other department',created_at:'2099-01-01T00:00:00Z'},
 {id:'SYN-A1',department_id:'SYN-A',department_revision:1,choice:'GO',rationale:'older revision newer clock',created_at:'2099-01-01T00:00:00Z'},
 {id:'SYN-A2',department_id:'SYN-A',department_revision:2,choice:'HOLD',rationale:'latest own',created_at:'2026-01-01T00:00:00Z'}];
let raw={notice_key:'SYN-NOTICE',title:'SYN',status:'OPEN',source_kind:'PPS',decisions};
let n=u.normalizeNotice(raw,0,{operatorDecisionsLoaded:true});
assert.equal(n.decision,'HOLD'); assert.equal(n.decisionComment,'latest own');assert.equal(n.decisions.length,4);
assert.equal(u.ownDepartmentRecords(n.decisions).sort(u.compareDepartmentRevision)[0].id,'SYN-A2');
assert.equal(u.operatorDecisionListAvailable([n]),true);
assert.equal(u.operatorDecisionListAvailable([{...n,decisionReadStatus:'ERROR'}]),false);
assert.equal(u.accountMutationHeaders()['X-CSRF-Token'],'SYN-CSRF');
n=u.normalizeNotice({...raw,decisions:decisions.slice(0,2),decision:'GO',comment:'other legacy fallback'},0,{operatorDecisionsLoaded:true});
assert.equal(n.decision,'');assert.equal(n.decisionComment,'');
const rows=decisions.map(d=>({...d,status:'LOST',source:'MANUAL_UI',updated_at:d.created_at}));
let r=u.normalizeResultLearningNotice({notice_key:'SYN',outcomes:rows,latest_outcome:rows[1]});
assert.equal(r.outcome.id,'SYN-A2');assert.equal(r.expectedOutcomeId,'SYN-A2');assert.equal(r.departmentOutcomes.length,4);
r=u.normalizeResultLearningNotice({notice_key:'SYN',outcomes:rows.slice(0,2),latest_outcome:rows[1]});
assert.equal(r.outcome,null);assert.equal(r.expectedOutcomeId,null);
const active={sourceKind:'PPS',noticeStatus:'OPEN',deadline:'2099-01-01',analysisState:'NOT_EVALUATED',analysisAttachmentCoverageComplete:false};
assert.equal(u.manualAnalysisAvailability(active).code,'ACCOUNT_ANALYSIS_FORBIDDEN');
u.state.accountSession.capabilities.request_paid_analysis=true;
assert.equal(u.manualAnalysisAvailability(active).enabled,true);
const reviewed={...active,analysisState:'EVALUATED',analysisAttachmentCoverageComplete:true};
u.state.accountSession.capabilities.request_paid_analysis=false;
assert.equal(u.manualAnalysisAvailability(reviewed).code,'RECOMPUTE_CURRENT');
u.state.accountSession.account={id:'SYN-admin',role:'ADMIN'};
u.state.accountSession.capabilities={write_decisions:false,write_results:false,recompute_analysis:true,request_paid_analysis:false};
assert.equal(u.canWriteDecision(),false);assert.equal(u.canWriteResults(),false);
assert.equal(u.normalizeNotice(raw).decision,'');
u.state.accountSession.authenticated=false;
assert.equal(u.accountMutationHeaders(),null);
assert.equal(u.manualAnalysisAvailability(active).code,'ACCOUNT_LOGIN_REQUIRED');
u.state.accountSession.enabled=false;u.state.writeControlsEnabled=true;
assert.equal(u.canWriteDecision(),true);
'''
    result = subprocess.run(['node', '-e', script], input=APP.read_text(encoding='utf-8'), capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
