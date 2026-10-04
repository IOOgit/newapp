(() => {
  const form = document.getElementById('time-form');
  const message = document.getElementById('time-message');
  const url = '/api/monitoring/time-sync';
  const data = () => ({server: form.elements.server.value.trim(), port: Number(form.elements.port.value),
    enabled: form.elements.enabled.checked, time: form.elements.time.value, timezone: form.elements.timezone.value});
  async function request(path, options) {
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail));
    return result;
  }
  function show(result) {
    document.getElementById('next-run').textContent = result.next_run ? 'Следующий запуск: ' + result.next_run : 'Расписание выключено или планировщик не запущен.';
    const container = document.getElementById('last-run');
    container.replaceChildren();
    const run = result.last_run;
    if (!run) { container.textContent = 'Запусков ещё не было.'; return; }
    const summary = document.createElement('p');
    summary.textContent = 'Начало: ' + run.started_at + (run.finished_at ? ' · Завершено: ' + run.finished_at : ' · Запуск не завершён') + (run.error ? ' · Ошибка: ' + run.error : '');
    container.append(summary);
    for (const device of run.devices) {
      const line = document.createElement('p');
      line.textContent = device.name + ' — ' + (device.ok ? 'Успешно' : 'Ошибка: ' + device.error) + ' · ' + device.at;
      container.append(line);
    }
  }
  async function submit(test) {
    if (!form.reportValidity()) return;
    const buttons = form.querySelectorAll('button');
    buttons.forEach(b => b.disabled = true);
    message.textContent = test ? 'Проверка NTP…' : 'Сохранение…';
    try {
      const result = await request(url + (test ? '/test' : ''), {method: test ? 'POST' : 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data())});
      if (!test) show(result);
      message.textContent = test ? 'Время NTP: ' + result.time : 'Настройки сохранены.';
    } catch (e) { message.textContent = e.message; }
    finally { buttons.forEach(b => b.disabled = false); }
  }
  form.addEventListener('submit', e => { e.preventDefault(); submit(false); });
  document.getElementById('test-ntp').addEventListener('click', () => submit(true));
  request(url).then(result => {
    for (const [key, value] of Object.entries(result.settings)) {
      if (key === 'enabled') form.elements[key].checked = value;
      else form.elements[key].value = value;
    }
    show(result);
    form.querySelectorAll('button').forEach(b => b.disabled = false);
  }).catch(e => { message.textContent = 'Не удалось загрузить настройки: ' + e.message; });
})();
