import subprocess

from test_department_accounts_frontend import APP, BEHAVIOR_HARNESS
from test_notice_filter_links_frontend import HARNESS as FILTER_HARNESS


def run_js(harness, script):
    result = subprocess.run(
        ["node", "-e", harness + "\n(async()=>{\n" + script + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"],
        input=APP.read_text(encoding="utf-8"), capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_money_formats_round_trip_without_silently_accepting_bad_paste():
    harness = BEHAVIOR_HARNESS.replace("globalThis.ui={state,els,", "globalThis.ui={state,els,moneyInputValue,formatMoneyInput,")
    run_js(harness, r'''
for(const [input,formatted,value] of [
 ['12345000','12,345,000',12345000],['1234.50','1,234.50',1234.5],['0','0',0],
 ['0.','0.',0],['1e20','1e20',1e20],['.5','.5',0.5]]) {
 assert.equal(u.formatMoneyInput(input),formatted);
 assert.equal(u.moneyInputValue(formatted),value);
}
for(const text of ['', '  ', '12,34', '₩100', '-1','1 000','Infinity','NaN']) assert.equal(u.moneyInputValue(text),null);
assert.equal(u.submittedRatePreview('1,765,433','2,000,000'),'88.2717');
''')


def test_both_automatic_rates_reopen_and_submit_independent_sources():
    run_js(BEHAVIOR_HARNESS, r'''
const basis={mode:'AUTO',basis_kind:'PLANNED_PRICE',basis_amount:2000000,basis_reference:'SYN planned price'};
u.openResultLearningDialog(u.normalizeResultLearningNotice({notice_key:'SYN-N',title:'SYN rates',outcomes:[{
 id:'SYN-R',department_id:'SYN-A',department_revision:1,source:'MANUAL_UI',status:'WON',
 updated_at:'2026-09-24T00:00:00Z',submitted_bid_amount:1765433,submitted_rate_calculation:basis,
 winning_bid_amount:1800000,winning_rate_calculation:{...basis,basis_kind:'BASE_AMOUNT',basis_reference:'SYN base amount'}}]}));
assert.equal(u.els.resultLearningSubmittedAmount.value,'1,765,433');
assert.equal(u.els.resultLearningWinningAmount.value,'1,800,000');
assert.equal(u.els.resultLearningSubmittedRate.value,'88.2717');
assert.equal(u.els.resultLearningWinningRate.value,'90.0000');
u.els.resultLearningWinningRateBasisAmount.value='2,500,000';
const saving=u.saveResultLearning({preventDefault(){}});await tick();
assert.equal(requests.length,1);
const body=JSON.parse(requests[0].options.body);
assert.equal(body.submitted_bid_rate,88.2717);assert.equal(body.winning_bid_rate,72);
assert.equal(body.winning_rate_calculation.basis_reference,'SYN base amount');
assert.equal(body.submitted_rate_calculation.basis_reference,'SYN planned price');
respond(requests[0],422,{detail:'SYN stop before reload'});await saving;
u.els.resultLearningWinningRateBasisReference.value='';
assert.equal(u.updateResultLearningRate(),false);
assert.equal(u.els.resultLearningWinningRate.value,'');
''')


def test_explicit_open_scope_survives_search_and_shared_link():
    harness = FILTER_HARNESS.replace("globalThis.ui={state,els,", "globalThis.ui={state,els,buildNoticeRequestPath,")
    run_js(harness, r'''
u.state.noticeScopeChoice='OPEN';u.els.searchInput.value='SYN education';
assert.equal(new URL(u.buildNoticeRequestPath(),'https://syn.invalid').searchParams.get('status'),'OPEN');
const link=u.noticeFilterHref();assert.equal(new URL(link).searchParams.get('scope'),'OPEN');
resetFields();u.state.noticeScopeChoice=null;location(link);u.restoreNoticeFiltersFromRoute();
assert.equal(u.state.noticeScopeChoice,'OPEN');assert.equal(u.els.searchInput.value,'SYN education');
u.state.notices=[notice('SYN education OPEN'),{...notice('SYN education ENDED'),deadline:'2020-01-01T00:00:00Z'}];
u.applyFilters();assert.equal(u.state.filteredNotices.length,1);
u.state.noticeScopeChoice='ALL';
assert.equal(new URL(u.buildNoticeRequestPath(),'https://syn.invalid').searchParams.has('status'),false);
''')


def test_all_pages_are_read_past_one_thousand_notices_without_duplicates():
    harness = FILTER_HARNESS.replace("globalThis.ui={state,els,", "globalThis.ui={state,els,fetchNoticePages,")
    run_js(harness, r'''
const all=Array.from({length:1001},(_,i)=>({notice_key:'SYN-'+i}));
const reading=u.fetchNoticePages({statusScope:'ALL'});
for(let offset=0;offset<1001;offset+=200){
 await new Promise(setImmediate);
 const request=requests.at(-1), params=new URL(request.path,'https://syn.invalid').searchParams;
 assert.equal(Number(params.get('offset')),offset);assert.equal(params.has('status'),false);
 request.resolve(all.slice(offset,offset+200));
}
const rows=await reading;
assert.equal(rows.length,1001);assert.equal(new Set(rows.map(r=>r.notice_key)).size,1001);
assert.equal(rows.at(-1).notice_key,'SYN-1000');
''')
