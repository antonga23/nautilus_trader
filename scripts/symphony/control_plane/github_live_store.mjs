#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

export const DEFAULT_GITHUB_ROOT =
  process.env.CONTROL_PLANE_GITHUB_ROOT || '/srv/symphony/worker-state/control-plane/github-actions';

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function chmodIfPossible(targetPath, mode) {
  try {
    fs.chmodSync(targetPath, mode);
  } catch {
    // ignore filesystem chmod failures
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

function readJson(filePath, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch {
    return fallback;
  }
}

function nowIso() {
  return new Date().toISOString();
}

export function getGitHubEventsPath(root = DEFAULT_GITHUB_ROOT) {
  return path.join(root, 'events.jsonl');
}

export function getGitHubJobsDir(root = DEFAULT_GITHUB_ROOT) {
  return path.join(root, 'jobs');
}

export function getGitHubJobDir(jobId, root = DEFAULT_GITHUB_ROOT) {
  return path.join(getGitHubJobsDir(root), String(jobId));
}

export function getGitHubJobMetaPath(jobId, root = DEFAULT_GITHUB_ROOT) {
  return path.join(getGitHubJobDir(jobId, root), 'job.json');
}

export function getGitHubJobLogStreamsPath(jobId, root = DEFAULT_GITHUB_ROOT) {
  return path.join(getGitHubJobDir(jobId, root), 'log-streams.json');
}

export function getGitHubJobRawDir(jobId, root = DEFAULT_GITHUB_ROOT) {
  return path.join(getGitHubJobDir(jobId, root), 'raw');
}

export function ensureGitHubJobLayout(jobId, root = DEFAULT_GITHUB_ROOT) {
  ensureDir(getGitHubJobDir(jobId, root));
  ensureDir(getGitHubJobRawDir(jobId, root));
}

function normalizePullRequests(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((entry) => ({
      number: Number(entry?.number || 0),
      url: String(entry?.url || entry?.html_url || '').trim()
    }))
    .filter((entry) => entry.number > 0 || entry.url);
}

function normalizeSteps(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((step) => ({
    number: Number(step?.number || 0),
    name: String(step?.name || '').trim(),
    status: String(step?.status || '').trim(),
    conclusion: step?.conclusion ?? null,
    startedAt: step?.started_at || step?.startedAt || null,
    completedAt: step?.completed_at || step?.completedAt || null
  }));
}

function normalizeObserver(value) {
  const observer = value && typeof value === 'object' ? value : {};
  return {
    runnerService: String(observer.runnerService || '').trim(),
    diagFile: String(observer.diagFile || '').trim(),
    dockerContainers: Array.isArray(observer.dockerContainers)
      ? observer.dockerContainers.map((entry) => ({
          id: String(entry?.id || '').trim(),
          name: String(entry?.name || '').trim(),
          image: String(entry?.image || '').trim(),
          status: String(entry?.status || '').trim()
        }))
      : [],
    lastObservedAt: observer.lastObservedAt || null,
    lastObserverError: String(observer.lastObserverError || '').trim()
  };
}

function normalizeJob(job = {}) {
  const existing = job && typeof job === 'object' ? job : {};
  const status = String(existing.status || '').trim() || 'queued';
  const conclusion = existing.conclusion ?? null;
  const startedAt = existing.startedAt || existing.started_at || null;
  const queuedAt = existing.queuedAt || existing.created_at || existing.queued_at || null;
  const completedAt = existing.completedAt || existing.completed_at || null;
  const steps = normalizeSteps(existing.steps || []);
  const currentStep =
    steps.find((step) => step.status === 'in_progress') ||
    steps.find((step) => step.status === 'queued') ||
    null;

  return {
    jobId: Number(existing.jobId || existing.id || 0),
    runId: Number(existing.runId || existing.run_id || 0),
    runAttempt: Number(existing.runAttempt || existing.run_attempt || 0),
    repoFullName: String(existing.repoFullName || existing.repository?.full_name || '').trim(),
    workflowName: String(existing.workflowName || existing.workflow_name || '').trim(),
    name: String(existing.name || '').trim(),
    htmlUrl: String(existing.htmlUrl || existing.html_url || '').trim(),
    checkRunUrl: String(existing.checkRunUrl || existing.check_run_url || '').trim(),
    runUrl: String(existing.runUrl || existing.run_url || '').trim(),
    status,
    conclusion,
    headSha: String(existing.headSha || existing.head_sha || '').trim(),
    headBranch: String(existing.headBranch || existing.head_branch || '').trim(),
    event: String(existing.event || '').trim(),
    labels: Array.isArray(existing.labels) ? existing.labels.map((item) => String(item || '').trim()).filter(Boolean) : [],
    runnerName: String(existing.runnerName || existing.runner_name || '').trim(),
    runnerGroupName: String(existing.runnerGroupName || existing.runner_group_name || '').trim(),
    runnerId: Number(existing.runnerId || existing.runner_id || 0),
    workflowId: Number(existing.workflowId || existing.workflow_id || 0),
    checkRunId: Number(existing.checkRunId || existing.check_run_id || 0),
    pullRequests: normalizePullRequests(existing.pullRequests || existing.pull_requests || []),
    queuedAt,
    startedAt,
    completedAt,
    lastWebhookAt: existing.lastWebhookAt || null,
    lastPolledAt: existing.lastPolledAt || null,
    lastUpdatedAt: existing.lastUpdatedAt || nowIso(),
    currentStepName: String(existing.currentStepName || currentStep?.name || '').trim(),
    currentStepNumber: Number(existing.currentStepNumber || currentStep?.number || 0),
    steps,
    logStreams: Array.isArray(existing.logStreams) ? existing.logStreams : [],
    observer: normalizeObserver(existing.observer),
    source: String(existing.source || '').trim(),
    error: String(existing.error || '').trim()
  };
}

export function loadGitHubJob(jobId, root = DEFAULT_GITHUB_ROOT) {
  return readJson(getGitHubJobMetaPath(jobId, root), null);
}

export function upsertGitHubJob(job = {}, root = DEFAULT_GITHUB_ROOT) {
  const incoming = job && typeof job === 'object' ? job : {};
  const jobId = Number(incoming.jobId || incoming.id || 0);
  if (!jobId) {
    throw new Error('jobId is required');
  }
  ensureGitHubJobLayout(jobId, root);
  const current = loadGitHubJob(jobId, root) || {};
  const merged = normalizeJob({
    ...current,
    ...incoming,
    observer: {
      ...(current.observer || {}),
      ...(incoming.observer || {})
    },
    lastUpdatedAt: nowIso()
  });
  writeJsonAtomic(getGitHubJobMetaPath(merged.jobId, root), merged);
  return merged;
}

export function listGitHubJobs({
  root = DEFAULT_GITHUB_ROOT,
  activeOnly = false,
  limit = 50,
  repoFullName = '',
  runId = 0,
  statuses = [],
  pullRequestNumber = 0
} = {}) {
  const jobsDir = getGitHubJobsDir(root);
  if (!fs.existsSync(jobsDir)) {
    return [];
  }
  const normalizedStatuses = Array.isArray(statuses)
    ? statuses.map((value) => String(value || '').trim()).filter(Boolean)
    : [];
  const jobs = [];
  for (const entry of fs.readdirSync(jobsDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) {
      continue;
    }
    const job = loadGitHubJob(entry.name, root);
    if (!job) {
      continue;
    }
    if (repoFullName && job.repoFullName !== repoFullName) {
      continue;
    }
    if (runId && Number(job.runId || 0) !== Number(runId)) {
      continue;
    }
    if (normalizedStatuses.length > 0 && !normalizedStatuses.includes(job.status)) {
      continue;
    }
    if (activeOnly && !['queued', 'in_progress', 'waiting'].includes(job.status)) {
      continue;
    }
    if (pullRequestNumber && !job.pullRequests.some((entry) => Number(entry.number || 0) === Number(pullRequestNumber))) {
      continue;
    }
    jobs.push(job);
  }
  jobs.sort((a, b) => {
    const left = new Date(b.lastUpdatedAt || b.startedAt || b.queuedAt || 0).getTime();
    const right = new Date(a.lastUpdatedAt || a.startedAt || a.queuedAt || 0).getTime();
    return left - right;
  });
  return limit ? jobs.slice(0, limit) : jobs;
}

export function appendGitHubEvent(event = {}, root = DEFAULT_GITHUB_ROOT) {
  const payload = {
    id: event.id || randomUUID(),
    timestamp: event.timestamp || nowIso(),
    cursor: '',
    eventType: String(event.eventType || 'github.event').trim(),
    level: String(event.level || 'info').trim(),
    summary: String(event.summary || '').trim(),
    source: String(event.source || 'github-actions').trim(),
    repoFullName: String(event.repoFullName || '').trim(),
    jobId: Number(event.jobId || 0),
    runId: Number(event.runId || 0),
    workflowName: String(event.workflowName || '').trim(),
    jobName: String(event.jobName || '').trim(),
    payload: event.payload || {}
  };
  payload.cursor = `${payload.timestamp}:${payload.id}`;
  appendLine(getGitHubEventsPath(root), JSON.stringify(payload));
  return payload;
}

export function listGitHubEvents({
  root = DEFAULT_GITHUB_ROOT,
  afterCursor = '',
  jobId = 0,
  runId = 0,
  limit = 200,
  search = '',
  eventType = ''
} = {}) {
  const filePath = getGitHubEventsPath(root);
  if (!fs.existsSync(filePath)) {
    return [];
  }
  const lines = fs.readFileSync(filePath, 'utf8').split(/\r?\n/).filter(Boolean);
  const events = [];
  const normalizedSearch = String(search || '').trim().toLowerCase();
  for (const line of lines) {
    try {
      const event = JSON.parse(line);
      if (afterCursor && String(event.cursor || '') <= String(afterCursor)) {
        continue;
      }
      if (jobId && Number(event.jobId || 0) !== Number(jobId)) {
        continue;
      }
      if (runId && Number(event.runId || 0) !== Number(runId)) {
        continue;
      }
      if (eventType && event.eventType !== eventType) {
        continue;
      }
      if (normalizedSearch) {
        const haystack = JSON.stringify(event).toLowerCase();
        if (!haystack.includes(normalizedSearch)) {
          continue;
        }
      }
      events.push(event);
    } catch {
      // ignore malformed lines
    }
  }
  if (limit && events.length > limit) {
    return events.slice(-limit);
  }
  return events;
}

export function upsertGitHubJobLogStream(jobId, stream = {}, root = DEFAULT_GITHUB_ROOT) {
  const filePath = getGitHubJobLogStreamsPath(jobId, root);
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
    bytes: Number(stream.bytes ?? existing.bytes ?? 0),
    startedAt: stream.startedAt || existing.startedAt || nowIso(),
    updatedAt: stream.updatedAt || nowIso(),
    completedAt: stream.completedAt ?? existing.completedAt ?? null,
    source: stream.source || existing.source || 'runner-observer',
    meta: {
      ...(existing.meta || {}),
      ...(stream.meta || {})
    }
  };
  if (idx === -1) {
    next.push(value);
  } else {
    next[idx] = {
      ...existing,
      ...value
    };
  }
  ensureGitHubJobLayout(jobId, root);
  writeJsonAtomic(filePath, next);
  upsertGitHubJob({ jobId, logStreams: next }, root);
  return value;
}

export function listGitHubJobLogStreams(jobId, root = DEFAULT_GITHUB_ROOT) {
  return readJson(getGitHubJobLogStreamsPath(jobId, root), []) || [];
}

export function readGitHubJobLogChunk(jobId, streamId, {
  root = DEFAULT_GITHUB_ROOT,
  offset = 0,
  limit = 64 * 1024
} = {}) {
  const streams = listGitHubJobLogStreams(jobId, root);
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
