// Measurement 2 (D-121): throughput at a stated concurrency, 50 virtual users in a
// closed loop for 60 s on alias `steady`, after a 10 s warm-up with 5 that is not counted.
// Environment: TARGET, ALIAS, LGW_KEY. Requests do not opt into the cache (D-119).
// One console line per request, read by loadtest/lgwload/records.py (D-122).

import http from 'k6/http';
import exec from 'k6/execution';

export const options = {
  scenarios: {
    warmup: {
      executor: 'constant-vus',
      vus: 5,
      duration: '10s',
    },
    measure: {
      executor: 'constant-vus',
      vus: 50,
      duration: '60s',
      startTime: '10s',
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
  messages: [{ role: 'user', content: 'throughput request' }],
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
