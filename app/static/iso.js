// Изометрическая «стойка NVR» в сайдбаре: срез здоровья парка.
// Строится из /api/devices + /api/worklist (худшие сверху); туда же — счётчик
// проблем у пункта «Доска проблем». Чертёжная изометрия, без анимации.
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const rackBox = document.getElementById("side-rack");
  const countEl = document.getElementById("nav-wl-count");
  if (!rackBox && !countEl) return;

  const el = (name, attrs) => {
    const n = document.createElementNS(NS, name);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  };
  const P = (pts) => pts.map((p) => p.join(",")).join(" ");

  async function build() {
    let devices = [], issues = [];
    try {
      const [dr, wr] = await Promise.all([fetch("/api/devices"), fetch("/api/worklist")]);
      if (!dr.ok) return;
      devices = await dr.json();
      if (wr.ok) issues = (await wr.json()).issues || [];
    } catch (e) { return; }

    if (countEl) countEl.textContent = issues.length ? String(issues.length) : "";
    if (!rackBox || !devices.length) return;

    const problem = new Set(issues.map((i) => i.device_id));
    const health = (d) => !d.enabled ? "gray" : !d.reachable ? "red" : problem.has(d.id) ? "yellow" : "green";
    const order = { red: 0, yellow: 1, gray: 2, green: 3 };
    const rows = devices.map((d) => ({ name: d.name, h: health(d) }))
      .sort((a, b) => order[a.h] - order[b.h]);

    // геометрия из дизайн-прототипа
    const L = 14, Rt = 96, DX = 20, DY = -12, top0 = 22, sh = 8.6;
    const n = rows.length, botY = top0 + n * sh;
    const svg = document.getElementById("rack-svg");
    svg.setAttribute("viewBox", `0 0 ${Rt + DX + 6} ${botY + 6}`);
    svg.innerHTML = "";

    const cap = el("polygon", { points: P([[L, top0], [Rt, top0], [Rt + DX, top0 + DY], [L + DX, top0 + DY]]) });
    cap.style.cssText = "fill:var(--color-neutral-200);stroke:var(--color-neutral-400);stroke-width:1";
    const side = el("polygon", { points: P([[Rt, top0], [Rt + DX, top0 + DY], [Rt + DX, botY + DY], [Rt, botY]]) });
    side.style.cssText = cap.style.cssText;
    svg.append(cap, side);

    const stTxt = { red: "недоступен", yellow: "есть проблемы", gray: "выключен", green: "норма" };
    for (let i = 0; i < n; i++) {
      const r = rows[i], y = top0 + i * sh, y2 = y + sh - 1.4;
      const fill = r.h === "red" ? "var(--color-accent)"
        : r.h === "yellow" ? "var(--color-accent-200)"
        : r.h === "gray" ? "var(--color-neutral-200)" : "var(--color-neutral-100)";
      const stroke = (r.h === "green" || r.h === "gray") ? "var(--color-neutral-400)" : "var(--color-accent)";
      const u = el("polygon", { points: P([[L, y], [Rt, y], [Rt, y2], [L, y2]]) });
      u.style.cssText = `fill:${fill};stroke:${stroke};stroke-width:1`;
      const t = el("title", {});
      t.textContent = `${r.name} — ${stTxt[r.h]}`;
      u.appendChild(t);
      const led = el("circle", { cx: L + 5, cy: (y + y2) / 2, r: 1.1 });
      led.style.cssText = `fill:${r.h === "green" || r.h === "gray" ? "var(--color-neutral-400)" : "var(--color-accent)"};stroke:none`;
      svg.append(u, led);
    }
    const frame = el("polygon", { points: P([[L, top0], [Rt, top0], [Rt, botY], [L, botY]]) });
    frame.style.cssText = "fill:none;stroke:var(--color-text);stroke-width:1.5";
    svg.append(frame);

    const counts = { green: 0, yellow: 0, red: 0 };
    rows.forEach((r) => { if (r.h in counts) counts[r.h]++; });
    const legend = document.getElementById("rack-legend");
    legend.innerHTML = "";
    const sw = {
      green: "fill:var(--color-neutral-100);stroke:var(--color-neutral-400);stroke-width:1.5",
      yellow: "fill:var(--color-accent-200);stroke:var(--color-accent);stroke-width:1.5",
      red: "fill:var(--color-accent);stroke:var(--color-accent);stroke-width:1.5",
    };
    for (const [k, label] of [["green", "норма"], ["yellow", "проблемы"], ["red", "недоступны"]]) {
      const row = document.createElement("span");
      const ico = el("svg", { width: 11, height: 11 });
      ico.style.flex = "none";
      const rect = el("rect", { x: 0.75, y: 0.75, width: 9.5, height: 9.5 });
      rect.style.cssText = sw[k];
      ico.appendChild(rect);
      row.appendChild(ico);
      row.appendChild(document.createTextNode(`${label} ${counts[k]}`));
      legend.appendChild(row);
    }
    document.getElementById("rack-cap").textContent = `Парк · ${devices.length} NVR`;
    rackBox.hidden = false;
  }
  build();
})();
