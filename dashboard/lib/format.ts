export const number = (n: number | null | undefined) => (n == null ? "–" : Math.round(n).toLocaleString("en-US"));

export const ms = (n: number | null | undefined) => (n == null ? "–" : `${Math.round(n).toLocaleString("en-US")} ms`);

// Local-model prices are fractions of a cent, so small amounts get more decimals instead of "$0.0000".
export const usd = (value: string | number) => {
  const n = Number(value);
  if (n === 0) return "$0";
  return n < 0.01 ? "$" + n.toPrecision(2) : "$" + n.toFixed(2);
};

export const date = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" }) + " UTC" : "–";
