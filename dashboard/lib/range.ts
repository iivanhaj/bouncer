// Plain module (not "use client"), so server pages can call parseDays: functions exported from
// a client module become references the server cannot call.
export const RANGES = [
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
  { days: 90, label: "90 days" },
];

export function parseDays(value: string | string[] | undefined): number {
  const days = Number(Array.isArray(value) ? value[0] : value);
  return RANGES.some((r) => r.days === days) ? days : 30;
}
