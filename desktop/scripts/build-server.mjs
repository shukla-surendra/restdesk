// Freeze the Python server into a standalone binary (no Python needed on the user's machine).
// Output: desktop/build/server-dist/restdesk-server/  (copied into the app as resources/server)
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repo = path.resolve(desktop, '..');
const build = path.join(desktop, 'build');

const r = spawnSync('uv', [
  'run', '--project', repo, '--with', 'pyinstaller>=6.10',
  'pyinstaller', '--noconfirm', '--clean', '--onedir', '--console',
  '--name', 'restdesk-server',
  '--collect-data', 'restdesk',
  '--hidden-import', 'restdesk.mcp', // `restdesk-server mcp` (imported lazily)
  '--distpath', path.join(build, 'server-dist'),
  '--workpath', path.join(build, 'pyinstaller'),
  '--specpath', build,
  path.join(desktop, 'server_entry.py'),
], { stdio: 'inherit', cwd: repo, shell: process.platform === 'win32' });
process.exit(r.status ?? 1);
