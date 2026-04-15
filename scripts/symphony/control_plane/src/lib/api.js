export async function getJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

export function formatRelative(dateText) {
  if (!dateText) return 'unknown';
  const deltaMs = Date.now() - new Date(dateText).getTime();
  const deltaMinutes = Math.floor(deltaMs / 60_000);
  if (deltaMinutes < 1) return 'just now';
  if (deltaMinutes < 60) return `${deltaMinutes}m ago`;
  const deltaHours = Math.floor(deltaMinutes / 60);
  if (deltaHours < 24) return `${deltaHours}h ago`;
  return `${Math.floor(deltaHours / 24)}d ago`;
}

export function formatDateTime(dateText) {
  if (!dateText) return 'unknown';
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(dateText));
}

export function formatDuration(startedAt, endedAt) {
  if (!startedAt) return 'n/a';
  const start = new Date(startedAt).getTime();
  const end = endedAt ? new Date(endedAt).getTime() : Date.now();
  const seconds = Math.floor(Math.max(0, end - start) / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function formatNumber(value) {
  if (value === null || value === undefined || value === '') return 'n/a';
  return new Intl.NumberFormat().format(Number(value));
}

export function pillStatus(status) {
  return String(status || 'unknown').replace(/[^a-z0-9_]+/gi, '_').toLowerCase();
}

export function coerceList(value) {
  if (Array.isArray(value)) return value.filter(Boolean);
  return String(value || '')
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}
