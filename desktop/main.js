// RestDesk desktop: a thin Electron shell around the same local server the web version uses.
//
// On start it launches the RestDesk server on a free port (the bundled binary in a packaged
// app, `uv run restdesk` from the repo in development), waits for its RESTDESK_READY line and
// opens that URL in a window. The server is started with --exit-with-stdin, so it stops when
// this process exits for any reason, including a crash.
'use strict';

const { app, BrowserWindow, Menu, dialog, shell } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');

const SMOKE = process.env.RESTDESK_SMOKE === '1'; // CI: start, check the UI renders, quit
const READY_TIMEOUT_MS = 30000;
const ICON = path.join(__dirname, 'resources', 'icon.png'); // window/taskbar icon on Windows and Linux

let server = null;
let serverUrl = null;
let quitting = false;

function serverCommand() {
  if (process.env.RESTDESK_SERVER) return { cmd: process.env.RESTDESK_SERVER, args: [] };
  if (app.isPackaged) {
    const exe = process.platform === 'win32' ? 'restdesk-server.exe' : 'restdesk-server';
    return { cmd: path.join(process.resourcesPath, 'server', exe), args: [] };
  }
  // Development: run the Python package from this checkout.
  return { cmd: 'uv', args: ['run', '--project', path.resolve(__dirname, '..'), 'restdesk'] };
}

function startServer() {
  const { cmd, args } = serverCommand();
  const extra = ['--port', '0', '--no-browser', '--exit-with-stdin'];
  if (process.env.RESTDESK_DATA) extra.push('--data', process.env.RESTDESK_DATA);

  return new Promise((resolve, reject) => {
    let out = '';
    let log = '';
    const fail = (msg) => { clearTimeout(timer); reject(new Error(`${msg}\n\n${log.slice(-2000)}`.trim())); };
    const timer = setTimeout(() => fail('The RestDesk server did not start in time.'), READY_TIMEOUT_MS);

    server = spawn(cmd, [...args, ...extra], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    server.on('error', (e) => fail(`Could not start the RestDesk server (${cmd}): ${e.message}`));
    server.stderr.on('data', (d) => { log += d; });
    server.stdout.on('data', (d) => {
      out += d;
      log += d;
      const m = out.match(/^RESTDESK_READY (\S+)/m);
      if (m && !serverUrl) { clearTimeout(timer); serverUrl = m[1]; resolve(serverUrl); }
    });
    server.on('exit', (code) => {
      if (!serverUrl) { fail(`The RestDesk server exited (code ${code}).`); return; }
      if (!quitting) {
        dialog.showErrorBox('RestDesk', `The RestDesk server stopped unexpectedly (code ${code}).\n\n${log.slice(-2000)}`);
        app.quit();
      }
    });
  });
}

function stopServer() {
  if (!server || server.exitCode !== null) return;
  server.stdin.end(); // --exit-with-stdin: lets SQLite close cleanly
  setTimeout(() => { if (server.exitCode === null) server.kill(); }, 3000).unref();
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1360,
    height: 860,
    minWidth: 760,
    minHeight: 480,
    title: 'RestDesk',
    show: false,
    autoHideMenuBar: true,
    icon: ICON,
    webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false, spellcheck: false },
  });
  win.once('ready-to-show', () => { if (!SMOKE) win.show(); });

  const sameOrigin = (url) => { try { return new URL(url).origin === new URL(serverUrl).origin; } catch { return false; } };
  // Links that leave the app open in the system browser, never inside it.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e, url) => {
    if (sameOrigin(url)) return;
    e.preventDefault();
    if (/^https?:/i.test(url)) shell.openExternal(url);
  });

  // Write any pending autosave before the window goes away (the server stops right after).
  let flushed = false;
  win.on('close', (e) => {
    if (flushed) return;
    e.preventDefault();
    const flush = win.webContents.executeJavaScript('typeof flushSave === "function" ? flushSave() : null', true);
    const timeout = new Promise((r) => setTimeout(r, 3000));
    Promise.race([flush, timeout]).catch(() => {}).finally(() => { flushed = true; win.close(); });
  });
  // If a save still could not finish, ask instead of silently refusing to close.
  win.webContents.on('will-prevent-unload', (e) => {
    const choice = dialog.showMessageBoxSync(win, {
      type: 'warning', buttons: ['Close anyway', 'Stay'], defaultId: 1, cancelId: 1,
      message: 'Some changes are still being saved.', detail: 'Closing now may lose your latest edit.',
    });
    if (choice === 0) e.preventDefault();
  });

  win.loadURL(serverUrl);
  return win;
}

async function smokeTest(win) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const rows = await win.webContents.executeJavaScript('document.querySelectorAll(".req-row").length').catch(() => 0);
    if (rows > 0) { console.log(`RESTDESK_SMOKE_OK ${rows} requests`); return 0; }
    await new Promise((r) => setTimeout(r, 250));
  }
  console.error('RESTDESK_SMOKE_FAIL: UI did not render');
  return 1;
}

function buildMenu() {
  const isMac = process.platform === 'darwin';
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(isMac ? [{ role: 'appMenu' }] : []),
    { role: 'fileMenu' },
    { role: 'editMenu' },
    { role: 'viewMenu' },
    { role: 'windowMenu' },
    { role: 'help', submenu: [
      { label: 'Documentation', click: () => shell.openExternal('https://github.com/shukla-surendra/restdesk/tree/main/docs') },
      { label: 'Open in Browser', click: () => serverUrl && shell.openExternal(serverUrl) },
    ] },
  ]));
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const win = BrowserWindow.getAllWindows()[0];
    if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
  });

  app.whenReady().then(async () => {
    buildMenu();
    if (process.platform === 'darwin' && !app.isPackaged) app.dock?.setIcon(ICON); // packaged apps use the .icns
    try {
      await startServer();
    } catch (e) {
      if (SMOKE) console.error(`RESTDESK_SMOKE_FAIL: ${e.message}`);
      else dialog.showErrorBox('RestDesk could not start', e.message);
      app.exit(1);
      return;
    }
    const win = createWindow();
    if (SMOKE) { const code = await smokeTest(win); quitting = true; stopServer(); app.exit(code); return; }
    app.on('activate', () => { if (!BrowserWindow.getAllWindows().length) createWindow(); });
  });

  app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
  app.on('before-quit', () => { quitting = true; });
  app.on('will-quit', stopServer);
}

