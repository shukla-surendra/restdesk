// Render the icon SVGs to the PNGs electron-builder and the web UI use.
// Run from desktop/:  npx electron scripts/render-icon.cjs
//   resources/icon.svg              -> resources/icon.png (1024, app icon; .icns/.ico are made from it)
//   ../src/restdesk/static/icon.svg -> ../src/restdesk/static/icon-192.png (home-screen / touch icon)
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');

const desktop = path.resolve(__dirname, '..');
const staticDir = path.resolve(desktop, '..', 'src', 'restdesk', 'static');
const SIZE = 1024;

async function render(win, svgFile) {
  const svg = Buffer.from(fs.readFileSync(svgFile, 'utf8')).toString('base64');
  await win.webContents.executeJavaScript(`new Promise((done) => {
    const img = document.getElementById('i');
    img.onload = () => requestAnimationFrame(() => requestAnimationFrame(done));
    img.src = 'data:image/svg+xml;base64,${svg}';
  })`);
  const img = await win.webContents.capturePage();
  return img.getSize().width === SIZE ? img : img.resize({ width: SIZE, height: SIZE, quality: 'best' });
}

app.disableHardwareAcceleration();
app.whenReady().then(async () => {
  try {
    const win = new BrowserWindow({ width: SIZE, height: SIZE, show: false, frame: false, transparent: true,
      useContentSize: true, webPreferences: { offscreen: true } });
    const html = `<html><body style="margin:0;background:transparent;overflow:hidden">
      <img id="i" width="${SIZE}" height="${SIZE}" style="display:block"></body></html>`;
    await win.loadURL('data:text/html;base64,' + Buffer.from(html).toString('base64'));

    const appIcon = await render(win, path.join(desktop, 'resources', 'icon.svg'));
    fs.writeFileSync(path.join(desktop, 'resources', 'icon.png'), appIcon.toPNG());
    const web = await render(win, path.join(staticDir, 'icon.svg'));
    fs.writeFileSync(path.join(staticDir, 'icon-192.png'), web.resize({ width: 192, height: 192, quality: 'best' }).toPNG());
    console.log('icons written');
  } catch (e) {
    console.error(e);
    process.exitCode = 1;
  }
  app.quit();
});
