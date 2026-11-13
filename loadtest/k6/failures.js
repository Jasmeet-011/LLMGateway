// Measurements 4 and 8 (D-120, D-121): success rate under injected failures.
// One scenario, 50 req/s, no warm-up (a warm-up would spend mock rolls and breaker
// outcomes, and in measurement 8 it would start the fault window's clock).
// Measurement 4: 60 s on alias failures-off (A) or failures-on (B).
// Measurement 8: 150 s on alias outage-off or outage-on, with the window 60 s to 90 s.
// Environment: TARGET, ALIAS, LGW_KEY, MEASUREMENT (4 or 8).
// Requests do not opt into the cache (D-119).
// One console line per request, read by loadtest/lgwload/records.py (D-122).

import http from 'k6/http';
import exec from 'k6/execution';

const DURATIONS = { 4: '60s', 8: '150s' };
const DURATION = DURATIONS[__ENV.MEASUREMENT];
if (DURATION === undefined) {
  throw new Error('MEASUREMENT must be 4 or 8');
}

export const options = {
  scenarios: {
    measure: {
      executor: 'constant-arrival-rate',
      rate: 50,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 100,
      maxVUs: 1000,
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
  model: __ENV.ALIAS,
  messages: [{ role: 'user', content: 'failure test request' }],
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
