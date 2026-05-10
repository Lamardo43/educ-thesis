import http from 'k6/http';

// const URL = 'http://192.168.1.76:8000/uuid-stub-jdk8-1.war/api/uuid';
const URL = 'http://192.168.1.76:8000/stub.war/api/uuid';
// const URL = 'http://localhost:8080/api/uuid';

const users = 600;

export const options = {
  scenarios: {
    stage_125: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: '1s',
      preAllocatedVUs: users,
      maxVUs: users*1000,
      startTime: '0s',
      stages: [
        { target: 125, duration: '10s' },
        { target: 125, duration: '120s' },
      ],
    },
    stage_250: {
      executor: 'ramping-arrival-rate',
      startRate: 100,
      timeUnit: '1s',
      preAllocatedVUs: users,
      maxVUs: users*1000,
      startTime: '130s',
      stages: [
        { target: 250, duration: '10s' },
        { target: 250, duration: '120s' },
      ],
    },
    stage_6500: {
      executor: 'ramping-arrival-rate',
      startRate: 500,
      timeUnit: '1s',
      preAllocatedVUs: users,
      maxVUs: users*1000,
      startTime: '260s',
      stages: [
        { target: 500, duration: '10s' },
        { target: 500, duration: '120s' },
      ],
    },
    stage_1000: {
      executor: 'ramping-arrival-rate',
      startRate: 1000,
      timeUnit: '1s',
      preAllocatedVUs: users,
      maxVUs: users*1000,
      startTime: '390s',
      stages: [
        { target: 1000, duration: '10s' },
        { target: 1000, duration: '120s' },
      ],
    },
  },
};

export default function () {
  http.get(URL);
}