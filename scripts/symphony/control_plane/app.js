const state = {
  overview: null,
  timeline: [],
  selectedIssue: null,
  selectedDetail: null,
  selectedRunId: null,
  selectedRunDetail: null,
  selectedLogStreamId: null,
  logOffsets: {},
  logContents: {},
  refreshTimer: null,
  logTimer: null,
  stream: null,
  streamStatus: 'disconnected',
  streamRefreshTimer: null,
  providerOutputs: {},
  timelineFilters: {
    issue: '',
    worker: '',
    eventType: '',
    level: '',
    search: '',
    limit: 300
  }
};

const metricContainer = document.getElementById('metrics');
const alertsContainer = document.getElementById('alerts');
const generatedAt = document.getElementById('generated-at');
const settingsPanel = document.getElementById('settings-panel');
const humanInboxRows = document.getElementById('human-inbox-rows');
const providerPanels = document.getElementById('provider-panels');
const workerRows = document.getElementById('worker-rows');
const issueRows = document.getElementById('issue-rows');
const issueDetail = document.getElementById('issue-detail');
const runRows = document.getElementById('run-rows');
const runDetail = document.getElementById('run-detail');
const timelineFilters = document.getElementById('timeline-filters');
const timelineEvents = document.getElementById('timeline-events');
const refreshButton = document.getElementById('refresh-button');
const streamStatus = document.getElementById('stream-status');

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function formatRelative(dateText) {
  if (!dateText) {
    return 'unknown';
  }
  const deltaMs = Date.now() - new Date(dateText).getTime();
  const deltaMinutes = Math.floor(deltaMs / 60_000);
  if (deltaMinutes < 1) {
    return 'just now';
  }
  if (deltaMinutes < 60) {
    return `${deltaMinutes}m ago`;
  }
  const deltaHours = Math.floor(deltaMinutes / 60);
  if (deltaHours < 24) {
    return `${deltaHours}h ago`;
  }
  const deltaDays = Math.floor(deltaHours / 24);
  return `${deltaDays}d ago`;
}

function formatDateTime(dateText) {
  if (!dateText) {
    return 'unknown';
  }
  const date = new Date(dateText);
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short'
  }).format(date);
}

function formatDuration(startedAt, endedAt) {
  if (!startedAt) {
    return 'n/a';
  }
  const start = new Date(startedAt).getTime();
  const end = endedAt ? new Date(endedAt).getTime() : Date.now();
  const delta = Math.max(0, end - start);
  const seconds = Math.floor(delta / 1000);
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.floor(seconds / 60);
  const remSeconds = seconds % 60;
  if (minutes < 60) {
    return `${minutes}m ${remSeconds}s`;
  }
  const hours = Math.floor(minutes / 60);
  const remMinutes = minutes % 60;
  return `${hours}h ${remMinutes}m`;
}

function formatNumber(value) {
  if (value === null || value === undefined || value === '') {
    return 'n/a';
  }
  return new Intl.NumberFormat().format(Number(value));
}

function pillClass(status) {
  return `pill pill-${String(status || 'unknown').replace(/[^a-z0-9_]+/gi, '_')}`;
}

function renderStreamStatus() {
  if (!streamStatus) {
    return;
  }
  const labels = {
    connected: 'Live stream connected',
    reconnecting: 'Reconnecting',
    disconnected: 'Disconnected'
  };
  const status = state.streamStatus || 'disconnected';
  streamStatus.className = `stream-status stream-status-${status}`;
  streamStatus.textContent = labels[status] || labels.disconnected;
}

function setStreamStatus(status) {
  if (state.streamStatus === status) {
    return;
  }
  state.streamStatus = status;
  renderStreamStatus();
}

async function getJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function saveProviderOutput(key, value) {
  state.providerOutputs[key] = value;
}

function providerOutputHtml(key) {
  const value = state.providerOutputs[key];
  if (!value) {
    return '<pre class="provider-output">No recent action.</pre>';
  }
  return `<pre class="provider-output">${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
}

function renderMetrics(overview) {
  const availableWorkers = overview.workers.filter((worker) => worker.availableForNewWork).length;
  const runningCount = overview.runs.filter((run) => !run.endedAt && ['starting', 'running', 'interrupted'].includes(run.status)).length;
  const humanCount = overview.humanInbox.length;
  const stalledCount = overview.stalledIssues.length;
  const diskUsed = overview.host?.disk?.usedPercent || 'n/a';
  const load = Array.isArray(overview.host?.load) ? overview.host.load.slice(0, 3).map((value) => value.toFixed(2)).join(' / ') : 'n/a';
  const metrics = [
    ['Running Runs', runningCount, `Recent runs tracked: ${overview.runs.length}`],
    ['Available Workers', availableWorkers, `Configured: ${overview.workers.length}`],
    ['Human Inbox', humanCount, 'Needs Human / Manual Action / Awaiting Credentials'],
    ['Stalled Issues', stalledCount, 'In Progress or Rework without a running session'],
    ['Disk Use', diskUsed, `Host load: ${load}`]
  ];
  metricContainer.innerHTML = metrics
    .map(
      ([label, value, detail]) => `
        <article class="metric-card">
          <p class="metric-label">${escapeHtml(label)}</p>
          <p class="metric-value">${escapeHtml(value)}</p>
          <p class="metric-detail">${escapeHtml(detail)}</p>
        </article>
      `
    )
    .join('');
}

function renderAlerts(overview) {
  const alerts = [...overview.alerts];
  const diskUsed = Number(String(overview.host?.disk?.usedPercent || '').replace('%', ''));
  if (!Number.isNaN(diskUsed) && diskUsed >= 85) {
    alerts.push({ level: 'warning', message: `Host disk usage is high at ${overview.host.disk.usedPercent}.` });
  }
  if (alerts.length === 0) {
    alertsContainer.innerHTML = '<div class="alert alert-warning">No active operator alerts.</div>';
    return;
  }
  alertsContainer.innerHTML = alerts
    .map(
      (alert) => `
        <div class="alert ${alert.level === 'error' ? 'alert-error' : 'alert-warning'}">
          ${escapeHtml(alert.message)}
        </div>
      `
    )
    .join('');
}

function renderSettings(overview) {
  const settings = overview.settings || {};
  settingsPanel.innerHTML = `
    <div class="filter-grid compact-grid">
      <label>
        <span class="input-label">Default Prompt Delivery</span>
        <select id="settings-default-prompt-delivery">
          ${['deliver_after_current_step', 'interrupt_now', 'deliver_when_idle']
            .map((mode) => `<option value="${escapeHtml(mode)}" ${settings.defaultPromptDeliveryMode === mode ? 'selected' : ''}>${escapeHtml(mode)}</option>`)
            .join('')}
        </select>
      </label>
      <label>
        <span class="input-label">Raw Log Retention (days)</span>
        <input id="settings-raw-log-retention" type="number" min="1" max="365" value="${escapeHtml(settings.rawLogRetentionDays || 30)}" />
      </label>
    </div>
    <div class="action-row">
      <button type="button" data-save-settings="true">Save Settings</button>
    </div>
    <p class="subtle">Updated: ${escapeHtml(settings.updatedAt ? formatDateTime(settings.updatedAt) : 'never')}</p>
  `;
}

function renderHumanInbox(overview) {
  if (overview.humanInbox.length === 0) {
    humanInboxRows.innerHTML = '<tr><td colspan="4" class="muted">No issues are waiting on a human.</td></tr>';
    return;
  }
  humanInboxRows.innerHTML = overview.humanInbox
    .map((issue) => {
      const prCell = issue.pr ? `<a href="${escapeHtml(issue.pr.url)}" target="_blank" rel="noreferrer">PR #${issue.pr.number}</a>` : '<span class="muted">none</span>';
      return `
        <tr data-issue="${escapeHtml(issue.identifier)}">
          <td><button class="link-button" data-open-issue="${escapeHtml(issue.identifier)}">${escapeHtml(issue.identifier)}</button><div class="muted">${escapeHtml(issue.title)}</div></td>
          <td><span class="${pillClass(issue.state?.name)}">${escapeHtml(issue.state?.name || 'unknown')}</span></td>
          <td>${escapeHtml(formatRelative(issue.updatedAt))}</td>
          <td>${prCell}</td>
        </tr>
      `;
    })
    .join('');
}

function workerActionButtons(worker) {
  const buttons = [];
  if (worker.cordoned) {
    buttons.push(`<button type="button" data-worker-action="${escapeHtml(worker.name)}:resume">Resume</button>`);
  } else {
    buttons.push(`<button type="button" data-worker-action="${escapeHtml(worker.name)}:cordon">Pause New Work</button>`);
  }
  if (worker.status === 'cooldown' || worker.status === 'rate_limited') {
    buttons.push(`<button type="button" data-worker-action="${escapeHtml(worker.name)}:clearCooldown">Clear Cooldown</button>`);
  }
  if (worker.latestRun?.runId) {
    buttons.push(`<button type="button" data-open-run="${escapeHtml(worker.latestRun.runId)}">Open Latest Run</button>`);
  }
  return buttons.join('');
}

function renderRuntimeProfileEditor(providerId, workerName, profile = {}) {
  return `
    <div class="muted">Subscription tier: ${escapeHtml(profile.subscriptionTier || 'unset')}</div>
    <div class="muted">Selection mode: ${escapeHtml(profile.selectionMode || 'provider-default')}</div>
    <div class="muted">Effective runtime model: ${escapeHtml(profile.effectiveRuntimeModel || 'provider default')}</div>
    <div class="muted">Available models: ${escapeHtml((profile.availableModels || []).join(', ') || 'not recorded')}</div>
    <div class="muted">Profile source: ${escapeHtml(profile.source || 'unset')}</div>
    <input id="${escapeHtml(providerId)}-tier-${escapeHtml(workerName)}" type="text" placeholder="free / plus / pro / max / team" value="${escapeHtml(profile.subscriptionTier || '')}" />
    <input id="${escapeHtml(providerId)}-runtime-model-${escapeHtml(workerName)}" type="text" placeholder="Exact runtime model id to pin, or leave blank for provider default" value="${escapeHtml(profile.runtimeModel || '')}" />
    <textarea id="${escapeHtml(providerId)}-available-models-${escapeHtml(workerName)}" rows="3" placeholder="Known available models, one per line or comma-separated">${escapeHtml((profile.availableModels || []).join('\n'))}</textarea>
    <textarea id="${escapeHtml(providerId)}-notes-${escapeHtml(workerName)}" rows="3" placeholder="Operational notes">${escapeHtml(profile.notes || '')}</textarea>
  `;
}

function codexProviderHtml(overview) {
  const codex = overview.providers?.codex;
  if (!codex) {
    return '';
  }
  return `
    <article class="card provider-card">
      <h3>Codex / ChatGPT</h3>
      <p class="subtle">Start device auth on EC2, then persist and restore worker auth via Secrets Manager.</p>
      <p class="muted">${escapeHtml(codex.note || '')}</p>
      ${codex.workers.map((worker) => `
        <div class="card">
          <strong>${escapeHtml(worker.displayName)}</strong>
          <div class="muted">Expected account: ${escapeHtml(worker.email)}</div>
          <div class="muted">Remote auth: ${escapeHtml(worker.authPresent ? (worker.authEmail || 'present') : 'missing')}</div>
          <div class="muted">Secret stored: ${worker.secretStored ? 'yes' : 'no'}</div>
          ${renderRuntimeProfileEditor('codex', worker.worker, worker.profile || {})}
          ${worker.session ? `<pre class="provider-output">${escapeHtml(JSON.stringify(worker.session, null, 2))}</pre>` : ''}
          <div class="action-row">
            <button type="button" data-provider-action="codex-profile:${escapeHtml(worker.worker)}:save">Save Runtime Profile</button>
            <button type="button" data-provider-action="codex:${escapeHtml(worker.worker)}:startAuth">Generate Auth Link</button>
            <button type="button" data-provider-action="codex:${escapeHtml(worker.worker)}:persist">Persist to Secrets</button>
            <button type="button" data-provider-action="codex:${escapeHtml(worker.worker)}:restore">Restore from Secrets</button>
          </div>
        </div>
      `).join('')}
      ${providerOutputHtml('codex')}
    </article>
  `;
}

function openRouterProviderHtml(overview) {
  const openrouter = overview.providers?.openrouter;
  if (!openrouter) {
    return '';
  }
  return `
    <article class="card provider-card">
      <h3>OpenRouter</h3>
      <p class="subtle">Store the shared API key in AWS Secrets Manager for future worker runtimes.</p>
      <div class="muted">Secret configured: ${openrouter.secretConfigured ? 'yes' : 'no'}</div>
      <div class="muted">Secrets key: ${escapeHtml(openrouter.secretKey)}</div>
      <input id="openrouter-api-key" type="password" placeholder="sk-or-v1-..." />
      <div class="action-row">
        <button type="button" data-provider-action="openrouter:setKey">Save API Key</button>
      </div>
      ${providerOutputHtml('openrouter')}
    </article>
  `;
}

function antigravityProviderHtml(overview) {
  const provider = overview.providers?.antigravity;
  if (!provider) {
    return '';
  }
  const config = provider.config || {};
  const warnings = Array.isArray(config.warnings) ? config.warnings : [];
  return `
    <article class="card provider-card">
      <h3>Antigravity / Google</h3>
      <p class="subtle">${escapeHtml(provider.guidance || '')}</p>
      <div class="card">
        <strong>OAuth Broker</strong>
        <div class="muted">Client ID configured: ${config.clientIdConfigured ? 'yes' : 'no'}</div>
        <div class="muted">Client secret configured: ${config.clientSecretConfigured ? 'yes' : 'no'}</div>
        <div class="muted">Public base URL: ${escapeHtml(config.publicBaseUrl || 'missing')}</div>
        <div class="muted">Callback URL: ${escapeHtml(config.callbackUrl || 'missing')}</div>
        <div class="muted">Callback ready: ${config.callbackReady ? 'yes' : 'no'}</div>
        <div class="muted">Scopes: ${escapeHtml((config.scopes || []).join(' '))}</div>
        ${warnings.length ? warnings.map((warning) => `<div class="alert alert-warning">${escapeHtml(warning)}</div>`).join('') : '<div class="alert alert-warning">No current Antigravity OAuth warnings.</div>'}
        <input id="antigravity-public-base-url" type="text" placeholder="https://agents.example.com" value="${escapeHtml(config.publicBaseUrl || '')}" />
        <input id="antigravity-client-id" type="text" placeholder="Google OAuth Client ID" value="${escapeHtml(config.clientId || '')}" />
        <input id="antigravity-client-secret" type="password" placeholder="Paste a new Google OAuth client secret only when rotating it" />
        <textarea id="antigravity-scopes" rows="4" placeholder="Google OAuth scopes, separated by spaces or commas">${escapeHtml((config.scopes || []).join('\n'))}</textarea>
        <div class="action-row">
          <button type="button" data-provider-action="antigravity:saveConfig">Save OAuth Config</button>
        </div>
      </div>
      ${provider.workers.map((worker) => `
        <div class="card">
          <strong>${escapeHtml(worker.displayName)}</strong>
          <div class="muted">Expected Google account: ${escapeHtml(worker.email)}</div>
          <div class="muted">Remote auth: ${worker.authPresent ? escapeHtml(worker.authRecord?.email || 'present') : 'missing'}</div>
          <div class="muted">Project: ${escapeHtml(worker.authRecord?.projectId || worker.authRecord?.managedProjectId || 'pending discovery')}</div>
          <div class="muted">Secret stored: ${worker.secretStored ? 'yes' : 'no'}</div>
          <div class="muted">Auth path: ${escapeHtml(worker.authPath)}</div>
          ${renderRuntimeProfileEditor('antigravity', worker.worker, worker.profile || {})}
          ${worker.session ? `<pre class="provider-output">${escapeHtml(JSON.stringify(worker.session, null, 2))}</pre>` : `<pre class="provider-output">${escapeHtml(JSON.stringify(worker.authRecord || { status: 'not_configured' }, null, 2))}</pre>`}
          ${worker.session?.authUrl ? `<p><a href="${escapeHtml(worker.session.authUrl)}" target="_blank" rel="noreferrer">Open Latest Auth Link</a></p>` : ''}
          <textarea id="antigravity-callback-${escapeHtml(worker.worker)}" rows="3" placeholder="Paste the full callback URL or just the authorization code if you need to complete the OAuth session manually."></textarea>
          <div class="action-row">
            <button type="button" data-provider-action="antigravity-profile:${escapeHtml(worker.worker)}:save">Save Runtime Profile</button>
            <button type="button" data-provider-action="antigravity-worker:${escapeHtml(worker.worker)}:startAuth">Generate Auth Link</button>
            <button type="button" data-provider-action="antigravity-worker:${escapeHtml(worker.worker)}:completeAuth">Complete Manually</button>
            <button type="button" data-provider-action="antigravity-worker:${escapeHtml(worker.worker)}:persist">Persist Remote Auth</button>
            <button type="button" data-provider-action="antigravity-worker:${escapeHtml(worker.worker)}:restore">Restore Remote Auth</button>
          </div>
        </div>
      `).join('')}
      ${providerOutputHtml('antigravity')}
    </article>
  `;
}

function renderProviders(overview) {
  providerPanels.innerHTML = [
    codexProviderHtml(overview),
    openRouterProviderHtml(overview),
    antigravityProviderHtml(overview)
  ].join('');
}

function renderWorkers(overview) {
  workerRows.innerHTML = overview.workers
    .map((worker) => `
      <tr>
        <td>
          <strong>${escapeHtml(worker.displayName || worker.name)}</strong>
          <div class="muted">${escapeHtml(worker.user)}</div>
        </td>
        <td>
          <span class="${pillClass(worker.status)}">${escapeHtml(worker.status)}</span>
          <div class="muted">${worker.availableForNewWork ? 'ready for new work' : 'not available'}</div>
        </td>
        <td>
          ${escapeHtml(worker.issueIdentifier || 'none')}
          ${worker.latestRun?.runId ? `<div class="muted">Latest run: <button class="link-button" data-open-run="${escapeHtml(worker.latestRun.runId)}">${escapeHtml(worker.latestRun.runId)}</button></div>` : ''}
        </td>
        <td>${escapeHtml(worker.email || 'unknown')}</td>
        <td>
          <div class="muted">Tier: ${escapeHtml(worker.codexSubscriptionTier || 'unset')}</div>
          <div class="muted">Model: ${escapeHtml(worker.effectiveCodexModel || 'provider default')}</div>
          <div class="muted">Policy: ${escapeHtml(worker.codexSelectionMode || 'provider-default')}</div>
        </td>
        <td class="actions">
          <div class="action-row">${workerActionButtons(worker)}</div>
        </td>
      </tr>
    `)
    .join('');
}

function renderRuns(overview) {
  if (!overview.runs.length) {
    runRows.innerHTML = '<tr><td colspan="7" class="muted">No durable run history has been recorded yet.</td></tr>';
    return;
  }
  runRows.innerHTML = overview.runs
    .map((run) => {
      const selected = state.selectedRunId === run.runId ? ' style="background:#f6efe0;"' : '';
      return `
        <tr${selected}>
          <td>
            <button class="link-button" data-open-run="${escapeHtml(run.runId)}">${escapeHtml(run.runId)}</button>
            <div class="muted">${escapeHtml(formatDuration(run.startedAt, run.endedAt))}</div>
          </td>
          <td>
            <span class="${pillClass(run.status)}">${escapeHtml(run.status || 'unknown')}</span>
            <div class="muted">Updated ${escapeHtml(formatRelative(run.lastUpdatedAt))}</div>
          </td>
          <td>
            ${escapeHtml(run.issueIdentifier || 'unknown')}
            ${run.issueIdentifier ? `<div class="muted"><button class="link-button" data-open-issue="${escapeHtml(run.issueIdentifier)}">Open issue</button></div>` : ''}
          </td>
          <td>${escapeHtml(run.workerName || 'unknown')}</td>
          <td>
            <div class="muted">${escapeHtml(run.effectiveModel || 'provider default')}</div>
            <div class="muted">${escapeHtml(run.modelProvider || run.provider || 'codex')}</div>
          </td>
          <td>${escapeHtml(formatDateTime(run.startedAt))}</td>
          <td>
            <div class="action-row compact-row">
              <button type="button" data-open-run="${escapeHtml(run.runId)}">Inspect</button>
            </div>
          </td>
        </tr>
      `;
    })
    .join('');
}

function renderIssues(overview) {
  issueRows.innerHTML = overview.issues
    .map((issue) => {
      const runtime = issue.running
        ? `running on ${issue.running.agent || 'unknown'}`
        : issue.retrying
          ? 'retrying'
          : issue.latestRun
            ? `last run ${issue.latestRun.status}`
            : 'idle';
      const prCell = issue.pr ? `<a href="${escapeHtml(issue.pr.url)}" target="_blank" rel="noreferrer">PR #${issue.pr.number}</a>` : '<span class="muted">none</span>';
      const selected = state.selectedIssue === issue.identifier ? ' style="background:#f6efe0;"' : '';
      return `
        <tr${selected}>
          <td>
            <button class="link-button" data-open-issue="${escapeHtml(issue.identifier)}">${escapeHtml(issue.identifier)}</button>
            <div class="muted">${escapeHtml(issue.title)}</div>
            ${issue.latestRun?.runId ? `<div class="muted">Latest run: <button class="link-button" data-open-run="${escapeHtml(issue.latestRun.runId)}">${escapeHtml(issue.latestRun.runId)}</button></div>` : ''}
          </td>
          <td><span class="${pillClass(issue.state?.name)}">${escapeHtml(issue.state?.name || 'unknown')}</span></td>
          <td>${escapeHtml(runtime)}</td>
          <td>${escapeHtml(formatRelative(issue.updatedAt))}</td>
          <td>${prCell}</td>
        </tr>
      `;
    })
    .join('');
}

function renderTimelineFilters() {
  timelineFilters.innerHTML = `
    <div class="filter-grid">
      <label>
        <span class="input-label">Issue</span>
        <input id="timeline-issue" type="text" placeholder="BET-11" value="${escapeHtml(state.timelineFilters.issue || '')}" />
      </label>
      <label>
        <span class="input-label">Worker</span>
        <input id="timeline-worker" type="text" placeholder="codex-c" value="${escapeHtml(state.timelineFilters.worker || '')}" />
      </label>
      <label>
        <span class="input-label">Event Type</span>
        <input id="timeline-event-type" type="text" placeholder="command.completed" value="${escapeHtml(state.timelineFilters.eventType || '')}" />
      </label>
      <label>
        <span class="input-label">Level</span>
        <select id="timeline-level">
          <option value="">Any</option>
          ${['debug', 'info', 'warning', 'error'].map((level) => `<option value="${level}" ${state.timelineFilters.level === level ? 'selected' : ''}>${level}</option>`).join('')}
        </select>
      </label>
      <label class="wide-field">
        <span class="input-label">Search</span>
        <input id="timeline-search" type="text" placeholder="command / error / qodo / checkpoint" value="${escapeHtml(state.timelineFilters.search || '')}" />
      </label>
      <label>
        <span class="input-label">Limit</span>
        <input id="timeline-limit" type="number" min="50" max="2000" value="${escapeHtml(state.timelineFilters.limit || 300)}" />
      </label>
    </div>
    <div class="action-row">
      <button type="button" data-apply-timeline-filters="true">Apply Filters</button>
      <button type="button" data-clear-timeline-filters="true">Clear Filters</button>
      ${state.selectedRunId ? `<button type="button" data-clear-selected-run="true">Return To Global Timeline</button>` : ''}
    </div>
  `;
}

function renderTimeline() {
  renderTimelineFilters();
  if (!state.timeline.length) {
    timelineEvents.innerHTML = '<div class="timeline-empty">No timeline events match the current filters.</div>';
    return;
  }
  timelineEvents.innerHTML = state.timeline
    .slice()
    .reverse()
    .map((event) => `
      <article class="timeline-event ${event.level === 'error' ? 'timeline-error' : event.level === 'warning' ? 'timeline-warning' : ''}">
        <div class="timeline-event-header">
          <div>
            <span class="${pillClass(event.level)}">${escapeHtml(event.level || 'info')}</span>
            <strong>${escapeHtml(event.eventType)}</strong>
          </div>
          <span class="muted">${escapeHtml(formatDateTime(event.timestamp))}</span>
        </div>
        <p>${escapeHtml(event.summary || '')}</p>
        <div class="timeline-tags">
          ${event.runId ? `<button class="tag-button" data-open-run="${escapeHtml(event.runId)}">Run ${escapeHtml(event.runId)}</button>` : ''}
          ${event.issueIdentifier ? `<button class="tag-button" data-open-issue="${escapeHtml(event.issueIdentifier)}">${escapeHtml(event.issueIdentifier)}</button>` : ''}
          ${event.workerName ? `<span class="tag">${escapeHtml(event.workerName)}</span>` : ''}
          ${event.source ? `<span class="tag">${escapeHtml(event.source)}</span>` : ''}
        </div>
        <details>
          <summary>Payload</summary>
          <pre>${escapeHtml(JSON.stringify(event.payload || {}, null, 2))}</pre>
        </details>
      </article>
    `)
    .join('');
}

function renderIssueDetail(detail) {
  const issue = detail.issue;
  const workpad = detail.workpad || {};
  const github = detail.github;
  const comments = Array.isArray(detail.comments) ? detail.comments : [];
  const runs = Array.isArray(detail.runs) ? detail.runs : [];
  issueDetail.classList.remove('empty');
  issueDetail.innerHTML = `
    <div class="card">
      <h3>${escapeHtml(issue.identifier)} · ${escapeHtml(issue.title)}</h3>
      <p class="comment-meta">
        State: <span class="${pillClass(issue.state?.name)}">${escapeHtml(issue.state?.name || 'unknown')}</span>
        · Updated ${escapeHtml(formatRelative(issue.updatedAt))}
        · <a href="${escapeHtml(issue.url)}" target="_blank" rel="noreferrer">Linear</a>
      </p>
      <div class="action-row">
        <button type="button" data-issue-state="${escapeHtml(issue.identifier)}:Ready to Resume">Move to Ready to Resume</button>
        <button type="button" data-issue-state="${escapeHtml(issue.identifier)}:Needs Human">Move to Needs Human</button>
        <button type="button" data-issue-state="${escapeHtml(issue.identifier)}:Manual Action">Move to Manual Action</button>
        <button type="button" data-issue-state="${escapeHtml(issue.identifier)}:Awaiting Credentials">Move to Awaiting Credentials</button>
      </div>
    </div>

    <div class="card">
      <h3>Linked Runs</h3>
      ${runs.length ? `
        <div class="action-column">
          ${runs.map((run) => `<button type="button" class="subtle-button" data-open-run="${escapeHtml(run.runId)}">${escapeHtml(run.runId)} · ${escapeHtml(run.status)} · ${escapeHtml(formatRelative(run.startedAt))}</button>`).join('')}
        </div>
      ` : '<p class="muted">No durable run history has been recorded for this issue yet.</p>'}
    </div>

    <div class="card">
      <h3>Workpad Summary</h3>
      <pre>${escapeHtml([
        ['Environment', workpad.environment],
        ['Current Status', workpad.currentStatus],
        ['Validation', workpad.validation],
        ['Blockers', workpad.blockers],
        ['Qodo Triage', workpad.qodo]
      ].map(([label, value]) => `${label}\n${value || 'n/a'}`).join('\n\n'))}</pre>
    </div>

    <div class="card">
      <h3>GitHub</h3>
      <pre>${escapeHtml(github ? JSON.stringify(github, null, 2) : 'No attached PR summary available.')}</pre>
    </div>

    <div class="card">
      <h3>Issue Comment</h3>
      <textarea id="issue-comment-body" rows="9" placeholder="Add operator context, resume instructions, or a manual action note."></textarea>
      <div class="action-row">
        <button type="button" data-save-comment="${escapeHtml(issue.identifier)}">Post Comment</button>
        <button type="button" data-save-comment-state="${escapeHtml(issue.identifier)}:Ready to Resume">Post Comment and Move to Ready to Resume</button>
      </div>
    </div>

    <div class="card">
      <h3>Recent Comments</h3>
      <div class="comment-list">
        ${comments.map((comment) => `
          <article class="card">
            <p class="comment-meta">${escapeHtml(comment.user?.name || 'Unknown')} · ${escapeHtml(formatRelative(comment.updatedAt))}</p>
            <pre>${escapeHtml(comment.body || '')}</pre>
          </article>
        `).join('')}
      </div>
    </div>
  `;
}

function renderRunSegments(detail) {
  const run = detail.run;
  const segments = Array.isArray(detail.segments) ? detail.segments : [];
  if (!segments.length) {
    return '<div class="timeline-empty">No step-level segments have been recorded for this run yet.</div>';
  }
  const started = new Date(run.startedAt || Date.now()).getTime();
  const ended = new Date(run.endedAt || Date.now()).getTime();
  const total = Math.max(1000, ended - started);
  return `
    <div class="segment-chart">
      ${segments.map((segment) => {
        const startMs = new Date(segment.startedAt || run.startedAt).getTime();
        const endMs = new Date(segment.endedAt || Date.now()).getTime();
        const left = Math.max(0, ((startMs - started) / total) * 100);
        const width = Math.max(1.2, ((Math.max(endMs, startMs + 500) - startMs) / total) * 100);
        return `
          <div class="segment-row">
            <div class="segment-label">
              <strong>${escapeHtml(segment.label || segment.id)}</strong>
              <div class="muted">${escapeHtml(segment.type)} · ${escapeHtml(segment.status || 'unknown')}</div>
            </div>
            <div class="segment-track">
              <div class="segment-bar segment-${escapeHtml(segment.status || 'running')}" style="left:${left}%;width:${width}%">
                <span>${escapeHtml(segment.tokenUsage?.total?.output_tokens ? `${formatNumber(segment.tokenUsage.total.output_tokens)} out` : formatDuration(segment.startedAt, segment.endedAt))}</span>
              </div>
            </div>
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function renderOperatorActions(detail) {
  const actions = Array.isArray(detail.operatorActions) ? detail.operatorActions.slice().reverse() : [];
  if (!actions.length) {
    return '<div class="timeline-empty">No operator prompts or interrupts have been queued for this run.</div>';
  }
  return actions
    .map((action) => `
      <article class="timeline-event ${action.status === 'failed' ? 'timeline-error' : action.status.includes('waiting') ? 'timeline-warning' : ''}">
        <div class="timeline-event-header">
          <div>
            <span class="${pillClass(action.status)}">${escapeHtml(action.status)}</span>
            <strong>${escapeHtml(action.type)}</strong>
          </div>
          <span class="muted">${escapeHtml(formatDateTime(action.createdAt))}</span>
        </div>
        ${action.prompt ? `<pre>${escapeHtml(action.prompt)}</pre>` : `<p>${escapeHtml(action.reason || 'No reason provided.')}</p>`}
        <div class="timeline-tags">
          <span class="tag">delivery: ${escapeHtml(action.deliveryMode || 'n/a')}</span>
          <span class="tag">requested by: ${escapeHtml(action.requestedBy || 'operator')}</span>
          ${action.error ? `<span class="tag">error: ${escapeHtml(action.error)}</span>` : ''}
        </div>
      </article>
    `)
    .join('');
}

function renderLogStreamButtons(detail) {
  const streams = Array.isArray(detail.logStreams) ? detail.logStreams : [];
  if (!streams.length) {
    return '<div class="timeline-empty">No log streams are available for this run yet.</div>';
  }
  return streams
    .map((stream) => `
      <button type="button" class="subtle-button ${state.selectedLogStreamId === stream.id ? 'active-subtle-button' : ''}" data-select-log="${escapeHtml(stream.id)}">
        ${escapeHtml(stream.label)} · ${escapeHtml(stream.severity)} · ${escapeHtml(formatNumber(stream.bytes || 0))} B
      </button>
    `)
    .join('');
}

function renderRunDetail(detail) {
  const run = detail.run;
  const tokenUsage = run.tokenUsage?.total || {};
  runDetail.classList.remove('empty');
  runDetail.innerHTML = `
    <div class="card">
      <h3>${escapeHtml(run.runId)} · ${escapeHtml(run.issueIdentifier || 'unknown issue')}</h3>
      <p class="comment-meta">
        Status: <span class="${pillClass(run.status)}">${escapeHtml(run.status)}</span>
        · Worker ${escapeHtml(run.workerName || 'unknown')}
        · Model ${escapeHtml(run.effectiveModel || 'provider default')}
        · Started ${escapeHtml(formatDateTime(run.startedAt))}
      </p>
      <div class="stats-grid">
        <div class="stat-card"><span class="input-label">Current Turn</span><strong>${escapeHtml(run.currentTurnId || 'none')}</strong></div>
        <div class="stat-card"><span class="input-label">Turn Status</span><strong>${escapeHtml(run.currentTurnStatus || 'idle')}</strong></div>
        <div class="stat-card"><span class="input-label">Prompt Tokens</span><strong>${escapeHtml(formatNumber(tokenUsage.input_tokens || 0))}</strong></div>
        <div class="stat-card"><span class="input-label">Output Tokens</span><strong>${escapeHtml(formatNumber(tokenUsage.output_tokens || 0))}</strong></div>
      </div>
    </div>

    <div class="card">
      <h3>Mission Timeline</h3>
      ${renderRunSegments(detail)}
    </div>

    <div class="card">
      <h3>Operator Controls</h3>
      <div class="filter-grid">
        <label class="wide-field">
          <span class="input-label">Queued Prompt</span>
          <textarea id="run-prompt-text" rows="5" placeholder="Add trusted operator guidance, context, or a direct instruction for the active run."></textarea>
        </label>
        <label>
          <span class="input-label">Delivery Mode</span>
          <select id="run-prompt-delivery">
            ${['deliver_after_current_step', 'interrupt_now', 'deliver_when_idle'].map((mode) => `<option value="${mode}" ${mode === (state.overview?.settings?.defaultPromptDeliveryMode || 'deliver_after_current_step') ? 'selected' : ''}>${mode}</option>`).join('')}
          </select>
        </label>
        <label>
          <span class="input-label">Interrupt Reason</span>
          <input id="run-interrupt-reason" type="text" placeholder="Why should this run be interrupted?" />
        </label>
        <label>
          <span class="input-label">Checkpoint Note</span>
          <input id="run-checkpoint-note" type="text" placeholder="Optional note for the checkpoint update" />
        </label>
      </div>
      <div class="action-row">
        <button type="button" data-run-prompt="${escapeHtml(run.runId)}">Queue Prompt</button>
        <button type="button" data-run-checkpoint="${escapeHtml(run.runId)}">Request Checkpoint</button>
        <button type="button" data-run-interrupt="${escapeHtml(run.runId)}">Interrupt</button>
        ${run.issueIdentifier ? `<button type="button" data-open-issue="${escapeHtml(run.issueIdentifier)}">Open Issue</button>` : ''}
      </div>
    </div>

    <div class="two-column nested-columns">
      <div class="card">
        <h3>Operator Queue</h3>
        ${renderOperatorActions(detail)}
      </div>
      <div class="card">
        <h3>Execution Log Stream</h3>
        <div class="action-column log-stream-list">
          ${renderLogStreamButtons(detail)}
        </div>
        <pre id="run-log-output">${escapeHtml(state.selectedLogStreamId ? (state.logContents[state.selectedLogStreamId] || '') : 'Select a log stream to tail it live.')}</pre>
      </div>
    </div>
  `;
}

async function loadOverview({ preserveSelection = true } = {}) {
  const overview = await getJson('/control/api/overview');
  state.overview = overview;
  generatedAt.textContent = `Last refresh ${formatRelative(overview.generatedAt)}`;
  renderMetrics(overview);
  renderAlerts(overview);
  renderSettings(overview);
  renderHumanInbox(overview);
  renderProviders(overview);
  renderWorkers(overview);
  renderRuns(overview);
  renderIssues(overview);

  if (preserveSelection && state.selectedIssue) {
    const stillExists = overview.issues.some((issue) => issue.identifier === state.selectedIssue);
    if (stillExists) {
      await loadIssueDetail(state.selectedIssue);
    } else {
      state.selectedIssue = null;
      state.selectedDetail = null;
      issueDetail.classList.add('empty');
      issueDetail.textContent = 'Selected issue is no longer in the project view.';
    }
  }

  if (preserveSelection && state.selectedRunId) {
    const stillExists = overview.runs.some((run) => run.runId === state.selectedRunId);
    if (stillExists) {
      await loadRunDetail(state.selectedRunId, { preserveLog: true });
    } else {
      clearRunSelection('Selected run is no longer available in the local run store.');
    }
  }
}

async function loadIssueDetail(identifier) {
  state.selectedIssue = identifier;
  const detail = await getJson(`/control/api/issues/${encodeURIComponent(identifier)}`);
  state.selectedDetail = detail;
  renderIssueDetail(detail);
  if (state.overview) {
    renderIssues(state.overview);
  }
}

function currentTimelineParams() {
  if (state.selectedRunId) {
    return new URLSearchParams({ runId: state.selectedRunId, limit: '500' });
  }
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(state.timelineFilters)) {
    if (value !== null && value !== undefined && String(value).trim() !== '') {
      params.set(key === 'issue' ? 'issue' : key, String(value));
    }
  }
  return params;
}

async function loadTimeline() {
  const payload = await getJson(`/control/api/timeline?${currentTimelineParams().toString()}`);
  state.timeline = payload.events || [];
  renderTimeline();
}

function clearRunSelection(message = 'Select a run to inspect the orchestration timeline, queue prompts, interrupt, and tail logs.') {
  state.selectedRunId = null;
  state.selectedRunDetail = null;
  state.selectedLogStreamId = null;
  state.logOffsets = {};
  state.logContents = {};
  runDetail.classList.add('empty');
  runDetail.textContent = message;
  stopLogPolling();
  connectEventStream();
}

async function loadRunDetail(runId, { preserveLog = false } = {}) {
  state.selectedRunId = runId;
  const detail = await getJson(`/control/api/runs/${encodeURIComponent(runId)}`);
  state.selectedRunDetail = detail;
  if (!preserveLog || !detail.logStreams.some((stream) => stream.id === state.selectedLogStreamId)) {
    state.selectedLogStreamId = detail.logStreams[0]?.id || null;
    state.logOffsets = {};
    state.logContents = {};
  }
  renderRunDetail(detail);
  renderRuns(state.overview || { runs: [] });
  await loadTimeline();
  connectEventStream();
  startLogPolling();
  await loadSelectedLogStream(true);
}

async function loadSelectedLogStream(reset = false) {
  if (!state.selectedRunId || !state.selectedLogStreamId) {
    return;
  }
  const streamId = state.selectedLogStreamId;
  const offset = reset ? 0 : Number(state.logOffsets[streamId] || 0);
  const payload = await getJson(`/control/api/runs/${encodeURIComponent(state.selectedRunId)}/logs/${encodeURIComponent(streamId)}?offset=${offset}&limit=32768`);
  if (reset) {
    state.logContents[streamId] = payload.chunk || '';
  } else if (payload.chunk) {
    state.logContents[streamId] = (state.logContents[streamId] || '') + payload.chunk;
  }
  state.logOffsets[streamId] = payload.nextOffset || offset;
  const output = document.getElementById('run-log-output');
  if (output) {
    output.textContent = state.logContents[streamId] || '';
    output.scrollTop = output.scrollHeight;
  }
}

function startLogPolling() {
  stopLogPolling();
  if (!state.selectedRunId || !state.selectedLogStreamId) {
    return;
  }
  state.logTimer = window.setInterval(() => {
    loadSelectedLogStream(false).catch((error) => {
      alertsContainer.innerHTML = `<div class="alert alert-error">${escapeHtml(error.message)}</div>`;
    });
  }, 2000);
}

function stopLogPolling() {
  if (state.logTimer) {
    window.clearInterval(state.logTimer);
    state.logTimer = null;
  }
}

async function postIssueComment(identifier, stateName) {
  const textarea = document.getElementById('issue-comment-body');
  const body = textarea?.value?.trim();
  if (!body) {
    window.alert('Comment body is required.');
    return;
  }
  await getJson(`/control/api/issues/${encodeURIComponent(identifier)}/comment`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ body, stateName })
  });
  await loadOverview();
  await loadIssueDetail(identifier);
}

async function changeIssueState(identifier, stateName) {
  await getJson(`/control/api/issues/${encodeURIComponent(identifier)}/state`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ stateName })
  });
  await loadOverview();
  await loadIssueDetail(identifier);
}

async function changeWorkerState(workerName, action) {
  await getJson(`/control/api/workers/${encodeURIComponent(workerName)}/action`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action })
  });
  await loadOverview();
}

async function saveSettings() {
  const defaultPromptDeliveryMode = document.getElementById('settings-default-prompt-delivery')?.value?.trim();
  const rawLogRetentionDays = document.getElementById('settings-raw-log-retention')?.value?.trim();
  await getJson('/control/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ defaultPromptDeliveryMode, rawLogRetentionDays })
  });
  await loadOverview();
  await loadTimeline();
  if (state.selectedRunId) {
    await loadRunDetail(state.selectedRunId, { preserveLog: true });
  }
}

async function queueRunPrompt(runId) {
  const prompt = document.getElementById('run-prompt-text')?.value?.trim();
  const deliveryMode = document.getElementById('run-prompt-delivery')?.value?.trim();
  if (!prompt) {
    window.alert('Prompt text is required.');
    return;
  }
  await getJson(`/control/api/runs/${encodeURIComponent(runId)}/prompts`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt, deliveryMode })
  });
  await loadRunDetail(runId, { preserveLog: true });
}

async function interruptRun(runId) {
  const reason = document.getElementById('run-interrupt-reason')?.value?.trim();
  await getJson(`/control/api/runs/${encodeURIComponent(runId)}/interrupt`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason })
  });
  await loadRunDetail(runId, { preserveLog: true });
}

async function requestCheckpoint(runId) {
  const note = document.getElementById('run-checkpoint-note')?.value?.trim();
  const deliveryMode = document.getElementById('run-prompt-delivery')?.value?.trim();
  await getJson(`/control/api/runs/${encodeURIComponent(runId)}/checkpoint`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ note, deliveryMode })
  });
  await loadRunDetail(runId, { preserveLog: true });
}

async function runProviderAction(kind, workerName, action) {
  let result;
  if (kind === 'codex') {
    result = await getJson(`/control/api/providers/codex/workers/${encodeURIComponent(workerName)}/action`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action })
    });
    saveProviderOutput('codex', result);
  } else if (kind === 'openrouter') {
    const apiKey = document.getElementById('openrouter-api-key')?.value?.trim();
    result = await getJson('/control/api/providers/openrouter', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ apiKey })
    });
    saveProviderOutput('openrouter', result);
  } else if (kind === 'antigravity') {
    const body = {
      publicBaseUrl: document.getElementById('antigravity-public-base-url')?.value?.trim(),
      clientId: document.getElementById('antigravity-client-id')?.value?.trim(),
      clientSecret: document.getElementById('antigravity-client-secret')?.value?.trim(),
      scopes: document.getElementById('antigravity-scopes')?.value?.trim()
    };
    result = await getJson('/control/api/providers/antigravity', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    saveProviderOutput('antigravity', result);
  } else if (kind === 'codex-profile' || kind === 'antigravity-profile') {
    const providerId = kind === 'codex-profile' ? 'codex' : 'antigravity';
    const body = {
      subscriptionTier: document.getElementById(`${providerId}-tier-${workerName}`)?.value?.trim(),
      runtimeModel: document.getElementById(`${providerId}-runtime-model-${workerName}`)?.value?.trim(),
      availableModels: document.getElementById(`${providerId}-available-models-${workerName}`)?.value?.trim(),
      notes: document.getElementById(`${providerId}-notes-${workerName}`)?.value?.trim()
    };
    result = await getJson(`/control/api/providers/${providerId}/workers/${encodeURIComponent(workerName)}/profile`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    saveProviderOutput(providerId, result);
  } else if (kind === 'antigravity-worker') {
    const body = { action };
    if (action === 'completeAuth') {
      body.callbackInput = document.getElementById(`antigravity-callback-${workerName}`)?.value?.trim();
      if (!body.callbackInput) {
        window.alert('Paste the full callback URL or authorization code first.');
        return;
      }
    }
    result = await getJson(`/control/api/providers/antigravity/workers/${encodeURIComponent(workerName)}/action`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    saveProviderOutput('antigravity', result);
  }
  await loadOverview();
}

function reconnectTimelineWithInputs() {
  state.timelineFilters.issue = document.getElementById('timeline-issue')?.value?.trim() || '';
  state.timelineFilters.worker = document.getElementById('timeline-worker')?.value?.trim() || '';
  state.timelineFilters.eventType = document.getElementById('timeline-event-type')?.value?.trim() || '';
  state.timelineFilters.level = document.getElementById('timeline-level')?.value?.trim() || '';
  state.timelineFilters.search = document.getElementById('timeline-search')?.value?.trim() || '';
  state.timelineFilters.limit = Number(document.getElementById('timeline-limit')?.value || 300);
}

function scheduleStreamRefresh() {
  if (state.streamRefreshTimer) {
    window.clearTimeout(state.streamRefreshTimer);
  }
  state.streamRefreshTimer = window.setTimeout(async () => {
    await loadOverview();
    await loadTimeline();
    if (state.selectedRunId) {
      await loadRunDetail(state.selectedRunId, { preserveLog: true });
    }
    if (state.selectedIssue) {
      await loadIssueDetail(state.selectedIssue);
    }
  }, 600);
}

function connectEventStream() {
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
  const params = currentTimelineParams();
  state.stream = new EventSource(`/control/api/stream?${params.toString()}`);
  setStreamStatus('reconnecting');
  state.stream.addEventListener('open', () => {
    setStreamStatus('connected');
  });
  state.stream.addEventListener('timeline', () => {
    scheduleStreamRefresh();
  });
  state.stream.addEventListener('error', (event) => {
    if (event.target?.readyState === EventSource.CLOSED) {
      setStreamStatus('disconnected');
      return;
    }
    setStreamStatus('reconnecting');
  });
}

document.addEventListener('click', async (event) => {
  const openIssue = event.target.closest('[data-open-issue]');
  if (openIssue) {
    await loadIssueDetail(openIssue.dataset.openIssue);
    return;
  }

  const openRun = event.target.closest('[data-open-run]');
  if (openRun) {
    await loadRunDetail(openRun.dataset.openRun);
    return;
  }

  const clearSelectedRun = event.target.closest('[data-clear-selected-run]');
  if (clearSelectedRun) {
    clearRunSelection();
    await loadTimeline();
    return;
  }

  const selectLog = event.target.closest('[data-select-log]');
  if (selectLog) {
    state.selectedLogStreamId = selectLog.dataset.selectLog;
    state.logOffsets[state.selectedLogStreamId] = 0;
    state.logContents[state.selectedLogStreamId] = '';
    if (state.selectedRunDetail) {
      renderRunDetail(state.selectedRunDetail);
      await loadSelectedLogStream(true);
      startLogPolling();
    }
    return;
  }

  const runPrompt = event.target.closest('[data-run-prompt]');
  if (runPrompt) {
    await queueRunPrompt(runPrompt.dataset.runPrompt);
    return;
  }

  const runInterrupt = event.target.closest('[data-run-interrupt]');
  if (runInterrupt) {
    await interruptRun(runInterrupt.dataset.runInterrupt);
    return;
  }

  const runCheckpoint = event.target.closest('[data-run-checkpoint]');
  if (runCheckpoint) {
    await requestCheckpoint(runCheckpoint.dataset.runCheckpoint);
    return;
  }

  const saveSettingsButton = event.target.closest('[data-save-settings]');
  if (saveSettingsButton) {
    await saveSettings();
    return;
  }

  const applyTimelineFilters = event.target.closest('[data-apply-timeline-filters]');
  if (applyTimelineFilters) {
    reconnectTimelineWithInputs();
    clearRunSelection();
    await loadTimeline();
    connectEventStream();
    return;
  }

  const clearTimelineFilters = event.target.closest('[data-clear-timeline-filters]');
  if (clearTimelineFilters) {
    state.timelineFilters = { issue: '', worker: '', eventType: '', level: '', search: '', limit: 300 };
    clearRunSelection();
    await loadTimeline();
    connectEventStream();
    return;
  }

  const workerAction = event.target.closest('[data-worker-action]');
  if (workerAction) {
    const [workerName, action] = workerAction.dataset.workerAction.split(':');
    await changeWorkerState(workerName, action);
    return;
  }

  const providerAction = event.target.closest('[data-provider-action]');
  if (providerAction) {
    const [kind, workerNameOrAction, maybeAction] = providerAction.dataset.providerAction.split(':');
    if (kind === 'openrouter' || kind === 'antigravity') {
      await runProviderAction(kind, null, workerNameOrAction);
    } else {
      await runProviderAction(kind, workerNameOrAction, maybeAction);
    }
    return;
  }

  const issueState = event.target.closest('[data-issue-state]');
  if (issueState) {
    const [identifier, stateName] = issueState.dataset.issueState.split(':');
    await changeIssueState(identifier, stateName);
    return;
  }

  const saveComment = event.target.closest('[data-save-comment]');
  if (saveComment) {
    await postIssueComment(saveComment.dataset.saveComment, null);
    return;
  }

  const saveCommentState = event.target.closest('[data-save-comment-state]');
  if (saveCommentState) {
    const [identifier, stateName] = saveCommentState.dataset.saveCommentState.split(':');
    await postIssueComment(identifier, stateName);
  }
});

refreshButton.addEventListener('click', async () => {
  refreshButton.disabled = true;
  try {
    await loadOverview();
    await loadTimeline();
    if (state.selectedRunId) {
      await loadRunDetail(state.selectedRunId, { preserveLog: true });
    }
    if (state.selectedIssue) {
      await loadIssueDetail(state.selectedIssue);
    }
  } finally {
    refreshButton.disabled = false;
  }
});

async function bootstrap() {
  renderStreamStatus();
  try {
    await loadOverview({ preserveSelection: false });
    await loadTimeline();
    connectEventStream();
  } catch (error) {
    setStreamStatus('disconnected');
    alertsContainer.innerHTML = `<div class="alert alert-error">${escapeHtml(error.message)}</div>`;
  }

  state.refreshTimer = window.setInterval(() => {
    loadOverview().catch((error) => {
      alertsContainer.innerHTML = `<div class="alert alert-error">${escapeHtml(error.message)}</div>`;
    });
  }, 15000);
}

bootstrap();
