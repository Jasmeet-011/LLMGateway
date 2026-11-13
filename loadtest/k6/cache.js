// Measurement 3 (D-121): cache hit and miss latency and hit rate on workload
// `hot10-repeat80`. Warm-up: 10 req/s for 10 s on alias `steady`, not opted in, not
// counted. Measured: 50 req/s for 60 s, every request opted in with x-gateway-cache: use.
// Request i of `measure` (from 0) sends a unique prompt when i mod 5 = 0; otherwise hot
// prompt k mod 10, where k = i - floor(i/5) - 1 counts the other requests from 0, so the
// ten hot prompts take turns. Every prompt carries RUN_ID, so each run starts cold.
// Environment: TARGET, ALIAS, LGW_KEY, RUN_ID.
// One console line per request, read by loadtest/lgwload/records.py (D-122).

import http from 'k6/http';
import exec from 'k6/execution';

export const options = {
  scenarios: {
    warmup: {
      executor: 'constant-arrival-rate',
      rate: 10,
      timeUnit: '1s',
      duration: '10s',
      preAllocatedVUs: 20,
      maxVUs: 200,
    },
    measure: {
      executor: 'constant-arrival-rate',
      rate: 50,
      timeUnit: '1s',
      duration: '60s',
      startTime: '10s',
      preAllocatedVUs: 50,
      maxVUs: 500,
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(50)', 'p(95)', 'p(99)'],
};

const URL = `${__ENV.TARGET}/v1/chat`;
const RUN_ID = __ENV.RUN_ID || 'no-run-id';
const HEADERS = {
  'Content-Type': 'application/json',
  Authorization: `Bearer ${__ENV.LGW_KEY || ''}`,
};

function prompt(i) {
  if (i % 5 === 0) {
    return `cold ${RUN_ID} ${i}`;
  }
  const k = i - Math.floor(i / 5) - 1;
  return `hot ${RUN_ID} ${k % 10}`;
}

function header(res, name) {
  const value = res.headers[name];
  return value === undefined ? null : value;
}

function record(res, started) {
  let ok = false;
  let instance = null;
  if (res.status === 200) {
    try {
      const body = res.json();
      ok = typeof body.content === 'string';
      instance = typeof body.provider_instance === 'string' ? body.provider_instance : null;
    } catch (e) {
      ok = false;
    }
  }
  console.log(
    JSON.stringify({
      sc: exec.scenario.name,
      t: started,
      s: res.status,
      d: res.timings.duration,
      ok: ok,
      c: header(res, 'X-Gateway-Cache'),
      i: instance,
      ra: header(res, 'Retry-After'),
    }),
  );
}

export default function () {
  const measuring = exec.scenario.name === 'measure';
  const content = measuring ? prompt(exec.scenario.iterationInTest) : 'warmup request';
  const headers = measuring ? Object.assign({ 'x-gateway-cache': 'use' }, HEADERS) : HEADERS;
  const body = JSON.stringify({
    model: __ENV.ALIAS || 'steady',
    messages: [{ role: 'user', content: content }],
    max_tokens: 64,
  });
  const started = Date.now();
  const res = http.post(URL, body, { headers: headers, timeout: '120s' });
  record(res, started);
}
