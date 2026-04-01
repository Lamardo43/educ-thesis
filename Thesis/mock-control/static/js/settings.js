(function () {
  'use strict';

  /* ── Toast (reuse or define locally) ─────────────────────────────────── */
  function toast(message, type = 'info') {
    let container = document.getElementById('toastContainer');
    if (!container) {
      container = document.createElement('div');
      container.id = 'toastContainer';
      document.body.appendChild(container);
    }
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

  async function apiFetch(url, options = {}) {
    const res = await fetch(url, options);
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(body.detail || res.statusText);
    }
    return res.status === 204 ? null : res.json();
  }

  /* ═══════════════════════════════════════════════════════════════════════
     HOSTS
  ═══════════════════════════════════════════════════════════════════════ */
  const addHostModal = document.getElementById('addHostModal');
  const hostForm = {
    editMode:    document.getElementById('hostEditMode'),
    editName:    document.getElementById('hostEditName'),
    hostname:    document.getElementById('hostHostname'),
    sshPort:     document.getElementById('hostSshPort'),
    accountUuid: document.getElementById('hostAccountUuid'),
    workingDir:  document.getElementById('hostWorkingDir'),
    javaPath:    document.getElementById('hostJavaPath'),
    portMin:     document.getElementById('hostPortMin'),
    portMax:     document.getElementById('hostPortMax'),
    description: document.getElementById('hostDescription'),
  };

  // Reset modal to "create" mode
  addHostModal?.addEventListener('show.bs.modal', () => {
    if (hostForm.editMode.value === 'create') {
      document.getElementById('hostModalTitle').textContent = 'Добавить хост';
      hostForm.hostname.disabled = false;
      hostForm.hostname.value = '';
      hostForm.sshPort.value = '22';
      hostForm.accountUuid.value = '';
      hostForm.workingDir.value = '/opt/mock-services';
      hostForm.javaPath.value = '/usr/lib/jvm/java-17-openjdk-amd64/bin/java';
      hostForm.portMin.value = '8100';
      hostForm.portMax.value = '8200';
      hostForm.description.value = '';
    }
  });

  addHostModal?.addEventListener('hidden.bs.modal', () => {
    hostForm.editMode.value = 'create';
    hostForm.editName.value = '';
    hostForm.hostname.disabled = false;
  });

  // Edit button — prefill modal
  document.querySelectorAll('.edit-host-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const hostname = btn.dataset.hostname;
      try {
        const host = await apiFetch(`/api/v1/hosts/${encodeURIComponent(hostname)}`);
        hostForm.editMode.value = 'edit';
        hostForm.editName.value = hostname;
        document.getElementById('hostModalTitle').textContent = 'Редактировать хост';
        hostForm.hostname.value = host.hostname;
        hostForm.hostname.disabled = true;
        hostForm.sshPort.value = host.ssh_port;
        hostForm.accountUuid.value = host.account_uuid;
        hostForm.workingDir.value = host.working_dir;
        hostForm.javaPath.value = host.java_path;
        hostForm.portMin.value = host.mock_port_min;
        hostForm.portMax.value = host.mock_port_max;
        hostForm.description.value = host.description || '';
      } catch (err) {
        toast(`Ошибка загрузки хоста: ${err.message}`, 'error');
      }
    });
  });

  // Save host (create or update)
  document.getElementById('saveHostBtn')?.addEventListener('click', async () => {
    const isEdit = hostForm.editMode.value === 'edit';
    const hostnameVal = isEdit ? hostForm.editName.value : hostForm.hostname.value.trim();
    if (!hostnameVal) { toast('Укажите hostname', 'error'); return; }
    if (!hostForm.accountUuid.value) { toast('Выберите учётную запись', 'error'); return; }

    const payload = {
      hostname:       hostnameVal,
      ssh_port:       parseInt(hostForm.sshPort.value, 10),
      account_uuid:   hostForm.accountUuid.value,
      working_dir:    hostForm.workingDir.value.trim(),
      java_path:      hostForm.javaPath.value.trim(),
      mock_port_min:  parseInt(hostForm.portMin.value, 10),
      mock_port_max:  parseInt(hostForm.portMax.value, 10),
      description:    hostForm.description.value.trim(),
    };

    try {
      if (isEdit) {
        await apiFetch(`/api/v1/hosts/${encodeURIComponent(hostnameVal)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        toast(`Хост «${hostnameVal}» обновлён`, 'success');
      } else {
        await apiFetch('/api/v1/hosts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        toast(`Хост «${hostnameVal}» добавлен`, 'success');
      }
      bootstrap.Modal.getInstance(addHostModal).hide();
      setTimeout(() => location.reload(), 600);
    } catch (err) {
      toast(`Ошибка: ${err.message}`, 'error');
    }
  });

  // Delete host
  document.querySelectorAll('.delete-host-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const hostname = btn.dataset.hostname;
      if (!confirm(`Удалить хост «${hostname}»?`)) return;
      try {
        await apiFetch(`/api/v1/hosts/${encodeURIComponent(hostname)}`, { method: 'DELETE' });
        toast(`Хост «${hostname}» удалён`, 'info');
        setTimeout(() => location.reload(), 600);
      } catch (err) {
        toast(`Ошибка: ${err.message}`, 'error');
      }
    });
  });

  /* ═══════════════════════════════════════════════════════════════════════
     ACCOUNTS
  ═══════════════════════════════════════════════════════════════════════ */
  const addAccountModal = document.getElementById('addAccountModal');
  const accForm = {
    editUuid:    document.getElementById('accountEditUuid'),
    username:    document.getElementById('accountUsername'),
    password:    document.getElementById('accountPassword'),
    description: document.getElementById('accountDescription'),
  };

  addAccountModal?.addEventListener('hidden.bs.modal', () => {
    accForm.editUuid.value = '';
    accForm.username.value = '';
    accForm.password.value = '';
    accForm.description.value = '';
    document.getElementById('accountModalTitle').textContent = 'Добавить учётную запись';
    accForm.password.required = true;
  });

  document.querySelectorAll('.edit-account-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      accForm.editUuid.value = btn.dataset.uuid;
      accForm.username.value = btn.dataset.username;
      accForm.description.value = btn.dataset.description || '';
      accForm.password.value = '';
      accForm.password.required = false;  // allow empty = keep existing
      document.getElementById('accountModalTitle').textContent = 'Редактировать учётную запись';
    });
  });

  document.getElementById('saveAccountBtn')?.addEventListener('click', async () => {
    const uuid = accForm.editUuid.value;
    const username = accForm.username.value.trim();
    const password = accForm.password.value;
    const description = accForm.description.value.trim();

    if (!username) { toast('Укажите имя пользователя', 'error'); return; }
    if (!uuid && !password) { toast('Укажите пароль', 'error'); return; }

    const payload = { username, password: password || '(unchanged)', description };

    try {
      if (uuid) {
        if (!password) {
          // No password change: fetch existing encrypted value (not possible from API)
          // Workaround: require password on edit
          toast('При изменении укажите пароль', 'error');
          return;
        }
        await apiFetch(`/api/v1/accounts/${encodeURIComponent(uuid)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        toast(`Учётная запись «${username}» обновлена`, 'success');
      } else {
        await apiFetch('/api/v1/accounts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        toast(`Учётная запись «${username}» создана`, 'success');
      }
      bootstrap.Modal.getInstance(addAccountModal).hide();
      setTimeout(() => location.reload(), 600);
    } catch (err) {
      toast(`Ошибка: ${err.message}`, 'error');
    }
  });

  document.querySelectorAll('.delete-account-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const uuid = btn.dataset.uuid;
      if (!confirm('Удалить учётную запись?')) return;
      try {
        await apiFetch(`/api/v1/accounts/${encodeURIComponent(uuid)}`, { method: 'DELETE' });
        toast('Учётная запись удалена', 'info');
        setTimeout(() => location.reload(), 600);
      } catch (err) {
        toast(`Ошибка: ${err.message}`, 'error');
      }
    });
  });

  /* ═══════════════════════════════════════════════════════════════════════
     GLOBAL SETTINGS
  ═══════════════════════════════════════════════════════════════════════ */
  document.getElementById('saveSettingsBtn')?.addEventListener('click', async () => {
    const form = document.getElementById('globalSettingsForm');
    const payload = {
      rate_limit_window_size:   parseInt(form.querySelector('[name="rate_limit_window_size"]').value, 10),
      host_check_interval_sec:  parseInt(form.querySelector('[name="host_check_interval_sec"]').value, 10),
      proxy_timeout_sec:        parseInt(form.querySelector('[name="proxy_timeout_sec"]').value, 10),
      log_retention_lines:      parseInt(form.querySelector('[name="log_retention_lines"]').value, 10),
    };
    try {
      await apiFetch('/api/v1/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      toast('Настройки сохранены', 'success');
    } catch (err) {
      toast(`Ошибка: ${err.message}`, 'error');
    }
  });

})();
