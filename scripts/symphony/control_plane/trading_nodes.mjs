import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

export const DEFAULT_STALE_HEARTBEAT_MS = 120_000;

function readJson(filePath, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch {
    return fallback;
  }
}

function writeJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmp = `${filePath}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2));
  fs.renameSync(tmp, filePath);
}

function readText(filePath) {
  try {
    return fs.readFileSync(filePath, 'utf8').trim();
  } catch {
    return '';
  }
}

function deepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

function runCommand(command, args, options = {}) {
  const result = spawnSync(command, args, {
    encoding: 'utf8',
    ...options,
  });
  if (result.error) {
    return { ok: false, stdout: '', stderr: result.error.message, status: -1 };
  }
  return {
    ok: result.status === 0,
    stdout: result.stdout || '',
    stderr: result.stderr || '',
    status: result.status ?? 0,
  };
}

function inspectDockerContainer(name) {
  const result = runCommand('docker', ['inspect', name]);
  if (!result.ok || !result.stdout.trim()) {
    return null;
  }
  const payload = readJsonBuffer(result.stdout, null);
  if (!Array.isArray(payload) || !payload.length) {
    return null;
  }
  const item = payload[0] || {};
  return {
    name,
    image: item?.Config?.Image || '',
    status: item?.State?.Status || '',
    running: Boolean(item?.State?.Running),
    startedAt: item?.State?.StartedAt || null,
    finishedAt: item?.State?.FinishedAt || null,
    exitCode: Number.isFinite(item?.State?.ExitCode) ? item.State.ExitCode : null,
    createdAt: item?.Created || null,
    command: Array.isArray(item?.Config?.Cmd) ? item.Config.Cmd.join(' ') : '',
  };
}

function readJsonBuffer(value, fallback = null) {
  try {
    return JSON.parse(value);
  } catch {
    return fallback;
  }
}

function isTradingNodeContainer(summary = {}) {
  const name = String(summary.name || summary.Names || '').toLowerCase();
  const image = String(summary.image || summary.Image || '').toLowerCase();
  const command = String(summary.command || '').toLowerCase();
  return (
    name.includes('betting-arbitrage-node') ||
    image.includes('betting-arbitrage-node') ||
    command.includes('nautilus_trader.live.strategy_nodes.betting_arbitrage')
  );
}

function listDockerContainers() {
  const result = runCommand('docker', ['ps', '-a', '--format', '{{json .}}']);
  if (!result.ok) {
    return [];
  }
  return result.stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => readJsonBuffer(line, null))
    .filter(Boolean)
    .map((row) => ({
      name: row.Names || '',
      image: row.Image || '',
      statusText: row.Status || '',
    }));
}

function normalizeVenue(venue = {}) {
  return {
    venue: String(venue.venue || ''),
    clientKey: String(venue.client_key || venue.clientKey || ''),
    dataEnabled: venue.data_enabled !== false && venue.dataEnabled !== false,
    executionEnabled: venue.execution_enabled === true || venue.executionEnabled === true,
  };
}

function computeStateClass({ registryEntry, discoveryEntry, staleHeartbeatMs }) {
  if (!discoveryEntry && registryEntry) {
    return registryEntry.intendedState === 'stopped' ? 'managed' : 'missing-container';
  }
  if (!discoveryEntry) {
    return 'unknown';
  }
  if (!registryEntry) {
    if (discoveryEntry.isHeartbeatStale) return 'stale-heartbeat';
    return 'discovered-unmanaged';
  }
  if (!discoveryEntry.container.exists) {
    return registryEntry.intendedState === 'stopped' ? 'managed' : 'missing-container';
  }
  if (discoveryEntry.isHeartbeatStale) {
    return 'stale-heartbeat';
  }
  return 'managed';
}

export function defaultLocalHostRecord({ hostId = 'local-ec2', rootDir = '/opt/cloudbet/strategy-nodes', displayName = 'EC2 trading host' } = {}) {
  return {
    hostId,
    displayName,
    kind: 'local',
    labels: ['trading', 'strategy-nodes', 'ec2', 'local'],
    enabled: true,
    rootDir,
    executor: { kind: 'local' },
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
}

export function readTradingHostRegistry(filePath, defaultHost) {
  const payload = readJson(filePath, { version: 1, hosts: [] }) || { version: 1, hosts: [] };
  const hosts = Array.isArray(payload.hosts) ? payload.hosts.map((host) => ({ ...host })) : [];
  if (defaultHost && !hosts.some((host) => host.hostId === defaultHost.hostId)) {
    hosts.unshift(defaultHost);
  }
  return {
    version: 1,
    updatedAt: payload.updatedAt || null,
    hosts,
  };
}

export function writeTradingHostRegistry(filePath, payload) {
  writeJson(filePath, {
    version: 1,
    updatedAt: new Date().toISOString(),
    hosts: Array.isArray(payload?.hosts) ? payload.hosts : [],
  });
}

export function readTradingNodeRegistry(filePath) {
  const payload = readJson(filePath, { version: 1, nodes: [] }) || { version: 1, nodes: [] };
  return {
    version: 1,
    updatedAt: payload.updatedAt || null,
    nodes: Array.isArray(payload.nodes) ? payload.nodes.map((node) => ({ ...node })) : [],
  };
}

export function writeTradingNodeRegistry(filePath, payload) {
  writeJson(filePath, {
    version: 1,
    updatedAt: new Date().toISOString(),
    nodes: Array.isArray(payload?.nodes) ? payload.nodes : [],
  });
}

export function writeTradingNodeDiscoverySnapshot(filePath, payload) {
  writeJson(filePath, {
    version: 1,
    updatedAt: new Date().toISOString(),
    ...payload,
  });
}

export function discoverLocalTradingNodes({ hostId = 'local-ec2', rootDir = '/opt/cloudbet/strategy-nodes', staleHeartbeatMs = DEFAULT_STALE_HEARTBEAT_MS } = {}) {
  const dirNames = fs.existsSync(rootDir)
    ? fs.readdirSync(rootDir, { withFileTypes: true }).filter((entry) => entry.isDirectory()).map((entry) => entry.name)
    : [];

  const dockerContainers = listDockerContainers();
  const relevantContainers = dockerContainers.filter((container) => isTradingNodeContainer(container));
  const candidateNames = new Set([...dirNames, ...relevantContainers.map((container) => container.name)]);
  const discoveredAt = new Date().toISOString();

  const nodes = Array.from(candidateNames)
    .sort((a, b) => a.localeCompare(b))
    .map((containerName) => {
      const nodeDir = path.join(rootDir, containerName);
      const hasNodeDir = fs.existsSync(nodeDir) && fs.statSync(nodeDir).isDirectory();
      const statusPath = path.join(nodeDir, 'status.json');
      const heartbeatPath = path.join(nodeDir, 'heartbeat.json');
      const releasePath = path.join(nodeDir, 'release.json');
      const manifestPath = path.join(nodeDir, 'manifest.runtime.json');
      const currentImagePath = path.join(nodeDir, 'current-image.txt');
      const previousImagePath = path.join(nodeDir, 'previous-image.txt');
      const status = readJson(statusPath, null);
      const heartbeat = readJson(heartbeatPath, null);
      const release = readJson(releasePath, null);
      const manifest = readJson(manifestPath, null);
      const container = inspectDockerContainer(containerName) || {
        name: containerName,
        image: '',
        status: '',
        running: false,
        startedAt: null,
        finishedAt: null,
        exitCode: null,
        createdAt: null,
        command: '',
      };
      container.exists = Boolean(inspectDockerContainer(containerName));
      const heartbeatAt = heartbeat?.at || status?.startedAt || status?.at || null;
      const heartbeatMs = heartbeatAt ? Date.parse(heartbeatAt) : NaN;
      const isHeartbeatStale = Boolean(
        heartbeatAt && Number.isFinite(heartbeatMs) && Date.now() - heartbeatMs > staleHeartbeatMs
      );
      const manifestVenues = Array.isArray(manifest?.venues) ? manifest.venues.map(normalizeVenue) : [];
      const currentImage = readText(currentImagePath) || release?.image || container.image || '';
      return {
        hostId,
        rootDir,
        discoveredAt,
        nodeDir,
        containerName,
        nodeId: status?.nodeId || manifest?.node_id || containerName,
        displayName: manifest?.node_id || status?.nodeId || containerName,
        strategyId: 'betting-arbitrage',
        traderId: manifest?.trader_id || null,
        manifestId: manifest?.node_id || null,
        venues: manifestVenues,
        logLevel: manifest?.log_level || null,
        validationMode: manifest?.validation_mode !== false,
        currentImage,
        previousImage: readText(previousImagePath),
        status,
        heartbeat,
        heartbeatAt,
        isHeartbeatStale,
        release,
        manifest,
        container,
        paths: {
          nodeDir,
          statusPath,
          heartbeatPath,
          releasePath,
          manifestPath,
          currentImagePath,
          previousImagePath,
        },
        hasNodeDir,
      };
    });

  return {
    hostId,
    rootDir,
    discoveredAt,
    nodes,
  };
}

function nodeMergeKey(node = {}) {
  return String(node.nodeId || node.containerName || '').trim();
}

export function buildEffectiveTradingNodes({ hosts = [], registry = { nodes: [] }, discoveries = [] } = {}) {
  const registryNodes = Array.isArray(registry?.nodes) ? registry.nodes : [];
  const discoveryNodes = Array.isArray(discoveries)
    ? discoveries.flatMap((discovery) => Array.isArray(discovery?.nodes) ? discovery.nodes : [])
    : [];
  const discoveryByKey = new Map();
  for (const node of discoveryNodes) {
    discoveryByKey.set(nodeMergeKey(node), node);
  }
  const registryByKey = new Map();
  for (const node of registryNodes) {
    registryByKey.set(nodeMergeKey(node), node);
  }
  const allKeys = new Set([...registryByKey.keys(), ...discoveryByKey.keys()].filter(Boolean));

  const effectiveNodes = Array.from(allKeys)
    .map((key) => {
      const registryEntry = registryByKey.get(key) || null;
      const discoveryEntry = discoveryByKey.get(key) || null;
      const hostId = registryEntry?.hostId || discoveryEntry?.hostId || hosts[0]?.hostId || 'local-ec2';
      const hostRecord = hosts.find((host) => host.hostId === hostId) || null;
      const stateClass = computeStateClass({ registryEntry, discoveryEntry, staleHeartbeatMs: DEFAULT_STALE_HEARTBEAT_MS });
      const container = discoveryEntry?.container || { exists: false, running: false, status: '' };
      const statusText = discoveryEntry?.status?.status || (container.running ? 'running' : registryEntry?.intendedState || 'unknown');
      return {
        nodeId: key,
        displayName: registryEntry?.displayName || discoveryEntry?.displayName || key,
        hostId,
        hostName: hostRecord?.displayName || hostId,
        hostKind: hostRecord?.kind || 'local',
        strategyId: registryEntry?.strategyId || discoveryEntry?.strategyId || 'betting-arbitrage',
        manifestId: registryEntry?.manifestId || discoveryEntry?.manifestId || null,
        containerName: registryEntry?.containerName || discoveryEntry?.containerName || key,
        venues: registryEntry?.venues || discoveryEntry?.venues || [],
        intendedState: registryEntry?.intendedState || (container.running ? 'running' : 'stopped'),
        lastAppliedConfig: registryEntry?.lastAppliedConfig || null,
        configOverrides: registryEntry?.configOverrides || null,
        imageRef: registryEntry?.imageRef || discoveryEntry?.currentImage || discoveryEntry?.release?.image || '',
        envFile: registryEntry?.envFile || discoveryEntry?.release?.envFile || null,
        manifestPath: registryEntry?.manifestPath || discoveryEntry?.release?.manifest || discoveryEntry?.paths?.manifestPath || null,
        status: statusText,
        stateClass,
        managed: Boolean(registryEntry),
        source: registryEntry && discoveryEntry ? 'managed+discovered' : registryEntry ? 'managed' : 'discovered',
        heartbeatAt: discoveryEntry?.heartbeatAt || null,
        isHeartbeatStale: Boolean(discoveryEntry?.isHeartbeatStale),
        discoveredAt: discoveryEntry?.discoveredAt || null,
        container,
        registry: registryEntry,
        discovery: discoveryEntry,
        metadata: {
          traderId: registryEntry?.traderId || discoveryEntry?.traderId || null,
          validationMode: discoveryEntry?.validationMode ?? true,
          logLevel: discoveryEntry?.logLevel || null,
        },
      };
    })
    .sort((a, b) => a.nodeId.localeCompare(b.nodeId));

  return effectiveNodes;
}

function normalizeBoolean(value) {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  const normalized = String(value || '').trim().toLowerCase();
  if (!normalized) return false;
  return ['1', 'true', 'yes', 'on'].includes(normalized);
}

function applyStrategyOverrides(target, strategyOverride = {}) {
  const allowedStrategyKeys = new Set([
    'min_profit_margin',
    'max_total_stake',
    'sport_filter',
    'market_timing_filter',
    'exclude_live',
    'rollover_aware',
    'auto_execute',
  ]);
  target.strategy = target.strategy && typeof target.strategy === 'object' ? { ...target.strategy } : {};
  for (const [key, value] of Object.entries(strategyOverride || {})) {
    if (!allowedStrategyKeys.has(key)) continue;
    target.strategy[key] = value;
  }
}

export function mergeTradingNodeManifest(baseManifest, override = {}) {
  const result = deepClone(baseManifest || {});
  if (override.log_level !== undefined) {
    result.log_level = String(override.log_level || '').trim() || result.log_level;
  }
  if (override.allow_dummy_credentials !== undefined) {
    result.allow_dummy_credentials = normalizeBoolean(override.allow_dummy_credentials);
  }
  if (override.validation_mode !== undefined) {
    result.validation_mode = normalizeBoolean(override.validation_mode);
  }
  if (override.execution_enabled !== undefined && Array.isArray(result.venues)) {
    const executionEnabled = normalizeBoolean(override.execution_enabled);
    result.venues = result.venues.map((venue) => ({ ...venue, execution_enabled: executionEnabled }));
  }
  if (Array.isArray(override.venues)) {
    result.venues = deepClone(override.venues);
  }
  if (override.metadata && typeof override.metadata === 'object') {
    result.metadata = {
      ...(result.metadata && typeof result.metadata === 'object' ? result.metadata : {}),
      ...deepClone(override.metadata),
    };
  }
  if (override.node_id !== undefined) {
    result.node_id = String(override.node_id || '').trim() || result.node_id;
  }
  if (override.trader_id !== undefined) {
    result.trader_id = String(override.trader_id || '').trim() || result.trader_id;
  }
  if (override.strategy && typeof override.strategy === 'object') {
    applyStrategyOverrides(result, override.strategy);
  }
  return result;
}

export function readTradingNodeLogs({ containerName, mode = 'recent', limit = 200, cursor = '' } = {}) {
  if (!containerName) {
    return { content: '', cursor, source: 'docker', error: 'containerName is required' };
  }
  const args = ['logs', '--timestamps'];
  if (mode === 'follow' && cursor) {
    args.push('--since', cursor);
  } else {
    args.push('--tail', String(limit || 200));
  }
  args.push(containerName);
  const result = runCommand('docker', args);
  if (!result.ok) {
    return {
      content: '',
      cursor,
      source: 'docker',
      error: (result.stderr || result.stdout || 'docker logs failed').trim(),
    };
  }
  const content = [result.stdout, result.stderr].filter(Boolean).join('');
  return {
    content,
    cursor: new Date().toISOString(),
    source: 'docker',
    mode,
    limit,
  };
}
