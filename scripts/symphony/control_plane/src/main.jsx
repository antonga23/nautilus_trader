import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, AlertTriangle, Bot, Braces, CheckCircle2, Clock3, GitBranch, KeyRound, PauseCircle, PlayCircle, RefreshCw, Send, Shield, SquareArrowOutUpRight, TerminalSquare, Workflow } from 'lucide-react';
import { Button, Card, Empty, Input, Panel, Pill, Select, Table, Textarea } from './components/ui.jsx';
import { coerceList, formatDateTime, formatDuration, formatNumber, formatRelative, getJson, pillStatus } from './lib/api.js';
import './styles.css';

const DEFAULT_FILTERS = { issue: '', worker: '', eventType: '', level: '', search: '', limit: 300 };
const DELIVERY_MODES = ['deliver_after_current_step', 'interrupt_now', 'deliver_when_idle'];
const RUNNING_STATUSES = new Set(['starting', 'running', 'interrupted']);

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
          <p className="subtle">Start remote device auth on EC2, then persist and restore worker auth via Secrets Manager.</p>
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

function App() {
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
          <a href="/" aria-current="page">Control Plane</a>
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

createRoot(document.getElementById('root')).render(<App />);
