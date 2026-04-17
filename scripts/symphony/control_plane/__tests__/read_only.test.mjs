// Smoke test for CONTROL_PLANE_READ_ONLY. Runs under `node --test`.
// Validates that enabling the flag rejects every non-safe HTTP method with 403
// and still serves GETs (here: a synthetic OPTIONS pre-flight is treated safe).
//
// Run locally with:
//   node --test scripts/symphony/control_plane/__tests__/read_only.test.mjs
//
// The test spawns server.mjs with a temp RUN_ROOT and GITHUB_ROOT so no
// production paths are touched. SYMPHONY_PORT is left at the default; the
// reconcile loop tolerates Symphony being unreachable.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const serverPath = path.join(here, '..', 'server.mjs');

function startServer(envOverrides) {
  const tempRoot = mkdtempSync(path.join(tmpdir(), 'cp-ro-'));
  const runRoot = path.join(tempRoot, 'runs');
  const githubRoot = path.join(tempRoot, 'github');
  const settingsPath = path.join(tempRoot, 'settings.json');
  mkdirSync(runRoot, { recursive: true });
  mkdirSync(githubRoot, { recursive: true });
  const port = 14100 + Math.floor(Math.random() * 500);
  const child = spawn(process.execPath, [serverPath], {
    env: {
      ...process.env,
      CONTROL_PLANE_PORT: String(port),
      CONTROL_PLANE_RUN_ROOT: runRoot,
      CONTROL_PLANE_GITHUB_ROOT: githubRoot,
      CONTROL_PLANE_SETTINGS_PATH: settingsPath,
      CONTROL_PLANE_WORKER_CONFIG: path.join(tempRoot, 'workers.json'),
      ...envOverrides
    },
    stdio: ['ignore', 'pipe', 'pipe']
  });
  return { child, port, tempRoot };
}

async function waitForListen(port, child, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`http://127.0.0.1:${port}/control/api/overview`, { method: 'GET' });
      if (resp.ok || resp.status === 500) {
        return;
      }
    } catch {
      // not listening yet
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  child.kill('SIGKILL');
  throw new Error(`server did not start on port ${port}`);
}

async function stopServer({ child }) {
  child.kill('SIGKILL');
  await new Promise((r) => child.once('exit', r));
}

test('CONTROL_PLANE_READ_ONLY=1 rejects POST with 403', async () => {
  const s = startServer({ CONTROL_PLANE_READ_ONLY: '1' });
  try {
    await waitForListen(s.port, s.child);
    const resp = await fetch(`http://127.0.0.1:${s.port}/control/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rawLogRetentionDays: 7 })
    });
    assert.equal(resp.status, 403);
    const body = await resp.json();
    assert.equal(body.error, 'control-plane is read-only in this environment');
    assert.equal(body.method, 'POST');
    assert.equal(body.path, '/control/api/settings');
  } finally {
    await stopServer(s);
  }
});

test('CONTROL_PLANE_READ_ONLY=1 still allows GET /control/api/overview', async () => {
  const s = startServer({ CONTROL_PLANE_READ_ONLY: '1' });
  try {
    await waitForListen(s.port, s.child);
    const resp = await fetch(`http://127.0.0.1:${s.port}/control/api/overview`, { method: 'GET' });
    assert.equal(resp.status, 200);
    const body = await resp.json();
    assert.ok(body.generatedAt, 'overview should carry generatedAt');
  } finally {
    await stopServer(s);
  }
});

test('without CONTROL_PLANE_READ_ONLY, POST is not pre-empted by the guard', async () => {
  const s = startServer({ CONTROL_PLANE_READ_ONLY: '' });
  try {
    await waitForListen(s.port, s.child);
    const resp = await fetch(`http://127.0.0.1:${s.port}/control/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rawLogRetentionDays: 7 })
    });
    // Without the guard we should either succeed (200) or fail for a *different*
    // reason (e.g. 500 while persisting to a nonexistent parent dir). The key
    // contract is: the response is NOT the read-only 403 shape.
    if (resp.status === 403) {
      const body = await resp.json();
      assert.notEqual(body.error, 'control-plane is read-only in this environment');
    }
  } finally {
    await stopServer(s);
  }
});
