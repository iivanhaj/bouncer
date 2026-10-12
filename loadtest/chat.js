// k6 load test: a fixed arrival rate of chat completions against one target.
//
// Run through loadtest/run.py, which starts it in Docker on the Compose network and runs it
// against both the mock upstream directly and the gateway in front of it.
//
//   TARGET    base URL (http://mock-upstream:8001 or http://gateway:8000)
//   API_KEY   gateway key (ignored by the mock)
//   RATE      requests per second
//   DURATION  e.g. "15s"
//   STREAM    "true" for streamed responses
//   OUT       path for the JSON summary
import http from "k6/http";
import { check } from "k6";

const RATE = Number(__ENV.RATE || 50);
const STREAM = __ENV.STREAM === "true";

export const options = {
  discardResponseBodies: !STREAM,
  scenarios: {
    load: {
      // Constant arrival rate: requests keep arriving on schedule even if responses slow down,
      // like real traffic. (A fixed number of looping users would slow down with the server and
      // hide latency problems.)
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: __ENV.DURATION || "15s",
      preAllocatedVUs: Math.max(20, RATE),
      maxVUs: Math.max(200, RATE * 4),
    },
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
};

const body = JSON.stringify({
  model: "mock",
  stream: STREAM,
  max_tokens: 20,
  messages: [{ role: "user", content: "Summarize the quarterly report in two sentences." }],
});

export default function () {
  const response = http.post(`${__ENV.TARGET}/v1/chat/completions`, body, {
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${__ENV.API_KEY}` },
    timeout: "30s",
  });
  check(response, { "status is 200": (r) => r.status === 200 });
}

export function handleSummary(data) {
  return { [__ENV.OUT || "/results/summary.json"]: JSON.stringify(data) };
}
