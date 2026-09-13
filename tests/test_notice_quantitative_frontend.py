"""Main-list quantitative displays consume only current read-only API results."""
import subprocess

from test_dashboard_loading_frontend import APP, HARNESS as DASHBOARD_HARNESS


HARNESS = DASHBOARD_HARNESS.replace(
    "globalThis.ui={state,els,normalizeDashboard,",
    """globalThis.ui={state,els,normalizeDashboard,
 noticeQuantitativeSummary,renderNoticeQuantitativeSummary,noticeQuantitativeAction,
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
