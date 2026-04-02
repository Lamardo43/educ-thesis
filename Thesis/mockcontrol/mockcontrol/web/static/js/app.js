/**
 * MockControl — клиентский JavaScript.
 *
 * Минимальный набор функций для интерактивности SSR-интерфейса:
 * - Модальные окна (open/close)
 * - Fetch-запросы к REST API
 * - Toast-уведомления
 * - File upload (drag & drop)
 * - Переключение вкладок
 */

const API = '/api/v1';

/* ============================================================
   Toast-уведомления
   ============================================================ */

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.3s';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

/* ============================================================
   Модальные окна
   ============================================================ */

function openModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.style.display = 'flex';
}

function closeModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.style.display = 'none';
}

// Закрытие по клику на overlay
document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal-overlay')) {
        e.target.style.display = 'none';
    }
});

// Закрытие по Escape
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal-overlay').forEach(m => m.style.display = 'none');
    }
});

/* ============================================================
   Вкладки (Settings)
   ============================================================ */

function switchTab(tabName) {
    // Скрыть все панели
    document.querySelectorAll('.tab-panel').forEach(p => p.style.display = 'none');
    // Деактивировать все вкладки
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));

    // Показать выбранную
    const panel = document.getElementById(`tab-${tabName}`);
    if (panel) panel.style.display = 'block';

    // Активировать кнопку
    const tabs = document.querySelectorAll('.tab');
    tabs.forEach(t => {
        if (t.textContent.trim().toLowerCase().includes(tabName === 'hosts' ? 'хост' : 'учёт')) {
            t.classList.add('active');
        }
    });
}

/* ============================================================
   File Upload
   ============================================================ */

document.addEventListener('DOMContentLoaded', () => {
    const fileInput = document.getElementById('artifact-file');
    const dropZone = document.getElementById('file-drop-zone');

    if (fileInput) {
        fileInput.addEventListener('change', () => {
            if (fileInput.files.length > 0) {
                showSelectedFile(fileInput.files[0].name);
            }
        });
    }

    if (dropZone) {
        dropZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropZone.classList.add('drag-over');
        });
        dropZone.addEventListener('dragleave', () => {
            dropZone.classList.remove('drag-over');
        });
        dropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropZone.classList.remove('drag-over');
            if (e.dataTransfer.files.length > 0) {
                fileInput.files = e.dataTransfer.files;
                showSelectedFile(e.dataTransfer.files[0].name);
            }
        });
    }
});

function showSelectedFile(name) {
    const dropText = document.querySelector('.file-drop-text');
    const selected = document.getElementById('file-selected');
    const fileName = document.getElementById('file-name');
    if (dropText) dropText.style.display = 'none';
    if (selected) selected.style.display = 'flex';
    if (fileName) fileName.textContent = name;
}

function clearFile() {
    const fileInput = document.getElementById('artifact-file');
    const dropText = document.querySelector('.file-drop-text');
    const selected = document.getElementById('file-selected');
    if (fileInput) fileInput.value = '';
    if (dropText) dropText.style.display = 'block';
    if (selected) selected.style.display = 'none';
}

/* ============================================================
   API-запросы: Заглушки (Dashboard)
   ============================================================ */

async function registerMock() {
    const fileInput = document.getElementById('artifact-file');
    const hostname = document.getElementById('reg-hostname').value;
    const jvmArgs = document.getElementById('reg-jvm-args').value;
    const rateLimit = document.getElementById('reg-rate-limit').value;
    const startNow = document.getElementById('reg-start-now').checked;

    if (!fileInput.files.length) {
        showToast('Выберите файл артефакта', 'error');
        return;
    }
    if (!hostname) {
        showToast('Выберите целевой хост', 'error');
        return;
    }

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    formData.append('hostname', hostname);
    formData.append('jvm_args', jvmArgs);
    formData.append('rate_limit', rateLimit);
    formData.append('start_immediately', startNow);

    try {
        const resp = await fetch(`${API}/mocks`, { method: 'POST', body: formData });
        const data = await resp.json();
        if (resp.ok) {
            showToast(`Заглушка "${data.filename}" зарегистрирована`, 'success');
            closeModal('register-modal');
            setTimeout(() => location.reload(), 500);
        } else {
            showToast(data.detail || 'Ошибка регистрации', 'error');
        }
    } catch (err) {
        showToast(`Ошибка сети: ${err.message}`, 'error');
    }
}

async function mockAction(mockName, action) {
    try {
        const resp = await fetch(`${API}/mocks/${encodeURIComponent(mockName)}/${action}`, {
            method: 'POST',
        });
        const data = await resp.json();
        if (resp.ok) {
            showToast(
                `${mockName}: ${action === 'start' ? 'запущена' : 'остановлена'}`,
                'success'
            );
            setTimeout(() => location.reload(), 500);
        } else {
            showToast(data.detail || `Ошибка: ${action}`, 'error');
        }
    } catch (err) {
        showToast(`Ошибка сети: ${err.message}`, 'error');
    }
}

function confirmDelete(mockName) {
    document.getElementById('delete-mock-name').textContent = mockName;
    const btn = document.getElementById('delete-confirm-btn');
    btn.onclick = () => deleteMock(mockName);
    openModal('delete-modal');
}

async function deleteMock(mockName) {
    try {
        const resp = await fetch(`${API}/mocks/${encodeURIComponent(mockName)}`, {
            method: 'DELETE',
        });
        if (resp.ok) {
            showToast(`Заглушка "${mockName}" удалена`, 'success');
            closeModal('delete-modal');
            setTimeout(() => location.reload(), 500);
        } else {
            const data = await resp.json();
            showToast(data.detail || 'Ошибка удаления', 'error');
        }
    } catch (err) {
        showToast(`Ошибка сети: ${err.message}`, 'error');
    }
}

async function toggleRateLimit(mockName, enabled) {
    try {
        const resp = await fetch(`${API}/mocks/${encodeURIComponent(mockName)}/rate-limit`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled }),
        });
        if (resp.ok) {
            showToast(
                `Rate Limiter ${enabled ? 'включён' : 'выключен'}: ${mockName}`,
                'info'
            );
        } else {
            const data = await resp.json();
            showToast(data.detail || 'Ошибка', 'error');
        }
    } catch (err) {
        showToast(`Ошибка сети: ${err.message}`, 'error');
    }
}

/* ============================================================
   API-запросы: Хосты (Settings)
   ============================================================ */

async function addHost() {
    const body = {
        hostname:     document.getElementById('host-hostname').value,
        ssh_port:     parseInt(document.getElementById('host-ssh-port').value) || 22,
        account_uuid: document.getElementById('host-account').value,
        working_dir:  document.getElementById('host-working-dir').value,
        java_path:    document.getElementById('host-java-path').value,
        mock_port_min: parseInt(document.getElementById('host-port-min').value) || 8100,
        mock_port_max: parseInt(document.getElementById('host-port-max').value) || 8200,
        description:  document.getElementById('host-description').value,
    };

    if (!body.hostname || !body.account_uuid || !body.working_dir) {
        showToast('Заполните обязательные поля', 'error');
        return;
    }

    try {
        const resp = await fetch(`${API}/hosts`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (resp.ok) {
            showToast(`Хост "${body.hostname}" добавлен (${data.status})`, 'success');
            closeModal('add-host-modal');
            setTimeout(() => location.reload(), 500);
        } else {
            showToast(data.detail || 'Ошибка', 'error');
        }
    } catch (err) {
        showToast(`Ошибка сети: ${err.message}`, 'error');
    }
}

async function checkHost(hostname) {
    try {
        const resp = await fetch(`${API}/hosts/${encodeURIComponent(hostname)}/check`, {
            method: 'POST',
        });
        const data = await resp.json();
        if (resp.ok) {
            showToast(`${hostname}: ${data.status}`, data.status === 'AVAILABLE' ? 'success' : 'error');
            setTimeout(() => location.reload(), 1000);
        } else {
            showToast(data.detail || 'Ошибка', 'error');
        }
    } catch (err) {
        showToast(`Ошибка сети: ${err.message}`, 'error');
    }
}

async function deleteHost(hostname) {
    if (!confirm(`Удалить хост "${hostname}"?`)) return;

    try {
        const resp = await fetch(`${API}/hosts/${encodeURIComponent(hostname)}`, {
            method: 'DELETE',
        });
        if (resp.ok) {
            showToast(`Хост "${hostname}" удалён`, 'success');
            setTimeout(() => location.reload(), 500);
        } else {
            const data = await resp.json();
            showToast(data.detail || 'Ошибка', 'error');
        }
    } catch (err) {
        showToast(`Ошибка сети: ${err.message}`, 'error');
    }
}

/* ============================================================
   API-запросы: Учётные записи (Settings)
   ============================================================ */

async function addAccount() {
    const body = {
        username:    document.getElementById('acc-username').value,
        password:    document.getElementById('acc-password').value,
        description: document.getElementById('acc-description').value,
    };

    if (!body.username || !body.password) {
        showToast('Заполните имя пользователя и пароль', 'error');
        return;
    }

    try {
        const resp = await fetch(`${API}/accounts`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (resp.ok) {
            showToast(`Учётная запись "${body.username}" создана`, 'success');
            closeModal('add-account-modal');
            setTimeout(() => location.reload(), 500);
        } else {
            showToast(data.detail || 'Ошибка', 'error');
        }
    } catch (err) {
        showToast(`Ошибка сети: ${err.message}`, 'error');
    }
}

async function deleteAccount(uuid) {
    if (!confirm('Удалить учётную запись?')) return;

    try {
        const resp = await fetch(`${API}/accounts/${encodeURIComponent(uuid)}`, {
            method: 'DELETE',
        });
        if (resp.ok) {
            showToast('Учётная запись удалена', 'success');
            setTimeout(() => location.reload(), 500);
        } else {
            const data = await resp.json();
            showToast(data.detail || 'Ошибка', 'error');
        }
    } catch (err) {
        showToast(`Ошибка сети: ${err.message}`, 'error');
    }
}
