"""Exercise top navigation with real event handlers and view selection."""

from pathlib import Path
import subprocess


APP = Path(__file__).parents[1] / "src/pai_loop/static/app.js"

HARNESS = r'''
const assert=require("node:assert/strict"),vm=require("node:vm");
const source=require("node:fs").readFileSync(0,"utf8");
const nodes=new Map(),listeners={},routes=[];
let mobile=false,viewportChange;
class HTMLElement {}
const document={documentElement:{dataset:{}},activeElement:null,
 getElementById(id){return nodes.get(id)||null;},querySelector(){return null;},
 addEventListener(type,handler){(listeners[type]??=[]).push(handler);}};
function element(id="",parent=null){
 const classes=new Set(),attributes={},events={};
 const el={id,parent,children:[],dataset:{},hidden:false,value:"",textContent:"",tagName:"DIV",
  classList:{contains:name=>classes.has(name),add:name=>classes.add(name),remove:name=>classes.delete(name),
   toggle(name,enabled){if(enabled??!classes.has(name))classes.add(name);else classes.delete(name);}},
  setAttribute(name,value){attributes[name]=String(value);},getAttribute:name=>attributes[name]??null,
  removeAttribute(name){delete attributes[name];},
  addEventListener(type,handler){(events[type]??=[]).push(handler);},
  fire(type){for(const handler of events[type]||[])handler({target:el});},
  contains(target){return target===el||el.children.some(child=>child.contains(target));},
  closest(selector){if(selector==="[data-nav-group]"&&el.dataset.navGroup)return el;return parent?.closest(selector)||null;},
  querySelector(selector){return selector===".nav-group-toggle[aria-controls]"?el.trigger||null:null;},
  matches(selector){return selector.split(",").some(tag=>tag.trim()===el.tagName.toLowerCase());},
  focus(){document.activeElement=el;},reset(){}};
 Object.setPrototypeOf(el,HTMLElement.prototype);
 if(id)nodes.set(id,el);if(parent)parent.children.push(el);return el;
}
document.body=element("body");
const media={get matches(){return mobile;},addEventListener(type,handler){assert.equal(type,"change");viewportChange=handler;}};
const context=vm.createContext({URL,URLSearchParams,Intl,console,document,HTMLElement,
 window:{location:{search:"",href:"https://syn.invalid/"},matchMedia(query){return query==="(max-width: 1100px)"?media:{matches:false};},setTimeout,clearTimeout}});
context.recordRoute=view=>routes.push(view);
const exported=`
closeDetail=()=>{};renderNoticeSearchMode=()=>{};
renderPreSpecificationView=()=>{};loadStoredPreSpecifications=async()=>{};
renderResultLearning=()=>{};loadResultLearning=async()=>{};loadPerformance=async()=>{};
renderCompanyAwardsView=()=>{};hideDemoBanner=()=>{};showDemoBanner=()=>{};
applyFilters=()=>{};renderDataSource=()=>{};syncRouteForView=view=>globalThis.recordRoute(view);
globalThis.ui={state,els,bindNavigationEvents,setView,handleGlobalKeydown,handleNavigationKeydown,
 toggleMobileMenu,closeMobileMenu,updateActiveNavigationGroup};`;
vm.runInContext(source.replace(/\}\)\(\);\s*$/,exported+"\n})();"),context);
const u=context.ui;
Object.setPrototypeOf(u.els,new Proxy({}, {get(_,key){return nodes.get(key)||element(key);}}));
u.els.appHeader=element("appHeader",document.body);
u.els.primaryNavigation=element("primaryNavigation",u.els.appHeader);
u.els.mobileMenuButton=element("mobileMenuButton",u.els.appHeader);
u.els.navItems=[];u.els.navGroupToggles=[];u.els.kpiViewButtons=[];
function item(view,parent=u.els.primaryNavigation){const el=element(view,parent);el.dataset.view=view;u.els.navItems.push(el);return el;}
const all=item("all");
function group(name,views){
 const group=element(name,u.els.primaryNavigation);group.dataset.navGroup=name;
 const trigger=element(name+"Trigger",group);group.trigger=trigger;
 const items=element(name+"Items",group);items.hidden=true;
 trigger.setAttribute("aria-controls",items.id);trigger.setAttribute("aria-expanded","false");
 u.els.navGroupToggles.push(trigger);for(const view of views)item(view,items);
 return {group,trigger,items};
}
const search=group("search",["new"]),decision=group("decision",["review","undecided"]),learning=group("learning",["closed","awards"]);
const performance=item("performance"),outside=element("outside",document.body);
function dispatch(type,target){for(const handler of listeners[type]||[])handler({target});}
function key(key,target){const event={key,target,prevented:false,preventDefault(){this.prevented=true;}};u.handleGlobalKeydown(event);return event;}
function resize(matches){mobile=matches;viewportChange({matches});}
function isOpen(group){return group.trigger.getAttribute("aria-expanded")==="true";}
function assertClosed(){for(const group of [search,decision,learning]){assert.equal(isOpen(group),false);assert.equal(group.items.hidden,true);}}
u.state.source="loading";u.state.loading=false;
u.bindNavigationEvents();
'''


def _run(script: str) -> None:
    result = subprocess.run(
        ["node", "-e", HARNESS + "\n" + script],
        input=APP.read_text(encoding="utf-8"),
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_dropdown_exclusivity_outside_interaction_and_view_selection() -> None:
    _run(r'''
assertClosed();
search.trigger.fire("click");assert.equal(isOpen(search),true);assert.equal(search.items.hidden,false);
decision.trigger.fire("click");assert.equal(isOpen(search),false);assert.equal(isOpen(decision),true);
decision.trigger.fire("click");assertClosed();
learning.trigger.fire("click");dispatch("click",outside);assertClosed();
search.trigger.fire("click");dispatch("focusin",outside);assertClosed();
decision.trigger.fire("click");nodes.get("review").fire("click");
assert.equal(u.state.currentView,"review");assert.equal(routes.at(-1),"review");
assert.equal(nodes.get("review").getAttribute("aria-current"),"page");
assert.equal(decision.group.classList.contains("is-current"),true);assertClosed();
assert.equal(document.activeElement,u.els.mainContent);
u.setView("prespec",{syncRoute:false,focusMain:false});
assert.equal(search.group.classList.contains("is-current"),true);
assert.equal(decision.group.classList.contains("is-current"),false);
assert.equal(nodes.get("new").getAttribute("aria-current"),"page");assertClosed();
all.fire("click");assert.equal(u.state.currentView,"all");
assert.equal(all.getAttribute("aria-current"),"page");
for(const group of [search,decision,learning])assert.equal(group.group.classList.contains("is-current"),false);
''')


def test_keyboard_disclosure_focus_and_native_tab_order() -> None:
    _run(r'''
decision.trigger.focus();
assert.equal(key("ArrowDown",decision.trigger).prevented,true);
assert.equal(isOpen(decision),true);assert.equal(document.activeElement,nodes.get("review"));
key("ArrowDown",document.activeElement);assert.equal(document.activeElement,nodes.get("undecided"));
key("ArrowDown",document.activeElement);assert.equal(document.activeElement,nodes.get("review"));
key("End",document.activeElement);assert.equal(document.activeElement,nodes.get("undecided"));
key("Home",document.activeElement);assert.equal(document.activeElement,nodes.get("review"));
assert.equal(key("Tab",document.activeElement).prevented,false);
assert.equal(key("Escape",document.activeElement).prevented,true);
assertClosed();assert.equal(document.activeElement,decision.trigger);
key("ArrowUp",decision.trigger);assert.equal(document.activeElement,nodes.get("undecided"));
assert.equal(key("Escape",document.activeElement).prevented,true);assertClosed();
assert.equal(key("Escape",decision.trigger).prevented,false);
assert.equal(key("ArrowDown",outside).prevented,false);
''')


def test_home_only_shows_dashboard_and_list_destinations_show_notices() -> None:
    _run(r'''
all.fire("click");
assert.equal(u.state.currentView,"all");
assert.equal(u.els.noticeSection.hidden,true,"home does not append a notice list below its dashboard");
for(const id of ["opportunityHero","opportunityKpis","analysisProgress"])
 assert.equal(u.els[id].hidden,false,id+" is visible on home");
for(const view of ["new","review","go","urgent","fail","cancelled","result-missing","undecided","collected"]){
 u.setView(view,{syncRoute:false,focusMain:false});
 assert.equal(u.els.noticeSection.hidden,false,view+" opens the notice list");
 for(const id of ["opportunityHero","opportunityKpis","analysisProgress"])
  assert.equal(u.els[id].hidden,true,view+" hides "+id);
}
for(const view of ["closed","awards","performance"]){
 u.setView(view,{syncRoute:false,focusMain:false});
 assert.equal(u.els.noticeSection.hidden,true,view+" keeps its dedicated content");
 assert.equal(u.els.opportunityKpis.hidden,true);
}
all.fire("click");
assert.equal(u.els.noticeSection.hidden,true);
assert.equal(u.els.opportunityKpis.hidden,false);
''')


def test_work_pipeline_queues_keep_their_own_view_instead_of_falling_back_home() -> None:
    """A queue with no route entry used to be rewritten to the dashboard.

    The card or menu item still looked selected while the list stayed
    unfiltered, so GO work that had left 진행 건 could not be reached at all.
    """

    _run(r'''
for(const view of ["pending-decision","in-progress","urgent-in-progress","result-missing-decided"]){
 u.setView(view,{focusMain:false});
 assert.equal(u.state.currentView,view,view+" must not fall back to the dashboard");
 assert.equal(routes.at(-1),view,view+" must own a route");
 assert.equal(u.els.noticeSection.hidden,false,view+" opens the notice list");
 assert.equal(u.els.opportunityKpis.hidden,true,view+" replaces the dashboard cards");
}
''')


def test_view_navigation_preserves_department_while_clearing_notice_filters() -> None:
    _run(r'''
u.els.departmentSelect.value="management-planning";
u.state.departmentSelectionAccountId="SYN-ACCOUNT";
all.fire("click");
for(const view of ["new","review","closed","awards","performance","all"]){
 u.els.searchInput.value="SYN previous search";
 u.els.priorityKeywordInput.value="SYN previous keyword";
 u.els.eligibilityFilter.value="FAIL";
 u.els.recommendationFilter.value="HOLD";
 u.setView(view,{syncRoute:false,focusMain:false});
 assert.equal(u.els.departmentSelect.value,"management-planning",view+" preserves chosen department");
 assert.equal(u.state.departmentSelectionAccountId,"SYN-ACCOUNT");
 assert.equal(u.els.searchInput.value,"");
 assert.equal(u.els.priorityKeywordInput.value,"");
 assert.equal(u.els.eligibilityFilter.value,"all");
 assert.equal(u.els.recommendationFilter.value,"all");
}
''')


def test_home_search_shortcut_opens_visible_notice_search_in_selected_department() -> None:
    _run(r'''
u.els.departmentSelect.value="management-planning";
all.fire("click");
assert.equal(u.els.noticeSection.hidden,true);
assert.equal(key("/",document.body).prevented,true);
assert.equal(u.state.currentView,"new");
assert.equal(u.els.noticeSection.hidden,false);
assert.equal(u.els.opportunityKpis.hidden,true);
assert.equal(document.activeElement,u.els.searchInput);
assert.equal(u.els.departmentSelect.value,"management-planning");
u.els.searchInput.tagName="INPUT";
assert.equal(key("/",u.els.searchInput).prevented,false,"typing slash in a search field remains native");
''')


def test_mobile_menu_escape_selection_and_viewport_reset_preserve_other_locks() -> None:
    _run(r'''
u.els.mobileMenuButton.fire("click");assert.equal(u.els.appHeader.classList.contains("is-open"),false);
resize(true);document.body.classList.add("is-locked");
u.els.mobileMenuButton.fire("click");assert.equal(u.els.appHeader.classList.contains("is-open"),true);
assert.equal(u.els.mobileMenuButton.getAttribute("aria-expanded"),"true");
assert.equal(u.els.mobileMenuButton.getAttribute("aria-label"),"메뉴 닫기");
learning.trigger.fire("click");key("Escape",learning.trigger);
assertClosed();assert.equal(u.els.appHeader.classList.contains("is-open"),true);
assert.equal(document.activeElement,learning.trigger);
key("Escape",learning.trigger);assert.equal(u.els.appHeader.classList.contains("is-open"),false);
assert.equal(document.activeElement,u.els.mobileMenuButton);
assert.equal(u.els.mobileMenuButton.getAttribute("aria-expanded"),"false");
assert.equal(document.body.classList.contains("is-locked"),true);
u.els.mobileMenuButton.fire("click");decision.trigger.fire("click");nodes.get("review").fire("click");
assertClosed();assert.equal(u.els.appHeader.classList.contains("is-open"),false);
u.els.mobileMenuButton.fire("click");dispatch("click",outside);
assert.equal(u.els.appHeader.classList.contains("is-open"),false);
u.els.mobileMenuButton.fire("click");decision.trigger.fire("click");u.els.mobileMenuButton.focus();resize(false);
assertClosed();assert.equal(u.els.appHeader.classList.contains("is-open"),false);
assert.equal(document.activeElement,all);
decision.trigger.fire("click");nodes.get("review").focus();resize(true);
assertClosed();assert.equal(document.activeElement,u.els.mobileMenuButton);
u.els.mobileMenuButton.fire("click");decision.trigger.fire("click");nodes.get("review").focus();resize(false);
assertClosed();assert.equal(document.activeElement,decision.trigger);
assert.equal(document.body.classList.contains("is-locked"),true);
''')
