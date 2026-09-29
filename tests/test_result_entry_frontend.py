"""Executable regressions for result entry from the missing-results queue."""

import json
import subprocess

import pytest

from test_department_accounts_frontend import APP, BEHAVIOR_HARNESS


HARNESS = BEHAVIOR_HARNESS.replace(
    "globalThis.ui={state,els,apiRequest,",
    """globalThis.ui={state,els,apiRequest,
 openNoticeResultLearning,handleNoticeActivation,handleNoticeKeydown,closeResultLearningDialog,updateResultLearningValidation,
 renderDetailResult,loadDetailResult,noticeListActions,matchesPipelineQueue,openNoticeFromRoute,
 setApplicationReload(fn){loadApplicationData=fn;},""",
).replace(
    "new Promise(resolve=>requests.push({path,options,resolve}))",
    "new Promise((resolve,reject)=>requests.push({path,options,resolve,reject}))",
) + r'''
const reloads=[];
u.setApplicationReload(async options=>reloads.push(options));
u.els.resultLearningForm.querySelector=selector=>selector.includes(':invalid')
 ? [...fields.values()].find(input=>input.validationMessage
   || (input.required&&!String(input.value??'').trim()))||null : null;
u.els.resultLearningForm.checkValidity=()=>!u.els.resultLearningForm.querySelector(':invalid');
function openEmptyResult(key='SYN-N'){
 u.openResultLearningDialog(u.normalizeResultLearningNotice({
  notice_key:key,bid_notice_no:'SYN-NUMBER',revision_no:'00',
  title:'SYN training service',notice_status:'CLOSED',outcomes:[]}));
}
function fillWonResult(){
 u.els.resultLearningStatus.value='WON';
 u.els.resultLearningRecordStatus.value='DRAFT';
 u.els.resultLearningWinningAmount.value='35501500';
 u.els.resultLearningWinningRate.value='95';
 u.els.resultLearningTechnicalScore.value='82.73';
 u.els.resultLearningPriceScore.value='7.37';
 u.els.resultLearningTotalScore.value='90.1';
 u.els.resultLearningRank.value='1';
 u.els.resultLearningWinner.value='SYN awardee';
 u.els.resultLearningOccurredAt.value='2026-01-27';
 u.els.resultLearningOperatorNote.value='SYN entered note';
}
'''


def _run(script):
    result = subprocess.run(
        ['node', '-e', HARNESS + '\n(async()=>{\n' + script
         + '\n})().catch(e=>{console.error(e);process.exit(1);});'],
        input=APP.read_text(encoding='utf-8'), capture_output=True,
        text=True, encoding='utf-8', timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_direct_result_entry_loads_exact_key_and_current_department_revision():
    _run(r'''
const key='SYN-공고?revision=00&value=1';
const trigger=field();
const opening=u.openNoticeResultLearning(key,trigger);await tick();
assert.equal(requests.length,1);
assert.ok(requests[0].path.endsWith('/result-learning/notices/'+encodeURIComponent(key)));
assert.equal(requests[0].options.credentials,'same-origin');
assert.equal(requests[0].options.headers.get('X-CSRF-Token'),'SYN-CSRF-SYN-A');
const own={id:'SYN-A2',department_id:'SYN-A',department_revision:2,
 source:'MANUAL_UI',status:'LOST',operator_note:'SYN own latest',updated_at:'2026-09-12T01:00:00Z'};
const other={id:'SYN-B99',department_id:'SYN-B',department_revision:99,
 source:'MANUAL_UI',status:'WON',operator_note:'SYN other department'};
respond(requests[0],200,{notice_key:key,title:'SYN exact notice',bid_notice_no:'SYN-NUMBER',
 revision_no:'00',latest_outcome:other,outcomes:[other,own,
 {...own,id:'SYN-A1',department_revision:1,operator_note:'SYN older',updated_at:'2099-01-01T00:00:00Z'}]});
await opening;
assert.equal(u.els.resultLearningDialog.open,true);
assert.equal(u.state.resultLearning.editingNotice.noticeKey,key);
assert.equal(u.state.resultLearning.editingOutcome.id,'SYN-A2');
assert.equal(u.els.resultLearningOperatorNote.value,'SYN own latest');
assert.equal(u.els.resultLearningStatus.value,'LOST');
assert.equal(trigger.disabled,false);
''')


def test_direct_entry_deduplicates_requests_and_discards_previous_account_response():
    _run(r'''
const old=u.openNoticeResultLearning('SYN-OLD');
const duplicate=u.openNoticeResultLearning('SYN-OLD');await tick();
assert.equal(requests.length,1);
login('SYN-B');
const current=u.openNoticeResultLearning('SYN-NEW');await tick();
assert.equal(requests.length,2);
respond(requests[0],200,{notice_key:'SYN-OLD',title:'SYN previous account',outcomes:[]});
await old;await duplicate;
assert.equal(u.els.resultLearningDialog.open,false);
respond(requests[1],200,{notice_key:'SYN-NEW',title:'SYN current account',outcomes:[]});
await current;
assert.equal(u.els.resultLearningDialog.open,true);
assert.equal(u.state.resultLearning.editingNotice.noticeKey,'SYN-NEW');
assert.equal(toasts.filter(row=>row[2]==='error').length,0);
''')


@pytest.mark.parametrize('view', ['result-missing', 'result-missing-decided'])
@pytest.mark.parametrize('activation', ['row', 'title'])
def test_missing_result_queue_row_and_title_open_result_form(activation, view):
    _run('const activation=' + json.dumps(activation) + ';const view=' + json.dumps(view) + ';' + r'''
u.state.currentView=view;
const details=[];u.setOpenDetail((...args)=>details.push(args));
const row=Object.assign(field(),{dataset:{noticeKey:'SYN-N'}});
const title=Object.assign(field(),{dataset:{openNotice:'SYN-N'}});
const target={closest(selector){
 if(selector==='[data-notice-key]')return row;
 if(selector==='[data-open-notice]')return activation==='title'?title:null;
 if(selector==='button, a, input, select, textarea')return activation==='title'?title:null;
 return null;
}};
u.handleNoticeActivation({target,preventDefault(){}});await tick();
assert.equal(details.length,0);
assert.equal(requests.length,1);
assert.ok(requests[0].path.endsWith('/result-learning/notices/SYN-N'));
respond(requests[0],200,{notice_key:'SYN-N',title:'SYN notice',outcomes:[]});await tick();
assert.equal(u.els.resultLearningDialog.open,true);
''')


@pytest.mark.parametrize("view", ["result-missing", "result-missing-decided"])
def test_missing_result_queue_explicit_detail_keeps_general_detail_available(view):
    _run('const view=' + json.dumps(view) + ';' + r'''
u.state.currentView=view;
const details=[];u.setOpenDetail((...args)=>details.push(args));
const row=Object.assign(field(),{dataset:{noticeKey:'SYN-N'}});
const detail={dataset:{resultDetail:'SYN-N',openNotice:'SYN-N'}};
const target={closest(selector){
 if(selector==='[data-notice-key]')return row;
 if(['[data-open-notice]','[data-result-detail]','button, a, input, select, textarea'].includes(selector))return detail;
 return null;
}};
u.handleNoticeActivation({target,preventDefault(){}});await tick();
assert.equal(requests.length,0);
assert.equal(details.length,1);
assert.equal(details[0][0],'SYN-N');
''')


def test_new_result_requires_explicit_choice_and_validated_source_before_request():
    _run(r'''
openEmptyResult();
assert.equal(u.els.resultLearningStatus.value,'');
await u.saveResultLearning({preventDefault(){}});
assert.equal(requests.length,0);
assert.ok(u.els.resultLearningStatus.validationMessage);
fillWonResult();u.els.resultLearningRecordStatus.value='VALIDATED';
await u.saveResultLearning({preventDefault(){}});
assert.equal(requests.length,0);
assert.equal(u.els.resultLearningSourceReference.required,true);
assert.match(u.els.resultLearningSourceReference.validationMessage,/출처|근거/);
u.els.resultLearningSourceReference.value='SYN official award record';
assert.equal(u.updateResultLearningValidation(),true);
assert.equal(u.els.resultLearningSourceReference.validationMessage,'');
const saving=u.saveResultLearning({preventDefault(){}});await tick();
assert.equal(requests.length,1);
const body=JSON.parse(requests[0].options.body);
assert.equal(body.record_status,'VALIDATED');
assert.equal(body.source_reference,'SYN official award record');
respond(requests[0],422,{detail:'SYN stop after request verification'});await saving;
''')


def test_won_draft_saves_entered_values_and_refreshes_application_queues():
    _run(r'''
openEmptyResult();fillWonResult();
const saving=u.saveResultLearning({preventDefault(){}});await tick();
assert.equal(requests.length,1);
assert.equal(requests[0].options.method,'POST');
const body=JSON.parse(requests[0].options.body);
assert.equal(body.status,'WON');assert.equal(body.record_status,'DRAFT');
assert.equal(body.winning_bid_amount,35501500);assert.equal(body.winning_bid_rate,95);
assert.equal(body.technical_score,82.73);assert.equal(body.price_score,7.37);
assert.equal(body.total_score,90.1);assert.equal(body.rank,1);
assert.equal(body.winner_name,'SYN awardee');assert.equal(body.operator_note,'SYN entered note');
assert.equal(body.occurred_at,'2026-01-27T00:00:00+09:00');
assert.equal(body.source_reference,null);assert.equal(body.opening_identity,null);
assert.equal(body.notice_key,'SYN-N');assert.equal(body.expected_outcome_id,null);
respond(requests[0],200,{id:'SYN-result'});await tick();
for(const request of requests.slice(1))respond(request,200,{records:[],total:0});
await saving;
assert.equal(u.els.resultLearningDialog.open,false);
assert.equal(reloads.length,1);assert.equal(reloads[0].forceApi,true);
assert.equal(u.els.resultLearningSaveButton.disabled,false);
assert.equal(toasts.filter(row=>row[2]==='success').length,1);
''')


def test_nonparticipation_with_award_values_is_explained_before_save_and_recoverable():
    _run(r'''
openEmptyResult();fillWonResult();u.els.resultLearningStatus.value='NO_BID';
await u.saveResultLearning({preventDefault(){}});
assert.equal(requests.length,0);
assert.equal(u.els.resultLearningError.hidden,false);
assert.match(u.els.resultLearningError.textContent,/미참여/);
assert.equal(u.els.resultLearningWinningAmount.value,'35501500');
u.els.resultLearningStatus.value='WON';
assert.equal(u.updateResultLearningValidation(),true);
assert.equal(u.els.resultLearningStatus.validationMessage,'');
const saving=u.saveResultLearning({preventDefault(){}});await tick();
assert.equal(requests.length,1);
assert.equal(JSON.parse(requests[0].options.body).status,'WON');
respond(requests[0],422,{detail:'SYN stop after correction'});await saving;
''')


def test_pending_save_blocks_duplicate_submission_and_closing():
    _run(r'''
openEmptyResult();fillWonResult();
const first=u.saveResultLearning({preventDefault(){}});
const second=u.saveResultLearning({preventDefault(){}});await tick();
assert.equal(requests.length,1);
assert.equal(u.state.resultLearning.saving,true);
assert.equal(u.els.resultLearningSaveButton.disabled,true);
assert.equal(u.els.resultLearningCloseButton.disabled,true);
assert.equal(u.els.resultLearningCancelButton.disabled,true);
u.closeResultLearningDialog();
assert.equal(u.els.resultLearningDialog.open,true);
assert.equal(u.els.resultLearningOperatorNote.value,'SYN entered note');
respond(requests[0],422,{detail:'SYN preserve draft'});await first;await second;
assert.equal(u.state.resultLearning.saving,false);
assert.equal(u.els.resultLearningCloseButton.disabled,false);
assert.equal(u.els.resultLearningCancelButton.disabled,false);
''')


def test_account_switch_closes_pending_result_and_old_response_cannot_touch_new_form():
    _run(r'''
openEmptyResult();fillWonResult();
const oldSave=u.saveResultLearning({preventDefault(){}});await tick();
assert.equal(requests.length,1);
login('SYN-B');
assert.equal(u.els.resultLearningDialog.open,false);
assert.equal(u.state.resultLearning.editingNotice,null);
assert.equal(u.state.resultLearning.saving,false);
openEmptyResult('SYN-B-N');
u.els.resultLearningStatus.value='SUBMITTED';
u.els.resultLearningOperatorNote.value='SYN new account note';
respond(requests[0],200,{id:'SYN-A-result'});await oldSave;
assert.equal(u.els.resultLearningDialog.open,true);
assert.equal(u.state.resultLearning.editingNotice.noticeKey,'SYN-B-N');
assert.equal(u.els.resultLearningOperatorNote.value,'SYN new account note');
assert.equal(reloads.length,0);
assert.equal(toasts.filter(row=>row[2]==='success'||row[2]==='error').length,0);
''')


def test_slow_post_save_refresh_cannot_disable_next_form_or_unlock_its_pending_save():
    _run(r'''
let finishRefresh;
u.setApplicationReload(()=>new Promise(resolve=>{finishRefresh=resolve;}));
openEmptyResult('SYN-FIRST');fillWonResult();
const first=u.saveResultLearning({preventDefault(){}});await tick();
respond(requests[0],200,{id:'SYN-first-result'});await tick();
assert.equal(typeof finishRefresh,'function');
assert.equal(u.els.resultLearningDialog.open,false);
assert.equal(u.state.resultLearning.saving,false);
openEmptyResult('SYN-NEXT');fillWonResult();
assert.equal(u.els.resultLearningDialog.open,true);
assert.equal(u.els.resultLearningFields.disabled,false);
assert.equal(u.els.resultLearningSaveButton.disabled,false);
assert.equal(u.els.resultLearningCloseButton.disabled,false);
assert.equal(u.els.resultLearningCancelButton.disabled,false);
const next=u.saveResultLearning({preventDefault(){}});await tick();
assert.equal(requests.length,2);
assert.equal(u.state.resultLearning.saving,true);
finishRefresh();await first;
assert.equal(u.state.resultLearning.saving,true);
assert.equal(u.els.resultLearningFields.disabled,true);
assert.equal(u.els.resultLearningSaveButton.disabled,true);
assert.equal(u.els.resultLearningCloseButton.disabled,true);
assert.equal(u.els.resultLearningCancelButton.disabled,true);
assert.equal(u.els.resultLearningDialog.open,true);
assert.equal(u.state.resultLearning.editingNotice.noticeKey,'SYN-NEXT');
respond(requests[1],422,{detail:'SYN stop next save'});await next;
assert.equal(u.state.resultLearning.saving,false);
assert.equal(u.els.resultLearningFields.disabled,false);
''')


def test_cancelled_notice_conflict_explains_cancellation_and_preserves_input():
    _run(r'''
openEmptyResult();fillWonResult();
const saving=u.saveResultLearning({preventDefault(){}});await tick();
const detail='취소된 공고의 결과는 변경할 수 없습니다.';
respond(requests[0],409,{detail});await saving;
assert.equal(u.els.resultLearningError.hidden,false);
assert.ok(u.els.resultLearningError.textContent.includes(detail));
assert.doesNotMatch(u.els.resultLearningError.textContent,/다른 결과가 먼저 저장/);
assert.equal(u.els.resultLearningDialog.open,true);
assert.equal(u.els.resultLearningWinningAmount.value,'35501500');
assert.equal(u.els.resultLearningOperatorNote.value,'SYN entered note');
assert.equal(u.els.resultLearningSaveButton.disabled,false);
assert.equal(requests.length,1);
''')


@pytest.mark.parametrize('failure', ['validation', 'conflict', 'network'])
def test_save_failure_keeps_dialog_values_and_inline_recoverable_error(failure):
    _run('const failure=' + json.dumps(failure) + ';' + r'''
openEmptyResult();fillWonResult();
const saving=u.saveResultLearning({preventDefault(){}});await tick();
if(failure==='network')requests[0].reject(new Error('SYN network disconnected'));
else if(failure==='conflict')respond(requests[0],409,{detail:'SYN department result changed'});
else respond(requests[0],422,{detail:[{loc:['body','source_reference'],
 msg:'Value error, validated result requires source_reference',type:'value_error'}]});
await saving;
assert.equal(u.els.resultLearningDialog.open,true);
assert.equal(u.state.resultLearning.editingNotice.noticeKey,'SYN-N');
assert.equal(u.els.resultLearningStatus.value,'WON');
assert.equal(u.els.resultLearningWinningAmount.value,'35501500');
assert.equal(u.els.resultLearningTechnicalScore.value,'82.73');
assert.equal(u.els.resultLearningOccurredAt.value,'2026-01-27');
assert.equal(u.els.resultLearningOperatorNote.value,'SYN entered note');
assert.equal(u.els.resultLearningError.hidden,false);
assert.ok(u.els.resultLearningError.textContent);
assert.doesNotMatch(u.els.resultLearningError.textContent,/\[object Object\]/);
if(failure==='validation')assert.match(u.els.resultLearningError.textContent,/출처|근거/);
assert.equal(u.els.resultLearningSaveButton.disabled,false);
assert.equal(requests.length,1,'failure must not discard values by replacing the record');
assert.equal(reloads.length,0);
''')


@pytest.mark.parametrize("view", ["result-missing", "result-missing-decided"])
@pytest.mark.parametrize("key", ["Enter", " "])
def test_result_queue_keyboard_opens_editor(view, key):
    _run('u.state.currentView=' + json.dumps(view) + ';const key=' + json.dumps(key) + ';' + r'''
const row=Object.assign(field(),{dataset:{noticeKey:'SYN-N'}});
u.handleNoticeKeydown({key,preventDefault(){},target:{closest(selector){return selector==='.notice-row'?row:null;}}});
await tick();assert.equal(details.length,0);assert.equal(requests.length,1);
respond(requests[0],200,{notice_key:'SYN-N',outcomes:[]});await tick();
assert.equal(u.els.resultLearningDialog.open,true);
''')


def test_save_opens_canonical_result_before_slow_list_refresh():
    _run(r'''
let finish;u.setApplicationReload(()=>new Promise(resolve=>{finish=resolve;}));
openEmptyResult();fillWonResult();u.els.resultLearningRecordStatus.value='VALIDATED';
u.els.resultLearningSourceReference.value='SYN official record';
const saving=u.saveResultLearning({preventDefault(){}});await tick();
respond(requests[0],200,{notice_key:'SYN-N',outcome:{id:'SYN-result',department_id:'SYN-A',department_revision:1,
 source:'MANUAL_UI',record_status:'VALIDATED',status:'WON',winning_bid_amount:35501500,winning_bid_rate:95.1234,
 source_reference:'SYN official record',operator_note:'<script>SYN</script>'}});await tick();
assert.equal(u.els.resultLearningDialog.open,false);assert.equal(details.length,1);
assert.equal(details[0][0],'SYN-N');assert.equal(details[0][2].initialTab,'result');
assert.equal(details[0][2].resultRecord.outcome.winningBidRate,95.1234);
u.state.selectedNotice=notice();u.renderDetailResult(details[0][2].resultRecord);
assert.match(u.els.detailResultContent.innerHTML,/35,501,500/);
assert.match(u.els.detailResultContent.innerHTML,/95.1234%/);
assert.match(u.els.detailResultContent.innerHTML,/&lt;script&gt;/);
assert.equal(u.els.detailResultEditButton.textContent,'결과 수정');
finish();await saving;
''')


def test_draft_action_does_not_require_confirmation_source_and_stays_in_queue():
    _run(r'''
openEmptyResult();fillWonResult();u.els.resultLearningRecordStatus.value='VALIDATED';
const saving=u.saveResultLearning({preventDefault(){},submitter:u.els.resultLearningDraftButton});await tick();
assert.equal(JSON.parse(requests[0].options.body).record_status,'DRAFT');
respond(requests[0],200,{outcome:{id:'SYN-result',source:'MANUAL_UI',department_id:'SYN-A',status:'WON',record_status:'DRAFT'}});
await saving;
const draft={...notice(),decision:'GO',status:'CLOSED',noticeStatus:'CLOSED',deadline:'2020-01-01',hasBidOutcome:true,resultEntryStatus:'DRAFT'};
assert.equal(u.matchesPipelineQueue(draft,'result-missing-decided'),true);
assert.match(u.noticeListActions(draft,true),/작성 중/);assert.match(u.noticeListActions(draft,true),/이어서 입력/);
assert.equal(u.matchesPipelineQueue({...draft,resultEntryStatus:'COMPLETE'},'result-missing-decided'),false);
''')


def test_result_detail_late_response_cannot_leak_across_accounts():
    _run(r'''
u.state.selectedNotice=notice();const loading=u.loadDetailResult('SYN-N');await tick();
login('SYN-B');u.state.selectedNotice=notice();
respond(requests[0],200,{notice_key:'SYN-N',outcomes:[{source:'MANUAL_UI',department_id:'SYN-A',operator_note:'SYN PRIVATE'}]});
await loading;assert.doesNotMatch(u.els.detailResultContent.innerHTML,/SYN PRIVATE/);
''')


def test_queue_refresh_keeps_the_open_result_detail_tab():
    _run(r'''
 u.state.currentView='result-missing-decided';
 context.window.location.search='?notice=SYN-N';
 u.state.selectedNotice={noticeKey:'SYN-N'};
 u.els.detailDrawer.classList.contains=value=>value==='is-open';
 u.openNoticeFromRoute();
 assert.equal(details.length,0);
 context.window.location.search='?notice=SYN-OTHER';
 u.openNoticeFromRoute();
 assert.equal(details.length,1);
 assert.equal(details[0][0],'SYN-OTHER');
''')


@pytest.mark.parametrize("status", ["WON", "LOST"])
def test_confirmed_result_submits_unknown_award_amount_as_null(status):
    _run("const outcomeStatus=" + json.dumps(status) + r'''
openEmptyResult();fillWonResult();
u.els.resultLearningStatus.value=outcomeStatus;
u.els.resultLearningRecordStatus.value='VALIDATED';
u.els.resultLearningSourceReference.value='SYN official result';
u.els.resultLearningLossReason.value='SYN evaluated second';
u.els.resultLearningWinningAmount.value='';
u.els.resultLearningWinningRate.value='';
assert.equal(u.updateResultLearningValidation(),true);
const saving=u.saveResultLearning({preventDefault(){}});await tick();
assert.equal(requests.length,1);
const body=JSON.parse(requests[0].options.body);
assert.equal(body.record_status,'VALIDATED');
assert.equal(body.winning_bid_amount,null);assert.equal(body.winning_bid_rate,null);
assert.equal(body.winning_rate_calculation.mode,'MANUAL');
respond(requests[0],200,{outcome:{...body,id:'SYN-result',source:'MANUAL_UI',department_id:'SYN-A'}});
await saving;
assert.equal(u.els.resultLearningDialog.open,false);
assert.equal(details[0][2].initialTab,'result');
u.renderDetailResult(details[0][2].resultRecord);
assert.match(u.els.detailResultContent.innerHTML,/<dt>낙찰금액<\/dt><dd>미확인<\/dd>/);
''')


def test_new_outcome_status_keeps_unknown_award_amount_optional():
    _run(r'''
const start=source.indexOf('    els.resultLearningStatus.addEventListener("change", () => {');
const end=source.indexOf('    });',start)+7;
let onChange;u.els.resultLearningStatus.addEventListener=(_event,callback)=>{onChange=callback;};
vm.runInContext(source.slice(start,end).replace(/els\./g,'ui.els.').replace(/state\./g,'ui.state.').replace('updateResultLearningRate();','ui.updateResultLearningRate();'),context);
openEmptyResult();
for (const status of ['WON','LOST']) {
 u.els.resultLearningStatus.value=status;onChange();
 assert.equal(u.els.resultLearningWinningRateMode.value,'MANUAL');
 assert.equal(u.els.resultLearningWinningAmount.required,false);
}
u.els.resultLearningWinningRateMode.value='AUTO';
u.els.resultLearningWinningRateMode.dataset.userSelected='true';onChange();
assert.equal(u.els.resultLearningWinningRateMode.value,'AUTO');
assert.equal(u.els.resultLearningWinningAmount.required,true);
''')
