#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

export const DEFAULT_RUN_ROOT = process.env.CONTROL_PLANE_RUN_ROOT || '/srv/symphony/worker-state/runs';
export const DEFAULT_SETTINGS_PATH = process.env.CONTROL_PLANE_SETTINGS_PATH || '/srv/symphony/worker-state/control-plane/settings.json';
export const VALID_DELIVERY_MODES = ['interrupt_now', 'deliver_after_current_step', 'deliver_when_idle'];

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function chmodIfPossible(targetPath, mode) {
  try {
    fs.chmodSync(targetPath, mode);
  } catch {
    // ignore when the platform/filesystem does not support chmod for this path
  }
}

function writeJsonAtomic(filePath, value) {
  ensureDir(path.dirname(filePath));
  const tmpPath = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(tmpPath, JSON.stringify(value, null, 2));
  chmodIfPossible(tmpPath, 0o664);
  fs.renameSync(tmpPath, filePath);
}

function appendLine(filePath, line) {
  ensureDir(path.dirname(filePath));
  fs.appendFileSync(filePath, `${line}\n`);
  chmodIfPossible(filePath, 0o664);
}

export function readJson(filePath, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch {
    return fallback;
  }
}

export function nowIso() {
  return new Date().toISOString();
}

export function sanitizeSegment(value) {
  return String(value || 'unknown')
    .trim()
    .replace(/[^a-zA-Z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80) || 'unknown';
}

export function makeRunId({ issueIdentifier = 'issue', workerName = 'worker' } = {}) {
  const stamp = nowIso().replace(/[-:TZ.]/g, '').slice(0, 14);
  const issuePart = sanitizeSegment(issueIdentifier);
  const workerPart = sanitizeSegment(workerName);
  return `${stamp}-${issuePart}-${workerPart}-${randomUUID().slice(0, 8)}`;
}

export function getRunDir(runId, runRoot = DEFAULT_RUN_ROOT) {
  return path.join(runRoot, runId);
}

export function getRunMetaPath(runId, runRoot = DEFAULT_RUN_ROOT) {
  return path.join(getRunDir(runId, runRoot), 'run.json');
}

export function getRunEventsPath(runId, runRoot = DEFAULT_RUN_ROOT) {
  return path.join(getRunDir(runId, runRoot), 'events.jsonl');
}

export function getRunActionsDir(runId, runRoot = DEFAULT_RUN_ROOT) {
  return path.join(getRunDir(runId, runRoot), 'operator-actions');
}

export function getRunActionPath(runId, actionId, runRoot = DEFAULT_RUN_ROOT) {
  return path.join(getRunActionsDir(runId, runRoot), `${actionId}.json`);
}

export function getRunRawDir(runId, runRoot = DEFAULT_RUN_ROOT) {
  return path.join(getRunDir(runId, runRoot), 'raw');
}

export function getRunLogStreamsPath(runId, runRoot = DEFAULT_RUN_ROOT) {
  return path.join(getRunDir(runId, runRoot), 'log-streams.json');
}

export function ensureRunLayout(runId, runRoot = DEFAULT_RUN_ROOT) {
  ensureDir(getRunDir(runId, runRoot));
  ensureDir(getRunActionsDir(runId, runRoot));
  ensureDir(getRunRawDir(runId, runRoot));
}

export function defaultSettings() {
  return {
    defaultPromptDeliveryMode: 'deliver_after_current_step',
    rawLogRetentionDays: 30,
    updatedAt: null
  };
}

export function readSettings(settingsPath = DEFAULT_SETTINGS_PATH) {
  const value = readJson(settingsPath, defaultSettings()) || defaultSettings();
  const deliveryMode = VALID_DELIVERY_MODES.includes(value.defaultPromptDeliveryMode)
    ? value.defaultPromptDeliveryMode
    : 'deliver_after_current_step';
  const rawLogRetentionDays = Number(value.rawLogRetentionDays || 30);
  return {
    defaultPromptDeliveryMode: deliveryMode,
    rawLogRetentionDays: Number.isFinite(rawLogRetentionDays) && rawLogRetentionDays > 0 ? rawLogRetentionDays : 30,
    updatedAt: value.updatedAt || null
  };
}

export function saveSettings(patch = {}, settingsPath = DEFAULT_SETTINGS_PATH) {
  const current = readSettings(settingsPath);
  const next = {
    ...current,
    ...patch,
    defaultPromptDeliveryMode: VALID_DELIVERY_MODES.includes(patch.defaultPromptDeliveryMode)
      ? patch.defaultPromptDeliveryMode
      : current.defaultPromptDeliveryMode,
    rawLogRetentionDays: Number.isFinite(Number(patch.rawLogRetentionDays)) && Number(patch.rawLogRetentionDays) > 0
      ? Number(patch.rawLogRetentionDays)
      : current.rawLogRetentionDays,
    updatedAt: nowIso()
  };
  writeJsonAtomic(settingsPath, next);
  chmodIfPossible(settingsPath, 0o664);
  return next;
}

export function createRun(meta = {}, runRoot = DEFAULT_RUN_ROOT) {
  const runId = meta.runId || makeRunId(meta);
  const startedAt = meta.startedAt || nowIso();
  ensureRunLayout(runId, runRoot);
  const run = {
    runId,
    issueIdentifier: meta.issueIdentifier || '',
    issueId: meta.issueId || '',
    workerName: meta.workerName || '',
    workerEmail: meta.workerEmail || '',
    workerUser: meta.workerUser || '',
    provider: meta.provider || 'codex',
    workspacePath: meta.workspacePath || '',
    startedAt,
    lastUpdatedAt: startedAt,
    endedAt: null,
    status: meta.status || 'starting',
    threadId: meta.threadId || '',
    currentTurnId: meta.currentTurnId || '',
    currentTurnStatus: meta.currentTurnStatus || '',
    currentItem: meta.currentItem || null,
    currentCommand: meta.currentCommand || null,
    serviceTier: meta.serviceTier || '',
    modelProvider: meta.modelProvider || '',
    effectiveModel: meta.effectiveModel || '',
    operatorSettings: {
      defaultPromptDeliveryMode: meta.defaultPromptDeliveryMode || readSettings().defaultPromptDeliveryMode
    },
    tokenUsage: meta.tokenUsage || null,
    counts: {
      events: 0,
      operatorActions: 0,
      logStreams: 0
    },
    logStreams: [],
    rawLogsExpiredAt: null,
    lastError: '',
    notes: meta.notes || ''
  };
  writeJsonAtomic(getRunMetaPath(runId, runRoot), run);
  writeJsonAtomic(getRunLogStreamsPath(runId, runRoot), []);
  return run;
}

export function loadRun(runId, runRoot = DEFAULT_RUN_ROOT) {
  return readJson(getRunMetaPath(runId, runRoot), null);
}

export function updateRun(runId, patchOrFn, runRoot = DEFAULT_RUN_ROOT) {
  const current = loadRun(runId, runRoot);
  if (!current) {
    throw new Error(`Unknown run: ${runId}`);
  }
  const patch = typeof patchOrFn === 'function' ? patchOrFn(current) || {} : patchOrFn || {};
  const next = {
    ...current,
    ...patch,
    lastUpdatedAt: patch.lastUpdatedAt || nowIso()
  };
  writeJsonAtomic(getRunMetaPath(runId, runRoot), next);
  return next;
}

export function appendRunEvent(runId, event = {}, runRoot = DEFAULT_RUN_ROOT) {
  const payload = {
    id: event.id || randomUUID(),
    timestamp: event.timestamp || nowIso(),
    issueIdentifier: event.issueIdentifier || '',
    runId,
    workerName: event.workerName || '',
    provider: event.provider || 'codex',
    eventType: event.eventType || 'event',
    level: event.level || 'info',
    summary: event.summary || '',
    payload: event.payload || {},
    source: event.source || 'control-plane'
  };
  payload.cursor = `${payload.timestamp}:${payload.id}`;
  appendLine(getRunEventsPath(runId, runRoot), JSON.stringify(payload));
  updateRun(runId, (current) => ({
    counts: {
      ...(current.counts || {}),
      events: Number(current.counts?.events || 0) + 1
    },
    lastUpdatedAt: payload.timestamp
  }), runRoot);
  return payload;
}

export function listRunEvents(runId, { runRoot = DEFAULT_RUN_ROOT, limit = 500, filters = {} } = {}) {
  const filePath = getRunEventsPath(runId, runRoot);
  if (!fs.existsSync(filePath)) {
    return [];
  }
  const lines = fs.readFileSync(filePath, 'utf8').split(/\r?\n/).filter(Boolean);
  const events = [];
  for (const line of lines) {
    try {
      const event = JSON.parse(line);
      if (filters.afterCursor && String(event.cursor || '') <= String(filters.afterCursor)) {
        continue;
      }
      if (filters.issueIdentifier && event.issueIdentifier !== filters.issueIdentifier) {
        continue;
      }
      if (filters.workerName && event.workerName !== filters.workerName) {
        continue;
      }
      if (filters.eventType && event.eventType !== filters.eventType) {
        continue;
      }
      if (filters.level && event.level !== filters.level) {
        continue;
      }
      if (filters.source && event.source !== filters.source) {
        continue;
      }
      const search = String(filters.search || '').trim().toLowerCase();
      if (search) {
        const haystack = JSON.stringify(event).toLowerCase();
        if (!haystack.includes(search)) {
          continue;
        }
      }
      events.push(event);
    } catch {
      // ignore malformed event lines
    }
  }
  if (limit && events.length > limit) {
    return events.slice(-limit);
  }
  return events;
}

export function listRuns({ runRoot = DEFAULT_RUN_ROOT, issueIdentifier = '', workerName = '', status = '', limit = 100 } = {}) {
  if (!fs.existsSync(runRoot)) {
    return [];
  }
  const runs = [];
  for (const entry of fs.readdirSync(runRoot, { withFileTypes: true })) {
    if (!entry.isDirectory()) {
      continue;
    }
    const run = loadRun(entry.name, runRoot);
    if (!run) {
      continue;
    }
    if (issueIdentifier && run.issueIdentifier !== issueIdentifier) {
      continue;
    }
    if (workerName && run.workerName !== workerName) {
      continue;
    }
    if (status && run.status !== status) {
      continue;
    }
    runs.push(run);
  }
  runs.sort((a, b) => new Date(b.startedAt || 0).getTime() - new Date(a.startedAt || 0).getTime());
  return limit ? runs.slice(0, limit) : runs;
}

export function createOperatorAction(runId, action = {}, runRoot = DEFAULT_RUN_ROOT) {
  const actionId = action.id || randomUUID();
  const stored = {
    id: actionId,
    runId,
    issueIdentifier: action.issueIdentifier || '',
    workerName: action.workerName || '',
    type: action.type || 'prompt',
    prompt: action.prompt || '',
    reason: action.reason || '',
    deliveryMode: VALID_DELIVERY_MODES.includes(action.deliveryMode)
      ? action.deliveryMode
      : readSettings().defaultPromptDeliveryMode,
    requestedBy: action.requestedBy || 'operator',
    createdAt: action.createdAt || nowIso(),
    status: action.status || 'queued',
    priority: Number(action.priority || 0),
    metadata: action.metadata || {},
    deliveredAt: action.deliveredAt || null,
    processedAt: action.processedAt || null,
    error: action.error || ''
  };
  writeJsonAtomic(getRunActionPath(runId, actionId, runRoot), stored);
  updateRun(runId, (current) => ({
    counts: {
      ...(current.counts || {}),
      operatorActions: Number(current.counts?.operatorActions || 0) + 1
    }
  }), runRoot);
  return stored;
}

export function updateOperatorAction(runId, actionId, patch = {}, runRoot = DEFAULT_RUN_ROOT) {
  const filePath = getRunActionPath(runId, actionId, runRoot);
  const current = readJson(filePath, null);
  if (!current) {
    throw new Error(`Unknown operator action: ${actionId}`);
  }
  const next = {
    ...current,
    ...patch,
    processedAt: patch.processedAt === undefined ? current.processedAt : patch.processedAt
  };
  writeJsonAtomic(filePath, next);
  return next;
}

export function listOperatorActions(runId, { runRoot = DEFAULT_RUN_ROOT, type = '', statuses = [], limit = 200 } = {}) {
  const dirPath = getRunActionsDir(runId, runRoot);
  if (!fs.existsSync(dirPath)) {
    return [];
  }
  const items = [];
  for (const entry of fs.readdirSync(dirPath, { withFileTypes: true })) {
    if (!entry.isFile() || !entry.name.endsWith('.json')) {
      continue;
    }
    const action = readJson(path.join(dirPath, entry.name), null);
    if (!action) {
      continue;
    }
    if (type && action.type !== type) {
      continue;
    }
    if (Array.isArray(statuses) && statuses.length > 0 && !statuses.includes(action.status)) {
      continue;
    }
    items.push(action);
  }
  items.sort((a, b) => new Date(a.createdAt || 0).getTime() - new Date(b.createdAt || 0).getTime());
  return limit ? items.slice(-limit) : items;
}

export function upsertLogStream(runId, stream = {}, runRoot = DEFAULT_RUN_ROOT) {
  const filePath = getRunLogStreamsPath(runId, runRoot);
  const current = readJson(filePath, []);
  const next = Array.isArray(current) ? [...current] : [];
  const idx = next.findIndex((entry) => entry.id === stream.id);
  const existing = idx === -1 ? {} : next[idx];
  const value = {
    id: stream.id,
    label: stream.label || existing.label || stream.id,
    type: stream.type || existing.type || 'log',
    severity: stream.severity || existing.severity || 'info',
    filePath: stream.filePath || existing.filePath || '',
    itemId: stream.itemId || existing.itemId || '',
    tool: stream.tool || existing.tool || '',
    taskId: stream.taskId || existing.taskId || '',
    bytes: Number(stream.bytes ?? existing.bytes ?? 0),
    startedAt: stream.startedAt || existing.startedAt || nowIso(),
    updatedAt: stream.updatedAt || nowIso(),
    completedAt: stream.completedAt ?? existing.completedAt ?? null,
    exitCode: stream.exitCode ?? existing.exitCode ?? null,
    meta: {
      ...(existing.meta || {}),
      ...(stream.meta || {})
    }
  };
  if (idx === -1) {
    next.push(value);
  } else {
    next[idx] = { ...existing, ...value };
  }
  writeJsonAtomic(filePath, next);
  updateRun(runId, (currentRun) => ({
    logStreams: next,
    counts: {
      ...(currentRun.counts || {}),
      logStreams: next.length
    }
  }), runRoot);
  return value;
}

export function listLogStreams(runId, runRoot = DEFAULT_RUN_ROOT) {
  return readJson(getRunLogStreamsPath(runId, runRoot), []) || [];
}

export function readLogChunk(runId, streamId, { runRoot = DEFAULT_RUN_ROOT, offset = 0, limit = 64 * 1024 } = {}) {
  const streams = listLogStreams(runId, runRoot);
  const stream = streams.find((entry) => entry.id === streamId);
  if (!stream || !stream.filePath || !fs.existsSync(stream.filePath)) {
    return { stream: stream || null, offset, nextOffset: offset, chunk: '' };
  }
  const fileBuffer = fs.readFileSync(stream.filePath);
  const safeOffset = Math.max(0, Math.min(Number(offset || 0), fileBuffer.length));
  const nextOffset = Math.min(fileBuffer.length, safeOffset + Number(limit || 64 * 1024));
  return {
    stream,
    offset: safeOffset,
    nextOffset,
    chunk: fileBuffer.slice(safeOffset, nextOffset).toString('utf8'),
    eof: nextOffset >= fileBuffer.length
  };
}

export function purgeExpiredRawLogs({ runRoot = DEFAULT_RUN_ROOT, retentionDays = readSettings().rawLogRetentionDays, now = Date.now() } = {}) {
  const cutoffMs = now - Number(retentionDays || 30) * 24 * 60 * 60 * 1000;
  const purged = [];
  for (const run of listRuns({ runRoot, limit: 0 })) {
    if (!run.startedAt || new Date(run.startedAt).getTime() > cutoffMs || run.rawLogsExpiredAt) {
      continue;
    }
    const rawDir = getRunRawDir(run.runId, runRoot);
    if (fs.existsSync(rawDir)) {
      fs.rmSync(rawDir, { recursive: true, force: true });
    }
    updateRun(run.runId, {
      rawLogsExpiredAt: nowIso(),
      logStreams: [],
      lastUpdatedAt: nowIso()
    }, runRoot);
    writeJsonAtomic(getRunLogStreamsPath(run.runId, runRoot), []);
    purged.push(run.runId);
  }
  return purged;
}
