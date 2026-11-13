/* RestDesk UI - vanilla JS, no build step. */
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const uid = () => (crypto.randomUUID ? crypto.randomUUID()
  : Math.random().toString(36).slice(2) + Date.now().toString(36));
const clone = (o) => JSON.parse(JSON.stringify(o));
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const HISTORY_PAGE = 100;

let ws = null;            // the whole workspace (persisted to disk by the server)
let currentId = null;     // id of the request being edited
let lastResult = null;    // last execution result for the response pane
let prettyMode = true;
let hist = { items: [], total: 0, q: '' };  // call history (stored server-side in SQLite)

/* ------------------------------------------------------------------ */
/* Server API                                                          */
/* ------------------------------------------------------------------ */
async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', 'X-RestDesk': '1', ...(opts.headers || {}) },
  });
  if (!r.ok) {
    const err = new Error(`${r.status} ${await r.text()}`);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

/* ------------------------------------------------------------------ */
/* Saving                                                              */
/*                                                                     */
/* Every save carries the revision this tab last loaded or saved. If    */
/* another tab saved in between, the server answers 409 instead of      */
/* overwriting that tab's work. Tabs also tell each other about saves   */
/* over a BroadcastChannel, so idle tabs quietly reload and conflicts   */
/* only happen when two tabs edit at the same moment.                   */
/* ------------------------------------------------------------------ */
let wsRevision = 0;
let saveTimer = null;      // debounce timer for a pending save
let saveInFlight = null;   // promise of the PUT currently running
const KEEPALIVE_LIMIT = 60 * 1024; // browsers cap keepalive request bodies at 64 KB
const channel = 'BroadcastChannel' in window ? new BroadcastChannel('restdesk') : null;
const hasPendingSave = () => !!saveTimer || !!saveInFlight;
const setSaveState = (text) => { $('#saveState').textContent = text; };

async function putWorkspace(force = false) {
  const body = JSON.stringify({ ...ws, baseRevision: force ? null : wsRevision });
  const r = await api('/api/workspace', { method: 'PUT', body });
  wsRevision = r.revision;
  channel?.postMessage({ type: 'saved', revision: wsRevision });
  return r;
}

async function doSave() {
  if (saveInFlight) { await saveInFlight.catch(() => {}); }
  saveInFlight = (async () => {
    try {
      await putWorkspace();
      setSaveState('saved');
    } catch (e) {
      if (e.status === 409) await resolveConflict();
      else {
        setSaveState('save failed');
        toast('Save failed. Is the RestDesk server running?');
        console.error(e);
      }
    }
  })();
  try { await saveInFlight; } finally { saveInFlight = null; }
}

async function resolveConflict() {
  setSaveState('conflict');
  const takeTheirs = confirm('This workspace was changed in another RestDesk tab or window.\n\n' +
    'OK: load the latest version (the edit you just made in this tab is discarded)\n' +
    'Cancel: keep this tab\'s version and overwrite the other changes');
  if (takeTheirs) { await reloadWorkspace(); toast('Loaded the latest version'); return; }
  await putWorkspace(true);
  setSaveState('saved');
}

/* Write pending changes now (before export, when the tab is hidden). */
async function flushSave() {
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; await doSave(); }
  else if (saveInFlight) await saveInFlight.catch(() => {});
}

function save() {
  clearTimeout(saveTimer);
  setSaveState('saving…');
  saveTimer = setTimeout(() => { saveTimer = null; doSave(); }, 350);
}

async function reloadWorkspace() {
  const keep = currentId;
  const data = await api('/api/workspace');
  wsRevision = data.revision || 0;
  delete data.revision;
  ws = normalizeWorkspace(data);
  renderEnvSelect();
  if (findRequest(keep)) { currentId = keep; renderTree(); renderEditor(); } else renderAll();
  setSaveState('saved');
}

channel?.addEventListener('message', ({ data }) => {
  if (data?.type === 'saved' && data.revision > wsRevision && !hasPendingSave()) {
    reloadWorkspace().catch(console.error);
  }
  if (data?.type === 'history') loadHistory();
});

/* toast('Saved') or toast('Deleted', { label: 'Undo', action: fn }) */
function toast(msg, opt = {}) {
  let t = $('#toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; t.setAttribute('role', 'status'); document.body.appendChild(t); }
  t.textContent = msg;
  if (opt.action) {
    const b = document.createElement('button');
    b.className = 'toast-action';
    b.textContent = opt.label || 'Undo';
    b.onclick = () => { t.classList.remove('show'); opt.action(); };
    t.appendChild(b);
  }
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('show'), opt.action ? 8000 : 1600);
}

/* ------------------------------------------------------------------ */
/* Model                                                               */
/* ------------------------------------------------------------------ */
function newRequest(name = 'New Request', patch = {}) {
  return {
    id: uid(), name, method: 'GET', url: '',
    params: [], headers: [],
    body: { mode: 'none', text: '', form: [] },
    auth: { type: 'none' },
    scripts: { pre: '', post: '' },
    settings: { timeoutMs: 30000, followRedirects: true, verifyTls: true },
    ...patch,
  };
}
const kv = (key, value, enabled = true) => ({ key, value, enabled });

function defaultWorkspace() {
  const env = { id: uid(), name: 'Default', vars: [kv('baseUrl', 'https://httpbin.org'), kv('todoId', '1')] };
  return {
    version: 1,
    activeEnvId: env.id,
    environments: [env],
    collections: [{
      id: uid(), name: 'Examples', collapsed: false, requests: [
        newRequest('Get a todo', {
          url: 'https://jsonplaceholder.typicode.com/todos/{{todoId}}',
          scripts: { pre: '', post: "test('status is 200', () => expect(res.status).toBe(200));\ntest('has title', () => expect(res.json()).toHaveProperty('title'));" },
        }),
        newRequest('Echo JSON (POST)', {
          method: 'POST', url: '{{baseUrl}}/post',
          headers: [kv('X-Request-Id', '{{$uuid}}')],
          body: { mode: 'json', text: '{\n  "message": "hello",\n  "sentAt": "{{$isoTimestamp}}"\n}', form: [] },
          scripts: { pre: '', post: "const j = res.json();\ntest('echoed body', () => expect(j.json.message).toBe('hello'));\nenv.set('lastEchoId', j.headers['X-Request-Id']);" },
        }),
        newRequest('Bearer auth check', {
          url: '{{baseUrl}}/bearer', auth: { type: 'bearer', token: 'my-secret-token' },
        }),
      ],
    }],
  };
}

function normalizeWorkspace(w) {
  if (!w || !Array.isArray(w.collections)) return defaultWorkspace();
  w.environments ||= [];
  delete w.history; // pre-0.2 workspaces kept a short history inline
  for (const c of w.collections) {
    c.requests ||= [];
    c.requests = c.requests.map((r) => ({ ...newRequest(), ...r,
      body: { mode: 'none', text: '', form: [], ...(r.body || {}) },
      settings: { timeoutMs: 30000, followRedirects: true, verifyTls: true, ...(r.settings || {}) },
      scripts: { pre: '', post: '', ...(r.scripts || {}) } }));
  }
  return w;
}

function findRequest(id) {
  for (const c of ws.collections) {
    const r = c.requests.find((x) => x.id === id);
    if (r) return { col: c, req: r };
  }
  return null;
}
const current = () => findRequest(currentId)?.req || null;
const currentCol = () => findRequest(currentId)?.col || ws.collections[0] || null;
const activeEnv = () => ws.environments.find((e) => e.id === ws.activeEnvId) || null;

/* ------------------------------------------------------------------ */
/* Variables                                                           */
/* ------------------------------------------------------------------ */
function envObject() {
  const o = {};
  for (const v of activeEnv()?.vars || []) if (v.enabled !== false && v.key) o[v.key] = v.value ?? '';
  return o;
}

function resolveVars(str, vars, depth = 0) {
  if (str == null) return '';
  return String(str).replace(/\{\{\s*([^{}]+?)\s*\}\}/g, (m, name) => {
    switch (name) {
      case '$uuid': return uid();
      case '$timestamp': return String(Math.floor(Date.now() / 1000));
      case '$isoTimestamp': return new Date().toISOString();
      case '$randomInt': return String(Math.floor(Math.random() * 1000));
    }
    if (Object.prototype.hasOwnProperty.call(vars, name)) {
      const v = String(vars[name]);
      return depth < 5 && v.includes('{{') ? resolveVars(v, vars, depth + 1) : v;
    }
    return m; // leave unknown variables visible so mistakes are obvious
  });
}

/* ------------------------------------------------------------------ */
/* Build the concrete HTTP request                                     */
/* ------------------------------------------------------------------ */
const hasHeader = (list, name) => list.some(([k]) => k.toLowerCase() === name.toLowerCase());
const b64 = (s) => btoa(unescape(encodeURIComponent(s)));

function buildFinal(req, vars) {
  const R = (s) => resolveVars(s, vars);
  let url = R((req.url || '').trim());
  if (url && !/^https?:\/\//i.test(url)) url = 'http://' + url;

  const query = (req.params || []).filter((p) => p.enabled !== false && p.key)
    .map((p) => [R(p.key), R(p.value)]);
  const headers = (req.headers || []).filter((h) => h.enabled !== false && h.key)
    .map((h) => [R(h.key), R(h.value)]);

  const a = req.auth || {};
  if (a.type === 'bearer' && a.token) headers.push(['Authorization', `${R(a.prefix || 'Bearer')} ${R(a.token)}`]);
  if (a.type === 'basic') headers.push(['Authorization', 'Basic ' + b64(`${R(a.username)}:${R(a.password)}`)]);
  if (a.type === 'apikey' && a.key) {
    if (a.in === 'query') query.push([R(a.key), R(a.value)]);
    else headers.push([R(a.key), R(a.value)]);
  }

  if (query.length) {
    const qs = query.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join('&');
    const [base, hash] = url.split('#');
    url = base + (base.includes('?') ? '&' : '?') + qs + (hash !== undefined ? '#' + hash : '');
  }

  let body = null;
  const b = req.body || {};
  const defaultType = { json: 'application/json', text: 'text/plain; charset=utf-8', xml: 'application/xml' }[b.mode];
  if (defaultType) {
    body = R(b.text || '');
    if (!hasHeader(headers, 'Content-Type')) headers.push(['Content-Type', defaultType]);
  } else if (b.mode === 'urlencoded') {
    const sp = new URLSearchParams();
    for (const f of b.form || []) if (f.enabled !== false && f.key) sp.append(R(f.key), R(f.value));
    body = sp.toString();
    if (!hasHeader(headers, 'Content-Type')) headers.push(['Content-Type', 'application/x-www-form-urlencoded']);
  } else if (b.mode === 'multipart') {
    const boundary = '----RestDesk' + Math.random().toString(16).slice(2);
    const parts = (b.form || []).filter((f) => f.enabled !== false && f.key).map((f) =>
      `--${boundary}\r\nContent-Disposition: form-data; name="${R(f.key).replace(/"/g, '%22')}"\r\n\r\n${R(f.value)}\r\n`);
    body = parts.join('') + `--${boundary}--\r\n`;
    // multipart needs our boundary, so it overrides any manual Content-Type
    for (let i = headers.length - 1; i >= 0; i--) if (headers[i][0].toLowerCase() === 'content-type') headers.splice(i, 1);
    headers.push(['Content-Type', `multipart/form-data; boundary=${boundary}`]);
  }

  return { method: (req.method || 'GET').toUpperCase(), url, headers, body, settings: { ...req.settings } };
}

/* ------------------------------------------------------------------ */
/* Scripts: pre-request + tests                                        */
/* ------------------------------------------------------------------ */
const SCRIPT_TIMEOUT_MS = 10000;

/* Run a user script in the sandboxed worker (see sandbox-worker.js). Never throws:
   returns {ok, error, vars, changes, req, tests, logs}. */
function runScript(code, { vars, req = null, res = null, timeoutMs = SCRIPT_TIMEOUT_MS }) {
  if (!code || !code.trim()) return Promise.resolve(null);
  return new Promise((resolve) => {
    let worker;
    try { worker = new Worker('sandbox-worker.js'); } catch (e) {
      resolve({ ok: false, error: `cannot start script sandbox: ${e.message}`, vars, changes: {}, req, tests: [], logs: [] });
      return;
    }
    const done = (out) => { clearTimeout(timer); worker.terminate(); resolve(out); };
    const timer = setTimeout(() => done({ ok: false, error: `script timed out after ${timeoutMs / 1000}s`, vars, changes: {}, req, tests: [], logs: [] }), timeoutMs);
    worker.onmessage = (e) => done(e.data);
    worker.onerror = (e) => { e.preventDefault(); done({ ok: false, error: e.message || 'script sandbox failed', vars, changes: {}, req, tests: [], logs: [] }); };
    worker.postMessage({ code, vars: { ...vars }, req, res });
  });
}

function applyEnvChanges(changes) {
  const env = activeEnv();
  if (!env || !Object.keys(changes).length) return;
  for (const [k, v] of Object.entries(changes)) {
    const i = env.vars.findIndex((x) => x.key === k);
    if (v == null) { if (i >= 0) env.vars.splice(i, 1); }
    else if (i >= 0) env.vars[i].value = v;
    else env.vars.push(kv(k, v));
  }
}

/* Execute one request end-to-end (no UI). Used by Send and by collection runs. */
async function execute(req) {
  let vars = envObject();
  const changes = {};
  const logs = [];
  const tests = [];
  const timeoutMs = Number(req.settings?.scriptTimeoutMs) || SCRIPT_TIMEOUT_MS;
  const result = { req, final: null, response: null, tests, logs, error: null };
  const absorb = (out) => {
    if (!out) return;
    Object.assign(changes, out.changes || {});
    logs.push(...(out.logs || []));
    if (out.vars) vars = out.vars;
  };

  let draft = clone(req);
  const pre = await runScript(req.scripts?.pre, { vars, req: draft, timeoutMs });
  absorb(pre);
  if (pre && !pre.ok) {
    result.error = `Pre-request script error: ${pre.error}`;
    applyEnvChanges(changes);
    return result;
  }
  if (pre?.req) draft = pre.req;

  const final = buildFinal(draft, vars);
  result.final = final;
  if (!final.url) { result.error = 'URL is empty'; applyEnvChanges(changes); return result; }

  try {
    result.response = await api('/api/send', {
      method: 'POST',
      body: JSON.stringify({ method: final.method, url: final.url, headers: final.headers, body: final.body, ...final.settings }),
    });
  } catch (e) {
    result.error = `RestDesk server unreachable: ${e.message}`;
    applyEnvChanges(changes);
    return result;
  }
  if (!result.response.ok) { result.error = result.response.error; applyEnvChanges(changes); return result; }

  const post = await runScript(req.scripts?.post, { vars, req: draft, res: result.response, timeoutMs });
  absorb(post);
  if (post) {
    tests.push(...(post.tests || []));
    if (!post.ok) tests.push({ name: 'script', pass: false, msg: `Script error: ${post.error}` });
  }
  applyEnvChanges(changes);
  return result;
}

/* ------------------------------------------------------------------ */
/* Sending from the UI                                                 */
/* ------------------------------------------------------------------ */
async function send() {
  const req = current();
  if (!req) return;
  const btn = $('#btnSend');
  btn.disabled = true; btn.textContent = 'Sending…';
  $('#statusLine').innerHTML = '<span class="muted">Waiting for response…</span>';
  try {
    const result = await execute(req);
    lastResult = result;
    const r = result.response;
    save();
    renderResponse();
    recordHistory(req, result, 'send');
    if ($('#envDialog').open) renderEnvDialog();
  } finally {
    btn.disabled = false; btn.textContent = 'Send';
  }
}

/* ------------------------------------------------------------------ */
/* Response rendering                                                  */
/* ------------------------------------------------------------------ */
function fmtSize(n) {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

function highlightJson(text) {
  return esc(text).replace(
    /(&quot;(?:\\u[a-fA-F0-9]{4}|\\[^u]|(?!&quot;)[^\\])*?&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (m, str, colon, lit, num) => {
      if (str) return colon ? `<span class="j-key">${str}</span>${colon}` : `<span class="j-str">${str}</span>`;
      if (lit) return `<span class="j-lit">${lit}</span>`;
      return `<span class="j-num">${num}</span>`;
    });
}

function jsonPath(obj, path) {
  path = path.trim();
  if (!path || path === '$') return obj;
  if (!path.startsWith('$')) path = '$.' + path;
  const re = /\.([^.[\]]+)|\[(\d+|\*|"[^"]*"|'[^']*')\]/g;
  let nodes = [obj];
  let m;
  let consumed = 1;
  re.lastIndex = 1;
  while ((m = re.exec(path))) {
    if (m.index !== consumed) throw new Error('bad path');
    consumed = re.lastIndex;
    const key = m[1] ?? m[2].replace(/^["']|["']$/g, '');
    nodes = nodes.flatMap((n) => {
      if (n == null) return [];
      if (key === '*') return Array.isArray(n) ? n : Object.values(n);
      return key in Object(n) ? [n[key]] : [];
    });
  }
  if (consumed !== path.length) throw new Error('bad path');
  return path.includes('*') ? nodes : nodes[0];
}

function renderResponse() {
  const r = lastResult;
  const sl = $('#statusLine');
  const body = $('#resBody');
  const ht = $('#resHeaders');
  const tests = $('#resTests');
  $('#hdrCount').textContent = '';
  $('#testCount').textContent = '';
  $('#testCount').className = 'badge';
  if (!r) return;

  if (r.error && !r.response?.ok) {
    sl.innerHTML = `<span class="pill serr">Error</span>${r.response?.timeMs != null ? `<span>${r.response.timeMs} ms</span>` : ''}`;
    body.innerHTML = `<div class="err-box">${esc(r.error)}</div>`;
    ht.innerHTML = '';
    renderTests(r, tests);
    return;
  }
  const res = r.response;
  const cls = `s${String(res.status)[0]}`;
  sl.innerHTML = `<span class="pill ${cls}">${res.status} ${esc(res.reason || '')}</span>` +
    `<span>${res.timeMs} ms</span><span>${fmtSize(res.size)}${res.wireSize !== res.size ? ` (${fmtSize(res.wireSize)} on wire)` : ''}</span>` +
    (res.truncated ? '<span class="err-box">truncated at 50 MB</span>' : '') +
    (res.url && res.url !== r.final.url ? `<span class="muted" title="${esc(res.url)}">→ redirected</span>` : '');

  ht.innerHTML = res.headers.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join('');
  $('#hdrCount').textContent = res.headers.length;
  renderBody();
  renderTests(r, tests);
}

function renderBody() {
  const r = lastResult;
  const el = $('#resBody');
  if (!r?.response?.ok) return;
  const res = r.response;
  const ctype = (res.headers.find(([k]) => k.toLowerCase() === 'content-type') || [, ''])[1].toLowerCase();

  if (res.bodyNotStored) {
    el.innerHTML = `<span class="muted">This ${fmtSize(res.size)} binary body was not kept in history (over the 2 MB storage limit).</span>`;
    return;
  }
  if (res.bodyEncoding === 'base64') {
    if (ctype.startsWith('image/')) el.innerHTML = `<img alt="response image" src="data:${esc(ctype.split(';')[0])};base64,${res.body}">`;
    else el.innerHTML = `<span class="muted">Binary response (${fmtSize(res.size)}, ${esc(ctype || 'unknown type')}) — not shown.</span>`;
    return;
  }
  if (!res.body) { el.innerHTML = '<span class="muted">(empty body)</span>'; return; }

  let parsed;
  let isJson = false;
  try { parsed = JSON.parse(res.body); isJson = true; } catch { /* not json */ }

  const filter = $('#jsonFilter').value.trim();
  $('#jsonFilter').style.borderColor = '';
  if (isJson && filter) {
    try { parsed = jsonPath(parsed, filter); } catch { $('#jsonFilter').style.borderColor = 'var(--err)'; }
  }
  if (!prettyMode) { el.textContent = isJson && filter ? JSON.stringify(parsed) : res.body; return; }
  if (isJson) {
    const text = JSON.stringify(parsed, null, 2) ?? 'undefined';
    if (text.length > 2_000_000) el.textContent = text;
    else el.innerHTML = highlightJson(text);
  } else {
    el.textContent = res.body;
  }
}

function renderTests(r, el) {
  const pass = r.tests.filter((t) => t.pass).length;
  const fail = r.tests.length - pass;
  if (r.tests.length) {
    $('#testCount').textContent = `${pass}/${r.tests.length}`;
    $('#testCount').className = `badge ${fail ? 'fail' : 'pass'}`;
  }
  let html = r.tests.length ? '' : '<p class="muted">No tests. Add some in the request\'s Scripts tab.</p>';
  for (const t of r.tests) {
    html += `<div class="test"><span class="${t.pass ? 'ok' : 'bad'}">${t.pass ? '✓' : '✗'}</span> ${esc(t.name)}` +
      (t.msg ? `<span class="why">${esc(t.msg)}</span>` : '') + '</div>';
  }
  if (r.logs.length) html += `<div class="logs">${esc(r.logs.join('\n'))}</div>`;
  el.innerHTML = html;
}

/* ------------------------------------------------------------------ */
/* Key/value table editor                                              */
/* ------------------------------------------------------------------ */
function renderKV(container, list, { keyPh = 'name', valPh = 'value', datalist = '' } = {}) {
  container.innerHTML = '';
  const table = document.createElement('table');
  table.className = 'kv';
  list.forEach((item, i) => {
    const tr = document.createElement('tr');
    if (item.enabled === false) tr.className = 'off';
    tr.innerHTML = `<td class="chk"><input type="checkbox" ${item.enabled !== false ? 'checked' : ''}></td>` +
      `<td><input type="text" class="k" placeholder="${keyPh}" ${datalist ? `list="${datalist}"` : ''} spellcheck="false"></td>` +
      `<td><input type="text" class="v" placeholder="${valPh}" spellcheck="false"></td>` +
      '<td class="del"><button class="icon" title="Remove">✕</button></td>';
    $('.k', tr).value = item.key || '';
    $('.v', tr).value = item.value || '';
    $('input[type=checkbox]', tr).onchange = (e) => { item.enabled = e.target.checked; tr.className = item.enabled ? '' : 'off'; changed(); };
    $('.k', tr).oninput = (e) => { item.key = e.target.value; changed(); };
    $('.v', tr).oninput = (e) => { item.value = e.target.value; changed(); };
    $('button', tr).onclick = () => { list.splice(i, 1); renderKV(container, list, { keyPh, valPh, datalist }); changed(); };
    table.appendChild(tr);
  });
  container.appendChild(table);
  const add = document.createElement('button');
  add.className = 'small kv-add';
  add.textContent = '＋ Add';
  add.onclick = () => {
    list.push(kv('', ''));
    renderKV(container, list, { keyPh, valPh, datalist });
    $$('.k', container).at(-1)?.focus();
    changed();
  };
  container.appendChild(add);
}

/* Called after any edit of the current request. */
function changed() {
  save();
  updateResolvedPreview();
}

/* ------------------------------------------------------------------ */
/* Sidebar                                                             */
/* ------------------------------------------------------------------ */
function renderTree() {
  const q = $('#filter').value.trim().toLowerCase();
  const tree = $('#tree');
  tree.innerHTML = '';
  for (const col of ws.collections) {
    const reqs = col.requests.filter((r) => !q || r.name.toLowerCase().includes(q) || r.url.toLowerCase().includes(q));
    if (q && !reqs.length) continue;
    const open = q ? true : !col.collapsed;
    const head = document.createElement('div');
    head.className = 'col-row';
    head.innerHTML = `<span class="caret">${open ? '▼' : '▶'}</span><span class="row-name"></span>` +
      '<span class="row-actions"><button data-a="add" title="New request">＋</button><button data-a="ren" title="Rename">✎</button><button data-a="del" title="Delete">🗑</button></span>';
    $('.row-name', head).textContent = col.name;
    head.onclick = (e) => {
      const a = e.target.dataset.a;
      if (a === 'add') { addRequest(col); return; }
      if (a === 'ren') { const n = prompt('Collection name', col.name); if (n) { col.name = n; save(); renderTree(); } return; }
      if (a === 'del') {
        const at = ws.collections.indexOf(col);
        ws.collections.splice(at, 1);
        if (!findRequest(currentId)) selectRequest(ws.collections[0]?.requests[0]?.id || null);
        save(); renderTree();
        toast(`Deleted collection "${col.name}"`, { label: 'Undo', action: () => {
          ws.collections.splice(Math.min(at, ws.collections.length), 0, col);
          save(); renderTree();
        } });
        return;
      }
      col.collapsed = !col.collapsed; save(); renderTree();
    };
    tree.appendChild(head);
    if (!open) continue;
    for (const r of reqs) {
      const row = document.createElement('div');
      row.className = 'req-row' + (r.id === currentId ? ' active' : '');
      row.innerHTML = `<span class="m m-${esc(r.method)}">${esc(r.method)}</span><span class="row-name"></span>` +
        '<span class="row-actions"><button data-a="dup" title="Duplicate">⧉</button><button data-a="del" title="Delete">🗑</button></span>';
      $('.row-name', row).textContent = r.name;
      row.title = r.url;
      row.onclick = (e) => {
        const a = e.target.dataset.a;
        if (a === 'dup') {
          const copy = { ...clone(r), id: uid(), name: r.name + ' (copy)' };
          col.requests.splice(col.requests.indexOf(r) + 1, 0, copy);
          selectRequest(copy.id); save(); return;
        }
        if (a === 'del') {
          const at = col.requests.indexOf(r);
          col.requests.splice(at, 1);
          if (r.id === currentId) selectRequest(col.requests[0]?.id || ws.collections.find((c) => c.requests.length)?.requests[0]?.id || null);
          save(); renderTree();
          toast(`Deleted "${r.name}"`, { label: 'Undo', action: () => {
            const home = ws.collections.includes(col) ? col : (ws.collections[0] || null);
            if (!home) { toast('Restore it from Trash instead'); return; }
            home.requests.splice(Math.min(at, home.requests.length), 0, r);
            selectRequest(r.id); save();
          } });
          return;
        }
        selectRequest(r.id);
      };
      tree.appendChild(row);
    }
  }
}

/* Call history: every send is stored on the server with its response. */
function historyRecord(req, result, source) {
  const { final, response, tests, logs, error } = result;
  return { id: uid(), ts: Date.now(), source, snapshot: clone(req),
    result: clone({ final, response, tests, logs, error }) };
}

async function recordHistory(req, result, source) {
  try {
    const row = await api('/api/history', { method: 'POST', body: JSON.stringify(historyRecord(req, result, source)) });
    if (!hist.q) { hist.items.unshift(row); hist.total++; renderHistory(); }
    else loadHistory();
    channel?.postMessage({ type: 'history' });
  } catch (e) { console.error(e); toast('Could not save to history'); }
}

async function loadHistory(append = false) {
  const offset = append ? hist.items.length : 0;
  const q = encodeURIComponent(hist.q);
  try {
    const page = await api(`/api/history?limit=${HISTORY_PAGE}&offset=${offset}&q=${q}`);
    hist.items = append ? hist.items.concat(page.items) : page.items;
    hist.total = page.total;
  } catch (e) { console.error(e); }
  renderHistory();
}

function timeAgo(ts) {
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return new Date(ts).toLocaleDateString();
}

async function openHistory(id) {
  let entry;
  try { entry = await api(`/api/history/${encodeURIComponent(id)}`); } catch (e) { toast(`History entry unavailable: ${e.message}`); return; }
  // Reuse the saved request only if it is unchanged since this call; otherwise the editor
  // would show a different request than the one that produced the stored response.
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const fields = ['method', 'url', 'params', 'headers', 'body', 'auth', 'scripts'];
  let target = findRequest(entry.snapshot?.id)?.req;
  if (target && !fields.every((f) => same(target[f], entry.snapshot[f]))) target = null;
  if (!target) {
    // Deleted or edited since: restore the snapshot into a "History" collection.
    let col = ws.collections.find((c) => c.name === 'History');
    if (!col) { col = { id: uid(), name: 'History', collapsed: false, requests: [] }; ws.collections.push(col); }
    target = { ...newRequest(), ...clone(entry.snapshot), id: uid() };
    col.requests.unshift(target);
    save();
  }
  selectRequest(target.id);
  lastResult = { req: target, tests: [], logs: [], ...entry.result };
  lastResult.final ||= { url: entry.url, method: entry.method };
  renderResponse();
  const notes = [];
  if (entry.result?.redacted) notes.push('secrets redacted');
  if (entry.result?.response?.storedTruncated) notes.push('body clipped to 2 MB');
  if (notes.length) $('#statusLine').insertAdjacentHTML('beforeend', `<span class="note" title="Stored copy, not the live response">${esc(notes.join(' · '))}</span>`);
  $('#statusLine').insertAdjacentHTML('beforeend',
    `<span class="muted" title="${esc(entry.method)} ${esc(entry.url)}">· from history: ${esc(entry.method)} ${esc(entry.url.replace(/^https?:\/\//, ''))} · ${esc(new Date(entry.ts).toLocaleString())}</span>`);
}

function renderHistory() {
  const el = $('#history');
  el.innerHTML = '';
  for (const h of hist.items) {
    const row = document.createElement('div');
    row.className = 'hist-row';
    const st = String(h.status);
    const color = st === 'ERR' ? 'var(--err)' : { 2: 'var(--ok)', 3: 'var(--info)', 4: 'var(--warn)', 5: 'var(--err)' }[st[0]] || 'var(--muted)';
    row.innerHTML = `<span class="m m-${esc(h.method)}">${esc(h.method)}</span><span class="row-name"></span>` +
      `<span class="ago">${esc(timeAgo(h.ts))}</span><span class="st" style="color:${color}">${esc(st)}</span>` +
      '<button class="icon del" title="Delete from history">×</button>';
    $('.row-name', row).textContent = h.url.replace(/^https?:\/\//, '');
    const tests = h.testsPass + h.testsFail ? `\nTests: ${h.testsPass}/${h.testsPass + h.testsFail} passed` : '';
    row.title = `${new Date(h.ts).toLocaleString()}${h.source === 'runner' ? ' (collection run)' : ''}\n${h.url}` +
      `${h.timeMs != null ? `\n${h.timeMs} ms` : ''}${tests}\nClick to view the stored response`;
    row.onclick = () => openHistory(h.id);
    $('.del', row).onclick = async (e) => {
      e.stopPropagation();
      try { await api(`/api/history/${encodeURIComponent(h.id)}`, { method: 'DELETE' }); } catch { return; }
      hist.items = hist.items.filter((x) => x.id !== h.id); hist.total--; renderHistory();
    };
    el.appendChild(row);
  }
  if (!hist.items.length) {
    el.innerHTML = `<div class="muted" style="padding:4px 8px;font-size:12px">${hist.q ? 'No matches' : 'Nothing sent yet'}</div>`;
  } else if (hist.items.length < hist.total) {
    const more = document.createElement('button');
    more.className = 'link more';
    more.textContent = `Load more (${hist.total - hist.items.length})`;
    more.onclick = () => loadHistory(true);
    el.appendChild(more);
  }
  $('#histCount').textContent = hist.total ? hist.total : '';
}

/* Trash: deleted collections/requests, kept server-side for 30 days. */
async function openTrash() {
  const dlg = $('#trashDialog');
  const table = $('#trashTable');
  table.innerHTML = '<tr><td class="muted">Loading…</td></tr>';
  if (!dlg.open) dlg.showModal();
  await flushSave();
  let items = [];
  try { ({ items } = await api('/api/trash')); } catch (e) { table.innerHTML = `<tr><td>${esc(e.message)}</td></tr>`; return; }
  if (!items.length) { table.innerHTML = '<tr><td class="muted">Trash is empty.</td></tr>'; return; }
  table.innerHTML = '<tr><th>Item</th><th>From</th><th>Deleted</th><th></th></tr>';
  for (const it of items) {
    const tr = document.createElement('tr');
    const what = it.kind === 'collection'
      ? `<b>${esc(it.name)}</b> <span class="muted">collection · ${it.requestCount} request(s)</span>`
      : `<span class="m m-${esc(it.method || 'GET')}">${esc(it.method || '')}</span> ${esc(it.name)}`;
    tr.innerHTML = `<td>${what}</td><td>${esc(it.collectionName || '')}</td><td>${esc(new Date(it.deletedAt).toLocaleString())}</td>` +
      '<td><button class="small">Restore</button></td>';
    $('button', tr).onclick = () => restoreFromTrash(it.id);
    table.appendChild(tr);
  }
}

async function restoreFromTrash(id) {
  let item;
  try { item = await api(`/api/trash/${encodeURIComponent(id)}/restore`, { method: 'POST', body: '{}' }); }
  catch (e) { toast(`Restore failed: ${e.message}`); return; }
  const taken = (rid) => !!findRequest(rid);
  if (item.kind === 'collection') {
    const col = item.data;
    if (ws.collections.some((c) => c.id === col.id)) col.id = uid();
    col.requests = (col.requests || []).map((r) => (taken(r.id) ? { ...r, id: uid() } : r));
    ws.collections.push(col);
    toast(`Restored collection "${col.name}"`);
  } else {
    const req = taken(item.data.id) ? { ...item.data, id: uid() } : item.data;
    let col = ws.collections.find((c) => c.id === item.collectionId)
      || ws.collections.find((c) => c.name === item.collectionName);
    if (!col) { col = { id: uid(), name: item.collectionName || 'Restored', collapsed: false, requests: [] }; ws.collections.push(col); }
    col.requests.push(req);
    col.collapsed = false;
    selectRequest(req.id);
    toast(`Restored "${req.name}" into ${col.name}`);
  }
  save(); renderTree();
  openTrash();
}

function addRequest(col, req = newRequest()) {
  col = col || ws.collections[0];
  if (!col) { col = { id: uid(), name: 'My Collection', collapsed: false, requests: [] }; ws.collections.push(col); }
  col.collapsed = false;
  col.requests.push(req);
  selectRequest(req.id);
  save();
  setTimeout(() => $('#url').focus(), 0);
}

/* ------------------------------------------------------------------ */
/* Request editor                                                      */
/* ------------------------------------------------------------------ */
function selectRequest(id) {
  currentId = id;
  ws.lastRequestId = id;
  lastResult = null;
  renderTree();
  renderEditor();
  renderResponse();
  $('#statusLine').innerHTML = '<span class="muted">Send a request to see the response · ⌘/Ctrl+Enter</span>';
  $('#resBody').innerHTML = '';
  $('#resHeaders').innerHTML = '';
  $('#resTests').innerHTML = '';
}

function renderEditor() {
  const req = current();
  const disabled = !req;
  $$('#main input, #main select, #main textarea, #main button').forEach((el) => {
    if (!['btnEnv', 'envSelect'].includes(el.id)) el.disabled = disabled;
  });
  if (!req) {
    $('#reqName').value = ''; $('#url').value = '';
    $('#paramsKV').innerHTML = '<p class="muted">Create a request with ＋ next to a collection.</p>';
    return;
  }
  $('#reqName').value = req.name;
  $('#method').value = req.method;
  $('#url').value = req.url;
  renderKV($('#paramsKV'), req.params, { keyPh: 'query param' });
  renderKV($('#headersKV'), req.headers, { keyPh: 'header', datalist: 'commonHeaders' });
  $('#bodyMode').value = req.body.mode;
  $('#bodyText').value = req.body.text || '';
  renderBodyMode();
  $('#authType').value = req.auth.type || 'none';
  renderAuth();
  $('#preScript').value = req.scripts.pre || '';
  $('#postScript').value = req.scripts.post || '';
  $('#setTimeout').value = req.settings.timeoutMs;
  $('#setScriptTimeout').value = req.settings.scriptTimeoutMs || '';
  $('#setFollow').checked = req.settings.followRedirects !== false;
  $('#setVerify').checked = req.settings.verifyTls !== false;
  updateResolvedPreview();
}

function renderBodyMode() {
  const req = current();
  const mode = req.body.mode;
  const isText = ['json', 'text', 'xml'].includes(mode);
  $('#bodyText').hidden = !isText;
  $('#btnBeautify').hidden = mode !== 'json';
  const formEl = $('#bodyFormKV');
  formEl.hidden = !['urlencoded', 'multipart'].includes(mode);
  if (!formEl.hidden) { req.body.form ||= []; renderKV(formEl, req.body.form, { keyPh: 'field' }); }
}

function renderAuth() {
  const req = current();
  const a = req.auth;
  const el = $('#authFields');
  const field = (label, key, type = 'text', ph = '') =>
    `<label>${label}</label><input data-k="${key}" type="${type}" placeholder="${ph}" spellcheck="false">`;
  el.innerHTML = {
    none: '<span class="muted">No authentication header will be added.</span>',
    bearer: field('Token', 'token', 'text', '{{token}}') + field('Prefix', 'prefix', 'text', 'Bearer'),
    basic: field('Username', 'username') + field('Password', 'password', 'password'),
    apikey: field('Key', 'key', 'text', 'X-Api-Key') + field('Value', 'value', 'text', '{{apiKey}}') +
      '<label>Add to</label><select data-k="in"><option value="header">Header</option><option value="query">Query params</option></select>',
  }[a.type || 'none'];
  $$('[data-k]', el).forEach((inp) => {
    inp.value = a[inp.dataset.k] ?? (inp.dataset.k === 'in' ? 'header' : '');
    inp.oninput = inp.onchange = () => { a[inp.dataset.k] = inp.value; changed(); };
  });
}

function updateResolvedPreview() {
  const req = current();
  const el = $('#resolved');
  if (!req) { el.textContent = ''; return; }
  const raw = req.url + req.params.map((p) => p.key + p.value).join('');
  if (!raw.includes('{{') && !req.params.some((p) => p.enabled !== false && p.key)) { el.textContent = ''; return; }
  const f = buildFinal(req, envObject());
  el.textContent = '→ ' + f.url;
  el.title = f.url;
}

/* Paste a URL with a query string → split it into the Params table. */
function absorbQuery() {
  const req = current();
  const qi = req.url.indexOf('?');
  if (qi < 0) return;
  const [rest, hash] = req.url.slice(qi + 1).split('#');
  req.url = req.url.slice(0, qi) + (hash !== undefined ? '#' + hash : '');
  for (const part of rest.split('&')) {
    if (!part) continue;
    const eq = part.indexOf('=');
    const dec = (s) => { try { return decodeURIComponent(s.replace(/\+/g, ' ')); } catch { return s; } };
    req.params.push(kv(dec(eq < 0 ? part : part.slice(0, eq)), dec(eq < 0 ? '' : part.slice(eq + 1))));
  }
  $('#url').value = req.url;
  renderKV($('#paramsKV'), req.params, { keyPh: 'query param' });
  changed();
}

/* ------------------------------------------------------------------ */
/* cURL import / export                                                */
/* ------------------------------------------------------------------ */
function shellSplit(s) {
  s = s.replace(/\\\r?\n/g, ' ').replace(/\^\r?\n/g, ' ');
  const out = [];
  let cur = '';
  let has = false;
  let q = null;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (q === "'") { if (c === "'") q = null; else cur += c; continue; }
    if (q === '$') {
      if (c === "'") { q = null; continue; }
      if (c === '\\' && i + 1 < s.length) {
        const n = s[++i];
        cur += { n: '\n', t: '\t', r: '\r', '\\': '\\', "'": "'", '"': '"' }[n] ?? '\\' + n;
        continue;
      }
      cur += c; continue;
    }
    if (q === '"') {
      if (c === '\\' && i + 1 < s.length && '"\\$`'.includes(s[i + 1])) { cur += s[++i]; continue; }
      if (c === '"') q = null; else cur += c;
      continue;
    }
    if (c === '$' && s[i + 1] === "'") { q = '$'; i++; has = true; continue; }
    if (c === "'" || c === '"') { q = c; has = true; continue; }
    if (c === '\\' && i + 1 < s.length) { cur += s[++i]; has = true; continue; }
    if (/\s/.test(c)) { if (cur || has) { out.push(cur); cur = ''; has = false; } continue; }
    cur += c; has = true;
  }
  if (cur || has) out.push(cur);
  return out;
}

function parseCurl(text) {
  const t = shellSplit(text.trim());
  if (!t.length || !/^curl(\.exe)?$/i.test(t[0])) throw new Error('Command must start with "curl"');
  const req = newRequest('Imported request');
  let method = null;
  const data = [];
  const form = [];
  let getMode = false;
  const withArg = new Set(['-o', '--output', '-w', '--write-out', '--connect-timeout', '-x', '--proxy', '--cacert', '--cert', '--key', '-r', '--range', '--retry']);
  for (let i = 1; i < t.length; i++) {
    let a = t[i];
    let inline = null;
    if (a.startsWith('--') && a.includes('=')) { inline = a.slice(a.indexOf('=') + 1); a = a.slice(0, a.indexOf('=')); }
    const next = () => inline ?? t[++i] ?? '';
    switch (a) {
      case '-X': case '--request': method = next().toUpperCase(); break;
      case '-H': case '--header': {
        const h = next(); const c = h.indexOf(':');
        if (c > 0) req.headers.push(kv(h.slice(0, c).trim(), h.slice(c + 1).trim()));
        break;
      }
      case '-d': case '--data': case '--data-raw': case '--data-binary': case '--data-ascii': case '--data-urlencode':
        data.push(next()); break;
      case '--json':
        data.push(next());
        if (!req.headers.some((h) => /^content-type$/i.test(h.key))) req.headers.push(kv('Content-Type', 'application/json'));
        if (!req.headers.some((h) => /^accept$/i.test(h.key))) req.headers.push(kv('Accept', 'application/json'));
        break;
      case '-F': case '--form': {
        const f = next(); const e = f.indexOf('=');
        form.push(kv(e < 0 ? f : f.slice(0, e), e < 0 ? '' : f.slice(e + 1)));
        break;
      }
      case '-u': case '--user': {
        const u = next(); const c = u.indexOf(':');
        req.auth = { type: 'basic', username: c < 0 ? u : u.slice(0, c), password: c < 0 ? '' : u.slice(c + 1) };
        break;
      }
      case '-b': case '--cookie': req.headers.push(kv('Cookie', next())); break;
      case '-A': case '--user-agent': req.headers.push(kv('User-Agent', next())); break;
      case '-e': case '--referer': req.headers.push(kv('Referer', next())); break;
      case '--url': req.url = next(); break;
      case '-G': case '--get': getMode = true; break;
      case '-I': case '--head': method = 'HEAD'; break;
      case '-k': case '--insecure': req.settings.verifyTls = false; break;
      case '-L': case '--location': req.settings.followRedirects = true; break;
      case '-m': case '--max-time': req.settings.timeoutMs = Math.round(parseFloat(next()) * 1000) || 30000; break;
      default:
        if (withArg.has(a)) { next(); break; }
        if (a.startsWith('-')) break; // flags like --compressed, -s, -v, -sSL
        if (!req.url) req.url = a;
    }
  }

  if (data.length && getMode) {
    req.url += (req.url.includes('?') ? '&' : '?') + data.join('&');
  } else if (form.length) {
    req.body = { mode: 'multipart', text: '', form };
    for (let i = req.headers.length - 1; i >= 0; i--) if (/^content-type$/i.test(req.headers[i].key)) req.headers.splice(i, 1);
  } else if (data.length) {
    const text = data.join('&');
    const ctype = (req.headers.find((h) => /^content-type$/i.test(h.key))?.value || '').toLowerCase();
    let looksJson = false;
    try { JSON.parse(text); looksJson = /^[[{]/.test(text.trim()); } catch { /* no */ }
    if (ctype.includes('json') || looksJson) {
      req.body = { mode: 'json', text: looksJson ? JSON.stringify(JSON.parse(text), null, 2) : text, form: [] };
    } else if ((!ctype || ctype.includes('x-www-form-urlencoded')) && /^[^=&\s]+=[^&]*(&[^=&\s]+=[^&]*)*$/.test(text)) {
      const dec = (s) => { try { return decodeURIComponent(s.replace(/\+/g, ' ')); } catch { return s; } };
      req.body = { mode: 'urlencoded', text: '', form: text.split('&').map((p) => { const e = p.indexOf('='); return kv(dec(p.slice(0, e)), dec(p.slice(e + 1))); }) };
      for (let i = req.headers.length - 1; i >= 0; i--) if (/^content-type$/i.test(req.headers[i].key)) req.headers.splice(i, 1);
    } else {
      req.body = { mode: ctype.includes('xml') ? 'xml' : 'text', text, form: [] };
    }
  }
  req.method = method || ((data.length && !getMode) || form.length ? 'POST' : 'GET');

  // split query string into params
  const qi = req.url.indexOf('?');
  if (qi >= 0) {
    for (const part of req.url.slice(qi + 1).split('#')[0].split('&')) {
      if (!part) continue;
      const e = part.indexOf('=');
      const dec = (s) => { try { return decodeURIComponent(s.replace(/\+/g, ' ')); } catch { return s; } };
      req.params.push(kv(dec(e < 0 ? part : part.slice(0, e)), dec(e < 0 ? '' : part.slice(e + 1))));
    }
    req.url = req.url.slice(0, qi);
  }
  try { req.name = `${req.method} ${new URL(req.url).pathname}`; } catch { /* keep default */ }
  return req;
}

const sq = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`;
function toCurl(final) {
  const lines = [`curl -X ${final.method} ${sq(final.url)}`];
  for (const [k, v] of final.headers) lines.push(`  -H ${sq(`${k}: ${v}`)}`);
  if (final.body != null && final.body !== '') lines.push(`  --data-raw ${sq(final.body)}`);
  if (final.settings?.verifyTls === false) lines.push('  --insecure');
  if (final.settings?.followRedirects !== false) lines.push('  --location');
  return lines.join(' \\\n');
}

/* ------------------------------------------------------------------ */
/* Environments dialog                                                 */
/* ------------------------------------------------------------------ */
let editingEnvId = null;
function renderEnvSelect() {
  const sel = $('#envSelect');
  sel.innerHTML = '<option value="">No environment</option>' +
    ws.environments.map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join('');
  sel.value = ws.activeEnvId || '';
}

function renderEnvDialog() {
  if (!ws.environments.find((e) => e.id === editingEnvId)) editingEnvId = ws.activeEnvId || ws.environments[0]?.id || null;
  $('#envList').innerHTML = ws.environments.map((e) =>
    `<div class="env-item ${e.id === editingEnvId ? 'active' : ''}" data-id="${esc(e.id)}">${esc(e.name)}${e.id === ws.activeEnvId ? ' <span class="hint">(active)</span>' : ''}</div>`).join('') ||
    '<p class="muted">No environments.</p>';
  $$('.env-item').forEach((el) => { el.onclick = () => { editingEnvId = el.dataset.id; renderEnvDialog(); }; });
  const env = ws.environments.find((e) => e.id === editingEnvId);
  $('.env-edit').style.visibility = env ? 'visible' : 'hidden';
  if (!env) return;
  $('#envName').value = env.name;
  renderKV($('#envKV'), env.vars, { keyPh: 'variable', valPh: 'value' });
}

/* ------------------------------------------------------------------ */
/* Collection runner                                                   */
/* ------------------------------------------------------------------ */
async function runCollection() {
  const col = currentCol();
  if (!col) return;
  const dlg = $('#runDialog');
  $('#runTitle').textContent = `Run: ${col.name}`;
  const table = $('#runTable');
  table.innerHTML = '<tr><th>Request</th><th>Status</th><th>Time</th><th>Tests</th></tr>';
  $('#runSummary').textContent = 'Running…';
  dlg.showModal();
  let passed = 0; let failed = 0; let errors = 0;
  const started = performance.now();
  for (const req of col.requests) {
    if (!dlg.open) break; // closing the dialog stops the run
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${esc(req.method)} ${esc(req.name)}</td><td>…</td><td></td><td></td>`;
    table.appendChild(tr);
    const r = await execute(req);
    recordHistory(req, r, 'runner');
    const cells = tr.children;
    if (r.error && !r.response?.ok) {
      errors++;
      cells[1].innerHTML = `<span style="color:var(--err)">${esc(r.error)}</span>`;
    } else {
      const s = r.response.status;
      cells[1].innerHTML = `<span class="pill s${String(s)[0]}">${s}</span>`;
      cells[2].textContent = `${r.response.timeMs} ms`;
    }
    const p = r.tests.filter((x) => x.pass).length;
    passed += p; failed += r.tests.length - p;
    cells[3].innerHTML = r.tests.map((x) =>
      `<div class="test"><span class="${x.pass ? 'ok' : 'bad'}">${x.pass ? '✓' : '✗'}</span> ${esc(x.name)}${x.msg ? `<span class="why">${esc(x.msg)}</span>` : ''}</div>`).join('') || '<span class="muted">—</span>';
  }
  save();
  $('#runSummary').innerHTML = `${col.requests.length} request(s) · <span style="color:var(--ok)">${passed} passed</span> · ` +
    `<span style="color:var(--err)">${failed} failed</span>${errors ? ` · ${errors} error(s)` : ''} · ${Math.round(performance.now() - started)} ms`;
}

/* ------------------------------------------------------------------ */
/* Wiring                                                              */
/* ------------------------------------------------------------------ */
function bindTabs() {
  $$('.tabs').forEach((nav) => {
    nav.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-tab]');
      if (!btn) return;
      $$('button', nav).forEach((b) => b.classList.toggle('active', b === btn));
      const pane = nav.parentElement;
      $$(':scope > .tab-body', pane).forEach((p) => { p.hidden = p.dataset.panel !== btn.dataset.tab; });
    });
  });
}

function bind() {
  bindTabs();
  const onReq = (sel, ev, fn) => $(sel).addEventListener(ev, (e) => { const r = current(); if (r) { fn(r, e); changed(); } });

  onReq('#reqName', 'input', (r, e) => { r.name = e.target.value; renderTree(); });
  onReq('#method', 'change', (r, e) => { r.method = e.target.value; renderTree(); });
  onReq('#url', 'input', (r, e) => { r.url = e.target.value; });
  $('#url').addEventListener('change', () => current() && absorbQuery());
  $('#url').addEventListener('paste', () => setTimeout(() => current() && absorbQuery(), 0));
  onReq('#bodyMode', 'change', (r, e) => { r.body.mode = e.target.value; renderBodyMode(); });
  onReq('#bodyText', 'input', (r, e) => { r.body.text = e.target.value; });
  onReq('#authType', 'change', (r, e) => { r.auth = { ...r.auth, type: e.target.value }; renderAuth(); });
  onReq('#preScript', 'input', (r, e) => { r.scripts.pre = e.target.value; });
  onReq('#postScript', 'input', (r, e) => { r.scripts.post = e.target.value; });
  onReq('#setTimeout', 'input', (r, e) => { r.settings.timeoutMs = parseInt(e.target.value, 10) || 30000; });
  onReq('#setScriptTimeout', 'input', (r, e) => { const v = parseInt(e.target.value, 10); if (v > 0) r.settings.scriptTimeoutMs = v; else delete r.settings.scriptTimeoutMs; });
  onReq('#setFollow', 'change', (r, e) => { r.settings.followRedirects = e.target.checked; });
  onReq('#setVerify', 'change', (r, e) => { r.settings.verifyTls = e.target.checked; });

  $('#btnBeautify').onclick = () => {
    const r = current();
    try { r.body.text = JSON.stringify(JSON.parse(r.body.text), null, 2); $('#bodyText').value = r.body.text; changed(); }
    catch (e) { toast(`Invalid JSON: ${e.message}`); }
  };
  // Tab key inserts two spaces inside code areas
  $$('textarea.code').forEach((ta) => ta.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab' || e.shiftKey) return;
    e.preventDefault();
    ta.setRangeText('  ', ta.selectionStart, ta.selectionEnd, 'end');
    ta.dispatchEvent(new Event('input'));
  }));

  $('#btnSend').onclick = send;
  document.addEventListener('keydown', (e) => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key === 'Enter') { e.preventDefault(); send(); }
    if (mod && e.key.toLowerCase() === 's') { e.preventDefault(); save(); toast('Saved'); }
  });

  $('#btnNewCol').onclick = () => {
    const name = prompt('Collection name', 'My Collection');
    if (!name) return;
    const col = { id: uid(), name, collapsed: false, requests: [] };
    ws.collections.push(col);
    addRequest(col);
  };
  $('#filter').oninput = renderTree;
  $('#btnClearHist').onclick = async () => {
    if (!hist.total || !confirm(`Delete all ${hist.total} history entries?`)) return;
    await api('/api/history', { method: 'DELETE' });
    hist.items = []; hist.total = 0; renderHistory();
  };
  let histTimer = null;
  $('#histFilter').oninput = (e) => {
    clearTimeout(histTimer);
    histTimer = setTimeout(() => { hist.q = e.target.value.trim(); loadHistory(); }, 200);
  };

  $('#btnPretty').onclick = () => { prettyMode = true; $('#btnPretty').classList.add('on'); $('#btnRaw').classList.remove('on'); renderBody(); };
  $('#btnRaw').onclick = () => { prettyMode = false; $('#btnRaw').classList.add('on'); $('#btnPretty').classList.remove('on'); renderBody(); };
  $('#jsonFilter').oninput = renderBody;
  $('#btnCopyBody').onclick = async () => {
    const text = $('#resBody').textContent;
    try { await navigator.clipboard.writeText(text); toast('Response copied'); } catch { toast('Clipboard not available'); }
  };

  $('#btnCopyCurl').onclick = async () => {
    const r = current(); if (!r) return;
    const c = toCurl(buildFinal(r, envObject()));
    try { await navigator.clipboard.writeText(c); toast('cURL copied (pre-request script not applied)'); }
    catch { prompt('Copy this cURL command:', c); }
  };
  $('#btnImportCurl').onclick = () => { $('#curlText').value = ''; $('#curlDialog').showModal(); $('#curlText').focus(); };
  $('#btnDoCurl').onclick = () => {
    try {
      const req = parseCurl($('#curlText').value);
      $('#curlDialog').close();
      addRequest(currentCol(), req);
      toast('Imported');
    } catch (e) { toast(e.message); }
  };

  $('#envSelect').onchange = (e) => { ws.activeEnvId = e.target.value || null; save(); updateResolvedPreview(); };
  $('#btnEnv').onclick = () => { renderEnvDialog(); $('#envDialog').showModal(); };
  $('#btnAddEnv').onclick = () => {
    const env = { id: uid(), name: 'New environment', vars: [kv('', '')] };
    ws.environments.push(env);
    editingEnvId = env.id;
    if (!ws.activeEnvId) ws.activeEnvId = env.id;
    save(); renderEnvSelect(); renderEnvDialog();
    $('#envName').select();
  };
  $('#envName').oninput = (e) => {
    const env = ws.environments.find((x) => x.id === editingEnvId);
    if (env) { env.name = e.target.value; save(); renderEnvSelect(); $$('.env-item.active').forEach((el) => { el.firstChild.textContent = env.name; }); }
  };
  $('#btnDelEnv').onclick = () => {
    const env = ws.environments.find((x) => x.id === editingEnvId);
    if (!env || !confirm(`Delete environment "${env.name}"?`)) return;
    ws.environments.splice(ws.environments.indexOf(env), 1);
    if (ws.activeEnvId === env.id) ws.activeEnvId = ws.environments[0]?.id || null;
    save(); renderEnvSelect(); renderEnvDialog(); updateResolvedPreview();
  };
  $('#envDialog').addEventListener('close', () => { renderEnvSelect(); updateResolvedPreview(); });

  $('#btnRunCol').onclick = runCollection;
  $('#btnTrash').onclick = openTrash;
  $('#btnEmptyTrash').onclick = async () => {
    if (!confirm('Permanently delete everything in the trash?')) return;
    await api('/api/trash', { method: 'DELETE' });
    openTrash();
  };
  $$('[data-close]').forEach((b) => { b.onclick = () => b.closest('dialog').close(); });

  $('#btnExport').onclick = async () => {
    await flushSave();
    const withHist = hist.total && confirm('Include call history in the export?\n(Responses may contain tokens or personal data.)');
    let text;
    try { text = JSON.stringify(await api(`/api/export?history=${withHist ? 1 : 0}`), null, 2); }
    catch (e) { toast(`Export failed: ${e.message}`); return; }
    const blob = new Blob([text], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `restdesk-workspace-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  $('#btnImport').onclick = () => $('#fileImport').click();
  $('#fileImport').onchange = async (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      if (!Array.isArray(data.collections)) throw new Error('not a RestDesk workspace');
      const mode = confirm('OK = merge into current workspace\nCancel = replace current workspace');
      const incoming = normalizeWorkspace(data);
      if (mode) {
        ws.collections.push(...incoming.collections);
        ws.environments.push(...incoming.environments.filter((e2) => !ws.environments.some((x) => x.id === e2.id)));
      } else {
        ws = incoming;
      }
      save(); renderAll();
      const calls = Array.isArray(data.callHistory) ? data.callHistory : [];
      for (const h of calls.slice().reverse()) {
        if (!h?.snapshot) continue;
        await api('/api/history', { method: 'POST', body: JSON.stringify({ id: h.id, ts: h.ts, source: h.source, snapshot: h.snapshot, result: h.result }) });
      }
      if (calls.length) loadHistory();
      toast(`Workspace imported${calls.length ? ` (+${calls.length} history entries)` : ''}`);
    } catch (err) { toast(`Import failed: ${err.message}`); }
  };
}

function renderAll() {
  renderEnvSelect();
  renderHistory();
  const first = ws.collections.find((c) => c.requests.length)?.requests[0]?.id || null;
  selectRequest(findRequest(ws.lastRequestId) ? ws.lastRequestId : first);
}

async function init() {
  bind();
  let fresh = false;
  try {
    const data = await api('/api/workspace');
    wsRevision = data.revision || 0;
    delete data.revision;
    fresh = !Array.isArray(data.collections);
    ws = normalizeWorkspace(data);
  } catch (e) {
    document.body.innerHTML = `<p style="padding:20px">Cannot reach the RestDesk server: ${esc(e.message)}.<br>Start it with <code>uv run restdesk</code>.</p>`;
    return;
  }
  renderAll();
  loadHistory();
  if (fresh) save(); // first run: persist the example workspace
  try {
    const info = await api('/api/info');
    $('#saveState').title = `Stored in ${info.dataPath}`;
  } catch { /* older server */ }

  // Save as soon as the tab is hidden (switching tabs, minimising, closing on mobile).
  document.addEventListener('visibilitychange', () => { if (document.hidden) flushSave(); });
  window.addEventListener('beforeunload', (e) => {
    if (!hasPendingSave()) return;
    const body = JSON.stringify({ ...ws, baseRevision: wsRevision });
    if (!saveInFlight && body.length < KEEPALIVE_LIMIT) {
      clearTimeout(saveTimer); saveTimer = null;
      // keepalive lets this small PUT finish while the page unloads
      fetch('/api/workspace', { method: 'PUT', body, keepalive: true, headers: { 'Content-Type': 'application/json', 'X-RestDesk': '1' } });
      return;
    }
    // Too big for keepalive (or a save is still running): ask the browser to confirm leaving.
    flushSave();
    e.preventDefault();
    e.returnValue = '';
  });
}

init();
