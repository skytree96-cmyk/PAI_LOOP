import subprocess

from test_department_accounts_frontend import APP, BEHAVIOR_HARNESS


HARNESS = BEHAVIOR_HARNESS.replace(
    "globalThis.ui={state,els,apiRequest,",
    """
renderNoticeList=()=>{};renderManualAnalysisDetailAction=()=>{};
manualAnalysisAvailability=()=>({enabled:true,recomputeCurrent:false});
confirmManualAnalysis=async()=>true;
manualAnalysisAuthHeaders=async()=>({'X-CSRF-Token':'SYN-CSRF'});
evaluationOnlyManualAnalysis=()=>false;
quantitativeEstimateIsVisible=()=>false;invalidateQuantitativeEstimate=()=>{};
loadQuantitativeEstimate=async()=>{};
globalThis.ui={requestManualAnalysis,renderQuantitativeDiagnosticsControl,
 setEstimate(fn){loadQuantitativeEstimate=fn;},
 setConfirmation(fn){confirmManualAnalysis=fn;},
 state,els,apiRequest,""",
) + r'''
const key='PPS-SYN-MANUAL-QUEUE';
function currentNotice(){u.state.notices=[{noticeKey:key,analysisState:'EVALUATED',
 analysisAttachmentCoverageComplete:true,analysisAttempted:true}];}
currentNotice();
'''


def _run(script):
    result = subprocess.run(
        ["node", "-e", HARNESS + "\n(async()=>{\n" + script + "\n})().catch(e=>{console.error(e);process.exit(1);});"],
        input=APP.read_text(encoding="utf-8"), capture_output=True, text=True, encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_duplicate_click_is_locked_before_async_preflight():
    _run(r'''
let release,estimates=0;
const waiting=new Promise(resolve=>release=resolve);
u.setEstimate(()=>{estimates++;return waiting;});
const first=u.requestManualAnalysis(key),duplicate=u.requestManualAnalysis(key);
assert.equal(estimates,1);
release();await tick();
assert.equal(requests.length,1);
respond(requests[0],200,{outcome:'REVIEW',message:'SYN complete'});
await Promise.all([first,duplicate]);
assert.equal(u.state.manualAnalysisRequests.has(key),false);
''')


def test_old_account_finally_cannot_clear_a_new_account_request():
    _run(r'''
const old=u.requestManualAnalysis(key);await tick();
assert.equal(requests.length,1);
login('SYN-B');currentNotice();
assert.equal(u.state.manualAnalysisRequests.has(key),false);
const current=u.requestManualAnalysis(key);await tick();
assert.equal(requests.length,2);
respond(requests[0],200,{outcome:'REVIEW',message:'SYN old'});await old;
assert.equal(u.state.manualAnalysisRequests.has(key),true);
assert.equal(toasts.length,0);
respond(requests[1],200,{outcome:'REVIEW',message:'SYN current'});await current;
assert.equal(u.state.manualAnalysisRequests.has(key),false);
assert.equal(toasts.length,1);
''')


def test_account_change_during_poll_delay_stops_old_polling():
    _run(r'''
const wakeups=[];
context.window.setTimeout=(callback,delay)=>delay===3000 ? (wakeups.push(callback),0) : setTimeout(callback,delay);
const old=u.requestManualAnalysis(key);await tick();
respond(requests[0],200,{outcome:'QUEUED',request_id:'SYN-job'});await tick();
assert.equal(wakeups.length,1);
login('SYN-B');wakeups[0]();await tick();
assert.equal(requests.length,1,'old poll must not use the new cookie session');
await old;assert.equal(toasts.length,0);
''')


def test_preflight_failure_recovers_busy_state_and_reports_once():
    _run(r'''
u.setEstimate(async()=>{throw Error('SYN preflight unavailable');});
let rejected=false;
await u.requestManualAnalysis(key).catch(()=>{rejected=true;});
assert.equal(rejected,false,'event handler must absorb/report its preflight failure');
assert.equal(u.state.manualAnalysisRequests.has(key),false);
assert.equal(toasts.length,1);assert.equal(requests.length,0);
''')


def test_declined_confirmation_never_posts_and_releases_the_button():
    _run(r'''
u.setConfirmation(async()=>false);
await u.requestManualAnalysis(key);
assert.equal(requests.length,0);assert.equal(toasts.length,0);
assert.equal(u.state.manualAnalysisRequests.has(key),false);
''')


def test_unknown_terminal_status_is_never_reported_as_completed():
    _run(r'''
const pending=u.requestManualAnalysis(key);await tick();
respond(requests[0],200,{outcome:'SYN-UNKNOWN'});await pending;
assert.equal(toasts.length,1);assert.equal(toasts[0][2],'error');
assert.equal(u.state.manualAnalysisRequests.has(key),false);
''')


def test_poll_response_must_belong_to_the_original_request():
    _run(r'''
const wakeups=[];
context.window.setTimeout=(callback,delay)=>delay===3000 ? (wakeups.push(callback),0) : setTimeout(callback,delay);
const pending=u.requestManualAnalysis(key);await tick();
respond(requests[0],200,{outcome:'QUEUED',request_id:'SYN-job'});await tick();
wakeups[0]();await tick();
respond(requests[1],200,{outcome:'COMPLETED',request_id:'SYN-other',notice_key:key});await pending;
assert.equal(toasts.length,1);assert.equal(toasts[0][2],'error');
assert.equal(u.state.manualAnalysisRequests.has(key),false);
''')


def test_diagnostic_retry_does_not_follow_another_request_id():
    _run(r'''
const wakeups=[];let container;
context.window.setTimeout=(callback,delay)=>delay===3000 ? (wakeups.push(callback),0) : setTimeout(callback,delay);
context.document.createElement=tag=>({tag,style:{},children:[],listeners:{},isConnected:true,
 setAttribute(){},append(...items){this.children.push(...items);},addEventListener(name,fn){this.listeners[name]=fn;}});
u.els.quantSeparationNote.insertAdjacentElement=(_,element)=>container=element;
u.state.manualAnalysisEnabled=true;
u.renderQuantitativeDiagnosticsControl(u.state.notices[0]);
const retry=container.children[4],output=container.children[5];
const pending=retry.listeners.click();await tick();
respond(requests[0],200,{outcome:'QUEUED',request_id:'SYN-job',notice_key:key});await tick();
wakeups[0]();await tick();
respond(requests[1],200,{outcome:'QUEUED',request_id:'SYN-other',notice_key:key});await pending;
assert.match(output.textContent,/요청을 완료하지 못했습니다/);
assert.equal(requests.length,2);assert.equal(wakeups.length,1);
assert.equal(retry.disabled,false);
''')


def test_matching_terminal_poll_completes_and_releases_the_button():
    _run(r'''
const wakeups=[];
context.window.setTimeout=(callback,delay)=>delay===3000 ? (wakeups.push(callback),0) : setTimeout(callback,delay);
const pending=u.requestManualAnalysis(key);await tick();
respond(requests[0],200,{outcome:'QUEUED',request_id:'SYN-job',notice_key:key});await tick();
wakeups[0]();await tick();
respond(requests[1],200,{outcome:'COMPLETED',request_id:'SYN-job',notice_key:key});await pending;
assert.equal(requests.length,2);assert.equal(wakeups.length,1);
assert.equal(toasts.length,1);assert.equal(toasts[0][2],'success');
assert.equal(u.state.manualAnalysisRequests.has(key),false);
''')
