"""Main-list quantitative displays consume only current read-only API results."""
import subprocess

from test_dashboard_loading_frontend import APP, HARNESS as DASHBOARD_HARNESS


HARNESS = DASHBOARD_HARNESS.replace(
    "globalThis.ui={state,els,normalizeDashboard,",
    """globalThis.ui={state,els,normalizeDashboard,
 noticeQuantitativeSummary,renderNoticeQuantitativeSummary,noticeQuantitativeAction,
 renderQuantitativeEstimate,renderQuantitativeEstimateRow,
 loadQuantitativeEstimate,invalidateQuantitativeEstimate,handleNoticeActivation,
 noticeListActions,renderNoticeCard,renderNoticeRow,""",
) + r'''
const key='SYN-quantitative';
const qnotice=u.normalizeNotice({notice_key:key,title:'SYN quantitative notice',source_kind:'MANUAL',
 status:'OPEN',deadline:'2099-01-01T00:00:00Z',analysis_state:'COLLECTED'});
u.state.notices=[qnotice];
const nodes=[{dataset:{noticeQuantitative:key},innerHTML:''},{dataset:{noticeQuantitative:key},innerHTML:''}];
const buttons=[{dataset:{loadQuantitative:key},attributes:{},textContent:'',setAttribute(k,v){this.attributes[k]=v;}}];
context.document.querySelectorAll=selector=>selector==='[data-notice-quantitative]'?nodes:buttons;
const data={rule_source_status:'AVAILABLE',source_validation_status:'SOURCE_VALIDATED',
 activation_status:'AUTO_ACTIVE',overall_status:'CONFIRMED',total_max_points:10,lower_points:8,upper_points:8,
 activation_reasons:[],criteria:[]};
function setData(value){u.state.quantitativeEstimates[key]={status:'ready',data:value};}
'''


def _run(script):
    result = subprocess.run(
        ["node", "-e", HARNESS + "\n(async()=>{\n" + script
         + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"],
        input=APP.read_text(encoding="utf-8"), capture_output=True,
        text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_main_card_and_table_are_explicit_reads_not_automatic_n_plus_one():
    _run(r'''
assert.match(u.renderNoticeCard(qnotice),/data-notice-quantitative/);
assert.match(u.renderNoticeRow(qnotice),/data-notice-quantitative/);
assert.match(u.noticeQuantitativeAction(qnotice),/정량 점수 확인/);
assert.equal(requests.length,0,'rendering a list never sends API calls');
assert.equal(u.noticeQuantitativeSummary(qnotice).value,'미조회');
assert.doesNotMatch(u.noticeListActions(qnotice,true),/data-load-quantitative/,'result-entry action stays focused');
u.state.source='demo';assert.equal(u.renderNoticeQuantitativeSummary(qnotice),'');
assert.equal(u.noticeQuantitativeAction(qnotice),'');
''')


def test_missing_values_never_become_confirmed_zero_and_source_gap_wins():
    _run(r'''
for(const missing of [null,undefined,'', '  ',false]){
 setData({...data,lower_points:missing});
 assert.equal(u.noticeQuantitativeSummary(qnotice).value,'미산정');
 assert.notEqual(u.noticeQuantitativeSummary(qnotice).status,'confirmed');
}
setData({...data,rule_source_status:'MISSING',lower_points:0,upper_points:0});
assert.equal(u.noticeQuantitativeSummary(qnotice).value,'미산정');
setData({...data,activation_reasons:['QUANTITATIVE_TABLE_NOT_ESTABLISHED']});
assert.equal(u.noticeQuantitativeSummary(qnotice).value,'미산정');
setData({...data,activation_status:'NOT_APPLICABLE'});
assert.equal(u.noticeQuantitativeSummary(qnotice).value,'비적용');
for(const invalid of [{activation_status:'REVIEW_REQUIRED'}, {activation_status:'UNKNOWN'},
 {source_validation_status:'REVIEW_REQUIRED'}, {rule_source_status:'INCOMPLETE'}]){
 setData({...data,...invalid});assert.equal(u.noticeQuantitativeSummary(qnotice).value,'미산정');
}
''')


def test_confirmed_and_partial_lower_bounds_remain_distinct_and_reasons_are_escaped():
    _run(r'''
setData(data);let summary=u.noticeQuantitativeSummary(qnotice);
assert.equal(summary.status,'confirmed');assert.equal(summary.value,'8 / 10점');
setData({...data,overall_status:'ESTIMATED',lower_points:3.5,upper_points:5,
 criteria:[{status:'ESTIMATED',rationale:'SYN <missing> contract scope'}]});
summary=u.noticeQuantitativeSummary(qnotice);
assert.equal(summary.status,'estimated');assert.equal(summary.value,'3.5 / 10점');
assert.match(summary.label,/잠정/);assert.match(summary.reason,/미확정 사유/);
assert.match(u.renderNoticeQuantitativeSummary(qnotice),/&lt;missing&gt;/);
setData({...data,overall_status:'UNSCORABLE',lower_points:0,upper_points:10,
 activation_status:'PARTIAL_ACTIVE',source_validation_status:'REVIEW_REQUIRED',
 activation_reasons:['REQUIRED_EVIDENCE_INCOMPLETE']});
summary=u.noticeQuantitativeSummary(qnotice);
assert.equal(summary.status,'estimated');assert.match(summary.reason,/회사 증빙/);
assert.notEqual(summary.label,'확정');
setData({...data,lower_points:12});assert.equal(u.noticeQuantitativeSummary(qnotice).value,'미산정');
''')


def test_public_review_without_numeric_points_does_not_claim_a_provisional_score():
    _run(r'''
setData({...data,ruleset_version:'public-quantitative-summary-v1',overall_status:'REVIEW',
 activation_status:'REVIEW_REQUIRED',source_validation_status:'REVIEW_REQUIRED',
 lower_points:null,upper_points:null,total_max_points:null,
 activation_reasons:['PUBLIC_ANALYSIS_REVIEW_REQUIRED']});
const summary=u.noticeQuantitativeSummary(qnotice);
assert.equal(summary.value,'미산정');
assert.equal(summary.reason,'저장된 평가 기준 또는 회사 증빙의 검증이 끝나지 않았습니다.');
assert.doesNotMatch(u.renderNoticeQuantitativeSummary(qnotice),/잠정 점수로 표시|점수 범위로 표시/);
''')


def test_cancelled_or_historical_estimates_are_not_presented_as_current_scores():
    _run(r'''
setData(data);
assert.equal(u.noticeQuantitativeSummary({...qnotice,providerDisposition:'CANCELLED'}).value,'산정 제외');
assert.equal(u.noticeQuantitativeAction({...qnotice,providerDisposition:'CANCELLED'}),'');
assert.equal(u.noticeQuantitativeSummary({...qnotice,historicalAnalysis:true}).value,'현재 점수 미산정');
''')


def test_explicit_read_updates_both_list_displays_and_reuses_the_detail_cache():
    _run(r'''
const opening=u.loadQuantitativeEstimate(key);
assert.equal(requests.length,1);assert.equal(requests[0].path,'/notices/'+encodeURIComponent(key)+'/quantitative-estimate');
assert.ok(nodes.every(node=>node.innerHTML.includes('조회 중')));
assert.equal(buttons[0].attributes['aria-disabled'],'true');
await u.loadQuantitativeEstimate(key);assert.equal(requests.length,1);
requests[0].resolve(data);await opening;
assert.ok(nodes.every(node=>node.innerHTML.includes('8 / 10점')));
assert.equal(buttons[0].attributes['aria-disabled'],'false');
await u.loadQuantitativeEstimate(key);assert.equal(requests.length,1,'detail uses the same ready cache');
u.invalidateQuantitativeEstimate(key);
assert.ok(nodes.every(node=>node.innerHTML.includes('미조회')));
assert.equal(requests.length,1,'invalidation does not enqueue model work');
''')


def test_failure_and_stale_response_cannot_republish_old_score():
    _run(r'''
const old=u.loadQuantitativeEstimate(key);u.invalidateQuantitativeEstimate(key);
const current=u.loadQuantitativeEstimate(key);assert.equal(requests.length,2);
requests[0].resolve(data);await old;
assert.equal(u.state.quantitativeEstimates[key].status,'loading');
requests[1].reject(Error('SYN read failed'));await current;
assert.equal(u.state.quantitativeEstimates[key].status,'error');
assert.ok(nodes.every(node=>node.innerHTML.includes('조회 실패')));
assert.ok(nodes.every(node=>!node.innerHTML.includes('8 / 10점')));
const after=u.loadQuantitativeEstimate(key);u.state.quantitativeEstimates={};u.state.notices=[];
requests[2].resolve(data);await after;
assert.equal(u.state.quantitativeEstimates[key],undefined);
''')


def test_main_quantitative_action_does_not_open_detail_or_result_entry():
    _run(r'''
const button={dataset:{loadQuantitative:key}};let prevented=0,stopped=0;
const event={target:{closest(selector){return selector==='[data-load-quantitative]'?button:null;}},
 preventDefault(){prevented++;},stopPropagation(){stopped++;}};
u.handleNoticeActivation(event);u.handleNoticeActivation(event);
assert.equal(requests.length,1);assert.equal(prevented,2);assert.equal(stopped,2);
requests[0].resolve(data);await tick();
assert.equal(u.state.quantitativeEstimates[key].status,'ready');
''')


def test_nonquantitative_rows_stay_separate_in_detail_and_do_not_mask_missing_facts():
    _run(r'''
const separate={label:'SYN <proposal>',status:'OUT_OF_SCOPE',max_points:60,
 lower_points:0,upper_points:60,rationale:'SYN proposal assessed separately',
 rule_floor_points:5,rule_base_points:10};
const objective={label:'SYN credit',status:'UNSCORABLE',max_points:40,
 lower_points:0,upper_points:40,rationale:'SYN missing current credit evidence'};
const mixed={...data,overall_status:'UNSCORABLE',total_max_points:40,
 lower_points:0,upper_points:40,out_of_scope_points:60,criteria:[separate,objective]};
setData(mixed);
assert.equal(u.noticeQuantitativeSummary(qnotice).value,'0 / 40점');
assert.match(u.noticeQuantitativeSummary(qnotice).reason,/missing current credit evidence/);
assert.doesNotMatch(u.noticeQuantitativeSummary(qnotice).reason,/proposal assessed separately/);
u.renderQuantitativeEstimate(mixed);
assert.match(u.els.scoreOverview.innerHTML,/0–40 \/ 40/);
assert.doesNotMatch(u.els.scoreOverview.innerHTML,/100/);
const rows=u.els.quantTableBody.innerHTML;
assert.ok(rows.indexOf('SYN credit')<rows.indexOf('quant-scope-divider'));
assert.ok(rows.indexOf('quant-scope-divider')<rows.indexOf('SYN &lt;proposal&gt;'));
assert.match(rows,/정량 합산 제외/);
assert.match(u.els.quantSeparationNote.textContent,/정량 외 배점 60점/);
const row=u.renderQuantitativeEstimateRow(separate);
assert.match(row,/정량 외/);
assert.doesNotMatch(row,/0–60점|조건부 하한|가감 전 기본/);
assert.equal(requests.length,0,'scope display does not request extraction');
''')


def test_separate_points_note_survives_public_summary_and_clears_on_next_notice():
    _run(r'''
u.renderQuantitativeEstimate({...data,criteria:[],out_of_scope_points:60,
 ruleset_version:'public-quantitative-summary-v1'});
assert.match(u.els.quantSeparationNote.textContent,/정량 외 배점 60점/);
assert.doesNotMatch(u.els.quantTableBody.innerHTML,/quant-scope-divider/);
u.renderQuantitativeEstimate({...data,out_of_scope_points:0});
assert.doesNotMatch(u.els.quantSeparationNote.textContent,/정량 외 배점/);
u.renderQuantitativeEstimate({...data,rule_source_status:'MISSING',
 source_validation_status:'MISSING',activation_status:'REVIEW_REQUIRED',
 total_max_points:null,lower_points:null,upper_points:null,criteria:[]});
assert.match(u.els.scoreOverview.innerHTML,/미산정/);
assert.match(u.els.quantSourceStatus.textContent,/배점표 미확보/);
''')


def test_only_separate_items_never_display_confirmed_zero():
    _run(r'''
const onlySeparate={...data,total_max_points:0,lower_points:0,upper_points:0,
 overall_status:'UNSCORABLE',out_of_scope_points:60,readiness_pct:null,readiness_band:'GRAY',
 criteria:[{label:'SYN proposal',status:'OUT_OF_SCOPE',max_points:60,lower_points:0,upper_points:60}]};
setData(onlySeparate);
assert.equal(u.noticeQuantitativeSummary(qnotice).value,'미산정');
u.renderQuantitativeEstimate(onlySeparate);
assert.match(u.els.scoreOverview.innerHTML,/미산정/);
assert.doesNotMatch(u.els.scoreOverview.innerHTML,/0 \/ 0/);
assert.match(u.els.quantSourceStatus.textContent,/산정 불가/);
assert.doesNotMatch(u.els.quantSourceStatus.textContent,/일부 항목 미산정/);
assert.match(u.els.quantTableBody.innerHTML,/자동 산정 가능한 항목 없음/);
assert.match(u.els.quantTableBody.innerHTML,/정량 합산 제외/);
''')
