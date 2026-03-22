/* ── Toast helper ──────────────────────────────────────────────────────── */
(function () {
  'use strict';

  // Inject toast container once
  const container = document.createElement('div');
  container.id = 'toastContainer';
  document.body.appendChild(container);

  function toast(message, type = 'info') {
    const el = document.createElement('div');
    el.className = `mc-toast ${type}`;
    el.textContent = message;
    container.appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));
    setTimeout(() => {
      el.classList.remove('show');
      setTimeout(() => el.remove(), 300);
    }, 3500);
  }

  window.mcToast = toast;

  /* ── API helpers ─────────────────────────────────────────────────────── */
  async function apiPost(url) {
    const res = await fetch(url, { method: 'POST' });
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(body.detail || res.statusText);
    }
    return res.json();
  }

  async function apiDelete(url) {
    const res = await fetch(url, { method: 'DELETE' });
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(body.detail || res.statusText);
    }
  }

  async function apiPatch(url, data) {
    const res = await fetch(url, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(body.detail || res.statusText);
    }
    return res.json();
  }

  /* ── Start / Stop / Delete actions ──────────────────────────────────── */
  document.querySelectorAll('.action-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const action = btn.dataset.action;
      const mock = btn.dataset.mock;

      if (action === 'delete') {
        if (!confirm(`Удалить заглушку «${mock}»?\nПроцесс будет остановлен, файл удалён с хоста.`)) return;
      }

      btn.disabled = true;
      const originalHtml = btn.innerHTML;
      btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';

      try {
        if (action === 'start') {
          await apiPost(`/api/v1/mocks/${encodeURIComponent(mock)}/start`);
          toast(`Заглушка «${mock}» запущена`, 'success');
        } else if (action === 'stop') {
          await apiPost(`/api/v1/mocks/${encodeURIComponent(mock)}/stop`);
          toast(`Заглушка «${mock}» остановлена`, 'info');
        } else if (action === 'delete') {
          await apiDelete(`/api/v1/mocks/${encodeURIComponent(mock)}`);
          toast(`Заглушка «${mock}» удалена`, 'info');
        }
        setTimeout(() => location.reload(), 800);
      } catch (err) {
        toast(`Ошибка: ${err.message}`, 'error');
        btn.disabled = false;
        btn.innerHTML = originalHtml;
      }
    });
  });

  /* ── Rate Limiter toggle ─────────────────────────────────────────────── */
  document.querySelectorAll('.rate-limit-toggle').forEach(toggle => {
    toggle.addEventListener('change', async () => {
      const mock = toggle.dataset.mock;
      const enabled = toggle.checked;
      toggle.disabled = true;
      try {
        await apiPatch(`/api/v1/mocks/${encodeURIComponent(mock)}/rate-limit`, { enabled });
        toast(`Rate Limiter для «${mock}» ${enabled ? 'включён' : 'выключен'}`, 'info');
      } catch (err) {
        toast(`Ошибка: ${err.message}`, 'error');
        toggle.checked = !enabled;  // revert
      } finally {
        toggle.disabled = false;
      }
    });
  });

  /* ── Register modal ──────────────────────────────────────────────────── */
  const dropZone = document.getElementById('dropZone');
  const fileInput = document.getElementById('fileInput');
  const fileSelected = document.getElementById('fileSelected');

  if (dropZone) {
    dropZone.addEventListener('click', () => fileInput.click());

    dropZone.addEventListener('dragover', e => {
      e.preventDefault();
      dropZone.classList.add('dragover');
    });

    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));

    dropZone.addEventListener('drop', e => {
      e.preventDefault();
      dropZone.classList.remove('dragover');
      const file = e.dataTransfer.files[0];
      if (file) setFile(file);
    });

    fileInput.addEventListener('change', () => {
      if (fileInput.files[0]) setFile(fileInput.files[0]);
    });

    function setFile(file) {
      // Transfer file to the real input via DataTransfer
      const dt = new DataTransfer();
      dt.items.add(file);
      fileInput.files = dt.files;
      fileSelected.textContent = `✓ ${file.name} (${(file.size / 1024 / 1024).toFixed(2)} МБ)`;
      fileSelected.classList.remove('d-none');
    }
  }

  const registerBtn = document.getElementById('registerBtn');
  const registerSpinner = document.getElementById('registerSpinner');
  const registerForm = document.getElementById('registerForm');

  if (registerBtn) {
    registerBtn.addEventListener('click', async () => {
      if (!registerForm.checkValidity()) {
        registerForm.reportValidity();
        return;
      }

      registerBtn.disabled = true;
      registerSpinner.classList.remove('d-none');

      const formData = new FormData(registerForm);
      // Convert checkbox value
      const startImmediately = registerForm.querySelector('[name="start_immediately"]').checked;
      formData.set('start_immediately', startImmediately);

      try {
        const res = await fetch('/api/v1/mocks', { method: 'POST', body: formData });
        if (!res.ok) {
          const body = await res.json().catch(() => ({ detail: res.statusText }));
          throw new Error(body.detail || res.statusText);
        }
        const mock = await res.json();
        toast(`Заглушка «${mock.filename}» зарегистрирована`, 'success');
        const modal = bootstrap.Modal.getInstance(document.getElementById('registerModal'));
        modal.hide();
        setTimeout(() => location.reload(), 800);
      } catch (err) {
        toast(`Ошибка регистрации: ${err.message}`, 'error');
      } finally {
        registerBtn.disabled = false;
        registerSpinner.classList.add('d-none');
      }
    });
  }

  /* Reset modal on close */
  document.getElementById('registerModal')?.addEventListener('hidden.bs.modal', () => {
    registerForm?.reset();
    if (fileSelected) {
      fileSelected.textContent = '';
      fileSelected.classList.add('d-none');
    }
  });

})();
