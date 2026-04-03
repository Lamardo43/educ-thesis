/**
 * Сценарий нагрузочного тестирования: ступенчатый рост нагрузки
 *
 * Профиль нагрузки (каждую минуту +100 RPS):
 *   0:00 – 1:00  →  100 RPS
 *   1:00 – 2:00  →  200 RPS
 *   2:00 – 3:00  →  300 RPS
 *   3:00 – 4:00  →  400 RPS
 *   4:00 – 5:00  →  500 RPS
 *
 * Использование:
 *   k6 run ramp_load.js
 *   k6 run -e TARGET_URL=http://192.168.1.76:8000/uuid-stub ramp_load.js
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend, Counter } from 'k6/metrics';

// ---------------------------------------------------------------------------
// Целевой URL (переопределяется через -e TARGET_URL=...)
// ---------------------------------------------------------------------------
const TARGET_URL = __ENV.TARGET_URL || 'http://localhost:8000/uuid-stub-jdk8-fixed-local.war/api/uuid';

// ---------------------------------------------------------------------------
// Кастомные метрики
// ---------------------------------------------------------------------------
const errorRate   = new Rate('errors');
const latency2xx  = new Trend('latency_2xx', true);
const totalReqs   = new Counter('total_requests');

// ---------------------------------------------------------------------------
// Конфигурация сценария: ramping-arrival-rate
//
// Каждый шаг = 5 сек разгон + 55 сек удержание = ровно 1 минута на уровень.
// startRate задаёт начальный темп сразу при старте.
// ---------------------------------------------------------------------------
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

  thresholds: {
    // Менее 1% ошибок на каждом уровне
    errors:          ['rate<0.01'],
    // Медиана < 500 мс, p95 < 1500 мс
    http_req_duration: ['p(50)<500', 'p(95)<1500'],
  },
};

// ---------------------------------------------------------------------------
// Основная функция виртуального пользователя
// ---------------------------------------------------------------------------
export default function () {
  const res = http.get(TARGET_URL, {
    tags:    { endpoint: TARGET_URL },
    timeout: '10s',
  });

  totalReqs.add(1);

  const ok = check(res, {
    'status 2xx': (r) => r.status >= 200 && r.status < 300,
  });

  errorRate.add(!ok);

  if (ok) {
    latency2xx.add(res.timings.duration);
  }
}
