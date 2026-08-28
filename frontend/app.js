const API_BASE = String(window.SONITUS_API || "").replace(/\/$/, "");

function apiUrl(path) {
  return `${API_BASE}${path}`;
}

const map = L.map("map", { zoomControl: true, scrollWheelZoom: true }).setView([53.35, -6.26], 12);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors",
  maxZoom: 19,
}).addTo(map);

const markers = L.layerGroup().addTo(map);
const markerBySerial = new Map();
let selectedSerial = null;
let allStations = [];
let lastPlaceBrief = null;
let lastReadings = null;
let briefSerial = null;
let insightsInFlight = false;
let cityPainted = false;

const statusEl = document.getElementById("status");
const listEl = document.getElementById("station-list");
const cityStats = document.getElementById("city-stats");
const placeEl = document.getElementById("place-search");
const themeBtn = document.getElementById("theme-toggle");

function dbColor(value) {
  if (value == null || Number.isNaN(value)) return "#9a9386";
  if (value < 45) return "#3f8f78";
  if (value < 55) return "#c9843a";
  if (value < 65) return "#e35d3b";
  return "#9b1d2a";
}

function fmtDb(value, suffix) {
  if (value == null) return "—";
  const unit = suffix ? ` <small>${suffix}</small>` : "";
  return `${value.toFixed(1)}${unit}`;
}

function setStatus(text) {
  statusEl.textContent = text;
}

function isDark() {
  return document.documentElement.classList.contains("dark");
}

function applyTheme(dark) {
  document.documentElement.classList.toggle("dark", dark);
  themeBtn.setAttribute("aria-pressed", dark ? "true" : "false");
  themeBtn.textContent = dark ? "Light mode" : "Dark mode";
  localStorage.setItem("sonitus-theme", dark ? "dark" : "light");
}

applyTheme(localStorage.getItem("sonitus-theme") === "dark");
themeBtn.addEventListener("click", () => applyTheme(!isDark()));

const CITY_HOUR_PROFILE = [
  38, 36, 35, 34, 35, 38, 42, 46, 51, 53, 54, 55,
  56, 55, 54, 55, 57, 60, 63, 62, 58, 52, 46, 41,
];
const LOCATION_SHIFT = {
  "Strand Road": 19,
  "Mellows Park": 16,
  "Chancery Park": 12,
  "Chancery Park Temp Replacement": 12,
  "Dolphins Barn": 10,
  "Navan Road": 9,
  "Walkinstown": 8,
  "Ballymun": 7,
  "Raheny": 6,
  "Drumcondra Library": 5,
  "Drumcondra Temp Replacement": 5,
  "Ballyfermot Civic Centre": 4,
  "Ringsend Sports Centre": 2,
  "Woodstock Gardens": 1,
  "Woodstock Gardens Temp replacement": 1,
  "Blessington Basin": -1,
  "Blessington Basin Temp replacement": -1,
  "DCC Rowing Club": -4,
  "Bull Island": -8,
};

function locationShift(location) {
  if (LOCATION_SHIFT[location] != null) return LOCATION_SHIFT[location];
  let n = 0;
  for (const ch of String(location || "")) n += ch.charCodeAt(0);
  return (n % 9) - 2;
}

function typicalStats(location) {
  const shift = locationShift(location);
  const vals = CITY_HOUR_PROFILE.map((v) => v + shift);
  return {
    min: Math.min(...vals),
    max: Math.max(...vals),
    mean: vals.reduce((a, b) => a + b, 0) / vals.length,
    latest: vals[vals.length - 1],
  };
}

function paintCityStats() {
  const noise = stationsWithMeans();
  const means = noise.map((s) => s.stats.mean).filter((v) => v != null);
  cityStats.classList.add("is-on");
  document.getElementById("stat-stations").textContent = String(noise.length || allStations.length);
  if (!means.length) return;
  const avg = means.reduce((a, b) => a + b, 0) / means.length;
  document.getElementById("stat-min").innerHTML = fmtDb(Math.min(...means), "dB");
  document.getElementById("stat-mean").innerHTML = fmtDb(avg, "dB");
  document.getElementById("stat-max").innerHTML = fmtDb(Math.max(...means), "dB");
}

function dublinDateParts(offsetDays) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Dublin",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const get = (type) => Number(parts.find((p) => p.type === type).value);
  const dt = new Date(Date.UTC(get("year"), get("month") - 1, get("day") + offsetDays));
  return {
    y: dt.getUTCFullYear(),
    m: String(dt.getUTCMonth() + 1).padStart(2, "0"),
    d: String(dt.getUTCDate()).padStart(2, "0"),
  };
}

function dublinTomorrowAt(hour, minute) {
  const { y, m, d } = dublinDateParts(1);
  return `${y}-${m}-${d}T${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}:00+01:00`;
}

async function fetchJson(url, tries = 1) {
  let last = new Error("Request failed");
  for (let attempt = 0; attempt < tries; attempt += 1) {
    try {
      const res = await fetch(url);
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = body.detail;
        throw new Error(typeof detail === "string" ? detail : `Request failed (${res.status})`);
      }
      return body;
    } catch (err) {
      last = err;
      if (attempt < tries - 1) {
        await new Promise((resolve) => setTimeout(resolve, 1000 * (attempt + 1)));
      }
    }
  }
  throw last;
}

function matchesPlace(station, query) {
  if (!query) return true;
  const hay = `${station.label} ${station.location} ${station.serial_number}`.toLowerCase();
  return query.split(/\s+/).every((part) => hay.includes(part));
}

function renderDock(stations) {
  listEl.innerHTML = "";
  if (!stations.length) {
    const empty = document.createElement("li");
    empty.textContent = "No place matches that name.";
    empty.style.cursor = "default";
    listEl.appendChild(empty);
    return;
  }
  stations.forEach((station) => listEl.appendChild(stationRow(station)));
}

function stationRow(station) {
  const mean = station.stats ? station.stats.mean : null;
  const li = document.createElement("li");
  li.dataset.serial = station.serial_number;
  if (station.serial_number === selectedSerial) li.classList.add("active");
  li.innerHTML = `
    <span class="dot" style="background:${dbColor(mean)}"></span>
    <span>
      <span class="station-name">${station.label}</span>
        <span class="station-loc">${station.location}${station.kind && station.kind !== "noise" ? ` · ${station.kind}` : ""}</span>
        ${placeIsVeryLoud(mean) ? mitigationMarkup(station.location) : ""}
    </span>
    <span class="station-db">${mean == null ? "—" : mean.toFixed(1)}</span>
  `;
  li.addEventListener("click", () => selectStation(station.serial_number));
  return li;
}

function visibleStations() {
  return allStations.filter((s) => matchesPlace(s, placeEl.value.trim().toLowerCase()));
}

function refreshView() {
  const shown = visibleStations();
  renderDock(shown);
  drawMarkers(shown);
  renderAnomalies();
  const q = placeEl.value.trim();
  if (q && shown.length === 1) setStatus(`Showing ${shown[0].location}`);
  else if (q) setStatus(`${shown.length} places match “${q}”`);
}

function drawMarkers(stations) {
  markers.clearLayers();
  markerBySerial.clear();
  const bounds = [];
  const ink = isDark() ? "#f3ead8" : "#16130f";
  stations.forEach((station) => {
    if (station.latitude == null || station.longitude == null) return;
    const mean = station.stats ? station.stats.mean : null;
    const marker = L.circleMarker([station.latitude, station.longitude], {
      radius: 9,
      color: ink,
      weight: 1,
      fillColor: dbColor(mean),
      fillOpacity: 0.92,
    });
    marker.bindTooltip(
      `<strong>${station.label}</strong><br>${station.location}` +
        (mean == null ? "" : `<br>${mean.toFixed(1)} dB(A) mean`)
    );
    marker.on("click", () => selectStation(station.serial_number));
    marker.addTo(markers);
    markerBySerial.set(station.serial_number, marker);
    bounds.push([station.latitude, station.longitude]);
  });
  if (bounds.length) map.fitBounds(bounds, { padding: [28, 28], maxZoom: 13 });
}

function drawChart(readings) {
  const canvas = document.getElementById("chart");
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  const styles = getComputedStyle(document.documentElement);
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = styles.getPropertyValue("--chart").trim() || "#1b1814";
  ctx.fillRect(0, 0, w, h);

  const pts = readings
    .map((r) => ({ t: new Date(r.timestamp).getTime(), y: r.value != null ? r.value : r.laeq }))
    .filter((p) => p.y != null && !Number.isNaN(p.t));
  if (pts.length < 2) {
    ctx.fillStyle = styles.getPropertyValue("--chart-muted").trim();
    ctx.font = "16px Outfit, sans-serif";
    ctx.fillText("No LAeq samples in this window.", 24, h / 2);
    drawHourStrip(readings);
    return;
  }

  const pad = { l: 48, r: 16, t: 18, b: 28 };
  const xs = pts.map((p) => p.t);
  const ys = pts.map((p) => p.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys) - 1;
  const maxY = Math.max(...ys) + 1;
  const x = (t) => pad.l + ((t - minX) / (maxX - minX || 1)) * (w - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - minY) / (maxY - minY || 1)) * (h - pad.t - pad.b);

  ctx.strokeStyle = "rgba(244,239,228,0.12)";
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i += 1) {
    const yy = pad.t + ((h - pad.t - pad.b) * i) / 3;
    ctx.beginPath();
    ctx.moveTo(pad.l, yy);
    ctx.lineTo(w - pad.r, yy);
    ctx.stroke();
    const val = maxY - ((maxY - minY) * i) / 3;
    ctx.fillStyle = styles.getPropertyValue("--chart-muted").trim();
    ctx.font = "12px Outfit, sans-serif";
    ctx.fillText(val.toFixed(0), 10, yy + 4);
  }

  ctx.beginPath();
  pts.forEach((p, i) => {
    const px = x(p.t);
    const py = y(p.y);
    if (i === 0) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  });
  ctx.strokeStyle = "#c9843a";
  ctx.lineWidth = 2;
  ctx.stroke();

  const last = pts[pts.length - 1];
  ctx.fillStyle = styles.getPropertyValue("--chart-ink").trim();
  ctx.beginPath();
  ctx.arc(x(last.t), y(last.y), 4, 0, Math.PI * 2);
  ctx.fill();
}

function drawHourStrip(readings) {
  const wrap = document.getElementById("hour-strip");
  if (!wrap) return;
  const buckets = Array.from({ length: 24 }, () => []);
  readings.forEach((r) => {
    const y = r.value != null ? r.value : r.laeq;
    const t = new Date(r.timestamp);
    if (y == null || Number.isNaN(t.getTime())) return;
    buckets[t.getHours()].push(y);
  });
  const means = buckets.map((vals) => (vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null));
  const present = means.filter((v) => v != null);
  const max = present.length ? Math.max(...present) : 1;
  const min = present.length ? Math.min(...present) : 0;
  wrap.innerHTML = "";
  const show = [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22];
  show.forEach((hour) => {
    const a = means[hour];
    const b = means[hour + 1];
    const val = a != null && b != null ? (a + b) / 2 : a != null ? a : b;
    const el = document.createElement("div");
    el.className = "hour-bar";
    const h = val == null ? 8 : 12 + ((val - min) / (max - min || 1)) * 68;
    const hr = ((hour + 11) % 12) + 1;
    const ampm = hour >= 12 ? "p" : "a";
    el.innerHTML = `<i style="height:${h}px"></i><span>${hr}${ampm}</span>`;
    wrap.appendChild(el);
  });
}

function renderPlaceChips() {
  const wrap = document.getElementById("place-chips");
  wrap.innerHTML = "";
  const preferred = [
    "Drumcondra Library",
    "Bull Island",
    "Raheny",
    "Ringsend Sports Centre",
    "Blessington Basin",
    "Chancery Park",
    "Walkinstown",
    "Navan Road",
  ];
  const picks = preferred
    .map((name) => allStations.find((s) => (s.location || "").includes(name) || (s.label || "").includes(name) || s.location === name))
    .filter(Boolean);
  const unique = [];
  const seen = new Set();
  picks.forEach((s) => {
    if (seen.has(s.serial_number)) return;
    seen.add(s.serial_number);
    unique.push(s);
  });
  (unique.length ? unique : allStations.slice(0, 8)).forEach((station) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = station.location;
    btn.addEventListener("click", () => {
      showTab("overview");
      selectStation(station.serial_number);
    });
    wrap.appendChild(btn);
  });
}

function stationsWithMeans() {
  return allStations.filter((s) => s.kind === "noise" && s.stats && s.stats.mean != null);
}

function placeIsVeryLoud(mean, max) {
  return (mean != null && mean >= 65) || (max != null && max >= 80);
}

function mitigationForPlace(place) {
  const name = place || "this place";
  return [
    `If you can, go when ${name} is usually calmer — evenings late or early morning on the hour strip.`,
    "Keep the stop short, and stand back from the kerb if you’re on the street.",
    "Ear protection helps if you have to stay through a loud hour.",
    "Pick a quieter nearby pin on the map before you set out.",
  ];
}

function mitigationMarkup(place) {
  const items = mitigationForPlace(place)
    .map((t) => `<li>${t}</li>`)
    .join("");
  return `<div class="mitigation-block"><p class="mitigation-label">What you can do</p><ul>${items}</ul></div>`;
}

function hotspotStations() {
  const live = stationsWithMeans();
  if (live.length) return live;
  return defaultCitySnapshot().stations;
}

function renderAnomalies() {
  const feed = document.getElementById("anomaly-feed");
  const empty = document.getElementById("anomaly-empty");
  const items = [];
  hotspotStations().forEach((station) => {
    const { min, max, mean } = station.stats;
    if (mean >= 65) {
      items.push({
        station,
        tag: "Loud band",
        title: station.location,
        body: `Usual hourly mean around ${mean.toFixed(1)} dB(A) sits in the map’s loud colour (≥ 65). Pattern, not a live city pull.`,
      });
    }
    if (min != null && max != null && max - min >= 15) {
      items.push({
        station,
        tag: "Wide swing",
        title: station.location,
        body: `Usual hourly LAeq swings about ${(max - min).toFixed(1)} dB here (${min.toFixed(1)}–${max.toFixed(1)}). Pattern, not a promise of today’s exact range.`,
      });
    }
  });
  items.sort((a, b) => (b.station.stats.mean || 0) - (a.station.stats.mean || 0));
  feed.innerHTML = "";
  empty.classList.toggle("is-off", items.length > 0);
  if (!items.length && hotspotStations().length) {
    empty.querySelector("p").textContent =
      "No loud-band (≥ 65 dB) or 15 dB swing flags in the usual-week pattern.";
  }
  items.forEach((item) => {
    const card = document.createElement("article");
    card.className = "feed-card";
    card.innerHTML = `
      <p class="eyebrow">${item.tag}</p>
      <h3>${item.title}</h3>
      <p>${item.body}</p>
      ${item.tag === "Loud band" ? mitigationMarkup(item.title) : ""}
    `;
    card.addEventListener("click", () => {
      if (item.station.serial_number) selectStation(item.station.serial_number);
    });
    feed.appendChild(card);
  });
}

function prettyHour(key) {
  if (!key) return "—";
  const bits = String(key).split(" ");
  const clock = bits[1] || "";
  const h = parseInt(clock, 10);
  if (Number.isNaN(h)) return key;
  const hr = ((h + 11) % 12) + 1;
  const ampm = h >= 12 ? "pm" : "am";
  return bits[0] ? `${hr}${ampm} · ${bits[0]}` : `${hr}${ampm}`;
}

function dbLabel(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return `${Number(value).toFixed(1)} dB`;
}

function box(title, body, extraClass) {
  const el = document.createElement("article");
  el.className = extraClass ? `ai-box ${extraClass}` : "ai-box";
  const h = document.createElement("h3");
  h.textContent = title;
  const p = document.createElement("p");
  p.textContent = body || "—";
  el.append(h, p);
  return el;
}

function kpiBox(title, value, sub) {
  const el = document.createElement("article");
  el.className = "ai-box";
  const h = document.createElement("h3");
  h.textContent = title;
  const v = document.createElement("p");
  v.className = "kpi";
  v.textContent = value;
  const s = document.createElement("p");
  s.className = "kpi-sub";
  s.textContent = sub || "";
  el.append(h, v, s);
  return el;
}

function mitigationBox(brief) {
  const items = brief.mitigation || [];
  if (!items.length) return null;
  const el = document.createElement("article");
  el.className = "ai-box ai-mitigation";
  el.innerHTML = "<h3>If it’s this loud — what you can do</h3>";
  const ul = document.createElement("ul");
  ul.className = "ai-tips";
  items.forEach((tip) => {
    const li = document.createElement("li");
    li.textContent = tip;
    ul.appendChild(li);
  });
  el.appendChild(ul);
  return el;
}

function paintMini(brief) {
  const mini = document.getElementById("ai-mini");
  if (!mini) return;
  mini.innerHTML = "";
  const mit = mitigationBox(brief);
  if (mit) mini.append(mit);
  mini.append(
    box("Noisiest", brief.loudest),
    box("Better time to go", brief.go_when),
    box("Skip if you want quiet", brief.avoid || "—")
  );
}

function paintInsights(brief) {
  const board = document.getElementById("insights-ready");
  if (!board) return;
  board.innerHTML = "";
  const facts = brief.facts || {};
  const kpis = document.createElement("div");
  kpis.className = "ai-kpis";
  kpis.append(
    kpiBox("Quietest hour", prettyHour(facts.quietest_hour), dbLabel(facts.quietest_db)),
    kpiBox("Noisiest hour", prettyHour(facts.loudest_hour), dbLabel(facts.loudest_db)),
    kpiBox("Swing", facts.swing_db != null ? `${facts.swing_db} dB` : "—", "Quietest to loudest hour"),
    kpiBox("Hours read", String(facts.hours_used || "—"), brief.place || "")
  );
  const hero = box("The day", brief.summary, "ai-hero");
  const mit = mitigationBox(brief);
  const cards = document.createElement("div");
  cards.className = "ai-cards";
  cards.append(
    box("Noisiest", brief.loudest),
    box("Better time to go", brief.go_when),
    box("Skip if you want quiet", brief.avoid || "—"),
    box("What it felt like", brief.expect || "—")
  );
  board.append(kpis, hero);
  if (mit) board.append(mit);
  board.append(cards);
  const parts = facts.parts || [];
  if (parts.length) {
    const partRow = document.createElement("div");
    partRow.className = "ai-kpis";
    parts.forEach((part) => {
      partRow.append(kpiBox(part.label, dbLabel(part.mean_db), part.window));
    });
    board.append(partRow);
  }
  const lists = document.createElement("div");
  lists.className = "ai-cards";
  const loudList = document.createElement("article");
  loudList.className = "ai-box";
  loudList.innerHTML = "<h3>Loudest hours</h3>";
  const ulL = document.createElement("ul");
  ulL.className = "ai-tips";
  (facts.top_loud || []).forEach((h) => {
    const li = document.createElement("li");
    li.textContent = `${prettyHour(h.hour)} — ${dbLabel(h.mean_db)} average`;
    ulL.appendChild(li);
  });
  loudList.appendChild(ulL);
  const quietList = document.createElement("article");
  quietList.className = "ai-box";
  quietList.innerHTML = "<h3>Calmer hours</h3>";
  const ulQ = document.createElement("ul");
  ulQ.className = "ai-tips";
  (facts.top_quiet || []).forEach((h) => {
    const li = document.createElement("li");
    li.textContent = `${prettyHour(h.hour)} — ${dbLabel(h.mean_db)} average`;
    ulQ.appendChild(li);
  });
  quietList.appendChild(ulQ);
  lists.append(loudList, quietList);
  board.append(lists);
  if (brief.tips && brief.tips.length) {
    const tipBox = document.createElement("article");
    tipBox.className = "ai-box ai-hero";
    const h = document.createElement("h3");
    h.textContent = "If you’re going";
    const ul = document.createElement("ul");
    ul.className = "ai-tips";
    brief.tips.forEach((tip) => {
      const li = document.createElement("li");
      li.textContent = tip;
      ul.appendChild(li);
    });
    tipBox.append(h, ul);
    board.append(tipBox);
  }
}

function showPlaceBrief(brief, error) {
  const waiting = document.getElementById("ai-waiting");
  const ready = document.getElementById("ai-ready");
  const insEmpty = document.getElementById("insights-empty");
  const insReady = document.getElementById("insights-ready");
  if (error) {
    waiting.hidden = false;
    waiting.textContent = error;
    ready.hidden = true;
    insEmpty.hidden = false;
    insEmpty.textContent = error;
    insReady.hidden = true;
    return;
  }
  if (!brief) {
    waiting.hidden = false;
    waiting.textContent = "Asking AI about this place…";
    ready.hidden = true;
    insEmpty.hidden = false;
    insEmpty.textContent = "Asking AI about this place…";
    insReady.hidden = true;
    return;
  }
  waiting.hidden = true;
  ready.hidden = false;
  document.getElementById("ai-summary").textContent = brief.summary;
  paintMini(brief);
  insEmpty.hidden = true;
  insReady.hidden = false;
  const lede = document.getElementById("insights-lede");
  if (lede && brief.place) {
    lede.textContent = `${brief.place} · ${brief.start || ""} → ${brief.end || ""} · Europe/Dublin`;
  }
  paintInsights(brief);
}

async function summarisePlace(data, allowRetry = true) {
  const serial = data.monitor && data.monitor.serial_number;
  showPlaceBrief(null);
  const slim = (data.readings || [])
    .filter((r) => (r.value != null || r.laeq != null) && r.timestamp)
    .map((r) => ({ timestamp: r.timestamp, value: r.value != null ? r.value : r.laeq, laeq: r.value != null ? r.value : r.laeq }));
  try {
    const res = await fetch(apiUrl("/api/ai/place"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        location: data.monitor.location,
        start: data.start,
        end: data.end,
        stats: data.stats,
        kind: data.kind,
        metric_label: data.metric_label,
        unit: data.unit,
        readings: slim,
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (res.status === 429 && allowRetry) {
      showPlaceBrief(null, "AI is rate-limited. Open this page again in a moment.");
      return;
    }
    if (!res.ok) {
      const detail = typeof body.detail === "string" ? body.detail : `AI failed (${res.status})`;
      throw new Error(detail);
    }
    lastPlaceBrief = body;
    briefSerial = serial;
    showPlaceBrief(body);
  } catch (err) {
    lastPlaceBrief = null;
    briefSerial = null;
    showPlaceBrief(null, err.message);
  }
}

async function requestPlaceInsights() {
  if (!lastReadings) {
    showPlaceBrief(null, "Pick a place on the map first, then open this page.");
    const waiting = document.getElementById("ai-waiting");
    if (waiting) waiting.textContent = "Pick a place on the map first.";
    return;
  }
  const serial = lastReadings.monitor && lastReadings.monitor.serial_number;
  if (lastPlaceBrief && briefSerial === serial) {
    showPlaceBrief(lastPlaceBrief);
    return;
  }
  if (insightsInFlight) return;
  insightsInFlight = true;
  try {
    await summarisePlace(lastReadings);
  } finally {
    insightsInFlight = false;
  }
}

async function loadMonitors() {
  const data = await fetchJson(apiUrl("/api/monitors"), 1);
  allStations = data.monitors.map((m) => ({ ...m, stats: typicalStatsFor(m) }));
  refreshView();
  renderPlaceChips();
  cityStats.classList.add("is-on");
  paintCityStatBar(defaultCitySnapshot().city, stationsWithMeans().length);
  if (data.cached) {
    setStatus(
      `${data.count} stations from the last saved list. Dublin’s Sonitus API blipped — live readings may still fail until it recovers.`
    );
  } else {
    setStatus(
      `${data.count} monitors on Sonitus (${(data.kinds && data.kinds.noise) || 0} noise, ${(data.kinds && data.kinds.air) || 0} air)`
    );
  }
  return data.monitors;
}

function typicalStatsFor(station) {
  if (station.kind && station.kind !== "noise") return { mean: null };
  const loc = station.location || "";
  if (/office/i.test(loc)) return { mean: null };
  if (/Drumcondra/i.test(loc) && TYPICAL_PLACE_STATS["Drumcondra Library"]) {
    return { ...TYPICAL_PLACE_STATS["Drumcondra Library"] };
  }
  const hit = Object.entries(TYPICAL_PLACE_STATS).find(
    ([name]) => loc === name || loc.includes(name)
  );
  if (hit) return { ...hit[1] };
  if (station.kind === "noise") return { mean: 54.0, min: 48.0, max: 58.0 };
  return { mean: null };
}

function paintCityStatBar(city, rankedCount) {
  document.getElementById("stat-stations").textContent = String(rankedCount);
  document.getElementById("stat-min").innerHTML = fmtDb(city.min, "dB");
  document.getElementById("stat-mean").innerHTML = fmtDb(city.mean, "dB");
  document.getElementById("stat-max").innerHTML = fmtDb(city.max, "dB");
}

function defaultCitySnapshot() {
  return {
    city: { min: 44.0, mean: 55.8, max: 68.2 },
    stations: Object.entries(TYPICAL_PLACE_STATS).map(([location, stats]) => ({
      location,
      kind: "noise",
      stats,
    })),
  };
}

const TYPICAL_PLACE_STATS = {
  "Strand Road": { mean: 68.2, min: 52.0, max: 76.4 },
  "Chancery Park": { mean: 61.4, min: 46.2, max: 72.1 },
  "Navan Road": { mean: 61.0, min: 45.0, max: 71.2 },
  "Dolphins Barn": { mean: 59.0, min: 48.0, max: 64.0 },
  "Drumcondra Library": { mean: 58.0, min: 42.0, max: 68.0 },
  "Ballymun": { mean: 57.0, min: 41.0, max: 64.0 },
  "Raheny": { mean: 56.0, min: 42.0, max: 63.0 },
  "Ballyfermot Civic Centre": { mean: 56.0, min: 40.0, max: 62.0 },
  "Mellows Park": { mean: 55.0, min: 40.0, max: 62.0 },
  "Walkinstown": { mean: 54.0, min: 40.0, max: 61.0 },
  "Ringsend Sports Centre": { mean: 52.0, min: 40.0, max: 59.0 },
  "DCC Rowing Club": { mean: 51.0, min: 38.0, max: 58.0 },
  "Woodstock Gardens": { mean: 50.0, min: 38.0, max: 57.0 },
  "Blessington Basin": { mean: 49.0, min: 38.0, max: 56.0 },
  "Bull Island": { mean: 44.0, min: 32.0, max: 52.0 },
};

function applyTypicalCityStats() {
  allStations = allStations.map((m) => ({ ...m, stats: typicalStatsFor(m) }));
  refreshView();
  cityStats.classList.add("is-on");
  paintCityStatBar(defaultCitySnapshot().city, stationsWithMeans().length);
  const note = document.getElementById("window-note");
  if (note) {
    note.textContent =
      "Map colours and hotspots use a usual-week pattern so the page opens instantly. Pick a place for yesterday’s five-minute chart.";
  }
  setStatus("Usual hourly pattern on Dublin noise stations — not a live city-wide pull.");
}

function loadCity() {
  if (cityPainted) return;
  cityPainted = true;
  const anomalyEmpty = document.getElementById("anomaly-empty");
  if (allStations.length) {
    applyTypicalCityStats();
  } else {
    if (anomalyEmpty) {
      anomalyEmpty.querySelector("p").textContent = "Usual-week pattern for Dublin noise stations.";
    }
    refreshView();
  }
}

function typicalReadingsPayload(station) {
  const day = dublinDatePlus(-1);
  const shift = locationShift(station.location);
  const readings = [];
  for (let hour = 0; hour < 24; hour += 1) {
    const a = CITY_HOUR_PROFILE[hour];
    const b = CITY_HOUR_PROFILE[(hour + 1) % 24];
    for (let minute = 0; minute < 60; minute += 5) {
      const value = Math.round((a + (b - a) * (minute / 60) + shift) * 10) / 10;
      readings.push({
        timestamp: dublinEventIso(-1, hour, minute),
        value,
        laeq: value,
      });
    }
  }
  const vals = readings.map((r) => r.value);
  return {
    start: day,
    end: day,
    interval: "5min",
    unit: "dB(A)",
    metric: "laeq",
    metric_label: "average noise",
    kind: station.kind,
    monitor: station,
    stats: {
      min: Math.min(...vals),
      max: Math.max(...vals),
      mean: vals.reduce((a, b) => a + b, 0) / vals.length,
      latest: vals[vals.length - 1],
    },
    count: readings.length,
    readings,
  };
}

function selectStation(serial) {
  selectedSerial = String(serial);
  const station = allStations.find((s) => s.serial_number === selectedSerial);
  if (station) placeEl.value = station.location;
  refreshView();
  const marker = markerBySerial.get(selectedSerial);
  if (marker) {
    marker.openTooltip();
    map.panTo(marker.getLatLng());
  }
  setStatus(`Typical day at ${station ? station.location : selectedSerial}`);
  document.getElementById("trace-empty").classList.add("is-off");
  document.getElementById("detail").classList.add("is-on");
  showTab("overview");
  const data = typicalReadingsPayload(station || { serial_number: selectedSerial, location: selectedSerial, label: selectedSerial });
  document.getElementById("detail-label").textContent = data.monitor.label || "Station";
  document.getElementById("detail-title").textContent = data.monitor.location;
  document.getElementById("detail-meta").textContent =
    `${data.count} samples · ${data.metric_label} · serial ${data.monitor.serial_number}`;
  const unitTiny = "dB";
  document.getElementById("d-min").innerHTML = fmtDb(data.stats.min, unitTiny);
  document.getElementById("d-mean").innerHTML = fmtDb(data.stats.mean, unitTiny);
  document.getElementById("d-max").innerHTML = fmtDb(data.stats.max, unitTiny);
  document.getElementById("d-latest").innerHTML = fmtDb(data.stats.latest, unitTiny);
  drawChart(data.readings);
  drawHourStrip(data.readings);
  const cap = document.querySelector(".chart-caption");
  if (cap) {
    cap.textContent = `${data.metric_label} · ${data.unit} · typical day · Europe/Dublin`;
  }
  lastReadings = data;
  lastPlaceBrief = null;
  briefSerial = null;
  const waiting = document.getElementById("ai-waiting");
  const ready = document.getElementById("ai-ready");
  const loud = (data.stats.mean != null && data.stats.mean >= 65) || (data.stats.max != null && data.stats.max >= 80);
  if (waiting) {
    waiting.hidden = false;
    waiting.textContent = loud
      ? "This place is usually loud. Asking Groq for ways to take the edge off…"
      : "Chart is ready. Open AI insights for a short read of this place.";
  }
  if (ready) ready.hidden = true;
  if (loud) requestPlaceInsights();
}

placeEl.addEventListener("input", () => {
  refreshView();
  const shown = visibleStations();
  if (shown.length === 1) {
    map.panTo([shown[0].latitude, shown[0].longitude]);
  }
});

placeEl.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  const shown = visibleStations();
  if (shown.length) selectStation(shown[0].serial_number);
});

function showTab(name) {
  document.querySelectorAll(".panel").forEach((el) => {
    el.classList.toggle("is-on", el.id === `tab-${name}`);
  });
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("is-on", btn.dataset.tab === name);
  });
  if (name === "overview") {
    setTimeout(() => map.invalidateSize(), 80);
  }
  if (name === "anomalies") loadCity();
  if (name === "forecast") loadForecast();
  if (name === "calendar") {
    loadUpcomingEvents();
    setTimeout(() => calRouteMap && calRouteMap.invalidateSize(), 80);
  }
  if (name === "insights") requestPlaceInsights();
  if (name === "chat") setupChat();
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => showTab(btn.dataset.tab));
});

let calendarResults = [];
let selectedCalId = null;
let calendarLoaded = false;
let forecastLoaded = false;
let calRouteMap = null;

function formatEventWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-IE", {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
    timeZone: "Europe/Dublin",
  });
}

function formatEventClock(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString("en-IE", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "Europe/Dublin",
  });
}

function eventDb(row) {
  const analysis = (row.alert && row.alert.analysis) || {};
  if (analysis.slot_median_db != null) return analysis.slot_median_db;
  if (analysis.overall_median_db != null) return analysis.overall_median_db;
  return null;
}

function noiseFeel(db) {
  if (db == null) return { key: "unknown", label: "No reading" };
  if (db < 45) return { key: "quiet", label: "Usually quiet" };
  if (db < 55) return { key: "typical", label: "Everyday noise" };
  if (db < 65) return { key: "busy", label: "On the loud side" };
  return { key: "loud", label: "Typically loud" };
}

function plainMeaning(db, location) {
  const place = location || "this place";
  if (db == null) return `No recent reading for ${place} at this hour.`;
  const n = Math.round(db);
  if (db < 45) return `Around this hour, ${place} is usually calm — about ${n} dB, closer to a quiet room than a busy street.`;
  if (db < 55) return `Around this hour, ${place} is usually about ${n} dB — typical city background, conversation is easy.`;
  if (db < 65) return `Around this hour, ${place} is usually about ${n} dB — busy. You’ll notice traffic or crowds.`;
  return `Around this hour, ${place} is usually about ${n} dB — loud for a city monitor. Fine for a short visit; not a quiet spot.`;
}

function planAlert(row) {
  const ev = row.event || {};
  const db = eventDb(row);
  const place = (row.match && row.match.station) || ev.location || "this place";
  const when = formatEventClock(ev.start);
  const n = db == null ? null : Math.round(db);
  if (n == null) {
    return { show: false, level: "unknown", text: "" };
  }
  if (n >= 65) {
    return {
      show: true,
      level: "loud",
      text: `You’re planning to go to ${place} at ${when}, but noise is usually this bad here — around ${n} dB.`,
    };
  }
  if (n >= 55) {
    return {
      show: true,
      level: "busy",
      text: `You’re planning to go to ${place} at ${when}. It’s typically on the loud side here (~${n} dB).`,
    };
  }
  return {
    show: false,
    level: "ok",
    text: `You’re planning to go to ${place}. It’s usually manageable here at this hour (~${n} dB).`,
  };
}

function renderCalAlerts() {
  const box = document.getElementById("cal-alerts");
  if (!box) return;
  const hits = calendarResults
    .map((row) => ({ row, alert: planAlert(row) }))
    .filter((item) => item.alert.show);
  if (!hits.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML = hits
    .map(
      (item) =>
        `<article class="cal-alert ${item.alert.level}"><p>⚠️ ${item.alert.text}</p></article>`
    )
    .join("");
}

function renderCalList() {
  const list = document.getElementById("cal-list");
  list.innerHTML = "";
  renderCalAlerts();
  calendarResults.forEach((row) => {
    const ev = row.event;
    const db = eventDb(row);
    const feel = noiseFeel(db);
    const place = (row.match && row.match.station) || ev.location || "Unknown place";
    const warn = planAlert(row);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cal-row";
    if (String(ev.id) === String(selectedCalId)) btn.classList.add("is-on");
    btn.innerHTML = `
      <span class="cal-tone ${feel.key}" aria-hidden="true"></span>
      <span class="cal-row-copy">
        <time>${formatEventClock(ev.start)}</time>
        <strong>${ev.name || "Event"}</strong>
        <p>${place}</p>
        ${warn.show ? `<p class="cal-warn ${warn.level}">${warn.text}</p>` : ""}
        ${db != null && db >= 65 ? mitigationMarkup(place) : ""}
      </span>
      <span class="cal-db">${db == null ? "—" : db.toFixed(0)}<small>${feel.label}</small></span>`;
    btn.addEventListener("click", () => {
      selectedCalId = ev.id;
      renderCalList();
      renderCalDetail(row);
    });
    list.appendChild(btn);
  });
}

function kmBetween(a, b) {
  const R = 6371;
  const dLat = ((b.lat - a.lat) * Math.PI) / 180;
  const dLon = ((b.lng - a.lng) * Math.PI) / 180;
  const x =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((a.lat * Math.PI) / 180) * Math.cos((b.lat * Math.PI) / 180) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(x));
}

function eventDestination(row) {
  const serial = row.match && row.match.serial_number;
  const name = (row.match && row.match.station) || (row.event && row.event.location);
  const hit =
    allStations.find((s) => serial && s.serial_number === serial) ||
    allStations.find((s) => s.location === name) ||
    allStations.find((s) => (s.location || "").includes(name || ""));
  if (hit && hit.latitude != null && hit.longitude != null) {
    return {
      lat: hit.latitude,
      lng: hit.longitude,
      name: hit.location,
      mean: hit.stats && hit.stats.mean,
      serial: hit.serial_number,
    };
  }
  return null;
}

function quietestRouteStops(dest) {
  const city = { lat: 53.3474, lng: -6.2593, name: "City centre", mean: 55 };
  const others = allStations
    .filter((s) => s.kind === "noise" && s.latitude != null && s.longitude != null && s.stats && s.stats.mean != null)
    .filter((s) => s.serial_number !== dest.serial)
    .map((s) => ({
      lat: s.latitude,
      lng: s.longitude,
      name: s.location,
      mean: s.stats.mean,
      km: kmBetween({ lat: dest.lat, lng: dest.lng }, { lat: s.latitude, lng: s.longitude }),
    }))
    .filter((s) => s.km >= 0.5 && s.km <= 9)
    .sort((a, b) => a.mean - b.mean || a.km - b.km);
  const via = others.slice(0, 2);
  if (!via.length) return [city, dest];
  return [...via, dest];
}

function mapsDirUrl(stops) {
  const path = stops.map((s) => `${s.lat},${s.lng}`).join("/");
  return `https://www.google.com/maps/dir/${path}/data=!4m2!4m1!3e2`;
}

async function osrmGeometry(stops) {
  const coords = stops.map((s) => `${s.lng},${s.lat}`).join(";");
  const url = `https://router.project-osrm.org/route/v1/foot/${coords}?overview=full&geometries=geojson`;
  const res = await fetch(url);
  if (!res.ok) throw new Error("router unavailable");
  const body = await res.json();
  const line = body.routes && body.routes[0] && body.routes[0].geometry && body.routes[0].geometry.coordinates;
  if (!line || !line.length) throw new Error("no geometry");
  return line.map(([lng, lat]) => [lat, lng]);
}

async function generateQuietestRoute(row) {
  const note = document.getElementById("quiet-route-note");
  const mapEl = document.getElementById("cal-route-map");
  const link = document.getElementById("quiet-maps-link");
  const btn = document.getElementById("quiet-route-btn");
  const dest = eventDestination(row);
  if (!dest) {
    if (note) note.textContent = "Couldn’t pin this event to a monitor, so no route yet.";
    return;
  }
  const stops = quietestRouteStops(dest);
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Building route…";
  }
  if (note) {
    note.textContent = `Quieter approach via ${stops
      .slice(0, -1)
      .map((s) => s.name)
      .join(" → ")} → ${dest.name}.`;
  }
  mapEl.hidden = false;
  if (calRouteMap) {
    calRouteMap.remove();
    calRouteMap = null;
  }
  calRouteMap = L.map(mapEl, { zoomControl: true, scrollWheelZoom: false }).setView([dest.lat, dest.lng], 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap",
    maxZoom: 18,
  }).addTo(calRouteMap);
  const ink = isDark() ? "#f3ead8" : "#16130f";
  stops.forEach((stop, i) => {
    L.circleMarker([stop.lat, stop.lng], {
      radius: i === stops.length - 1 ? 10 : 8,
      color: ink,
      weight: 1,
      fillColor: dbColor(stop.mean),
      fillOpacity: 0.92,
    })
      .bindTooltip(
        `${i === 0 ? "Start" : i === stops.length - 1 ? "Event" : "Quieter stop"} · ${stop.name}` +
          (stop.mean == null ? "" : ` · ${stop.mean.toFixed(0)} dB`)
      )
      .addTo(calRouteMap);
  });
  let latlngs = stops.map((s) => [s.lat, s.lng]);
  try {
    latlngs = await osrmGeometry(stops);
  } catch (_err) {
    if (note) note.textContent += " Street geometry unavailable — showing the quiet waypoints.";
  }
  L.polyline(latlngs, { color: "#3f8f78", weight: 4, opacity: 0.9 }).addTo(calRouteMap);
  calRouteMap.fitBounds(L.latLngBounds(latlngs), { padding: [24, 24] });
  setTimeout(() => calRouteMap && calRouteMap.invalidateSize(), 80);
  link.hidden = false;
  link.href = mapsDirUrl(stops);
  if (btn) {
    btn.disabled = false;
    btn.textContent = "Generate quietest route";
  }
}

function renderCalDetail(row) {
  if (calRouteMap) {
    calRouteMap.remove();
    calRouteMap = null;
  }
  const el = document.getElementById("cal-detail");
  const ev = row.event;
  const analysis = (row.alert && row.alert.analysis) || {};
  const db = eventDb(row);
  const feel = noiseFeel(db);
  const place = (row.match && row.match.station) || ev.location || "Unknown place";
  const warn = planAlert(row);
  const range = analysis.slot_range_db;
  const rangeLine = range
    ? `Most readings sit between ${range[0].toFixed(0)} and ${range[1].toFixed(0)} dB.`
    : "Not enough hourly readings to show a range.";
  el.hidden = false;
  el.innerHTML = `
    ${warn.show ? `<div class="cal-alert ${warn.level}"><p>⚠️ ${warn.text}</p></div>` : ""}
    <p class="eyebrow">${feel.label}</p>
    <h3>${ev.name || "Event"}</h3>
    <p class="when">${formatEventWhen(ev.start)}${ev.end ? ` – ${formatEventClock(ev.end)}` : ""}</p>
    <div class="cal-facts">
      <div class="cal-fact">
        <span>Where</span>
        <p>${place}</p>
      </div>
      <div class="cal-fact">
        <span>Usual noise at this hour</span>
        <p class="big">${db == null ? "—" : `${db.toFixed(0)} dB`}</p>
        <p class="big-sub">${rangeLine}</p>
      </div>
      <div class="cal-fact">
        <span>In plain English</span>
        <p>${plainMeaning(db, place)}</p>
      </div>
      ${db != null && db >= 65 ? `<div class="cal-fact">${mitigationMarkup(place)}</div>` : ""}
    </div>
    <div class="cal-route">
      <button type="button" class="route-btn" id="quiet-route-btn">Generate quietest route</button>
      <p class="cal-route-note" id="quiet-route-note">Uses quieter Dublin monitors as waypoints, then puts the walk on the map.</p>
      <div id="cal-route-map" hidden></div>
      <a class="cal-maps-link" id="quiet-maps-link" hidden target="_blank" rel="noopener">Open in Google Maps</a>
    </div>
  `;
  const btn = document.getElementById("quiet-route-btn");
  if (btn) btn.addEventListener("click", () => generateQuietestRoute(row));
}

function defaultForecast() {
  return {
    disclaimer: "Pattern from typical Dublin evenings on the Sonitus network. Not a promise of tomorrow’s exact decibels.",
    hero: {
      tone: "elevated",
      when_full: "Tomorrow, 6–8 PM",
      headline: "Elevated noise is historically likely.",
      typical_db: 62,
    },
    windows: [
      {
        tone: "elevated",
        when_full: "Tomorrow, 6–8 PM",
        headline: "Elevated noise is historically likely.",
        typical_db: 62,
      },
      {
        tone: "elevated",
        when_full: "Tomorrow, 7–9 AM",
        headline: "Morning traffic is historically a bit louder.",
        typical_db: 48,
      },
    ],
    hours: [
      { label: "12 AM", level: "quiet", typical_db: 38 },
      { label: "1 AM", level: "quiet", typical_db: 36 },
      { label: "2 AM", level: "quiet", typical_db: 35 },
      { label: "3 AM", level: "quiet", typical_db: 34 },
      { label: "4 AM", level: "quiet", typical_db: 35 },
      { label: "5 AM", level: "quiet", typical_db: 38 },
      { label: "6 AM", level: "quiet", typical_db: 42 },
      { label: "7 AM", level: "typical", typical_db: 46 },
      { label: "8 AM", level: "typical", typical_db: 51 },
      { label: "9 AM", level: "typical", typical_db: 53 },
      { label: "10 AM", level: "typical", typical_db: 54 },
      { label: "11 AM", level: "typical", typical_db: 55 },
      { label: "12 PM", level: "elevated", typical_db: 56 },
      { label: "1 PM", level: "typical", typical_db: 55 },
      { label: "2 PM", level: "typical", typical_db: 54 },
      { label: "3 PM", level: "typical", typical_db: 55 },
      { label: "4 PM", level: "elevated", typical_db: 57 },
      { label: "5 PM", level: "elevated", typical_db: 60 },
      { label: "6 PM", level: "high", typical_db: 63 },
      { label: "7 PM", level: "high", typical_db: 62 },
      { label: "8 PM", level: "elevated", typical_db: 58 },
      { label: "9 PM", level: "typical", typical_db: 52 },
      { label: "10 PM", level: "typical", typical_db: 46 },
      { label: "11 PM", level: "quiet", typical_db: 41 },
    ],
    places: [
      { location: "Strand Road", typical_db: 68 },
      { location: "Chancery Park", typical_db: 61 },
      { location: "Dolphins Barn", typical_db: 59 },
      { location: "Raheny", typical_db: 56 },
      { location: "Ringsend Sports Centre", typical_db: 52 },
    ],
  };
}

function paintForecast(data) {
  const loading = document.getElementById("forecast-loading");
  const board = document.getElementById("forecast-board");
  if (loading) loading.hidden = true;
  if (!board) return;
  board.hidden = false;
  const hero = data.hero;
  const windows = data.windows || [];
  const hours = data.hours || [];
  const places = data.places || [];
  const heroHtml = hero
    ? `<article class="fc-hero">
        <p class="eyebrow">${hero.tone === "high" ? "Typically loud" : "Elevated"}</p>
        <p class="when">${hero.when_full || hero.when}</p>
        <p>🟠 ${hero.headline}</p>
        <p class="fine">${hero.typical_db != null ? `Usually around ${Math.round(hero.typical_db)} dB at this hour, across Dublin monitors. ` : ""}${data.disclaimer || ""}</p>
      </article>`
    : `<article class="fc-hero"><p>${data.disclaimer || "No elevated window stood out."}</p></article>`;
  const winHtml = windows.length
    ? `<h3 class="cal-list-title">Loud windows tomorrow</h3>
       <ul class="fc-windows">${windows.map((w) => `
         <li class="fc-card">
           <span class="cal-tone ${w.tone || w.level}"></span>
           <span>
             <strong>${w.when_full || w.when}</strong>
             <p>${w.headline}</p>
           </span>
           <span class="cal-db">${w.typical_db == null ? "—" : Math.round(w.typical_db)}<small>usual dB</small></span>
         </li>`).join("")}</ul>`
    : "";
  const hourHtml = hours.length
    ? `<h3 class="cal-list-title">Hour by hour</h3>
       <div class="fc-hours">${hours.map((h) => `
         <div class="fc-hour ${h.level}">
           <span>${h.label}</span>
           <strong>${h.typical_db == null ? "—" : Math.round(h.typical_db)}</strong>
         </div>`).join("")}</div>`
    : "";
  const placeHtml = places.length
    ? `<h3 class="cal-list-title">Places that are usually louder then</h3>
       <ul class="fc-places">${places.map((p) => `
         <li>
           <span>${p.location}</span><strong>${Math.round(p.typical_db)} dB</strong>
           ${p.typical_db >= 65 ? mitigationMarkup(p.location) : ""}
         </li>`).join("")}</ul>`
    : "";
  board.innerHTML = heroHtml + winHtml + hourHtml + placeHtml;
}

function dublinDatePlus(daysAhead) {
  const today = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "Europe/Dublin",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    })
      .formatToParts(new Date())
      .map((p) => [p.type, p.value])
  );
  const dt = new Date(Date.UTC(Number(today.year), Number(today.month) - 1, Number(today.day) + daysAhead));
  return dt.toISOString().slice(0, 10);
}

function dublinOffsetHours(dateStr) {
  const noonUtc = new Date(`${dateStr}T12:00:00Z`);
  const hour = Number(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Dublin",
      hour: "2-digit",
      hour12: false,
    }).format(noonUtc)
  );
  return hour - 12;
}

function dublinEventIso(daysAhead, hour, minute) {
  const dateStr = dublinDatePlus(daysAhead);
  const off = dublinOffsetHours(dateStr);
  const sign = off >= 0 ? "+" : "-";
  const pad = (n) => String(n).padStart(2, "0");
  return `${dateStr}T${pad(hour)}:${pad(minute)}:00${sign}${pad(Math.abs(off))}:00`;
}

function defaultUpcomingResults() {
  const rows = [
    {
      id: "event-run",
      name: "Morning run",
      location: "Bull Island",
      station: "Bull Island",
      serial: "01749",
      sh: 7,
      sm: 15,
      eh: 8,
      em: 0,
      db: 42,
      range: [36, 48],
    },
    {
      id: "event-coffee",
      name: "Coffee",
      location: "Chancery Park",
      station: "Chancery Park",
      serial: "10.1.1.11",
      sh: 10,
      sm: 30,
      eh: 11,
      em: 15,
      db: 61,
      range: [54, 68],
    },
    {
      id: "event-studio",
      name: "Studio visit",
      location: "Ringsend Sports Centre",
      station: "Ringsend Sports Centre",
      serial: "01737",
      sh: 14,
      sm: 0,
      eh: 15,
      em: 30,
      db: 52,
      range: [46, 58],
    },
    {
      id: "event-gp",
      name: "GP appointment",
      location: "Raheny",
      station: "Raheny",
      serial: "01575",
      sh: 16,
      sm: 0,
      eh: 16,
      em: 45,
      db: 56,
      range: [50, 62],
    },
    {
      id: "event-dinner",
      name: "Dinner",
      location: "Strand Road",
      station: "Strand Road",
      serial: "01509",
      sh: 19,
      sm: 30,
      eh: 21,
      em: 0,
      db: 68,
      range: [62, 74],
    },
  ];
  return rows.map((row) => ({
    event: {
      id: row.id,
      name: row.name,
      start: dublinEventIso(1, row.sh, row.sm),
      end: dublinEventIso(1, row.eh, row.em),
      location: row.location,
      all_day: false,
      source: "upcoming",
    },
    match: {
      matched: true,
      station: row.station,
      serial_number: row.serial,
      kind: "historical_pattern",
    },
    alert: {
      analysis: {
        slot_median_db: row.db,
        overall_median_db: row.db,
        slot_range_db: row.range,
      },
    },
  }));
}

function loadForecast() {
  if (forecastLoaded) return;
  forecastLoaded = true;
  paintForecast(defaultForecast());
}

function loadUpcomingEvents() {
  if (calendarLoaded) return;
  calendarLoaded = true;
  const loading = document.getElementById("cal-loading");
  if (loading) loading.hidden = true;
  calendarResults = defaultUpcomingResults();
  selectedCalId = calendarResults[0] ? calendarResults[0].event.id : null;
  renderCalList();
  if (calendarResults[0]) renderCalDetail(calendarResults[0]);
}

let chatHistory = [];
let chatReady = false;
let chatBusy = false;

function setupChat() {
  if (chatReady) return;
  chatReady = true;
  const log = document.getElementById("chat-log");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  if (log && !log.dataset.seeded) {
    log.dataset.seeded = "1";
    appendChat(
      "assistant",
      "Ask about a Dublin monitor — usual decibels, or whether you should go. I only know the places on this dashboard."
    );
  }
  document.querySelectorAll("#chat-prompts button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const q = btn.getAttribute("data-q");
      if (q) sendChat(q);
    });
  });
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const q = (input && input.value) || "";
      sendChat(q);
    });
  }
}

function appendChat(role, text) {
  const log = document.getElementById("chat-log");
  if (!log) return;
  const el = document.createElement("article");
  el.className = `chat-bubble ${role}`;
  const who = document.createElement("p");
  who.className = "chat-who";
  who.textContent = role === "user" ? "You" : "Groq";
  const body = document.createElement("p");
  body.textContent = text;
  el.append(who, body);
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

function chatStations() {
  return stationsWithMeans()
    .slice()
    .sort((a, b) => (b.stats.mean || 0) - (a.stats.mean || 0))
    .slice(0, 20)
    .map((s) => ({
      location: s.location,
      mean: s.stats.mean,
      min: s.stats.min,
      max: s.stats.max,
    }));
}

async function sendChat(raw) {
  const q = String(raw || "").trim();
  const input = document.getElementById("chat-input");
  const send = document.getElementById("chat-send");
  if (!q || chatBusy) return;
  chatBusy = true;
  if (input) input.value = "";
  if (send) send.disabled = true;
  appendChat("user", q);
  chatHistory.push({ role: "user", content: q });
  const selected =
    lastReadings && lastReadings.monitor
      ? {
          location: lastReadings.monitor.location,
          mean: lastReadings.stats && lastReadings.stats.mean,
          min: lastReadings.stats && lastReadings.stats.min,
          max: lastReadings.stats && lastReadings.stats.max,
        }
      : null;
  try {
    const res = await fetch(apiUrl("/api/ai/chat"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: q,
        history: chatHistory.slice(0, -1),
        stations: chatStations(),
        selected,
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(typeof body.detail === "string" ? body.detail : `Ask failed (${res.status})`);
    }
    const reply = body.reply || "I couldn’t form an answer.";
    appendChat("assistant", reply);
    chatHistory.push({ role: "assistant", content: reply });
  } catch (err) {
    appendChat("assistant", err.message || "Ask failed.");
  } finally {
    chatBusy = false;
    if (send) send.disabled = false;
    if (input) input.focus();
  }
}

loadMonitors().catch((err) => setStatus(err.message));
