import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, AlertTriangle, Bot, Braces, CheckCircle2, Clock3, GitBranch, KeyRound, PauseCircle, PlayCircle, RefreshCw, Send, Shield, SquareArrowOutUpRight, TerminalSquare, Workflow } from 'lucide-react';
import { BrowserRouter, NavLink, Navigate, Route, Routes, useNavigate, useParams } from 'react-router-dom';
import { Button, Card, Empty, Input, Panel, Pill, Select, Table, Textarea } from './components/ui.jsx';
import { coerceList, formatDateTime, formatDuration, formatNumber, formatRelative, getJson, pillStatus } from './lib/api.js';
import './styles.css';

const DEFAULT_FILTERS = { issue: '', worker: '', eventType: '', level: '', search: '', limit: 300 };
const DELIVERY_MODES = ['deliver_after_current_step', 'interrupt_now', 'deliver_when_idle'];
const RUNNING_STATUSES = new Set(['starting', 'running', 'interrupted']);
const NODE_LOG_LIMIT = 500;

function asArray(value) {
  if (Array.isArray(value)) return value;
  if (Array.isArray(value?.nodes)) return value.nodes;
  if (Array.isArray(value?.items)) return value.items;
  if (Array.isArray(value?.entries)) return value.entries;
  if (Array.isArray(value?.data)) return value.data;
  return [];
}

function parseJsonOrNull(value) {
  const text = String(value || '').trim();
  if (!text) return null;
  return JSON.parse(text);
}

function formatNodeState(node) {
  return node?.state || node?.status || node?.runtimeState || node?.health || node?.containerState || 'unknown';
}

function normalizeNode(node = {}) {
  const source = node.source || node.discoverySource || (node.managed ? 'registry' : 'discovery');
  const venues = asArray(node.venues || node.venueNames || node.enabledVenues).map((venue) => {
    if (typeof venue === 'string') return venue;
    if (venue && typeof venue === 'object') {
      return venue.venue || venue.clientKey || venue.client_key || JSON.stringify(venue);
    }
    return String(venue || '');
  }).filter(Boolean);
  return {
    raw: node,
    nodeId: node.nodeId || node.id || node.name || node.containerName || 'unknown',
    displayName: node.displayName || node.title || node.nodeId || node.id || node.name || 'Unknown node',
    hostId: node.hostId || node.host || node.hostName || 'local',
    hostName: node.hostName || node.hostDisplayName || node.host || node.hostId || 'local',
    hostKind: node.hostKind || node.executorKind || node.kind || 'local',
    strategyId: node.strategyId || node.strategy || node.strategyName || 'betting_arbitrage',
    manifestId: node.manifestId || node.manifestFile || node.manifest || 'unknown',
    containerName: node.containerName || node.container || node.name || 'unknown',
    image: node.image || node.imageRef || node.currentImage || node.runtime?.image || node.release?.image || 'unknown',
    venues,
    source,
    managed: node.managed !== undefined ? Boolean(node.managed) : source === 'registry' || source.includes('managed'),
    intendedState: node.intendedState || node.desiredState || node.requestedState || 'running',
    lastHeartbeatAt: node.lastHeartbeatAt || node.heartbeatAt || node.lastSeenAt || node.observedAt || null,
    updatedAt: node.updatedAt || node.discoveredAt || node.syncedAt || node.lastSeenAt || null,
    runtimeConfigPath: node.runtimeConfigPath || node.renderedConfigPath || node.configPath || null,
    logPath: node.logPath || node.logsPath || null,
    registry: node.registry || null,
    discovery: node.discovery || node.observed || null,
    release: node.release || null,
    config: node.config || node.runtimeConfig || node.manifestRuntime || null,
    rawNode: node
  };
}

function normalizeNodesPayload(payload) {
  return asArray(payload).map((node) => normalizeNode(node));
}

function normalizeNodeDetail(payload, nodeId) {
  const node = payload?.node || payload?.item || payload?.data || payload || {};
  const summary = normalizeNode({ ...node, nodeId: node.nodeId || nodeId });
  return {
    ...payload,
    node: summary,
    registry: payload?.registry || payload?.registryEntry || node.registry || null,
    registryEntry: payload?.registryEntry || payload?.registry || node.registry || null,
    discovery: payload?.discovery || payload?.discoveryEntry || node.discovery || null,
    discoveryEntry: payload?.discoveryEntry || payload?.discovery || node.discovery || null,
    logs: payload?.logs || node.logs || null,
    configPreview: payload?.configPreview || node.configPreview || null,
    effectiveConfig: payload?.effectiveConfig || payload?.manifest || node.effectiveConfig || null,
    history: asArray(payload?.history || node.history),
  };
}

function normalizeNodeLogText(payload) {
  if (typeof payload === 'string') return payload;
  if (!payload || typeof payload !== 'object') return String(payload || '');
  if (typeof payload.content === 'string') return payload.content;
  if (typeof payload.chunk === 'string') return payload.chunk;
  if (typeof payload.text === 'string') return payload.text;
  if (typeof payload.log === 'string') return payload.log;
  if (typeof payload.output === 'string') return payload.output;
  if (Array.isArray(payload.lines)) return payload.lines.join('\n');
  if (Array.isArray(payload.entries)) return payload.entries.map((entry) => entry.line || entry.message || JSON.stringify(entry)).join('\n');
  return JSON.stringify(payload, null, 2);
}

function Metric({ label, value, detail, icon: Icon }) {
  return (
    <Card className="metric-card">
      <div className="metric-icon">{Icon ? <Icon size={18} /> : null}</div>
      <p className="metric-label">{label}</p>
      <p className="metric-value">{value}</p>
      <p className="metric-detail">{detail}</p>
    </Card>
  );
}

function JsonBlock({ value, empty = 'No recent action.' }) {
  return <pre className="provider-output">{value ? JSON.stringify(value, null, 2) : empty}</pre>;
}

function LinkButton({ children, onClick }) {
  return <button className="link-button" type="button" onClick={onClick}>{children}</button>;
}

function RuntimeProfileEditor({ providerId, workerName, profile = {}, onSave }) {
  const [tier, setTier] = useState(profile.subscriptionTier || '');
  const [model, setModel] = useState(profile.runtimeModel || '');
  const [models, setModels] = useState((profile.availableModels || []).join('\n'));
  const [notes, setNotes] = useState(profile.notes || '');

  useEffect(() => {
    setTier(profile.subscriptionTier || '');
    setModel(profile.runtimeModel || '');
    setModels((profile.availableModels || []).join('\n'));
    setNotes(profile.notes || '');
  }, [profile.subscriptionTier, profile.runtimeModel, profile.notes, JSON.stringify(profile.availableModels || [])]);

  return (
    <div className="runtime-editor">
      <div className="muted">Selection mode: {profile.selectionMode || 'provider-default'}</div>
      <div className="muted">Effective runtime model: {profile.effectiveRuntimeModel || 'provider default'}</div>
      <Input placeholder="free / plus / pro / max / team" value={tier} onChange={(event) => setTier(event.target.value)} />
      <Input placeholder="Exact runtime model id to pin, or leave blank for provider default" value={model} onChange={(event) => setModel(event.target.value)} />
      <Textarea rows={3} placeholder="Known available models, one per line or comma-separated" value={models} onChange={(event) => setModels(event.target.value)} />
      <Textarea rows={3} placeholder="Operational notes" value={notes} onChange={(event) => setNotes(event.target.value)} />
      <Button onClick={() => onSave(providerId, workerName, { subscriptionTier: tier, runtimeModel: model, availableModels: coerceList(models), notes })}>Save Runtime Profile</Button>
    </div>
  );
}

function SettingsPanel({ overview, onSave }) {
  const [deliveryMode, setDeliveryMode] = useState(overview?.settings?.defaultPromptDeliveryMode || 'deliver_after_current_step');
  const [retentionDays, setRetentionDays] = useState(String(overview?.settings?.rawLogRetentionDays || 30));

  useEffect(() => {
    setDeliveryMode(overview?.settings?.defaultPromptDeliveryMode || 'deliver_after_current_step');
    setRetentionDays(String(overview?.settings?.rawLogRetentionDays || 30));
  }, [overview?.settings?.defaultPromptDeliveryMode, overview?.settings?.rawLogRetentionDays]);

  return (
    <div className="form-grid compact">
      <label>
        <span>Default prompt delivery</span>
        <Select value={deliveryMode} onChange={(event) => setDeliveryMode(event.target.value)}>
          {DELIVERY_MODES.map((mode) => <option key={mode} value={mode}>{mode}</option>)}
        </Select>
      </label>
      <label>
        <span>Raw log retention days</span>
        <Input type="number" min="1" value={retentionDays} onChange={(event) => setRetentionDays(event.target.value)} />
      </label>
      <Button onClick={() => onSave({ defaultPromptDeliveryMode: deliveryMode, rawLogRetentionDays: retentionDays })}>Save Settings</Button>
    </div>
  );
}

function ProviderPanels({ overview, outputs, onCodexAction, onOpenRouterSave, onAntigravityConfig, onAntigravityAction, onProfileSave }) {
  const [openRouterKey, setOpenRouterKey] = useState('');
  const [agConfig, setAgConfig] = useState({ publicBaseUrl: '', clientId: '', clientSecret: '', scopes: '' });
  const [callbacks, setCallbacks] = useState({});

  const antigravity = overview?.providers?.antigravity;
  useEffect(() => {
    const config = antigravity?.config || {};
    setAgConfig({
      publicBaseUrl: config.publicBaseUrl || '',
      clientId: config.clientId || '',
      clientSecret: '',
      scopes: Array.isArray(config.scopes) ? config.scopes.join('\n') : String(config.scopes || '')
    });
  }, [antigravity?.config?.publicBaseUrl, antigravity?.config?.clientId, JSON.stringify(antigravity?.config?.scopes || [])]);

  return (
    <div className="provider-grid">
      {overview?.providers?.codex ? (
        <Card className="provider-card">
          <h3><KeyRound size={16} /> Codex ChatGPT Auth</h3>
          <p className="subtle">Start remote device auth on EC2, then persist and restore worker auth via Secrets Manager. This is only needed for control-plane-driven remote Codex work, not for the GitHub Actions SSH deploy path.</p>
          {(overview.providers.codex.workers || []).map((worker) => (
            <div className="provider-worker" key={worker.worker}>
              <strong>{worker.displayName}</strong>
              <div className="muted">Expected account: {worker.email}</div>
              <div className="muted">Remote auth: {worker.authPresent ? (worker.authEmail || 'present') : 'missing'}</div>
              <div className="muted">Secret stored: {worker.secretStored ? 'yes' : 'no'}</div>
              <RuntimeProfileEditor providerId="codex" workerName={worker.worker} profile={worker.profile || {}} onSave={onProfileSave} />
              {worker.session ? <JsonBlock value={worker.session} /> : null}
              <div className="action-row">
                <Button onClick={() => onCodexAction(worker.worker, 'startAuth')}>Generate Auth Link</Button>
                <Button onClick={() => onCodexAction(worker.worker, 'persist')}>Persist to Secrets</Button>
                <Button onClick={() => onCodexAction(worker.worker, 'restore')}>Restore from Secrets</Button>
              </div>
            </div>
          ))}
          <JsonBlock value={outputs.codex} />
        </Card>
      ) : null}

      {overview?.providers?.openrouter ? (
        <Card className="provider-card">
          <h3><Braces size={16} /> OpenRouter</h3>
          <p className="subtle">Store the shared API key in AWS Secrets Manager for OpenRouter-backed workers.</p>
          <div className="muted">Secret configured: {overview.providers.openrouter.secretConfigured ? 'yes' : 'no'}</div>
          <div className="muted">Secret key: {overview.providers.openrouter.secretKey || 'OPEN_ROUTER_API_KEY'}</div>
          <Input type="password" placeholder="OPEN_ROUTER_API_KEY" value={openRouterKey} onChange={(event) => setOpenRouterKey(event.target.value)} />
          <Button onClick={() => onOpenRouterSave(openRouterKey)}>Save API Key</Button>
          <JsonBlock value={outputs.openrouter} />
        </Card>
      ) : null}

      {antigravity ? (
        <Card className="provider-card wide">
          <h3><Workflow size={16} /> Antigravity CLI</h3>
          <p className="subtle">{antigravity.guidance || 'Configure one OAuth web client and reuse it across Google accounts.'}</p>
          <div className="nested-columns">
            <div className="form-grid">
              <label><span>Public base URL</span><Input value={agConfig.publicBaseUrl} onChange={(event) => setAgConfig({ ...agConfig, publicBaseUrl: event.target.value })} /></label>
              <label><span>Google client ID</span><Input value={agConfig.clientId} onChange={(event) => setAgConfig({ ...agConfig, clientId: event.target.value })} /></label>
              <label><span>Google client secret</span><Input type="password" value={agConfig.clientSecret} onChange={(event) => setAgConfig({ ...agConfig, clientSecret: event.target.value })} /></label>
              <label><span>Scopes</span><Textarea rows={4} value={agConfig.scopes} onChange={(event) => setAgConfig({ ...agConfig, scopes: event.target.value })} /></label>
              <Button onClick={() => onAntigravityConfig(agConfig)}>Save OAuth Config</Button>
            </div>
            <JsonBlock value={antigravity.config} empty="Antigravity config unavailable." />
          </div>
          {(antigravity.workers || []).map((worker) => (
            <div className="provider-worker" key={worker.worker}>
              <strong>{worker.displayName}</strong>
              <div className="muted">Expected Google account: {worker.email}</div>
              <div className="muted">Remote auth: {worker.authPresent ? (worker.authRecord?.email || 'present') : 'missing'}</div>
              <div className="muted">Project: {worker.authRecord?.projectId || worker.authRecord?.managedProjectId || 'pending discovery'}</div>
              <div className="muted">Secret stored: {worker.secretStored ? 'yes' : 'no'}</div>
              <div className="muted">Auth path: {worker.authPath}</div>
              <RuntimeProfileEditor providerId="antigravity" workerName={worker.worker} profile={worker.profile || {}} onSave={onProfileSave} />
              {worker.session?.authUrl ? <p><a href={worker.session.authUrl} target="_blank" rel="noreferrer">Open latest auth link <SquareArrowOutUpRight size={12} /></a></p> : null}
              <Textarea rows={3} placeholder="Paste the callback URL or authorization code to complete manually." value={callbacks[worker.worker] || ''} onChange={(event) => setCallbacks({ ...callbacks, [worker.worker]: event.target.value })} />
              <div className="action-row">
                <Button onClick={() => onAntigravityAction(worker.worker, 'startAuth')}>Generate Auth Link</Button>
                <Button onClick={() => onAntigravityAction(worker.worker, 'completeAuth', callbacks[worker.worker] || '')}>Complete Manually</Button>
                <Button onClick={() => onAntigravityAction(worker.worker, 'persist')}>Persist Remote Auth</Button>
                <Button onClick={() => onAntigravityAction(worker.worker, 'restore')}>Restore Remote Auth</Button>
              </div>
              <JsonBlock value={worker.session || worker.authRecord || { status: 'not_configured' }} />
            </div>
          ))}
          <JsonBlock value={outputs.antigravity} />
        </Card>
      ) : null}
    </div>
  );
}

function StrategyNodeDeploymentsPanel({ overview, onRequest }) {
  const manifests = overview?.strategyNodes?.manifests || [];
  const requests = overview?.strategyNodes?.requests || [];
  const [manifestFile, setManifestFile] = useState(manifests[0]?.manifestFile || '');
  const [rolloutMode, setRolloutMode] = useState('validate_only');
  const [target, setTarget] = useState('production');
  const [workerName, setWorkerName] = useState(manifests[0]?.operatorFlow?.recommendedWorker || 'codex-a');
  const [requestedBy, setRequestedBy] = useState('control-plane');
  const [imageRef, setImageRef] = useState('');
  const [notes, setNotes] = useState('');

  useEffect(() => {
    if (!manifests.length) return;
    setManifestFile((current) => current || manifests[0].manifestFile || '');
    setWorkerName((current) => current || manifests[0]?.operatorFlow?.recommendedWorker || 'codex-a');
  }, [manifests.map((manifest) => manifest.manifestFile).join('|')]);

  const selectedManifest = manifests.find((manifest) => manifest.manifestFile === manifestFile) || manifests[0] || null;

  return (
    <Card>
      <h3><Workflow size={16} /> Strategy Nodes</h3>
      <p className="subtle">Validate a deployable manifest, queue a rollout request, and review the current request backlog for betting-arbitrage nodes. GCP handles CI/build work; EC2 remains the deploy and trading host.</p>
      <div className="nested-columns">
        <div className="form-grid">
          <label>
            <span>Manifest</span>
            <Select value={manifestFile} onChange={(event) => setManifestFile(event.target.value)}>
              {manifests.map((manifest) => <option key={manifest.manifestFile} value={manifest.manifestFile}>{manifest.manifestFile}</option>)}
            </Select>
          </label>
          <label>
            <span>Rollout mode</span>
            <Select value={rolloutMode} onChange={(event) => setRolloutMode(event.target.value)}>
              <option value="validate_only">validate_only</option>
              <option value="deploy">deploy</option>
            </Select>
          </label>
          <label>
            <span>Target</span>
            <Select value={target} onChange={(event) => setTarget(event.target.value)}>
              <option value="production">production</option>
              <option value="staging">staging</option>
              <option value="dry-run">dry-run</option>
            </Select>
          </label>
          <label>
            <span>Requested by</span>
            <Input value={requestedBy} onChange={(event) => setRequestedBy(event.target.value)} />
          </label>
          <label>
            <span>Worker</span>
            <Input value={workerName} onChange={(event) => setWorkerName(event.target.value)} />
          </label>
          <label>
            <span>Image ref override</span>
            <Input placeholder="Optional GHCR image ref" value={imageRef} onChange={(event) => setImageRef(event.target.value)} />
          </label>
          <label>
            <span>Notes</span>
            <Textarea rows={3} value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Mixed-venue validation notes, operator context, or rollout caveats." />
          </label>
          <Button onClick={() => onRequest({ manifestFile, rolloutMode, target, requestedBy, workerName, imageRef, notes, metadata: { track: selectedManifest?.metadata?.track || '', phase: selectedManifest?.metadata?.phase || '' } })}>Queue Deployment Request</Button>
        </div>
        <div className="form-grid">
          <Card>
            <h4>Selected Manifest</h4>
            {selectedManifest ? (
              <>
                <div className="muted">Node: {selectedManifest.nodeId || 'unknown'}</div>
                <div className="muted">Trader: {selectedManifest.traderId || 'unknown'}</div>
                <div className="muted">Recommended worker: {selectedManifest.operatorFlow?.recommendedWorker || 'codex-a'}</div>
                <div className="muted">Dummy credentials allowed: {selectedManifest.allowDummyCredentials ? 'yes' : 'no'}</div>
                <div className="muted">Validation mode: {selectedManifest.validationMode ? 'yes' : 'no'}</div>
                <div className="muted">Required live secrets: {(selectedManifest.requirements?.requiredEnvKeys || []).join(', ') || 'none'}</div>
                <div className="muted">Validation fallback: {(selectedManifest.requirements?.dummyCredentialKeys || []).join(', ') || 'none'}</div>
                <div className="muted">Worker auth purpose: {selectedManifest.requirements?.workerAuthPurpose || 'Only required for control-plane remote worker actions.'}</div>
                <div className="muted">Local auth step (control-plane remote worker only): <code>{selectedManifest.operatorFlow?.localAuthCommand || './scripts/symphony/capture_worker_auth.sh codex-a'}</code></div>
                <div className="muted">Install step (control-plane remote worker only): <code>{selectedManifest.operatorFlow?.installCommand || './scripts/symphony/install_worker_auths.sh'}</code></div>
                <div className="muted">Start step: <pre>{selectedManifest.operatorFlow?.startCommandTemplate || 'ssh ... deploy_betting_strategy_node.sh ...'}</pre></div>
                <div className="muted">Monitor step: <pre>{selectedManifest.operatorFlow?.monitorCommandTemplate || './scripts/deploy/strategy_nodes/wait_for_strategy_node_status.sh ...'}</pre></div>
              </>
            ) : <Empty>No strategy-node manifests found.</Empty>}
          </Card>
          <Card>
            <h4>Manifest Requirements</h4>
            {selectedManifest ? <JsonBlock value={selectedManifest.requirements || {}} empty="No manifest requirements captured." /> : <Empty>Select a manifest to inspect requirements.</Empty>}
          </Card>
        </div>
      </div>
      <div className="nested-columns">
        <Card>
          <h4>Deployment Requests</h4>
          {requests.length ? requests.map((request) => (
            <div className="comment-card" key={request.id}>
              <div className="timeline-meta">
                <span>{formatDateTime(request.requestedAt)}</span>
                <span className="tag">{request.rolloutMode || 'validate_only'}</span>
                <span className="tag">{request.status || 'queued'}</span>
                <span className="tag">{request.workerName || 'codex-a'}</span>
              </div>
              <strong>{request.manifestFile}</strong>
              <div className="muted">Target: {request.target || 'production'} · Requested by {request.requestedBy || 'control-plane'}</div>
              {request.notes ? <pre>{request.notes}</pre> : null}
              <div className="muted">Image: {request.imageRef || 'manifest/default'}</div>
            </div>
          )) : <Empty>No deployment requests have been queued yet.</Empty>}
        </Card>
      </div>
    </Card>
  );
}

function Timeline({ events, filters, setFilters, onApply, onReset, selectedRunId }) {
  return (
    <>
      {!selectedRunId ? (
        <div className="timeline-filter-grid">
          <Input placeholder="Issue, e.g. BET-11" value={filters.issue} onChange={(event) => setFilters({ ...filters, issue: event.target.value })} />
          <Input placeholder="Worker, e.g. codex-c" value={filters.worker} onChange={(event) => setFilters({ ...filters, worker: event.target.value })} />
          <Input placeholder="Event type" value={filters.eventType} onChange={(event) => setFilters({ ...filters, eventType: event.target.value })} />
          <Select value={filters.level} onChange={(event) => setFilters({ ...filters, level: event.target.value })}>
            <option value="">Any level</option>
            <option value="info">info</option>
            <option value="warning">warning</option>
            <option value="error">error</option>
          </Select>
          <Input placeholder="Search" value={filters.search} onChange={(event) => setFilters({ ...filters, search: event.target.value })} />
          <Input type="number" min="1" max="1000" value={filters.limit} onChange={(event) => setFilters({ ...filters, limit: Number(event.target.value || 300) })} />
          <Button onClick={onApply}>Apply</Button>
          <Button variant="ghost" onClick={onReset}>Reset</Button>
        </div>
      ) : <p className="subtle">Showing timeline for selected run.</p>}
      <div className="timeline-list">
        {events.length ? events.map((event) => <TimelineEvent event={event} key={event.cursor || event.id} />) : <Empty>No timeline events match the current view.</Empty>}
      </div>
    </>
  );
}

function TimelineEvent({ event }) {
  return (
    <article className={`timeline-event level-${pillStatus(event.level)}`}>
      <div className="timeline-rail"><span /></div>
      <div className="timeline-body">
        <div className="timeline-meta">
          <span>{formatDateTime(event.timestamp)}</span>
          {event.workerName ? <span className="tag">{event.workerName}</span> : null}
          {event.issueIdentifier ? <span className="tag">{event.issueIdentifier}</span> : null}
          <span className="tag">{event.eventType || 'event'}</span>
          <span className="tag">{event.level || 'info'}</span>
        </div>
        <strong>{event.summary || 'Event'}</strong>
        {event.payload && Object.keys(event.payload).length ? <JsonBlock value={event.payload} empty="" /> : null}
      </div>
    </article>
  );
}

function RunDetail({ detail, overview, logStreamId, setLogStreamId, logContent, onPrompt, onInterrupt, onCheckpoint }) {
  const [prompt, setPrompt] = useState('');
  const [deliveryMode, setDeliveryMode] = useState(overview?.settings?.defaultPromptDeliveryMode || 'deliver_after_current_step');
  const [reason, setReason] = useState('');
  const [checkpointNote, setCheckpointNote] = useState('');

  useEffect(() => {
    setDeliveryMode(overview?.settings?.defaultPromptDeliveryMode || 'deliver_after_current_step');
  }, [overview?.settings?.defaultPromptDeliveryMode, detail?.run?.runId]);

  if (!detail?.run) {
    return <Empty>Select a run to inspect the orchestration timeline, queue prompts, interrupt, and tail logs.</Empty>;
  }

  const run = detail.run;
  const actions = detail.operatorActions || [];
  const streams = detail.logStreams || [];

  return (
    <div className="run-detail-grid">
      <Card className="run-summary">
        <div className="run-title-row">
          <h3>{run.runId}</h3>
          <Pill status={pillStatus(run.status)}>{run.status || 'unknown'}</Pill>
        </div>
        <p className="subtle">Issue {run.issueIdentifier || 'unknown'} · Worker {run.workerName || 'unknown'} · Model {run.effectiveModel || 'provider default'}</p>
        <div className="stats-grid">
          <Metric label="Duration" value={formatDuration(run.startedAt, run.endedAt)} detail={run.startedAt ? `Started ${formatRelative(run.startedAt)}` : 'No start time'} icon={Clock3} />
          <Metric label="Events" value={formatNumber(run.counts?.events || 0)} detail="Timeline entries" icon={Activity} />
          <Metric label="Actions" value={formatNumber(run.counts?.operatorActions || actions.length)} detail="Operator prompts/checkpoints" icon={Send} />
        </div>
      </Card>

      <Card>
        <h3>Operator Controls</h3>
        <label><span>Prompt / instruction</span><Textarea rows={5} value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Add trusted operator guidance, context, or a direct instruction for the active run." /></label>
        <label><span>Delivery mode</span><Select value={deliveryMode} onChange={(event) => setDeliveryMode(event.target.value)}>{DELIVERY_MODES.map((mode) => <option key={mode} value={mode}>{mode}</option>)}</Select></label>
        <label><span>Interrupt reason</span><Input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Why should this run be interrupted?" /></label>
        <label><span>Checkpoint note</span><Input value={checkpointNote} onChange={(event) => setCheckpointNote(event.target.value)} placeholder="Optional checkpoint note" /></label>
        <div className="action-row">
          <Button onClick={() => onPrompt(run.runId, prompt, deliveryMode).then(() => setPrompt(''))}><Send size={14} /> Queue Prompt</Button>
          <Button onClick={() => onCheckpoint(run.runId, checkpointNote, deliveryMode).then(() => setCheckpointNote(''))}><CheckCircle2 size={14} /> Request Checkpoint</Button>
          <Button variant="danger" onClick={() => onInterrupt(run.runId, reason).then(() => setReason(''))}><PauseCircle size={14} /> Interrupt</Button>
        </div>
      </Card>

      <Card>
        <h3>Operator Queue</h3>
        {actions.length ? actions.map((action) => (
          <div className="operator-action" key={action.actionId || action.id}>
            <div className="timeline-meta"><span>{formatDateTime(action.createdAt || action.timestamp)}</span><span className="tag">{action.actionType || action.type}</span><span className="tag">{action.deliveryMode || 'default'}</span></div>
            {action.prompt ? <pre>{action.prompt}</pre> : <p>{action.reason || action.note || 'No details provided.'}</p>}
          </div>
        )) : <Empty>No operator prompts or interrupts have been queued for this run.</Empty>}
      </Card>

      <Card className="log-card">
        <h3><TerminalSquare size={16} /> Execution Log Stream</h3>
        <div className="action-row wrap">
          {streams.length ? streams.map((stream) => (
            <Button key={stream.id} variant={stream.id === logStreamId ? 'default' : 'ghost'} onClick={() => setLogStreamId(stream.id)}>{stream.label || stream.id}</Button>
          )) : <span className="muted">No log streams recorded.</span>}
        </div>
        <pre className="log-output">{logStreamId ? (logContent || '') : 'Select a log stream to tail it live.'}</pre>
      </Card>
    </div>
  );
}

function MissionControlPage() {
  const [overview, setOverview] = useState(null);
  const [timeline, setTimeline] = useState([]);
  const [timelineFilters, setTimelineFilters] = useState(DEFAULT_FILTERS);
  const [selectedIssue, setSelectedIssue] = useState('');
  const [issueDetail, setIssueDetail] = useState(null);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [runDetail, setRunDetail] = useState(null);
  const [selectedLogStreamId, setSelectedLogStreamId] = useState('');
  const [logOffsets, setLogOffsets] = useState({});
  const [logContents, setLogContents] = useState({});
  const [providerOutputs, setProviderOutputs] = useState({});
  const [streamStatus, setStreamStatus] = useState('disconnected');
  const [error, setError] = useState('');
  const eventSourceRef = useRef(null);

  const api = useCallback(async (fn) => {
    try {
      setError('');
      return await fn();
    } catch (err) {
      setError(err.message || String(err));
      throw err;
    }
  }, []);

  const loadOverview = useCallback(() => api(async () => {
    const payload = await getJson('/control/api/overview');
    setOverview(payload);
    return payload;
  }), [api]);

  const timelineParams = useMemo(() => {
    if (selectedRunId) return new URLSearchParams({ runId: selectedRunId, limit: '500' });
    const params = new URLSearchParams();
    Object.entries(timelineFilters).forEach(([key, value]) => {
      if (value !== null && value !== undefined && String(value).trim() !== '') params.set(key, String(value));
    });
    return params;
  }, [selectedRunId, timelineFilters]);

  const loadTimeline = useCallback(() => api(async () => {
    const payload = await getJson(`/control/api/timeline?${timelineParams.toString()}`);
    setTimeline(payload.events || []);
  }), [api, timelineParams]);

  const loadIssue = useCallback((identifier) => api(async () => {
    setSelectedIssue(identifier);
    const payload = await getJson(`/control/api/issues/${encodeURIComponent(identifier)}`);
    setIssueDetail(payload);
  }), [api]);

  const loadRun = useCallback((runId) => api(async () => {
    setSelectedRunId(runId);
    const payload = await getJson(`/control/api/runs/${encodeURIComponent(runId)}`);
    setRunDetail(payload);
    const streams = payload.logStreams || [];
    setSelectedLogStreamId((current) => streams.some((stream) => stream.id === current) ? current : (streams[0]?.id || ''));
  }), [api]);

  const loadSelectedLog = useCallback((reset = false) => api(async () => {
    if (!selectedRunId || !selectedLogStreamId) return;
    const offset = reset ? 0 : Number(logOffsets[selectedLogStreamId] || 0);
    const payload = await getJson(`/control/api/runs/${encodeURIComponent(selectedRunId)}/logs/${encodeURIComponent(selectedLogStreamId)}?offset=${offset}&limit=32768`);
    setLogContents((current) => ({ ...current, [selectedLogStreamId]: reset ? (payload.chunk || '') : `${current[selectedLogStreamId] || ''}${payload.chunk || ''}` }));
    setLogOffsets((current) => ({ ...current, [selectedLogStreamId]: payload.nextOffset || offset }));
  }), [api, selectedRunId, selectedLogStreamId, logOffsets]);

  useEffect(() => { loadOverview(); }, [loadOverview]);
  useEffect(() => { loadTimeline(); }, [loadTimeline]);
  useEffect(() => { if (selectedRunId && selectedLogStreamId) loadSelectedLog(true); }, [selectedRunId, selectedLogStreamId]);
  useEffect(() => {
    if (!selectedRunId || !selectedLogStreamId) return undefined;
    const timer = window.setInterval(() => { loadSelectedLog(false).catch(() => {}); }, 2000);
    return () => window.clearInterval(timer);
  }, [selectedRunId, selectedLogStreamId, loadSelectedLog]);

  useEffect(() => {
    if (eventSourceRef.current) eventSourceRef.current.close();
    const stream = new EventSource(`/control/api/stream?${timelineParams.toString()}`);
    eventSourceRef.current = stream;
    setStreamStatus('reconnecting');
    let refreshTimer = null;
    const refresh = () => {
      window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(() => {
        loadOverview().catch(() => {});
        loadTimeline().catch(() => {});
        if (selectedRunId) loadRun(selectedRunId).catch(() => {});
        if (selectedIssue) loadIssue(selectedIssue).catch(() => {});
      }, 600);
    };
    stream.addEventListener('open', () => setStreamStatus('connected'));
    stream.addEventListener('timeline', refresh);
    stream.addEventListener('error', (event) => setStreamStatus(event.target?.readyState === EventSource.CLOSED ? 'disconnected' : 'reconnecting'));
    return () => {
      window.clearTimeout(refreshTimer);
      stream.close();
    };
  }, [timelineParams, loadOverview, loadTimeline, loadRun, loadIssue, selectedRunId, selectedIssue]);

  const metrics = useMemo(() => {
    if (!overview) return [];
    const availableWorkers = (overview.workers || []).filter((worker) => worker.availableForNewWork).length;
    const runningCount = (overview.runs || []).filter((run) => !run.endedAt && RUNNING_STATUSES.has(run.status)).length;
    const diskUsed = overview.host?.disk?.usedPercent || 'n/a';
    const load = Array.isArray(overview.host?.load) ? overview.host.load.slice(0, 3).map((value) => Number(value).toFixed(2)).join(' / ') : 'n/a';
    return [
      { label: 'Running Runs', value: runningCount, detail: `Recent runs tracked: ${(overview.runs || []).length}`, icon: PlayCircle },
      { label: 'Available Workers', value: availableWorkers, detail: `Configured: ${(overview.workers || []).length}`, icon: Bot },
      { label: 'Human Inbox', value: (overview.humanInbox || []).length, detail: 'Needs Human / Manual Action / Awaiting Credentials', icon: AlertTriangle },
      { label: 'Stalled Issues', value: (overview.stalledIssues || []).length, detail: 'In Progress or Rework without a running session', icon: Shield },
      { label: 'Disk Use', value: diskUsed, detail: `Host load: ${load}`, icon: Activity }
    ];
  }, [overview]);

  const refreshAll = useCallback(async () => {
    await loadOverview();
    await loadTimeline();
    if (selectedRunId) await loadRun(selectedRunId);
    if (selectedIssue) await loadIssue(selectedIssue);
  }, [loadOverview, loadTimeline, loadRun, loadIssue, selectedRunId, selectedIssue]);

  async function postJson(url, body) {
    const result = await getJson(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    await refreshAll();
    return result;
  }

  const rows = {
    human: (overview?.humanInbox || []).map((issue) => <tr key={issue.identifier}><td><LinkButton onClick={() => loadIssue(issue.identifier)}>{issue.identifier}</LinkButton><div className="muted">{issue.title}</div></td><td><Pill status={pillStatus(issue.state?.name)}>{issue.state?.name || 'unknown'}</Pill></td><td>{formatRelative(issue.updatedAt)}</td><td>{issue.pullRequestUrl ? <a href={issue.pullRequestUrl} target="_blank" rel="noreferrer">Open PR</a> : 'none'}</td></tr>),
    runs: (overview?.runs || []).map((run) => <tr key={run.runId}><td><LinkButton onClick={() => loadRun(run.runId)}>{run.runId}</LinkButton></td><td><Pill status={pillStatus(run.status)}>{run.status || 'unknown'}</Pill></td><td>{run.issueIdentifier || 'none'}</td><td>{run.workerName || 'unknown'}</td><td><div>{run.effectiveModel || 'provider default'}</div><div className="muted">{run.modelProvider || run.provider || 'codex'}</div></td><td>{formatRelative(run.startedAt)}</td><td><Button onClick={() => loadRun(run.runId)}>Open</Button></td></tr>),
    workers: (overview?.workers || []).map((worker) => <tr key={worker.name}><td><strong>{worker.displayName || worker.name}</strong><div className="muted">{worker.user}</div></td><td><Pill status={pillStatus(worker.status)}>{worker.status}</Pill><div className="muted">{worker.availableForNewWork ? 'ready for new work' : 'not available'}</div></td><td>{worker.issueIdentifier || 'none'}{worker.latestRun?.runId ? <div className="muted">Latest run: <LinkButton onClick={() => loadRun(worker.latestRun.runId)}>{worker.latestRun.runId}</LinkButton></div> : null}</td><td>{worker.email || 'unknown'}</td><td><div className="muted">Tier: {worker.codexSubscriptionTier || 'unset'}</div><div className="muted">Model: {worker.effectiveCodexModel || 'provider default'}</div><div className="muted">Policy: {worker.codexSelectionMode || 'provider-default'}</div></td><td><div className="action-row"><Button onClick={() => postJson(`/control/api/workers/${encodeURIComponent(worker.name)}/action`, { action: worker.cordoned ? 'resume' : 'cordon' })}>{worker.cordoned ? 'Resume' : 'Pause New Work'}</Button>{worker.status === 'cooldown' || worker.status === 'rate_limited' ? <Button onClick={() => postJson(`/control/api/workers/${encodeURIComponent(worker.name)}/action`, { action: 'clearCooldown' })}>Clear Cooldown</Button> : null}</div></td></tr>),
    issues: (overview?.issues || []).map((issue) => <tr key={issue.identifier}><td><LinkButton onClick={() => loadIssue(issue.identifier)}>{issue.identifier}</LinkButton><div className="muted">{issue.title}</div></td><td><Pill status={pillStatus(issue.state?.name)}>{issue.state?.name || 'unknown'}</Pill></td><td><div className="muted">Worker: {issue.workerName || 'unassigned'}</div><div className="muted">Run: {issue.latestRunId || 'none'}</div></td><td>{formatRelative(issue.updatedAt)}</td><td>{issue.pullRequestUrl ? <a href={issue.pullRequestUrl} target="_blank" rel="noreferrer">Open PR</a> : 'none'}</td></tr>)
  };

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">Betting Arbitrage Control Plane</p>
          <h1>Symphony Mission Control</h1>
          <p className="subtitle">A React operator surface for Symphony, Linear, GitHub, provider auth, Codex workers, and in-flight run control.</p>
        </div>
        <nav className="topnav">
          <span className={`stream-status stream-status-${streamStatus}`} role="status" aria-live="polite">{streamStatus === 'connected' ? 'Live stream connected' : streamStatus === 'reconnecting' ? 'Reconnecting' : 'Disconnected'}</span>
          <NavLink to="/control">Control Plane</NavLink>
          <NavLink to="/nodes">Trading Nodes</NavLink>
          <a href="/symphony/">Symphony</a>
          <Button onClick={refreshAll}><RefreshCw size={14} /> Refresh</Button>
        </nav>
      </header>

      <main className="layout">
        {error ? <div className="alert alert-error">{error}</div> : null}
        <section className="grid metrics">{metrics.map((metric) => <Metric key={metric.label} {...metric} />)}</section>

        <section className="two-column">
          <Panel title="Alerts" subtitle="Operator-visible blockers, run failures, and host pressure." action={<p className="subtle">{overview ? `Last refresh ${formatRelative(overview.generatedAt)}` : 'Waiting for state...'}</p>}>
            {(overview?.alerts || []).length ? overview.alerts.map((alert, index) => <div className={`alert alert-${alert.level || 'warning'}`} key={`${alert.message}-${index}`}>{alert.message}</div>) : <div className="alert alert-ok">No active control-plane alerts.</div>}
          </Panel>
          <Panel title="Control Settings" subtitle="Global prompt delivery policy and raw-log retention.">
            <SettingsPanel overview={overview} onSave={(settings) => postJson('/control/api/settings', settings)} />
          </Panel>
        </section>

        <Panel title="Human Inbox" subtitle="Issues waiting on a human, credentials, or manual intervention."><Table columns={['Issue', 'State', 'Updated', 'PR']} rows={rows.human} empty="No issues currently need human intervention." /></Panel>
        <Panel title="Runs" subtitle="Recent Codex-backed runs with live operator-control entry points."><Table columns={['Run', 'Status', 'Issue', 'Worker', 'Model', 'Started', 'Actions']} rows={rows.runs} /></Panel>
        <Panel title="Timeline" subtitle="Filterable Temporal-style event history across issues, workers, and runs."><Timeline events={timeline} filters={timelineFilters} setFilters={setTimelineFilters} onApply={loadTimeline} onReset={() => setTimelineFilters(DEFAULT_FILTERS)} selectedRunId={selectedRunId} /></Panel>
        <Panel title="Run Detail" subtitle="Temporal-style mission-control view, queue controls, and live logs."><RunDetail detail={runDetail} overview={overview} logStreamId={selectedLogStreamId} setLogStreamId={setSelectedLogStreamId} logContent={logContents[selectedLogStreamId]} onPrompt={(runId, prompt, deliveryMode) => postJson(`/control/api/runs/${encodeURIComponent(runId)}/prompts`, { prompt, deliveryMode })} onInterrupt={(runId, reason) => postJson(`/control/api/runs/${encodeURIComponent(runId)}/interrupt`, { reason })} onCheckpoint={(runId, note, deliveryMode) => postJson(`/control/api/runs/${encodeURIComponent(runId)}/checkpoint`, { note, deliveryMode })} /></Panel>
        <Panel title="Auth & Providers" subtitle="Configure worker identities, OAuth brokers, model keys, and runtime policy."><ProviderPanels overview={overview} outputs={providerOutputs} onCodexAction={(worker, action) => postJson(`/control/api/providers/codex/workers/${encodeURIComponent(worker)}/action`, { action }).then((result) => setProviderOutputs((current) => ({ ...current, codex: result })))} onOpenRouterSave={(apiKey) => postJson('/control/api/providers/openrouter', { apiKey }).then((result) => setProviderOutputs((current) => ({ ...current, openrouter: result })))} onAntigravityConfig={(config) => postJson('/control/api/providers/antigravity', config).then((result) => setProviderOutputs((current) => ({ ...current, antigravity: result })))} onAntigravityAction={(worker, action, callbackInput) => postJson(`/control/api/providers/antigravity/workers/${encodeURIComponent(worker)}/action`, { action, callbackInput }).then((result) => setProviderOutputs((current) => ({ ...current, antigravity: result })))} onProfileSave={(providerId, worker, profile) => postJson(`/control/api/providers/${providerId}/workers/${encodeURIComponent(worker)}/profile`, profile).then((result) => setProviderOutputs((current) => ({ ...current, [providerId]: result })))} /></Panel>
        <Panel title="Strategy Deployments" subtitle="Validate manifests, queue rollout requests, and inspect node-specific requirements."><StrategyNodeDeploymentsPanel overview={overview} onRequest={(payload) => postJson('/control/api/deployments/requests', payload)} /></Panel>
        <Panel title="Workers" subtitle="One provider auth identity per isolated Linux user."><Table columns={['Worker', 'Status', 'Issue', 'Email', 'Codex Runtime', 'Actions']} rows={rows.workers} /></Panel>
        <Panel title="Issues" subtitle="Project issues with current run, PR status, and issue-level controls."><Table columns={['Issue', 'State', 'Runtime', 'Updated', 'PR']} rows={rows.issues} /></Panel>
        <Panel title="Issue Detail" subtitle="Workpad summary, recent comments, and linked runs.">
          {issueDetail ? <IssueDetail detail={issueDetail} onComment={(identifier, body, stateName) => postJson(`/control/api/issues/${encodeURIComponent(identifier)}/comment`, { body, stateName })} onState={(identifier, stateName) => postJson(`/control/api/issues/${encodeURIComponent(identifier)}/state`, { stateName })} onRun={loadRun} /> : <Empty>Select an issue to inspect comments, workpad state, and linked run attempts.</Empty>}
        </Panel>
      </main>
    </>
  );
}

function IssueDetail({ detail, onComment, onState, onRun }) {
  const issue = detail.issue || detail;
  const [comment, setComment] = useState('');
  const [stateName, setStateName] = useState('Ready to Resume');
  const comments = detail.comments || detail.recentComments || [];
  const runs = detail.runs || [];
  return (
    <div className="issue-detail-grid">
      <Card>
        <h3>{issue.identifier} · {issue.title}</h3>
        <p className="subtle">State {issue.state?.name || issue.state || 'unknown'} · Updated {formatRelative(issue.updatedAt)}</p>
        {issue.url ? <a href={issue.url} target="_blank" rel="noreferrer">Open in Linear <SquareArrowOutUpRight size={12} /></a> : null}
        {issue.description ? <pre className="description-block">{issue.description}</pre> : null}
      </Card>
      <Card>
        <h3>Issue Controls</h3>
        <Textarea rows={5} value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Add a Linear comment from the control plane." />
        <Select value={stateName} onChange={(event) => setStateName(event.target.value)}>
          {['Ready to Resume', 'Needs Human', 'Manual Action', 'Awaiting Credentials', 'Rework', 'In Progress', 'Done'].map((name) => <option key={name} value={name}>{name}</option>)}
        </Select>
        <div className="action-row"><Button onClick={() => onComment(issue.identifier, comment, stateName).then(() => setComment(''))}>Comment + State</Button><Button onClick={() => onState(issue.identifier, stateName)}>Change State</Button></div>
      </Card>
      <Card>
        <h3>Linked Runs</h3>
        {runs.length ? runs.map((run) => <div className="compact-row" key={run.runId}><LinkButton onClick={() => onRun(run.runId)}>{run.runId}</LinkButton><Pill status={pillStatus(run.status)}>{run.status}</Pill></div>) : <Empty>No linked runs recorded.</Empty>}
      </Card>
      <Card>
        <h3>Recent Comments</h3>
        {comments.length ? comments.map((item) => <div className="comment-card" key={item.id || item.createdAt}><div className="muted">{item.user?.name || item.author || 'unknown'} · {formatRelative(item.createdAt)}</div><pre>{item.body || item.text || ''}</pre></div>) : <Empty>No recent comments.</Empty>}
      </Card>
    </div>
  );
}

function TradingNodesPage() {
  const navigate = useNavigate();
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState('');

  const loadNodes = useCallback(async () => {
    setError('');
    const result = await getJson('/control/api/nodes');
    setPayload(result);
    return result;
  }, []);

  useEffect(() => {
    loadNodes().catch((err) => setError(err.message || String(err)));
  }, [loadNodes]);

  const nodes = useMemo(() => normalizeNodesPayload(payload), [payload]);
  const metrics = useMemo(() => {
    const managed = nodes.filter((node) => node.managed).length;
    const discovered = nodes.filter((node) => !node.managed).length;
    const running = nodes.filter((node) => /running|starting/i.test(formatNodeState(node))).length;
    const hosts = new Set(nodes.map((node) => node.hostId || node.hostName).filter(Boolean)).size;
    return [
      { label: 'Managed Nodes', value: managed, detail: 'Registry-backed records', icon: Shield },
      { label: 'Discovered Nodes', value: discovered, detail: 'Observed from host state', icon: GitBranch },
      { label: 'Running Nodes', value: running, detail: 'Live containers and active processes', icon: PlayCircle },
      { label: 'Hosts', value: hosts, detail: 'Current host discovery scope', icon: Activity }
    ];
  }, [nodes]);

  const refresh = useCallback(() => loadNodes().catch((err) => setError(err.message || String(err))), [loadNodes]);

  const postNodeAction = useCallback(async (nodeId, action, body = {}) => {
    await getJson(`/control/api/nodes/${encodeURIComponent(nodeId)}/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    await refresh();
  }, [refresh]);

  const rows = nodes.map((node) => (
    <tr key={node.nodeId}>
      <td>
        <LinkButton onClick={() => navigate(`/nodes/${encodeURIComponent(node.nodeId)}`)}>{node.nodeId}</LinkButton>
        <div className="muted">{node.displayName}</div>
      </td>
      <td>
        <div>{node.hostName}</div>
        <div className="muted">{node.hostKind || 'local'}</div>
      </td>
      <td>
        <div>{node.strategyId}</div>
        <div className="muted">{node.manifestId}</div>
      </td>
      <td>{node.venues.length ? node.venues.join(', ') : 'all venues'}</td>
      <td><Pill status={pillStatus(formatNodeState(node))}>{formatNodeState(node)}</Pill></td>
      <td><Pill status={pillStatus(node.source)}>{node.source}</Pill><div className="muted">{node.managed ? 'durable registry' : 'host discovery'}</div></td>
      <td>{formatRelative(node.updatedAt || node.lastHeartbeatAt)}</td>
      <td>
        <div className="action-row wrap">
          <Button onClick={() => navigate(`/nodes/${encodeURIComponent(node.nodeId)}`)}>Open</Button>
          <Button variant="ghost" onClick={() => postNodeAction(node.nodeId, 'start').catch((err) => setError(err.message || String(err)))}>Start</Button>
          <Button variant="ghost" onClick={() => postNodeAction(node.nodeId, 'stop').catch((err) => setError(err.message || String(err)))}>Stop</Button>
          <Button variant="ghost" onClick={() => postNodeAction(node.nodeId, 'restart').catch((err) => setError(err.message || String(err)))}>Restart</Button>
        </div>
      </td>
    </tr>
  ));

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">Betting Arbitrage Control Plane</p>
          <h1>Trading Nodes</h1>
          <p className="subtitle">Inventory, logs, lifecycle control, and registry-backed state for live trading nodes.</p>
        </div>
        <nav className="topnav">
          <NavLink to="/control">Control Plane</NavLink>
          <NavLink to="/nodes">Trading Nodes</NavLink>
          <a href="/symphony/">Symphony</a>
          <Button onClick={refresh}><RefreshCw size={14} /> Refresh</Button>
        </nav>
      </header>

      <main className="layout">
        {error ? <div className="alert alert-error">{error}</div> : null}
        <section className="grid metrics">{metrics.map((metric) => <Metric key={metric.label} {...metric} />)}</section>

        <Panel title="Inventory" subtitle="Registry-backed node records merged with host-discovered runtime truth.">
          <Table
            columns={['Node', 'Host', 'Strategy', 'Venues', 'State', 'Source', 'Updated', 'Actions']}
            rows={rows}
            empty="No trading nodes discovered yet."
          />
        </Panel>

        <section className="two-column">
          <Panel title="Registry Snapshot" subtitle="Durable managed state for hosts and nodes.">
            <JsonBlock value={payload?.nodeRegistry || payload?.registry || { hosts: payload?.hosts || [] }} empty="Registry data unavailable." />
          </Panel>
          <Panel title="Discovery Snapshot" subtitle="Host-observed node state, reconciled into the operator view.">
            <JsonBlock value={payload?.discovery || payload?.discoveries || payload?.snapshot || payload?.observed || {}} empty="Discovery data unavailable." />
          </Panel>
        </section>
      </main>
    </>
  );
}

function TradingNodeDetailPage() {
  const navigate = useNavigate();
  const { nodeId } = useParams();
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState('');
  const [logText, setLogText] = useState('');
  const [previewResult, setPreviewResult] = useState(null);
  const [overrideText, setOverrideText] = useState('');
  const [followLogs, setFollowLogs] = useState(true);
  const [actionResult, setActionResult] = useState(null);

  const loadDetail = useCallback(async () => {
    if (!nodeId) return null;
    setError('');
    const result = await getJson(`/control/api/nodes/${encodeURIComponent(nodeId)}`);
    const normalized = normalizeNodeDetail(result, nodeId);
    setDetail(normalized);
    const seed = normalized.manifest || normalized.effectiveConfig || normalized.registryEntry?.lastAppliedConfig || normalized.discoveryEntry?.manifest || {};
    setOverrideText((current) => (current.trim() ? current : JSON.stringify(seed || {}, null, 2)));
    return normalized;
  }, [nodeId]);

  const loadLogs = useCallback(async () => {
    if (!nodeId) return null;
    const result = await getJson(`/control/api/nodes/${encodeURIComponent(nodeId)}/logs?mode=recent&limit=${NODE_LOG_LIMIT}`);
    setLogText(normalizeNodeLogText(result));
    return result;
  }, [nodeId]);

  useEffect(() => {
    loadDetail().catch((err) => setError(err.message || String(err)));
    loadLogs().catch((err) => setError(err.message || String(err)));
    setPreviewResult(null);
    setActionResult(null);
  }, [loadDetail, loadLogs, nodeId]);

  useEffect(() => {
    if (!followLogs || !nodeId) return undefined;
    const timer = window.setInterval(() => {
      loadLogs().catch((err) => setError(err.message || String(err)));
    }, 2500);
    return () => window.clearInterval(timer);
  }, [followLogs, loadLogs, nodeId]);

  const node = detail?.node || null;

  async function postAction(action, body = {}) {
    if (!nodeId) return null;
    const result = await getJson(`/control/api/nodes/${encodeURIComponent(nodeId)}/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    setActionResult(result);
    await loadDetail();
    await loadLogs();
    return result;
  }

  async function previewConfig() {
    const override = parseJsonOrNull(overrideText);
    const payload = override ? { override } : {};
    const result = await getJson(`/control/api/nodes/${encodeURIComponent(nodeId)}/render-config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    setPreviewResult(result);
  }

  async function startNode() {
    const override = parseJsonOrNull(overrideText);
    return postAction('start', override ? { override } : {});
  }

  async function restartNode() {
    const override = parseJsonOrNull(overrideText);
    return postAction('restart', override ? { override } : {});
  }

  async function stopNode() {
    return postAction('stop');
  }

  const summaryMetrics = useMemo(() => {
    if (!node) return [];
    return [
      { label: 'State', value: formatNodeState(node), detail: node.intendedState || 'desired state', icon: PlayCircle },
      { label: 'Host', value: node.hostName || 'local', detail: node.hostKind || 'host', icon: GitBranch },
      { label: 'Strategy', value: node.strategyId || 'unknown', detail: node.manifestId || 'manifest unknown', icon: Workflow },
      { label: 'Heartbeat', value: node.lastHeartbeatAt ? formatRelative(node.lastHeartbeatAt) : 'unknown', detail: node.source || 'discovery source', icon: Clock3 }
    ];
  }, [node]);

  if (!nodeId) {
    return <Navigate to="/nodes" replace />;
  }

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">Betting Arbitrage Control Plane</p>
          <h1>Trading Node Detail</h1>
          <p className="subtitle">Lifecycle, config preview, logs, and registry/discovery reconciliation for a live node.</p>
        </div>
        <nav className="topnav">
          <NavLink to="/control">Control Plane</NavLink>
          <NavLink to="/nodes">Trading Nodes</NavLink>
          <a href="/symphony/">Symphony</a>
          <Button onClick={() => { loadDetail().catch((err) => setError(err.message || String(err))); loadLogs().catch((err) => setError(err.message || String(err))); }}><RefreshCw size={14} /> Refresh</Button>
        </nav>
      </header>

      <main className="layout">
        <div className="action-row wrap">
          <Button variant="ghost" onClick={() => navigate('/nodes')}>Back to Inventory</Button>
          <Pill status={pillStatus(node?.source)}>{node?.source || 'unknown source'}</Pill>
          <Pill status={pillStatus(formatNodeState(node))}>{formatNodeState(node)}</Pill>
          {node?.managed ? <Pill status="running">registry managed</Pill> : <Pill status="interrupted">host discovered</Pill>}
        </div>

        {error ? <div className="alert alert-error">{error}</div> : null}
        <section className="grid metrics">{summaryMetrics.map((metric) => <Metric key={metric.label} {...metric} />)}</section>

        <section className="two-column">
          <Card>
            <h3>{node?.displayName || nodeId}</h3>
            <div className="node-details">
              <div><span className="muted">Node ID</span><strong>{node?.nodeId || nodeId}</strong></div>
              <div><span className="muted">Host</span><strong>{node?.hostName || 'local'}</strong></div>
              <div><span className="muted">Host kind</span><strong>{node?.hostKind || 'local'}</strong></div>
              <div><span className="muted">Container</span><strong>{node?.containerName || 'unknown'}</strong></div>
              <div><span className="muted">Strategy</span><strong>{node?.strategyId || 'unknown'}</strong></div>
              <div><span className="muted">Manifest</span><strong>{node?.manifestId || 'unknown'}</strong></div>
              <div><span className="muted">Image</span><strong>{node?.image || 'unknown'}</strong></div>
              <div><span className="muted">Venues</span><strong>{node?.venues?.length ? node.venues.join(', ') : 'all venues'}</strong></div>
              <div><span className="muted">Runtime config</span><strong>{node?.runtimeConfigPath || 'unknown'}</strong></div>
              <div><span className="muted">Heartbeat</span><strong>{node?.lastHeartbeatAt ? formatDateTime(node.lastHeartbeatAt) : 'unknown'}</strong></div>
            </div>
            <div className="action-row wrap">
              <Button onClick={() => startNode().catch((err) => setError(err.message || String(err)))}>Start</Button>
              <Button variant="ghost" onClick={() => stopNode().catch((err) => setError(err.message || String(err)))}>Stop</Button>
              <Button variant="ghost" onClick={() => restartNode().catch((err) => setError(err.message || String(err)))}>Restart</Button>
              <Button variant="ghost" onClick={() => previewConfig().catch((err) => setError(err.message || String(err)))}>Preview Config</Button>
            </div>
          </Card>

          <Card>
            <h3>Config Override</h3>
            <p className="subtle">Bounded JSON override for restart, start, and preview. Secrets stay out of this surface.</p>
            <Textarea rows={12} value={overrideText} onChange={(event) => setOverrideText(event.target.value)} placeholder='{"validationMode": true, "executionEnabled": false}' />
            <div className="action-row wrap">
              <Button onClick={() => startNode().catch((err) => setError(err.message || String(err)))}>Start with Override</Button>
              <Button variant="ghost" onClick={() => restartNode().catch((err) => setError(err.message || String(err)))}>Restart with Override</Button>
              <Button variant="ghost" onClick={() => previewConfig().catch((err) => setError(err.message || String(err)))}>Preview Effective Config</Button>
            </div>
            <div className="muted">Follow logs: {followLogs ? 'enabled' : 'disabled'}</div>
            <div className="action-row wrap">
              <Button variant="ghost" onClick={() => setFollowLogs((current) => !current)}>{followLogs ? 'Pause Auto Refresh' : 'Resume Auto Refresh'}</Button>
              {actionResult ? <Button variant="ghost" onClick={() => setActionResult(null)}>Clear Action Result</Button> : null}
            </div>
          </Card>
        </section>

        <section className="two-column">
          <Card>
            <h3>Recent Logs</h3>
            <p className="subtle">The latest host-observed logs for the currently selected node. Auto refresh follows the active process.</p>
            <pre className="log-output">{logText || 'No logs loaded yet.'}</pre>
          </Card>
          <Card>
            <h3>Registry / Discovery</h3>
            <div className="nested-columns">
              <div>
                <h4>Registry</h4>
                <JsonBlock value={detail?.registryEntry || detail?.registry || node?.registry || {}} empty="No durable registry snapshot available." />
              </div>
              <div>
                <h4>Discovery</h4>
                <JsonBlock value={detail?.discoveryEntry || detail?.discovery || node?.discovery || {}} empty="No host discovery snapshot available." />
              </div>
            </div>
          </Card>
        </section>

        <section className="two-column">
          <Card>
            <h3>Effective Config Preview</h3>
            <JsonBlock value={previewResult?.renderedConfig || previewResult?.effectiveConfig || previewResult?.manifest || previewResult || {}} empty="Run a config preview to inspect the effective configuration." />
          </Card>
          <Card>
            <h3>Last Action Result</h3>
            <JsonBlock value={actionResult || {}} empty="Run a lifecycle action to inspect the server response." />
          </Card>
        </section>
      </main>
    </>
  );
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/control" replace />} />
        <Route path="/control" element={<MissionControlPage />} />
        <Route path="/nodes" element={<TradingNodesPage />} />
        <Route path="/nodes/:nodeId" element={<TradingNodeDetailPage />} />
        <Route path="*" element={<Navigate to="/control" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

createRoot(document.getElementById('root')).render(<App />);
