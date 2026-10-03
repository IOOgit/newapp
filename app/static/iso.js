// Виртуальная стойка: выбор регистратора и чтение уже собранной сводки.
// Действия со стойкой не отправляют команды оборудованию.
(() => {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const rack = document.getElementById('side-rack');
  const count = document.getElementById('nav-wl-count');
  if (!rack && !count) return;
  const byId = id => document.getElementById(id);
  const svg = byId('rack-svg');
  const refreshButton = byId('rack-refresh');
  const PAGE_SIZE = 8;
  const labels = {red:'Недоступен', yellow:'Есть проблемы', green:'В норме', gray:'Мониторинг выключен', unknown:'Нет полной сводки'};
  const order = {red:0, yellow:1, unknown:2, gray:3, green:4};
  let rows = [], selectedId = null, page = 0, loading = false, scanTimer;

  function shape(name, attrs = {}, text) {
    const node = document.createElementNS(NS, name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = text;
    return node;
  }
  const points = values => values.map(point => point.join(',')).join(' ');
  const polygon = (values, className) => shape('polygon', {points:points(values), class:className});
  function put(id, value) { const node = byId(id); if (node) node.textContent = value; }
  function date(value) {
    if (typeof value !== 'string' || !value) return '—';
    const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : value + 'Z';
    const parsed = new Date(normalized);
    return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString('ru-RU', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'});
  }
  async function get(path) {
    const response = await fetch(path, {cache:'no-store', signal:AbortSignal.timeout(12000)});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    return response.json();
  }
  function health(device, issues, known) {
    if (device.enabled === false) return 'gray';
    if (device.enabled !== true) return 'unknown';
    if (device.reachable === false) return 'red';
    if (device.reachable !== true || !known) return 'unknown';
    return issues.length ? 'yellow' : 'green';
  }
  function worklistCount(value) {
    if (!count) return;
    const known = Array.isArray(value?.issues);
    if (known) {
      const total = value.issues.filter(issue => issue && typeof issue === 'object').length;
      count.textContent = total ? String(total) : '';
      count.dataset.known = 'true';
      count.removeAttribute('title');
      count.setAttribute('aria-label', 'Проблем: ' + total);
    } else {
      if (!count.dataset.known || !count.textContent) count.textContent = '—';
      const message = count.dataset.known ? 'Предыдущий счётчик; список проблем не обновился.' : 'Список проблем сейчас недоступен.';
      count.title = message;
      count.setAttribute('aria-label', message);
    }
  }

  function detail() {
    const row = rows.find(item => item.device.id === selectedId);
    if (!row) return;
    const device = row.device;
    const box = byId('rack-detail');
    if (!box) return;
    box.hidden = false;
    box.dataset.state = row.state;
    put('rack-device-name', device.name || 'Регистратор ' + device.id);
    put('rack-device-status', labels[row.state]);
    put('rack-device-host', device.host ? device.host + (device.http_port ? ':' + device.http_port : '') : '—');
    put('rack-device-model', device.model || 'Не определена');
    put('rack-device-seen', date(device.last_seen));
    put('rack-device-issues', row.known ? String(row.issues.length) : '—');
    const link = byId('rack-device-link');
    if (link) link.href = '/devices/' + device.id;
    const list = byId('rack-device-notes');
    if (list) {
      list.replaceChildren();
      const messages = row.issues.slice(0, 2).map(issue => issue.title || 'Требуется проверка');
      if (!messages.length) messages.push(!row.known ? 'Список проблем сейчас недоступен.' :
        row.state === 'gray' ? 'Регистратор исключён из регулярного опроса.' :
        row.state === 'red' ? 'Нет связи с регистратором. Откройте карточку для диагностики.' :
        row.state === 'unknown' ? 'Дождитесь результатов опроса регистратора.' : 'В текущей сводке проблем нет.');
      if (row.issues.length > 2) messages.push('Ещё замечаний: ' + (row.issues.length - 2));
      for (const message of messages) {
        const item = document.createElement('li');
        item.textContent = message;
        list.append(item);
      }
    }
    svg?.querySelectorAll('.rack-unit').forEach(unit => {
      const selected = Number(unit.dataset.deviceId) === selectedId;
      unit.setAttribute('aria-pressed', String(selected));
      unit.setAttribute('tabindex', selected ? '0' : '-1');
      unit.classList.toggle('is-selected', selected);
    });
  }

  function select(id, focus = false) {
    const index = rows.findIndex(row => row.device.id === id);
    if (index < 0) return;
    selectedId = id;
    const nextPage = Math.floor(index / PAGE_SIZE);
    if (nextPage !== page) { page = nextPage; draw(); }
    detail();
    if (focus) svg?.querySelector('[data-device-id="' + id + '"]')?.focus();
  }

  function draw() {
    if (!svg || !rack) return;
    const focused = svg.contains(document.activeElement) ? document.activeElement?.dataset.deviceId : null;
    const visible = rows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
    const slots = Math.max(4, visible.length);
    const L = 38, R = 266, DX = 28, DY = -16, T = 48, H = 34;
    const bottom = T + slots * H + 8;
    svg.setAttribute('viewBox', '0 0 340 ' + (bottom + 35));
    svg.style.setProperty('--rack-scan-distance', (slots * H) + 'px');
    svg.replaceChildren();
    svg.append(shape('ellipse', {cx:174, cy:bottom + 20, rx:133, ry:10, class:'rack-floor-shadow'}));
    svg.append(polygon([[R,T],[R+DX,T+DY],[R+DX,bottom+DY],[R,bottom]], 'rack-chassis-side'));
    svg.append(polygon([[L,T],[R,T],[R+DX,T+DY],[L+DX,T+DY]], 'rack-chassis-top'));
    svg.append(shape('rect', {x:L, y:T, width:R-L, height:bottom-T, rx:2, class:'rack-chassis-front'}));
    for (const x of [L + 4, R - 8]) {
      for (let y = T + 11; y < bottom - 4; y += 17) svg.append(shape('rect', {x, y, width:4, height:5, rx:.5, class:'rack-rail-hole'}));
    }
    for (const x of [L + 10, R - 26]) svg.append(shape('rect', {x, y:bottom, width:19, height:8, rx:1, class:'rack-foot'}));
    for (let i = 0; i < slots; i++) {
      const y = T + 4 + i * H;
      svg.append(shape('rect', {x:L+13, y, width:R-L-26, height:H-3, rx:1, class:'rack-bay-well'}));
      const row = visible[i];
      if (!row) {
        const blank = shape('g', {'aria-hidden':'true'});
        blank.append(shape('rect', {x:L+16,y:y+3,width:R-L-32,height:H-9,rx:1,class:'rack-blank'}));
        for (let x=L+37;x<R-33;x+=9) blank.append(shape('path',{d:'M'+x+' '+(y+10)+'v10',class:'rack-blank-vent'}));
        svg.append(blank);
        continue;
      }
      const name = row.device.name || 'Регистратор ' + row.device.id;
      const unit = shape('g', {class:'rack-unit rack-state-' + row.state, role:'button', tabindex:'-1',
        'data-device-id':row.device.id, 'aria-pressed':'false', 'aria-label':name + ': ' + labels[row.state]});
      unit.style.setProperty('--rack-delay', Math.min(i * 35, 245) + 'ms');
      unit.append(shape('title', {}, name + ' — ' + labels[row.state]));
      unit.append(shape('rect', {x:L-5, y:y-2, width:R-L+20, height:H, class:'rack-hit'}));
      const tray = shape('g', {class:'rack-tray'});
      const x1 = L+13, x2 = R-12, y1 = y+3, y2 = y+H-3;
      tray.append(polygon([[x1,y1],[x2,y1],[x2+13,y1-7],[x1+13,y1-7]],'rack-unit-top'));
      tray.append(polygon([[x2,y1],[x2+13,y1-7],[x2+13,y2-7],[x2,y2]],'rack-unit-side'));
      tray.append(shape('rect', {x:x1,y:y1,width:x2-x1,height:y2-y1,rx:1.5,class:'rack-unit-front'}));
      tray.append(shape('path', {d:'M'+(x1+3)+' '+(y1+2)+'H'+(x2-3),class:'rack-unit-shine'}));
      for (const x of [x1+5,x2-5]) tray.append(shape('circle',{cx:x,cy:y1+13,r:1.5,class:'rack-screw'}));
      tray.append(shape('text', {x:x1+13,y:y1+17,class:'rack-unit-number'},String(page * PAGE_SIZE + i + 1).padStart(2,'0')));
      const clipId = 'rack-label-' + row.device.id;
      const clip = shape('clipPath', {id:clipId});
      clip.append(shape('rect', {x:x1+36,y:y1,width:x2-x1-80,height:y2-y1}));
      tray.append(clip);
      const shortName = Array.from(name);
      tray.append(shape('text', {x:x1+36,y:y1+17,class:'rack-unit-name','clip-path':'url(#'+clipId+')'},shortName.length > 19 ? shortName.slice(0,18).join('')+'…' : name));
      for(let v=0;v<3;v++) tray.append(shape('path',{d:'M'+(x2-38)+' '+(y1+8+v*5)+'h12',class:'rack-unit-vent'}));
      tray.append(shape('circle',{cx:x2-16,cy:y1+13,r:2.7,class:'rack-unit-led'}));
      unit.append(tray);
      unit.addEventListener('click', () => select(row.device.id));
      unit.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(row.device.id); return; }
        const index = rows.findIndex(item => item.device.id === row.device.id);
        const next = event.key === 'ArrowDown' ? Math.min(rows.length-1,index+1) :
          event.key === 'ArrowUp' ? Math.max(0,index-1) : event.key === 'Home' ? 0 : event.key === 'End' ? rows.length-1 : null;
        if (next !== null) { event.preventDefault(); select(rows[next].device.id, true); }
      });
      svg.append(unit);
    }
    svg.append(shape('rect', {x:L+1,y:T-3,width:R-L-2,height:5,rx:1,class:'rack-cap-strip'}));
    svg.append(shape('rect', {x:L+11,y:T,width:R-L-22,height:22,class:'rack-scan','aria-hidden':'true'}));
    put('rack-page-label', (page * PAGE_SIZE + 1) + '–' + Math.min((page+1)*PAGE_SIZE, rows.length) + ' из ' + rows.length);
    const previous = byId('rack-prev'), next = byId('rack-next');
    if (previous) previous.disabled = page === 0;
    if (next) next.disabled = (page+1)*PAGE_SIZE >= rows.length;
    const pages = byId('rack-pagination');
    if (pages) pages.hidden = rows.length <= PAGE_SIZE;
    detail();
    if (focused) svg.querySelector('[data-device-id="' + focused + '"]')?.focus();
  }

  function legend() {
    const box = byId('rack-legend');
    if (!box) return;
    box.replaceChildren();
    for (const [key,label] of [['green','В норме'],['yellow','С проблемами'],['red','Недоступны'],['gray','Выключены'],['unknown','Нет сводки']]) {
      const total = rows.filter(row => row.state === key).length;
      if (!total && (key === 'gray' || key === 'unknown')) continue;
      const item = document.createElement('div');
      item.className = 'rack-stat rack-state-' + key;
      const dot = document.createElement('i'); dot.setAttribute('aria-hidden','true');
      const value = document.createElement('strong'); value.textContent = total;
      const text = document.createElement('span'); text.textContent = label;
      item.append(dot,value,text); box.append(item);
    }
  }

  async function refresh() {
    if (loading) return;
    loading = true;
    if (refreshButton) refreshButton.disabled = true;
    if (rack) {
      rack.setAttribute('aria-busy','true');
      rack.classList.add('is-refreshing');
      rack.classList.remove('is-scanning');
      void rack.offsetWidth;
      rack.classList.add('is-scanning');
      clearTimeout(scanTimer);
      scanTimer = setTimeout(() => rack.classList.remove('is-scanning'), 1300);
    }
    try {
      const [devicesResult, worklistResult] = await Promise.allSettled([
        rack && svg ? get('/api/devices') : Promise.resolve(null),
        get('/api/worklist').then(value => { worklistCount(value); return value; }, error => { worklistCount(null); throw error; })
      ]);
      const known = worklistResult.status === 'fulfilled' && Array.isArray(worklistResult.value?.issues);
      const issues = known ? worklistResult.value.issues.filter(issue => issue && typeof issue === 'object') : [];
      if (!rack || !svg) return;
      if (devicesResult.status !== 'fulfilled' || !Array.isArray(devicesResult.value)) {
        rack.hidden = false;
        rack.classList.add('rack-is-stale');
        put('rack-update-note', rows.length ? 'Сводка не обновилась. Показаны предыдущие данные.' : 'Не удалось загрузить стойку. Попробуйте обновить сводку.');
        return;
      }
      const devices = devicesResult.value.filter(d => d && Number.isSafeInteger(d.id) && d.id > 0);
      rows = devices.map(device => {
        const deviceIssues = issues.filter(issue => issue.device_id === device.id);
        return {device, issues:deviceIssues, known, state:health(device, deviceIssues, known)};
      }).sort((a,b) => order[a.state]-order[b.state] || a.device.id-b.device.id);
      if (!rows.length) { selectedId = null; page = 0; rack.hidden = true; return; }
      if (!rows.some(row => row.device.id === selectedId)) selectedId = rows[0].device.id;
      page = Math.floor(rows.findIndex(row => row.device.id === selectedId) / PAGE_SIZE);
      rack.hidden = false;
      rack.classList.remove('rack-is-stale');
      put('rack-cap', 'Парк · ' + rows.length + ' NVR');
      put('rack-update-note', known ? 'Сводка загружена · ' + new Date().toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'}) : 'Состояние связи получено. Список проблем сейчас недоступен.');
      legend(); draw();
    } finally {
      loading = false;
      if (refreshButton) refreshButton.disabled = false;
      if (rack) { rack.removeAttribute('aria-busy'); rack.classList.remove('is-refreshing'); }
    }
  }
  if (refreshButton) refreshButton.addEventListener('click', refresh);
  byId('rack-prev')?.addEventListener('click', () => select(rows[(page-1)*PAGE_SIZE]?.device.id, true));
  byId('rack-next')?.addEventListener('click', () => select(rows[(page+1)*PAGE_SIZE]?.device.id, true));
  refresh();
})();
