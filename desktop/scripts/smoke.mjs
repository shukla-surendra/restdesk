// Launch the app hidden, check the UI renders against a throwaway database, and exit.
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import electron from 'electron';

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = mkdtempSync(path.join(tmpdir(), 'restdesk-smoke-'));
const args = [desktop, ...(process.env.CI && process.platform === 'linux' ? ['--no-sandbox'] : [])];
const r = spawnSync(electron, args, {
  stdio: 'inherit',
  env: { ...process.env, RESTDESK_SMOKE: '1', RESTDESK_DATA: path.join(dir, 'smoke.db') },
});
rmSync(dir, { recursive: true, force: true });
process.exit(r.status ?? 1);
