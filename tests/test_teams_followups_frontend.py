from __future__ import annotations

import subprocess
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "src" / "pai_loop" / "static" / "app.js"


def test_personal_teams_followups_request_recovery_and_session_isolation() -> None:
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = require('node:fs').readFileSync(0, 'utf8');
const context = vm.createContext({
  document: {documentElement: {dataset: {}}, getElementById() {return null;},
    addEventListener() {}, querySelectorAll() {return [];}, activeElement: null},
  window: {matchMedia() {return {matches: false};}}, URL, URLSearchParams,
});
const exports = `globalThis.ui = {state, els, normalizeNotice, loadTeamsFollowups,
  clearTeamsFollowups, toggleTeamsFollow, teamsFollowItem, teamsFollowAction,
  renderTeamsFollowupItem, createTeamsLinkCode, clearTeamsLinkCode,
  setApi(fn) {apiRequest = fn;}, setToast(fn) {showToast = fn;}};`;
vm.runInContext(source.replace(/\}\)\(\);\s*$/, exports + '\n})();'), context);
const u = context.ui;
const toasts = [];
u.setToast((...args) => toasts.push(args));
const el = () => ({hidden: false, disabled: false, textContent: '', innerHTML: '', value: '',
  attrs: {}, setAttribute(k, v) {this.attrs[k] = v;}, removeAttribute(k) {delete this[k];},
  replaceChildren() {this.innerHTML = '';}, showModal() {this.open = true;}, close() {this.open = false;}});
for (const id of ['teamsFollowsDialog', 'teamsFollowsStatus', 'teamsFollowsSummary',
  'teamsFollowsError', 'teamsFollowsDeliveryNotice', 'teamsFollowsRefresh', 'teamsLinkButton', 'teamsBotChatLink',
  'teamsLinkCodePanel', 'teamsLinkCommand', 'teamsLinkExpiry', 'teamsFollowsList',
  'teamsFollowsEmpty', 'teamsPendingFollow', 'teamsPendingFollowLabel', 'teamsPendingFollowButton',
  'teamsBriefingToggle', 'teamsBriefingEnabled']) u.els[id] = el();
u.state.source = 'api';
u.state.accountSession = {enabled: true, authenticated: true, csrfToken: 'SYN-csrf'};
const notice = u.normalizeNotice({notice_key: 'SYN-notice/key', title: 'SYN 공고',
  status: 'OPEN', deadline: '2099-09-13T08:00:00Z'});
u.state.notices = [notice];
const item = {notice_id: 'SYN-id', notice_key: notice.noticeKey, notice_title: notice.title,
  active: true, deadline: '2099-09-13T08:00:00Z', deliveries: [
    {event_kind: 'REGISTERED', status: 'PENDING', scheduled_at: '2099-09-01T00:00:00Z'}]};
const connected = {enabled: true, connected: true, bot_chat_url: 'https://teams.microsoft.com/l/chat/0/0'};
const deferred = () => {let resolve, reject; const promise = new Promise((a, b) => {resolve = a; reject = b;}); return {promise, resolve, reject};};
(async () => {
  const calls = [];
  u.setApi(async (path, options) => {calls.push([path, options]); return path === '/teams/connection' ? connected : {items: [], enabled: false};});
  await u.loadTeamsFollowups();
  assert.equal(u.state.teamsFollowups.connected, true);
  assert.equal(u.els.teamsFollowsDeliveryNotice.hidden, false);
  assert.match(u.els.teamsFollowsDeliveryNotice.textContent, /알림 발송이 아직 활성화되지/);
  assert.deepEqual(calls.map(x => x[0]), ['/teams/connection', '/teams/follows']);

  // No optimistic success, duplicate click, or channel endpoint during registration.
  const create = deferred();
  calls.length = 0;
  u.setApi((path, options) => {calls.push([path, options]); return create.promise;});
  const first = u.toggleTeamsFollow(notice.noticeKey);
  await u.toggleTeamsFollow(notice.noticeKey);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/teams/follows/SYN-notice%2Fkey');
  assert.equal(calls[0][1].method, 'POST');
  assert.equal(calls[0][1].headers['X-CSRF-Token'], 'SYN-csrf');
  assert.equal(u.teamsFollowItem(notice.noticeKey), undefined);
  assert.match(u.teamsFollowAction(notice), /disabled/);
  create.resolve(item);
  await first;
  assert.equal(u.teamsFollowItem(notice.noticeKey).notice_id, 'SYN-id');
  assert.match(u.teamsFollowAction(notice), /aria-pressed="true"/);
  assert.doesNotMatch(toasts.at(-1).join(' '), /전송됨|발송 완료/);

  // Failed removal retains the saved subscription and permits a retry.
  u.setApi(async () => {throw new Error('SYN temporary failure');});
  await u.toggleTeamsFollow(notice.noticeKey);
  assert.ok(u.teamsFollowItem(notice.noticeKey));
  assert.equal(u.state.teamsFollowups.pending.size, 0);
  assert.ok(u.state.teamsFollowups.error);
  u.setApi(async (path, options) => {assert.equal(options.method, 'DELETE'); return {active: false};});
  await u.toggleTeamsFollow(notice.noticeKey);
  assert.equal(u.teamsFollowItem(notice.noticeKey), undefined);

  // An absent personal pairing opens the connection flow, never a POST follow.
  u.state.teamsFollowups.connected = false;
  calls.length = 0;
  u.setApi(async (path, options) => {calls.push([path, options]); return {...connected, connected: false};});
  await u.toggleTeamsFollow(notice.noticeKey);
  assert.equal(u.els.teamsFollowsDialog.open, true);
  assert.equal(u.state.teamsFollowups.pendingNoticeKey, notice.noticeKey);
  assert.deepEqual(calls.map(x => x[0]), ['/teams/connection']);
  assert.equal(u.els.teamsPendingFollowButton.disabled, true);

  // Codes are explicit authenticated mutations, with allowlisted Teams links.
  u.setApi(async (path, options) => {
    assert.equal(path, '/teams/link-code');
    assert.equal(options.method, 'POST');
    assert.equal(options.headers['X-CSRF-Token'], 'SYN-csrf');
    return {code: 'SYN-link-code', expires_at: '2099-09-13T00:00:00Z', bot_chat_url: 'javascript:alert(1)'};
  });
  await u.createTeamsLinkCode();
  assert.equal(u.els.teamsLinkCommand.value, '연결 SYN-link-code');
  assert.equal(u.state.teamsFollowups.botChatUrl, '');
  assert.equal(u.els.teamsBotChatLink.hidden, true);
  u.clearTeamsLinkCode();
  assert.equal(u.els.teamsLinkCommand.value, '');

  // Logout/account change while a mutation is in flight cannot repopulate data.
  Object.assign(u.state.teamsFollowups, {connected: true, loaded: true});
  const stale = deferred();
  u.setApi(() => stale.promise);
  const inFlight = u.toggleTeamsFollow(notice.noticeKey);
  u.state.accountEpoch += 1;
  u.clearTeamsFollowups();
  stale.resolve(item);
  await inFlight;
  assert.equal(u.state.teamsFollowups.items.length, 0);
  assert.equal(u.state.teamsFollowups.connected, false);
  assert.equal(u.els.teamsLinkCommand.value, '');
  assert.equal(u.els.teamsFollowsDialog.open, false);

  // A late load also cannot restore another person's subscriptions.
  const staleRead = deferred();
  u.setApi(() => staleRead.promise);
  const reading = u.loadTeamsFollowups();
  u.state.accountEpoch += 1;
  u.clearTeamsFollowups();
  staleRead.resolve(connected);
  await reading;
  assert.equal(u.state.teamsFollowups.loaded, false);
  assert.equal(u.state.teamsFollowups.items.length, 0);

  // Stored notice text is escaped. Uncertain delivery is never shown as sent.
  const html = u.renderTeamsFollowupItem({...item, notice_title: '<img src=x onerror=alert(1)>',
    deliveries: [{event_kind: 'DEADLINE_DAY', status: 'UNKNOWN', scheduled_at: '2099-09-13T00:00:00Z'}]});
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img|전송됨/);
  assert.match(html, /전송 결과 확인 필요/);
  assert.match(html, /마감일 오전/);
})().catch(error => {process.stderr.write(error.stack); process.exitCode = 1;});
"""
    result = subprocess.run(
        ["node", "-e", script],
        input=APP_JS.read_text(encoding="utf-8"),
        text=True,
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_personal_followups_are_reachable_and_clear_with_account_state() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = APP_JS.with_name("index.html").read_text(encoding="utf-8")
    clear_account = source.split("function clearAccountPrivateState()", 1)[1].split(
        "function canWriteResults()", 1
    )[0]
    assert "clearTeamsFollowups();" in clear_account
    assert source.count("${teamsFollowAction(notice)}") == 2
    assert "renderDetailFollowAction(notice);" in source
    for element in ("teamsFollowsButton", "teamsFollowsDialog", "detailFollowButton",
                    "teamsPendingFollowButton", "teamsLinkCommand", "teamsFollowsRefresh"):
        assert f'id="{element}"' in html
    assert "마감 5일 전 오전 9시" in html
    assert "마감일 오전 9시" in html
    assert "새로 로그인하면 다시 연결해야 합니다" in html
    # The panel states the three steps, including that the bot stays silent.
    assert "봇은 답장하지 않습니다" in html
    assert html.count("<li>") >= 3
    feature = source.split("function bindTeamsFollowupEvents()", 1)[1].split(
        "function createDemoData()", 1
    )[0]
    assert "localStorage" not in feature
    assert "sessionStorage" not in feature
    assert "PAI_LOOP_API_KEY" not in feature
