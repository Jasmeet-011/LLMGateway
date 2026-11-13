// Measurement 5 (D-121): 429 behavior under a burst, with compose.burst.yaml's limits
// (a burst of 20 per key, refilled at 1 per second).
// `burst`: 100 requests from 100 virtual users at once.
// `paced`: from 40 s, when the bucket has refilled, 0.5 req/s for 20 s; all within the limit.
// Environment: TARGET, ALIAS, LGW_KEY. Requests do not opt into the cache (D-119).
// One console line per request, read by loadtest/lgwload/records.py (D-122).

import http from 'k6/http';
import exec from 'k6/execution';

export const options = {
  scenarios: {
    burst: {
      executor: 'shared-iterations',
      vus: 100,
      iterations: 100,
      maxDuration: '30s',
    },
    paced: {
      executor: 'constant-arrival-rate',
      rate: 1,
      timeUnit: '2s',
      duration: '20s',
      startTime: '40s',
      preAllocatedVUs: 2,
      maxVUs: 10,
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(50)', 'p(95)', 'p(99)'],
};

const URL = `${__ENV.TARGET}/v1/chat`;
const PARAMS = {
  headers: {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${__ENV.LGW_KEY || ''}`,
  },
  timeout: '120s',
};
const BODY = JSON.stringify({
  model: __ENV.ALIAS || 'steady',
  messages: [{ role: 'user', content: 'burst request' }],
  max_tokens: 64,
});

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
  const started = Date.now();
  const res = http.post(URL, BODY, PARAMS);
  record(res, started);
}
