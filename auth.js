const AUTH_STORAGE_KEY = 'workshop.auth.session.v1';
const authState = {
  config: null,
  session: null,
  user: null,
  entered: false,
  refreshPromise: null,
};

const ROLE_VIEWS = {
  owner: ['dashboard','leads','projects','workorders','labor','bom','contracts','profit','invoices','prices','settings'],
  project_manager: ['dashboard','projects','workorders','labor','bom','contracts','prices'],
  finance: ['dashboard','projects','labor','bom','contracts','profit','invoices','prices'],
  business: ['dashboard','leads','projects'],
};
const ROLE_WRITES = {
  owner: ['dashboard','leads','projects','workorders','labor','bom','contracts','profit','invoices','prices','settings'],
  project_manager: ['projects','workorders','labor','bom'],
  finance: ['contracts','profit','invoices'],
  business: ['leads'],
};

window.canAccessView = view => (ROLE_VIEWS[authState.user?.role] || []).includes(view);
window.canWriteView = view => (ROLE_WRITES[authState.user?.role] || []).includes(view);
window.getCurrentUser = () => authState.user;

function ensureAuthScreen() {
  if (document.querySelector('#auth-screen')) return;
  const screen = document.createElement('div');
  screen.id = 'auth-screen';
  screen.innerHTML = `
    <div class="auth-card">
      <span class="auth-mark">工</span>
      <h1>登录工装管家</h1>
      <p id="auth-status">正在检查登录状态…</p>
      <form id="auth-form" hidden>
        <label>邮箱<input name="email" type="email" autocomplete="username" required></label>
        <label>密码<input name="password" type="password" autocomplete="current-password" required></label>
        <div id="auth-error" role="alert"></div>
        <button type="submit">登录</button>
      </form>
      <button id="auth-retry" type="button" hidden>重新连接</button>
    </div>`;
  document.body.appendChild(screen);
  screen.querySelector('#auth-retry').onclick = () => window.location.reload();
}

function setAuthVisible(visible) {
  const screen = document.querySelector('#auth-screen');
  if (screen) {
    screen.hidden = !visible;
    screen.style.display = visible ? 'grid' : 'none';
  }
  document.querySelector('.app-shell')?.classList.toggle('auth-locked', visible);
}

function setAuthMode(mode, message = '') {
  const form = document.querySelector('#auth-form');
  const status = document.querySelector('#auth-status');
  const retry = document.querySelector('#auth-retry');
  setAuthVisible(true);
  form.hidden = mode !== 'login';
  retry.hidden = mode !== 'error';
  status.textContent = message || (mode === 'loading' ? '正在检查登录状态…' : mode === 'login' ? '使用管理员为你创建的账号登录' : '登录服务连接失败');
  if (mode === 'login') window.requestAnimationFrame(() => form.elements.email.focus());
}

function withTimeout(promise, timeoutMs, message) {
  return Promise.race([
    promise,
    new Promise((_, reject) => window.setTimeout(() => reject(new Error(message)), timeoutMs)),
  ]);
}

function readStoredSession() {
  try { return JSON.parse(window.localStorage.getItem(AUTH_STORAGE_KEY) || 'null'); }
  catch { return null; }
}

function saveSession(session) {
  const normalized = {
    ...session,
    expires_at: Number(session.expires_at || Math.floor(Date.now() / 1000) + Number(session.expires_in || 3600)),
  };
  authState.session = normalized;
  window.localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(normalized));
  return normalized;
}

function clearSession() {
  authState.session = null;
  authState.user = null;
  authState.entered = false;
  window.currentUser = null;
  window.localStorage.removeItem(AUTH_STORAGE_KEY);
}

async function authRequest(path, { body, token, timeoutMs = 8000 } = {}) {
  const key = authState.config.anonKey;
  const response = await withTimeout(fetch(`${authState.config.url.replace(/\/$/, '')}${path}`, {
    method: 'POST',
    headers: {
      apikey: key,
      Authorization: `Bearer ${token || key}`,
      'Content-Type': 'application/json',
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  }), timeoutMs, '认证服务响应超时');
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error_description || result.msg || result.message || result.error || `认证失败（${response.status}）`);
  return result;
}

async function refreshSession() {
  if (!authState.session?.refresh_token) throw new Error('登录已过期');
  if (authState.refreshPromise) return authState.refreshPromise;
  authState.refreshPromise = authRequest('/auth/v1/token?grant_type=refresh_token', {
    body: { refresh_token: authState.session.refresh_token },
  }).then(saveSession).finally(() => { authState.refreshPromise = null; });
  return authState.refreshPromise;
}

async function ensureFreshSession() {
  if (!authState.session) return null;
  if (Number(authState.session.expires_at || 0) > Math.floor(Date.now() / 1000) + 60) return authState.session;
  return refreshSession();
}

async function loadCurrentUser(canRefresh = true) {
  if (!authState.session?.access_token) throw new Error('请重新登录');
  let response = await withTimeout(fetch(`${window.API_ORIGIN}/api/me`, {
    headers: { Authorization: `Bearer ${authState.session.access_token}` },
  }), 8000, '账号信息加载超时');
  if (response.status === 401 && canRefresh && authState.session.refresh_token) {
    await refreshSession();
    return loadCurrentUser(false);
  }
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || '账号尚未启用');
  authState.user = result;
  window.currentUser = result;
  return result;
}

function bindLoginForm() {
  const form = document.querySelector('#auth-form');
  form.onsubmit = async event => {
    event.preventDefault();
    const errorBox = document.querySelector('#auth-error');
    const submit = form.querySelector('button[type="submit"]');
    const originalLabel = submit.textContent;
    const values = Object.fromEntries(new FormData(form));
    let navigating = false;
    errorBox.textContent = '';
    submit.disabled = true;
    submit.textContent = '登录中…';
    try {
      const session = await authRequest('/auth/v1/token?grant_type=password', {
        body: { email: values.email, password: values.password },
      });
      saveSession(session);
      navigating = true;
      submit.textContent = '登录成功，正在进入…';
      window.location.replace('/?authfix=20260715-v9&signedin=1');
    } catch (error) {
      clearSession();
      errorBox.textContent = error.message === 'Invalid login credentials' ? '邮箱或密码错误' : error.message;
    } finally {
      if (!navigating) {
        submit.disabled = false;
        submit.textContent = originalLabel;
      }
    }
  };
}

function bindLogout() {
  const profile = document.querySelector('.profile');
  if (!profile) return;
  profile.title = '点击退出登录';
  profile.tabIndex = 0;
  profile.onclick = () => {
    if (!authState.session || !window.confirm('确定退出登录？')) return;
    const token = authState.session.access_token;
    clearSession();
    setAuthMode('login');
    void authRequest('/auth/v1/logout', { token }).catch(() => {});
  };
}

async function initializeAuth() {
  ensureAuthScreen();
  setAuthMode('loading');
  const response = await withTimeout(fetch(`${window.API_ORIGIN}/api/auth-config`), 5000, '登录服务连接超时');
  if (!response.ok) throw new Error('登录服务配置加载失败');
  authState.config = await response.json();
  bindLoginForm();
  bindLogout();
  authState.session = readStoredSession();
  if (!authState.session) {
    setAuthMode('login');
    return null;
  }
  try {
    await ensureFreshSession();
    await loadCurrentUser();
    document.querySelector('#auth-screen')?.remove();
    document.querySelector('.app-shell')?.classList.remove('auth-locked');
    return authState.user;
  } catch (error) {
    clearSession();
    document.querySelector('#auth-error').textContent = '登录已过期，请重新登录';
    setAuthMode('login');
    return null;
  }
}

window.getAuthAccessToken = async function getAuthAccessToken() {
  await window.authReady;
  if (!authState.session) return null;
  try { await ensureFreshSession(); }
  catch { clearSession(); setAuthMode('login'); return null; }
  return authState.session?.access_token || null;
};

ensureAuthScreen();
window.authReady = initializeAuth().catch(error => {
  document.querySelector('#auth-error').textContent = error.message;
  setAuthMode('error', error.message);
  return null;
});
