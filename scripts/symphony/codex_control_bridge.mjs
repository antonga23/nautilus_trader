#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import readline from 'node:readline';
import {
  createRun,
  appendRunEvent,
  createOperatorAction,
  getRunRawDir,
  listLogStreams,
  listOperatorActions,
  nowIso,
  readJson,
  readSettings,
  updateOperatorAction,
  updateRun,
  upsertLogStream
} from './control_plane/run_store.mjs';

const RUN_ROOT = process.env.CONTROL_PLANE_RUN_ROOT || '/srv/symphony/worker-state/runs';
const WORKER_STATE_ROOT = process.env.CONTROL_PLANE_WORKER_STATE_ROOT || '/srv/symphony/worker-state/workers';
const ISSUE_IDENTIFIER = process.env.SYMPHONY_ISSUE_IDENTIFIER || path.basename(process.cwd());
const WORKER_NAME = process.env.SYMPHONY_WORKER_NAME || 'unknown-worker';
const WORKER_EMAIL = process.env.SYMPHONY_WORKER_EMAIL || '';
const WORKER_USER = process.env.USER || '';
const WORKSPACE_PATH = process.cwd();
const EFFECTIVE_MODEL = process.env.SYMPHONY_EFFECTIVE_MODEL || '';
const CODEX_BIN = process.env.CODEX_BIN || 'codex';
const PROVIDER = 'codex';
const SETTINGS = readSettings();
const RUN = createRun({
  issueIdentifier: ISSUE_IDENTIFIER,
  workerName: WORKER_NAME,
  workerEmail: WORKER_EMAIL,
  workerUser: WORKER_USER,
  workspacePath: WORKSPACE_PATH,
  provider: PROVIDER,
  effectiveModel: EFFECTIVE_MODEL,
  defaultPromptDeliveryMode: SETTINGS.defaultPromptDeliveryMode
}, RUN_ROOT);

const RAW_DIR = getRunRawDir(RUN.runId, RUN_ROOT);
const PROTOCOL_IN_PATH = path.join(RAW_DIR, 'protocol-in.jsonl');
const PROTOCOL_OUT_PATH = path.join(RAW_DIR, 'protocol-out.jsonl');
const STDERR_PATH = path.join(RAW_DIR, 'bridge-stderr.log');
const WORKER_STATE_PATH = path.join(WORKER_STATE_ROOT, `${WORKER_NAME}.json`);

const parentRequests = new Map();
const brokerRequests = new Map();
const state = {
  runId: RUN.runId,
  issueIdentifier: ISSUE_IDENTIFIER,
  workerName: WORKER_NAME,
  workerEmail: WORKER_EMAIL,
  workspacePath: WORKSPACE_PATH,
  threadId: RUN.threadId || '',
  currentTurnId: RUN.currentTurnId || '',
  currentTurnStatus: RUN.currentTurnStatus || '',
  currentModel: RUN.effectiveModel || '',
  modelProvider: RUN.modelProvider || '',
  serviceTier: RUN.serviceTier || '',
  tokenUsage: RUN.tokenUsage || null,
  activeItems: new Map(),
  commandLogs: new Map(),
  runStatus: 'starting',
  lastError: '',
  lastEventAt: RUN.startedAt,
  lastRequestAt: RUN.startedAt,
  activeCommand: null,
  exitCode: null
};

fs.mkdirSync(RAW_DIR, { recursive: true });

const childArgs = ['app-server'];
childArgs.push('-c', 'shell_environment_policy.inherit="all"');
if (EFFECTIVE_MODEL) {
  childArgs.push('-c', `model=\"${EFFECTIVE_MODEL}\"`);
}

const child = spawn(CODEX_BIN, childArgs, {
  stdio: ['pipe', 'pipe', 'pipe'],
  env: {
    ...process.env,
    CONTROL_PLANE_RUN_ID: RUN.runId
  }
});

function writeWorkerState(patch = {}) {
  try {
    const current = readJson(WORKER_STATE_PATH, {}) || {};
    const next = {
      ...current,
      ...patch,
      runId: state.runId,
      issueIdentifier: ISSUE_IDENTIFIER,
      workspacePath: WORKSPACE_PATH,
      currentTurnId: state.currentTurnId || '',
      currentTurnStatus: state.currentTurnStatus || '',
      currentCommand: state.activeCommand || null,
      activeItemCount: state.activeItems.size,
      effectiveModel: state.currentModel || EFFECTIVE_MODEL || '',
      lastEventAt: state.lastEventAt,
      lastError: patch.lastError ?? state.lastError ?? current.lastError ?? ''
    };
    const tmpPath = `${WORKER_STATE_PATH}.${process.pid}.${Date.now()}.tmp`;
    fs.writeFileSync(tmpPath, JSON.stringify(next, null, 2));
    fs.renameSync(tmpPath, WORKER_STATE_PATH);
  } catch (error) {
    process.stderr.write(`[codex-bridge] worker state write failed: ${error.message}\n`);
  }
}

function snapshot() {
  return {
    threadId: state.threadId,
    currentTurnId: state.currentTurnId,
    currentTurnStatus: state.currentTurnStatus,
    effectiveModel: state.currentModel || EFFECTIVE_MODEL || '',
    modelProvider: state.modelProvider || '',
    serviceTier: state.serviceTier || '',
    tokenUsage: state.tokenUsage,
    activeCommand: state.activeCommand,
    activeItems: [...state.activeItems.values()].map((item) => ({
      id: item.id,
      type: item.type,
      startedAt: item.startedAt,
      summary: item.summary || ''
    }))
  };
}

function recordEvent(eventType, summary, payload = {}, level = 'info', source = 'codex-bridge') {
  const timestamp = nowIso();
  state.lastEventAt = timestamp;
  const event = appendRunEvent(state.runId, {
    timestamp,
    issueIdentifier: ISSUE_IDENTIFIER,
    workerName: WORKER_NAME,
    provider: PROVIDER,
    eventType,
    level,
    summary,
    payload: {
      ...payload,
      snapshot: snapshot()
    },
    source
  }, RUN_ROOT);
  updateRun(state.runId, {
    status: state.runStatus,
    threadId: state.threadId,
    currentTurnId: state.currentTurnId,
    currentTurnStatus: state.currentTurnStatus,
    currentItem: state.activeItems.size
      ? [...state.activeItems.values()].map((item) => ({ id: item.id, type: item.type, summary: item.summary || '' }))
      : null,
    currentCommand: state.activeCommand,
    effectiveModel: state.currentModel || EFFECTIVE_MODEL || '',
    modelProvider: state.modelProvider || '',
    serviceTier: state.serviceTier || '',
    tokenUsage: state.tokenUsage,
    lastError: state.lastError || '',
    lastUpdatedAt: timestamp
  }, RUN_ROOT);
  writeWorkerState();
  return event;
}

function ensureStream(streamId, value) {
  return upsertLogStream(state.runId, { id: streamId, ...value }, RUN_ROOT);
}

function appendRaw(filePath, chunk) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.appendFileSync(filePath, chunk);
}

function updateStreamBytes(streamId, filePath, patch = {}) {
  let bytes = 0;
  try {
    bytes = fs.statSync(filePath).size;
  } catch {
    bytes = 0;
  }
  ensureStream(streamId, {
    filePath,
    bytes,
    updatedAt: nowIso(),
    ...patch
  });
}

function getCommandStreamId(itemId) {
  return `command-${itemId}`;
}

function getCommandLogPath(itemId) {
  return path.join(RAW_DIR, `${getCommandStreamId(itemId)}.log`);
}

function summarizeInput(input = []) {
  return input
    .map((entry) => (entry?.type === 'text' ? entry.text : entry?.type || 'input'))
    .join('\n')
    .slice(0, 600);
}

function jsonLine(rawLine) {
  try {
    return JSON.parse(rawLine);
  } catch {
    return null;
  }
}

function nextBrokerRequestId() {
  return `control-${Date.now()}-${randomUUID().slice(0, 8)}`;
}

function sendBrokerRequest(method, params, meta = {}) {
  const id = nextBrokerRequestId();
  const message = { jsonrpc: '2.0', id, method, params };
  brokerRequests.set(id, { method, params, meta, sentAt: nowIso() });
  child.stdin.write(`${JSON.stringify(message)}\n`);
  appendRaw(PROTOCOL_IN_PATH, `${JSON.stringify({ source: 'broker', ...message })}\n`);
  return id;
}

function currentPromptDeliveryMode(action) {
  return action.deliveryMode || SETTINGS.defaultPromptDeliveryMode;
}

function buildPromptInput(prompt) {
  return [{ type: 'text', text: prompt, text_elements: [] }];
}

function markAction(actionId, patch) {
  return updateOperatorAction(state.runId, actionId, patch, RUN_ROOT);
}

function deliverPromptAction(action, deliveryMethod) {
  const prompt = String(action.prompt || '').trim();
  if (!prompt) {
    markAction(action.id, { status: 'failed', processedAt: nowIso(), error: 'Prompt is empty' });
    recordEvent('agent.prompt_failed', `Prompt action ${action.id} is empty`, { actionId: action.id }, 'error');
    return true;
  }

  if (!state.threadId) {
    markAction(action.id, { status: 'waiting_for_thread', error: '' });
    return false;
  }

  if (deliveryMethod === 'turn/steer') {
    if (!state.currentTurnId) {
      markAction(action.id, { status: 'waiting_for_turn', error: '' });
      return false;
    }
    sendBrokerRequest('turn/steer', {
      threadId: state.threadId,
      expectedTurnId: state.currentTurnId,
      input: buildPromptInput(prompt)
    }, { actionId: action.id, type: action.type, op: 'deliverPrompt', deliveryMethod });
  } else {
    sendBrokerRequest('turn/start', {
      threadId: state.threadId,
      input: buildPromptInput(prompt)
    }, { actionId: action.id, type: action.type, op: 'deliverPrompt', deliveryMethod });
  }

  markAction(action.id, {
    status: 'sent',
    deliveredAt: nowIso(),
    error: '',
    metadata: {
      ...(action.metadata || {}),
      deliveryMethod
    }
  });
  recordEvent('agent.prompt_sent', `Delivered queued ${action.type} to ${deliveryMethod}`, {
    actionId: action.id,
    prompt,
    deliveryMethod,
    deliveryMode: currentPromptDeliveryMode(action)
  });
  return true;
}

function requestInterrupt(action, { withPrompt = false } = {}) {
  if (!state.threadId || !state.currentTurnId) {
    markAction(action.id, {
      status: withPrompt ? 'pending_interrupt' : 'waiting_for_turn',
      error: ''
    });
    return false;
  }
  const interruptRequestedAt = nowIso();
  sendBrokerRequest('turn/interrupt', {
    threadId: state.threadId,
    turnId: state.currentTurnId
  }, { actionId: action.id, type: action.type, op: withPrompt ? 'interruptThenPrompt' : 'interrupt' });
  markAction(action.id, {
    status: withPrompt ? 'pending_interrupt' : 'interrupt_sent',
    metadata: {
      ...(action.metadata || {}),
      interruptRequestedAt
    },
    error: ''
  });
  recordEvent('agent.interrupt_requested', withPrompt ? 'Interrupt requested before queued prompt delivery' : 'Interrupt requested by operator', {
    actionId: action.id,
    reason: action.reason || '',
    withPrompt
  }, 'warning');
  return true;
}

function maybeProcessAction(action) {
  const deliveryMode = currentPromptDeliveryMode(action);

  if (action.type === 'interrupt') {
    if (['interrupt_sent', 'completed', 'failed', 'skipped'].includes(action.status)) {
      return false;
    }
    return requestInterrupt(action);
  }

  if (!['prompt', 'checkpoint'].includes(action.type)) {
    return false;
  }

  if (deliveryMode === 'interrupt_now') {
    if (state.currentTurnId && state.currentTurnStatus === 'inProgress') {
      if (action.status !== 'pending_interrupt') {
        return requestInterrupt(action, { withPrompt: true });
      }
      return false;
    }
    return deliverPromptAction(action, 'turn/start');
  }

  if (deliveryMode === 'deliver_when_idle') {
    if (state.currentTurnId && state.currentTurnStatus === 'inProgress') {
      markAction(action.id, { status: 'waiting_for_idle', error: '' });
      return false;
    }
    return deliverPromptAction(action, 'turn/start');
  }

  if (!state.threadId) {
    markAction(action.id, { status: 'waiting_for_thread', error: '' });
    return false;
  }

  if (state.currentTurnId && state.currentTurnStatus === 'inProgress') {
    if (state.activeItems.size > 0) {
      markAction(action.id, { status: 'waiting_for_step', error: '' });
      return false;
    }
    return deliverPromptAction(action, 'turn/steer');
  }

  return deliverPromptAction(action, 'turn/start');
}

function processOperatorActions() {
  const actions = listOperatorActions(state.runId, {
    runRoot: RUN_ROOT,
    statuses: ['queued', 'waiting_for_thread', 'waiting_for_turn', 'waiting_for_step', 'waiting_for_idle', 'pending_interrupt']
  });
  for (const action of actions) {
    maybeProcessAction(action);
  }
}

function completePendingInterruptPrompts() {
  const actions = listOperatorActions(state.runId, {
    runRoot: RUN_ROOT,
    statuses: ['pending_interrupt']
  }).filter((action) => ['prompt', 'checkpoint'].includes(action.type));
  for (const action of actions) {
    maybeProcessAction(action);
  }
}

function finalizeQueuedActions(reason) {
  const actions = listOperatorActions(state.runId, {
    runRoot: RUN_ROOT,
    statuses: ['queued', 'waiting_for_thread', 'waiting_for_turn', 'waiting_for_step', 'waiting_for_idle', 'pending_interrupt', 'sent', 'interrupt_sent']
  });
  for (const action of actions) {
    if (!['completed', 'failed', 'skipped'].includes(action.status)) {
      markAction(action.id, {
        status: 'skipped',
        processedAt: nowIso(),
        error: reason
      });
    }
  }
}

function handleParentRequest(message) {
  parentRequests.set(message.id, { method: message.method, params: message.params || {} });
  state.lastRequestAt = nowIso();
  appendRaw(PROTOCOL_IN_PATH, `${JSON.stringify(message)}\n`);

  if (message.method === 'turn/start') {
    recordEvent('agent.turn_requested', 'Symphony requested a new turn', {
      requestId: message.id,
      inputPreview: summarizeInput(message.params?.input || []),
      requestedModel: message.params?.model || ''
    });
  } else if (message.method === 'turn/steer') {
    recordEvent('agent.prompt_acknowledged', 'Symphony steered the active turn', {
      requestId: message.id,
      inputPreview: summarizeInput(message.params?.input || [])
    });
  } else if (message.method === 'thread/start') {
    recordEvent('agent.thread_requested', 'Symphony requested a new Codex thread', {
      requestId: message.id,
      requestedModel: message.params?.model || EFFECTIVE_MODEL || ''
    });
  }
}

function handleParentLine(rawLine) {
  const message = jsonLine(rawLine);
  if (message?.method && Object.prototype.hasOwnProperty.call(message, 'id')) {
    handleParentRequest(message);
  } else {
    appendRaw(PROTOCOL_IN_PATH, `${rawLine}\n`);
  }
  child.stdin.write(`${rawLine}\n`);
}

function handleThreadStart(result) {
  if (!result?.thread?.id) {
    return;
  }
  state.threadId = result.thread.id;
  state.currentModel = result.model || state.currentModel || EFFECTIVE_MODEL || '';
  state.modelProvider = result.modelProvider || state.modelProvider || '';
  state.serviceTier = result.serviceTier || state.serviceTier || '';
  state.runStatus = 'running';
  recordEvent('agent.started', 'Codex thread started', {
    threadId: state.threadId,
    model: state.currentModel,
    modelProvider: state.modelProvider,
    serviceTier: state.serviceTier || null
  });
}

function handleTurnResponse(result, method) {
  if (!result?.turn?.id) {
    return;
  }
  state.currentTurnId = result.turn.id;
  state.currentTurnStatus = result.turn.status || state.currentTurnStatus;
  state.runStatus = 'running';
  recordEvent(method === 'turn/steer' ? 'agent.prompt_acknowledged' : 'agent.turn_started', method === 'turn/steer' ? 'Queued prompt was accepted by Codex' : 'Codex turn started', {
    turnId: state.currentTurnId,
    status: state.currentTurnStatus,
    via: method
  });
}

function handleBrokerResponse(message) {
  const request = brokerRequests.get(message.id);
  if (!request) {
    return false;
  }
  brokerRequests.delete(message.id);
  if (message.error) {
    state.lastError = message.error.message || JSON.stringify(message.error);
    if (request.meta?.actionId) {
      markAction(request.meta.actionId, {
        status: 'failed',
        processedAt: nowIso(),
        error: state.lastError
      });
    }
    recordEvent('agent.operator_action_failed', `Operator action failed for ${request.method}`, {
      request,
      error: message.error
    }, 'error');
    return true;
  }

  if (request.method === 'turn/interrupt') {
    if (request.meta?.actionId) {
      const nextStatus = request.meta.op === 'interrupt' ? 'completed' : 'pending_interrupt';
      markAction(request.meta.actionId, {
        status: nextStatus,
        processedAt: request.meta.op === 'interrupt' ? nowIso() : null,
        error: ''
      });
    }
    return true;
  }

  if (request.method === 'turn/start' || request.method === 'turn/steer') {
    if (request.meta?.actionId) {
      markAction(request.meta.actionId, {
        status: 'completed',
        processedAt: nowIso(),
        error: ''
      });
    }
    handleTurnResponse(message.result, request.method);
    return true;
  }

  return true;
}

function handleChildResponse(message) {
  const parentRequest = parentRequests.get(message.id);
  if (!parentRequest) {
    return;
  }
  parentRequests.delete(message.id);
  if (message.error) {
    state.lastError = message.error.message || JSON.stringify(message.error);
    recordEvent('agent.request_failed', `Codex request failed: ${parentRequest.method}`, {
      method: parentRequest.method,
      error: message.error
    }, 'error');
    return;
  }
  if (parentRequest.method === 'thread/start') {
    handleThreadStart(message.result || {});
  } else if (parentRequest.method === 'turn/start' || parentRequest.method === 'turn/steer') {
    handleTurnResponse(message.result || {}, parentRequest.method);
  }
}

function updateCommandState(item, turnId) {
  const itemId = item.id;
  const logPath = getCommandLogPath(itemId);
  const streamId = getCommandStreamId(itemId);
  state.commandLogs.set(itemId, { streamId, logPath });
  ensureStream(streamId, {
    label: item.command || `Command ${itemId}`,
    type: 'command',
    severity: 'info',
    filePath: logPath,
    itemId,
    tool: 'commandExecution',
    taskId: turnId,
    startedAt: nowIso(),
    meta: {
      cwd: item.cwd,
      command: item.command,
      processId: item.processId || null
    }
  });
  state.activeCommand = {
    itemId,
    command: item.command,
    cwd: item.cwd,
    processId: item.processId || null,
    startedAt: nowIso()
  };
}

function handleNotification(method, params) {
  switch (method) {
    case 'turn/started': {
      state.currentTurnId = params?.turn?.id || state.currentTurnId;
      state.currentTurnStatus = params?.turn?.status || 'inProgress';
      state.runStatus = 'running';
      recordEvent('agent.turn_started', 'Codex reported a running turn', {
        turnId: state.currentTurnId,
        status: state.currentTurnStatus
      });
      break;
    }
    case 'turn/completed': {
      state.currentTurnId = params?.turn?.id || state.currentTurnId;
      state.currentTurnStatus = params?.turn?.status || 'completed';
      state.runStatus = params?.turn?.status === 'failed' ? 'failed' : params?.turn?.status === 'interrupted' ? 'interrupted' : 'running';
      state.activeItems.clear();
      state.activeCommand = null;
      recordEvent('agent.turn_completed', `Turn ${state.currentTurnStatus}`, {
        turnId: state.currentTurnId,
        status: state.currentTurnStatus,
        error: params?.turn?.error || null
      }, state.currentTurnStatus === 'failed' ? 'error' : state.currentTurnStatus === 'interrupted' ? 'warning' : 'info');
      if (state.currentTurnStatus !== 'inProgress') {
        state.currentTurnId = '';
      }
      completePendingInterruptPrompts();
      processOperatorActions();
      break;
    }
    case 'item/started': {
      const item = params?.item;
      if (!item?.id) {
        break;
      }
      const summary = item.type === 'commandExecution'
        ? item.command
        : item.type === 'mcpToolCall'
          ? `${item.server}/${item.tool}`
          : item.type;
      state.activeItems.set(item.id, {
        id: item.id,
        type: item.type,
        summary,
        startedAt: nowIso()
      });
      if (item.type === 'commandExecution') {
        updateCommandState(item, params?.turnId || state.currentTurnId);
        recordEvent('command.started', `Command started: ${item.command}`, {
          itemId: item.id,
          turnId: params?.turnId || state.currentTurnId,
          command: item.command,
          cwd: item.cwd,
          processId: item.processId || null
        });
      } else {
        recordEvent('item.started', `Started ${summary}`, {
          itemId: item.id,
          turnId: params?.turnId || state.currentTurnId,
          itemType: item.type,
          summary
        });
      }
      break;
    }
    case 'item/completed': {
      const item = params?.item;
      if (!item?.id) {
        break;
      }
      const started = state.activeItems.get(item.id);
      state.activeItems.delete(item.id);
      if (item.type === 'commandExecution') {
        const commandLog = state.commandLogs.get(item.id);
        const severity = item.exitCode && item.exitCode !== 0 ? 'error' : 'info';
        if (commandLog) {
          updateStreamBytes(commandLog.streamId, commandLog.logPath, {
            completedAt: nowIso(),
            exitCode: item.exitCode ?? null,
            severity,
            meta: {
              command: item.command,
              cwd: item.cwd,
              processId: item.processId || null,
              durationMs: item.durationMs || null
            }
          });
        }
        state.activeCommand = null;
        recordEvent('command.completed', `Command completed${item.exitCode || item.exitCode === 0 ? ` with exit ${item.exitCode}` : ''}`, {
          itemId: item.id,
          turnId: params?.turnId || state.currentTurnId,
          command: item.command,
          cwd: item.cwd,
          exitCode: item.exitCode ?? null,
          durationMs: item.durationMs || null,
          aggregatedOutputBytes: commandLog?.logPath && fs.existsSync(commandLog.logPath) ? fs.statSync(commandLog.logPath).size : 0
        }, severity);
      } else {
        recordEvent('item.completed', `Completed ${item.type}`, {
          itemId: item.id,
          turnId: params?.turnId || state.currentTurnId,
          itemType: item.type,
          durationMs: item.durationMs || null,
          startedAt: started?.startedAt || null
        });
      }
      processOperatorActions();
      break;
    }
    case 'item/commandExecution/outputDelta': {
      const itemId = params?.itemId;
      if (!itemId) {
        break;
      }
      const entry = state.commandLogs.get(itemId) || { streamId: getCommandStreamId(itemId), logPath: getCommandLogPath(itemId) };
      state.commandLogs.set(itemId, entry);
      appendRaw(entry.logPath, params.delta || '');
      updateStreamBytes(entry.streamId, entry.logPath, { updatedAt: nowIso(), severity: 'info' });
      break;
    }
    case 'thread/tokenUsage/updated': {
      state.tokenUsage = params?.tokenUsage || null;
      recordEvent('agent.token_usage_updated', 'Token usage updated', {
        turnId: params?.turnId || state.currentTurnId,
        tokenUsage: state.tokenUsage
      }, 'debug');
      break;
    }
    case 'model/rerouted': {
      state.currentModel = params?.to || state.currentModel;
      recordEvent('agent.model_rerouted', 'Model was rerouted', {
        from: params?.from || null,
        to: params?.to || null,
        reason: params?.reason || null
      }, 'warning');
      break;
    }
    case 'error': {
      state.lastError = params?.message || JSON.stringify(params || {});
      recordEvent('agent.error', 'Codex emitted an error notification', params || {}, 'error');
      break;
    }
    default:
      if (method === 'item/agentMessage/delta' || method === 'item/plan/delta') {
        recordEvent(method === 'item/agentMessage/delta' ? 'agent.message_delta' : 'agent.plan_delta', 'Codex emitted a streamed delta', {
          itemId: params?.itemId || '',
          deltaPreview: String(params?.delta || '').slice(0, 400)
        }, 'debug');
      }
      break;
  }
}

function handleChildLine(rawLine) {
  appendRaw(PROTOCOL_OUT_PATH, `${rawLine}\n`);
  const message = jsonLine(rawLine);
  if (!message) {
    process.stdout.write(`${rawLine}\n`);
    return;
  }
  if (Object.prototype.hasOwnProperty.call(message, 'id')) {
    if (handleBrokerResponse(message)) {
      return;
    }
    handleChildResponse(message);
    process.stdout.write(`${rawLine}\n`);
    processOperatorActions();
    return;
  }
  if (message.method) {
    handleNotification(message.method, message.params || {});
  }
  process.stdout.write(`${rawLine}\n`);
}

function attachLineReader(stream, onLine, { onChunk = null } = {}) {
  stream.setEncoding('utf8');
  if (onChunk) {
    stream.on('data', onChunk);
  }
  const rl = readline.createInterface({ input: stream, crlfDelay: Infinity });
  rl.on('line', onLine);
  return rl;
}

ensureStream('protocol-in', {
  label: 'Protocol In',
  type: 'protocol',
  severity: 'debug',
  filePath: PROTOCOL_IN_PATH,
  tool: 'protocol',
  taskId: RUN.runId,
  startedAt: RUN.startedAt
});
ensureStream('protocol-out', {
  label: 'Protocol Out',
  type: 'protocol',
  severity: 'debug',
  filePath: PROTOCOL_OUT_PATH,
  tool: 'protocol',
  taskId: RUN.runId,
  startedAt: RUN.startedAt
});
ensureStream('bridge-stderr', {
  label: 'Codex stderr',
  type: 'stderr',
  severity: 'error',
  filePath: STDERR_PATH,
  tool: 'stderr',
  taskId: RUN.runId,
  startedAt: RUN.startedAt
});

recordEvent('agent.run_created', 'Codex bridge created a durable run envelope', {
  runId: RUN.runId,
  workerName: WORKER_NAME,
  workspacePath: WORKSPACE_PATH,
  effectiveModel: EFFECTIVE_MODEL || null
});
writeWorkerState({ status: 'busy' });

attachLineReader(process.stdin, handleParentLine);
attachLineReader(child.stdout, handleChildLine);
attachLineReader(child.stderr, (line) => {
  appendRaw(STDERR_PATH, `${line}\n`);
  updateStreamBytes('bridge-stderr', STDERR_PATH, { updatedAt: nowIso(), severity: 'error' });
  process.stderr.write(`${line}\n`);
}, {
  onChunk: (chunk) => {
    if (chunk) {
      appendRaw(STDERR_PATH, chunk);
      updateStreamBytes('bridge-stderr', STDERR_PATH, { updatedAt: nowIso(), severity: 'error' });
    }
  }
});

process.stdin.on('end', () => {
  child.stdin.end();
});

const pollTimer = setInterval(() => {
  processOperatorActions();
}, 1000);

function shutdown(signal) {
  recordEvent('agent.signal', `Bridge received ${signal}`, { signal }, 'warning');
  child.kill(signal);
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

child.on('exit', (code, signal) => {
  clearInterval(pollTimer);
  state.exitCode = code;
  state.runStatus = code === 0 && state.currentTurnStatus !== 'failed' ? 'completed' : 'failed';
  state.currentTurnStatus = state.currentTurnStatus || (code === 0 ? 'completed' : 'failed');
  state.currentTurnId = '';
  state.activeItems.clear();
  state.activeCommand = null;
  if (code !== 0) {
    state.lastError = state.lastError || `Codex app-server exited with code ${code}${signal ? ` (${signal})` : ''}`;
  }
  updateStreamBytes('bridge-stderr', STDERR_PATH, { completedAt: nowIso(), severity: code === 0 ? 'info' : 'error' });
  updateStreamBytes('protocol-in', PROTOCOL_IN_PATH, { completedAt: nowIso() });
  updateStreamBytes('protocol-out', PROTOCOL_OUT_PATH, { completedAt: nowIso() });
  finalizeQueuedActions(`Run exited before delivery${code || signal ? ` (${code ?? signal})` : ''}`);
  updateRun(state.runId, {
    status: state.runStatus,
    currentTurnId: '',
    currentTurnStatus: state.currentTurnStatus,
    currentItem: null,
    currentCommand: null,
    endedAt: nowIso(),
    effectiveModel: state.currentModel || EFFECTIVE_MODEL || '',
    modelProvider: state.modelProvider || '',
    serviceTier: state.serviceTier || '',
    tokenUsage: state.tokenUsage,
    lastError: state.lastError || ''
  }, RUN_ROOT);
  writeWorkerState({ currentTurnId: '', currentTurnStatus: state.currentTurnStatus, currentCommand: null, activeItemCount: 0, lastError: state.lastError || '' });
  recordEvent('agent.exited', `Codex bridge exited${code !== null ? ` with code ${code}` : ''}${signal ? ` (${signal})` : ''}`, {
    code,
    signal,
    finalTurnStatus: state.currentTurnStatus,
    lastError: state.lastError || ''
  }, code === 0 ? 'info' : 'error');
  process.exit(code ?? 1);
});
