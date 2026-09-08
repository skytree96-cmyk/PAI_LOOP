"""Synthetic account UI tests; never use a deployed account or password."""
import json
import subprocess

import pytest

from test_department_accounts_frontend import APP, BEHAVIOR_HARNESS


HARNESS = BEHAVIOR_HARNESS.replace(
    "activateManagedAccount,toggleManagedAccountPaidAccess,",
    "activateManagedAccount,toggleManagedAccountPaidAccess,resetManagedAccountPassword,clearManagedPasswordInputs,renderManagedAccounts,",
) + r'''
const admin=payload('SYN-ADMIN');admin.account.role='ADMIN';admin.capabilities={manage_accounts:true};u.applyAccountSession(admin);
const row={id:'SYN-target',role:'DEPARTMENT',username:'SYN-user',department_name:'SYN department',active:true,paid_analysis_allowed:true,revision:4};
u.state.managedAccounts.records=[row];
const passwordInput={value:'',dataset:{managedPassword:row.id}};
u.els.accountManagementList.querySelectorAll=()=>[passwordInput];
'''


def run_ui(script):
    result = subprocess.run(
        ['node', '-e', HARNESS + '\n(async()=>{\n' + script + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'],
        input=APP.read_text(encoding='utf-8'), capture_output=True, text=True, encoding='utf-8',
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('password', ['Q7!', 'SYN-new-password-value', 'S' * 256])
def test_reset_is_masked_cas_secret_ephemeral_and_serialized(password):
    run_ui('const secret=' + json.dumps(password) + ';' + r'''
u.renderManagedAccounts();
assert.match(u.els.accountManagementList.innerHTML,/type="password" autocomplete="new-password"/);
assert.match(u.els.accountManagementList.innerHTML,/minlength="3" maxlength="256"/);
passwordInput.value=secret;
const saving=u.resetManagedAccountPassword(row.id);await tick();
assert.equal(passwordInput.value,'');assert.equal(requests.length,1);
assert.equal(requests[0].options.method,'PATCH');
assert.deepEqual(JSON.parse(requests[0].options.body),{expected_revision:4,password:secret});
assert.equal(requests[0].options.headers.get('X-CSRF-Token'),'SYN-CSRF-SYN-ADMIN');
assert.equal(requests[0].options.headers.has('X-PAI-LOOP-API-KEY'),false);
assert.equal(u.els.accountManagementRefresh.disabled,true);
assert.match(u.els.accountManagementList.innerHTML,/data-reset-password[^>]*disabled/);
await u.resetManagedAccountPassword(row.id);await u.toggleManagedAccountPaidAccess(row.id);await u.activateManagedAccount(row.id);await u.loadManagedAccounts();
assert.equal(requests.length,1);
respond(requests[0],200,{...row,revision:5,password:'SYN-unexpected-secret-field'});await saving;
assert.equal(u.state.managedAccounts.records[0].revision,5);
assert.equal(u.state.managedAccounts.records[0].paid_analysis_allowed,true);
assert.equal(u.state.managedAccounts.records[0].active,true);
assert.equal('password' in u.state.managedAccounts.records[0],false);
assert.equal(JSON.stringify(u.state).includes(secret),false);
assert.equal(u.els.accountManagementList.innerHTML.includes(secret),false);
assert.equal(u.els.accountManagementStatus.textContent.includes(secret),false);
assert.equal(passwordInput.value,'');assert.equal(requests.length,1);
''')


@pytest.mark.parametrize('password', ['Q7', 'S' * 257])
def test_reset_bounds_clear_input_without_request(password):
    run_ui('passwordInput.value=' + json.dumps(password) + ';' + r'''
await u.resetManagedAccountPassword(row.id);
assert.equal(requests.length,0);assert.equal(passwordInput.value,'');
assert.match(u.els.accountManagementStatus.textContent,/3자 이상 256자 이하/);
''')


@pytest.mark.parametrize('failure', ['unavailable', 'wrong-ack'])
def test_ambiguous_password_write_cannot_be_replayed_even_after_list_refresh(failure):
    run_ui('const failure=' + json.dumps(failure) + ';' + r'''
passwordInput.value='Q7!';const saving=u.resetManagedAccountPassword(row.id);await tick();
respond(requests[0],failure==='unavailable'?503:200,failure==='unavailable'?{detail:'SYN-PRIVATE-CANARY'}:{...row,revision:4});await saving;
assert.equal(u.state.managedAccounts.passwordUnconfirmed.has(row.id),true);
assert.match(u.els.accountManagementStatus.textContent,/재전송하지 않습니다/);
assert.equal(u.els.accountManagementStatus.textContent.includes('SYN-PRIVATE-CANARY'),false);
assert.match(u.els.accountManagementList.innerHTML,/data-reset-password[^>]*disabled/);
const refreshing=u.loadManagedAccounts();await tick();
respond(requests[1],200,{accounts:[{...row,revision:5}]});await refreshing;
passwordInput.value='Q7!';await u.resetManagedAccountPassword(row.id);
assert.equal(requests.length,2);assert.equal(requests[1].options.method,undefined);
assert.equal(u.state.managedAccounts.passwordUnconfirmed.has(row.id),true);
''')


@pytest.mark.parametrize('status', [409, 422])
def test_known_rejection_requires_fresh_read_without_replay(status):
    run_ui('const status=' + str(status) + ';' + r'''
passwordInput.value='Q7!';const saving=u.resetManagedAccountPassword(row.id);await tick();
respond(requests[0],status,{detail:'SYN rejection'});await saving;
assert.equal(u.state.managedAccounts.records.length,0);
assert.match(u.els.accountManagementStatus.textContent,/변경되지 않았습니다/);
await u.resetManagedAccountPassword(row.id);assert.equal(requests.length,1);
''')


@pytest.mark.parametrize('status', [200, 503])
def test_self_password_reset_locks_private_ui_until_explicit_relogin(status):
    run_ui('const status=' + str(status) + ';' + r'''
row.id='SYN-ADMIN';row.role='ADMIN';passwordInput.dataset.managedPassword=row.id;
passwordInput.value='Q7!';u.state.resultLearning.records=[{noticeKey:'SYN-private'}];
const saving=u.resetManagedAccountPassword(row.id);await tick();
respond(requests[0],status,status===200?{...row,revision:5}:{detail:'SYN ambiguous'});await saving;
assert.equal(passwordInput.value,'');assert.equal(u.state.accountSession.authenticated,false);
assert.equal(context.document.body.cleared,true);assert.equal(u.state.managedAccounts.records.length,0);
assert.equal(u.state.resultLearning.records.length,0);assert.equal(context.window.location.replaced,undefined);
if(status===200){
 assert.equal(context.document.created.find(node=>node.tag==='h1').textContent,'계정 정보가 변경되어 다시 로그인해야 합니다');
 assert.equal(context.document.created.find(node=>node.tag==='p').textContent,'변경된 계정 정보로 다시 로그인해 주세요.');
}
await u.resetManagedAccountPassword(row.id);assert.equal(requests.length,1);
context.document.created.find(node=>node.tag==='button').events.click();
assert.equal(context.window.location.replaced,'https://syn.invalid/notices?notice=SYN-N');
assert.equal(requests.length,1);
''')


def test_department_and_late_admin_response_cannot_restore_private_state():
    run_ui(r'''
login('SYN-DEPT');u.state.managedAccounts.records=[row];passwordInput.value='Q7!';
await u.resetManagedAccountPassword(row.id);assert.equal(requests.length,0);
u.applyAccountSession(admin);u.state.managedAccounts.records=[row];
const saving=u.resetManagedAccountPassword(row.id);await tick();
login('SYN-OTHER');respond(requests[0],200,{...row,revision:5});await saving;
assert.equal(u.state.accountSession.account.id,'SYN-OTHER');
assert.equal(u.state.managedAccounts.records.length,0);assert.equal(passwordInput.value,'');
''')


def test_password_cleanup_does_not_keep_values_when_dialog_closes():
    run_ui(r'''
passwordInput.value='SYN-new-password-value';u.clearManagedPasswordInputs();
assert.equal(passwordInput.value,'');assert.equal(requests.length,0);
''')
    source = APP.read_text(encoding='utf-8')
    assert 'els.accountPassword.value = ""; clearManagedPasswordInputs();' in source
