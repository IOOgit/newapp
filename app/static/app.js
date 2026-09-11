// Минимальный клиент дашборда: добавление/тест/удаление/опрос устройств.
const $ = (sel) => document.querySelector(sel);

function formData() {
  const f = $("#device-form");
  return {
    name: f.name.value,
    host: f.host.value,
    http_port: parseInt(f.http_port.value || "80", 10),
    username: f.username.value,
    password: f.password.value,
    api_type: f.api_type.value,
    timeout: parseFloat(f.timeout.value || "15"),
    latitude: f.latitude.value ? parseFloat(f.latitude.value) : null,
    longitude: f.longitude.value ? parseFloat(f.longitude.value) : null,
  };
}

document.addEventListener("DOMContentLoaded", () => {
  // Здесь только UTC-метки проверок; время самих записей показывает шаблон NVR.
  document.querySelectorAll("time[data-monitoring-time]").forEach((el) => {
    const value = el.dataset.monitoringTime;
    const stamp = new Date(value);
    if (!Number.isNaN(stamp.getTime())) {
      el.dateTime = value;
      el.textContent = stamp.toLocaleString("ru-RU");
    }
  });
  const modal = $("#modal");
  const openBtn = $("#add-device-btn");
  if (openBtn) openBtn.onclick = (e) => { e.preventDefault(); modal.classList.remove("hidden"); };
  const cancel = $("#cancel-btn");
  if (cancel) cancel.onclick = () => modal.classList.add("hidden");

  const testBtn = $("#test-btn");
  if (testBtn) testBtn.onclick = async () => {
    const out = $("#test-result");
    out.textContent = "Проверка соединения…";
    const d = formData();
    const resp = await fetch("/api/devices/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(d),
    });
    const r = await resp.json();
    out.textContent = (r.ok ? "✅ OK\n" : "❌ Ошибка\n") +
      `тип: ${r.api_type} (${r.auth_scheme})\n` +
      (r.model ? `модель: ${r.model}\nпрошивка: ${r.firmware || "-"}\n` : "") +
      `возможности: ${JSON.stringify(r.capabilities)}\n${r.detail}`;
    if (r.ok && r.api_type) $("#device-form").api_type.value = r.api_type;
  };

  const form = $("#device-form");
  if (form) form.onsubmit = async (e) => {
    e.preventDefault();
    const resp = await fetch("/api/devices", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formData()),
    });
    if (resp.ok) location.reload();
    else $("#test-result").textContent = "Ошибка сохранения: " + (await resp.text());
  };
});

async function pollNow(id) {
  await fetch(`/api/devices/${id}/poll`, { method: "POST" });
  location.reload();
}
function recordingFeedback(message, failed = false) {
  const out = document.getElementById("recording-action-result");
  if (out) {
    out.hidden = false;
    out.className = failed ? "err-text" : "muted";
    out.textContent = message;
  }
}
async function recordingCheck(id, button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Проверяем запись…";
  recordingFeedback("Проверяем свежие фрагменты архива по каналам…");
  try {
    const response = await fetch(`/api/devices/${id}/recording-check`, { method: "POST" });
    if (!response.ok) throw new Error(await response.text());
    location.reload();
  } catch (error) {
    recordingFeedback("Не удалось проверить запись: " + error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}
async function setRecordingMode(deviceId, channelId, select) {
  const previous = select.dataset.previousMode;
  const mode = select.value;
  select.disabled = true;
  recordingFeedback(`Сохраняем ожидаемый режим записи канала ${channelId}…`);
  try {
    const response = await fetch(`/api/devices/${deviceId}/channels/${channelId}/recording`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recording_mode: mode }),
    });
    if (!response.ok) throw new Error(await response.text());
    select.dataset.previousMode = mode;
    location.reload();
  } catch (error) {
    select.value = previous;
    recordingFeedback("Не удалось сохранить режим: " + error.message, true);
  } finally {
    select.disabled = false;
  }
}
async function editCoords(id, lat, lon) {
  const cur = (lat !== null ? lat : "") + ", " + (lon !== null ? lon : "");
  const v = prompt("Координаты объекта (широта, долгота). Пусто — очистить:", cur.trim() === "," ? "" : cur);
  if (v === null) return;
  const parts = v.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean);
  const latitude = parts[0] ? parseFloat(parts[0]) : null;
  const longitude = parts[1] ? parseFloat(parts[1]) : null;
  const r = await fetch(`/api/devices/${id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ latitude, longitude }),
  });
  if (r.ok) location.reload();
  else alert("Ошибка: " + (await r.text()));
}
async function recheck(id) {
  const r = await fetch(`/api/devices/${id}/recheck`, { method: "POST" });
  if (r.ok) location.reload();
  else alert("Не удалось перепроверить: " + (await r.text()));
}
async function syncTime(id) {
  const r = await fetch(`/api/devices/${id}/sync-time`, { method: "POST" });
  if (r.ok) { alert("Время синхронизировано"); location.reload(); }
  else alert("Ошибка: " + (await r.text()));
}
async function rebootDevice(id) {
  if (!confirm("Перезагрузить регистратор? Запись и просмотр прервутся на пару минут.")) return;
  const r = await fetch(`/api/devices/${id}/reboot`, { method: "POST" });
  alert(r.ok ? "Команда перезагрузки отправлена" : "Ошибка: " + (await r.text()));
}
async function archiveDepth(id) {
  if (!confirm("Измерить реальную глубину архива? Это может занять до минуты.")) return;
  const r = await fetch(`/api/devices/${id}/archive-depth`, { method: "POST" });
  alert(r.ok ? "Глубина архива измерена" : "Ошибка: " + (await r.text()));
  if (r.ok) location.reload();
}
async function qualityCheck(id) {
  const r = await fetch(`/api/devices/${id}/quality-check`, { method: "POST" });
  alert(r.ok ? "Качество картинки проверено" : "Ошибка: " + (await r.text()));
  if (r.ok) location.reload();
}
async function toggleChannel(deviceId, channelId, btn) {
  const r = await fetch(`/api/devices/${deviceId}/channels/${channelId}/toggle`, { method: "POST" });
  if (r.ok) location.reload();
  else alert("Ошибка: " + (await r.text()));
}
// ── Массовые операции с прогрессом (фоновый прогон, опрос статуса) ──────────
async function bulkRun(action) {
  const sel = document.getElementById("bulk-group");
  const groupId = sel ? sel.value : "";
  if (action === "sync_time" && !confirm("Синхронизировать время на выбранных регистраторах?")) return;
  const q = new URLSearchParams({ action });
  if (groupId) q.set("group_id", groupId);
  const r = await fetch(`/api/bulk/run?${q}`, { method: "POST" });
  if (r.status === 409) { alert("Массовая операция уже выполняется — дождитесь завершения."); return; }
  if (!r.ok) { alert("Ошибка: " + (await r.text())); return; }
  bulkPollStatus();
}

let _bulkTimer = null;
async function bulkPollStatus() {
  const box = document.getElementById("bulk-progress");
  const fill = document.getElementById("bulk-fill");
  const text = document.getElementById("bulk-text");
  const st = await fetch("/api/bulk/status").then(r => r.json()).catch(() => null);
  if (!st) return;
  if (box) box.style.display = "";
  const pct = st.total ? Math.round(st.done / st.total * 100) : 0;
  if (fill) fill.style.width = pct + "%";
  if (text) {
    text.textContent = st.running
      ? `${st.label}: ${st.done}/${st.total}${st.current ? " · " + st.current : ""}`
      : `${st.label || "Готово"}: ${st.ok} ок, ${st.failed} с ошибкой из ${st.total}`;
  }
  if (st.running) {
    _bulkTimer = setTimeout(bulkPollStatus, 1000);
  } else {
    clearTimeout(_bulkTimer);
    setTimeout(() => location.reload(), 1200);  // подтянуть свежие статусы
  }
}

// Совместимость со старыми кнопками дашборда
function bulkPoll() { bulkRun("poll"); }
function bulkSyncTime() { bulkRun("sync_time"); }
function diag(id) {
  const p = prompt(
    "Эндпоинт NVR для диагностики:",
    "/ISAPI/System/Video/inputs/channels"
  );
  if (p) window.open(`/api/devices/${id}/raw?path=${encodeURIComponent(p)}`, "_blank");
}
function showSnap(deviceId, channelId, btn) {
  const cell = btn.parentElement;
  const url = `/api/devices/${deviceId}/channels/${channelId}/snapshot?t=` + Date.now();
  const img = document.createElement("img");
  img.className = "snap";
  img.src = url;
  img.onerror = () => { cell.innerHTML = "<span class='muted'>нет кадра</span>"; };
  img.onclick = () => window.open(url, "_blank");
  cell.innerHTML = "";
  cell.appendChild(img);
}
async function archiveCheck(id) {
  const r = await fetch(`/api/devices/${id}/archive-check`, { method: "POST" });
  const j = await r.json();
  alert("Проверка архива за " + j.day + " выполнена");
  location.reload();
}
async function deleteDevice(id) {
  if (!confirm("Удалить устройство?")) return;
  await fetch(`/api/devices/${id}`, { method: "DELETE" });
  location.href = "/";
}
