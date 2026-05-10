#!/bin/bash
export K6_PROMETHEUS_RW_SERVER_URL="http://192.168.1.82:9090/api/v1/write"
#export K6_PROMETHEUS_RW_USERNAME="my-user"
#export K6_PROMETHEUS_RW_PASSWORD="my-password"
export K6_PROMETHEUS_RW_TREND_STATS="p(95),p(99),avg"
export K6_PROMETHEUS_RW_STALE_MARKERS=true

./k6.exe run -o experimental-prometheus-rw $1