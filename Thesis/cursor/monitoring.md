# Monitoring Proxy Metrics

Ниже перечислены метрики, которые были добавлены для диагностики всплесков latency в прокси, и примеры PromQL-запросов для визуализации.

## Labels

- `mock` — имя заглушки (filename), также есть агрегат `mock="__all__"`.
- `stage` — этап обработки запроса (например, `redis_mock_hmget_ms`, `httpx_pool_wait_ms`, `proxy_total_ms`).
- `outcome` — исход проксирования (`ok`, `timeout`, `request_error`, `not_found`, `not_running`, `invalid_target`, `rate_limited`).
- `status` — HTTP-статус upstream-ответа (например, `200`, `500`).

## 1) `mockcontrol_proxy_stage_latency_ms_sum`

Counter: накопленная сумма времени этапа в миллисекундах.

**PromQL (скорость роста суммарного времени этапа):**

```promql
sum by (mock, stage) (
  rate(mockcontrol_proxy_stage_latency_ms_sum{mock!="__all__"}[5m])
)
```

Что показывает график: сколько миллисекунд в секунду "съедает" каждый этап. Удобно видеть, какой этап начал резко дорожать после 2.5 минут.

## 2) `mockcontrol_proxy_stage_latency_ms_count`

Counter: количество наблюдений по этапу.

**PromQL (RPS по этапу):**

```promql
sum by (mock, stage) (
  rate(mockcontrol_proxy_stage_latency_ms_count{mock!="__all__"}[5m])
)
```

Что показывает график: частоту наблюдений этапа (практически частоту запросов, если этап выполняется на каждом запросе). Помогает отличить рост latency от роста нагрузки.

## 3) `mockcontrol_proxy_stage_latency_ms_avg`

Gauge: среднее время этапа (`sum / count`), уже вычисляется на стороне exporter.

**PromQL (текущее среднее по этапу):**

```promql
avg by (mock, stage) (
  mockcontrol_proxy_stage_latency_ms_avg{mock!="__all__"}
)
```

Что показывает график: среднюю задержку каждого этапа. Это главный график для поиска "узкого места" в пайплайне прокси.

## 4) `mockcontrol_proxy_stage_latency_ms_last`

Gauge: последнее измеренное значение времени этапа.

**PromQL (последнее значение с сглаживанием max):**

```promql
max_over_time(
  mockcontrol_proxy_stage_latency_ms_last{mock!="__all__", stage="proxy_total_ms"}[5m]
)
```

Что показывает график: недавние пики времени по этапу (например, end-to-end `proxy_total_ms`), даже если среднее не сильно изменилось.

## 5) `mockcontrol_proxy_outcome_total`

Counter: количество исходов проксирования по типам (`ok`, `timeout`, `rate_limited`, ...).

**PromQL (доля ошибок по исходам):**

```promql
sum by (mock, outcome) (
  rate(mockcontrol_proxy_outcome_total{mock!="__all__", outcome!="ok"}[5m])
)
```

Что показывает график: какой тип неуспеха растет во времени (например, `timeout` или `rate_limited`) и у какой заглушки.

## 6) `mockcontrol_proxy_upstream_status_total`

Counter: количество HTTP-статусов, которые вернул upstream.

**PromQL (rate по классам статусов):**

```promql
sum by (mock, status) (
  rate(mockcontrol_proxy_upstream_status_total{mock!="__all__"}[5m])
)
```

Что показывает график: распределение ответов upstream (2xx/4xx/5xx в разрезе кода). Полезно, если всплеск latency сопровождается ростом 5xx.

## Практический набор графиков для всплеска после ~2.5 минут

1. **End-to-end latency:** `stage="proxy_total_ms"` по `mockcontrol_proxy_stage_latency_ms_avg`.
2. **Pool wait:** `stage="httpx_pool_wait_ms"` по `mockcontrol_proxy_stage_latency_ms_avg`.
3. **Redis этапы:** `stage=~"redis_.*"` по `mockcontrol_proxy_stage_latency_ms_avg`.
4. **Outcomes:** `rate(mockcontrol_proxy_outcome_total[5m])` по `outcome`.
5. **Upstream status:** `rate(mockcontrol_proxy_upstream_status_total[5m])` по `status`.

Если график `httpx_pool_wait_ms` растет раньше `proxy_total_ms`, причина обычно в исчерпании пула/очереди соединений. Если растут `redis_*` этапы — вероятен bottleneck Redis.
