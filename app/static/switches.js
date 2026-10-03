/* Общие формы и детали коммутаторов. Значения агента вставляются только как текст. */
(() => {
  'use strict';
  const dataNode = document.getElementById('sw-data');
  const sw = dataNode ? JSON.parse(dataNode.textContent) : null;
  let busy = 0, returnFocus = null;
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const show = value => value === null || value === undefined || value === '' ? '—' : String(value);
  function rate(value) {
    if (value === null || value === undefined) return '—';
    for (const unit of ['bit/s', 'Kbit/s', 'Mbit/s', 'Gbit/s']) {
      if (Math.abs(value) < 1000 || unit === 'Gbit/s') return value.toFixed(1) + ' ' + unit;
      value /= 1000;
    }
  }
  async function call(path, method = 'GET', payload) {
    const response = await fetch(path, {method, cache: 'no-store', signal: AbortSignal.timeout(15000),
      headers: payload ? {'Content-Type': 'application/json'} : {}, body: payload ? JSON.stringify(payload) : undefined});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(Array.isArray(result.detail) ? result.detail.map(x => x.msg).join('; ') : result.detail || 'Ошибка операции');
    return result;
  }
  async function waitJob(id) {
    const deadline = Date.now() + 240000;
    while (Date.now() < deadline) {
      await delay(1000);
      const job = await call('/api/switches/jobs/' + encodeURIComponent(id));
      if (job.state === 'done') return job.result;
      if (job.state !== 'running') throw new Error('Проверка прервана');
    }
    throw new Error('Проверка ещё выполняется. Обновите состояние позже.');
  }
  function openModal(modal) {
    returnFocus = document.activeElement;
    modal.classList.remove('hidden');
    modal.querySelector('input, button, select')?.focus();
  }
  function closeModal(modal) {
    modal.classList.add('hidden');
    const secret = modal.querySelector('[name="community"]');
    if (secret) secret.value = '';
    returnFocus?.focus();
  }
  document.querySelectorAll('[data-open]').forEach(button => button.onclick = () => openModal(document.getElementById(button.dataset.open)));
  document.querySelectorAll('[data-close]').forEach(button => button.onclick = () => closeModal(button.closest('.modal')));
  document.addEventListener('keydown', event => {
    const modal = document.querySelector('.modal:not(.hidden)');
    if (!modal) return;
    if (event.key === 'Escape') closeModal(modal);
    if (event.key === 'Tab') {
      const fields = [...modal.querySelectorAll('input, select, button')].filter(el => !el.disabled && el.offsetParent !== null);
      if (!fields.length) return;
      if (event.shiftKey && document.activeElement === fields[0]) { fields.at(-1).focus(); event.preventDefault(); }
      else if (!event.shiftKey && document.activeElement === fields.at(-1)) { fields[0].focus(); event.preventDefault(); }
    }
  });
  const form = document.querySelector('[data-switch-form]');
  if (form) {
    const id = form.dataset.switchId;
    function snmpFields() {
      const v = new FormData(form);
      const payload = {host: v.get('host').trim(), snmp_port: Number(v.get('snmp_port')),
        snmp_version: v.get('snmp_version'), timeout: Number(v.get('timeout')), retries: Number(v.get('retries'))};
      if (v.get('community')) payload.community = v.get('community');
      return payload;
    }
    form.onsubmit = async event => {
      event.preventDefault();
      const v = new FormData(form), button = form.querySelector('[type="submit"]'), out = form.querySelector('.err-text');
      const payload = {...snmpFields(), name: v.get('name').trim(), model: v.get('model').trim(),
        group_id: v.get('group_id') ? Number(v.get('group_id')) : null,
        management_port: Number(v.get('management_port')), enabled: v.has('enabled'), snmp_enabled: v.has('snmp_enabled')};
      button.disabled = true; busy++;
      try {
        const result = await call('/api/switches' + (id ? '/' + id : ''), id ? 'PUT' : 'POST', payload);
        form.elements.community.value = '';
        location.href = '/switches/' + result.id + location.hash;
      } catch (error) { out.textContent = error.message; }
      finally { button.disabled = false; busy--; }
    };
    const probeButton = form.querySelector('[data-snmp-probe]');
    probeButton.onclick = async () => {
      if (!['host', 'snmp_port', 'timeout', 'retries'].every(name => form.elements[name].reportValidity())) return;
      const out = form.querySelector('[data-probe-result]');
      probeButton.disabled = true; busy++; out.textContent = 'Проверка SNMP…';
      try {
        const payload = snmpFields();
        if (id) payload.switch_id = Number(id);
        const job = await call('/api/switches/probe', 'POST', payload);
        const result = await waitJob(job.job_id);
        if (result.snmp_status !== 'available') out.textContent = result.error || 'SNMP недоступен';
        else out.textContent = ['SNMP доступен', 'Производитель: ' + show(result.vendor), 'Модель: ' + show(result.detected_model),
          'sysName: ' + show(result.sys_name), 'sysDescr: ' + show(result.sys_descr), 'sysObjectID: ' + show(result.sys_object_id),
          'Время работы: ' + (result.uptime_ticks == null ? '—' : Math.floor(result.uptime_ticks / 100) + ' с')].join('\n');
      } catch (error) { out.textContent = error.message; }
      finally { probeButton.disabled = false; busy--; }
    };
  }
  const actionOut = document.getElementById('sw-action-result');
  const pollButton = document.querySelector('[data-switch-poll]');
  if (pollButton) pollButton.onclick = async () => {
    pollButton.disabled = true; busy++; actionOut.textContent = 'Фоновый опрос…';
    try {
      const job = await call('/api/switches/' + sw.id + '/poll', 'POST');
      await waitJob(job.job_id); location.reload();
    } catch (error) { actionOut.textContent = error.message; }
    finally { pollButton.disabled = false; busy--; }
  };
  const deleteButton = document.querySelector('[data-switch-delete]');
  if (deleteButton) deleteButton.onclick = async () => {
    if (!confirm('Удалить коммутатор из мониторинга?')) return;
    try { await call('/api/switches/' + sw.id, 'DELETE'); location.href = '/switches'; }
    catch (error) { actionOut.textContent = error.message; }
  };
  const tabs = [...document.querySelectorAll('[data-sw-tab]')];
  function selectTab(key) {
    if (!tabs.some(tab => tab.dataset.swTab === key)) key = 'overview';
    tabs.forEach(tab => { const selected = tab.dataset.swTab === key; tab.setAttribute('aria-selected', selected); tab.tabIndex = selected ? 0 : -1; });
    document.querySelectorAll('.sw-panel').forEach(panel => { panel.hidden = panel.id !== 'sw-' + key; });
  }
  tabs.forEach((tab, index) => {
    tab.onclick = () => { selectTab(tab.dataset.swTab); history.replaceState(null, '', '#' + tab.dataset.swTab); };
    tab.onkeydown = event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs.at(-1) : tabs[(index + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      next.focus(); next.click();
    };
  });
  if (tabs.length) selectTab(location.hash.slice(1));
  let selectedPort = null;
  const portForm = document.getElementById('sw-port-form');
  document.querySelectorAll('[data-port]').forEach(button => button.onclick = () => {
    const p = selectedPort = sw.ports.find(port => port.id === Number(button.dataset.port));
    const fresh = sw.telemetry_available && p.present;
    document.getElementById('sw-port-title').textContent = p.name || 'ifIndex ' + p.if_index;
    const entries = [['Состояние', p.health.label], ['ifIndex', p.if_index], ['Admin', fresh ? ({1:'Включён',2:'Выключен',3:'Тест'}[p.admin_status]) : null],
      ['Скорость', fresh && p.speed_mbps != null ? p.speed_mbps + ' Mbit/s' : null], ['RX', rate(p.in_bps)], ['TX', rate(p.out_bps)],
      ['Ошибки RX / TX', show(p.in_error_delta) + ' / ' + show(p.out_error_delta)], ['Потери RX / TX', show(p.in_discard_delta) + ' / ' + show(p.out_discard_delta)],
      ['MAC', p.mac_address], ['Alias', p.alias], ['Описание', p.description],
      ['Изменение линка', fresh && p.last_change_ticks != null && sw.uptime_ticks != null ? Math.max(0, Math.floor((sw.uptime_ticks - p.last_change_ticks) / 100)) + ' с назад' : null],
      ['PoE', {1:'Выключено',2:'Поиск нагрузки',3:'Подаётся',4:'Ошибка',5:'Тест',6:'Неисправность'}[p.poe.status]],
      ['Мощность PoE', p.poe.power_w != null ? p.poe.power_w + ' W' : null],
      ['Ошибки PoE (накопленные)', p.poe.errors && Object.values(p.poe.errors).every(v => v != null) ? Object.values(p.poe.errors).reduce((a,b) => a+b,0) : null]];
    const details = document.getElementById('sw-port-details'); details.replaceChildren();
    for (const [label, value] of entries) { const dt = document.createElement('dt'), dd = document.createElement('dd'); dt.textContent = label; dd.textContent = show(value); details.append(dt, dd); }
    portForm.elements.expected_up.checked = p.expected_up;
    portForm.elements.channel_ref_id.value = p.channel_ref_id || '';
    const poeSelect = portForm.elements.poe_index;
    if (p.poe_index && ![...poeSelect.options].some(option => option.value === p.poe_index)) {
      poeSelect.add(new Option(p.poe_index + ' · сохранённая привязка', p.poe_index));
    }
    poeSelect.value = p.poe_index || '';
    portForm.querySelector('.err-text').textContent = '';
    openModal(document.getElementById('sw-port-modal'));
  });
  if (portForm) portForm.onsubmit = async event => {
    event.preventDefault(); const v = new FormData(portForm);
    const payload = {expected_up: v.has('expected_up'), channel_ref_id: v.get('channel_ref_id') ? Number(v.get('channel_ref_id')) : null,
      poe_index: v.get('poe_index') || null};
    try { await call('/api/switches/' + sw.id + '/ports/' + selectedPort.id, 'PATCH', payload); location.reload(); }
    catch (error) { portForm.querySelector('.err-text').textContent = error.message; }
  };
  const chart = document.getElementById('sw-history-chart');
  if (chart) {
    const samples = JSON.parse(document.getElementById('sw-history-data').textContent);
    const end = Date.now(), start = end - 86400000;
    const maximum = Math.max(1, ...samples.flatMap(s => [s.in_bps || 0, s.out_bps || 0]));
    for (const [key, className] of [['in_bps','sw-rx'], ['out_bps','sw-tx']]) {
      let points = [], previous = null;
      function draw() { if (points.length > 1) { const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline'); line.setAttribute('points', points.join(' ')); line.setAttribute('class', className); chart.append(line); } points = []; }
      for (const sample of samples) {
        const time = Date.parse(sample.created_at);
        if (sample[key] == null || (previous && time - previous > 180000)) draw();
        if (sample[key] != null) points.push(((time - start) / 86400000 * 800) + ',' + (76 - sample[key] / maximum * 72));
        previous = time;
      }
      draw();
    }
  }
  setInterval(() => { if (!busy && !document.hidden && !document.querySelector('.modal:not(.hidden)')) location.reload(); }, 30000);
})();
