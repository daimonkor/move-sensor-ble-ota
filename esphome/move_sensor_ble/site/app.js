// Панель Move Sensor BLE: текущие показания из SSE /events (без истории и статистики).
// Сущности ищем по name_id ESPHome ("sensor/Temperature", "binary_sensor/Occupancy" …) —
// это имена из move-sensor-ble.yaml. Переименовал сущность в YAML → поправь ключ здесь.
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var S = {};            // name_id → последнее JSON-сообщение state
  var lastMsg = 0;       // время последнего события SSE (state/ping)
  var lastUpd = 0;       // время последнего state
  var lastMotion = 0;    // когда страница последний раз видела «занято» (только с момента открытия)
  var prevOcc = null;

  // ── ключи сущностей ──
  var K = {
    occ: "binary_sensor/Occupancy", batLow: "binary_sensor/Battery low", svc: "binary_sensor/Service mode",
    temp: "sensor/Temperature", hum: "sensor/Humidity", bat: "sensor/Battery", volt: "sensor/Voltage",
    d0: "sensor/D0 raw voltage", wakes: "sensor/Wake count",
    wPct: "sensor/WiFi Signal", wDbm: "sensor/WiFi RSSI", bPct: "sensor/BLE Signal", bDbm: "sensor/BLE RSSI",
    why: "text_sensor/Wake reason", esv: "text_sensor/ESPHome Version", build: "text_sensor/Build Version",
    keep: "number/Keep time", delay: "number/Detect delay", clim: "number/Climate report interval", vf: "number/Voltage factor",
    led: "switch/Motion tracking LED", sim: "switch/Simulate climate"
  };

  function num(k) {
    var j = S[k]; if (!j) return null;
    if (typeof j.value === "number" && isFinite(j.value)) return j.value;
    var p = parseFloat(j.state); return isFinite(p) ? p : null;
  }
  function bool(k) {
    var j = S[k]; if (!j) return null;
    if (typeof j.value === "boolean") return j.value;
    if (j.state === "ON") return true; if (j.state === "OFF") return false;
    return null;
  }
  function str(k) { var j = S[k]; return j ? (j.value !== undefined && j.value !== null ? String(j.value) : j.state) : null; }
  function fmt(v, d) { return v === null ? "—" : v.toFixed(d); }
  function txt(id, s) { var e = $(id); if (e && e.textContent !== s) e.textContent = s; }
  function clamp(x, a, b) { return x < a ? a : x > b ? b : x; }
  function ago(ms) {
    var s = Math.round(ms / 1000);
    if (s < 60) return s + " с";
    var m = Math.floor(s / 60); if (m < 60) return m + " мин";
    var h = Math.floor(m / 60); return h + " ч " + (m % 60) + " мин";
  }
  function hms(t) { var d = new Date(t); return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }

  var WHY = { "cold boot": "холодный старт", "timer": "таймер климата", "service button": "сервисный переключатель", "pir": "PIR (движение)" };

  // ── «пик» при появлении движения (WebAudio, без файлов). Браузер даёт звук только после
  // первого касания/клика по странице — до этого AudioContext «спит», будим его на жесте. ──
  var sndOn = true, actx = null;
  try { sndOn = localStorage.getItem("ms-snd") !== "0"; } catch (e) { /* без хранилища — включён */ }
  function audio() {
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    if (!actx) { try { actx = new AC(); actx.onstatechange = syncSnd; } catch (e) { return null; } }
    return actx;
  }
  function tone(a) {
    var t = a.currentTime + 0.02, o = a.createOscillator(), g = a.createGain();
    o.type = "square"; o.frequency.value = 1320;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.3, t + 0.01);
    g.gain.setValueAtTime(0.3, t + 0.14);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.2);
    o.connect(g); g.connect(a.destination);
    o.start(t); o.stop(t + 0.22);
  }
  function beep() {
    if (!sndOn) return;
    var a = audio();
    if (!a) return;
    if (a.state === "running") { tone(a); return; }
    // контекст спит: resume() сработает только если по странице уже был клик
    var p = a.resume && a.resume();
    if (p && p.then) p.then(function () { if (a.state === "running") tone(a); }, function () {});
  }
  function armed() { return !!actx && actx.state === "running"; }
  function syncSnd() {
    var b = $("ms-snd"), wait = sndOn && !armed();
    b.classList.toggle("on", sndOn && !wait); b.classList.toggle("off", !sndOn); b.classList.toggle("wait", wait);
    b.title = !sndOn ? "Звук при движении: выключен" : wait ? "Звук ждёт клика — нажмите, чтобы включить" : "Звук при движении: включён";
    b.setAttribute("aria-label", b.title); b.setAttribute("aria-pressed", sndOn);
  }
  // Любой жест по странице будит звук. На кнопке: если звук ещё «спит» — клик только будит
  // его и даёт пробный пик (не выключает); иначе — переключает вкл/выкл.
  var wasWait = false;
  function unlock(e) {
    wasWait = sndOn && !armed();
    if (!sndOn) return;
    var a = audio();
    if (a && a.state !== "running" && a.resume) a.resume();
  }
  ["pointerdown", "touchend", "keydown"].forEach(function (ev) { document.addEventListener(ev, unlock, true); });
  $("ms-snd").addEventListener("click", function () {
    if (!wasWait) {
      sndOn = !sndOn;
      try { localStorage.setItem("ms-snd", sndOn ? "1" : "0"); } catch (e) { /* нет хранилища */ }
    }
    wasWait = false;
    syncSnd();
    if (sndOn) beep();  // пробный пик
  });
  syncSnd();

  // ── отрисовка ──
  function renderMotion() {
    var occ = bool(K.occ), hero = $("ms-hero");
    if (occ === true && prevOcc === false) beep();  // «пик» только на переходе тихо → движение
    if (occ === true) lastMotion = Date.now();
    prevOcc = occ;
    hero.classList.toggle("occ", occ === true);
    hero.classList.toggle("clear", occ === false);
    txt("ms-occ", occ === null ? "—" : occ ? "Есть движение" : "Тихо");
    var keep = num(K.keep), del = num(K.delay);
    if (occ === true) txt("ms-occsub", "Освободится через " + (keep === null ? "keep time" : keep + " с") + " после спада PIR");
    else if (occ === false) txt("ms-occsub", lastMotion ? "Последнее движение " + ago(Date.now() - lastMotion) + " назад" : "Движения нет");
    else txt("ms-occsub", " ");
    txt("ms-keep", "Keep time " + (keep === null ? "—" : keep + " с"));
    txt("ms-delay", "Detect delay " + (del === null ? "—" : del + " мс"));
  }

  // маркер текущего значения на шкале: позиция в % и цвет зоны
  function pin(id, pct, zone) {
    var p = $(id);
    p.classList.toggle("hide", pct === null);
    if (pct === null) return;
    p.style.left = clamp(pct, 0, 100) + "%";
    p.className = "pin " + zone;
  }
  function renderClimate() {
    var t = num(K.temp), h = num(K.hum), sim = bool(K.sim) === true;
    $("ms-sim1").classList.toggle("hide", !sim);
    $("ms-sim2").classList.toggle("hide", !sim);
    txt("ms-temp", fmt(t, 1));
    txt("ms-temp-n", t === null ? " " : t < 18 ? "Прохладно" : t <= 26 ? "Комфортно" : "Жарко");
    pin("ms-temp-pin", t === null ? null : (t - 10) / 25 * 100, t === null ? "" : t < 18 ? "cold" : t <= 26 ? "ok" : "hot");
    txt("ms-hum", fmt(h, 0));
    txt("ms-hum-n", h === null ? " " : h < 30 ? "Сухо" : h <= 60 ? "Норма" : "Влажно");
    pin("ms-hum-pin", h, h === null ? "" : h < 30 ? "dry" : h <= 60 ? "ok" : "wet");
  }

  function renderBattery() {
    var p = num(K.bat), v = num(K.volt), low = bool(K.batLow) === true, card = $("ms-batcard");
    txt("ms-bat", fmt(p, 0));
    txt("ms-volt", fmt(v, 2));
    $("ms-batfill").style.width = p === null ? "0" : clamp(p, 0, 100) + "%";
    $("ms-batlow").classList.toggle("hide", !low);
    card.classList.toggle("low", low || (p !== null && p < 20));
    card.classList.toggle("mid", !low && p !== null && p >= 20 && p < 40);
  }

  function bars(id, pct) {
    var n = pct === null ? 0 : pct >= 75 ? 4 : pct >= 50 ? 3 : pct >= 25 ? 2 : pct > 0 ? 1 : 0;
    var box = $(id);
    Array.prototype.forEach.call(box.children, function (b, i) { b.classList.toggle("on", i < n); });
    box.classList.toggle("weak", n > 0 && n <= 1);
  }
  function renderSignal() {
    var wp = num(K.wPct), wd = num(K.wDbm), bp = num(K.bPct), bd = num(K.bDbm);
    bars("ms-wbars", wp); bars("ms-bbars", bp);
    txt("ms-wpct", wp === null ? "—" : Math.round(wp) + "%");
    txt("ms-wdbm", (wd === null ? "—" : Math.round(wd)) + " dBm");
    txt("ms-bpct", bp === null ? "—" : Math.round(bp) + "%");
    txt("ms-bdbm", bd === null ? "нет данных из HA" : Math.round(bd) + " dBm");
  }

  function renderDiag() {
    var why = str(K.why), svc = bool(K.svc), clim = num(K.clim), led = bool(K.led), d0 = num(K.d0), vf = num(K.vf), w = num(K.wakes);
    txt("ms-why", why ? (WHY[why] || why) : "—");
    txt("ms-wakes", w === null ? "—" : String(Math.round(w)));
    txt("ms-svc", svc === null ? "—" : svc ? "включён" : "выключен");
    txt("ms-clim", clim === null ? "—" : clim < 1 ? "выключен" : "каждые " + clim + " мин");
    txt("ms-d0", d0 === null ? "—" : d0.toFixed(3) + " В");
    txt("ms-vf", vf === null ? "—" : vf.toFixed(3));
    txt("ms-led", led === null ? "—" : led ? "включён" : "выключен");
    txt("ms-esv", str(K.esv) || "—");
    txt("ms-build", str(K.build) || "—");
    txt("ms-upd", lastUpd ? hms(lastUpd) : "—");
  }

  var rq = 0;
  function render() {
    if (rq) return;
    rq = requestAnimationFrame(function () {
      rq = 0;
      renderMotion(); renderClimate(); renderBattery(); renderSignal(); renderDiag(); renderWifi();
    });
  }

  // ── связь ──
  function renderOnline() {
    var on = lastMsg && Date.now() - lastMsg < 30000, el = $("ms-onl");
    el.classList.toggle("on", !!on);
    el.classList.toggle("off", !on && !!lastMsg);
    txt("ms-onl-t", on ? "В сети" : lastMsg ? "Нет связи — переподключение…" : "Подключение…");
    var m = document.querySelector("#ms main");
    if (m) m.classList.toggle("stale", !on && !!lastMsg);
  }

  function onState(e) {
    var j;
    try { j = JSON.parse(e.data); } catch (err) { return; }
    var id = j.name_id || j.id;
    if (!id) return;
    // старый формат id ("sensor-temperature") → домен + имя
    if (id.indexOf("/") < 0 && j.name) id = id.split("-")[0] + "/" + j.name;
    S[id] = j;
    lastMsg = lastUpd = Date.now();
    render();
  }
  function onPing() { lastMsg = Date.now(); }

  function attach(src) {
    src.addEventListener("state", onState);
    src.addEventListener("ping", onPing);
  }

  // Своё SSE, пока штатный интерфейс не запущен. После запуска «Устройства» берём его
  // поток (window.source) и своё закрываем — второе соединение плате ни к чему.
  var es = null, shared = false;
  function openOwn() {
    if (shared || es || !window.EventSource) return;
    es = new EventSource("/events");
    attach(es);
    es.onerror = function () { renderOnline(); };
  }
  function useShared() {
    if (shared) return;
    var src = window.source;
    if (!src) { setTimeout(useShared, 500); return; }
    shared = true;
    attach(src);
    if (es) { es.close(); es = null; }
  }

  // ── «Wi-Fi роутера» (move_sensor_ble/wifi_setup.yaml): как у весов. Данные — из того же SSE,
  // действия — REST: текст в «New Wi-Fi SSID/Password» → «Reconnect to router». ──
  function post(url) { return fetch(url, { method: "POST" }).then(function (r) { if (!r.ok) throw new Error(r.status); }); }
  function postText(name, v) { return post("/text/" + encodeURIComponent(name) + "/set?value=" + encodeURIComponent(v)); }
  function press(name) { return post("/button/" + encodeURIComponent(name) + "/press"); }
  var wBusy = false, resetArmed = 0;
  function wstat(t) { txt("ms-wstat", t || ""); }
  function setWBusy(on) {
    wBusy = on;
    Array.prototype.forEach.call(document.querySelectorAll("#ms-wsheet button:not(#ms-wclose), #ms-wsheet input"), function (c) { c.disabled = on; });
  }
  function syncGo() { txt("ms-wgo", $("ms-wssid").value.trim() ? "Подключиться к сети" : "Переподключиться"); }
  function parseNets(text) {  // "SSID: -63dB" по строке (wifi_info scan_results)
    var out = [], seen = {};
    (text || "").split("\n").forEach(function (l) {
      var m = /^(.*): (-?\d+)dB$/.exec(l.trim());
      if (m && m[1] && !seen[m[1]]) { seen[m[1]] = 1; out.push({ ssid: m[1], rssi: parseInt(m[2], 10) }); }
    });
    return out.sort(function (a, b) { return b.rssi - a.rssi; });
  }
  var netsKey = null;
  function renderWifi() {
    if ($("ms-wsheet").classList.contains("hide")) return;
    var now = str("text_sensor/Wi-Fi SSID");
    txt("ms-wnow", now ? "Сейчас: " + now : "К роутеру не подключён");
    var raw = str("text_sensor/Wi-Fi Scan Results") || "", cur = $("ms-wssid").value.trim();
    if (raw + "|" + cur === netsKey) return;
    netsKey = raw + "|" + cur;
    var box = $("ms-nets"), nets = parseNets(raw);
    box.textContent = "";
    if (!nets.length) { var e = document.createElement("div"); e.className = "none"; e.textContent = "Сетей пока нет — «Найти сети»"; box.appendChild(e); return; }
    nets.forEach(function (n) {
      var b = document.createElement("button"), nm = document.createElement("span"), r = document.createElement("span");
      b.type = "button"; b.className = n.ssid === cur ? "on" : "";
      nm.textContent = n.ssid; r.className = "rs"; r.textContent = n.rssi + " dBm";
      b.appendChild(nm); b.appendChild(r);
      b.addEventListener("click", function () { $("ms-wssid").value = n.ssid; syncGo(); $("ms-wpass").focus(); renderWifi(); });
      box.appendChild(b);
    });
  }
  function openWifi() { $("ms-wsheet").classList.remove("hide"); netsKey = null; syncGo(); wstat(""); renderWifi(); }
  function closeWifi() {
    if (wBusy) return;
    $("ms-wsheet").classList.add("hide");
    if (location.hash === "#wifi") history.replaceState(null, "", location.pathname);
  }
  $("ms-wopen").addEventListener("click", openWifi);
  $("ms-wclose").addEventListener("click", closeWifi);
  $("ms-wsheet").addEventListener("click", function (e) { if (e.target === $("ms-wsheet")) closeWifi(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeWifi(); });
  $("ms-wssid").addEventListener("input", function () { syncGo(); renderWifi(); });
  // После переподключения датчик пропадает до ~20 с (+ откат) — ждём, пока снова пойдёт SSE.
  function waitBack(t0, want) {
    (function wait() {
      setTimeout(function () {
        var now = str("text_sensor/Wi-Fi SSID");
        if (lastMsg > t0 + 5000 && now && (!want || now === want)) {
          setWBusy(false); var ok = "Готово: " + now; wstat(ok);
          // успех — показать «Готово» 2 с и закрыть окно (если его не открыли/не трогали заново)
          setTimeout(function () { if (!wBusy && $("ms-wstat").textContent === ok) closeWifi(); }, 2000);
        }
        else if (Date.now() - t0 < 45000) wait();
        else { setWBusy(false); wstat(lastMsg > t0 + 5000 ? "Не подключилось — осталась прежняя сеть" : "Связь с датчиком потеряна — проверьте сеть"); }
      }, 2500);
    })();
  }
  $("ms-wgo").addEventListener("click", function () {
    if (wBusy) return;
    var ssid = $("ms-wssid").value.trim(), pass = $("ms-wpass").value;
    wstat("Отправлено, подключаюсь…"); setWBusy(true);
    postText("New Wi-Fi SSID", ssid)
      .then(function () { return postText("New Wi-Fi Password", ssid ? pass : ""); })
      .then(function () { return press("Reconnect to router"); })
      .then(function () { $("ms-wpass").value = ""; waitBack(Date.now(), ssid); },
            function () { setWBusy(false); wstat("Не удалось отправить"); });
  });
  $("ms-wscan").addEventListener("click", function () {
    if (wBusy) return;
    var before = str("text_sensor/Wi-Fi Scan Results"), t0 = Date.now();
    wstat("Ищу сети…"); setWBusy(true);
    press("Scan Wi-Fi networks").then(function () {
      (function wait() {
        setTimeout(function () {
          // фоновый скан ~2–4 с; тот же список — результат не меняется, ждём не дольше 10 с
          var nowRes = str("text_sensor/Wi-Fi Scan Results");
          if (nowRes !== before || Date.now() - t0 > 10000) {
            setWBusy(false); netsKey = null; renderWifi();
            wstat(nowRes ? "" : "Сети не найдены — попробуйте ещё раз");
          }
          else wait();
        }, 1000);
      })();
    }, function () { setWBusy(false); wstat("Не удалось отправить"); });
  });
  $("ms-wreset").addEventListener("click", function () {  // два нажатия вместо confirm()
    var b = $("ms-wreset");
    if (Date.now() > resetArmed) {
      resetArmed = Date.now() + 4000; b.classList.add("warn"); b.textContent = "Точно? Нажмите ещё раз";
      setTimeout(function () { if (Date.now() > resetArmed) { b.classList.remove("warn"); b.textContent = "Сеть из прошивки"; } }, 4100);
      return;
    }
    resetArmed = 0; b.classList.remove("warn"); b.textContent = "Сеть из прошивки";
    wstat("Отправлено, подключаюсь…"); setWBusy(true);
    press("Reset Wi-Fi to firmware defaults").then(function () { waitBack(Date.now(), ""); },
      function () { setWBusy(false); wstat("Не удалось отправить"); });
  });
  window.addEventListener("hashchange", function () { if (location.hash === "#wifi") openWifi(); });

  // ── тема: авто → светлая → тёмная (как у весов), одна на панель и штатный интерфейс ──
  var THEMES = ["auto", "light", "dark"];
  var ICON = {
    auto: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3v18" /><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/></svg>',
    light: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
    dark: '<svg viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/></svg>'
  };
  var NAME = { auto: "авто", light: "светлая", dark: "тёмная" };
  var darkMq = window.matchMedia("(prefers-color-scheme: dark)");
  var theme = "auto";
  try { theme = localStorage.getItem("ms-theme") || "auto"; } catch (e) { /* без хранилища — авто */ }
  if (THEMES.indexOf(theme) < 0) theme = "auto";
  function effTheme() { return theme === "auto" ? (darkMq.matches ? "dark" : "light") : theme; }
  function syncEspApp() {
    var app = document.querySelector("esp-app");
    if (app && window.customElements && customElements.get("esp-app")) app.scheme = effTheme();
  }
  function applyTheme() {
    var root = document.documentElement;
    if (theme === "auto") root.removeAttribute("data-ms-theme"); else root.setAttribute("data-ms-theme", theme);
    root.style.colorScheme = theme === "auto" ? "light dark" : theme;
    var b = $("ms-theme");
    b.innerHTML = ICON[theme];
    b.title = "Тема: " + NAME[theme];
    b.setAttribute("aria-label", b.title);
    var dark = effTheme() === "dark";
    Array.prototype.forEach.call(document.querySelectorAll('meta[name="theme-color"]'), function (m) {
      m.setAttribute("content", theme === "auto" ? (m.media.indexOf("dark") >= 0 ? "#0b0d12" : "#f4f5f8") : dark ? "#0b0d12" : "#f4f5f8");
    });
    syncEspApp();
  }
  $("ms-theme").addEventListener("click", function () {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    try { localStorage.setItem("ms-theme", theme); } catch (e) { /* нет хранилища */ }
    applyTheme();
  });
  if (darkMq.addEventListener) darkMq.addEventListener("change", function () { if (theme === "auto") applyTheme(); });

  // ── вкладки: «Датчик» (эта панель) / «Устройство» (штатный ESPHome v3, запускается по первому клику) ──
  function showTab(t) {
    var dev = t === "dev";
    Array.prototype.forEach.call(document.querySelectorAll("#ms .tabs button"), function (b) {
      var on = b.dataset.t === t; b.classList.toggle("on", on); b.setAttribute("aria-selected", on);
    });
    $("ms").classList.toggle("dev", dev);
    if (dev && window.MS_espApp) {
      window.MS_espApp();
      useShared();
      if (window.customElements) customElements.whenDefined("esp-app").then(function () { setTimeout(syncEspApp, 0); });
    }
    var app = document.querySelector("esp-app");
    if (app) app.style.display = dev ? "" : "none";
    try { sessionStorage.setItem("ms-tab", t); } catch (e) { /* не помним */ }
  }
  Array.prototype.forEach.call(document.querySelectorAll("#ms .tabs button"), function (b) {
    b.addEventListener("click", function () { showTab(b.dataset.t); });
  });

  // ── старт ──
  txt("ms-host", location.hostname || "move-sensor-ble.local");
  applyTheme();
  var startTab = "main";
  try { startTab = sessionStorage.getItem("ms-tab") || "main"; } catch (e) { /* нет */ }
  if (startTab === "dev") showTab("dev"); else openOwn();
  setInterval(function () { renderOnline(); renderMotion(); txt("ms-upd", lastUpd ? hms(lastUpd) : "—"); }, 1000);
  renderOnline();
  render();
  if (location.hash === "#wifi") openWifi();  // ссылка «Wi-Fi роутера» с лендинга точки доступа
})();
