/* Shiro NC — Control panel frontend.
 * All requests are relative (no hardcoded host), so this keeps working
 * whether it's served at shironc.com/admin/ or admin.shironc.com/.
 */

const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  let body = null;
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) {
    // Sessions now expire, so a 401 mid-visit means the login lapsed rather
    // than anything being wrong with the request — bounce to the login screen
    // instead of painting "unauthorized" into whatever view asked.
    // The two auth endpoints answer 401 as a normal outcome (wrong password,
    // wrong code), and bouncing on those would throw away the OTP step.
    const isAuthStep = path.startsWith("api/login") || path.startsWith("api/verify-otp");
    if (res.status === 401 && !isAuthStep) showLogin();
    const err = new Error((body && body.error) || `Request failed (${res.status})`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// ---------------------------------------------------------------- toast ---

let toastTimer = null;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("is-error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

// ---------------------------------------------------------------- auth ----

async function checkAuth() {
  try {
    const { authed } = await api("api/me");
    if (authed) {
      showApp();
    } else {
      showLogin();
    }
  } catch (_) {
    showLogin();
  }
}

function showLogin() {
  $("#login-screen").hidden = false;
  $("#app-shell").hidden = true;
  showPasswordStep();
}

function showPasswordStep() {
  $("#login-form").hidden = false;
  $("#otp-form").hidden = true;
  $("#otp-code").value = "";
  $("#otp-trust").checked = false;
  $("#otp-error").hidden = true;
}

function showOtpStep(sentTo) {
  $("#login-form").hidden = true;
  $("#otp-form").hidden = false;
  $("#otp-target").textContent = sentTo || "your email";
  $("#otp-error").hidden = true;
  $("#otp-code").focus();
}

function showApp() {
  $("#login-screen").hidden = true;
  $("#app-shell").hidden = false;
  loadDashboard();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const password = $("#password").value;
  const errEl = $("#login-error");
  errEl.hidden = true;
  try {
    const res = await api("api/login", { method: "POST", body: JSON.stringify({ password }) });
    $("#password").value = "";
    if (res && res.otp_required) {
      showOtpStep(res.sent_to);
      return;
    }
    showApp();
  } catch (err) {
    errEl.textContent = err.status === 401 ? "Incorrect password." : err.message;
    errEl.hidden = false;
  }
});

$("#otp-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = $("#otp-code").value.trim();
  const trust_device = $("#otp-trust").checked;
  const errEl = $("#otp-error");
  errEl.hidden = true;
  try {
    await api("api/verify-otp", {
      method: "POST",
      body: JSON.stringify({ code, trust_device }),
    });
    showApp();
  } catch (err) {
    // attempts_left is only sent for a wrong code, i.e. the one case still
    // worth retrying. Anything else means the challenge is spent or expired,
    // so send them back rather than leaving them typing into a dead form.
    const retryable = err.body && typeof err.body.attempts_left === "number";
    if (!retryable) {
      showPasswordStep();
      const loginErr = $("#login-error");
      loginErr.textContent = err.message;
      loginErr.hidden = false;
      return;
    }
    errEl.textContent = `${err.message} — ${err.body.attempts_left} attempt(s) left.`;
    errEl.hidden = false;
    $("#otp-code").select();
  }
});

$("#otp-cancel").addEventListener("click", () => {
  showPasswordStep();
  $("#password").focus();
});

$("#logout-btn").addEventListener("click", async () => {
  await api("api/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

// ---------------------------------------------------------------- nav -----

$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

function switchView(view) {
  $$(".nav-item").forEach((b) => b.classList.toggle("is-active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.dataset.view === view));

  if (view === "dashboard") loadDashboard();
  if (view === "licenses") loadLicenses();
  if (view === "broadcast") loadBroadcast();
  if (view === "release") { loadMinVersion(); loadRelease(); }
  if (view === "poll") loadPollConfig();
  if (view === "security") loadDevices();
}

// ---------------------------------------------------------------- dashboard

async function loadDashboard() {
  try {
    const stats = await api("api/stats");
    $$("[data-stat]").forEach((el) => {
      el.textContent = stats[el.dataset.stat] ?? "—";
    });
  } catch (err) {
    toast(err.message, true);
  }
}

$("#quick-add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = $("#qa-key").value.trim();
  const days = Number($("#qa-days").value);
  const customer_name = $("#qa-customer").value.trim();
  const email = $("#qa-email").value.trim();
  const resultEl = $("#quick-add-result");
  try {
    const res = await api("api/licenses", {
      method: "POST",
      body: JSON.stringify({ key, days, customer_name, email }),
    });

    let msg = `Created: ${res.key}`;
    let isError = false;
    if (email) {
      if (res.email_sent && res.email_sent.ok) {
        msg += " — onboarding email sent";
      } else {
        msg += ` — email failed: ${(res.email_sent && res.email_sent.error) || "unknown error"}`;
        isError = true;
      }
    }
    resultEl.textContent = msg;
    resultEl.className = isError ? "field-note is-error" : "field-note is-success";

    $("#qa-key").value = "";
    $("#qa-customer").value = "";
    $("#qa-email").value = "";
    loadDashboard();
  } catch (err) {
    resultEl.textContent = err.message;
    resultEl.className = "field-note is-error";
  }
});

// ---------------------------------------------------------------- licenses

let licensesCache = [];

async function loadLicenses() {
  const tbody = $("#license-tbody");
  tbody.innerHTML = `<tr><td colspan="9" class="table-empty">Loading…</td></tr>`;
  try {
    const q = $("#license-search").value.trim();
    const { licenses } = await api(`api/licenses${q ? `?q=${encodeURIComponent(q)}` : ""}`);
    licensesCache = licenses;
    renderLicenses(licenses);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="9" class="table-empty">${escapeHtml(err.message)}</td></tr>`;
  }
}

function renderLicenses(licenses) {
  const tbody = $("#license-tbody");
  if (!licenses.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="table-empty">No licenses found.</td></tr>`;
    return;
  }
  tbody.innerHTML = licenses.map((lic) => `
    <tr>
      <td>${lic.customer_name ? escapeHtml(lic.customer_name) : "—"}</td>
      <td class="key-cell">${escapeHtml(lic.key)}</td>
      <td><span class="badge badge-${lic.status}">${lic.status}</span></td>
      <td><span class="badge badge-${lic.online ? "online" : "offline"}" title="Last seen: ${lic.last_seen ? formatDate(lic.last_seen) : "never"}">${lic.online ? "online" : "offline"}</span></td>
      <td>${formatDays(lic.days)}</td>
      <td>${lic.activated_at ? formatDate(lic.activated_at) : "—"}</td>
      <td>${lic.device_id ? escapeHtml(truncate(lic.device_id, 14)) : "—"}</td>
      <td>${lic.app_version ? escapeHtml(lic.app_version) : "—"}</td>
      <td class="actions-cell">
        <button class="btn btn-icon" data-edit="${escapeHtml(lic.key)}">Edit</button>
      </td>
    </tr>
  `).join("");

  $$("[data-edit]", tbody).forEach((btn) => {
    btn.addEventListener("click", () => openLicenseModal(btn.dataset.edit));
  });
}

function formatDays(days) {
  if (days === 0) return "Unlimited";
  if (days < 0) return `${Math.abs(days)} min (test)`;
  return `${days} days`;
}
function formatDate(ts) {
  return new Date(ts * 1000).toLocaleString();
}
function truncate(str, n) {
  return str.length > n ? str.slice(0, n) + "…" : str;
}
function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

let debounceTimer = null;
$("#license-search").addEventListener("input", () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(loadLicenses, 250);
});
$("#license-refresh").addEventListener("click", loadLicenses);
$("#license-new-btn").addEventListener("click", () => openLicenseModal(null));

// ---- license modal ----

function openLicenseModal(key) {
  const modal = $("#license-modal");
  const isNew = key === null;
  const lic = isNew ? null : licensesCache.find((l) => l.key === key);

  $("#license-modal-title").textContent = isNew ? "New license" : `Edit ${key}`;
  $("#lm-key-original").value = isNew ? "" : key;
  $("#lm-customer").value = isNew ? "" : (lic.customer_name || "");
  $("#lm-key").value = isNew ? "" : key;
  $("#lm-key").disabled = !isNew;
  $("#lm-days").value = isNew ? 90 : lic.days;
  $("#lm-reset-device").checked = false;
  $("#lm-reset-activation").checked = false;
  $("#lm-delete").hidden = isNew;
  $("#lm-submit").textContent = isNew ? "Create" : "Save changes";
  $("#license-modal-result").textContent = "";
  modal.hidden = false;
}

function closeLicenseModal() {
  $("#license-modal").hidden = true;
}
$("#lm-cancel").addEventListener("click", closeLicenseModal);
$("#license-modal").addEventListener("click", (e) => {
  if (e.target.id === "license-modal") closeLicenseModal();
});

$("#license-modal-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const original = $("#lm-key-original").value;
  const isNew = !original;
  const resultEl = $("#license-modal-result");

  try {
    if (isNew) {
      const key = $("#lm-key").value.trim();
      const days = Number($("#lm-days").value);
      const customer_name = $("#lm-customer").value.trim();
      const res = await api("api/licenses", { method: "POST", body: JSON.stringify({ key, days, customer_name }) });
      toast(`License ${res.key} created`);
    } else {
      const payload = {
        days: Number($("#lm-days").value),
        customer_name: $("#lm-customer").value.trim(),
        reset_device: $("#lm-reset-device").checked,
        reset_activation: $("#lm-reset-activation").checked,
      };
      await api(`api/licenses/${encodeURIComponent(original)}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      toast(`License ${original} updated`);
    }
    closeLicenseModal();
    loadLicenses();
    loadDashboard();
  } catch (err) {
    resultEl.textContent = err.message;
    resultEl.className = "field-note is-error";
  }
});

$("#lm-delete").addEventListener("click", async () => {
  const key = $("#lm-key-original").value;
  if (!confirm(`Delete license ${key}? This can't be undone.`)) return;
  try {
    await api(`api/licenses/${encodeURIComponent(key)}`, { method: "DELETE" });
    toast(`License ${key} deleted`);
    closeLicenseModal();
    loadLicenses();
    loadDashboard();
  } catch (err) {
    toast(err.message, true);
  }
});

// ---------------------------------------------------------------- broadcast

async function loadBroadcast() {
  try {
    const { message } = await api("api/broadcast");
    $("#broadcast-text").value = message || "";
  } catch (err) {
    toast(err.message, true);
  }
}

$("#broadcast-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const resultEl = $("#broadcast-result");
  try {
    await api("api/broadcast", {
      method: "POST",
      body: JSON.stringify({ message: $("#broadcast-text").value }),
    });
    resultEl.textContent = "Published.";
    resultEl.className = "field-note is-success";
  } catch (err) {
    resultEl.textContent = err.message;
    resultEl.className = "field-note is-error";
  }
});

$("#broadcast-clear").addEventListener("click", async () => {
  $("#broadcast-text").value = "";
  try {
    await api("api/broadcast", { method: "POST", body: JSON.stringify({ message: "" }) });
    toast("Broadcast message cleared");
  } catch (err) {
    toast(err.message, true);
  }
});

// ---------------------------------------------------------------- release --

async function loadMinVersion() {
  try {
    const { min_version } = await api("api/min-version");
    $("#min-version-input").value = min_version || "";
  } catch (err) {
    toast(err.message, true);
  }
}

$("#min-version-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const resultEl = $("#min-version-result");
  try {
    await api("api/min-version", {
      method: "POST",
      body: JSON.stringify({ min_version: $("#min-version-input").value.trim() }),
    });
    resultEl.textContent = "Saved.";
    resultEl.className = "field-note is-success";
  } catch (err) {
    resultEl.textContent = err.message;
    resultEl.className = "field-note is-error";
  }
});

async function loadRelease() {
  try {
    const { release } = await api("api/release");
    $("#rel-version").value = release.latest_version || "";
    $("#rel-object-key").value = release.object_key || "";
    $("#rel-installer-key").value = release.installer_object_key || "";
  } catch (err) {
    toast(err.message, true);
  }
}

$("#release-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const resultEl = $("#release-result");
  try {
    await api("api/release", {
      method: "POST",
      body: JSON.stringify({
        latest_version: $("#rel-version").value.trim(),
        object_key: $("#rel-object-key").value.trim(),
        installer_object_key: $("#rel-installer-key").value.trim(),
      }),
    });
    resultEl.textContent = "Saved.";
    resultEl.className = "field-note is-success";
  } catch (err) {
    resultEl.textContent = err.message;
    resultEl.className = "field-note is-error";
  }
});

// ---------------------------------------------------------------- poll ----

async function loadPollConfig() {
  try {
    const { poll_config } = await api("api/poll-config");
    $("#poll-license").value = poll_config.license_poll_interval ?? "";
    $("#poll-monitor").value = poll_config.monitor_interval ?? "";
    $("#poll-shadow").value = poll_config.shadow_check_every ?? "";
    $("#poll-failure").value = poll_config.failure_threshold ?? "";
  } catch (err) {
    toast(err.message, true);
  }
}

$("#poll-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const resultEl = $("#poll-result");
  const cfg = {
    license_poll_interval: Number($("#poll-license").value),
    monitor_interval: Number($("#poll-monitor").value),
    shadow_check_every: Number($("#poll-shadow").value),
    failure_threshold: Number($("#poll-failure").value),
  };
  try {
    await api("api/poll-config", { method: "POST", body: JSON.stringify({ poll_config: cfg }) });
    resultEl.textContent = "Saved. Clients pick this up within 30 minutes (cache TTL).";
    resultEl.className = "field-note is-success";
  } catch (err) {
    resultEl.textContent = err.message;
    resultEl.className = "field-note is-error";
  }
});

// ---------------------------------------------------------------- security

async function loadDevices() {
  const tbody = $("#device-tbody");
  tbody.innerHTML = `<tr><td colspan="5" class="table-empty">Loading…</td></tr>`;
  try {
    const { devices, current_trusted, otp_enabled, otp_email } = await api("api/devices");

    const note = $("#trust-result");
    if (!otp_enabled) {
      note.textContent =
        "ADMIN_OTP_EMAIL isn't set, so the password alone signs in anywhere — trusting a device changes nothing until you set it.";
      note.className = "field-note is-error";
    } else {
      note.textContent = `Codes are sent to ${otp_email}.`;
      note.className = "field-note";
    }

    const btn = $("#trust-device-btn");
    btn.disabled = current_trusted;
    btn.textContent = current_trusted ? "This device is trusted" : "Trust this device";

    if (!devices.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="table-empty">No trusted devices.</td></tr>`;
      return;
    }
    tbody.innerHTML = devices.map((d) => `
      <tr>
        <td>${escapeHtml(d.label || "Unknown device")}${d.current ? ' <span class="badge badge-online">this device</span>' : ""}</td>
        <td>${formatDate(d.created_at)}</td>
        <td>${d.last_used_at ? formatDate(d.last_used_at) : "—"}</td>
        <td>${formatDate(d.expires_at)}</td>
        <td class="actions-cell">
          <button class="btn btn-icon" data-revoke="${d.id}">Revoke</button>
        </td>
      </tr>
    `).join("");

    $$("[data-revoke]", tbody).forEach((b) => {
      b.addEventListener("click", () => revokeDevice(Number(b.dataset.revoke)));
    });
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="table-empty">${escapeHtml(err.message)}</td></tr>`;
  }
}

async function revokeDevice(id) {
  if (!confirm("Revoke this device? It will need an emailed code next time it signs in.")) return;
  try {
    await api(`api/devices/${id}`, { method: "DELETE" });
    toast("Device revoked.");
    loadDevices();
  } catch (err) {
    toast(err.message, true);
  }
}

$("#trust-device-btn").addEventListener("click", async () => {
  try {
    await api("api/devices/trust", { method: "POST" });
    toast("This device is now trusted.");
    loadDevices();
  } catch (err) {
    toast(err.message, true);
  }
});

// ---------------------------------------------------------------- init ----

checkAuth();
