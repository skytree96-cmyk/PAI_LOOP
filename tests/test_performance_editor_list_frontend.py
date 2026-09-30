"""Exercise the registered-performance list paging, filtering and sorting in app.js."""

from pathlib import Path
import subprocess


APP = Path(__file__).parents[1] / "src/pai_loop/static/app.js"

HARNESS = r'''
const assert=require("node:assert/strict"),vm=require("node:vm");
const source=require("node:fs").readFileSync(0,"utf8");
const requests=[];
function element(tag="div"){
 const node={tag,hidden:false,disabled:false,textContent:"",innerHTML:"",value:"",children:[],
  closest(){return null;},addEventListener(){}};
 if(tag==="select"){
  let selected="";
  Object.defineProperty(node,"innerHTML",{get(){return "";},set(html){
   this.options=[...html.matchAll(/<option value="([^"]*)"/g)].map(match=>({value:match[1]}));}});
  node.options=[{value:""}];
  Object.defineProperty(node,"value",{get(){return selected;},set(value){selected=this.options.some(option=>option.value===value)?value:"";}});
 }
 return node;
}
const document={documentElement:{dataset:{}},addEventListener(){},getElementById(){return null;},createElement:tag=>element(tag)};
const window={location:new URL("https://syn.invalid/performance"),matchMedia(){return {matches:false};},setTimeout,clearTimeout};
const context=vm.createContext({URL,URLSearchParams,Intl,console,document,window,navigator:{},history:{}});
context.request=async path=>{
 requests.push(path);
 const offset=Number(new URL(path,"https://syn.invalid").searchParams.get("offset")||0);
 const rows=Array.from({length:Math.max(Math.min(200,450-offset),0)},(_,i)=>{
  const n=offset+i;
  return {id:`SYN-${n}`,record_status:"VALIDATED",project_name:`합성 사업 ${String(n).padStart(3,"0")}`,
   agency:n%2?"합성 기관 가":"합성 기관 나",division:["합성 부서 B","합성 부서 A",""][n%3],
   contract_date:`20${String(10+n%15).padStart(2,"0")}-01-01`,contract_amount:n*1000};
 });
 return {records:rows,total:450};
};
const exported=`
apiRequest=(...args)=>globalThis.request(...args);
manualAnalysisAuthHeaders=async()=>({});
showToast=()=>{};
globalThis.ui={state,els,loadPerformanceEditor,renderPerformanceEditor,changePerformanceEditorPage};`;
vm.runInContext(source.replace(/\}\)\(\);\s*$/,exported+"\n})();"),context);
const u=context.ui;
for(const id of ["performanceEditorSummary","performanceEditorUnlockButton","performanceEditorCreateButton","performanceEditorList",
 "performanceEditorState","performanceEditorFilterForm","performanceEditorPagination","performanceEditorPageRange",
 "performanceEditorPageLabel","performanceEditorPreviousButton","performanceEditorNextButton","performanceEditorSearchInput"]){
 u.els[id]=element();
}
u.els.performanceEditorDivisionFilter=element("select");
u.els.performanceEditorSort=element("select");
u.state.accountEpoch=1;
u.state.performance={summary:{recordCount:0}};
'''


def _run(script: str) -> None:
    result = subprocess.run(
        ["node", "-e", HARNESS + "\n(async()=>{\n" + script
         + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
        input=APP.read_text(encoding="utf-8"), capture_output=True,
        text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_registered_performance_list_loads_every_page_and_shows_twenty_rows() -> None:
    _run(r'''
await u.loadPerformanceEditor({force:true});
assert.deepEqual(requests,["/performance-records?limit=200","/performance-records?limit=200&offset=200","/performance-records?limit=200&offset=400"]);
assert.equal(u.state.performanceEditor.records.length,450);
assert.equal((u.els.performanceEditorList.innerHTML.match(/<article/g)||[]).length,20);
assert.equal(u.els.performanceEditorPageLabel.textContent,"1 / 23");
assert.equal(u.els.performanceEditorPreviousButton.disabled,true);
assert.equal(u.els.performanceEditorFilterForm.hidden,false);
u.changePerformanceEditorPage(1);
assert.equal(u.els.performanceEditorPageLabel.textContent,"2 / 23");
assert.match(u.els.performanceEditorPageRange.textContent,/^21–40 \/ 450건$/);
''')


def test_registered_performance_list_filters_and_sorts_on_the_client() -> None:
    _run(r'''
await u.loadPerformanceEditor({force:true});
const editor=u.state.performanceEditor;
assert.deepEqual(u.els.performanceEditorDivisionFilter.options.map(option=>option.value),["","합성 부서 A","합성 부서 B"]);
editor.sort="amount";editor.page=1;u.renderPerformanceEditor();
assert.match(u.els.performanceEditorList.innerHTML,/합성 사업 449/);
editor.division="합성 부서 A";editor.query="기관 가";editor.page=1;u.renderPerformanceEditor();
const titles=[...u.els.performanceEditorList.innerHTML.matchAll(/<h4>([^<]+)<\/h4>/g)].map(match=>match[1]);
assert.ok(titles.length>0);
for(const title of titles){const n=Number(title.slice(-3));assert.equal(n%3,1);assert.equal(n%2,1);}
editor.sort="division";editor.division="";editor.query="";editor.page=23;u.renderPerformanceEditor();
assert.match(u.els.performanceEditorList.innerHTML,/수행부서 미입력/);
editor.query="없는 사업";editor.page=1;u.renderPerformanceEditor();
assert.equal(u.els.performanceEditorPagination.hidden,true);
assert.match(u.els.performanceEditorState.innerHTML,/조건에 맞는 실적이 없습니다/);
''')
