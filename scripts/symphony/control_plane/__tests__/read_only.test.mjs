// Smoke test for CONTROL_PLANE_READ_ONLY. Runs under `node --test`.
// Validates that enabling the flag rejects mutating control-plane calls with 403
// while still allowing safe reads for overview/settings/deployment catalog and
// strategy-node request history.
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
import { chmodSync, mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const serverPath = path.join(here, '..', 'server.mjs');

function startServer(envOverrides) {
  const tempRoot = mkdtempSync(path.join(tmpdir(), 'cp-ro-'));
  const binDir = path.join(tempRoot, 'bin');
  const runRoot = path.join(tempRoot, 'runs');
  const githubRoot = path.join(tempRoot, 'github');
  const settingsPath = path.join(tempRoot, 'settings.json');
  const strategyNodeManifestRoot = path.join(tempRoot, 'strategy-node-manifests');
  const strategyNodeRequestRoot = path.join(tempRoot, 'strategy-node-requests');
  const tradingNodeRoot = path.join(tempRoot, 'strategy-nodes');
  mkdirSync(binDir, { recursive: true });
  mkdirSync(runRoot, { recursive: true });
  mkdirSync(githubRoot, { recursive: true });
  mkdirSync(strategyNodeManifestRoot, { recursive: true });
  mkdirSync(strategyNodeRequestRoot, { recursive: true });
  mkdirSync(tradingNodeRoot, { recursive: true });
  const dockerPath = path.join(binDir, 'docker');
  writeFileSync(
    dockerPath,
    `#!/usr/bin/env bash
set -euo pipefail
case "\${1:-}" in
  ps) exit 0 ;;
  inspect) exit 1 ;;
  logs) exit 1 ;;
  *) exit 0 ;;
esac
`,
    'utf8',
  );
  chmodSync(dockerPath, 0o755);
  const port = 14100 + Math.floor(Math.random() * 500);
  const child = spawn(process.execPath, [serverPath], {
    env: {
      ...process.env,
      PATH: `${binDir}:${process.env.PATH || ''}`,
      CONTROL_PLANE_PORT: String(port),
      CONTROL_PLANE_RUN_ROOT: runRoot,
      CONTROL_PLANE_GITHUB_ROOT: githubRoot,
      CONTROL_PLANE_SETTINGS_PATH: settingsPath,
      CONTROL_PLANE_STRATEGY_NODE_MANIFEST_ROOT: strategyNodeManifestRoot,
      CONTROL_PLANE_STRATEGY_NODE_REQUEST_ROOT: strategyNodeRequestRoot,
      CONTROL_PLANE_TRADING_NODE_ROOT: tradingNodeRoot,
      CONTROL_PLANE_DISABLE_SECRET_MANAGER: '1',
      CONTROL_PLANE_WORKER_CONFIG: path.join(tempRoot, 'workers.json'),
      ...envOverrides
    },
    stdio: ['ignore', 'pipe', 'pipe']
  });
  child.output = '';
  child.stdout.on('data', (chunk) => {
    child.output += chunk.toString();
  });
  child.stderr.on('data', (chunk) => {
    child.output += chunk.toString();
  });
  return { child, port, tempRoot, strategyNodeManifestRoot, strategyNodeRequestRoot };
}

function writeStrategyNodeManifest(rootDir, fileName, manifest = {}) {
  const filePath = path.join(rootDir, fileName);
  writeFileSync(
    filePath,
    JSON.stringify(
      {
        node_id: 'sxbet-single-venue',
        trader_id: 'betting-arbitrage',
        validation_mode: true,
        allow_dummy_credentials: true,
        metadata: { recommended_worker: 'codex-a' },
        venues: [
          {
            venue: 'SXBET',
            client_key: 'SXBET',
            data_enabled: true,
            execution_enabled: false
          }
        ],
        ...manifest
      },
      null,
      2
    )
  );
  return filePath;
}

async function waitForListen(port, child, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(`server exited before listening: ${child.output || '<no output>'}`);
    }
    try {
      const resp = await fetch(`http://127.0.0.1:${port}/control/api/settings`, { method: 'GET' });
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
  if (child.exitCode !== null || child.signalCode !== null) {
    return;
  }
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

test('CONTROL_PLANE_READ_ONLY=1 rejects strategy-node deployment requests with 403', async () => {
  const s = startServer({ CONTROL_PLANE_READ_ONLY: '1' });
  try {
    writeStrategyNodeManifest(s.strategyNodeManifestRoot, 'sxbet-single-venue.json');
    await waitForListen(s.port, s.child);
    const resp = await fetch(`http://127.0.0.1:${s.port}/control/api/deployments/requests`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifestFile: 'sxbet-single-venue.json',
        rolloutMode: 'validate_only',
        requestedBy: 'unit-test',
        workerName: 'codex-a',
        target: 'production',
        notes: 'should be blocked by read-only'
      })
    });
    assert.equal(resp.status, 403);
    const body = await resp.json();
    assert.equal(body.error, 'control-plane is read-only in this environment');
    assert.equal(body.method, 'POST');
    assert.equal(body.path, '/control/api/deployments/requests');
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

test('CONTROL_PLANE_READ_ONLY=1 still allows strategy-node catalog and request listing', async () => {
  const s = startServer({ CONTROL_PLANE_READ_ONLY: '1' });
  try {
    writeStrategyNodeManifest(s.strategyNodeManifestRoot, 'sxbet-single-venue.json');
    await waitForListen(s.port, s.child);
    const catalogResp = await fetch(`http://127.0.0.1:${s.port}/control/api/deployments/catalog`, {
      method: 'GET'
    });
    assert.equal(catalogResp.status, 200);
    const catalogBody = await catalogResp.json();
    assert.ok(Array.isArray(catalogBody.manifests));
    assert.equal(catalogBody.manifests.length, 1);
    assert.equal(catalogBody.manifests[0].manifestFile, 'sxbet-single-venue.json');

    const requestsResp = await fetch(`http://127.0.0.1:${s.port}/control/api/deployments/requests?limit=5`, {
      method: 'GET'
    });
    assert.equal(requestsResp.status, 200);
    const requestsBody = await requestsResp.json();
    assert.deepEqual(requestsBody.requests, []);
  } finally {
    await stopServer(s);
  }
});

test('without CONTROL_PLANE_READ_ONLY, strategy-node deployment requests are queued and listed', async () => {
  const s = startServer({ CONTROL_PLANE_READ_ONLY: '' });
  try {
    writeStrategyNodeManifest(s.strategyNodeManifestRoot, 'sxbet-single-venue.json');
    await waitForListen(s.port, s.child);
    const resp = await fetch(`http://127.0.0.1:${s.port}/control/api/deployments/requests`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifestFile: 'sxbet-single-venue.json',
        rolloutMode: 'validate_only',
        requestedBy: 'unit-test',
        workerName: 'codex-a',
        target: 'production',
        notes: 'queue a strategy-node request'
      })
    });
    assert.equal(resp.status, 200);
    const body = await resp.json();
    assert.equal(body.success, true);
    assert.equal(body.request.status, 'queued');
    assert.equal(body.request.manifestFile, 'sxbet-single-venue.json');
    assert.equal(body.request.rolloutMode, 'validate_only');

    const listResp = await fetch(`http://127.0.0.1:${s.port}/control/api/deployments/requests?limit=5`, {
      method: 'GET'
    });
    assert.equal(listResp.status, 200);
    const listBody = await listResp.json();
    assert.ok(Array.isArray(listBody.requests));
    assert.equal(listBody.requests.length, 1);
    assert.equal(listBody.requests[0].id, body.request.id);
    assert.equal(listBody.requests[0].status, 'queued');
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
