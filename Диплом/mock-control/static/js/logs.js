(function () {
  'use strict';

  const mockSelect   = document.getElementById('mockSelect');
  const logContainer = document.getElementById('logContainer');
  const autoRefresh  = document.getElementById('autoRefresh');
  const refreshBtn   = document.getElementById('refreshBtn');
  const clearBtn     = document.getElementById('clearBtn');
  const processInfo  = document.getElementById('processInfo');

  let pollTimer = null;
  const POLL_INTERVAL_MS = 3000;

  /* ── Log rendering ───────────────────────────────────────────────────── */
  function renderLog(text) {
    if (!text || !text.trim()) {
      logContainer.innerHTML = '<span class="text-muted">Лог пуст или файл ещё не создан.</span>';
      return;
    }

    const fragment = document.createDocumentFragment();
    const lines = text.split('\n');

    lines.forEach(line => {
      const span = document.createElement('span');
      span.textContent = line + '\n';

      const upper = line.toUpperCase();
      if (upper.includes(' ERROR') || upper.includes('EXCEPTION')) {
        span.className = 'log-line-error';
      } else if (upper.includes(' WARN')) {
        span.className = 'log-line-warn';
      }

      // Highlight timestamps: 2024-11-01 or similar ISO-like patterns
      if (/^\d{4}-\d{2}-\d{2}/.test(line)) {
        const tsEnd = line.indexOf(' ', 20);
        if (tsEnd > 0) {
          const ts = document.createElement('span');
          ts.className = 'log-timestamp';
          ts.textContent = line.substring(0, tsEnd);
          span.textContent = '';
          span.appendChild(ts);
          span.appendChild(document.createTextNode(line.substring(tsEnd) + '\n'));
        }
      }

      fragment.appendChild(span);
    });

    logContainer.innerHTML = '';
    logContainer.appendChild(fragment);
    logContainer.scrollTop = logContainer.scrollHeight;
  }

  /* ── Fetch logs from API ─────────────────────────────────────────────── */
  async function fetchLogs() {
    const mock = mockSelect.value;
    if (!mock) return;

    try {
      const res = await fetch(`/api/v1/mocks/${encodeURIComponent(mock)}/logs?lines=500`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: res.statusText }));
        logContainer.innerHTML = `<span class="text-danger">Ошибка: ${body.detail}</span>`;
        return;
      }
      const text = await res.text();
      renderLog(text);
    } catch (err) {
      logContainer.innerHTML = `<span class="text-danger">Ошибка загрузки: ${err.message}</span>`;
    }
  }

  /* ── Fetch mock status for info bar ──────────────────────────────────── */
  async function updateProcessInfo() {
    const mock = mockSelect.value;
    if (!mock) { processInfo.textContent = ''; return; }
    try {
      const res = await fetch(`/api/v1/mocks/${encodeURIComponent(mock)}`);
      if (!res.ok) return;
      const data = await res.json();
      const pid  = data.pid  ? `PID ${data.pid}` : 'нет PID';
      const port = data.port ? `порт ${data.port}` : 'нет порта';
      processInfo.textContent = `${mock} · ${pid} · ${port} · ${data.status}`;
    } catch (_) { /* ignore */ }
  }

  /* ── Polling control ─────────────────────────────────────────────────── */
  function startPolling() {
    stopPolling();
    if (mockSelect.value && autoRefresh.checked) {
      pollTimer = setInterval(async () => {
        await fetchLogs();
        await updateProcessInfo();
      }, POLL_INTERVAL_MS);
    }
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  /* ── Event listeners ─────────────────────────────────────────────────── */
  mockSelect.addEventListener('change', async () => {
    logContainer.innerHTML = '<span class="text-muted">Загрузка...</span>';
    await fetchLogs();
    await updateProcessInfo();
    startPolling();
  });

  autoRefresh.addEventListener('change', () => {
    if (autoRefresh.checked) startPolling();
    else stopPolling();
  });

  refreshBtn.addEventListener('click', async () => {
    await fetchLogs();
    await updateProcessInfo();
  });

  clearBtn.addEventListener('click', () => {
    logContainer.innerHTML = '<span class="text-muted">Буфер очищен.</span>';
  });

  // Check if mock was pre-selected via URL hash
  const hash = window.location.hash.replace('#', '');
  if (hash && mockSelect.querySelector(`option[value="${hash}"]`)) {
    mockSelect.value = hash;
    fetchLogs();
    updateProcessInfo();
    startPolling();
  }

})();
