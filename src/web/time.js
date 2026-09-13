export function formatRelativeTime(value, nowMs = Date.now()) {
  const timestampMs = Date.parse(value);
  if (!Number.isFinite(timestampMs)) return "-";

  const elapsedMs = Math.max(0, nowMs - timestampMs);
  const minutes = Math.floor(elapsedMs / 60_000);
  if (minutes < 60) return `${minutes} min ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ${hours === 1 ? "hour" : "hours"} ago`;

  const days = Math.floor(hours / 24);
  return `${days} ${days === 1 ? "day" : "days"} ago`;
}

export function formatUtcTimestamp(value) {
  const timestamp = new Date(value);
  return Number.isFinite(timestamp.getTime()) ? timestamp.toISOString() : "-";
}