/* RestDesk script sandbox.
 *
 * Pre-request and test scripts run here, in a dedicated Web Worker, instead of in the page:
 * - no DOM, no access to the workspace, history or other requests' data;
 * - the server sends this file with `Content-Security-Policy: default-src 'none'; script-src
 *   'unsafe-eval'`, so fetch/XHR/WebSocket/importScripts are blocked by the browser;
 * - as a second layer, network and storage APIs are deleted before user code runs;
 * - the page terminates the worker if a script runs past its time limit.
 *
 * Protocol: the page posts {code, vars, req, res}. The worker replies once with
 * {ok, error, vars, changes, req, tests, logs}.
 */
'use strict';

const reply = self.postMessage.bind(self);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function lockdown() {
  const names = ['fetch', 'XMLHttpRequest', 'WebSocket', 'WebSocketStream', 'WebTransport', 'EventSource',
    'importScripts', 'indexedDB', 'caches', 'Worker', 'SharedWorker', 'BroadcastChannel', 'Request',
    'Response', 'FileReaderSync', 'navigator', 'storageFoundation', 'cookieStore', 'serviceWorker'];
  for (const name of names) {
    for (let o = self; o; o = Object.getPrototypeOf(o)) {
      try { delete o[name]; } catch { /* non-configurable: shadow below */ }
    }
    try { Object.defineProperty(self, name, { value: undefined, writable: false, configurable: false }); } catch { /* ignore */ }
  }
}

const fmt = (v) => { try { return JSON.stringify(v) ?? String(v); } catch { return String(v); } };

function expect(actual) {
  const fail = (msg) => { throw new Error(msg); };
  const m = {
    toBe: (e) => actual === e || fail(`expected ${fmt(actual)} to be ${fmt(e)}`),
    toEqual: (e) => JSON.stringify(actual) === JSON.stringify(e) || fail(`expected ${fmt(actual)} to equal ${fmt(e)}`),
    toBeTruthy: () => !!actual || fail(`expected ${fmt(actual)} to be truthy`),
    toBeFalsy: () => !actual || fail(`expected ${fmt(actual)} to be falsy`),
    toContain: (e) => (actual != null && actual.includes(e)) || fail(`expected ${fmt(actual)} to contain ${fmt(e)}`),
    toMatch: (re) => new RegExp(re).test(String(actual)) || fail(`expected ${fmt(actual)} to match ${re}`),
    toBeLessThan: (e) => actual < e || fail(`expected ${fmt(actual)} < ${fmt(e)}`),
    toBeGreaterThan: (e) => actual > e || fail(`expected ${fmt(actual)} > ${fmt(e)}`),
    toHaveProperty: (k) => (actual != null && k in Object(actual)) || fail(`expected object to have property ${fmt(k)}`),
    toBeOneOf: (arr) => arr.includes(actual) || fail(`expected ${fmt(actual)} to be one of ${fmt(arr)}`),
  };
  m.not = Object.fromEntries(Object.entries(m).map(([name, fn]) => [name, (...args) => {
    let passed = true;
    try { fn(...args); } catch { passed = false; }
    if (passed) throw new Error(`expected not ${name}(${args.map(fmt).join(', ')}) on ${fmt(actual)}`);
  }]));
  return m;
}

function makeRes(r) {
  const headers = {};
  for (const [k, v] of r.headers || []) {
    const key = k.toLowerCase();
    headers[key] = headers[key] ? `${headers[key]}, ${v}` : v;
  }
  let parsed;
  return Object.freeze({
    status: r.status, statusText: r.reason, headers, body: r.body, timeMs: r.timeMs, size: r.size, url: r.url,
    header: (n) => headers[String(n).toLowerCase()],
    json: () => { if (parsed === undefined) parsed = JSON.parse(r.body); return parsed; },
  });
}

self.onmessage = async ({ data }) => {
  const { code, vars, req, res } = data;
  const changes = {};
  const logs = [];
  const tests = [];
  const pending = [];
  const out = (lvl) => (...a) => logs.push(`${lvl ? `${lvl} ` : ''}${a.map((x) => (typeof x === 'string' ? x : fmt(x))).join(' ')}`);
  const ctx = {
    env: {
      get: (k) => vars[k],
      set: (k, v) => { vars[k] = v == null ? '' : String(v); changes[k] = vars[k]; },
      unset: (k) => { delete vars[k]; changes[k] = null; },
      all: () => ({ ...vars }),
    },
    req,
    console: { log: out(''), info: out(''), warn: out('[warn]'), error: out('[error]') },
    expect,
    test: (name, fn) => {
      pending.push((async () => {
        try { await fn(); tests.push({ name: String(name), pass: true }); } catch (e) { tests.push({ name: String(name), pass: false, msg: e?.message ?? String(e) }); }
      })());
    },
  };
  if (req) {
    req.setHeader = (k, v) => {
      req.headers ||= [];
      const h = req.headers.find((x) => String(x.key).toLowerCase() === String(k).toLowerCase());
      if (h) { h.value = String(v); h.enabled = true; } else req.headers.push({ key: String(k), value: String(v), enabled: true });
    };
  }
  if (res) ctx.res = makeRes(res);

  let error = null;
  lockdown();
  try {
    const fn = new AsyncFunction(...Object.keys(ctx), code);
    await fn(...Object.values(ctx));
  } catch (e) {
    error = e?.message ?? String(e);
  }
  await Promise.all(pending);
  if (req) delete req.setHeader;
  let safeReq = null;
  try { safeReq = req ? JSON.parse(JSON.stringify(req)) : null; } catch { error ||= 'req is no longer serialisable'; }
  reply({ ok: !error, error, vars, changes, req: safeReq, tests, logs });
};
