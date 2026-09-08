(() => {
  "use strict";
  const form = document.getElementById("entryLoginForm");
  const username = document.getElementById("entryUsername");
  const password = document.getElementById("entryPassword");
  const submit = document.getElementById("entryLoginSubmit");
  const status = document.getElementById("entryLoginStatus");
  const retry = document.getElementById("entryLoginRetry");
  let pending = false;
  const message = (text, error = false) => { status.textContent = text; status.dataset.error = String(error); };
  async function request(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(`/api/v1/accounts/${path}`, { credentials: "same-origin", cache: "no-store", ...options, signal: controller.signal });
      const payload = await response.json();
      return { response, payload };
    } finally { clearTimeout(timer); }
  }
  function enter() {
    password.value = "";
    // Reload this exact same-origin URL; no redirect parameter can choose another destination.
    window.location.reload();
  }
  async function checkSession() {
    if (pending) return;
    pending = true; submit.disabled = true; retry.hidden = true;
    message("로그인 상태를 확인하고 있습니다.");
    try {
      const { response, payload } = await request("me");
      if (response.ok && payload.enabled === true && payload.authenticated === true && payload.account?.id) { enter(); return; }
      if (response.ok && payload.enabled === false) {
        message("현재 로그인을 사용할 수 없습니다. 관리자에게 문의해 주세요.", true);
        retry.hidden = false; return;
      }
      if (!response.ok && response.status !== 401) throw new Error("Session unavailable");
      if (response.ok && !(payload.enabled === true && payload.authenticated === false)) throw new Error("Invalid session");
      submit.disabled = false; message("아이디와 비밀번호를 입력해 주세요.");
    } catch (_) {
      message("로그인 상태를 확인하지 못했습니다. 연결 상태를 확인한 뒤 다시 시도해 주세요.", true);
      retry.hidden = false;
    } finally { pending = false; }
  }
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (pending || submit.disabled || !form.reportValidity()) return;
    pending = true; submit.disabled = true; retry.hidden = true; message("로그인하고 있습니다.");
    try {
      const { response, payload } = await request("login", {
        method: "POST", headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ username: username.value.trim(), password: password.value }),
      });
      if (response.ok && payload.enabled === true && payload.authenticated === true && payload.account?.id) { enter(); return; }
      if (response.status === 401) { message("아이디 또는 비밀번호를 확인해 주세요.", true); submit.disabled = false; }
      else if (response.status === 429) { message("로그인 시도가 많습니다. 잠시 후 상태를 다시 확인해 주세요.", true); retry.hidden = false; }
      else throw new Error("Login unconfirmed");
    } catch (_) {
      message("로그인 결과를 확인하지 못했습니다. 상태를 다시 확인해 주세요.", true); retry.hidden = false;
    } finally { password.value = ""; pending = false; }
  });
  retry.addEventListener("click", checkSession);
  window.addEventListener("pageshow", event => { if (event.persisted) void checkSession(); });
  void checkSession();
})();
