import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { chmodSync, mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const serverPath = path.join(here, '..', 'server.mjs');

function writeExecutable(filePath, content) {
  mkdirSync(path.dirname(filePath), { recursive: true });
  writeFileSync(filePath, content, 'utf8');
  chmodSync(filePath, 0o755);
}

function writeJson(filePath, payload) {
  mkdirSync(path.dirname(filePath), { recursive: true });
  writeFileSync(filePath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
}

function createFixtureLayout(tempRoot, options = {}) {
  const binDir = path.join(tempRoot, 'bin');
  const repoRoot = path.join(tempRoot, 'repo');
  const nodeRoot = path.join(tempRoot, 'strategy-nodes');
  const stateRoot = path.join(tempRoot, 'state');
  const workerStateRoot = path.join(tempRoot, 'worker-state');
  const hostRegistryPath = path.join(workerStateRoot, 'trading-hosts', 'hosts.json');
  const nodeRegistryPath = path.join(workerStateRoot, 'trading-nodes', 'registry.json');
  const discoveryRoot = path.join(workerStateRoot, 'trading-nodes', 'discovery');
  const runRoot = path.join(tempRoot, 'runs');
  const githubRoot = path.join(tempRoot, 'github');
  const settingsPath = path.join(tempRoot, 'settings.json');
  const manifestRoot = path.join(tempRoot, 'strategy-node-manifests');
  const requestRoot = path.join(tempRoot, 'strategy-node-requests');
  const workerConfigPath = path.join(tempRoot, 'workers.json');
  const nodeDir = path.join(nodeRoot, 'betting-arbitrage-node-sxbet');
  const sessionId = '20260417T042532Z-test';
  const sessionDir = path.join(nodeDir, 'sessions', sessionId);
  const freshHeartbeatAt = new Date().toISOString();

  mkdirSync(binDir, { recursive: true });
  mkdirSync(repoRoot, { recursive: true });
  mkdirSync(nodeDir, { recursive: true });
  mkdirSync(stateRoot, { recursive: true });
  mkdirSync(runRoot, { recursive: true });
  mkdirSync(githubRoot, { recursive: true });
  mkdirSync(manifestRoot, { recursive: true });
  mkdirSync(requestRoot, { recursive: true });
  mkdirSync(sessionDir, { recursive: true });

  writeExecutable(
    path.join(binDir, 'docker'),
    `#!/usr/bin/env bash
set -euo pipefail
cmd="$1"
shift || true
case "$cmd" in
  ps)
    echo '{"Names":"betting-arbitrage-node-sxbet","Image":"local/betting-arbitrage-node:sxbet-test","Status":"Up 5 minutes"}'
    ;;
  inspect)
    name="\${1:-}"
    if [ "$name" != "betting-arbitrage-node-sxbet" ]; then
      exit 1
    fi
    cat <<'JSON'
[{"Config":{"Image":"local/betting-arbitrage-node:sxbet-test","Entrypoint":["/var/lib/nautilus-node/run_with_logs.sh"],"Cmd":["python3","-m","nautilus_trader.live.strategy_nodes.betting_arbitrage","run","--manifest","/srv/node/manifest.json"]},"State":{"Status":"running","Running":true,"Restarting":false,"Dead":false,"Pid":4242,"StartedAt":"2026-04-17T04:25:32Z","FinishedAt":"0001-01-01T00:00:00Z","ExitCode":0},"RestartCount":0,"HostConfig":{"LogConfig":{"Type":"json-file","Config":{"max-size":"20m","max-file":"5"}}},"Created":"2026-04-17T04:25:31Z"}]
JSON
    ;;
  top)
    cat <<'TOP'
PID PPID ELAPSED CMD
4242 1 00:03:11 python3 -m nautilus_trader.live.strategy_nodes.betting_arbitrage run --manifest /srv/node/manifest.json
TOP
    ;;
  logs)
    cat <<'LOG'
2026-04-17T04:25:32Z strategy node boot
2026-04-17T04:25:33Z heartbeat ok
LOG
    ;;
  rm)
    if [ "\${CONTROL_PLANE_TEST_DOCKER_RM_MISSING:-}" = "1" ]; then
      echo "Error: No such container: \${2:-}" >&2
      exit 1
    fi
    echo "\${2:-}" > "\${CONTROL_PLANE_TEST_STATE_ROOT}/docker-rm.txt"
    ;;
  *)
    exit 0
    ;;
esac
`,
  );

  writeExecutable(
    path.join(binDir, 'python3'),
    `#!/usr/bin/env bash
set -euo pipefail
cat <<'JSON'
{"engine":"fake-render","nodeId":"sxbet-single-venue","venues":["SXBET"]}
JSON
`,
  );

  writeExecutable(
    path.join(repoRoot, 'scripts', 'deploy', 'strategy_nodes', 'deploy_betting_strategy_node.sh'),
    `#!/usr/bin/env bash
set -euo pipefail
manifest=""
image=""
name=""
root=""
env_file=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --manifest) manifest="$2"; shift 2 ;;
    --image) image="$2"; shift 2 ;;
    --name) name="$2"; shift 2 ;;
    --root) root="$2"; shift 2 ;;
    --env-file) env_file="$2"; shift 2 ;;
    --registry-user|--registry-token-file) shift 2 ;;
    *) shift ;;
  esac
done
node_dir="$root/$name"
mkdir -p "$node_dir"
cp "$manifest" "$node_dir/manifest.runtime.json"
printf '%s\\n' "$image" > "$node_dir/current-image.txt"
cat > "$node_dir/status.json" <<JSON
{"nodeId":"sxbet-single-venue","status":"running","startedAt":"2026-04-17T04:25:32Z","manifestPath":"$manifest","renderedConfigPath":"$node_dir/runtime-config.json"}
JSON
cat > "$node_dir/heartbeat.json" <<JSON
{"at":"2026-04-17T04:25:33Z"}
JSON
echo "$env_file" > "\${CONTROL_PLANE_TEST_STATE_ROOT}/deploy-env-path.txt"
    echo "$image" > "\${CONTROL_PLANE_TEST_STATE_ROOT}/deploy-image.txt"
`,
  );

  if (!options.discoveryWithoutNodeId) {
    writeJson(path.join(nodeDir, 'status.json'), {
      nodeId: 'sxbet-single-venue',
      status: 'running',
      startedAt: '2026-04-17T04:25:32Z',
      manifestPath: path.join(nodeDir, 'manifest.runtime.json'),
      renderedConfigPath: path.join(nodeDir, 'runtime-config.json'),
    });
  }
  writeJson(path.join(nodeDir, 'heartbeat.json'), { at: freshHeartbeatAt });
  writeJson(path.join(nodeDir, 'release.json'), {
    image: 'local/betting-arbitrage-node:sxbet-test',
    manifest: path.join(nodeDir, 'manifest.runtime.json'),
    envFile: path.join(nodeDir, 'runtime.env'),
    sessionId,
    sessionDir,
    logPath: path.join(sessionDir, 'node.log'),
    eventLogPath: path.join(sessionDir, 'events.jsonl'),
  });
  writeJson(path.join(nodeDir, 'current-session.json'), {
    container: 'betting-arbitrage-node-sxbet',
    sessionId,
    hostSessionDir: sessionDir,
    logPath: path.join(sessionDir, 'node.log'),
    eventLogPath: path.join(sessionDir, 'events.jsonl'),
    startedAt: '2026-04-17T04:25:32Z',
  });
  writeFileSync(
    path.join(sessionDir, 'events.jsonl'),
    [
      '{"at":"2026-04-17T04:25:31Z","event":"deploy_started","message":"Preparing strategy-node deployment"}',
      '{"at":"2026-04-17T04:25:32Z","event":"process_started","message":"Launching betting arbitrage trading node","sessionId":"20260417T042532Z-test"}',
      '{"at":"2026-04-17T04:25:33Z","event":"running","message":"Trading node heartbeat observed","sessionId":"20260417T042532Z-test"}',
    ].join('\n') + '\n',
    'utf8',
  );
  writeFileSync(
    path.join(sessionDir, 'node.log'),
    [
      '2026-04-17T04:25:32Z TradingNode: Starting trader_id=betting-arbitrage node_id=sxbet-single-venue',
      '2026-04-17T04:25:33Z TradingNode: Building data client for SXBET_PRIMARY',
      '2026-04-17T04:25:34Z TradingNode: Running strategy betting_arbitrage venues=SXBET',
    ].join('\n') + '\n',
    'utf8',
  );
  if (!options.discoveryWithoutNodeId) {
    writeJson(path.join(nodeDir, 'manifest.runtime.json'), {
      node_id: 'sxbet-single-venue',
      trader_id: 'betting-arbitrage',
      log_level: 'INFO',
      validation_mode: true,
      venues: [
        {
          venue: 'SXBET',
          client_key: 'SXBET',
          data_enabled: true,
          execution_enabled: false,
        },
      ],
      strategy: {
        min_profit_margin: 0.01,
        max_total_stake: 50,
        auto_execute: false,
      },
    });
  }
  writeFileSync(path.join(nodeDir, 'current-image.txt'), 'local/betting-arbitrage-node:sxbet-test\n', 'utf8');
  writeJson(hostRegistryPath, {
    version: 1,
    hosts: [
      {
        hostId: 'local-ec2',
        displayName: 'EC2 trading host',
        kind: 'local',
        labels: ['trading', 'strategy-nodes', 'ec2', 'local'],
        enabled: true,
        rootDir: nodeRoot,
        executor: { kind: 'local' },
      },
    ],
  });
  writeJson(nodeRegistryPath, {
    version: 1,
    nodes: [
      {
        nodeId: 'sxbet-single-venue',
        displayName: 'SXBET single venue',
        hostId: 'local-ec2',
        strategyId: 'betting-arbitrage',
        manifestId: 'sxbet-single-venue',
        containerName: 'betting-arbitrage-node-sxbet',
        venues: ['SXBET'],
        intendedState: 'running',
        lastAppliedConfig: {
          node_id: 'sxbet-single-venue',
          strategy: { min_profit_margin: 0.01 },
        },
      },
    ],
  });
  writeJson(workerConfigPath, { workers: [] });

  return {
    binDir,
    repoRoot,
    nodeRoot,
    stateRoot,
    hostRegistryPath,
    nodeRegistryPath,
    discoveryRoot,
    runRoot,
    githubRoot,
    settingsPath,
    manifestRoot,
    requestRoot,
    workerConfigPath,
  };
}

function startServer(envOverrides = {}, options = {}) {
  const tempRoot = mkdtempSync(path.join(tmpdir(), 'cp-nodes-'));
  const fixture = createFixtureLayout(tempRoot, options);
  const port = 14600 + Math.floor(Math.random() * 400);
  const child = spawn(process.execPath, [serverPath], {
    env: {
      ...process.env,
      PATH: `${fixture.binDir}:${process.env.PATH || ''}`,
      CONTROL_PLANE_PORT: String(port),
      CONTROL_PLANE_RUN_ROOT: fixture.runRoot,
      CONTROL_PLANE_GITHUB_ROOT: fixture.githubRoot,
      CONTROL_PLANE_SETTINGS_PATH: fixture.settingsPath,
      CONTROL_PLANE_STRATEGY_NODE_MANIFEST_ROOT: fixture.manifestRoot,
      CONTROL_PLANE_STRATEGY_NODE_REQUEST_ROOT: fixture.requestRoot,
      CONTROL_PLANE_WORKER_CONFIG: fixture.workerConfigPath,
      CONTROL_PLANE_TRADING_HOST_REGISTRY_PATH: fixture.hostRegistryPath,
      CONTROL_PLANE_TRADING_NODE_REGISTRY_PATH: fixture.nodeRegistryPath,
      CONTROL_PLANE_TRADING_NODE_DISCOVERY_ROOT: fixture.discoveryRoot,
      CONTROL_PLANE_TRADING_NODE_ROOT: fixture.nodeRoot,
      CONTROL_PLANE_DISABLE_SECRET_MANAGER: '1',
      CONTROL_PLANE_TEST_STATE_ROOT: fixture.stateRoot,
      CONTROL_REPO_ROOT: fixture.repoRoot,
      ...envOverrides,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.output = '';
  child.stdout.on('data', (chunk) => {
    child.output += chunk.toString();
  });
  child.stderr.on('data', (chunk) => {
    child.output += chunk.toString();
  });
  return { child, port, tempRoot, fixture };
}

async function waitForListen(port, child, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(`server exited before listening: ${child.output || '<no output>'}`);
    }
    try {
      const resp = await fetch(`http://127.0.0.1:${port}/control/api/overview`, { method: 'GET' });
      if (resp.ok || resp.status === 500) {
        return;
      }
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  child.kill('SIGKILL');
  throw new Error(`server did not start on port ${port}`);
}

async function stopServer({ child }) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return;
  }
  child.kill('SIGKILL');
  await new Promise((resolve) => child.once('exit', resolve));
}

test('trading node inventory and detail endpoints merge registry with discovery', async () => {
  const s = startServer();
  try {
    await waitForListen(s.port, s.child);
    const listResp = await fetch(`http://127.0.0.1:${s.port}/control/api/nodes`);
    assert.equal(listResp.status, 200);
    const listBody = await listResp.json();
    assert.equal(listBody.nodes.length, 1);
    assert.equal(listBody.nodes[0].nodeId, 'sxbet-single-venue');
    assert.equal(listBody.nodes[0].stateClass, 'managed');
    assert.equal(listBody.summary.total, 1);

    const detailResp = await fetch(`http://127.0.0.1:${s.port}/control/api/nodes/sxbet-single-venue`);
    assert.equal(detailResp.status, 200);
    const detailBody = await detailResp.json();
    assert.equal(detailBody.node.nodeId, 'sxbet-single-venue');
    assert.equal(detailBody.registryEntry.nodeId, 'sxbet-single-venue');
    assert.equal(detailBody.discoveryEntry.containerName, 'betting-arbitrage-node-sxbet');
    assert.equal(detailBody.node.sessionId, '20260417T042532Z-test');
    assert.equal(detailBody.node.container.pid, 4242);
    assert.match(detailBody.node.container.processes[0].command, /betting_arbitrage/);
    assert.match(detailBody.logs.content, /Running strategy betting_arbitrage/);
    assert.equal(detailBody.logs.source, 'session-log');

    const aliasResp = await fetch(`http://127.0.0.1:${s.port}/control/api/trading-nodes`);
    assert.equal(aliasResp.status, 200);
    const aliasBody = await aliasResp.json();
    assert.equal(aliasBody.nodes[0].nodeId, 'sxbet-single-venue');

    const sessionsResp = await fetch(
      `http://127.0.0.1:${s.port}/control/api/trading-nodes/sxbet-single-venue/sessions`,
    );
    assert.equal(sessionsResp.status, 200);
    const sessionsBody = await sessionsResp.json();
    assert.equal(sessionsBody.sessions.length, 1);
    assert.equal(sessionsBody.sessions[0].sessionId, '20260417T042532Z-test');
    assert.equal(sessionsBody.sessions[0].active, true);

    const sessionResp = await fetch(
      `http://127.0.0.1:${s.port}/control/api/trading-nodes/sxbet-single-venue/sessions/20260417T042532Z-test`,
    );
    assert.equal(sessionResp.status, 200);
    const sessionBody = await sessionResp.json();
    assert.equal(sessionBody.session.events.length, 3);

    const logsResp = await fetch(
      `http://127.0.0.1:${s.port}/control/api/trading-nodes/sxbet-single-venue/logs?sessionId=current&limit=2`,
    );
    assert.equal(logsResp.status, 200);
    const logsBody = await logsResp.json();
    assert.equal(logsBody.source, 'session-log');
    assert.match(logsBody.content, /Building data client/);
    assert.match(logsBody.content, /Running strategy betting_arbitrage/);
  } finally {
    await stopServer(s);
  }
});

test('trading node inventory still reconciles registry and discovery by container name when discovery loses nodeId', async () => {
  const s = startServer({}, { discoveryWithoutNodeId: true });
  try {
    await waitForListen(s.port, s.child);
    const listResp = await fetch(`http://127.0.0.1:${s.port}/control/api/nodes`);
    assert.equal(listResp.status, 200);
    const listBody = await listResp.json();
    assert.equal(listBody.nodes.length, 1);
    assert.equal(listBody.nodes[0].nodeId, 'sxbet-single-venue');
    assert.equal(listBody.nodes[0].source, 'managed+discovered');

    const detailResp = await fetch(`http://127.0.0.1:${s.port}/control/api/nodes/sxbet-single-venue`);
    assert.equal(detailResp.status, 200);
    const detailBody = await detailResp.json();
    assert.equal(detailBody.discoveryEntry.containerName, 'betting-arbitrage-node-sxbet');
  } finally {
    await stopServer(s);
  }
});

test('trading node render-config stays available in read-only mode while lifecycle actions are blocked', async () => {
  const s = startServer({ CONTROL_PLANE_READ_ONLY: '1' });
  try {
    await waitForListen(s.port, s.child);

    const renderResp = await fetch(`http://127.0.0.1:${s.port}/control/api/nodes/sxbet-single-venue/render-config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        override: {
          validationMode: true,
          executionEnabled: false,
          strategy: { min_profit_margin: 0.025 },
        },
      }),
    });
    assert.equal(renderResp.status, 200);
    const renderBody = await renderResp.json();
    assert.equal(renderBody.ok, true);
    assert.equal(renderBody.manifest.strategy.min_profit_margin, 0.025);
    assert.equal(renderBody.manifest.validation_mode, true);
    assert.equal(renderBody.manifest.venues[0].execution_enabled, false);
    assert.deepEqual(renderBody.renderedConfig.venues, ['SXBET']);

    const restartResp = await fetch(`http://127.0.0.1:${s.port}/control/api/nodes/sxbet-single-venue/restart`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    assert.equal(restartResp.status, 403);
    const restartBody = await restartResp.json();
    assert.equal(restartBody.error, 'control-plane is read-only in this environment');
  } finally {
    await stopServer(s);
  }
});

test('trading node lifecycle endpoints use local host actions and SPA fallback serves routed pages', async () => {
  const s = startServer();
  try {
    await waitForListen(s.port, s.child);

    const spaResp = await fetch(`http://127.0.0.1:${s.port}/nodes`);
    assert.equal(spaResp.status, 200);
    assert.match(await spaResp.text(), /<div id="root"><\/div>/);
    const tradingNodesSpaResp = await fetch(`http://127.0.0.1:${s.port}/trading-nodes/sxbet-single-venue`);
    assert.equal(tradingNodesSpaResp.status, 200);
    assert.match(await tradingNodesSpaResp.text(), /<div id="root"><\/div>/);

    const restartResp = await fetch(`http://127.0.0.1:${s.port}/control/api/nodes/sxbet-single-venue/restart`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        override: {
          strategy: { min_profit_margin: 0.05 },
        },
        imageRef: 'local/betting-arbitrage-node:sxbet-hotfix',
      }),
    });
    assert.equal(restartResp.status, 200);
    const restartBody = await restartResp.json();
    assert.equal(restartBody.success, true);
    assert.equal(restartBody.registryEntry.intendedState, 'running');
    assert.equal(restartBody.registryEntry.imageRef, 'local/betting-arbitrage-node:sxbet-hotfix');

    const stopResp = await fetch(`http://127.0.0.1:${s.port}/control/api/nodes/sxbet-single-venue/stop`, {
      method: 'POST',
    });
    assert.equal(stopResp.status, 200);
    const stopBody = await stopResp.json();
    assert.equal(stopBody.success, true);
    assert.equal(stopBody.registryEntry.intendedState, 'stopped');
  } finally {
    await stopServer(s);
  }
});

test('trading node stop succeeds when the container is already gone', async () => {
  const s = startServer({ CONTROL_PLANE_TEST_DOCKER_RM_MISSING: '1' });
  try {
    await waitForListen(s.port, s.child);
    const stopResp = await fetch(`http://127.0.0.1:${s.port}/control/api/nodes/sxbet-single-venue/stop`, {
      method: 'POST',
    });
    assert.equal(stopResp.status, 200);
    const stopBody = await stopResp.json();
    assert.equal(stopBody.success, true);
    assert.equal(stopBody.registryEntry.intendedState, 'stopped');
  } finally {
    await stopServer(s);
  }
});
