#!/usr/bin/env node
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { execSync, spawn, spawnSync } from 'node:child_process';
import { isIP } from 'node:net';
import { fileURLToPath } from 'node:url';
import {
  appendRunEvent,
  createOperatorAction,
  listLogStreams,
  listOperatorActions,
  listRunEvents,
  listRuns,
  loadRun,
  purgeExpiredRawLogs,
  readLogChunk,
  readSettings as readControlPlaneSettings,
  saveSettings as saveControlPlaneSettings
} from './run_store.mjs';
import {
  appendGitHubEvent,
  DEFAULT_GITHUB_ROOT,
  listGitHubEvents,
  listGitHubJobLogStreams,
  listGitHubJobs,
  loadGitHubJob,
  readGitHubJobLogChunk,
  upsertGitHubJob,
  upsertGitHubJobLogStream
} from './github_live_store.mjs';
import {
  DEFAULT_STALE_HEARTBEAT_MS,
  buildEffectiveTradingNodes,
  defaultLocalHostRecord,
  discoverLocalTradingNodes,
  mergeTradingNodeManifest,
  readTradingHostRegistry,
  readTradingNodeLogs,
  readTradingNodeRegistry,
  writeTradingHostRegistry,
  writeTradingNodeDiscoverySnapshot,
  writeTradingNodeRegistry,
} from './trading_nodes.mjs';

const DEFAULT_ENV_PATH = '/srv/symphony/symphony.env';
const FILE_ENV = loadEnvFile(DEFAULT_ENV_PATH);
const ENV = { ...FILE_ENV, ...process.env };

const PORT = Number(ENV.CONTROL_PLANE_PORT || 4100);
const SYMPHONY_PORT = Number(ENV.SYMPHONY_PORT || 4000);
// READ_ONLY dev mode: block every non-safe HTTP method (POST/PUT/PATCH/DELETE)
// before it reaches any handler. Used by the Azure dev deployment to prevent
// accidental mutation of prod Symphony/Linear/GitHub state from the dev plane.
// Accepts "1", "true", "yes" (case-insensitive) as truthy.
const CONTROL_PLANE_READ_ONLY = /^(1|true|yes)$/i.test(String(ENV.CONTROL_PLANE_READ_ONLY || '').trim());
const READ_ONLY_SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
const LINEAR_API_KEY = ENV.LINEAR_API_KEY || '';
const GITHUB_TOKEN = ENV.GITHUB_TOKEN || '';
const PROJECT_ID = ENV.LINEAR_PROJECT_ID || '2e1bd292-7154-405c-9252-be85623a0ed3';
const TEAM_ID = ENV.LINEAR_TEAM_ID || 'acea1b83-3ede-48c2-a703-2ff3a409c3c9';
const TEAM_KEY = ENV.LINEAR_TEAM_KEY || 'BET';
const WORKER_CONFIG_PATH = ENV.CONTROL_PLANE_WORKER_CONFIG || '/srv/symphony/control-repo/scripts/symphony/workers.json';
const WORKER_STATE_DIR = '/srv/symphony/worker-state/workers';
const WORKER_LOCK_DIR = '/srv/symphony/worker-state/locks';
const AUTH_SESSION_DIR = '/srv/symphony/worker-state/auth-sessions';
const RUN_ROOT = ENV.CONTROL_PLANE_RUN_ROOT || '/srv/symphony/worker-state/runs';
const GITHUB_STATE_ROOT = ENV.CONTROL_PLANE_GITHUB_ROOT || DEFAULT_GITHUB_ROOT;
const STRATEGY_NODE_MANIFEST_ROOT =
  ENV.CONTROL_PLANE_STRATEGY_NODE_MANIFEST_ROOT ||
  '/srv/symphony/control-repo/deploy/strategy_nodes/betting_arbitrage';
const STRATEGY_NODE_REQUEST_ROOT =
  ENV.CONTROL_PLANE_STRATEGY_NODE_REQUEST_ROOT ||
  '/srv/symphony/worker-state/strategy-node-requests';
const TRADING_HOST_REGISTRY_PATH =
  ENV.CONTROL_PLANE_TRADING_HOST_REGISTRY_PATH ||
  '/srv/symphony/worker-state/trading-hosts/hosts.json';
const TRADING_NODE_REGISTRY_PATH =
  ENV.CONTROL_PLANE_TRADING_NODE_REGISTRY_PATH ||
  '/srv/symphony/worker-state/trading-nodes/registry.json';
const TRADING_NODE_DISCOVERY_ROOT =
  ENV.CONTROL_PLANE_TRADING_NODE_DISCOVERY_ROOT ||
  '/srv/symphony/worker-state/trading-nodes/discovery';
const TRADING_NODE_ROOT_DIR =
  ENV.CONTROL_PLANE_TRADING_NODE_ROOT ||
  '/opt/cloudbet/strategy-nodes';
const TRADING_LOCAL_HOST_ID = ENV.CONTROL_PLANE_TRADING_LOCAL_HOST_ID || 'local-ec2';
const TRADING_LOCAL_HOST_NAME = ENV.CONTROL_PLANE_TRADING_LOCAL_HOST_NAME || 'EC2 trading host';
const CONTROL_SETTINGS_PATH = ENV.CONTROL_PLANE_SETTINGS_PATH || '/srv/symphony/worker-state/control-plane/settings.json';
const STATIC_DIR = path.dirname(fileURLToPath(import.meta.url));
const STATIC_DIST_DIR = path.join(STATIC_DIR, 'dist');
const STATIC_ROOT = fs.existsSync(path.join(STATIC_DIST_DIR, 'index.html')) ? STATIC_DIST_DIR : STATIC_DIR;
const CONTROL_REPO_ROOT = ENV.CONTROL_REPO_ROOT || path.resolve(STATIC_DIR, '..', '..', '..');
const SYMPHONY_STATE_URL = `http://127.0.0.1:${SYMPHONY_PORT}/api/v1/state`;
const RECONCILE_INTERVAL_MS = 30_000;
const EVENT_STREAM_INTERVAL_MS = 2_000;
const GITHUB_DISCOVERY_INTERVAL_MS = 10_000;
const GITHUB_STEP_POLL_INTERVAL_MS = 5_000;
const GITHUB_OBSERVER_POLL_INTERVAL_MS = 3_000;
const GITHUB_OBSERVER_GRACE_MS = 20_000;
const HANDOFF_STATES = ['Needs Human', 'Manual Action', 'Awaiting Credentials', 'Ready to Resume'];
const ACTIVE_EXECUTION_STATES = ['Todo', 'In Progress', 'Ready to Resume', 'Rework', 'Merging'];
const STALLED_EXECUTION_STATES = ['In Progress', 'Rework'];
const CONTROL_MARKER = 'control-plane:blocker';
const STALL_THRESHOLD_MS = 15 * 60 * 1000;
const AGENT_SECRET_ID = ENV.AGENT_SECRET_ID || 'cloudbet-market-maker/credentials';
const WORKER_PROVIDER_PROFILE_SECRET_KEY = ENV.WORKER_PROVIDER_PROFILE_SECRET_KEY || 'WORKER_PROVIDER_PROFILES_JSON';
const GITHUB_WEBHOOK_SECRET_KEY = ENV.GITHUB_WEBHOOK_SECRET_KEY || 'GITHUB_WEBHOOK_SECRET';
const CONTROL_PLANE_PUBLIC_BASE_URL_KEY = 'CONTROL_PLANE_PUBLIC_BASE_URL';
const ANTIGRAVITY_GOOGLE_CLIENT_ID_KEY = 'ANTIGRAVITY_GOOGLE_CLIENT_ID';
const ANTIGRAVITY_GOOGLE_CLIENT_SECRET_KEY = 'ANTIGRAVITY_GOOGLE_CLIENT_SECRET';
const ANTIGRAVITY_GOOGLE_SCOPES_KEY = 'ANTIGRAVITY_GOOGLE_SCOPES';
const OPENROUTER_SECRET_KEYS = ['OPEN_ROUTER_API_KEY', 'OPENROUTER_API_KEY'];
const ANTIGRAVITY_DEFAULT_SCOPES = [
  'https://www.googleapis.com/auth/cloud-platform',
  'https://www.googleapis.com/auth/userinfo.email',
  'https://www.googleapis.com/auth/userinfo.profile',
  'https://www.googleapis.com/auth/cclog',
  'https://www.googleapis.com/auth/experimentsandconfigs'
];
const ANTIGRAVITY_GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth';
const ANTIGRAVITY_GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token';
const ANTIGRAVITY_GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v2/userinfo';
const ANTIGRAVITY_LOAD_CODE_ASSIST_ENDPOINTS = [
  'https://cloudcode-pa.googleapis.com',
  'https://daily-cloudcode-pa.googleapis.com'
];
const ANTIGRAVITY_ONBOARD_ENDPOINTS = [
  'https://daily-cloudcode-pa.googleapis.com',
  'https://cloudcode-pa.googleapis.com'
];
const ANTIGRAVITY_CLIENT_VERSION = '1.107.0';
const ANTIGRAVITY_REQUEST_TIMEOUT_MS = 30_000;
const SUDO_BIN = '/usr/bin/sudo';
const GITHUB_REPO_SLUG = (ENV.GITHUB_REPO || '').trim();
const RUNNER_SERVICE_NAME =
  ENV.GITHUB_RUNNER_SERVICE_NAME || 'actions.runner.antonga23-cloudbet-market-maker.EC2-Runner.service';
const RUNNER_DIAG_DIR = ENV.GITHUB_RUNNER_DIAG_DIR || '/home/ubuntu/actions-runner/_diag';
const TRADING_NODE_STALE_HEARTBEAT_MS = Number(
  ENV.CONTROL_PLANE_TRADING_NODE_STALE_HEARTBEAT_MS || DEFAULT_STALE_HEARTBEAT_MS,
);

let latestOverview = {
  generatedAt: new Date().toISOString(),
  symphony: null,
  host: {},
  workers: [],
  tradingNodes: {
    hosts: [],
    nodes: [],
    summary: {},
    errors: []
  },
  providers: {},
  issues: [],
  runs: [],
  settings: readControlPlaneSettings(CONTROL_SETTINGS_PATH),
  humanInbox: [],
  stalledIssues: [],
  alerts: []
};
let reconcileBusy = false;
const authProcessRegistry = new Map();
const githubObserverRegistry = new Map();
let secretCache = { fetchedAt: 0, value: null };
let lastRawLogCleanupAt = 0;
let lastGitHubDiscoveryAt = 0;
let lastGitHubStepPollAt = 0;

function loadEnvFile(filePath) {
  if (!fs.existsSync(filePath)) {
    return {};
  }

  const result = {};
  const lines = fs.readFileSync(filePath, 'utf8').split(/\r?\n/);
  for (const line of lines) {
    if (!line || line.trim().startsWith('#')) {
      continue;
    }
    const idx = line.indexOf('=');
    if (idx === -1) {
      continue;
    }
    const key = line.slice(0, idx).trim();
    const value = line.slice(idx + 1).trim();
    result[key] = value;
  }
  return result;
}

function readJson(filePath, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch {
    return fallback;
  }
}

function parseJsonString(value, fallback = {}) {
  if (!value) {
    return fallback;
  }
  if (typeof value === 'object') {
    return value;
  }
  try {
    return JSON.parse(value);
  } catch {
    return fallback;
  }
}

function writeJson(filePath, value) {
  const dir = path.dirname(filePath);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = `${filePath}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2));
  fs.renameSync(tmp, filePath);
}

function listStrategyNodeCatalogEntries() {
  if (!fs.existsSync(STRATEGY_NODE_MANIFEST_ROOT)) {
    return [];
  }

  return fs
    .readdirSync(STRATEGY_NODE_MANIFEST_ROOT, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith('.json'))
    .map((entry) => {
      const manifestPath = path.join(STRATEGY_NODE_MANIFEST_ROOT, entry.name);
      const manifest = readJson(manifestPath, {});
      const venues = Array.isArray(manifest.venues)
        ? manifest.venues.map((venue) => ({
            venue: String(venue?.venue || ''),
            clientKey: String(venue?.client_key || venue?.clientKey || ''),
            dataEnabled: venue?.data_enabled !== false && venue?.dataEnabled !== false,
            executionEnabled: venue?.execution_enabled === true || venue?.executionEnabled === true
          }))
        : [];

      return {
        manifestId: path.basename(entry.name, '.json'),
        manifestFile: entry.name,
        manifestPath,
        nodeId: manifest.node_id || null,
        traderId: manifest.trader_id || null,
        validationMode: manifest.validation_mode !== false,
        allowDummyCredentials: manifest.allow_dummy_credentials !== false,
        recommendedWorker: manifest.metadata?.recommended_worker || 'codex-a',
        venues,
        metadata: manifest.metadata || {},
        statusPath: manifest.status_path || null,
        heartbeatPath: manifest.heartbeat_path || null,
        renderedConfigPath: manifest.rendered_config_path || null,
        requirements: describeStrategyNodeRequirements(manifest),
        operatorFlow: describeStrategyNodeOperatorFlow(manifest)
      };
    })
    .sort((a, b) => String(a.manifestFile).localeCompare(String(b.manifestFile)));
}

function describeStrategyNodeRequirements(manifest) {
  const requiredEnvKeys = new Set();
  const dummyCredentialKeys = new Set();
  const liveNotes = [];
  const validationNotes = [];
  for (const venue of manifest.venues || []) {
    const venueName = String(venue?.venue || '').toUpperCase();
    if (venueName === 'SXBET') {
      requiredEnvKeys.add('SXBET_API_KEY');
      requiredEnvKeys.add('SXBET_PRIVATE_KEY');
      requiredEnvKeys.add('SXBET_WALLET_ADDRESS');
      dummyCredentialKeys.add('SXBET_API_KEY');
      dummyCredentialKeys.add('SXBET_PRIVATE_KEY');
      dummyCredentialKeys.add('SXBET_WALLET_ADDRESS');
      liveNotes.push('SX.bet live execution needs API key, private key, and wallet address.');
      validationNotes.push('SX.bet validation mode can run with deterministic dummy SXBET credentials.');
    } else if (venueName === 'POLYMARKET') {
      requiredEnvKeys.add('POLYMARKET_API_KEY');
      requiredEnvKeys.add('POLYMARKET_API_SECRET');
      requiredEnvKeys.add('POLYMARKET_PASSPHRASE');
      requiredEnvKeys.add('POLYMARKET_PRIVATE_KEY');
      requiredEnvKeys.add('POLYMARKET_FUNDER');
      dummyCredentialKeys.add('POLYMARKET_API_KEY');
      dummyCredentialKeys.add('POLYMARKET_API_SECRET');
      dummyCredentialKeys.add('POLYMARKET_PASSPHRASE');
      dummyCredentialKeys.add('POLYMARKET_PRIVATE_KEY');
      dummyCredentialKeys.add('POLYMARKET_FUNDER');
      liveNotes.push('Polymarket live execution needs API key, API secret, passphrase, private key, and funder.');
      validationNotes.push('Polymarket validation mode can run with deterministic dummy Polymarket credentials.');
    }
  }

  return {
    requiredEnvKeys: [...requiredEnvKeys].sort(),
    dummyCredentialKeys: [...dummyCredentialKeys].sort(),
    hostPrereqs: ['docker', 'python3', 'jq', 'aws cli'],
    validationNotes,
    liveNotes,
    deploymentNotes: [
      'Store STRATEGY_NODE_ENV_FILE as a secret payload, not a committed file.',
      'Use a dedicated GHCR PAT for STRATEGY_NODE_GHCR_TOKEN when the image registry is private.'
    ],
    deploymentSecrets: [
      'STRATEGY_NODE_HOST',
      'STRATEGY_NODE_SSH_USER',
      'STRATEGY_NODE_SSH_KEY',
      'STRATEGY_NODE_ENV_FILE',
      'STRATEGY_NODE_GHCR_USERNAME',
      'STRATEGY_NODE_GHCR_TOKEN'
    ],
    workerAuthSecret: 'CODEX_WORKER_AUTH_<WORKER>_B64',
    workerAuthInstallFlow: [
      './scripts/symphony/capture_worker_auth.sh codex-a',
      './scripts/symphony/install_worker_auths.sh'
    ],
    workerAuthPurpose:
      'Only required when the control plane starts a remote Codex worker on EC2. GitHub Actions SSH deploys use STRATEGY_NODE_* secrets only.'
  };
}

function describeStrategyNodeOperatorFlow(manifest) {
  const worker = manifest.metadata?.recommended_worker || 'codex-a';
  return {
    recommendedWorker: worker,
    localAuthCommand: `./scripts/symphony/capture_worker_auth.sh ${worker}`,
    installCommand: './scripts/symphony/install_worker_auths.sh',
    startCommandTemplate: `ssh -i ./ec2-dev-betting-project.pem ubuntu@13.51.235.85 \
  'chmod +x /tmp/deploy_betting_strategy_node.sh && \
  /tmp/deploy_betting_strategy_node.sh \
    --manifest /tmp/strategy-node-manifest.json \
    --image ghcr.io/antonga23/cloudbet-market-maker/betting-arbitrage-node:<tag> \
    --name betting-arbitrage-node \
    --env-file /tmp/strategy-node.env \
    --registry-user <ghcr-username> \
    --registry-token-file /tmp/strategy-node-ghcr-token'`,
    monitorCommandTemplate: `./scripts/deploy/strategy_nodes/wait_for_strategy_node_status.sh \
  --status-file /opt/cloudbet/strategy-nodes/betting-arbitrage-node/status.json \
  --timeout-seconds 600 \
  --success-status running,completed,validated,built`,
    catalogPath: '/control/api/deployments/catalog',
    requestPath: '/control/api/deployments/requests',
    requestListPath: '/control/api/deployments/requests',
    monitorPath: '/control/api/deployments/requests?limit=40'
  };
}

function listStrategyNodeRequests(limit = 100) {
  if (!fs.existsSync(STRATEGY_NODE_REQUEST_ROOT)) {
    return [];
  }

  return fs
    .readdirSync(STRATEGY_NODE_REQUEST_ROOT, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith('.json'))
    .map((entry) => readJson(path.join(STRATEGY_NODE_REQUEST_ROOT, entry.name), null))
    .filter(Boolean)
    .sort((a, b) => String(b.requestedAt || '').localeCompare(String(a.requestedAt || '')))
    .slice(0, limit);
}

function createStrategyNodeRequest(payload) {
  const manifestFile = String(payload?.manifestFile || '').trim();
  if (!manifestFile) {
    throw new Error('manifestFile is required');
  }

  const manifestPath = path.join(STRATEGY_NODE_MANIFEST_ROOT, path.basename(manifestFile));
  if (!fs.existsSync(manifestPath)) {
    throw new Error(`Unknown manifest: ${manifestFile}`);
  }

  const manifest = readJson(manifestPath, {});
  const requestId = `deploy-${Date.now()}-${randomBytes(4).toString('hex')}`;
  const request = {
    id: requestId,
    requestedAt: new Date().toISOString(),
    manifestFile: path.basename(manifestFile),
    manifestPath,
    nodeId: manifest.node_id || null,
    traderId: manifest.trader_id || null,
    rolloutMode: String(payload?.rolloutMode || 'validate_only'),
    imageRef: payload?.imageRef ? String(payload.imageRef) : null,
    requestedBy: payload?.requestedBy ? String(payload.requestedBy) : 'control-plane',
    workerName: payload?.workerName ? String(payload.workerName) : manifest.metadata?.recommended_worker || 'codex-a',
    notes: payload?.notes ? String(payload.notes) : '',
    target: payload?.target ? String(payload.target) : 'production',
    status: 'queued',
    metadata: payload?.metadata && typeof payload.metadata === 'object' ? payload.metadata : {}
  };

  writeJson(path.join(STRATEGY_NODE_REQUEST_ROOT, `${requestId}.json`), request);
  return request;
}

function getLocalTradingHost() {
  return defaultLocalHostRecord({
    hostId: TRADING_LOCAL_HOST_ID,
    rootDir: TRADING_NODE_ROOT_DIR,
    displayName: TRADING_LOCAL_HOST_NAME,
  });
}

function loadTradingNodeState() {
  const localHost = getLocalTradingHost();
  const hostRegistry = readTradingHostRegistry(TRADING_HOST_REGISTRY_PATH, localHost);
  if (!fs.existsSync(TRADING_HOST_REGISTRY_PATH)) {
    try {
      writeTradingHostRegistry(TRADING_HOST_REGISTRY_PATH, hostRegistry);
    } catch {
      // non-fatal on local dev machines without /srv permissions
    }
  }

  const nodeRegistry = readTradingNodeRegistry(TRADING_NODE_REGISTRY_PATH);
  const discovery = discoverLocalTradingNodes({
    hostId: localHost.hostId,
    rootDir: localHost.rootDir,
    staleHeartbeatMs: TRADING_NODE_STALE_HEARTBEAT_MS,
  });
  try {
    writeTradingNodeDiscoverySnapshot(
      path.join(TRADING_NODE_DISCOVERY_ROOT, `${localHost.hostId}.json`),
      discovery,
    );
  } catch {
    // non-fatal on local dev machines without /srv permissions
  }

  const nodes = buildEffectiveTradingNodes({
    hosts: hostRegistry.hosts,
    registry: nodeRegistry,
    discoveries: [discovery],
  });

  return {
    hosts: hostRegistry.hosts,
    hostRegistry,
    nodeRegistry,
    registry: nodeRegistry,
    discoveries: [discovery],
    discovery,
    nodes,
  };
}

function summarizeTradingNodesSnapshot(snapshot) {
  const summary = {
    total: snapshot.nodes.length,
    running: 0,
    managed: 0,
    discoveredUnmanaged: 0,
    staleHeartbeat: 0,
    missingContainer: 0,
  };
  for (const node of snapshot.nodes) {
    if (node.status === 'running') {
      summary.running += 1;
    }
    if (node.stateClass === 'managed') {
      summary.managed += 1;
    }
    if (node.stateClass === 'discovered-unmanaged') {
      summary.discoveredUnmanaged += 1;
    }
    if (node.stateClass === 'stale-heartbeat') {
      summary.staleHeartbeat += 1;
    }
    if (node.stateClass === 'missing-container') {
      summary.missingContainer += 1;
    }
  }
  return summary;
}

function upsertTradingNodeRegistryEntry(snapshot, nextEntry) {
  const registry = snapshot.registry || { nodes: [] };
  const nodes = Array.isArray(registry.nodes) ? registry.nodes.map((node) => ({ ...node })) : [];
  const index = nodes.findIndex(
    (node) =>
      node.nodeId === nextEntry.nodeId ||
      (node.containerName && node.containerName === nextEntry.containerName),
  );
  const base = index >= 0 ? nodes[index] : null;
  const merged = {
    ...(base || {}),
    ...nextEntry,
    updatedAt: new Date().toISOString(),
    createdAt: base?.createdAt || new Date().toISOString(),
  };
  if (index >= 0) {
    nodes[index] = merged;
  } else {
    nodes.push(merged);
  }
  writeTradingNodeRegistry(TRADING_NODE_REGISTRY_PATH, { nodes });
  return merged;
}

function findTradingNode(snapshot, nodeId) {
  return snapshot.nodes.find((node) => node.nodeId === nodeId || node.containerName === nodeId) || null;
}

function ensureNodeDir(node) {
  const nodeDir = node.discovery?.paths?.nodeDir || path.join(TRADING_NODE_ROOT_DIR, node.containerName);
  fs.mkdirSync(nodeDir, { recursive: true });
  return nodeDir;
}

function parseNodeOverridePayload(payload = {}) {
  const override = {};
  const source = payload.override || payload.overrides || payload.configOverrides || {};
  if (source && typeof source === 'object') {
    const logLevel = source.log_level ?? source.logLevel;
    if (logLevel !== undefined) {
      override.log_level = logLevel;
    }
    const validationMode = source.validation_mode ?? source.validationMode;
    if (validationMode !== undefined) {
      override.validation_mode = validationMode;
    }
    const executionEnabled = source.execution_enabled ?? source.executionEnabled;
    if (executionEnabled !== undefined) {
      override.execution_enabled = executionEnabled;
    }
    if (source.strategy && typeof source.strategy === 'object') {
      override.strategy = source.strategy;
    }
    if (source.venues && Array.isArray(source.venues)) {
      override.venues = source.venues;
    }
    if (source.metadata && typeof source.metadata === 'object') {
      override.metadata = source.metadata;
    }
    const allowDummyCredentials =
      source.allow_dummy_credentials ?? source.allowDummyCredentials;
    if (allowDummyCredentials !== undefined) {
      override.allow_dummy_credentials = allowDummyCredentials;
    }
  }
  const imageRef = payload.imageRef ? String(payload.imageRef).trim() : '';
  return { override, imageRef };
}

function isMissingDockerContainerError(result) {
  const text = String(result?.stderr || result?.stdout || '').toLowerCase();
  return text.includes('no such container') || text.includes('cannot remove container');
}

function renderTradingNodeConfigPreview(manifest) {
  const manifestDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cp-node-render-'));
  const manifestPath = path.join(manifestDir, 'manifest.json');
  fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
  const result = spawnSync(
    'python3',
    [
      '-m',
      'nautilus_trader.live.strategy_nodes.betting_arbitrage',
      'render-node-config',
      '--manifest',
      manifestPath,
    ],
    {
      encoding: 'utf8',
      cwd: CONTROL_REPO_ROOT,
    },
  );
  fs.rmSync(manifestDir, { recursive: true, force: true });
  if (result.status !== 0) {
    return {
      ok: false,
      error: (result.stderr || result.stdout || 'render-node-config failed').trim(),
    };
  }
  return {
    ok: true,
    renderedConfig: parseJsonString(result.stdout, null),
    raw: result.stdout,
  };
}

function writeNodeStatusFile(node, payload) {
  const statusPath =
    node.discovery?.paths?.statusPath ||
    path.join(ensureNodeDir(node), 'status.json');
  fs.writeFileSync(statusPath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
}

function materializeTradingNodeEnvFile(node) {
  const nodeDir = ensureNodeDir(node);
  const envDir = path.join(nodeDir, 'runtime');
  fs.mkdirSync(envDir, { recursive: true });
  const envPath = path.join(envDir, `deploy-${Date.now()}.env`);
  const keys = [
    'SXBET_API_KEY',
    'SXBET_PRIVATE_KEY',
    'SXBET_WALLET_ADDRESS',
    'POLYMARKET_API_KEY',
    'POLYMARKET_API_SECRET',
    'POLYMARKET_PASSPHRASE',
    'POLYMARKET_PRIVATE_KEY',
    'POLYMARKET_PK',
    'POLYMARKET_FUNDER',
    'SYMPHONY_WORKSPACE_ROOT',
    'SOURCE_REPO_URL',
    'SYMPHONY_PORT',
    'CONTROL_PLANE_PORT',
    'CONTROL_PLANE_WORKER_CONFIG',
    'AGENT_SECRET_ID',
    'AWS_REGION',
    'AWS_DEFAULT_REGION'
  ];
  const lines = [];
  for (const key of keys) {
    const value = ENV[key] || process.env[key];
    if (value) {
      lines.push(`${key}=${value}`);
    }
  }
  fs.writeFileSync(envPath, `${lines.join('\n')}\n`, 'utf8');
  return envPath;
}

function runDeployScriptForNode(node, mergedManifest, imageRef) {
  const nodeDir = ensureNodeDir(node);
  const requestedManifestPath = path.join(nodeDir, 'manifest.requested.json');
  fs.writeFileSync(requestedManifestPath, `${JSON.stringify(mergedManifest, null, 2)}\n`, 'utf8');

  const args = [
    path.join(CONTROL_REPO_ROOT, 'scripts/deploy/strategy_nodes/deploy_betting_strategy_node.sh'),
    '--manifest',
    requestedManifestPath,
    '--image',
    imageRef,
    '--name',
    node.containerName,
    '--root',
    TRADING_NODE_ROOT_DIR,
  ];
  const envFile = node.envFile || node.discovery?.release?.envFile || materializeTradingNodeEnvFile(node);
  node.envFile = envFile;
  if (envFile) {
    args.push('--env-file', envFile);
  }
  const secretJson = getSecretJson();
  const registryUser = String(secretJson.STRATEGY_NODE_GHCR_USERNAME || '').trim();
  const registryToken = String(secretJson.STRATEGY_NODE_GHCR_TOKEN || '').trim();
  let tokenFile = '';
  try {
    if (registryUser && registryToken) {
      tokenFile = path.join(os.tmpdir(), `cp-ghcr-${Date.now()}-${randomBytes(4).toString('hex')}`);
      fs.writeFileSync(tokenFile, registryToken, 'utf8');
      args.push('--registry-user', registryUser, '--registry-token-file', tokenFile);
    }
    return spawnSync('bash', args, {
      encoding: 'utf8',
      cwd: CONTROL_REPO_ROOT,
    });
  } finally {
    if (tokenFile) {
      fs.rmSync(tokenFile, { force: true });
    }
  }
}

function decodeJwtPayload(token) {
  if (!token || typeof token !== 'string') {
    return {};
  }
  const parts = token.split('.');
  if (parts.length < 2) {
    return {};
  }
  const payload = parts[1];
  const padded = payload + '='.repeat((4 - (payload.length % 4 || 4)) % 4);
  try {
    return JSON.parse(Buffer.from(padded, 'base64url').toString('utf8'));
  } catch {
    return {};
  }
}

function getAuthEmailFromJson(authJson) {
  const tokens = authJson?.tokens || {};
  const payload =
    decodeJwtPayload(tokens.id_token) ||
    decodeJwtPayload(tokens.access_token) ||
    {};
  return (
    payload.email ||
    payload['https://api.openai.com/profile']?.email ||
    ''
  );
}

function sudoFileExists(filePath) {
  const result = spawnSync(SUDO_BIN, ['test', '-f', filePath], { encoding: 'utf8' });
  return result.status === 0;
}

function sudoReadFile(filePath) {
  const result = spawnSync(SUDO_BIN, ['cat', filePath], { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 });
  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout || `Failed to read ${filePath} via sudo`);
  }
  return result.stdout;
}

function readJsonViaSudo(filePath, fallback = null) {
  try {
    return JSON.parse(sudoReadFile(filePath));
  } catch {
    return fallback;
  }
}

function getWorkerByName(workerName) {
  return loadWorkerDefinitions().workers.find((worker) => worker.name === workerName) || null;
}

function normalizeStringList(value) {
  if (Array.isArray(value)) {
    return [...new Set(value.map((item) => String(item || '').trim()).filter(Boolean))];
  }
  if (typeof value === 'string') {
    return [
      ...new Set(
        value
          .split(/[\n,]+/)
          .map((item) => item.trim())
          .filter(Boolean)
      )
    ];
  }
  return [];
}

function normalizeWorkerProviderProfile(profile = {}) {
  const subscriptionTier = String(profile.subscriptionTier || '').trim();
  const runtimeModel = String(profile.runtimeModel || '').trim();
  const availableModels = normalizeStringList(profile.availableModels || []);
  const notes = String(profile.notes || '').trim();
  return {
    subscriptionTier,
    runtimeModel,
    availableModels,
    notes,
    effectiveRuntimeModel: runtimeModel,
    selectionMode: runtimeModel ? 'explicit-model' : 'provider-default'
  };
}

function mergeWorkerProviderProfiles(baseProfiles = {}, overrideProfiles = {}) {
  const providers = ['codex', 'antigravity'];
  const merged = {};
  for (const providerId of providers) {
    const hasOverride = Object.prototype.hasOwnProperty.call(overrideProfiles, providerId);
    const hasBase = Object.prototype.hasOwnProperty.call(baseProfiles, providerId);
    merged[providerId] = {
      ...normalizeWorkerProviderProfile({
        ...(baseProfiles[providerId] || {}),
        ...(overrideProfiles[providerId] || {})
      }),
      source: hasOverride ? 'aws-secrets' : hasBase ? 'repo' : 'unset'
    };
  }
  return merged;
}

function getCodexSecretKey(worker) {
  return worker.secretKey || `CODEX_WORKER_AUTH_${worker.name.replace(/-/g, '_').toUpperCase()}_B64`;
}

function getOpenRouterSecretKey(secretJson = {}) {
  return OPENROUTER_SECRET_KEYS.find((key) => secretJson[key]) || OPENROUTER_SECRET_KEYS[0];
}

function parseScopeString(rawValue) {
  if (!rawValue || !String(rawValue).trim()) {
    return [...ANTIGRAVITY_DEFAULT_SCOPES];
  }
  const trimmed = String(rawValue).trim();
  try {
    const parsed = JSON.parse(trimmed);
    if (Array.isArray(parsed) && parsed.every((item) => typeof item === 'string' && item.trim())) {
      return [...new Set(parsed.map((item) => item.trim()))];
    }
  } catch {
    // ignore and fall back to whitespace/comma parsing
  }
  return [
    ...new Set(
      trimmed
        .split(/[\s,]+/)
        .map((item) => item.trim())
        .filter(Boolean)
    )
  ];
}

function getAntigravityScopes(secretJson = {}) {
  return parseScopeString(secretJson[ANTIGRAVITY_GOOGLE_SCOPES_KEY] || ENV[ANTIGRAVITY_GOOGLE_SCOPES_KEY] || '');
}

function getConfiguredPublicBaseUrl(secretJson = {}, req = null) {
  const explicit = secretJson[CONTROL_PLANE_PUBLIC_BASE_URL_KEY] || ENV[CONTROL_PLANE_PUBLIC_BASE_URL_KEY] || '';
  if (explicit) {
    return explicit.trim().replace(/\/+$/, '');
  }
  if (!req) {
    return '';
  }
  const protocol = req.headers['x-forwarded-proto'] || 'http';
  const host = req.headers['x-forwarded-host'] || req.headers.host;
  return `${protocol}://${host}`.replace(/\/+$/, '');
}

function getAntigravityAuthSecretKey(worker) {
  return (
    worker.antigravityAuthSecretKey ||
    `ANTIGRAVITY_WORKER_AUTH_${worker.name.replace(/-/g, '_').toUpperCase()}_JSON_B64`
  );
}

function getAntigravityAuthDir(worker) {
  return `/home/${worker.user}/.config/antigravity-cli`;
}

function getAntigravityAuthPath(worker) {
  return path.join(getAntigravityAuthDir(worker), 'auth.json');
}

function getAntigravityClientPlatform() {
  if (process.platform === 'darwin') {
    return process.arch === 'arm64' ? 2 : 1;
  }
  if (process.platform === 'linux') {
    return process.arch === 'arm64' ? 4 : 3;
  }
  if (process.platform === 'win32') {
    return 5;
  }
  return 0;
}

function getAntigravityClientMetadata() {
  return {
    ideType: 9,
    platform: getAntigravityClientPlatform(),
    pluginType: 2
  };
}

function getAntigravityHeaders() {
  return {
    'User-Agent': `antigravity/${ANTIGRAVITY_CLIENT_VERSION} ${process.platform}/${process.arch}`,
    'Content-Type': 'application/json',
    'X-Client-Name': 'antigravity',
    'X-Client-Version': ANTIGRAVITY_CLIENT_VERSION,
    'x-goog-api-client': 'gl-node/18.18.2 fire/0.8.6 grpc/1.10.x'
  };
}

function getAntigravityConfig(secretJson = {}, req = null) {
  const clientId = (secretJson[ANTIGRAVITY_GOOGLE_CLIENT_ID_KEY] || ENV[ANTIGRAVITY_GOOGLE_CLIENT_ID_KEY] || '').trim();
  const clientSecret = (secretJson[ANTIGRAVITY_GOOGLE_CLIENT_SECRET_KEY] || ENV[ANTIGRAVITY_GOOGLE_CLIENT_SECRET_KEY] || '').trim();
  const publicBaseUrl = getConfiguredPublicBaseUrl(secretJson, req);
  const scopes = getAntigravityScopes(secretJson);
  const warnings = [];
  let callbackUrl = '';
  let parsedBaseUrl = null;

  if (!publicBaseUrl) {
    warnings.push(`Set ${CONTROL_PLANE_PUBLIC_BASE_URL_KEY} to a stable HTTPS dashboard URL before using Google OAuth.`);
  } else {
    try {
      parsedBaseUrl = new URL(publicBaseUrl);
      callbackUrl = new URL('/control/api/providers/antigravity/oauth/callback', parsedBaseUrl).toString();
      if (parsedBaseUrl.protocol !== 'https:') {
        warnings.push('Google web OAuth redirect URIs must use HTTPS. The current public base URL is not HTTPS.');
      }
      if (['localhost', '127.0.0.1'].includes(parsedBaseUrl.hostname) || isIP(parsedBaseUrl.hostname)) {
        warnings.push('Use a stable DNS hostname for Google OAuth. Raw EC2 IP and localhost callbacks are not suitable for this remote workflow.');
      }
    } catch {
      warnings.push(`The configured ${CONTROL_PLANE_PUBLIC_BASE_URL_KEY} is not a valid URL.`);
    }
  }

  if (!clientId) {
    warnings.push(`Set ${ANTIGRAVITY_GOOGLE_CLIENT_ID_KEY} in AWS Secrets Manager.`);
  }
  if (!clientSecret) {
    warnings.push(`Set ${ANTIGRAVITY_GOOGLE_CLIENT_SECRET_KEY} in AWS Secrets Manager.`);
  }

  return {
    clientId,
    clientSecret,
    scopes,
    publicBaseUrl,
    callbackUrl,
    warnings,
    ready: Boolean(clientId && clientSecret && callbackUrl && warnings.length === 0),
    secretsConfigured: {
      clientId: Boolean(clientId),
      clientSecret: Boolean(clientSecret),
      publicBaseUrl: Boolean(publicBaseUrl)
    }
  };
}

function getAuthSessionPath(providerId, workerName) {
  return path.join(AUTH_SESSION_DIR, `${providerId}-${workerName}.json`);
}

function parseFirstUrl(text) {
  const match = text.match(/https?:\/\/\S+/);
  return match ? match[0] : null;
}

function parseFirstDeviceCode(text) {
  const match = text.match(/\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b/);
  return match ? match[0] : null;
}

function stripAnsi(text) {
  return text.replace(/\u001B\[[0-9;]*[A-Za-z]/g, '');
}

function commandExists(command) {
  const result = spawnSync('bash', ['-lc', `command -v ${command}`], { encoding: 'utf8' });
  return result.status === 0;
}

function runAwsCli(args, input = null) {
  const result = spawnSync('aws', args, {
    encoding: 'utf8',
    input,
    env: { ...process.env, ...ENV }
  });
  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout || `aws ${args.join(' ')} failed`);
  }
  return result.stdout.trim();
}

function getSecretJson() {
  const now = Date.now();
  if (secretCache.value && now - secretCache.fetchedAt < 15_000) {
    return secretCache.value;
  }
  const raw = runAwsCli([
    'secretsmanager',
    'get-secret-value',
    '--secret-id',
    AGENT_SECRET_ID,
    '--query',
    'SecretString',
    '--output',
    'text'
  ]);
  const parsed = JSON.parse(raw || '{}');
  secretCache = { fetchedAt: now, value: parsed };
  return parsed;
}

function getWorkerProviderProfileOverrides(secretJson = getSecretJson()) {
  const parsed = parseJsonString(secretJson[WORKER_PROVIDER_PROFILE_SECRET_KEY], {});
  return parsed && typeof parsed === 'object' ? parsed : {};
}

function saveWorkerProviderProfile(workerName, providerId, payload = {}) {
  if (!['codex', 'antigravity'].includes(providerId)) {
    throw new Error(`Unsupported provider profile: ${providerId}`);
  }

  const secretJson = getSecretJson();
  const currentProfiles = getWorkerProviderProfileOverrides(secretJson);
  const nextWorkerProfiles = {
    ...(currentProfiles[workerName] || {}),
    [providerId]: {
      subscriptionTier: String(payload.subscriptionTier || '').trim(),
      runtimeModel: String(payload.runtimeModel || '').trim(),
      availableModels: normalizeStringList(payload.availableModels || []),
      notes: String(payload.notes || '').trim()
    }
  };
  const nextProfiles = {
    ...currentProfiles,
    [workerName]: nextWorkerProfiles
  };

  upsertSecretEntries({
    [WORKER_PROVIDER_PROFILE_SECRET_KEY]: JSON.stringify(nextProfiles)
  });

  return {
    worker: workerName,
    providerId,
    profile: {
      ...normalizeWorkerProviderProfile(nextWorkerProfiles[providerId]),
      source: 'aws-secrets'
    },
    secretKey: WORKER_PROVIDER_PROFILE_SECRET_KEY
  };
}

function upsertSecretEntries(entries) {
  const current = getSecretJson();
  const updated = { ...current, ...entries };
  runAwsCli([
    'secretsmanager',
    'put-secret-value',
    '--secret-id',
    AGENT_SECRET_ID,
    '--secret-string',
    JSON.stringify(updated)
  ]);
  secretCache = { fetchedAt: Date.now(), value: updated };
  return updated;
}

function getGitHubWebhookConfig(secretJson = getSecretJson(), req = null) {
  const repo = parseRepoSlug();
  const secret = String(secretJson[GITHUB_WEBHOOK_SECRET_KEY] || ENV[GITHUB_WEBHOOK_SECRET_KEY] || '').trim();
  const derivedBaseUrl =
    getConfiguredPublicBaseUrl(secretJson, req) ||
    (secretJson.EC2_HOST || ENV.EC2_HOST ? `https://${String(secretJson.EC2_HOST || ENV.EC2_HOST).trim()}` : '');
  const publicBaseUrl = derivedBaseUrl;
  let webhookUrl = '';
  let warnings = [];
  if (!repo) {
    warnings.push('Set GITHUB_REPO in the control-plane environment to enable GitHub webhook monitoring.');
  }
  if (!secret) {
    warnings.push(`Set ${GITHUB_WEBHOOK_SECRET_KEY} or let the control plane generate one before configuring the webhook.`);
  }
  if (!publicBaseUrl) {
    warnings.push('The control plane needs a public HTTPS base URL before GitHub can deliver webhook events.');
  } else {
    try {
      const parsed = new URL(publicBaseUrl);
      webhookUrl = new URL('/control/api/github/webhooks', parsed).toString();
      if (parsed.protocol !== 'https:') {
        warnings.push('GitHub webhooks must use HTTPS.');
      }
    } catch {
      warnings.push('The configured public base URL is not a valid URL.');
    }
  }
  return {
    repo,
    secret,
    secretConfigured: Boolean(secret),
    webhookUrl,
    warnings,
    ready: Boolean(repo && secret && webhookUrl && warnings.length === 0)
  };
}

function ensureGitHubWebhookSecret() {
  const secretJson = getSecretJson();
  const current = String(secretJson[GITHUB_WEBHOOK_SECRET_KEY] || '').trim();
  if (current) {
    return current;
  }
  const generated = randomBytes(32).toString('hex');
  upsertSecretEntries({ [GITHUB_WEBHOOK_SECRET_KEY]: generated });
  return generated;
}

function verifyGitHubWebhookSignature(rawBody, providedSignature, secret) {
  if (!secret || !providedSignature || !providedSignature.startsWith('sha256=')) {
    return false;
  }
  const expected = createHmac('sha256', secret).update(rawBody).digest('hex');
  const expectedBuffer = Buffer.from(`sha256=${expected}`, 'utf8');
  const actualBuffer = Buffer.from(providedSignature, 'utf8');
  if (expectedBuffer.length !== actualBuffer.length) {
    return false;
  }
  return timingSafeEqual(expectedBuffer, actualBuffer);
}

function getGitHubJobLogFilePath(jobId, streamId) {
  return path.join(GITHUB_STATE_ROOT, 'jobs', String(jobId), 'raw', `${streamId}.log`);
}

function appendGitHubJobStreamChunk(jobId, streamId, label, chunk, patch = {}) {
  const filePath = getGitHubJobLogFilePath(jobId, streamId);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.appendFileSync(filePath, chunk);
  const bytes = fs.statSync(filePath).size;
  return upsertGitHubJobLogStream(jobId, {
    id: streamId,
    label,
    filePath,
    bytes,
    updatedAt: new Date().toISOString(),
    ...patch
  }, GITHUB_STATE_ROOT);
}

function summarizeGitHubJob(job) {
  return {
    ...job,
    active: ['queued', 'in_progress', 'waiting'].includes(job.status),
    stepCounts: {
      total: Array.isArray(job.steps) ? job.steps.length : 0,
      completed: Array.isArray(job.steps)
        ? job.steps.filter((step) => step.status === 'completed' && step.conclusion === 'success').length
        : 0,
      failed: Array.isArray(job.steps)
        ? job.steps.filter((step) => step.status === 'completed' && ['failure', 'timed_out', 'cancelled'].includes(step.conclusion)).length
        : 0,
      inProgress: Array.isArray(job.steps) ? job.steps.filter((step) => step.status === 'in_progress').length : 0
    },
    logStreams: listGitHubJobLogStreams(job.jobId, GITHUB_STATE_ROOT)
  };
}

function readGitHubActionsState(limit = 30) {
  const jobs = listGitHubJobs({ root: GITHUB_STATE_ROOT, limit });
  const activeJobs = listGitHubJobs({ root: GITHUB_STATE_ROOT, activeOnly: true, limit });
  const recentEvents = listGitHubEvents({ root: GITHUB_STATE_ROOT, limit: 60 });
  return {
    repo: parseRepoSlug()?.fullName || '',
    activeJobs: activeJobs.map((job) => summarizeGitHubJob(job)),
    recentJobs: jobs.map((job) => summarizeGitHubJob(job)),
    recentEvents
  };
}

function installFileAsUser(content, destinationPath, user, mode = '600') {
  const tmpFile = path.join('/tmp', `control-plane-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  fs.writeFileSync(tmpFile, content);
  try {
    const result = spawnSync(SUDO_BIN, ['install', '-o', user, '-g', user, '-m', mode, tmpFile, destinationPath], {
      encoding: 'utf8'
    });
    if (result.status !== 0) {
      throw new Error(result.stderr || result.stdout || `Failed to install ${destinationPath}`);
    }
  } finally {
    fs.rmSync(tmpFile, { force: true });
  }
}

function ensureUserDir(targetPath, user, mode = '700') {
  const result = spawnSync(SUDO_BIN, ['install', '-d', '-o', user, '-g', user, '-m', mode, targetPath], { encoding: 'utf8' });
  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout || `Failed to create ${targetPath}`);
  }
}

function readAntigravityWorkerAuth(worker) {
  const authPath = getAntigravityAuthPath(worker);
  if (!sudoFileExists(authPath)) {
    return null;
  }
  return readJsonViaSudo(authPath, null);
}

function buildAntigravityCompositeRefreshToken(refreshToken, projectId = '', managedProjectId = '') {
  const base = `${refreshToken || ''}|${projectId || ''}`;
  return managedProjectId ? `${base}|${managedProjectId}` : base;
}

function persistAntigravityWorkerAuth(worker, authRecord) {
  if (!authRecord?.email) {
    throw new Error('Antigravity auth record is missing email');
  }
  if (worker.email && authRecord.email !== worker.email) {
    throw new Error(`Worker ${worker.name} expected ${worker.email} but Antigravity auth is for ${authRecord.email}`);
  }
  if (!authRecord.refreshToken) {
    throw new Error('Antigravity auth record is missing refreshToken');
  }

  const authDir = getAntigravityAuthDir(worker);
  ensureUserDir(authDir, worker.user, '700');
  const serialized = JSON.stringify(authRecord, null, 2);
  installFileAsUser(serialized, getAntigravityAuthPath(worker), worker.user, '600');
  upsertSecretEntries({
    [getAntigravityAuthSecretKey(worker)]: Buffer.from(serialized, 'utf8').toString('base64')
  });

  return {
    authPath: getAntigravityAuthPath(worker),
    secretKey: getAntigravityAuthSecretKey(worker),
    email: authRecord.email,
    projectId: authRecord.projectId || null,
    managedProjectId: authRecord.managedProjectId || null
  };
}

function persistAntigravityWorkerAuthFromDisk(worker) {
  const authRecord = readAntigravityWorkerAuth(worker);
  if (!authRecord) {
    throw new Error(`Missing ${getAntigravityAuthPath(worker)}`);
  }
  return persistAntigravityWorkerAuth(worker, authRecord);
}

function restoreAntigravityWorkerAuth(worker) {
  const secretJson = getSecretJson();
  const authB64 = secretJson[getAntigravityAuthSecretKey(worker)];
  if (!authB64) {
    return { restored: false, secretKey: getAntigravityAuthSecretKey(worker) };
  }
  const authRecord = JSON.parse(Buffer.from(authB64, 'base64').toString('utf8'));
  return {
    restored: true,
    ...persistAntigravityWorkerAuth(worker, authRecord)
  };
}

function summarizeAntigravityAuthRecord(authRecord) {
  if (!authRecord) {
    return null;
  }
  return {
    version: authRecord.version || 1,
    provider: authRecord.provider || 'antigravity',
    email: authRecord.email || '',
    projectId: authRecord.projectId || null,
    managedProjectId: authRecord.managedProjectId || null,
    capturedAt: authRecord.capturedAt || null,
    completionMethod: authRecord.completionMethod || null,
    scopes: Array.isArray(authRecord.scopes) ? authRecord.scopes : []
  };
}

function summarizeAntigravitySession(session) {
  if (!session) {
    return null;
  }
  return {
    providerId: session.providerId,
    worker: session.worker,
    email: session.email || session.authEmail || '',
    redirectUri: session.redirectUri || '',
    authUrl: session.authUrl || '',
    status: session.status || 'unknown',
    startedAt: session.startedAt || null,
    completedAt: session.completedAt || null,
    error: session.error || '',
    projectId: session.projectId || null,
    managedProjectId: session.managedProjectId || null
  };
}

function sendJson(res, statusCode, payload) {
  const body = JSON.stringify(payload, null, 2);
  res.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store'
  });
  res.end(body);
}

function sendText(res, statusCode, body, contentType = 'text/plain; charset=utf-8') {
  res.writeHead(statusCode, {
    'Content-Type': contentType,
    'Cache-Control': 'no-store'
  });
  res.end(body);
}

function contentTypeFor(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  if (ext === '.html') return 'text/html; charset=utf-8';
  if (ext === '.css') return 'text/css; charset=utf-8';
  if (ext === '.js' || ext === '.mjs') return 'application/javascript; charset=utf-8';
  if (ext === '.json') return 'application/json; charset=utf-8';
  if (ext === '.svg') return 'image/svg+xml';
  if (ext === '.png') return 'image/png';
  if (ext === '.ico') return 'image/x-icon';
  if (ext === '.woff2') return 'font/woff2';
  return 'application/octet-stream';
}

function sendStaticFile(res, requestPath) {
  const relativePath = requestPath === '/' ? 'index.html' : requestPath.replace(/^\/+/, '');
  const normalized = path.normalize(relativePath);
  if (normalized.startsWith('..') || path.isAbsolute(normalized)) {
    sendJson(res, 400, { error: 'Invalid static path' });
    return true;
  }
  const filePath = path.join(STATIC_ROOT, normalized);
  if (!filePath.startsWith(STATIC_ROOT) || !fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    return false;
  }
  const cacheControl = normalized.startsWith('assets/') ? 'public, max-age=31536000, immutable' : 'no-store';
  res.writeHead(200, {
    'Content-Type': contentTypeFor(filePath),
    'Cache-Control': cacheControl
  });
  fs.createReadStream(filePath).pipe(res);
  return true;
}

async function readBody(req) {
  let body = '';
  for await (const chunk of req) {
    body += chunk;
  }
  return body;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`HTTP ${response.status} for ${url}: ${text}`);
  }
  return await response.json();
}

async function linearQuery(query, variables = {}) {
  if (!LINEAR_API_KEY) {
    throw new Error('LINEAR_API_KEY is not configured');
  }

  const response = await fetch('https://api.linear.app/graphql', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: LINEAR_API_KEY
    },
    body: JSON.stringify({ query, variables })
  });

  const payload = await response.json();
  if (!response.ok || payload.errors) {
    throw new Error(`Linear query failed: ${JSON.stringify(payload.errors || payload)}`);
  }
  return payload.data;
}

async function githubRequest(url, options = {}) {
  if (!GITHUB_TOKEN) {
    return null;
  }

  const response = await fetch(url, {
    method: options.method || 'GET',
    headers: {
      Accept: 'application/vnd.github+json',
      Authorization: `Bearer ${GITHUB_TOKEN}`,
      'X-GitHub-Api-Version': '2022-11-28',
      ...(options.headers || {})
    },
    body: options.body === undefined ? undefined : options.body
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`GitHub API ${response.status} for ${url}: ${text}`);
  }
  if (response.status === 204) {
    return null;
  }
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    return await response.json();
  }
  return await response.text();
}

async function githubJson(url) {
  try {
    return await githubRequest(url);
  } catch {
    return null;
  }
}

function parseRepoSlug(repoSlug = GITHUB_REPO_SLUG) {
  const [owner, repo] = String(repoSlug || '').trim().split('/');
  if (!owner || !repo) {
    return null;
  }
  return { owner, repo, fullName: `${owner}/${repo}` };
}

function loadWorkerDefinitions() {
  const config = readJson(WORKER_CONFIG_PATH, { workers: [] });
  const profileOverrides = getWorkerProviderProfileOverrides();
  const workers = Array.isArray(config?.workers) ? config.workers : [];
  return {
    maxConcurrentAgents: Number(config?.maxConcurrentAgents || 3),
    workers: workers
      .filter((worker) => worker && worker.enabled !== false)
      .map((worker) => ({
        ...worker,
        providerProfiles: mergeWorkerProviderProfiles(
          worker.providerProfiles || {},
          profileOverrides[worker.name] || {}
        )
      }))
      .sort((a, b) => Number(a.priority || 999) - Number(b.priority || 999))
  };
}

function getWorkerStatePath(name) {
  return path.join(WORKER_STATE_DIR, `${name}.json`);
}

function readWorkers() {
  const { workers, maxConcurrentAgents } = loadWorkerDefinitions();
  const now = Math.floor(Date.now() / 1000);

  return workers.map((worker) => {
    const stateFile = getWorkerStatePath(worker.name);
    const rawState = readJson(stateFile, {});
    const authPath = `/home/${worker.user}/.codex/auth.json`;
    const authPresent = sudoFileExists(authPath);
    const authEmail = authPresent ? getAuthEmailFromJson(readJsonViaSudo(authPath, {})) : '';
    const lockPath = path.join(WORKER_LOCK_DIR, `${worker.name}.lock`);
    const lockPresent = fs.existsSync(lockPath);
    const cooldownUntilEpoch = Number(rawState.cooldownUntilEpoch || 0);
    const cordoned = Boolean(rawState.cordoned);
    let status = rawState.status || 'idle';

    if (!authPresent) {
      status = 'missing_auth';
    } else if (cordoned) {
      status = 'cordoned';
    } else if (cooldownUntilEpoch > now) {
      status = 'cooldown';
    } else if (status === 'busy') {
      status = 'busy';
    } else {
      status = 'idle';
    }

    return {
      ...worker,
      ...rawState,
      status,
      authPresent,
      authEmail,
      effectiveCodexModel: worker.providerProfiles?.codex?.effectiveRuntimeModel || '',
      codexSelectionMode: worker.providerProfiles?.codex?.selectionMode || 'provider-default',
      codexSubscriptionTier: worker.providerProfiles?.codex?.subscriptionTier || '',
      cordoned,
      cooldownUntilEpoch,
      lockPresent,
      availableForNewWork:
        authPresent && !cordoned && cooldownUntilEpoch <= now && status === 'idle',
      stateFile
    };
  }).map((worker) => ({ ...worker, maxConcurrentAgents }));
}

function normalizeDeliveryMode(value) {
  const allowed = ['interrupt_now', 'deliver_after_current_step', 'deliver_when_idle'];
  return allowed.includes(value) ? value : 'deliver_after_current_step';
}

function readControlSettings() {
  return readControlPlaneSettings(CONTROL_SETTINGS_PATH);
}

function saveControlSettings(payload = {}) {
  return saveControlPlaneSettings(
    {
      defaultPromptDeliveryMode: normalizeDeliveryMode(payload.defaultPromptDeliveryMode),
      rawLogRetentionDays: Number(payload.rawLogRetentionDays || 30)
    },
    CONTROL_SETTINGS_PATH
  );
}

function summarizeRun(run) {
  const actions = listOperatorActions(run.runId, { runRoot: RUN_ROOT, limit: 300 });
  const countsByStatus = actions.reduce((acc, action) => {
    const key = action.status || 'unknown';
    acc[key] = Number(acc[key] || 0) + 1;
    return acc;
  }, {});
  const latestAction = actions.length > 0 ? actions[actions.length - 1] : null;
  return {
    ...run,
    actionsCount: actions.length,
    actionCounts: countsByStatus,
    latestAction,
    logStreams: listLogStreams(run.runId, RUN_ROOT)
  };
}

function listRecentRuns(limit = 50) {
  return listRuns({ runRoot: RUN_ROOT, limit }).map((run) => summarizeRun(run));
}

function readRunSummary(runId) {
  const run = loadRun(runId, RUN_ROOT);
  return run ? summarizeRun(run) : null;
}

function listEventsForRuns(runs, filters = {}) {
  const events = [];
  for (const run of runs) {
    const runEvents = listRunEvents(run.runId, {
      runRoot: RUN_ROOT,
      limit: Number(filters.perRunLimit || 250),
      filters: {
        afterCursor: filters.afterCursor || '',
        issueIdentifier: filters.issueIdentifier || '',
        workerName: filters.workerName || '',
        eventType: filters.eventType || '',
        level: filters.level || '',
        search: filters.search || ''
      }
    });
    for (const event of runEvents) {
      events.push(event);
    }
  }
  events.sort((a, b) => String(a.cursor || '').localeCompare(String(b.cursor || '')));
  if (filters.limit && events.length > Number(filters.limit)) {
    return events.slice(-Number(filters.limit));
  }
  return events;
}

function readTimeline(filters = {}) {
  const runs = listRuns({
    runRoot: RUN_ROOT,
    issueIdentifier: filters.issueIdentifier || '',
    workerName: filters.workerName || '',
    limit: Number(filters.runLimit || 120)
  });
  return listEventsForRuns(runs, filters);
}

function buildRunSegments(runId) {
  const events = listRunEvents(runId, {
    runRoot: RUN_ROOT,
    limit: 1000
  });
  const segmentsByItemId = new Map();
  for (const event of events) {
    const itemId = event.payload?.itemId || '';
    const itemType = event.payload?.itemType || (event.eventType.startsWith('command.') ? 'commandExecution' : '');
    if (event.eventType === 'command.started' && itemId) {
      segmentsByItemId.set(itemId, {
        id: itemId,
        type: 'command',
        label: event.payload?.command || itemId,
        startedAt: event.timestamp,
        endedAt: null,
        status: 'running',
        tokenUsage: event.payload?.snapshot?.tokenUsage || null,
        causeEventId: event.id
      });
    } else if (event.eventType === 'command.completed' && itemId) {
      const current = segmentsByItemId.get(itemId) || {
        id: itemId,
        type: 'command',
        label: event.payload?.command || itemId,
        startedAt: event.timestamp
      };
      segmentsByItemId.set(itemId, {
        ...current,
        endedAt: event.timestamp,
        status: event.level === 'error' ? 'failed' : 'completed',
        exitCode: event.payload?.exitCode ?? null,
        durationMs: event.payload?.durationMs ?? null,
        tokenUsage: event.payload?.snapshot?.tokenUsage || current.tokenUsage || null,
        completionEventId: event.id
      });
    } else if (event.eventType === 'item.started' && itemId) {
      segmentsByItemId.set(itemId, {
        id: itemId,
        type: itemType || 'item',
        label: event.payload?.summary || itemType || itemId,
        startedAt: event.timestamp,
        endedAt: null,
        status: 'running',
        tokenUsage: event.payload?.snapshot?.tokenUsage || null,
        causeEventId: event.id
      });
    } else if (event.eventType === 'item.completed' && itemId) {
      const current = segmentsByItemId.get(itemId) || {
        id: itemId,
        type: itemType || 'item',
        label: event.payload?.itemType || itemId,
        startedAt: event.timestamp
      };
      segmentsByItemId.set(itemId, {
        ...current,
        endedAt: event.timestamp,
        status: 'completed',
        durationMs: event.payload?.durationMs ?? null,
        tokenUsage: event.payload?.snapshot?.tokenUsage || current.tokenUsage || null,
        completionEventId: event.id
      });
    }
  }
  return [...segmentsByItemId.values()].sort((a, b) => new Date(a.startedAt || 0) - new Date(b.startedAt || 0));
}

function buildRunDetail(runId) {
  const run = readRunSummary(runId);
  if (!run) {
    return null;
  }
  const timeline = listRunEvents(runId, { runRoot: RUN_ROOT, limit: 1200 });
  const operatorActions = listOperatorActions(runId, { runRoot: RUN_ROOT, limit: 400 });
  const logStreams = listLogStreams(runId, RUN_ROOT);
  return {
    run,
    timeline,
    operatorActions,
    logStreams,
    segments: buildRunSegments(runId)
  };
}

function maybeRunRawLogCleanup() {
  const now = Date.now();
  if (now - lastRawLogCleanupAt < 60 * 60 * 1000) {
    return [];
  }
  lastRawLogCleanupAt = now;
  const settings = readControlSettings();
  return purgeExpiredRawLogs({
    runRoot: RUN_ROOT,
    retentionDays: settings.rawLogRetentionDays,
    now
  });
}

function readHostStats() {
  let disk = { filesystem: 'unknown', usedPercent: null, available: null, mountedOn: '/srv/symphony' };
  try {
    const output = execSync('df -Pk /srv/symphony | tail -n 1', { encoding: 'utf8' }).trim();
    const [filesystem, , , available, usedPercent, mountedOn] = output.split(/\s+/);
    disk = { filesystem, available, usedPercent, mountedOn };
  } catch {
    // ignore
  }

  const totalMem = os.totalmem();
  const freeMem = os.freemem();
  const cpuCount = os.cpus().length;
  const load = os.loadavg();

  return {
    hostname: os.hostname(),
    platform: os.platform(),
    uptimeSeconds: os.uptime(),
    cpuCount,
    load,
    totalMem,
    freeMem,
    usedMem: totalMem - freeMem,
    disk
  };
}

function readAuthSession(providerId, workerName) {
  return readJson(getAuthSessionPath(providerId, workerName), null);
}

function writeAuthSession(providerId, workerName, value) {
  writeJson(getAuthSessionPath(providerId, workerName), value);
}

function pidExists(pid) {
  if (!pid) {
    return false;
  }
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function refreshAuthSession(providerId, workerName, completionStatus = 'failed') {
  const session = readAuthSession(providerId, workerName);
  if (!session) {
    return null;
  }
  if (session.logPath && fs.existsSync(session.logPath)) {
    session.rawOutput = stripAnsi(fs.readFileSync(session.logPath, 'utf8')).slice(-16_000);
    session.authUrl = parseFirstUrl(session.rawOutput) || session.authUrl;
    session.userCode = parseFirstDeviceCode(session.rawOutput) || session.userCode;
    if (!session.completedAt && session.authUrl) {
      session.status = 'pending';
    }
  }
  if (!session.completedAt && session.pid && !pidExists(session.pid)) {
    session.completedAt = new Date().toISOString();
    session.status = session.authUrl ? completionStatus : 'failed';
  }
  if (completionStatus === 'configured' && session.completedAt) {
    session.status = 'configured';
    delete session.error;
  }
  writeAuthSession(providerId, workerName, session);
  return session;
}

function persistCodexWorkerAuth(worker) {
  const authPath = `/home/${worker.user}/.codex/auth.json`;
  if (!sudoFileExists(authPath)) {
    throw new Error(`Missing ${authPath}`);
  }
  const authJson = readJsonViaSudo(authPath, {});
  const authEmail = getAuthEmailFromJson(authJson);
  if (!authEmail) {
    throw new Error(`Could not determine signed-in email for ${worker.name}`);
  }
  if (authEmail !== worker.email) {
    throw new Error(`Worker ${worker.name} expected ${worker.email} but auth is for ${authEmail}`);
  }
  const authB64 = Buffer.from(JSON.stringify(authJson, null, 2), 'utf8').toString('base64');
  upsertSecretEntries({ [getCodexSecretKey(worker)]: authB64 });
  return { authEmail, secretKey: getCodexSecretKey(worker) };
}

function restoreCodexWorkerAuth(worker) {
  const secretJson = getSecretJson();
  const authB64 = secretJson[getCodexSecretKey(worker)];
  if (!authB64) {
    throw new Error(`No Secrets Manager entry for ${getCodexSecretKey(worker)}`);
  }
  const authDir = `/home/${worker.user}/.codex`;
  ensureUserDir(authDir, worker.user, '700');
  installFileAsUser(Buffer.from(authB64, 'base64'), `${authDir}/auth.json`, worker.user, '600');
  installFileAsUser('cli_auth_credentials_store = "file"\nforced_login_method = "chatgpt"\n', `${authDir}/config.toml`, worker.user, '600');
  const authEmail = getAuthEmailFromJson(JSON.parse(Buffer.from(authB64, 'base64').toString('utf8')));
  if (authEmail && authEmail !== worker.email) {
    throw new Error(`Secrets auth for ${worker.name} is mapped to ${authEmail}, expected ${worker.email}`);
  }
  return { restored: true, authEmail };
}

function buildAntigravityRedirectUri(publicBaseUrl) {
  return new URL('/control/api/providers/antigravity/oauth/callback', publicBaseUrl).toString();
}

function generatePkcePair() {
  const verifier = randomBytes(32).toString('base64url');
  const challenge = createHash('sha256').update(verifier).digest('base64url');
  return { verifier, challenge };
}

function buildAntigravityAuthorizationUrl(config, redirectUri, state, challenge) {
  const params = new URLSearchParams({
    client_id: config.clientId,
    redirect_uri: redirectUri,
    response_type: 'code',
    scope: config.scopes.join(' '),
    access_type: 'offline',
    prompt: 'consent',
    code_challenge: challenge,
    code_challenge_method: 'S256',
    state
  });
  return `${ANTIGRAVITY_GOOGLE_AUTH_URL}?${params.toString()}`;
}

function extractGoogleCodeFromInput(input) {
  if (!input || !String(input).trim()) {
    throw new Error('OAuth callback URL or authorization code is required');
  }
  const trimmed = String(input).trim();
  if (/^https?:\/\//i.test(trimmed)) {
    const url = new URL(trimmed);
    const error = url.searchParams.get('error');
    if (error) {
      throw new Error(`OAuth error: ${error}`);
    }
    const code = url.searchParams.get('code');
    if (!code) {
      throw new Error('The callback URL does not contain an authorization code');
    }
    return {
      code,
      state: url.searchParams.get('state') || ''
    };
  }
  return { code: trimmed, state: '' };
}

function findAuthSessionByState(providerId, state) {
  if (!state || !fs.existsSync(AUTH_SESSION_DIR)) {
    return null;
  }
  const prefix = `${providerId}-`;
  for (const entry of fs.readdirSync(AUTH_SESSION_DIR)) {
    if (!entry.startsWith(prefix) || !entry.endsWith('.json')) {
      continue;
    }
    const session = readJson(path.join(AUTH_SESSION_DIR, entry), null);
    if (session?.state === state) {
      return session;
    }
  }
  return null;
}

async function exchangeAntigravityCode({ code, verifier, redirectUri, clientId, clientSecret }) {
  const response = await fetch(ANTIGRAVITY_GOOGLE_TOKEN_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      client_id: clientId,
      client_secret: clientSecret,
      code,
      code_verifier: verifier,
      grant_type: 'authorization_code',
      redirect_uri: redirectUri
    })
  });
  const rawBody = await response.text();
  let payload;
  try {
    payload = JSON.parse(rawBody);
  } catch {
    payload = { raw: rawBody };
  }
  if (!response.ok) {
    throw new Error(`Token exchange failed: ${JSON.stringify(payload)}`);
  }
  if (!payload.access_token) {
    throw new Error('Google token response did not contain an access token');
  }
  if (!payload.refresh_token) {
    throw new Error('Google token response did not contain a refresh token. Re-consent with prompt=consent and offline access.');
  }
  return {
    accessToken: payload.access_token,
    refreshToken: payload.refresh_token,
    expiresIn: payload.expires_in,
    idToken: payload.id_token || ''
  };
}

async function getGoogleUserInfo(accessToken) {
  const response = await fetch(ANTIGRAVITY_GOOGLE_USERINFO_URL, {
    headers: {
      Authorization: `Bearer ${accessToken}`
    }
  });
  const rawBody = await response.text();
  let payload;
  try {
    payload = JSON.parse(rawBody);
  } catch {
    payload = { raw: rawBody };
  }
  if (!response.ok) {
    throw new Error(`Failed to fetch Google user info: ${JSON.stringify(payload)}`);
  }
  return payload;
}

function extractCloudProjectId(payload) {
  if (!payload) {
    return null;
  }
  if (typeof payload.cloudaicompanionProject === 'string' && payload.cloudaicompanionProject) {
    return payload.cloudaicompanionProject;
  }
  if (payload.cloudaicompanionProject?.id) {
    return payload.cloudaicompanionProject.id;
  }
  return null;
}

function getDefaultTierId(allowedTiers = []) {
  if (!Array.isArray(allowedTiers) || allowedTiers.length === 0) {
    return undefined;
  }
  return allowedTiers.find((tier) => tier?.isDefault)?.id || allowedTiers[0]?.id;
}

async function loadAntigravityCodeAssist(accessToken) {
  let lastPayload = null;
  for (const endpoint of ANTIGRAVITY_LOAD_CODE_ASSIST_ENDPOINTS) {
    const response = await fetch(`${endpoint}/v1internal:loadCodeAssist`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${accessToken}`,
        ...getAntigravityHeaders()
      },
      body: JSON.stringify({
        metadata: getAntigravityClientMetadata()
      })
    });

    if (!response.ok) {
      continue;
    }

    const payload = await response.json();
    lastPayload = payload;
    const projectId = extractCloudProjectId(payload);
    if (projectId) {
      return {
        endpoint,
        payload,
        projectId,
        managedProjectId: payload.response?.cloudaicompanionProject?.id || null
      };
    }
  }

  return lastPayload
    ? {
        payload: lastPayload,
        projectId: null,
        managedProjectId: null
      }
    : null;
}

async function onboardAntigravityUser(accessToken, tierId, projectId = undefined, maxAttempts = 6, delayMs = 3000) {
  const metadata = { ...getAntigravityClientMetadata() };
  if (projectId) {
    metadata.duetProject = projectId;
  }

  const requestBody = {
    tierId,
    metadata
  };

  for (const endpoint of ANTIGRAVITY_ONBOARD_ENDPOINTS) {
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      const response = await fetch(`${endpoint}/v1internal:onboardUser`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${accessToken}`,
          ...getAntigravityHeaders()
        },
        body: JSON.stringify(requestBody)
      });

      if (!response.ok) {
        break;
      }

      const payload = await response.json();
      const discoveredProjectId = payload.response?.cloudaicompanionProject?.id || extractCloudProjectId(payload);
      if (payload.done && discoveredProjectId) {
        return {
          projectId: projectId || discoveredProjectId,
          managedProjectId: discoveredProjectId
        };
      }
      if (payload.done && projectId) {
        return { projectId, managedProjectId: null };
      }

      if (attempt < maxAttempts - 1) {
        await new Promise((resolve) => setTimeout(resolve, delayMs));
      }
    }
  }

  return null;
}

async function discoverAntigravityProject(accessToken) {
  const discovery = await loadAntigravityCodeAssist(accessToken);
  if (!discovery) {
    return { projectId: null, managedProjectId: null, source: 'none', allowedTiers: [] };
  }

  if (discovery.projectId) {
    return {
      projectId: discovery.projectId,
      managedProjectId: discovery.managedProjectId || null,
      source: 'loadCodeAssist',
      allowedTiers: discovery.payload?.allowedTiers || []
    };
  }

  const tierId = getDefaultTierId(discovery.payload?.allowedTiers || []);
  if (!tierId) {
    return {
      projectId: null,
      managedProjectId: null,
      source: 'loadCodeAssist',
      allowedTiers: discovery.payload?.allowedTiers || []
    };
  }

  const onboarded = await onboardAntigravityUser(accessToken, tierId);
  if (!onboarded) {
    return {
      projectId: null,
      managedProjectId: null,
      source: 'onboardUser',
      allowedTiers: discovery.payload?.allowedTiers || []
    };
  }

  return {
    projectId: onboarded.projectId || onboarded.managedProjectId || null,
    managedProjectId: onboarded.managedProjectId || null,
    source: 'onboardUser',
    allowedTiers: discovery.payload?.allowedTiers || []
  };
}

async function completeAntigravityAuthSession(session, code, completionMethod = 'oauth-callback') {
  const worker = getWorkerByName(session.worker);
  if (!worker) {
    throw new Error(`Unknown worker for Antigravity session: ${session.worker}`);
  }
  const config = getAntigravityConfig(getSecretJson());
  if (!config.clientId || !config.clientSecret) {
    throw new Error('Antigravity Google OAuth is not fully configured in AWS Secrets Manager');
  }

  const tokens = await exchangeAntigravityCode({
    code,
    verifier: session.verifier,
    redirectUri: session.redirectUri,
    clientId: config.clientId,
    clientSecret: config.clientSecret
  });
  const userInfo = await getGoogleUserInfo(tokens.accessToken);
  const email = userInfo.email || '';
  if (!email) {
    throw new Error('Could not determine the Google account email from the OAuth token');
  }
  if (worker.email && email !== worker.email) {
    throw new Error(`Worker ${worker.name} expected ${worker.email} but Google OAuth completed as ${email}`);
  }

  const project = await discoverAntigravityProject(tokens.accessToken);
  const authRecord = {
    version: 1,
    provider: 'antigravity',
    worker: worker.name,
    email,
    refreshToken: buildAntigravityCompositeRefreshToken(
      tokens.refreshToken,
      project.projectId || '',
      project.managedProjectId || ''
    ),
    projectId: project.projectId || null,
    managedProjectId: project.managedProjectId || null,
    scopes: config.scopes,
    clientMetadata: getAntigravityClientMetadata(),
    capturedAt: new Date().toISOString(),
    completionMethod
  };
  const persisted = persistAntigravityWorkerAuth(worker, authRecord);
  const nextSession = {
    ...session,
    status: 'configured',
    completedAt: new Date().toISOString(),
    completionMethod,
    authEmail: email,
    projectId: authRecord.projectId,
    managedProjectId: authRecord.managedProjectId
  };
  writeAuthSession('antigravity', worker.name, nextSession);
  return {
    session: nextSession,
    persisted
  };
}

function computeProviderOverview(workers) {
  const secretJson = getSecretJson();
  const antigravityConfig = getAntigravityConfig(secretJson);
  return {
    codex: {
      note:
        'The dashboard reflects auth installed on EC2 or stored in AWS Secrets Manager. Local laptop captures are not visible here until you run install_worker_auths.sh or use remote device auth. If no runtime model override is set, Codex runs with the CLI provider default.',
      workers: workers.map((worker) => {
        const session = refreshAuthSession('codex', worker.name, worker.authPresent ? 'configured' : 'failed');
        return {
          worker: worker.name,
          displayName: worker.displayName || worker.name,
          email: worker.email,
          user: worker.user,
          authPresent: worker.authPresent,
          authEmail: worker.authEmail || '',
          secretKey: getCodexSecretKey(worker),
          secretStored: Boolean(secretJson[getCodexSecretKey(worker)]),
          profile: worker.providerProfiles?.codex || normalizeWorkerProviderProfile(),
          session
        };
      })
    },
    openrouter: {
      secretConfigured: Boolean(getOpenRouterSecretKey(secretJson) && secretJson[getOpenRouterSecretKey(secretJson)]),
      secretKey: getOpenRouterSecretKey(secretJson)
    },
    antigravity: {
      config: {
        clientId: antigravityConfig.clientId,
        publicBaseUrl: antigravityConfig.publicBaseUrl,
        callbackUrl: antigravityConfig.callbackUrl,
        scopes: antigravityConfig.scopes,
        callbackReady: antigravityConfig.ready,
        warnings: antigravityConfig.warnings,
        clientIdConfigured: antigravityConfig.secretsConfigured.clientId,
        clientSecretConfigured: antigravityConfig.secretsConfigured.clientSecret,
        publicBaseUrlConfigured: antigravityConfig.secretsConfigured.publicBaseUrl
      },
      guidance:
        'Use one published Google OAuth web client for all Antigravity workers. The callback is configured once on that client and reused for every Google account.',
      workers: workers.map((worker) => ({
        worker: worker.name,
        displayName: worker.displayName || worker.name,
        email: worker.email,
        user: worker.user,
        authPath: getAntigravityAuthPath(worker),
        authPresent: sudoFileExists(getAntigravityAuthPath(worker)),
        authRecord: summarizeAntigravityAuthRecord(readAntigravityWorkerAuth(worker)),
        secretKey: getAntigravityAuthSecretKey(worker),
        secretStored: Boolean(secretJson[getAntigravityAuthSecretKey(worker)]),
        profile: worker.providerProfiles?.antigravity || normalizeWorkerProviderProfile(),
        session: summarizeAntigravitySession(refreshAuthSession('antigravity', worker.name, 'configured'))
      }))
    }
  };
}

function spawnAuthSession({
  providerId,
  worker,
  command,
  env = {},
  successHandler,
  cwd = null
}) {
  const key = `${providerId}:${worker.name}`;
  const existingSession = readAuthSession(providerId, worker.name);
  if (existingSession?.pid) {
    try {
      process.kill(existingSession.pid, 0);
      return existingSession;
    } catch {
      // stale session, continue
    }
  }

  fs.mkdirSync(AUTH_SESSION_DIR, { recursive: true });
  const logPath = path.join(AUTH_SESSION_DIR, `${providerId}-${worker.name}.log`);
  fs.writeFileSync(logPath, '');
  const child = spawn(
    SUDO_BIN,
    [
      '-u',
      worker.user,
      '-H',
      'env',
      `HOME=/home/${worker.user}`,
      ...Object.entries(env).flatMap(([keyName, value]) => [`${keyName}=${value}`]),
      ...command
    ],
    { stdio: ['ignore', 'pipe', 'pipe'], cwd: cwd || '/tmp' }
  );

  authProcessRegistry.set(key, child);
  const session = {
    providerId,
    worker: worker.name,
    pid: child.pid,
    status: 'starting',
    startedAt: new Date().toISOString(),
    logPath,
    rawOutput: ''
  };
  writeAuthSession(providerId, worker.name, session);

  const capture = (chunk) => {
    const text = stripAnsi(chunk.toString());
    fs.appendFileSync(logPath, text);
    session.rawOutput = `${session.rawOutput}${text}`.slice(-16_000);
    const firstUrl = parseFirstUrl(session.rawOutput);
    const firstCode = parseFirstDeviceCode(session.rawOutput);
    if (firstUrl) {
      session.authUrl = firstUrl;
    }
    if (firstCode) {
      session.userCode = firstCode;
    }
    session.status = session.authUrl ? 'pending' : session.status;
    writeAuthSession(providerId, worker.name, session);
  };

  child.stdout.on('data', capture);
  child.stderr.on('data', capture);
  child.on('error', (error) => {
    authProcessRegistry.delete(key);
    writeAuthSession(providerId, worker.name, {
      ...session,
      status: 'error',
      completedAt: new Date().toISOString(),
      error: error.message
    });
  });
  child.on('exit', (code) => {
    authProcessRegistry.delete(key);
    try {
      if (code === 0) {
        const success = successHandler();
        writeAuthSession(providerId, worker.name, {
          ...session,
          status: 'configured',
          completedAt: new Date().toISOString(),
          exitCode: code,
          ...success
        });
      } else {
        writeAuthSession(providerId, worker.name, {
          ...session,
          status: 'failed',
          completedAt: new Date().toISOString(),
          exitCode: code
        });
      }
    } catch (error) {
      writeAuthSession(providerId, worker.name, {
        ...session,
        status: 'error',
        completedAt: new Date().toISOString(),
        exitCode: code,
        error: error.message
      });
    }
  });

  return session;
}

function startCodexAuthSession(workerName) {
  const worker = getWorkerByName(workerName);
  if (!worker) {
    throw new Error(`Unknown worker: ${workerName}`);
  }
  return spawnAuthSession({
    providerId: 'codex',
    worker,
    env: {
      CODEX_HOME: `/home/${worker.user}/.codex`,
      PATH: `/home/${worker.user}/.local/bin:/home/${worker.user}/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin`
    },
    command: ['codex', 'login', '--device-auth'],
    successHandler: () => persistCodexWorkerAuth(worker)
  });
}

function saveOpenRouterSecret(apiKey) {
  if (!apiKey || !apiKey.trim()) {
    throw new Error('OPEN_ROUTER_API_KEY is required');
  }
  const secretKey = OPENROUTER_SECRET_KEYS[0];
  upsertSecretEntries({
    [secretKey]: apiKey.trim(),
    [OPENROUTER_SECRET_KEYS[1]]: apiKey.trim()
  });
  return { secretConfigured: true, secretKey };
}

function saveAntigravityConfig({ clientId, clientSecret, publicBaseUrl, scopes }) {
  const updates = {};
  if (clientId && String(clientId).trim()) {
    updates[ANTIGRAVITY_GOOGLE_CLIENT_ID_KEY] = String(clientId).trim();
  }
  if (clientSecret && String(clientSecret).trim()) {
    updates[ANTIGRAVITY_GOOGLE_CLIENT_SECRET_KEY] = String(clientSecret).trim();
  }
  if (typeof publicBaseUrl === 'string' && publicBaseUrl.trim()) {
    const normalized = publicBaseUrl.trim().replace(/\/+$/, '');
    new URL(normalized);
    updates[CONTROL_PLANE_PUBLIC_BASE_URL_KEY] = normalized;
  }
  if (typeof scopes === 'string' && scopes.trim()) {
    updates[ANTIGRAVITY_GOOGLE_SCOPES_KEY] = parseScopeString(scopes).join(' ');
  }
  if (Object.keys(updates).length === 0) {
    throw new Error('At least one Antigravity config field is required');
  }
  const updated = upsertSecretEntries(updates);
  return {
    updatedKeys: Object.keys(updates),
    config: getAntigravityConfig(updated)
  };
}

function startAntigravityAuthSession(workerName, req = null) {
  const worker = getWorkerByName(workerName);
  if (!worker) {
    throw new Error(`Unknown worker: ${workerName}`);
  }
  const config = getAntigravityConfig(getSecretJson(), req);
  if (!config.ready) {
    throw new Error(config.warnings.join(' '));
  }
  const { verifier, challenge } = generatePkcePair();
  const state = randomBytes(16).toString('hex');
  const redirectUri = buildAntigravityRedirectUri(config.publicBaseUrl);
  const session = {
    providerId: 'antigravity',
    worker: worker.name,
    email: worker.email,
    state,
    verifier,
    redirectUri,
    authUrl: buildAntigravityAuthorizationUrl(config, redirectUri, state, challenge),
    status: 'pending',
    scopes: config.scopes,
    startedAt: new Date().toISOString()
  };
  writeAuthSession('antigravity', worker.name, session);
  return session;
}

async function fetchSymphonyState() {
  try {
    return await fetchJson(SYMPHONY_STATE_URL);
  } catch (error) {
    return {
      error: error.message,
      running: [],
      retrying: [],
      counts: { running: 0, retrying: 0 },
      rate_limits: null,
      generated_at: new Date().toISOString()
    };
  }
}

async function fetchTeamStates() {
  const query = `
    query($teamId: String!) {
      team(id: $teamId) {
        id
        name
        key
        states {
          nodes {
            id
            name
            type
            position
          }
        }
      }
    }
  `;
  const data = await linearQuery(query, { teamId: TEAM_ID });
  const nodes = data?.team?.states?.nodes || [];
  const stateByName = Object.fromEntries(nodes.map((node) => [node.name, node]));
  return { team: data?.team, nodes, stateByName };
}

async function fetchProjectIssues() {
  const query = `
    query($projectId: String!) {
      project(id: $projectId) {
        id
        name
        issues(first: 100) {
          nodes {
            id
            identifier
            title
            url
            updatedAt
            priority
            state {
              id
              name
              type
            }
            attachments {
              nodes {
                id
                title
                url
              }
            }
          }
        }
      }
    }
  `;
  const data = await linearQuery(query, { projectId: PROJECT_ID });
  return data?.project?.issues?.nodes || [];
}

async function fetchIssueDetail(issueId) {
  const query = `
    query($id: String!) {
      issue(id: $id) {
        id
        identifier
        title
        url
        description
        updatedAt
        state { id name type }
        project { id name }
        attachments { nodes { id title url } }
        comments(first: 50) {
          nodes {
            id
            body
            updatedAt
            user { name }
          }
        }
      }
    }
  `;
  const data = await linearQuery(query, { id: issueId });
  const issue = data?.issue;
  if (!issue) {
    throw new Error(`Issue not found: ${issueId}`);
  }
  issue.comments.nodes.sort((a, b) => new Date(b.updatedAt) - new Date(a.updatedAt));
  return issue;
}

async function updateIssueState(issueId, stateId) {
  const mutation = `
    mutation($id: String!, $input: IssueUpdateInput!) {
      issueUpdate(id: $id, input: $input) {
        success
      }
    }
  `;
  await linearQuery(mutation, { id: issueId, input: { stateId } });
}

async function createIssueComment(issueId, body) {
  const mutation = `
    mutation($input: CommentCreateInput!) {
      commentCreate(input: $input) {
        success
      }
    }
  `;
  await linearQuery(mutation, { input: { issueId, body } });
}

function parseGitHubPr(attachments) {
  for (const attachment of attachments || []) {
    const match = attachment.url?.match(/^https:\/\/github\.com\/([^/]+)\/([^/]+)\/pull\/(\d+)/);
    if (match) {
      return {
        owner: match[1],
        repo: match[2],
        number: Number(match[3]),
        url: attachment.url,
        title: attachment.title
      };
    }
  }
  return null;
}

async function fetchGitHubPrSummary(prInfo) {
  if (!prInfo || !GITHUB_TOKEN) {
    return null;
  }

  const base = `https://api.github.com/repos/${prInfo.owner}/${prInfo.repo}`;
  const pr = await githubJson(`${base}/pulls/${prInfo.number}`);
  if (!pr) {
    return null;
  }

  const [checks, issueComments, reviewComments] = await Promise.all([
    githubJson(`${base}/commits/${pr.head.sha}/check-runs?per_page=100`),
    githubJson(`${base}/issues/${prInfo.number}/comments?per_page=100`),
    githubJson(`${base}/pulls/${prInfo.number}/comments?per_page=100`)
  ]);

  const checkRuns = checks?.check_runs || [];
  const qodoIssueComments = Array.isArray(issueComments)
    ? issueComments.filter((comment) => comment?.user?.login === 'qodo-code-review')
    : [];
  const qodoReviewComments = Array.isArray(reviewComments)
    ? reviewComments.filter((comment) => comment?.user?.login === 'qodo-code-review')
    : [];

  return {
    number: pr.number,
    url: pr.html_url,
    state: pr.state,
    mergeable: pr.mergeable,
    draft: pr.draft,
    headSha: pr.head.sha,
    checks: {
      total: checkRuns.length,
      success: checkRuns.filter((run) => run.conclusion === 'success').length,
      pending: checkRuns.filter((run) => !run.conclusion && run.status !== 'completed').length,
      failed: checkRuns.filter((run) => ['failure', 'timed_out', 'cancelled', 'startup_failure'].includes(run.conclusion)).length
    },
    qodo: {
      issueComments: qodoIssueComments.length,
      reviewComments: qodoReviewComments.length
    }
  };
}

async function fetchGitHubRun(owner, repo, runId) {
  if (!owner || !repo || !runId) {
    return null;
  }
  return githubJson(`https://api.github.com/repos/${owner}/${repo}/actions/runs/${runId}`);
}

async function fetchGitHubJobsForRun(owner, repo, runId) {
  if (!owner || !repo || !runId) {
    return [];
  }
  const payload = await githubJson(`https://api.github.com/repos/${owner}/${repo}/actions/runs/${runId}/jobs?per_page=100`);
  return Array.isArray(payload?.jobs) ? payload.jobs : [];
}

async function fetchGitHubJobDetail(owner, repo, jobId) {
  if (!owner || !repo || !jobId) {
    return null;
  }
  return githubJson(`https://api.github.com/repos/${owner}/${repo}/actions/jobs/${jobId}`);
}

function normalizeGitHubRunPullRequests(runPayload = {}) {
  const prs = Array.isArray(runPayload.pull_requests) ? runPayload.pull_requests : [];
  return prs
    .map((entry) => ({
      number: Number(entry?.number || 0),
      url: String(entry?.url || entry?.html_url || '').trim()
    }))
    .filter((entry) => entry.number > 0 || entry.url);
}

function buildGitHubJobRecord(jobPayload = {}, repoFullName, runPayload = null, source = 'github-api') {
  const activeStep = Array.isArray(jobPayload.steps)
    ? jobPayload.steps.find((step) => step.status === 'in_progress') || null
    : null;
  return {
    jobId: Number(jobPayload.id || 0),
    runId: Number(jobPayload.run_id || runPayload?.id || 0),
    runAttempt: Number(runPayload?.run_attempt || 0),
    repoFullName,
    workflowName: String(jobPayload.workflow_name || runPayload?.name || '').trim(),
    name: String(jobPayload.name || '').trim(),
    htmlUrl: String(jobPayload.html_url || '').trim(),
    checkRunUrl: String(jobPayload.check_run_url || '').trim(),
    runUrl: String(jobPayload.run_url || runPayload?.url || '').trim(),
    status: String(jobPayload.status || '').trim(),
    conclusion: jobPayload.conclusion ?? null,
    headSha: String(runPayload?.head_sha || '').trim(),
    headBranch: String(runPayload?.head_branch || '').trim(),
    event: String(runPayload?.event || '').trim(),
    labels: Array.isArray(jobPayload.labels) ? jobPayload.labels : [],
    runnerName: String(jobPayload.runner_name || '').trim(),
    runnerGroupName: String(jobPayload.runner_group_name || '').trim(),
    runnerId: Number(jobPayload.runner_id || 0),
    workflowId: Number(runPayload?.workflow_id || 0),
    checkRunId: Number(jobPayload.check_run_id || 0),
    pullRequests: normalizeGitHubRunPullRequests(runPayload || {}),
    queuedAt: jobPayload.started_at ? null : runPayload?.created_at || null,
    startedAt: jobPayload.started_at || null,
    completedAt: jobPayload.completed_at || null,
    steps: Array.isArray(jobPayload.steps) ? jobPayload.steps : [],
    currentStepName: String(activeStep?.name || '').trim(),
    currentStepNumber: Number(activeStep?.number || 0),
    source
  };
}

function appendGitHubLifecycleEvent(job, eventType, summary, payload = {}, source = 'github-actions', level = 'info') {
  appendGitHubEvent({
    eventType,
    level,
    summary,
    source,
    repoFullName: job.repoFullName,
    jobId: job.jobId,
    runId: job.runId,
    workflowName: job.workflowName,
    jobName: job.name,
    payload
  }, GITHUB_STATE_ROOT);
}

function maybeAppendGitHubStateTransition(currentJob, nextJob, source) {
  if (!currentJob) {
    appendGitHubLifecycleEvent(
      nextJob,
      'github.job.discovered',
      `Discovered GitHub job ${nextJob.name || nextJob.jobId}`,
      {
        status: nextJob.status,
        conclusion: nextJob.conclusion,
        headBranch: nextJob.headBranch,
        pullRequests: nextJob.pullRequests
      },
      source
    );
    return;
  }

  if (currentJob.status !== nextJob.status || currentJob.conclusion !== nextJob.conclusion) {
    const level = nextJob.conclusion && nextJob.conclusion !== 'success' ? 'warning' : 'info';
    appendGitHubLifecycleEvent(
      nextJob,
      'github.job.transition',
      `GitHub job ${nextJob.name || nextJob.jobId} is ${nextJob.status}${nextJob.conclusion ? ` (${nextJob.conclusion})` : ''}`,
      {
        previousStatus: currentJob.status,
        previousConclusion: currentJob.conclusion,
        status: nextJob.status,
        conclusion: nextJob.conclusion
      },
      source,
      level
    );
  }

  const previousStep = String(currentJob.currentStepName || '').trim();
  const nextStep = String(nextJob.currentStepName || '').trim();
  if (nextStep && previousStep !== nextStep) {
    appendGitHubLifecycleEvent(
      nextJob,
      'github.job.step',
      `GitHub job ${nextJob.name || nextJob.jobId} entered step ${nextStep}`,
      {
        previousStep,
        currentStep: nextStep,
        steps: nextJob.steps
      },
      source
    );
  }
}

async function upsertGitHubJobSnapshot(jobPayload, runPayload, source = 'github-api') {
  const repoFullName =
    runPayload?.repository?.full_name ||
    runPayload?.head_repository?.full_name ||
    parseRepoSlug()?.fullName ||
    '';
  const current = loadGitHubJob(jobPayload.id || jobPayload.jobId, GITHUB_STATE_ROOT);
  const next = upsertGitHubJob(
    {
      ...buildGitHubJobRecord(jobPayload, repoFullName, runPayload, source),
      lastWebhookAt: source === 'github-webhook' ? new Date().toISOString() : current?.lastWebhookAt || null,
      lastPolledAt: source === 'github-poll' || source === 'github-discovery' ? new Date().toISOString() : current?.lastPolledAt || null
    },
    GITHUB_STATE_ROOT
  );
  maybeAppendGitHubStateTransition(current, next, source);
  return next;
}

async function discoverActiveGitHubJobs() {
  const repo = parseRepoSlug();
  if (!repo) {
    return [];
  }
  const runsPayload = await githubJson(`https://api.github.com/repos/${repo.owner}/${repo.repo}/actions/runs?per_page=20`);
  const workflowRuns = Array.isArray(runsPayload?.workflow_runs) ? runsPayload.workflow_runs : [];
  const activeRuns = workflowRuns.filter((run) => ['queued', 'in_progress'].includes(run.status));
  const discovered = [];
  for (const run of activeRuns) {
    const jobs = await fetchGitHubJobsForRun(repo.owner, repo.repo, run.id);
    for (const job of jobs) {
      const snapshot = await upsertGitHubJobSnapshot(job, run, 'github-discovery');
      discovered.push(snapshot);
    }
  }
  return discovered;
}

async function pollGitHubJob(job) {
  const repoFullName = job.repoFullName || parseRepoSlug()?.fullName || '';
  const [owner, repo] = repoFullName.split('/');
  if (!owner || !repo || !job.jobId) {
    return job;
  }
  const detail = await fetchGitHubJobDetail(owner, repo, job.jobId);
  let runPayload = null;
  if (job.runId) {
    runPayload = await fetchGitHubRun(owner, repo, job.runId);
  }
  if (!detail) {
    return job;
  }
  return await upsertGitHubJobSnapshot(detail, runPayload, 'github-poll');
}

function safeKillChild(child) {
  if (!child || child.killed) {
    return;
  }
  try {
    child.kill('SIGTERM');
  } catch {
    // ignore
  }
}

function startGitHubStreamProcess(jobId, streamId, label, command, args, patch = {}) {
  const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'pipe'] });
  const basePatch = { source: 'runner-observer', ...patch };
  const onChunk = (chunk, severity = basePatch.severity || 'info') => {
    appendGitHubJobStreamChunk(jobId, streamId, label, stripAnsi(chunk.toString()), {
      ...basePatch,
      severity
    });
  };
  child.stdout.on('data', (chunk) => onChunk(chunk, basePatch.severity || 'info'));
  child.stderr.on('data', (chunk) => onChunk(chunk, 'warning'));
  child.on('close', (code) => {
    upsertGitHubJobLogStream(jobId, {
      id: streamId,
      label,
      completedAt: new Date().toISOString(),
      exitCode: code,
      ...basePatch
    }, GITHUB_STATE_ROOT);
  });
  return child;
}

function pickRunnerDiagFile(job) {
  if (!fs.existsSync(RUNNER_DIAG_DIR)) {
    return '';
  }
  const threshold = Math.max(
    0,
    new Date(job.startedAt || job.queuedAt || Date.now()).getTime() - 10 * 60 * 1000
  );
  const candidates = fs.readdirSync(RUNNER_DIAG_DIR)
    .filter((entry) => /^Worker_.*\.log$/.test(entry))
    .map((entry) => {
      const fullPath = path.join(RUNNER_DIAG_DIR, entry);
      const stat = fs.statSync(fullPath);
      return { fullPath, mtimeMs: stat.mtimeMs };
    })
    .sort((a, b) => b.mtimeMs - a.mtimeMs);
  return candidates.find((entry) => entry.mtimeMs >= threshold)?.fullPath || candidates[0]?.fullPath || '';
}

function refreshGitHubDiagTail(observer, job) {
  const nextDiagFile = pickRunnerDiagFile(job);
  if (!nextDiagFile || observer.diagFile === nextDiagFile) {
    return;
  }
  if (observer.diagProc) {
    safeKillChild(observer.diagProc);
  }
  observer.diagFile = nextDiagFile;
  observer.diagProc = startGitHubStreamProcess(
    job.jobId,
    'worker-diag',
    'Runner Worker Log',
    'tail',
    ['-n', '0', '-F', nextDiagFile],
    {
      type: 'diag',
      meta: { filePath: nextDiagFile }
    }
  );
}

function refreshGitHubDockerTails(observer, job) {
  const result = spawnSync('docker', ['ps', '--format', '{{json .}}'], { encoding: 'utf8' });
  if (result.status !== 0) {
    observer.lastObserverError = (result.stderr || result.stdout || 'docker ps failed').trim();
    return;
  }
  observer.lastObserverError = '';
  const containers = result.stdout
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => {
      try {
        const parsed = JSON.parse(line);
        return {
          id: String(parsed.ID || '').trim(),
          name: String(parsed.Names || '').trim(),
          image: String(parsed.Image || '').trim(),
          status: String(parsed.Status || '').trim()
        };
      } catch {
        return null;
      }
    })
    .filter(Boolean);

  const seen = new Set();
  for (const container of containers) {
    seen.add(container.id);
    if (!observer.dockerProcs.has(container.id)) {
      const child = startGitHubStreamProcess(
        job.jobId,
        `docker-${container.id}`,
        `Docker ${container.name || container.id}`,
        'docker',
        ['logs', '-f', '--timestamps', container.id],
        {
          type: 'docker',
          meta: container
        }
      );
      observer.dockerProcs.set(container.id, { child, ...container });
    }
  }
  for (const [containerId, containerState] of observer.dockerProcs.entries()) {
    if (!seen.has(containerId)) {
      safeKillChild(containerState.child);
      observer.dockerProcs.delete(containerId);
    }
  }
}

function stopGitHubJobObserver(jobId, immediate = false) {
  const observer = githubObserverRegistry.get(Number(jobId));
  if (!observer) {
    return;
  }
  if (!immediate) {
    clearTimeout(observer.stopTimer);
    observer.stopTimer = setTimeout(() => stopGitHubJobObserver(jobId, true), GITHUB_OBSERVER_GRACE_MS);
    return;
  }
  clearTimeout(observer.stopTimer);
  clearInterval(observer.interval);
  safeKillChild(observer.journalProc);
  safeKillChild(observer.diagProc);
  for (const entry of observer.dockerProcs.values()) {
    safeKillChild(entry.child);
  }
  githubObserverRegistry.delete(Number(jobId));
}

function startGitHubJobObserver(job) {
  const jobId = Number(job.jobId || 0);
  if (!jobId) {
    return;
  }
  let observer = githubObserverRegistry.get(jobId);
  if (!observer) {
    observer = {
      jobId,
      journalProc: startGitHubStreamProcess(
        jobId,
        'runner-journal',
        'Runner Service',
        SUDO_BIN,
        ['journalctl', '-u', RUNNER_SERVICE_NAME, '-f', '-n', '0', '-o', 'short-iso'],
        { type: 'journal', meta: { service: RUNNER_SERVICE_NAME } }
      ),
      diagProc: null,
      diagFile: '',
      dockerProcs: new Map(),
      interval: null,
      stopTimer: null,
      lastObserverError: ''
    };
    observer.interval = setInterval(() => {
      const latestJob = loadGitHubJob(jobId, GITHUB_STATE_ROOT) || job;
      refreshGitHubDiagTail(observer, latestJob);
      refreshGitHubDockerTails(observer, latestJob);
      upsertGitHubJob({
        jobId,
        observer: {
          runnerService: RUNNER_SERVICE_NAME,
          diagFile: observer.diagFile,
          dockerContainers: [...observer.dockerProcs.values()].map((entry) => ({
            id: entry.id,
            name: entry.name,
            image: entry.image,
            status: entry.status
          })),
          lastObservedAt: new Date().toISOString(),
          lastObserverError: observer.lastObserverError
        }
      }, GITHUB_STATE_ROOT);
    }, GITHUB_OBSERVER_POLL_INTERVAL_MS);
    githubObserverRegistry.set(jobId, observer);
  }

  clearTimeout(observer.stopTimer);
  refreshGitHubDiagTail(observer, job);
  refreshGitHubDockerTails(observer, job);
}

function refreshGitHubObserverState(job) {
  if (String(job.status) === 'in_progress') {
    startGitHubJobObserver(job);
    return;
  }
  if (['completed', 'queued'].includes(String(job.status))) {
    stopGitHubJobObserver(job.jobId, false);
  }
}

async function refreshGitHubActions() {
  const now = Date.now();
  if (now - lastGitHubDiscoveryAt >= GITHUB_DISCOVERY_INTERVAL_MS) {
    lastGitHubDiscoveryAt = now;
    const discovered = await discoverActiveGitHubJobs();
    for (const job of discovered) {
      refreshGitHubObserverState(job);
    }
  }

  if (now - lastGitHubStepPollAt >= GITHUB_STEP_POLL_INTERVAL_MS) {
    lastGitHubStepPollAt = now;
    const activeJobs = listGitHubJobs({ root: GITHUB_STATE_ROOT, activeOnly: true, limit: 50 });
    for (const job of activeJobs) {
      const next = await pollGitHubJob(job);
      refreshGitHubObserverState(next);
    }
  }
}

function extractSection(markdown, heading) {
  if (!markdown) {
    return '';
  }
  const escaped = heading.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const regex = new RegExp(`^### ${escaped}\\n([\\s\\S]*?)(?=^### |^## |\\Z)`, 'm');
  const match = markdown.match(regex);
  return match ? match[1].trim() : '';
}

function summarizeWorkpad(body) {
  return {
    environment: extractSection(body, 'Environment'),
    currentStatus: extractSection(body, 'Status') || extractSection(body, 'Current Status'),
    validation: extractSection(body, 'Validation'),
    blockers: extractSection(body, 'Current Blockers') || extractSection(body, 'Blockers'),
    qodo: extractSection(body, 'Qodo Triage')
  };
}

function buildHandoffComment(issue, handoffState, reason, details) {
  const marker = `<!-- ${CONTROL_MARKER}:${reason}:${issue.identifier} -->`;
  const heading = handoffState === 'Awaiting Credentials' ? 'Awaiting Credentials' : 'Needs Human';
  return `${marker}\n## Human Input Required\n\n- Issue: ${issue.identifier}\n- Requested state: ${handoffState}\n- Reason: ${details.reasonText}\n- Required action: ${details.requiredAction}\n- Observed at: ${new Date().toISOString()}\n\n### Context\n${details.context}\n\n### Resume condition\n${details.resumeCondition}`;
}

function determineStall(issue, workers, symphony) {
  const availableWorkers = workers.filter((worker) => worker.availableForNewWork);
  const missingAuthWorkers = workers.filter((worker) => worker.status === 'missing_auth');
  const rateLimitedWorkers = workers.filter((worker) => worker.status === 'rate_limited' || worker.status === 'cooldown');
  const globalRateLimited = Boolean(
    symphony?.rate_limits?.secondary?.used_percent >= 100 ||
      (symphony?.rate_limits?.credits && symphony.rate_limits.credits.has_credits === false && symphony.rate_limits.credits.balance === '0')
  );
  const issueAgeMs = issue?.updatedAt ? Date.now() - new Date(issue.updatedAt).getTime() : Number.POSITIVE_INFINITY;

  if (availableWorkers.length > 0) {
    if (issueAgeMs < STALL_THRESHOLD_MS) {
      return null;
    }

    return {
      stateName: 'Needs Human',
      reason: 'scheduler-stall',
      details: {
        reasonText: 'The issue is no longer running, no retry is pending, and worker capacity exists, which indicates an orchestration stall.',
        requiredAction: 'Inspect the Symphony queue and worker logs, then move the issue to Ready to Resume once the scheduler path is healthy.',
        context: `Issue last updated at ${issue.updatedAt}; available workers: ${availableWorkers.map((worker) => worker.name).join(', ')}`,
        resumeCondition: 'The scheduler path is healthy again and the issue is moved to Ready to Resume.'
      }
    };
  }

  if (missingAuthWorkers.length === workers.length) {
    return {
      stateName: 'Awaiting Credentials',
      reason: 'missing-auth',
      details: {
        reasonText: 'No worker has a valid ChatGPT Codex auth.json installed on the EC2 host.',
        requiredAction:
          'Capture auth on a browser-capable machine with scripts/symphony/capture_worker_auth.sh and sync it with scripts/symphony/install_worker_auths.sh.',
        context: `Enabled workers: ${workers.map((worker) => `${worker.name} (${worker.email})`).join(', ')}`,
        resumeCondition: 'At least one worker has a valid auth.json and the issue is moved to Ready to Resume.'
      }
    };
  }

  if (rateLimitedWorkers.length > 0 || globalRateLimited) {
    return {
      stateName: 'Needs Human',
      reason: 'rate-limit',
      details: {
        reasonText: 'No worker is currently available because Codex usage is rate-limited or cooling down.',
        requiredAction:
          'Wait for reset, clear worker cooldowns if appropriate, or add another authenticated worker account.',
        context: `Rate-limited workers: ${rateLimitedWorkers.map((worker) => worker.name).join(', ') || 'unknown'}; Symphony rate limits: ${JSON.stringify(symphony?.rate_limits || {})}`,
        resumeCondition: 'A worker is available again and the issue is moved to Ready to Resume.'
      }
    };
  }

  return {
    stateName: 'Needs Human',
    reason: 'no-available-worker',
    details: {
      reasonText: 'No worker is currently available to claim new work.',
      requiredAction: 'Review worker health, clear cordons/cooldowns, or provision additional workers.',
      context: `Worker statuses: ${workers.map((worker) => `${worker.name}=${worker.status}`).join(', ')}`,
      resumeCondition: 'A worker is available again and the issue is moved to Ready to Resume.'
    }
  };
}

async function computeOverview() {
  const [symphony, issues, teamStates, tradingNodes] = await Promise.all([
    fetchSymphonyState(),
    fetchProjectIssues(),
    fetchTeamStates(),
    Promise.resolve(loadTradingNodeState())
  ]);

  const workers = readWorkers();
  const providers = computeProviderOverview(workers);
  const githubActions = {
    ...readGitHubActionsState(40),
    webhook: getGitHubWebhookConfig(getSecretJson())
  };
  const strategyNodes = {
    manifests: listStrategyNodeCatalogEntries(),
    requests: listStrategyNodeRequests(40),
    hosts: tradingNodes.hosts,
    nodes: tradingNodes.nodes,
    summary: summarizeTradingNodesSnapshot(tradingNodes),
  };
  const settings = readControlSettings();
  const runs = listRecentRuns(60);
  const latestRunByIssue = new Map();
  const latestRunByWorker = new Map();
  for (const run of runs) {
    if (run.issueIdentifier && !latestRunByIssue.has(run.issueIdentifier)) {
      latestRunByIssue.set(run.issueIdentifier, run);
    }
    if (run.workerName && !latestRunByWorker.has(run.workerName)) {
      latestRunByWorker.set(run.workerName, run);
    }
  }
  const running = Array.isArray(symphony?.running) ? symphony.running : [];
  const retrying = Array.isArray(symphony?.retrying) ? symphony.retrying : [];
  const runningMap = new Map(running.map((entry) => [entry.issue_identifier, entry]));
  const retryingMap = new Map(retrying.map((entry) => [entry.issue_identifier, entry]));

  const issueRows = issues.map((issue) => {
    const pr = parseGitHubPr(issue.attachments?.nodes || []);
    const latestRun = latestRunByIssue.get(issue.identifier) || null;
    return {
      ...issue,
      running: runningMap.get(issue.identifier) || null,
      retrying: retryingMap.get(issue.identifier) || null,
      pr,
      latestRun
    };
  });

  const humanInbox = issueRows.filter((issue) => HANDOFF_STATES.includes(issue.state?.name));
  const stalledIssues = issueRows.filter(
    (issue) =>
      STALLED_EXECUTION_STATES.includes(issue.state?.name) &&
      !issue.running &&
      !issue.retrying
  );

  const alerts = [];
  if (workers.every((worker) => !worker.authPresent)) {
    alerts.push({ level: 'warning', message: 'No EC2 Codex worker currently has an auth.json installed.' });
  }
  if (symphony?.rate_limits?.secondary?.used_percent >= 100) {
    alerts.push({ level: 'warning', message: 'Symphony reports a fully exhausted secondary rate limit window.' });
  }
  if (runs.some((run) => ['failed', 'interrupted'].includes(run.status))) {
    alerts.push({ level: 'warning', message: 'Recent runs include failed or interrupted executions. Inspect the Timeline or Run Detail panels.' });
  }
  if (githubActions.activeJobs.some((job) => ['failure', 'timed_out', 'cancelled'].includes(job.conclusion))) {
    alerts.push({ level: 'warning', message: 'A live GitHub Actions job is failing or was cancelled. Inspect GitHub Actions Live.' });
  }
  for (const warning of githubActions.webhook.warnings || []) {
    alerts.push({ level: 'warning', message: `GitHub webhooks: ${warning}` });
  }
  for (const warning of providers?.antigravity?.config?.warnings || []) {
    alerts.push({ level: 'warning', message: `Antigravity OAuth: ${warning}` });
  }

  return {
    generatedAt: new Date().toISOString(),
    symphony,
    teamStates,
    host: readHostStats(),
    tradingNodes: {
      ...tradingNodes,
      summary: summarizeTradingNodesSnapshot(tradingNodes),
    },
    workers: workers.map((worker) => ({
      ...worker,
      latestRun: latestRunByWorker.get(worker.name) || null
    })),
    githubActions,
    strategyNodes,
    providers,
    issues: issueRows,
    runs,
    settings,
    humanInbox,
    stalledIssues,
    alerts
  };
}

async function reconcile(overview) {
  if (reconcileBusy) {
    return;
  }
  reconcileBusy = true;

  try {
    const stateByName = overview.teamStates?.stateByName || {};
    for (const issue of overview.stalledIssues) {
      const plan = determineStall(issue, overview.workers, overview.symphony);
      if (!plan) {
        continue;
      }

      const targetState = stateByName[plan.stateName];
      if (!targetState) {
        continue;
      }

      if (issue.state?.name !== plan.stateName) {
        await updateIssueState(issue.id, targetState.id);
      }

      const detail = await fetchIssueDetail(issue.identifier);
      const marker = `${CONTROL_MARKER}:${plan.reason}:${issue.identifier}`;
      const hasComment = detail.comments.nodes.some((comment) => comment.body?.includes(marker));
      if (!hasComment) {
        const body = buildHandoffComment(issue, plan.stateName, plan.reason, plan.details);
        await createIssueComment(issue.id, body);
      }
    }
  } catch (error) {
    console.error(`[control-plane] reconcile failed: ${error.message}`);
  } finally {
    reconcileBusy = false;
  }
}

async function refreshOverview() {
  maybeRunRawLogCleanup();
  await refreshGitHubActions();
  latestOverview = await computeOverview();
  await reconcile(latestOverview);
}

function renderHtml() {
  return fs.readFileSync(path.join(STATIC_ROOT, 'index.html'), 'utf8');
}

function parseTimelineFilters(requestUrl) {
  return {
    runId: requestUrl.searchParams.get('runId') || '',
    issueIdentifier: requestUrl.searchParams.get('issue') || '',
    workerName: requestUrl.searchParams.get('worker') || '',
    eventType: requestUrl.searchParams.get('eventType') || '',
    level: requestUrl.searchParams.get('level') || '',
    search: requestUrl.searchParams.get('search') || '',
    afterCursor: requestUrl.searchParams.get('afterCursor') || '',
    limit: Number(requestUrl.searchParams.get('limit') || 300)
  };
}

function readFilteredTimeline(filters = {}) {
  if (filters.runId) {
    return listRunEvents(filters.runId, {
      runRoot: RUN_ROOT,
      limit: filters.limit,
      filters
    });
  }
  return readTimeline(filters);
}

async function handleTimeline(res, requestUrl) {
  try {
    const filters = parseTimelineFilters(requestUrl);
    const events = readFilteredTimeline(filters);
    sendJson(res, 200, { filters, events });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleRunList(res, requestUrl) {
  try {
    const issueIdentifier = requestUrl.searchParams.get('issue') || '';
    const workerName = requestUrl.searchParams.get('worker') || '';
    const runs = listRuns({
      runRoot: RUN_ROOT,
      issueIdentifier,
      workerName,
      limit: Number(requestUrl.searchParams.get('limit') || 100)
    }).map((run) => summarizeRun(run));
    sendJson(res, 200, { runs });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleRunDetail(res, runId) {
  try {
    const detail = buildRunDetail(runId);
    if (!detail) {
      sendJson(res, 404, { error: `Unknown run: ${runId}` });
      return;
    }
    sendJson(res, 200, detail);
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleRunPromptQueue(req, res, runId) {
  try {
    const run = readRunSummary(runId);
    if (!run) {
      sendJson(res, 404, { error: `Unknown run: ${runId}` });
      return;
    }
    const raw = await readBody(req);
    const payload = JSON.parse(raw || '{}');
    const prompt = String(payload.prompt || '').trim();
    if (!prompt) {
      sendJson(res, 400, { error: 'prompt is required' });
      return;
    }
    const action = createOperatorAction(runId, {
      issueIdentifier: run.issueIdentifier,
      workerName: run.workerName,
      type: 'prompt',
      prompt,
      deliveryMode: normalizeDeliveryMode(payload.deliveryMode || readControlSettings().defaultPromptDeliveryMode),
      priority: Number(payload.priority || 0),
      requestedBy: 'dashboard',
      metadata: {
        note: String(payload.note || '').trim()
      }
    }, RUN_ROOT);
    appendRunEvent(runId, {
      issueIdentifier: run.issueIdentifier,
      workerName: run.workerName,
      provider: run.provider || 'codex',
      eventType: 'agent.prompt_queued',
      level: 'info',
      summary: 'Operator queued a prompt',
      payload: {
        actionId: action.id,
        prompt: action.prompt,
        deliveryMode: action.deliveryMode
      },
      source: 'dashboard'
    }, RUN_ROOT);
    sendJson(res, 200, { success: true, action, detail: buildRunDetail(runId) });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleRunInterrupt(req, res, runId) {
  try {
    const run = readRunSummary(runId);
    if (!run) {
      sendJson(res, 404, { error: `Unknown run: ${runId}` });
      return;
    }
    const raw = await readBody(req);
    const payload = JSON.parse(raw || '{}');
    const action = createOperatorAction(runId, {
      issueIdentifier: run.issueIdentifier,
      workerName: run.workerName,
      type: 'interrupt',
      reason: String(payload.reason || '').trim(),
      requestedBy: 'dashboard',
      deliveryMode: 'interrupt_now',
      metadata: {
        mode: String(payload.mode || 'interrupt_now')
      }
    }, RUN_ROOT);
    appendRunEvent(runId, {
      issueIdentifier: run.issueIdentifier,
      workerName: run.workerName,
      provider: run.provider || 'codex',
      eventType: 'agent.interrupt_requested',
      level: 'warning',
      summary: 'Operator requested an interrupt',
      payload: {
        actionId: action.id,
        reason: action.reason || ''
      },
      source: 'dashboard'
    }, RUN_ROOT);
    sendJson(res, 200, { success: true, action, detail: buildRunDetail(runId) });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleRunCheckpoint(req, res, runId) {
  try {
    const run = readRunSummary(runId);
    if (!run) {
      sendJson(res, 404, { error: `Unknown run: ${runId}` });
      return;
    }
    const raw = await readBody(req);
    const payload = JSON.parse(raw || '{}');
    const note = String(payload.note || '').trim();
    const prompt = note
      ? `After the current step, publish a concise checkpoint update. Include current objective, active blocker or risk, most recent validation signal, next planned action, and this operator note: ${note}`
      : 'After the current step, publish a concise checkpoint update. Include current objective, active blocker or risk, most recent validation signal, and next planned action.';
    const action = createOperatorAction(runId, {
      issueIdentifier: run.issueIdentifier,
      workerName: run.workerName,
      type: 'checkpoint',
      prompt,
      deliveryMode: normalizeDeliveryMode(payload.deliveryMode || readControlSettings().defaultPromptDeliveryMode),
      requestedBy: 'dashboard',
      metadata: { note }
    }, RUN_ROOT);
    appendRunEvent(runId, {
      issueIdentifier: run.issueIdentifier,
      workerName: run.workerName,
      provider: run.provider || 'codex',
      eventType: 'agent.checkpoint_requested',
      level: 'info',
      summary: 'Operator requested a checkpoint update',
      payload: {
        actionId: action.id,
        note
      },
      source: 'dashboard'
    }, RUN_ROOT);
    sendJson(res, 200, { success: true, action, detail: buildRunDetail(runId) });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleRunPromptList(res, runId) {
  try {
    const run = readRunSummary(runId);
    if (!run) {
      sendJson(res, 404, { error: `Unknown run: ${runId}` });
      return;
    }
    const actions = listOperatorActions(runId, {
      runRoot: RUN_ROOT,
      limit: 300
    });
    sendJson(res, 200, {
      run,
      actions
    });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleRunLogChunk(res, runId, streamId, requestUrl) {
  try {
    const chunk = readLogChunk(runId, streamId, {
      runRoot: RUN_ROOT,
      offset: Number(requestUrl.searchParams.get('offset') || 0),
      limit: Number(requestUrl.searchParams.get('limit') || 64 * 1024)
    });
    sendJson(res, 200, chunk);
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleSettingsGet(res) {
  try {
    sendJson(res, 200, { settings: readControlSettings() });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleSettingsPost(req, res) {
  try {
    const raw = await readBody(req);
    const payload = JSON.parse(raw || '{}');
    const settings = saveControlSettings(payload || {});
    latestOverview = await computeOverview();
    sendJson(res, 200, { success: true, settings });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleStrategyNodeCatalog(res) {
  try {
    sendJson(res, 200, { manifests: listStrategyNodeCatalogEntries() });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleStrategyNodeRequestList(res, requestUrl) {
  try {
    const limit = Number.parseInt(requestUrl.searchParams.get('limit') || '100', 10);
    sendJson(res, 200, {
      requests: listStrategyNodeRequests(Number.isFinite(limit) ? limit : 100)
    });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleStrategyNodeRequestCreate(req, res) {
  try {
    const raw = await readBody(req);
    const payload = JSON.parse(raw || '{}');
    const request = createStrategyNodeRequest(payload);
    sendJson(res, 200, { success: true, request });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleTradingNodeStop(res, nodeId) {
  try {
    const snapshot = loadTradingNodeState();
    const node = findTradingNode(snapshot, nodeId);
    if (!node) {
      sendJson(res, 404, { error: `Unknown trading node: ${nodeId}` });
      return;
    }
    if (node.container?.exists !== false) {
      const result = spawnSync('docker', ['rm', '-f', node.containerName], { encoding: 'utf8' });
      if (result.status !== 0 && !isMissingDockerContainerError(result)) {
        sendJson(res, 500, { error: (result.stderr || result.stdout || 'docker rm failed').trim() });
        return;
      }
    }
    writeNodeStatusFile(node, {
      nodeId: node.nodeId,
      status: 'stopped',
      stoppedAt: new Date().toISOString(),
      manifestPath: node.manifestPath,
      renderedConfigPath: node.discovery?.status?.renderedConfigPath || null,
    });
    const registryEntry = upsertTradingNodeRegistryEntry(snapshot, {
      nodeId: node.nodeId,
      displayName: node.displayName,
      hostId: node.hostId,
      strategyId: node.strategyId,
      manifestId: node.manifestId,
      containerName: node.containerName,
      venues: node.venues,
      intendedState: 'stopped',
      lastAppliedConfig: node.discovery?.manifest || node.registry?.lastAppliedConfig || null,
      configOverrides: node.registry?.configOverrides || null,
      manifestPath: node.manifestPath,
      imageRef: node.imageRef,
      envFile: node.envFile,
      traderId: node.metadata?.traderId || null,
    });
    latestOverview = await computeOverview();
    sendJson(res, 200, { success: true, nodeId: node.nodeId, registryEntry });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleTradingNodeStartOrRestart(req, res, nodeId, action) {
  try {
    const snapshot = loadTradingNodeState();
    const node = findTradingNode(snapshot, nodeId);
    if (!node) {
      sendJson(res, 404, { error: `Unknown trading node: ${nodeId}` });
      return;
    }
    const baseManifest = node.discovery?.manifest || node.registry?.lastAppliedConfig || null;
    if (!baseManifest) {
      sendJson(res, 400, { error: 'No manifest is available for this node' });
      return;
    }
    const raw = await readBody(req);
    const payload = JSON.parse(raw || '{}');
    const { override, imageRef } = parseNodeOverridePayload(payload);
    const mergedManifest = mergeTradingNodeManifest(baseManifest, override);
    const nextImageRef = imageRef || node.imageRef || '';
    if (!nextImageRef) {
      sendJson(res, 400, { error: 'No imageRef is available for this node' });
      return;
    }
    const result = runDeployScriptForNode(node, mergedManifest, nextImageRef);
    if (result.status !== 0) {
      sendJson(res, 500, {
        error: (result.stderr || result.stdout || `${action} failed`).trim(),
      });
      return;
    }
    const registryEntry = upsertTradingNodeRegistryEntry(snapshot, {
      nodeId: node.nodeId,
      displayName: node.displayName,
      hostId: node.hostId,
      strategyId: node.strategyId,
      manifestId: node.manifestId,
      containerName: node.containerName,
      venues: mergedManifest.venues || node.venues,
      intendedState: 'running',
      lastAppliedConfig: mergedManifest,
      configOverrides: override,
      manifestPath: node.manifestPath,
      imageRef: nextImageRef,
      envFile: node.envFile,
      traderId: node.metadata?.traderId || null,
    });
    latestOverview = await computeOverview();
    sendJson(res, 200, {
      success: true,
      action,
      nodeId: node.nodeId,
      registryEntry,
      output: [result.stdout, result.stderr].filter(Boolean).join('\n').trim(),
    });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleTradingNodeList(res) {
  try {
    const snapshot = loadTradingNodeState();
    sendJson(res, 200, {
      hosts: snapshot.hosts,
      hostRegistry: snapshot.hostRegistry,
      nodeRegistry: snapshot.nodeRegistry,
      registry: snapshot.registry,
      discovery: snapshot.discovery,
      discoveries: snapshot.discoveries,
      nodes: snapshot.nodes,
      summary: summarizeTradingNodesSnapshot(snapshot),
    });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleTradingNodeDetail(res, nodeId) {
  try {
    const snapshot = loadTradingNodeState();
    const node = findTradingNode(snapshot, nodeId);
    if (!node) {
      sendJson(res, 404, { error: `Unknown trading node: ${nodeId}` });
      return;
    }
    sendJson(res, 200, {
      node,
      hosts: snapshot.hosts,
      hostRegistry: snapshot.hostRegistry,
      nodeRegistry: snapshot.nodeRegistry,
      registry: node.registry || null,
      registryEntry: node.registry || null,
      discovery: node.discovery || null,
      discoveryEntry: node.discovery || null,
      manifest: node.discovery?.manifest || node.registry?.lastAppliedConfig || null,
      effectiveConfig: node.registry?.lastAppliedConfig || node.discovery?.manifest || null,
      summary: summarizeTradingNodesSnapshot(snapshot),
      logs: readTradingNodeLogs({
        containerName: node.containerName,
        mode: 'recent',
        limit: 200,
      }),
    });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleTradingNodeLogs(res, nodeId, requestUrl) {
  try {
    const snapshot = loadTradingNodeState();
    const node = findTradingNode(snapshot, nodeId);
    if (!node) {
      sendJson(res, 404, { error: `Unknown trading node: ${nodeId}` });
      return;
    }
    const limit = Number.parseInt(requestUrl.searchParams.get('limit') || '200', 10);
    const mode = String(requestUrl.searchParams.get('mode') || 'recent').trim();
    const result = readTradingNodeLogs({
      containerName: node.containerName,
      mode,
      limit: Number.isFinite(limit) ? limit : 200,
      cursor: String(requestUrl.searchParams.get('cursor') || ''),
    });
    sendJson(res, 200, { nodeId: node.nodeId, ...result });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleTradingNodeRenderConfig(req, res, nodeId, requestUrl) {
  try {
    const snapshot = loadTradingNodeState();
    const node = findTradingNode(snapshot, nodeId);
    if (!node) {
      sendJson(res, 404, { error: `Unknown trading node: ${nodeId}` });
      return;
    }
    const payload =
      req.method === 'POST'
        ? JSON.parse((await readBody(req)) || '{}')
        : Object.fromEntries(requestUrl.searchParams.entries());
    const { override } = parseNodeOverridePayload(payload);
    const manifest = mergeTradingNodeManifest(
      node.discovery?.manifest || node.registry?.lastAppliedConfig || {},
      override,
    );
    const result = renderTradingNodeConfigPreview(manifest);
    sendJson(res, 200, {
      ok: Boolean(result.ok),
      nodeId: node.nodeId,
      containerName: node.containerName,
      manifest,
      ...result,
    });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleTradingNodeAction(req, res, nodeId, action) {
  try {
    if (action === 'stop') {
      await handleTradingNodeStop(res, nodeId);
      return;
    }
    await handleTradingNodeStartOrRestart(req, res, nodeId, action);
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

function handleEventStream(req, res, requestUrl) {
  const filters = parseTimelineFilters(requestUrl);
  let afterCursor = filters.afterCursor || '';
  let githubAfterCursor = requestUrl.searchParams.get('githubAfterCursor') || '';

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-store',
    Connection: 'keep-alive'
  });
  res.write(`event: ready\ndata: ${JSON.stringify({ ok: true, afterCursor })}\n\n`);

  const emit = () => {
    try {
      const events = readFilteredTimeline({ ...filters, afterCursor, limit: 200 });
      for (const event of events) {
        afterCursor = event.cursor || afterCursor;
        res.write(`event: timeline\ndata: ${JSON.stringify(event)}\n\n`);
      }
      const githubEvents = listGitHubEvents({ root: GITHUB_STATE_ROOT, afterCursor: githubAfterCursor, limit: 200 });
      for (const event of githubEvents) {
        githubAfterCursor = event.cursor || githubAfterCursor;
        res.write(`event: github_actions\ndata: ${JSON.stringify(event)}\n\n`);
      }
      res.write(`event: heartbeat\ndata: ${JSON.stringify({ at: new Date().toISOString() })}\n\n`);
    } catch (error) {
      res.write(`event: error\ndata: ${JSON.stringify({ error: error.message })}\n\n`);
    }
  };

  emit();
  const timer = setInterval(emit, EVENT_STREAM_INTERVAL_MS);
  req.on('close', () => {
    clearInterval(timer);
  });
}

async function handleOverview(res) {
  try {
    latestOverview = await computeOverview();
    sendJson(res, 200, latestOverview);
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

function findGitHubJobsForIssue(issue) {
  const prInfo = parseGitHubPr(issue.attachments?.nodes || []);
  if (prInfo?.number) {
    return listGitHubJobs({
      root: GITHUB_STATE_ROOT,
      pullRequestNumber: prInfo.number,
      limit: 20
    }).map((job) => summarizeGitHubJob(job));
  }
  return [];
}

async function handleGitHubActions(res, requestUrl) {
  try {
    const pullRequestNumber = Number(requestUrl.searchParams.get('pr') || 0);
    const limit = Number(requestUrl.searchParams.get('limit') || 30);
    const jobs = listGitHubJobs({
      root: GITHUB_STATE_ROOT,
      pullRequestNumber,
      limit
    }).map((job) => summarizeGitHubJob(job));
    const activeJobs = listGitHubJobs({
      root: GITHUB_STATE_ROOT,
      pullRequestNumber,
      activeOnly: true,
      limit
    }).map((job) => summarizeGitHubJob(job));
    const events = listGitHubEvents({
      root: GITHUB_STATE_ROOT,
      limit: Number(requestUrl.searchParams.get('eventLimit') || 60)
    });
    sendJson(res, 200, {
      repo: parseRepoSlug()?.fullName || '',
      webhook: getGitHubWebhookConfig(getSecretJson()),
      activeJobs,
      recentJobs: jobs,
      recentEvents: events
    });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleGitHubJobDetail(res, jobId) {
  try {
    const job = loadGitHubJob(jobId, GITHUB_STATE_ROOT);
    if (!job) {
      sendJson(res, 404, { error: `Unknown GitHub job: ${jobId}` });
      return;
    }
    sendJson(res, 200, {
      job: summarizeGitHubJob(job),
      events: listGitHubEvents({ root: GITHUB_STATE_ROOT, jobId, limit: 200 }),
      logStreams: listGitHubJobLogStreams(jobId, GITHUB_STATE_ROOT)
    });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleGitHubJobLogChunk(res, jobId, streamId, requestUrl) {
  try {
    const chunk = readGitHubJobLogChunk(jobId, streamId, {
      root: GITHUB_STATE_ROOT,
      offset: Number(requestUrl.searchParams.get('offset') || 0),
      limit: Number(requestUrl.searchParams.get('limit') || 64 * 1024)
    });
    sendJson(res, 200, chunk);
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleIssueDetail(res, identifier) {
  try {
    const issue = await fetchIssueDetail(identifier);
    const workpadComment = issue.comments.nodes.find((comment) => comment.body?.includes('## Codex Workpad'));
    const github = await fetchGitHubPrSummary(parseGitHubPr(issue.attachments?.nodes || []));
    const session = (latestOverview.issues || []).find((row) => row.identifier === issue.identifier)?.running || null;
    const runs = listRuns({
      runRoot: RUN_ROOT,
      issueIdentifier: issue.identifier,
      limit: 20
    }).map((run) => summarizeRun(run));
    sendJson(res, 200, {
      issue,
      comments: issue.comments.nodes,
      workpad: summarizeWorkpad(workpadComment?.body || ''),
      github,
      githubLiveJobs: findGitHubJobsForIssue(issue),
      session,
      runs
    });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleIssueComment(req, res, identifier) {
  try {
    const raw = await readBody(req);
    const { body, stateName } = JSON.parse(raw || '{}');
    if (!body || !body.trim()) {
      sendJson(res, 400, { error: 'Comment body is required' });
      return;
    }

    const issue = await fetchIssueDetail(identifier);
    await createIssueComment(issue.id, body.trim());

    if (stateName) {
      const teamStates = await fetchTeamStates();
      const state = teamStates.stateByName[stateName];
      if (!state) {
        sendJson(res, 400, { error: `Unknown state: ${stateName}` });
        return;
      }
      await updateIssueState(issue.id, state.id);
    }

    sendJson(res, 200, { success: true });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleIssueState(req, res, identifier) {
  try {
    const raw = await readBody(req);
    const { stateName } = JSON.parse(raw || '{}');
    if (!stateName) {
      sendJson(res, 400, { error: 'stateName is required' });
      return;
    }

    const teamStates = await fetchTeamStates();
    const state = teamStates.stateByName[stateName];
    if (!state) {
      sendJson(res, 400, { error: `Unknown state: ${stateName}` });
      return;
    }

    const issue = await fetchIssueDetail(identifier);
    await updateIssueState(issue.id, state.id);
    sendJson(res, 200, { success: true });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleWorkerAction(req, res, workerName) {
  try {
    const raw = await readBody(req);
    const { action } = JSON.parse(raw || '{}');
    const worker = readWorkers().find((entry) => entry.name === workerName);
    if (!worker) {
      sendJson(res, 404, { error: `Unknown worker: ${workerName}` });
      return;
    }

    const stateFile = getWorkerStatePath(workerName);
    const state = readJson(stateFile, {});
    const next = { ...state };
    const authPath = `/home/${worker.user}/.codex/auth.json`;
    const authPresent = sudoFileExists(authPath);
    switch (action) {
      case 'cordon':
        next.cordoned = true;
        next.status = next.status === 'busy' ? 'busy' : 'cordoned';
        break;
      case 'resume':
        next.cordoned = false;
        next.cooldownUntilEpoch = 0;
        if (next.status !== 'busy') {
          next.status = authPresent ? 'idle' : 'missing_auth';
        }
        break;
      case 'clearCooldown':
        next.cordoned = false;
        next.cooldownUntilEpoch = 0;
        if (next.status !== 'busy') {
          next.status = authPresent ? 'idle' : 'missing_auth';
        }
        break;
      default:
        sendJson(res, 400, { error: `Unknown worker action: ${action}` });
        return;
    }
    writeJson(stateFile, next);
    sendJson(res, 200, { success: true });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleWorkerProviderProfile(req, res, providerId, workerName) {
  try {
    const worker = getWorkerByName(workerName);
    if (!worker) {
      sendJson(res, 404, { error: `Unknown worker: ${workerName}` });
      return;
    }

    const raw = await readBody(req);
    const payload = JSON.parse(raw || '{}');
    const result = saveWorkerProviderProfile(workerName, providerId, payload || {});
    latestOverview = await computeOverview();
    sendJson(res, 200, { success: true, result });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleProviderCodexWorkerAction(req, res, workerName) {
  try {
    const raw = await readBody(req);
    const { action } = JSON.parse(raw || '{}');
    const worker = getWorkerByName(workerName);
    if (!worker) {
      sendJson(res, 404, { error: `Unknown worker: ${workerName}` });
      return;
    }

    let result;
    if (action === 'startAuth') {
      result = startCodexAuthSession(workerName);
    } else if (action === 'persist') {
      result = persistCodexWorkerAuth(worker);
    } else if (action === 'restore') {
      result = restoreCodexWorkerAuth(worker);
    } else {
      sendJson(res, 400, { error: `Unknown codex worker action: ${action}` });
      return;
    }
    latestOverview = await computeOverview();
    sendJson(res, 200, { success: true, result });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleProviderOpenRouter(req, res) {
  try {
    const raw = await readBody(req);
    const { apiKey } = JSON.parse(raw || '{}');
    const result = saveOpenRouterSecret(apiKey);
    latestOverview = await computeOverview();
    sendJson(res, 200, { success: true, result });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleProviderAntigravity(req, res) {
  try {
    const raw = await readBody(req);
    const payload = JSON.parse(raw || '{}');
    const result = saveAntigravityConfig(payload || {});
    latestOverview = await computeOverview();
    sendJson(res, 200, { success: true, result });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleProviderAntigravityWorkerAction(req, res, workerName) {
  try {
    const raw = await readBody(req);
    const { action, callbackInput } = JSON.parse(raw || '{}');
    const worker = getWorkerByName(workerName);
    if (!worker) {
      sendJson(res, 404, { error: `Unknown worker: ${workerName}` });
      return;
    }

    let result;
    if (action === 'startAuth') {
      result = startAntigravityAuthSession(workerName, req);
    } else if (action === 'completeAuth') {
      const session = readAuthSession('antigravity', worker.name);
      if (!session) {
        sendJson(res, 404, { error: `No pending Antigravity auth session for ${workerName}` });
        return;
      }
      const parsed = extractGoogleCodeFromInput(callbackInput);
      if (parsed.state && session.state && parsed.state !== session.state) {
        throw new Error(`OAuth state mismatch for ${workerName}`);
      }
      result = await completeAntigravityAuthSession(session, parsed.code, 'manual-dashboard-complete');
    } else if (action === 'persist') {
      result = persistAntigravityWorkerAuthFromDisk(worker);
    } else if (action === 'restore') {
      result = restoreAntigravityWorkerAuth(worker);
    } else {
      sendJson(res, 400, { error: `Unknown antigravity worker action: ${action}` });
      return;
    }
    latestOverview = await computeOverview();
    sendJson(res, 200, { success: true, result });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

function renderOAuthCallbackPage({ success, title, body }) {
  return `<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>${title}</title>
    <style>
      body { margin: 0; font-family: system-ui, sans-serif; background: #f3f0e8; color: #1e1b16; }
      main { max-width: 720px; margin: 0 auto; padding: 40px 20px; }
      article { background: #fffdf8; border: 1px solid #d8cfbf; border-radius: 18px; padding: 24px; box-shadow: 0 12px 30px rgba(49,37,19,.08); }
      h1 { margin-top: 0; color: ${success ? '#027a48' : '#b42318'}; }
      code, pre { background: #f8f5ef; border-radius: 8px; padding: 2px 6px; }
      pre { white-space: pre-wrap; padding: 12px; }
      a { color: #0f766e; }
    </style>
  </head>
  <body>
    <main>
      <article>
        <h1>${title}</h1>
        ${body}
      </article>
    </main>
  </body>
</html>`;
}

async function handleProviderAntigravityCallback(req, res) {
  try {
    const requestUrl = new URL(req.url || '/', `http://${req.headers.host || '127.0.0.1'}`);
    const state = requestUrl.searchParams.get('state') || '';
    const code = requestUrl.searchParams.get('code') || '';
    const error = requestUrl.searchParams.get('error') || '';
    const session = findAuthSessionByState('antigravity', state);
    if (!session) {
      sendText(
        res,
        400,
        renderOAuthCallbackPage({
          success: false,
          title: 'Antigravity Authentication Failed',
          body: '<p>No matching auth session was found for this callback. Generate a new auth link from the dashboard and try again.</p>'
        }),
        'text/html; charset=utf-8'
      );
      return;
    }
    if (error) {
      writeAuthSession('antigravity', session.worker, {
        ...session,
        status: 'failed',
        completedAt: new Date().toISOString(),
        error: `OAuth error: ${error}`
      });
      sendText(
        res,
        400,
        renderOAuthCallbackPage({
          success: false,
          title: 'Antigravity Authentication Failed',
          body: `<p>Google returned an OAuth error: <code>${error}</code>.</p><p>Return to the dashboard, generate a fresh link, and try again.</p>`
        }),
        'text/html; charset=utf-8'
      );
      return;
    }
    if (!code) {
      throw new Error('OAuth callback did not contain an authorization code');
    }
    const result = await completeAntigravityAuthSession(session, code, 'oauth-callback');
    latestOverview = await computeOverview();
    sendText(
      res,
      200,
      renderOAuthCallbackPage({
        success: true,
        title: 'Antigravity Authentication Complete',
        body: `<p>Worker <code>${session.worker}</code> is now configured for <code>${result.persisted.email}</code>.</p>
<p>Project: <code>${result.persisted.projectId || 'pending discovery'}</code></p>
<p>You can close this window and return to the dashboard.</p>`
      }),
      'text/html; charset=utf-8'
    );
  } catch (error) {
    const requestUrl = new URL(req.url || '/', `http://${req.headers.host || '127.0.0.1'}`);
    const state = requestUrl.searchParams.get('state') || '';
    const session = findAuthSessionByState('antigravity', state);
    if (session) {
      writeAuthSession('antigravity', session.worker, {
        ...session,
        status: 'failed',
        completedAt: new Date().toISOString(),
        error: error.message
      });
    }
    sendText(
      res,
      500,
      renderOAuthCallbackPage({
        success: false,
        title: 'Antigravity Authentication Failed',
        body: `<p>${error.message}</p><p>You can copy the full callback URL from your browser and complete the session manually from the dashboard if needed.</p>`
      }),
      'text/html; charset=utf-8'
    );
  }
}

async function ensureGitHubWebhook(req) {
  const repo = parseRepoSlug();
  if (!repo) {
    throw new Error('GITHUB_REPO is not configured');
  }
  ensureGitHubWebhookSecret();
  const config = getGitHubWebhookConfig(getSecretJson(), req);
  if (!config.ready) {
    throw new Error(config.warnings.join(' '));
  }

  const base = `https://api.github.com/repos/${repo.owner}/${repo.repo}/hooks`;
  const hooks = await githubRequest(`${base}?per_page=100`);
  const existing = Array.isArray(hooks)
    ? hooks.find((hook) => hook?.name === 'web' && hook?.config?.url === config.webhookUrl)
    : null;
  const body = {
    name: 'web',
    active: true,
    events: ['workflow_job'],
    config: {
      url: config.webhookUrl,
      content_type: 'json',
      secret: config.secret,
      insecure_ssl: '0'
    }
  };

  if (existing?.id) {
    await githubRequest(`${base}/${existing.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    return {
      action: 'updated',
      hookId: existing.id,
      webhookUrl: config.webhookUrl,
      events: body.events
    };
  }

  const created = await githubRequest(base, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  return {
    action: 'created',
    hookId: created?.id || null,
    webhookUrl: config.webhookUrl,
    events: body.events
  };
}

async function handleGitHubWebhookEnsure(req, res) {
  try {
    const result = await ensureGitHubWebhook(req);
    latestOverview = await computeOverview();
    sendJson(res, 200, { success: true, result });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

async function handleGitHubWebhook(req, res) {
  try {
    const rawBody = await readBody(req);
    const config = getGitHubWebhookConfig(getSecretJson(), req);
    const signature = String(req.headers['x-hub-signature-256'] || '');
    if (!verifyGitHubWebhookSignature(rawBody, signature, config.secret)) {
      sendJson(res, 401, { error: 'Invalid GitHub webhook signature' });
      return;
    }

    const eventName = String(req.headers['x-github-event'] || '');
    const deliveryId = String(req.headers['x-github-delivery'] || '');
    let payload = {};
    try {
      payload = JSON.parse(rawBody || '{}');
    } catch {
      sendJson(res, 400, { error: 'Webhook payload is not valid JSON' });
      return;
    }

    if (eventName === 'ping') {
      appendGitHubEvent({
        eventType: 'github.webhook.ping',
        level: 'info',
        summary: 'Received GitHub webhook ping',
        source: 'github-webhook',
        repoFullName: payload?.repository?.full_name || parseRepoSlug()?.fullName || '',
        payload: { deliveryId }
      }, GITHUB_STATE_ROOT);
      sendJson(res, 202, { ok: true, event: 'ping' });
      return;
    }

    if (eventName !== 'workflow_job' || !payload?.workflow_job) {
      sendJson(res, 202, { ok: true, ignored: true, event: eventName });
      return;
    }

    const repoFullName = payload.repository?.full_name || parseRepoSlug()?.fullName || '';
    const [owner, repo] = repoFullName.split('/');
    let runPayload = null;
    if (payload.workflow_job.run_id && owner && repo) {
      runPayload = await fetchGitHubRun(owner, repo, payload.workflow_job.run_id);
    }
    const job = await upsertGitHubJobSnapshot(payload.workflow_job, runPayload, 'github-webhook');
    appendGitHubEvent({
      eventType: 'github.webhook.received',
      level: 'info',
      summary: `Received workflow_job webhook (${payload.action || 'unknown'}) for ${job.name || job.jobId}`,
      source: 'github-webhook',
      repoFullName: repoFullName,
      jobId: job.jobId,
      runId: job.runId,
      workflowName: job.workflowName,
      jobName: job.name,
      payload: {
        action: payload.action || '',
        deliveryId
      }
    }, GITHUB_STATE_ROOT);
    refreshGitHubObserverState(job);
    latestOverview = await computeOverview();
    sendJson(res, 202, { ok: true, action: payload.action || '', jobId: job.jobId });
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
}

const server = http.createServer(async (req, res) => {
  const requestUrl = new URL(req.url || '/', `http://${req.headers.host || '127.0.0.1'}`);
  const pathname = requestUrl.pathname;

  if (CONTROL_PLANE_READ_ONLY && !READ_ONLY_SAFE_METHODS.has(req.method || '')) {
    const renderConfigAllowed =
      req.method === 'POST' && /^\/control\/api\/nodes\/[^/]+\/render-config$/.test(pathname);
    if (!renderConfigAllowed) {
      sendJson(res, 403, {
        error: 'control-plane is read-only in this environment',
        method: req.method,
        path: pathname
      });
      return;
    }
  }

  if (req.method === 'GET' && pathname === '/') {
    sendText(res, 200, renderHtml(), 'text/html; charset=utf-8');
    return;
  }

  if (req.method === 'GET' && pathname === '/control.css') {
    if (!sendStaticFile(res, '/control.css')) {
      sendJson(res, 404, { error: 'control.css not found' });
    }
    return;
  }

  if (req.method === 'GET' && pathname === '/app.js') {
    if (!sendStaticFile(res, '/app.js')) {
      sendJson(res, 404, { error: 'app.js not found' });
    }
    return;
  }

  if (req.method === 'GET' && (pathname.startsWith('/assets/') || pathname === '/favicon.ico')) {
    if (!sendStaticFile(res, pathname)) {
      sendJson(res, 404, { error: 'Static asset not found' });
    }
    return;
  }

  if (req.method === 'GET' && pathname === '/control/api/overview') {
    await handleOverview(res);
    return;
  }

  if (req.method === 'GET' && pathname === '/control/api/github/actions') {
    await handleGitHubActions(res, requestUrl);
    return;
  }

  if (req.method === 'POST' && pathname === '/control/api/github/actions/webhook/ensure') {
    await handleGitHubWebhookEnsure(req, res);
    return;
  }

  if (req.method === 'POST' && pathname === '/control/api/github/webhooks') {
    await handleGitHubWebhook(req, res);
    return;
  }

  if (req.method === 'GET' && pathname === '/control/api/settings') {
    await handleSettingsGet(res);
    return;
  }

  if (req.method === 'POST' && pathname === '/control/api/settings') {
    await handleSettingsPost(req, res);
    return;
  }

  if (req.method === 'GET' && pathname === '/control/api/deployments/catalog') {
    await handleStrategyNodeCatalog(res);
    return;
  }

  if (req.method === 'GET' && pathname === '/control/api/deployments/requests') {
    await handleStrategyNodeRequestList(res, requestUrl);
    return;
  }

  if (req.method === 'POST' && pathname === '/control/api/deployments/requests') {
    await handleStrategyNodeRequestCreate(req, res);
    return;
  }

  if (req.method === 'GET' && pathname === '/control/api/nodes') {
    await handleTradingNodeList(res);
    return;
  }

  const tradingNodeLogsMatch = pathname.match(/^\/control\/api\/nodes\/([^/]+)\/logs$/);
  if (req.method === 'GET' && tradingNodeLogsMatch) {
    await handleTradingNodeLogs(res, decodeURIComponent(tradingNodeLogsMatch[1]), requestUrl);
    return;
  }

  const tradingNodeRenderConfigMatch = pathname.match(/^\/control\/api\/nodes\/([^/]+)\/render-config$/);
  if ((req.method === 'GET' || req.method === 'POST') && tradingNodeRenderConfigMatch) {
    await handleTradingNodeRenderConfig(req, res, decodeURIComponent(tradingNodeRenderConfigMatch[1]), requestUrl);
    return;
  }

  const tradingNodeActionMatch = pathname.match(/^\/control\/api\/nodes\/([^/]+)\/(start|stop|restart)$/);
  if (req.method === 'POST' && tradingNodeActionMatch) {
    await handleTradingNodeAction(
      req,
      res,
      decodeURIComponent(tradingNodeActionMatch[1]),
      tradingNodeActionMatch[2]
    );
    return;
  }

  const tradingNodeDetailMatch = pathname.match(/^\/control\/api\/nodes\/([^/]+)$/);
  if (req.method === 'GET' && tradingNodeDetailMatch) {
    await handleTradingNodeDetail(res, decodeURIComponent(tradingNodeDetailMatch[1]));
    return;
  }

  if (req.method === 'GET' && pathname === '/control/api/timeline') {
    await handleTimeline(res, requestUrl);
    return;
  }

  if (req.method === 'GET' && pathname === '/control/api/runs') {
    await handleRunList(res, requestUrl);
    return;
  }

  if (req.method === 'GET' && pathname === '/control/api/stream') {
    handleEventStream(req, res, requestUrl);
    return;
  }

  const githubJobMatch = pathname.match(/^\/control\/api\/github\/actions\/jobs\/(\d+)$/);
  if (req.method === 'GET' && githubJobMatch) {
    await handleGitHubJobDetail(res, Number(githubJobMatch[1]));
    return;
  }

  const githubJobLogMatch = pathname.match(/^\/control\/api\/github\/actions\/jobs\/(\d+)\/logs\/([^/]+)$/);
  if (req.method === 'GET' && githubJobLogMatch) {
    await handleGitHubJobLogChunk(res, Number(githubJobLogMatch[1]), decodeURIComponent(githubJobLogMatch[2]), requestUrl);
    return;
  }

  const issueMatch = pathname.match(/^\/control\/api\/issues\/([^/]+)$/);
  if (req.method === 'GET' && issueMatch) {
    await handleIssueDetail(res, decodeURIComponent(issueMatch[1]));
    return;
  }

  const issueTimelineMatch = pathname.match(/^\/control\/api\/issues\/([^/]+)\/timeline$/);
  if (req.method === 'GET' && issueTimelineMatch) {
    const issueIdentifier = decodeURIComponent(issueTimelineMatch[1]);
    await handleTimeline(res, new URL(`${requestUrl.origin}/control/api/timeline?issue=${encodeURIComponent(issueIdentifier)}&limit=${requestUrl.searchParams.get('limit') || '400'}`));
    return;
  }

  const issueCommentMatch = pathname.match(/^\/control\/api\/issues\/([^/]+)\/comment$/);
  if (req.method === 'POST' && issueCommentMatch) {
    await handleIssueComment(req, res, decodeURIComponent(issueCommentMatch[1]));
    return;
  }

  const issueStateMatch = pathname.match(/^\/control\/api\/issues\/([^/]+)\/state$/);
  if (req.method === 'POST' && issueStateMatch) {
    await handleIssueState(req, res, decodeURIComponent(issueStateMatch[1]));
    return;
  }

  const workerActionMatch = pathname.match(/^\/control\/api\/workers\/([^/]+)\/action$/);
  if (req.method === 'POST' && workerActionMatch) {
    await handleWorkerAction(req, res, decodeURIComponent(workerActionMatch[1]));
    return;
  }

  const workerTimelineMatch = pathname.match(/^\/control\/api\/workers\/([^/]+)\/timeline$/);
  if (req.method === 'GET' && workerTimelineMatch) {
    const workerName = decodeURIComponent(workerTimelineMatch[1]);
    await handleTimeline(res, new URL(`${requestUrl.origin}/control/api/timeline?worker=${encodeURIComponent(workerName)}&limit=${requestUrl.searchParams.get('limit') || '400'}`));
    return;
  }

  const runMatch = pathname.match(/^\/control\/api\/runs\/([^/]+)$/);
  if (req.method === 'GET' && runMatch) {
    await handleRunDetail(res, decodeURIComponent(runMatch[1]));
    return;
  }

  const runPromptsMatch = pathname.match(/^\/control\/api\/runs\/([^/]+)\/prompts$/);
  if (req.method === 'GET' && runPromptsMatch) {
    await handleRunPromptList(res, decodeURIComponent(runPromptsMatch[1]));
    return;
  }
  if (req.method === 'POST' && runPromptsMatch) {
    await handleRunPromptQueue(req, res, decodeURIComponent(runPromptsMatch[1]));
    return;
  }

  const runInterruptMatch = pathname.match(/^\/control\/api\/runs\/([^/]+)\/interrupt$/);
  if (req.method === 'POST' && runInterruptMatch) {
    await handleRunInterrupt(req, res, decodeURIComponent(runInterruptMatch[1]));
    return;
  }

  const runCheckpointMatch = pathname.match(/^\/control\/api\/runs\/([^/]+)\/checkpoint$/);
  if (req.method === 'POST' && runCheckpointMatch) {
    await handleRunCheckpoint(req, res, decodeURIComponent(runCheckpointMatch[1]));
    return;
  }

  const runLogChunkMatch = pathname.match(/^\/control\/api\/runs\/([^/]+)\/logs\/([^/]+)$/);
  if (req.method === 'GET' && runLogChunkMatch) {
    await handleRunLogChunk(res, decodeURIComponent(runLogChunkMatch[1]), decodeURIComponent(runLogChunkMatch[2]), requestUrl);
    return;
  }

  const workerProfileMatch = pathname.match(/^\/control\/api\/providers\/(codex|antigravity)\/workers\/([^/]+)\/profile$/);
  if (req.method === 'POST' && workerProfileMatch) {
    await handleWorkerProviderProfile(req, res, decodeURIComponent(workerProfileMatch[1]), decodeURIComponent(workerProfileMatch[2]));
    return;
  }

  const codexWorkerActionMatch = pathname.match(/^\/control\/api\/providers\/codex\/workers\/([^/]+)\/action$/);
  if (req.method === 'POST' && codexWorkerActionMatch) {
    await handleProviderCodexWorkerAction(req, res, decodeURIComponent(codexWorkerActionMatch[1]));
    return;
  }

  if (req.method === 'POST' && pathname === '/control/api/providers/openrouter') {
    await handleProviderOpenRouter(req, res);
    return;
  }

  if (req.method === 'POST' && pathname === '/control/api/providers/antigravity') {
    await handleProviderAntigravity(req, res);
    return;
  }

  if (req.method === 'GET' && pathname === '/control/api/providers/antigravity/oauth/callback') {
    await handleProviderAntigravityCallback(req, res);
    return;
  }

  const antigravityWorkerActionMatch = pathname.match(/^\/control\/api\/providers\/antigravity\/workers\/([^/]+)\/action$/);
  if (req.method === 'POST' && antigravityWorkerActionMatch) {
    await handleProviderAntigravityWorkerAction(req, res, decodeURIComponent(antigravityWorkerActionMatch[1]));
    return;
  }

  if (req.method === 'GET' && !pathname.startsWith('/control/api/')) {
    sendText(res, 200, renderHtml(), 'text/html; charset=utf-8');
    return;
  }

  sendJson(res, 404, { error: 'Not found' });
});

server.listen(PORT, '127.0.0.1', async () => {
  console.error(`[control-plane] listening on 127.0.0.1:${PORT}`);
  if (CONTROL_PLANE_READ_ONLY) {
    console.error('[control-plane] READ_ONLY mode enabled (all POST/PUT/PATCH/DELETE requests return 403)');
  }
  await refreshOverview();
});

setInterval(() => {
  refreshGitHubActions().catch((error) => {
    console.error(`[control-plane] github monitor failed: ${error.message}`);
  });
}, Math.min(GITHUB_DISCOVERY_INTERVAL_MS, GITHUB_STEP_POLL_INTERVAL_MS));

setInterval(() => {
  refreshOverview().catch((error) => {
    console.error(`[control-plane] refresh failed: ${error.message}`);
  });
}, RECONCILE_INTERVAL_MS);
