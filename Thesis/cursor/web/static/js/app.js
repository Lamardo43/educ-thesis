/**
 * MockControl — минимальный vanilla JS (ТЗ: .cursor/rules/cursor_prompt.md).
 * Делегирование событий, fetch + обработка ошибок.
 */
(function () {
  "use strict";

  var logPollTimer = null;
  var dashboardPollTimer = null;
  var logsState = {
    lastMeta: { filename: "", pid: null, port: null },
  };

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function escapeAttr(s) {
    return escapeHtml(String(s)).replace(/"/g, "&quot;");
  }

  function readErrorDetail(response) {
    return response
      .json()
      .then(function (body) {
        if (!body || typeof body !== "object") return response.statusText;
        var d = body.detail;
        if (typeof d === "string") return d;
        if (Array.isArray(d)) return JSON.stringify(d, null, 2);
        if (d && typeof d === "object") return JSON.stringify(d, null, 2);
        return response.statusText;
      })
      .catch(function () {
        return response.statusText;
      });
  }

  function setButtonLoading(btn, loading) {
    if (!btn) return;
    if (loading) {
      if (!btn.dataset.origHtml) btn.dataset.origHtml = btn.innerHTML;
      btn.innerHTML =
        '<span class="btn__spinner" aria-hidden="true"></span><span class="sr-only">Загрузка…</span>';
      btn.disabled = true;
    } else {
      if (btn.dataset.origHtml) {
        btn.innerHTML = btn.dataset.origHtml;
        delete btn.dataset.origHtml;
      }
      btn.disabled = false;
    }
  }

  function badgeHtml(status) {
    var s = String(status || "");
    var cls = "badge badge--unknown";
    if (s === "RUNNING") cls = "badge badge--running";
    else if (s === "ERROR") cls = "badge badge--error";
    else if (s === "REGISTERED") cls = "badge badge--registered";
    else if (s === "STOPPED") cls = "badge badge--stopped";
    return '<span class="' + cls + '">' + escapeHtml(s) + "</span>";
  }

  function portCell(mock) {
    if (mock.port != null && mock.port !== "") {
      return escapeHtml(String(mock.port));
    }
    return '<span class="text-dash">—</span>';
  }

  function buildMockRow(mock) {
    var fn = mock.filename;
    var fnA = escapeAttr(fn);
    var checked = mock.rate_limit_enabled ? " checked" : "";
    var startOrStop =
      mock.status === "RUNNING"
        ? '<button type="button" class="btn btn--ghost btn--sm" data-action="mock-stop" data-mock-filename="' +
          fnA +
          '">Стоп</button>'
        : '<button type="button" class="btn btn--ghost btn--sm" data-action="mock-start" data-mock-filename="' +
          fnA +
          '">Старт</button>';
    return (
      "<tr data-mock-filename=\"" +
      fnA +
      "\">" +
      '<td class="mono">' +
      escapeHtml(fn) +
      "</td>" +
      "<td>" +
      escapeHtml(mock.hostname || "") +
      "</td>" +
      "<td>" +
      badgeHtml(mock.status) +
      "</td>" +
      "<td>" +
      portCell(mock) +
      "</td>" +
      "<td>" +
      '<label class="toggle" title="Включить или выключить ограничение запросов" data-action="mock-rate-limit-toggle" data-mock-filename="' +
      fnA +
      '">' +
      "<input type=\"checkbox\"" +
      checked +
      ' aria-label="Rate limiter для ' +
      fnA +
      '"' +
      ' data-mock-filename="' +
      fnA +
      '"' +
      ' data-rate-limit-enabled="' +
      (mock.rate_limit_enabled ? "true" : "false") +
      "\" />" +
      '<span class="toggle__track"><span class="toggle__thumb"></span></span>' +
      "</label>" +
      "</td>" +
      "<td><div class=\"table-actions\">" +
      startOrStop +
      ' <button type="button" class="btn btn--danger btn--sm" data-action="mock-delete" data-mock-filename="' +
      fnA +
      '">Удалить</button>' +
      "</div></td></tr>"
    );
  }

  function updateDashboardCards(dashboard, mocks, hosts) {
    var grid = dashboard.querySelector(".stat-grid");
    if (!grid) return;
    var cards = grid.querySelectorAll("article.card .card__value");
    if (cards.length < 4) return;
    var total = mocks.length;
    var running = mocks.filter(function (m) {
      return m.status === "RUNNING";
    }).length;
    var errors = mocks.filter(function (m) {
      return m.status === "ERROR";
    }).length;
    var hostsOk = Array.isArray(hosts)
      ? hosts.filter(function (h) {
          return h.status === "AVAILABLE";
        }).length
      : 0;
    cards[0].textContent = String(total);
    cards[1].textContent = String(running);
    cards[2].textContent = String(errors);
    cards[3].textContent = String(hostsOk);
  }

  function renderDashboardTable(dashboard, mocks) {
    var tbody = dashboard.querySelector(".table tbody");
    if (!tbody) return;
    if (!mocks.length) {
      tbody.innerHTML =
        '<tr><td colspan="6" class="text-muted" style="text-align:center;padding:24px">Нет зарегистрированных заглушек. Нажмите «+ Зарегистрировать заглушку».</td></tr>';
      return;
    }
    tbody.innerHTML = mocks.map(buildMockRow).join("");
  }

  function refreshDashboardFromApi(dashboard) {
    var apiMocks = dashboard.dataset.apiMocks || "/api/v1/mocks";
    var apiHosts = dashboard.dataset.apiHosts || "/api/v1/hosts";
    return Promise.all([
      fetch(apiMocks, { credentials: "same-origin" }).then(function (r) {
        if (!r.ok) throw new Error("mocks " + r.status);
        return r.json();
      }),
      fetch(apiHosts, { credentials: "same-origin" }).then(function (r) {
        if (!r.ok) throw new Error("hosts " + r.status);
        return r.json();
      }),
    ])
      .then(function (pair) {
        var mocks = pair[0];
        var hosts = pair[1];
        renderDashboardTable(dashboard, Array.isArray(mocks) ? mocks : []);
        updateDashboardCards(
          dashboard,
          Array.isArray(mocks) ? mocks : [],
          Array.isArray(hosts) ? hosts : []
        );
      })
      .catch(function (err) {
        try {
          console.warn(err);
        } catch (e) {}
      });
  }

  function startDashboardPoll(dashboard) {
    if (dashboardPollTimer) clearInterval(dashboardPollTimer);
    var ms = parseInt(dashboard.dataset.pollIntervalMs || "5000", 10);
    dashboardPollTimer = setInterval(function () {
      refreshDashboardFromApi(dashboard);
    }, ms);
  }

  function openModal(backdrop) {
    if (!backdrop) return;
    backdrop.classList.add("is-open");
    backdrop.setAttribute("aria-hidden", "false");
  }

  function closeModal(backdrop) {
    if (!backdrop) return;
    backdrop.classList.remove("is-open");
    backdrop.setAttribute("aria-hidden", "true");
  }

  function closestModalBackdrop(el) {
    return el && el.closest && el.closest(".modal-backdrop");
  }

  function onDashboardClick(e) {
    var t = e.target;
    var regModal = document.getElementById("modal-register-mock");
    if (regModal && e.target === regModal) {
      closeModal(regModal);
      return;
    }
    var closeReg = t.closest("[data-action=\"close-register-mock-modal\"]");
    if (closeReg) {
      e.preventDefault();
      closeModal(regModal);
      return;
    }

    var dashboard = t.closest(".dashboard");
    if (!dashboard) return;

    var openReg = t.closest("[data-action=\"open-register-mock-modal\"]");
    if (openReg) {
      e.preventDefault();
      openModal(regModal);
      return;
    }

    var startBtn = t.closest("[data-action=\"mock-start\"]");
    if (startBtn) {
      e.preventDefault();
      var fn = startBtn.getAttribute("data-mock-filename");
      if (!fn) return;
      setButtonLoading(startBtn, true);
      var api = dashboard.dataset.apiMocks || "/api/v1/mocks";
      fetch(api + "/" + encodeURIComponent(fn) + "/start", {
        method: "POST",
        credentials: "same-origin",
      })
        .then(function (r) {
          if (!r.ok) return readErrorDetail(r).then(function (msg) { throw new Error(msg); });
        })
        .then(function () {
          return refreshDashboardFromApi(dashboard);
        })
        .catch(function (err) {
          alert(err.message || String(err));
        })
        .finally(function () {
          setButtonLoading(startBtn, false);
        });
      return;
    }

    var stopBtn = t.closest("[data-action=\"mock-stop\"]");
    if (stopBtn) {
      e.preventDefault();
      var fn2 = stopBtn.getAttribute("data-mock-filename");
      if (!fn2) return;
      setButtonLoading(stopBtn, true);
      var api2 = dashboard.dataset.apiMocks || "/api/v1/mocks";
      fetch(api2 + "/" + encodeURIComponent(fn2) + "/stop", {
        method: "POST",
        credentials: "same-origin",
      })
        .then(function (r) {
          if (!r.ok) return readErrorDetail(r).then(function (msg) { throw new Error(msg); });
        })
        .then(function () {
          return refreshDashboardFromApi(dashboard);
        })
        .catch(function (err) {
          alert(err.message || String(err));
        })
        .finally(function () {
          setButtonLoading(stopBtn, false);
        });
      return;
    }

    var delBtn = t.closest("[data-action=\"mock-delete\"]");
    if (delBtn) {
      e.preventDefault();
      var fn3 = delBtn.getAttribute("data-mock-filename");
      if (!fn3) return;
      if (!confirm("Удалить заглушку «" + fn3 + "»?")) return;
      setButtonLoading(delBtn, true);
      var api3 = dashboard.dataset.apiMocks || "/api/v1/mocks";
      fetch(api3 + "/" + encodeURIComponent(fn3), {
        method: "DELETE",
        credentials: "same-origin",
      })
        .then(function (r) {
          if (!r.ok && r.status !== 204) {
            return readErrorDetail(r).then(function (msg) { throw new Error(msg); });
          }
        })
        .then(function () {
          return refreshDashboardFromApi(dashboard);
        })
        .catch(function (err) {
          alert(err.message || String(err));
        })
        .finally(function () {
          setButtonLoading(delBtn, false);
        });
    }
  }

  function onDashboardChange(e) {
    var t = e.target;
    if (!t.matches || !t.matches("input[type=\"checkbox\"][data-mock-filename]")) return;
    var label = t.closest("[data-action=\"mock-rate-limit-toggle\"]");
    if (!label || !t.closest(".dashboard")) return;

    var dashboard = t.closest(".dashboard");
    var fn = t.getAttribute("data-mock-filename");
    if (!fn) return;
    var api = dashboard.dataset.apiMocks || "/api/v1/mocks";
    t.disabled = true;

    fetch(api + "/" + encodeURIComponent(fn) + "/rate-limit", {
      method: "PATCH",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: t.checked }),
    })
      .then(function (r) {
        if (!r.ok) return readErrorDetail(r).then(function (msg) { throw new Error(msg); });
        return r.json();
      })
      .then(function (data) {
        var en = !!(data && data.rate_limit_enabled);
        t.checked = en;
        t.setAttribute("data-rate-limit-enabled", en ? "true" : "false");
      })
      .catch(function () {
        t.checked = !t.checked;
      })
      .finally(function () {
        t.disabled = false;
      });
  }

  function initRegisterModal(dashboard) {
    var modal = document.getElementById("modal-register-mock");
    var form = document.getElementById("form-register-mock");
    if (!modal || !form) return;

    var dz = form.querySelector("[data-register-dropzone]");
    var fileInput = form.querySelector("[data-register-file-input]");
    var fileNameEl = form.querySelector("[data-register-file-name]");

    function showFileName(file) {
      if (!fileNameEl || !fileInput) return;
      if (file) {
        fileNameEl.textContent = file.name;
        fileNameEl.hidden = false;
      } else {
        fileNameEl.textContent = "";
        fileNameEl.hidden = true;
      }
    }

    function setFile(file) {
      if (!fileInput || !file) return;
      var max = dz ? parseInt(dz.getAttribute("data-max-bytes") || "0", 10) : 0;
      if (max && file.size > max) {
        alert("Файл слишком большой.");
        return;
      }
      try {
        var dt = new DataTransfer();
        dt.items.add(file);
        fileInput.files = dt.files;
        showFileName(file);
      } catch (err) {
        alert("Не удалось прикрепить файл.");
      }
    }

    if (dz && fileInput) {
      dz.addEventListener("dragover", function (ev) {
        ev.preventDefault();
        dz.classList.add("is-dragover");
      });
      dz.addEventListener("dragleave", function () {
        dz.classList.remove("is-dragover");
      });
      dz.addEventListener("drop", function (ev) {
        ev.preventDefault();
        dz.classList.remove("is-dragover");
        var f = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
        if (f) setFile(f);
      });
      fileInput.addEventListener("change", function () {
        var f = fileInput.files && fileInput.files[0];
        showFileName(f || null);
      });
    }

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (!fileInput || !fileInput.files || !fileInput.files[0]) {
        alert("Выберите файл .jar / .war.");
        return;
      }
      var submitBtn = form.querySelector("[data-action=\"submit-register-mock\"]");
      setButtonLoading(submitBtn, true);
      var fd = new FormData(form);
      var action = form.getAttribute("action") || "/api/v1/mocks";
      fetch(action, {
        method: "POST",
        body: fd,
        credentials: "same-origin",
      })
        .then(function (r) {
          if (r.status === 202 || r.ok) {
            window.location.reload();
            return;
          }
          return readErrorDetail(r).then(function (msg) { throw new Error(msg); });
        })
        .catch(function (err) {
          alert(err.message || String(err));
        })
        .finally(function () {
          setButtonLoading(submitBtn, false);
        });
    });
  }

  function initDashboard() {
    var dashboard = document.querySelector(".dashboard");
    if (!dashboard) return;
    document.addEventListener("click", onDashboardClick);
    document.addEventListener("change", onDashboardChange);
    initRegisterModal(dashboard);
    refreshDashboardFromApi(dashboard);
    startDashboardPoll(dashboard);
  }

  function switchSettingsTab(tab) {
    var root = document.querySelector(".settings-page");
    if (!root) return;
    var buttons = root.querySelectorAll(".tabs__btn[data-action=\"settings-tab\"]");
    var panels = root.querySelectorAll(".tab-panel[data-tab-panel]");
    buttons.forEach(function (btn) {
      var is = btn.getAttribute("data-tab") === tab;
      btn.classList.toggle("is-active", is);
      btn.setAttribute("aria-selected", is ? "true" : "false");
    });
    panels.forEach(function (p) {
      var is = p.getAttribute("data-tab-panel") === tab;
      p.classList.toggle("is-active", is);
    });
  }

  function findHostRow(hostname) {
    var rows = document.querySelectorAll("[data-host-row]");
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute("data-hostname") === hostname) return rows[i];
    }
    return null;
  }

  function findAccountRow(uuid) {
    var rows = document.querySelectorAll("[data-account-row]");
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute("data-account-uuid") === uuid) return rows[i];
    }
    return null;
  }

  function setHostFormMode(form, mode, editHostname) {
    var modeInput = form.querySelector("[data-host-form-mode]");
    var title = document.querySelector("[data-host-modal-title]");
    var hn = form.querySelector("[data-host-field=\"hostname\"]");
    if (modeInput) modeInput.value = mode;
    if (title) title.textContent = mode === "edit" ? "Изменить хост" : "Добавить хост";
    if (hn) {
      hn.readOnly = mode === "edit";
      hn.classList.toggle("is-readonly", mode === "edit");
    }
    form.dataset.editHostname = editHostname || "";
  }

  function fillHostFormFromRow(row) {
    var form = document.getElementById("form-host");
    if (!form || !row) return;
    var fields = ["hostname", "ssh_port", "account_uuid", "working_dir", "java_path", "mock_port_min", "mock_port_max", "description"];
    fields.forEach(function (name) {
      var el = form.querySelector("[data-host-field=\"" + name + "\"]");
      if (!el) return;
      var val;
      if (name === "hostname") val = row.getAttribute("data-hostname");
      else if (name === "ssh_port") val = row.getAttribute("data-ssh-port");
      else if (name === "account_uuid") val = row.getAttribute("data-account-uuid");
      else if (name === "working_dir") val = row.getAttribute("data-working-dir");
      else if (name === "java_path") val = row.getAttribute("data-java-path");
      else if (name === "mock_port_min") val = row.getAttribute("data-mock-port-min");
      else if (name === "mock_port_max") val = row.getAttribute("data-mock-port-max");
      else if (name === "description") val = row.getAttribute("data-description");
      if (val == null) val = "";
      el.value = val;
    });
  }

  function collectHostJson(form) {
    return {
      ssh_port: parseInt(form.querySelector("[data-host-field=\"ssh_port\"]").value, 10),
      account_uuid: form.querySelector("[data-host-field=\"account_uuid\"]").value.trim(),
      working_dir: form.querySelector("[data-host-field=\"working_dir\"]").value.trim(),
      java_path: form.querySelector("[data-host-field=\"java_path\"]").value.trim(),
      mock_port_min: parseInt(form.querySelector("[data-host-field=\"mock_port_min\"]").value, 10),
      mock_port_max: parseInt(form.querySelector("[data-host-field=\"mock_port_max\"]").value, 10),
      description: (form.querySelector("[data-host-field=\"description\"]").value || "").trim(),
    };
  }

  /** Проверка обязательных полей хоста (создание и редактирование). */
  function validateHostRequired(form) {
    var hostname = form.querySelector("[data-host-field=\"hostname\"]").value.trim();
    var body = collectHostJson(form);
    var sp = body.ssh_port;
    var pmin = body.mock_port_min;
    var pmax = body.mock_port_max;
    if (!hostname) {
      alert("Укажите hostname или IP.");
      return false;
    }
    if (!body.account_uuid) {
      alert("Выберите учётную запись.");
      return false;
    }
    if (!body.working_dir) {
      alert("Укажите рабочую директорию.");
      return false;
    }
    if (!body.java_path) {
      alert("Укажите путь к Java.");
      return false;
    }
    if (!Number.isFinite(sp) || sp < 1 || sp > 65535) {
      alert("Укажите корректный SSH-порт (1–65535).");
      return false;
    }
    if (!Number.isFinite(pmin) || pmin < 1 || pmin > 65535) {
      alert("Укажите корректный минимальный порт мока (1–65535).");
      return false;
    }
    if (!Number.isFinite(pmax) || pmax < 1 || pmax > 65535) {
      alert("Укажите корректный максимальный порт мока (1–65535).");
      return false;
    }
    return true;
  }

  function resetHostForm(form) {
    form.reset();
    var sp = form.querySelector("[data-host-field=\"ssh_port\"]");
    if (sp) sp.value = "22";
    var wd = form.querySelector("[data-host-field=\"working_dir\"]");
    if (wd) wd.value = "/opt/mocks";
    var jp = form.querySelector("[data-host-field=\"java_path\"]");
    if (jp) jp.value = "/usr/bin/java";
    var pmin = form.querySelector("[data-host-field=\"mock_port_min\"]");
    if (pmin) pmin.value = "8100";
    var pmax = form.querySelector("[data-host-field=\"mock_port_max\"]");
    if (pmax) pmax.value = "8200";
    setHostFormMode(form, "add", "");
    var hn = form.querySelector("[data-host-field=\"hostname\"]");
    if (hn) hn.readOnly = false;
  }

  function onSettingsClick(e) {
    var t = e.target;
    var page = t.closest(".settings-page");
    if (!page) return;

    var tabBtn = t.closest("[data-action=\"settings-tab\"]");
    if (tabBtn) {
      e.preventDefault();
      switchSettingsTab(tabBtn.getAttribute("data-tab"));
      return;
    }

    var openHost = t.closest("[data-action=\"open-host-modal\"]");
    if (openHost) {
      e.preventDefault();
      var form = document.getElementById("form-host");
      if (form) resetHostForm(form);
      openModal(document.getElementById("modal-host"));
      return;
    }

    var editHost = t.closest("[data-action=\"host-edit\"]");
    if (editHost) {
      e.preventDefault();
      var hn = editHost.getAttribute("data-hostname");
      var row = findHostRow(hn);
      var form2 = document.getElementById("form-host");
      if (form2 && row) {
        fillHostFormFromRow(row);
        setHostFormMode(form2, "edit", hn);
        openModal(document.getElementById("modal-host"));
      }
      return;
    }

    var delHost = t.closest("[data-action=\"host-delete\"]");
    if (delHost) {
      e.preventDefault();
      var hname = delHost.getAttribute("data-hostname");
      if (!hname || !confirm("Удалить хост «" + hname + "»?")) return;
      var api = page.dataset.apiHosts || "/api/v1/hosts";
      fetch(api + "/" + encodeURIComponent(hname), {
        method: "DELETE",
        credentials: "same-origin",
      })
        .then(function (r) {
          if (r.status === 204) {
            window.location.reload();
            return;
          }
          return readErrorDetail(r).then(function (msg) { throw new Error(msg); });
        })
        .catch(function (err) {
          alert(err.message || String(err));
        });
      return;
    }

    var openAcc = t.closest("[data-action=\"open-account-modal\"]");
    if (openAcc) {
      e.preventDefault();
      var af = document.getElementById("form-account");
      if (af) {
        af.reset();
        var uuidEl = af.querySelector("[data-account-field=\"uuid\"]");
        if (uuidEl) uuidEl.value = "";
        var hint = af.querySelector("[data-account-password-hint]");
        if (hint) hint.textContent = "Обязательно при создании записи.";
        var pt = af.querySelector("[data-account-field=\"password\"]");
        if (pt) {
          pt.required = true;
        }
        var at = document.querySelector("[data-account-modal-title]");
        if (at) at.textContent = "Добавить учётную запись";
      }
      openModal(document.getElementById("modal-account"));
      return;
    }

    var editAcc = t.closest("[data-action=\"account-edit\"]");
    if (editAcc) {
      e.preventDefault();
      var uid = editAcc.getAttribute("data-account-uuid");
      var arow = findAccountRow(uid);
      var af2 = document.getElementById("form-account");
      if (af2 && arow) {
        af2.querySelector("[data-account-field=\"uuid\"]").value = uid;
        af2.querySelector("[data-account-field=\"username\"]").value =
          arow.getAttribute("data-username") || "";
        af2.querySelector("[data-account-field=\"description\"]").value =
          arow.getAttribute("data-description") || "";
        af2.querySelector("[data-account-field=\"password\"]").value = "";
        var hint2 = af2.querySelector("[data-account-password-hint]");
        if (hint2) hint2.textContent = "Оставьте пустым, чтобы не менять пароль.";
        var pt2 = af2.querySelector("[data-account-field=\"password\"]");
        if (pt2) pt2.required = false;
        var at2 = document.querySelector("[data-account-modal-title]");
        if (at2) at2.textContent = "Изменить учётную запись";
        openModal(document.getElementById("modal-account"));
      }
      return;
    }

    var delAcc = t.closest("[data-action=\"account-delete\"]");
    if (delAcc) {
      e.preventDefault();
      var uuid = delAcc.getAttribute("data-account-uuid");
      if (!uuid || !confirm("Удалить эту учётную запись?")) return;
      var apiA = page.dataset.apiAccounts || "/api/v1/accounts";
      fetch(apiA + "/" + encodeURIComponent(uuid), {
        method: "DELETE",
        credentials: "same-origin",
      })
        .then(function (r) {
          if (r.status === 204) {
            window.location.reload();
            return;
          }
          return readErrorDetail(r).then(function (msg) { throw new Error(msg); });
        })
        .catch(function (err) {
          alert(err.message || String(err));
        });
      return;
    }

    var ch = t.closest("[data-action=\"close-host-modal\"]");
    if (ch) {
      e.preventDefault();
      closeModal(document.getElementById("modal-host"));
      return;
    }
    var ca = t.closest("[data-action=\"close-account-modal\"]");
    if (ca) {
      e.preventDefault();
      closeModal(document.getElementById("modal-account"));
      return;
    }

    var mHost = document.getElementById("modal-host");
    if (mHost && e.target === mHost) {
      closeModal(mHost);
      return;
    }
    var mAcc = document.getElementById("modal-account");
    if (mAcc && e.target === mAcc) {
      closeModal(mAcc);
      return;
    }
  }

  function onSettingsSubmit(e) {
    var form = e.target;
    if (!form || !form.closest(".settings-page")) return;
    if (form.id === "form-host") {
      e.preventDefault();
      var page = form.closest(".settings-page");
      var api = page.dataset.apiHosts || "/api/v1/hosts";
      var modeInput = form.querySelector("[data-host-form-mode]");
      var mode = modeInput && modeInput.value === "edit" ? "edit" : "add";
      var submitBtn = form.querySelector("[data-action=\"submit-host-form\"]");
      setButtonLoading(submitBtn, true);

      var run = function () {
        if (!validateHostRequired(form)) {
          return Promise.resolve();
        }
        if (mode === "add") {
          var hostname = form.querySelector("[data-host-field=\"hostname\"]").value.trim();
          var body = collectHostJson(form);
          body.hostname = hostname;
          return fetch(api, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }).then(function (r) {
            if (r.ok) {
              window.location.reload();
              return;
            }
            return readErrorDetail(r).then(function (msg) { throw new Error(msg); });
          });
        }
        var orig = form.dataset.editHostname || "";
        var bodyPut = collectHostJson(form);
        return fetch(api + "/" + encodeURIComponent(orig), {
          method: "PUT",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(bodyPut),
        }).then(function (r) {
          if (r.ok) {
            window.location.reload();
            return;
          }
          return readErrorDetail(r).then(function (msg) { throw new Error(msg); });
        });
      };

      run()
        .catch(function (err) {
          alert(err.message || String(err));
        })
        .finally(function () {
          setButtonLoading(submitBtn, false);
        });
      return;
    }

    if (form.id === "form-account") {
      e.preventDefault();
      var page2 = form.closest(".settings-page");
      var apiA = page2.dataset.apiAccounts || "/api/v1/accounts";
      var uuid = form.querySelector("[data-account-field=\"uuid\"]").value.trim();
      var username = form.querySelector("[data-account-field=\"username\"]").value.trim();
      var password = form.querySelector("[data-account-field=\"password\"]").value;
      var description = form.querySelector("[data-account-field=\"description\"]").value || "";
      var submitBtn2 = form.querySelector("[data-action=\"submit-account-form\"]");
      setButtonLoading(submitBtn2, true);

      var p = function () {
        if (!uuid) {
          if (!username) {
            alert("Укажите имя пользователя (логин).");
            return Promise.resolve();
          }
          if (!password || !password.trim()) {
            alert("Укажите пароль.");
            return Promise.resolve();
          }
          return fetch(apiA, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username: username, password: password, description: description }),
          }).then(function (r) {
            if (r.ok) {
              window.location.reload();
              return;
            }
            return readErrorDetail(r).then(function (msg) { throw new Error(msg); });
          });
        }
        if (!username) {
          alert("Укажите имя пользователя (логин).");
          return Promise.resolve();
        }
        var body = { username: username, description: description };
        if (password && password.trim()) body.password = password;
        return fetch(apiA + "/" + encodeURIComponent(uuid), {
          method: "PUT",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }).then(function (r) {
          if (r.ok) {
            window.location.reload();
            return;
          }
          return readErrorDetail(r).then(function (msg) { throw new Error(msg); });
        });
      };

      p()
        .catch(function (err) {
          alert(err.message || String(err));
        })
        .finally(function () {
          setButtonLoading(submitBtn2, false);
        });
    }
  }

  function initSettings() {
    if (!document.querySelector(".settings-page")) return;
    document.addEventListener("click", onSettingsClick);
    document.addEventListener("submit", onSettingsSubmit);
  }

  function formatLogLine(raw) {
    var ts = "";
    var rest = raw;
    var iso = raw.match(
      /^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,3})?(?:Z|[+-]\d{2}:?\d{2})?)\s*/
    );
    if (iso) {
      ts = iso[1];
      rest = raw.slice(iso[0].length);
    } else {
      var br = raw.match(/^\[[^\]\n]{1,64}\]\s*/);
      if (br) {
        ts = br[0].trim();
        rest = raw.slice(br[0].length);
      }
    }
    var isError = /\bERROR\b/.test(raw);
    var isWarn = /\bWARN\b/.test(raw) && !isError;
    var cls = "terminal__line";
    if (isError) cls += " terminal__line--error";
    else if (isWarn) cls += " terminal__line--warn";
    var tsHtml = ts
      ? '<span class="terminal__ts">' + escapeHtml(ts) + "</span>"
      : "";
    return "<p class=\"" + cls + "\">" + tsHtml + escapeHtml(rest) + "</p>";
  }

  function renderLogTerminal(lines) {
    var terminal = document.getElementById("log-terminal");
    var lineCountEl = document.getElementById("log-line-count");
    if (!terminal) return;
    if (!lines || !lines.length) {
      terminal.innerHTML = "";
      if (lineCountEl) lineCountEl.textContent = "0";
      return;
    }
    terminal.innerHTML = lines.map(formatLogLine).join("");
    if (lineCountEl) lineCountEl.textContent = String(lines.length);
    terminal.scrollTop = terminal.scrollHeight;
  }

  function updateLogStatusBar() {
    var statusMain = document.getElementById("log-status-main");
    var m = logsState.lastMeta;
    var fn = m.filename || "—";
    var pid = m.pid != null && m.pid !== "" ? String(m.pid) : "—";
    var port = m.port != null && m.port !== "" ? String(m.port) : "—";
    if (statusMain) {
      statusMain.textContent = fn + " — PID " + pid + " — порт " + port;
    }
  }

  function logsRefreshAll(root) {
    var apiLogs = root.dataset.apiLogsBase || "/api/v1/logs";
    var apiMocks = root.dataset.apiMocksBase || "/api/v1/mocks";
    var linesN = parseInt(root.dataset.defaultLines || "1000", 10);
    var select = document.getElementById("log-mock-select");
    var fn = select && select.value && select.value.trim();
    if (!fn) {
      logsState.lastMeta = { filename: "", pid: null, port: null };
      updateLogStatusBar();
      renderLogTerminal([]);
      return Promise.resolve();
    }
    logsState.lastMeta.filename = fn;
    updateLogStatusBar();

    return fetch(apiMocks + "/" + encodeURIComponent(fn), { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("mock");
        return r.json();
      })
      .then(function (data) {
        logsState.lastMeta.pid = data.pid != null ? data.pid : null;
        logsState.lastMeta.port = data.port != null ? data.port : null;
        updateLogStatusBar();
        return fetch(
          apiLogs + "/" + encodeURIComponent(fn) + "?lines=" + encodeURIComponent(String(linesN)),
          { credentials: "same-origin" }
        );
      })
      .then(function (r) {
        if (!r.ok) throw new Error("logs");
        return r.json();
      })
      .then(function (arr) {
        renderLogTerminal(Array.isArray(arr) ? arr : []);
      })
      .catch(function () {
        logsState.lastMeta.pid = null;
        logsState.lastMeta.port = null;
        updateLogStatusBar();
        renderLogTerminal([]);
      });
  }

  function logsClear(root) {
    var apiLogs = root.dataset.apiLogsBase || "/api/v1/logs";
    var select = document.getElementById("log-mock-select");
    var fn = select && select.value && select.value.trim();
    if (!fn) return;
    fetch(apiLogs + "/" + encodeURIComponent(fn), {
      method: "DELETE",
      credentials: "same-origin",
    })
      .then(function (r) {
        if (r.ok || r.status === 204) return logsRefreshAll(root);
      })
      .catch(function () {});
  }

  function logsStartPoll(root) {
    if (logPollTimer) {
      clearInterval(logPollTimer);
      logPollTimer = null;
    }
    var auto = document.getElementById("log-auto-refresh");
    if (!auto || !auto.checked) return;
    var ms = parseInt(root.dataset.pollIntervalMs || "2000", 10);
    logPollTimer = setInterval(function () {
      var sel = document.getElementById("log-mock-select");
      if (sel && sel.value) logsRefreshAll(root);
    }, ms);
  }

  function logsSetControlsEnabled(enabled) {
    var ids = ["log-btn-refresh", "log-btn-clear", "log-auto-refresh"];
    ids.forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.disabled = !enabled;
    });
  }

  function onLogsPageClick(e) {
    var t = e.target;
    var root = t.closest(".logs-page");
    if (!root) return;
    var btn = t.closest("button");
    if (btn && btn.id === "log-btn-refresh") {
      e.preventDefault();
      try {
        logsRefreshAll(root);
      } catch (err) {
        console.warn(err);
      }
      return;
    }
    if (btn && btn.id === "log-btn-clear") {
      e.preventDefault();
      try {
        logsClear(root);
      } catch (err) {
        console.warn(err);
      }
    }
  }

  function onLogsPageChange(e) {
    var t = e.target;
    if (t.id !== "log-auto-refresh") return;
    var root = t.closest(".logs-page");
    if (!root) return;
    logsStartPoll(root);
    if (t.checked) logsRefreshAll(root);
  }

  function onLogsSelectChange(e) {
    if (e.target.id !== "log-mock-select") return;
    var root = e.target.closest(".logs-page");
    if (!root) return;
    logsRefreshAll(root);
    logsStartPoll(root);
  }

  function initLogs() {
    var root = document.querySelector(".logs-page");
    if (!root) return;
    var select = document.getElementById("log-mock-select");
    var hasMocks =
      select &&
      Array.prototype.some.call(select.options, function (o) {
        return o.value && !o.disabled;
      });
    logsSetControlsEnabled(!!hasMocks);
    document.addEventListener("click", onLogsPageClick);
    document.addEventListener("change", function (e) {
      onLogsPageChange(e);
      onLogsSelectChange(e);
    });
    window.addEventListener("beforeunload", function () {
      if (logPollTimer) clearInterval(logPollTimer);
    });
  }

  function init() {
    try {
      initDashboard();
      initSettings();
      initLogs();
    } catch (err) {
      try {
        console.warn(err);
      } catch (e) {}
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
