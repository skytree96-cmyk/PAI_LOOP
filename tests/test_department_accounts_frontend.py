from pathlib import Path
import subprocess

APP = Path(__file__).parents[1] / 'src/pai_loop/static/app.js'


def test_department_frontend_ownership_versions_and_permissions():
    script = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const source=require('node:fs').readFileSync(0,'utf8');
const context=vm.createContext({URL,URLSearchParams,Intl,
 document:{documentElement:{dataset:{}},getElementById(){return null;},addEventListener(){}},
 window:{matchMedia(){return {matches:false}}}});
const exported=`globalThis.ui={state,els,normalizeNotice,normalizeDecisionRecord,normalizeResultLearningNotice,
 ownDepartmentRecords,compareDepartmentRevision,canWriteDecision,canWriteResults,manualAnalysisAvailability,
 accountMutationHeaders,operatorDecisionListAvailable};`;
vm.runInContext(source.replace(/\}\)\(\);\s*$/,exported+'\n})();'),context);
const u=context.ui;
u.state.source='api';u.state.manualAnalysisEnabled=true;u.state.accessMode='PUBLIC_READ_ONLY';
u.state.accountSession={enabled:true,authenticated:true,account:{id:'SYN-A',role:'DEPARTMENT',department_id:'SYN-A',department_name:'SYN A'},
 capabilities:{write_decisions:true,write_results:true,recompute_analysis:true,request_paid_analysis:false},csrfToken:'SYN-CSRF'};
const decisions=[
 {id:'SYN-legacy',actor_label:'SYN A',choice:'GO',rationale:'legacy is unassigned',created_at:'2099-01-01T00:00:00Z'},
 {id:'SYN-B1',department_id:'SYN-B',department_revision:99,choice:'NO_GO',rationale:'other department',created_at:'2099-01-01T00:00:00Z'},
 {id:'SYN-A1',department_id:'SYN-A',department_revision:1,choice:'GO',rationale:'older revision newer clock',created_at:'2099-01-01T00:00:00Z'},
 {id:'SYN-A2',department_id:'SYN-A',department_revision:2,choice:'HOLD',rationale:'latest own',created_at:'2026-01-01T00:00:00Z'}];
let raw={notice_key:'SYN-NOTICE',title:'SYN',status:'OPEN',source_kind:'PPS',decisions};
let n=u.normalizeNotice(raw,0,{operatorDecisionsLoaded:true});
assert.equal(n.decision,'HOLD'); assert.equal(n.decisionComment,'latest own');assert.equal(n.decisions.length,4);
assert.equal(u.ownDepartmentRecords(n.decisions).sort(u.compareDepartmentRevision)[0].id,'SYN-A2');
assert.equal(u.operatorDecisionListAvailable([n]),true);
assert.equal(u.operatorDecisionListAvailable([{...n,decisionReadStatus:'ERROR'}]),false);
assert.equal(u.accountMutationHeaders()['X-CSRF-Token'],'SYN-CSRF');
n=u.normalizeNotice({...raw,decisions:decisions.slice(0,2),decision:'GO',comment:'other legacy fallback'},0,{operatorDecisionsLoaded:true});
assert.equal(n.decision,'');assert.equal(n.decisionComment,'');
const rows=decisions.map(d=>({...d,status:'LOST',source:'MANUAL_UI',updated_at:d.created_at}));
let r=u.normalizeResultLearningNotice({notice_key:'SYN',outcomes:rows,latest_outcome:rows[1]});
assert.equal(r.outcome.id,'SYN-A2');assert.equal(r.expectedOutcomeId,'SYN-A2');assert.equal(r.departmentOutcomes.length,4);
r=u.normalizeResultLearningNotice({notice_key:'SYN',outcomes:rows.slice(0,2),latest_outcome:rows[1]});
assert.equal(r.outcome,null);assert.equal(r.expectedOutcomeId,null);
const active={sourceKind:'PPS',noticeStatus:'OPEN',deadline:'2099-01-01',analysisState:'NOT_EVALUATED',analysisAttachmentCoverageComplete:false};
assert.equal(u.manualAnalysisAvailability(active).code,'ACCOUNT_ANALYSIS_FORBIDDEN');
u.state.accountSession.capabilities.request_paid_analysis=true;
assert.equal(u.manualAnalysisAvailability(active).enabled,true);
const reviewed={...active,analysisState:'EVALUATED',analysisAttachmentCoverageComplete:true};
u.state.accountSession.capabilities.request_paid_analysis=false;
assert.equal(u.manualAnalysisAvailability(reviewed).code,'RECOMPUTE_CURRENT');
u.state.accountSession.account={id:'SYN-admin',role:'ADMIN'};
u.state.accountSession.capabilities={write_decisions:false,write_results:false,recompute_analysis:true,request_paid_analysis:false};
assert.equal(u.canWriteDecision(),false);assert.equal(u.canWriteResults(),false);
assert.equal(u.normalizeNotice(raw).decision,'');
u.state.accountSession.authenticated=false;
assert.equal(u.accountMutationHeaders(),null);
assert.equal(u.manualAnalysisAvailability(active).code,'ACCOUNT_LOGIN_REQUIRED');
u.state.accountSession.enabled=false;u.state.writeControlsEnabled=true;
assert.equal(u.canWriteDecision(),true);
'''
    result = subprocess.run(['node', '-e', script], input=APP.read_text(encoding='utf-8'), capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
