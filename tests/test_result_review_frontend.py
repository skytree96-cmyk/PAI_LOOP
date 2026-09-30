"""Executable regressions for the automatic-result review tab and presentation badge."""

import subprocess

from test_department_accounts_frontend import APP, BEHAVIOR_HARNESS


def _run_behavior(script):
    # A never-settled await ends node with exit code 0 before the remaining
    # assertions run, so require an explicit completion marker as well.
    result = subprocess.run(
        ['node', '-e', BEHAVIOR_HARNESS + '\n(async()=>{\n' + script
         + '\nconsole.log("SYN-SCRIPT-COMPLETE");\n})().catch(e=>{console.error(e);process.exitCode=1;});'],
        input=APP.read_text(encoding='utf-8'), capture_output=True, text=True, encoding='utf-8', timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "SYN-SCRIPT-COMPLETE" in result.stdout, "script stopped at an unsettled await"


RECORD = r'''
const reviewRecord={notice_key:'SYN-R',title:'SYN result',bid_notice_no:'SYN-R',notice_status:'CLOSED',
 auto_review_pending:true,outcomes:[{id:'SYN-AUTO',source:'PPS_AUTO_FEEDBACK',department_id:null,
 status:'WON',record_status:'VALIDATED',demo:true,source_reference:'SYN 시연 예시'}]};
'''


def test_review_queue_is_requested_and_rendered_with_counts_and_demo_badge():
    _run_behavior(RECORD + r'''
u.state.currentView='closed';
u.state.resultLearning.queue='AUTO_REVIEW';
const load=u.loadResultLearning({force:true});await tick();
assert.equal(requests.length,1);
assert.match(requests[0].path,/\/result-learning\?/);
assert.match(requests[0].path,/queue=AUTO_REVIEW/);
respond(requests[0],200,{total:1,with_outcome_count:3,auto_review_count:1,records:[reviewRecord]});
await load;
assert.equal(u.state.resultLearning.autoReviewCount,1);
assert.equal(u.state.resultLearning.withOutcomeCount,3);
const record=u.state.resultLearning.records[0];
assert.equal(record.autoReviewPending,true);
assert.equal(record.outcome.demo,true);
u.renderResults();
const html=u.els.resultLearningList.innerHTML;
assert.match(html,/record-status--review">확인 필요/);
assert.match(html,/demo-record-badge[^>]*>시연/);
assert.match(html,/나라장터 자동 환류/);
assert.match(html,/검토본 만들기/);
assert.equal(u.els.resultLearningReviewCount.textContent,'1건');
assert.equal(u.els.resultLearningAllCount.textContent,'결과 3건');
assert.match(u.els.resultLearningSummary.textContent,/확인이 필요한 자동 반영 1건/);
assert.equal(u.els.navResultReviewCount.hidden,false);
assert.equal(u.els.navResultReviewCount.textContent,'확인 1');
''')


def test_reviewed_or_real_rows_keep_their_stored_status_without_a_badge():
    _run_behavior(RECORD + r'''
u.state.currentView='closed';
const load=u.loadResultLearning({force:true});await tick();
assert.doesNotMatch(requests[0].path,/queue=/);
const own={id:'SYN-OWN',source:'MANUAL_UI',department_id:'SYN-A',department_revision:1,status:'LOST',
 record_status:'VALIDATED',demo:false};
respond(requests[0],200,{total:1,with_outcome_count:1,auto_review_count:0,
 records:[{...reviewRecord,auto_review_pending:false,outcomes:[own,...reviewRecord.outcomes]}]});
await load;
u.renderResults();
const html=u.els.resultLearningList.innerHTML;
assert.match(html,/record-status--validated">확인 완료/);
assert.doesNotMatch(html,/demo-record-badge/);
assert.equal(u.els.navResultReviewCount.hidden,true);
''')


def test_results_request_before_discovery_says_it_is_waiting_instead_of_silently_ignoring():
    _run_behavior(r'''
u.state.currentView='closed';
u.state.authDiscoveryReady=false;
await u.loadResultLearning({force:true});
assert.equal(requests.length,0);
assert.equal(u.els.resultLearningState.hidden,false);
assert.match(u.els.resultLearningState.innerHTML,/로그인 상태를 확인하고 있습니다/);
''')


def test_results_view_reads_records_before_every_notice_page_arrives():
    _run_behavior(r'''
u.useRealBootstrap();
u.state.authDiscoveryReady=false;
u.state.runtimeProfileAvailable=false;
u.els.navItems=[];u.els.kpiViewButtons=[];
u.setView('closed',{syncRoute:false,focusMain:false});
login();u.state.authDiscoveryReady=false;
const bootstrap=u.loadApplicationData();await tick();
assert.equal(requests.length,2);
assert.match(requests[0].path,/\/notices\?/);
assert.match(requests[1].path,/\/runtime-profile$/);
respond(requests[1],200,{department_accounts_enabled:true});await tick();
assert.match(requests[2].path,/\/accounts\/me$/);
respond(requests[2],200,payload('SYN-A'));await tick();await tick();
const results=()=>requests.filter(r=>r.path.includes('/result-learning?'));
assert.equal(results().length,1,'records load while the notice pages are still pending');
assert.equal(u.state.authDiscoveryReady,true);
respond(results()[0],200,{records:[],total:0});
respond(requests[0],200,[]);await bootstrap;await tick();
assert.equal(results().length,1,'the settled bootstrap does not read the records twice');
assert.equal(requests.filter(r=>r.path.endsWith('/accounts/me')).length,1);
''')


def test_markup_exposes_review_tabs_and_navigation_badge():
    html = (APP.parent / "index.html").read_text(encoding="utf-8")
    assert 'id="resultLearningQueueTabs"' in html
    assert 'data-result-queue="AUTO_REVIEW"' in html
    assert 'id="navResultReviewCount"' in html
    assert "부서 로그인 후 결과 기록을 불러오세요" not in html
