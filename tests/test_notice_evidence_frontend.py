"""Evidence gaps remain visible without inventing facts or numeric risk values."""
import subprocess
import tempfile
from pathlib import Path

from test_notice_quantitative_frontend import APP, HARNESS as QUANT_HARNESS

HARNESS = QUANT_HARNESS.replace(
    "renderQuantitativeEstimate,renderQuantitativeEstimateRow,",
    "renderQuantitativeEstimate,renderQuantitativeEstimateRow,renderRiskPanel,renderQuantitativeEvidenceSummary,observeVisibleQuantitativeSummaries,",
)


def _run(script):
    with tempfile.TemporaryDirectory() as directory:
        runner = Path(directory) / "notice-evidence-test.cjs"
        runner.write_text(HARNESS + "\n(async()=>{\n" + script
                          + "\n})().catch(e=>{console.error(e);process.exitCode=1;});", encoding="utf-8")
        result = subprocess.run(
            ["node", str(runner)],
            input=APP.read_text(encoding="utf-8"), capture_output=True,
            text=True, encoding="utf-8", timeout=20,
        )
    assert result.returncode == 0, result.stderr


def test_missing_risks_show_six_checklists_without_zero_or_a_fabricated_total():
    _run(r'''
u.renderRiskPanel({...qnotice,analysisState:'EVALUATED',riskScore:null,riskAxes:[]});
assert.match(u.els.riskTotalLabel.textContent,/산정 보류.*0\/6/);
const html=u.els.riskBars.innerHTML;
assert.equal((html.match(/class="risk-row is-unknown"/g)||[]).length,6);
for(const label of ['자격 조건','수행 역량','경쟁 강도','수익성','운영 부담','문서 품질']) assert.ok(html.includes(label));
assert.match(html,/확인할 내용/);assert.doesNotMatch(html,/aria-label="[^"]+ 0점"/);
assert.doesNotMatch(html,/empty-panel/);
u.renderRiskPanel({...qnotice,analysisState:'EVALUATED',riskScore:40,riskAxes:[{label:'자격 조건',score:0},{label:'수행 역량',score:25}]});
assert.match(u.els.riskTotalLabel.textContent,/2\/6/);
assert.match(u.els.riskBars.innerHTML,/자격 조건 0점/);
assert.equal((u.els.riskBars.innerHTML.match(/class="risk-row is-unknown"/g)||[]).length,4);
u.renderRiskPanel({...qnotice,analysisState:'COLLECTED',riskScore:40,riskAxes:[{label:'자격 조건',score:0}]});
assert.equal(u.els.riskTotalLabel.textContent,'분석 전');assert.doesNotMatch(u.els.riskBars.innerHTML,/자격 조건 0점/);
const legacyAxes=['자격 조건','증빙 완전성','경쟁 강도','제안 일정','수행 운영','데이터 품질'].map(label=>({label,score:20}));
u.renderRiskPanel({...qnotice,analysisState:'EVALUATED',riskScore:20,riskAxes:legacyAxes});
assert.equal((u.els.riskBars.innerHTML.match(/class="risk-row /g)||[]).length,6);
assert.match(u.els.riskBars.innerHTML,/증빙 완전성 20점/);
''')


def test_quantitative_basis_and_missing_inputs_use_only_public_fields():
    _run(r'''
const html=u.renderQuantitativeEvidenceSummary({...data,company_value:'SYN-PRIVATE',source_payload:'SYN-RAW',
 criteria:[{label:'SYN credit',status:'CONFIRMED',lower_points:5,upper_points:5},
 {label:'SYN <experience>',status:'ESTIMATED',lower_points:0,upper_points:3,rationale:'SYN <period> 확인'}]},
 {publicEvidenceHidden:true,sourceLabel:'SYN public source',activationReasons:['SYN coverage incomplete']});
assert.match(html,/산출 근거/);assert.match(html,/확인 필요 사항/);
assert.match(html,/정량 대상 2개 항목 중 점수 표시 2개/);
assert.match(html,/SYN &lt;experience&gt;/);assert.match(html,/SYN &lt;period&gt;/);
assert.match(html,/SYN coverage incomplete/);assert.match(html,/내부 증빙은 공개 화면에서 숨깁니다/);
assert.doesNotMatch(html,/SYN-PRIVATE|SYN-RAW|<experience>|<period>|empty-panel/);
const empty=u.renderQuantitativeEvidenceSummary({},{});
assert.match(empty,/항목별 산출 근거가 없습니다/);assert.match(empty,/적용 증빙 연결/);
const na=u.renderQuantitativeEvidenceSummary({}, {notApplicable:true});
assert.match(na,/적용 대상이 아닙니다/);assert.doesNotMatch(na,/적용 증빙 연결/);
''')


def test_optional_score_requests_wait_until_notice_paging_finishes():
    _run(r'''
let observers=0;
context.IntersectionObserver=class {constructor(){observers++;}disconnect(){}observe(){}};
u.state.noticeListPartial=true;u.observeVisibleQuantitativeSummaries();
assert.equal(observers,0);assert.equal(requests.length,0);
u.state.noticeListPartial=false;u.observeVisibleQuantitativeSummaries();assert.equal(observers,1);
''')


def test_list_actions_keep_full_detail_and_refresh_accessible_with_short_labels():
    _run(r'''
setData(data);
const html=u.noticeListActions(qnotice,false);
assert.match(html,/notice-detail-actions/);assert.match(html,/>점수 갱신<\/button>/);
assert.match(html,/data-load-quantitative=/);assert.match(html,/data-open-notice/);
assert.match(html,/전체 상세 보기/);assert.doesNotMatch(html,/>정량 점수 새로 확인</);
''')
