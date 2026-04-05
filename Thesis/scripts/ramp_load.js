
import http from 'k6/http';

// ---------------------------------------------------------------------------
// Целевой URL (переопределяется через -e TARGET_URL=...)
// ---------------------------------------------------------------------------
const TARGET_URL = 'http://localhost:8000/uuid-stub-jdk8-fixed-local.war/api/uuid';
//const TARGET_URL = 'http://192.168.1.76:8100/api/uuid';

export const options = {
  scenarios: {
    ramp_load: {
      executor: 'ramping-arrival-rate',
      startRate: 200,
      timeUnit: '1s',
      preAllocatedVUs: 150,
      maxVUs: 700,
      stages: [
        { duration: '55s', target: 200 },
        { duration: '55s', target: 300 },
        { duration: '55s', target: 400 },
        { duration: '60s', target: 500 },
      ],
    },
  },
};

// ---------------------------------------------------------------------------
// Основная функция виртуального пользователя
// ---------------------------------------------------------------------------
export default function () {
  const res = http.get(TARGET_URL, {timeout: '10s'});
}
