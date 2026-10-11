"""Exercise the read-only company basis panel on the performance screen in app.js."""

from pathlib import Path
import json
import subprocess


ROOT = Path(__file__).parents[1]
APP = ROOT / "src/pai_loop/static/app.js"
INDEX = ROOT / "src/pai_loop/static/index.html"
PROFILE = ROOT / "src/pai_loop/data/company_public_profile.json"

HARNESS = r'''
const assert=require("node:assert/strict"),vm=require("node:vm");
const source=require("node:fs").readFileSync(0,"utf8");
const requests=[];
function element(){
 return {hidden:false,textContent:"",innerHTML:"",children:[],replaceChildren(){this.innerHTML="";this.children=[];},addEventListener(){}};
}
const document={documentElement:{dataset:{}},addEventListener(){},getElementById(){return null;},createElement:()=>element()};
const window={location:new URL("https://syn.invalid/performance"),matchMedia(){return {matches:false};},setTimeout,clearTimeout};
const context=vm.createContext({URL,URLSearchParams,Intl,console,document,window,navigator:{},history:{}});
context.respond=null;
context.request=async path=>{requests.push(path); return context.respond(path);};
const exported=`
apiRequest=(...args)=>globalThis.request(...args);
showToast=()=>{};
globalThis.ui={state,els,buildCompanyBasisGroups,renderCompanyBasis,loadCompanyBasis};`;
vm.runInContext(source.replace(/\}\)\(\);\s*$/,exported+"\n})();"),context);
const u=context.ui;
for(const id of ["companyBasisSection","companyBasisSummary","companyBasisGrid","companyBasisNote"]) u.els[id]=element();
'''


def _run(script: str, profile: dict | None = None) -> None:
    setup = f"const PROFILE={json.dumps(profile if profile is not None else {}, ensure_ascii=False)};\n"
    result = subprocess.run(
        ["node", "-e", HARNESS + setup + "\n(async()=>{\n" + script
         + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
        input=APP.read_text(encoding="utf-8"), capture_output=True,
        text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def _repository_profile() -> dict:
    return json.loads(PROFILE.read_text(encoding="utf-8"))


def test_repository_profile_is_fully_labelled_in_korean_groups() -> None:
    _run(r'''
const view=u.buildCompanyBasisGroups(PROFILE);
assert.equal(view.organization,"사단법인 한국능률협회");
assert.equal(view.factCount,Object.keys(PROFILE.facts).length);
assert.equal(view.unlabeledCount,0,"새 회사 사실 키는 한글 묶음에 등록해야 한다");
assert.deepEqual(JSON.parse(JSON.stringify(view.groups.map(g=>g.key))),["registration","industries","clearances","absent","evidence"]);
const byKey=Object.fromEntries(view.groups.map(g=>[g.key,g]));
assert.equal(byKey.registration.headline,"경쟁입찰참가자격 등록");
assert.equal(byKey.industries.headline,`${PROFILE.facts.industry_code_name_inventory.value.length}개 업종 등록`);
assert.ok(byKey.industries.items.some(i=>i.label==="5608 직업능력개발훈련수탁기관"));
assert.ok(byKey.industries.note.startsWith("지정 직업훈련시설 NCS "));
assert.ok(byKey.industries.items.some(i=>i.label==="여행업 보증보험"));
// 같은 사실이 두 번 보이지 않는다(업종코드로 보이는 인허가는 따로 나열하지 않음)
const all=view.groups.flatMap(g=>[g.headline,...g.items.map(i=>i.label)]);
assert.equal(new Set(all).size,all.length);
assert.ok(!all.includes("종합여행업"));
assert.equal(byKey.clearances.headline,`${byKey.clearances.items.length}개 항목 확인`);
assert.ok(byKey.clearances.items.some(i=>i.label==="부정당 제재 없음"));
assert.ok(byKey.absent.items.some(i=>i.label==="직접생산확인증명서"&&i.tag==="미보유 확인"));
assert.equal(byKey.evidence.items.length,PROFILE.evidence.length);
const labels=view.groups.flatMap(g=>[g.title,g.headline,...g.items.map(i=>i.label)]).join("\n");
assert.doesNotMatch(labels,/_clear|_registration|_certificate|COMPANY_|VERIFIED/);
''', _repository_profile())


def test_panel_loads_once_and_escapes_profile_text() -> None:
    profile = {
        "profile_version": "SYN-1",
        "organization": {"display_name": "합성 <b>법인</b>"},
        "evidence": [{"display_name": "<img src=x onerror=alert(1)>", "last_observed_at": "2026-10-01"}],
        "facts": {
            "bidder_registration": {"value": True, "evidence_state": "VERIFIED"},
            "sanction_clear": {"value": True, "evidence_state": "COMPANY_DECLARATION"},
            "sme_certificate": {"value": False, "evidence_state": "COMPANY_CONFIRMED_ABSENT"},
            "brand_new_fact": {"value": True, "evidence_state": "COMPANY_CONFIRMED"},
        },
    }
    _run(r'''
context.respond=()=>PROFILE;
await u.loadCompanyBasis();
await u.loadCompanyBasis();
assert.deepEqual(requests,["/company-profile"]);
const grid=u.els.companyBasisGrid.innerHTML;
assert.equal((grid.match(/<details class="company-basis-card"/g)||[]).length,4);
assert.doesNotMatch(grid,/<img/);
assert.match(grid,/&lt;img src=x onerror=alert\(1\)&gt;/);
assert.match(grid,/회사 신고/);
assert.match(u.els.companyBasisSummary.textContent,/^합성 <b>법인<\/b> · 참가자격 판정에 쓰는 공개 회사 정보 4개 항목$/);
assert.match(u.els.companyBasisNote.textContent,/프로필 SYN-1 .* 따로 표시하지 않은 항목 1개$/);
''', profile)


def test_panel_shows_a_plain_error_when_the_profile_cannot_load() -> None:
    _run(r'''
context.respond=()=>{throw new Error("network");};
await u.loadCompanyBasis();
assert.equal(u.state.companyBasis.loaded,false);
assert.match(u.els.companyBasisSummary.textContent,/불러오지 못했습니다/);
assert.equal(u.els.companyBasisGrid.innerHTML,"");
''')


def test_performance_screen_contains_the_panel_and_wires_the_loader() -> None:
    html = INDEX.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    for element_id in ("companyBasisSection", "companyBasisSummary", "companyBasisGrid", "companyBasisNote"):
        assert f'id="{element_id}"' in html
        assert f'"{element_id}"' in app
    assert html.index('id="companyBasisSection"') < html.index('aria-labelledby="performanceEditorHeading"')
    loader = app[app.index("async function loadPerformance("):]
    assert "void loadCompanyBasis();" in loader[:400]


def test_cards_start_folded_and_list_every_item_in_two_columns() -> None:
    _run(r'''
context.respond=()=>PROFILE;
await u.loadCompanyBasis();
const grid=u.els.companyBasisGrid.innerHTML;
const cards=grid.match(/<details class="company-basis-card"[^>]*>/g)||[];
assert.equal(cards.length,5);
assert.ok(cards.every(card=>!/\bopen\b/.test(card)),"카드는 기본으로 접혀 있어야 한다");
const industries=grid.slice(grid.indexOf('data-group="industries"'),grid.indexOf('data-group="clearances"'));
const total=PROFILE.facts.industry_code_name_inventory.value.length+1;
assert.match(industries,new RegExp(`<span class="company-basis-card__toggle">${total}개 항목 보기</span>`));
assert.match(industries,/<ul class="company-basis-list">/);
assert.equal((industries.match(/<li>/g)||[]).length,total);
assert.match(industries,/title="1426 [^"]+"/);
''', _repository_profile())
