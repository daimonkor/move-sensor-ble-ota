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
    occ: "binary_sensor/Occupancy", svc: "binary_sensor/Service mode",
    temp: "sensor/Temperature", hum: "sensor/Humidity", bat: "sensor/Battery", volt: "sensor/Voltage",
    d0: "sensor/D0 raw voltage", wakes: "sensor/Wake count",
    wPct: "sensor/WiFi Signal", wDbm: "sensor/WiFi RSSI", bPct: "sensor/BLE Signal", bDbm: "sensor/BLE RSSI",
    why: "text_sensor/Wake reason", esv: "text_sensor/ESPHome Version", build: "text_sensor/Build Version",
    keep: "number/Keep time", delay: "number/Detect delay", clim: "number/Climate report interval", vf: "number/Voltage factor",
    led: "switch/Motion tracking LED",
    // 2026-11-15: настройки, которые HA шлёт по BLE, + батарея в покое и IP (раньше на сайте не было)
    mwin: "number/Motion window", cwin: "number/Climate window",
    toff: "number/Temperature offset", hoff: "number/Humidity offset",
    rest: "sensor/Battery rest voltage", ip: "text_sensor/WiFi IP", devid: "text_sensor/Device ID",
    pir: "binary_sensor/PIR raw", bo: "sensor/Brownout count", crash: "sensor/Crash count",
    rlast: "text_sensor/Last reset reason"
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
    if (s < 60) return t("sec", s);
    var m = Math.floor(s / 60); if (m < 60) return t("min", m);
    var h = Math.floor(m / 60); return t("hmin", h, m % 60);
  }
  function hms(ts) { var d = new Date(ts); return d.toLocaleTimeString(t("locale"), { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }

  // ── языки сайта (2026-11-15): EN / UA / RU, переключатель в шапке. Ключ — data-i в panel.html
  // (текст), data-it (title + aria-label) или t("key") в коде. {0}, {1} — подстановки. ──
  var I18N = {
    en: {
      langName: "English", langBtn: "Language: English",
      snd: "Motion sound", sndOff: "Motion sound: off", sndWait: "Sound waits for a click — tap to enable", sndOn: "Motion sound: on",
      wifiTitle: "Router Wi-Fi", help: "User manual", close: "Close", tabs: "Tabs", tabMain: "Sensor", tabDev: "Device",
      theme: "Theme: {0}", thAuto: "auto", thLight: "light", thDark: "dark",
      devnote: "If the sensor works over BLE (on battery), its settings (Keep time, Detect delay, offsets, windows, LED) are set by the Move Sensor BLE integration in HA — it may restore its value at the next wake-up. Over Wi-Fi (WiFi on, USB power) change them here or in HA on the ESPHome device.",
      svcbar: "The WiFi switch is on — the sensor does not sleep. On battery it drains faster: switch WiFi off after setup. On USB you can leave it on — the sensor then works over Wi-Fi.",
      motion: "Motion", temp: "Temperature", hum: "Humidity", bat: "Battery", batLow: "low battery", batNote: " V · LiPo 920 mAh", sig: "Signal",
      dev: "Device", dSvc: "Service mode", dClim: "Climate report", dToff: "Temperature offset", dHoff: "Humidity offset",
      dLed: "Motion LED", dIp: "IP address", dBuild: "Build", dUpd: "Updated",
      dnote: "On battery, data and settings go over BLE (integration in HA); with WiFi on — also over Wi-Fi (ESPHome device in HA).",
      dbg: "Debug", dbgBadge: "service", pirRaw: "Raw PIR", pirSub: "SR602 output before Detect delay and Keep time",
      pLast: "Last PIR pulse", pCnt: "Pulses since opened", pMm: "Shortest / longest", pWhy: "Wake reason", pWakes: "Wake-ups",
      pRst: "Resets: brownout / crashes", pRstLast: "Last reset", pMwin: "Motion window (BLE)", pCwin: "Climate window (BLE)",
      pRest: "Battery at rest", pD0: "Voltage on D0",
      wScan: "Find networks", wNet: "Network", wPass: "Password",
      wHint: "Empty \"Network\" field — reconnect to the current one. Not connected within 20 s — the sensor returns to the previous network.",
      wReset: "Firmware network", apHead: "Sensor access point password", apNew: "New password", apGo: "Change AP password",
      apHint: "8–63 Latin characters. After the change the sensor reboots (~5 s) and your phone disconnects — reconnect with the new password.",
      apReset: "Factory reset without the site: start with WiFi off, switch it on 3 times within 10 s and leave the 3rd on — firmware network and default AP password, LED flashes 6 times.",
      occYes: "Motion detected", occNo: "Clear", occSubOn: "Clears {0} after the PIR goes low", occSubLast: "Last motion {0} ago", occSubNone: "No motion",
      sec: "{0} s", min: "{0} min", hmin: "{0} h {1} min", ms: "{0} ms", keepChip: "Keep time {0}", delayChip: "Detect delay {0}",
      cool: "Cool", comfy: "Comfortable", hot: "Hot", dry: "Dry", normal: "Normal", humid: "Humid", offset: " · offset {0}",
      noHa: "no data from HA", on: "on", off: "off", climEvery: "every {0} min", volt: "{0} V",
      whyCold: "cold start", whyTimer: "climate timer", whySvc: "service switch", whyPir: "PIR (motion)",
      rrPower: "power on", rrPin: "RESET button", rrSw: "software (OTA/restart)", rrPanic: "panic (crash)",
      rrWdtInt: "watchdog (interrupts)", rrWdtTask: "watchdog (task)", rrBrown: "brownout (supply dip)",
      pirHigh: "HIGH (signal)", pirGoing: "running {0}",
      onl: "Online", onlLost: "No connection — reconnecting…", onlConn: "Connecting…",
      wConnect: "Connect to network", wReconnect: "Reconnect", apCustom: "Now: own password", apDefault: "Now: default password (from the label)",
      wNow: "Now: {0}", wNoRouter: "Not connected to the router", wNoNets: "No networks yet — \"Find networks\"",
      wDone: "Done: {0}", wFail: "Did not connect — the previous network is kept", wLost: "Lost connection to the sensor — check the network",
      wSent: "Sent, connecting…", wSendFail: "Could not send", wScanning: "Searching for networks…", wNoFound: "No networks found — try again",
      sure: "Sure? Tap again", apSure: "Change for sure? Tap again", saving: "Saving…",
      apSaved: "AP password saved. The sensor reboots (~5 s) — reconnect to the access point with the new password",
      apEmptyField: "AP password NOT changed: the \"New AP Password\" field is empty — type the password, press Enter, then \"Save AP password\"",
      apLen: "AP password NOT changed: {0} characters, need 8–63", apChars: "AP password NOT changed: only Latin letters, digits and symbols",
      apEnter: "Enter a new access point password", apBad: "Password not accepted: need 8–63 Latin characters (letters, digits, symbols, no Cyrillic)",
      apNow: " — now {0}", apSendFail: "Could not send the password to the sensor", locale: "en-GB"
    },
    uk: {
      langName: "Українська", langBtn: "Мова: українська",
      snd: "Звук при русі", sndOff: "Звук при русі: вимкнено", sndWait: "Звук чекає на дотик — натисніть, щоб увімкнути", sndOn: "Звук при русі: увімкнено",
      wifiTitle: "Wi-Fi роутера", help: "Інструкція", close: "Закрити", tabs: "Вкладки", tabMain: "Датчик", tabDev: "Пристрій",
      theme: "Тема: {0}", thAuto: "авто", thLight: "світла", thDark: "темна",
      devnote: "Якщо датчик працює по BLE (від батареї), його налаштування (Keep time, Detect delay, поправки, вікна, LED) задає інтеграція Move Sensor BLE в HA — вона може повернути своє значення при наступному пробудженні. По Wi-Fi (WiFi увімкнено, живлення від USB) змінюйте тут або в HA на пристрої ESPHome.",
      svcbar: "Увімкнено перемикач WiFi — датчик не спить. Від батареї вона сідає швидше: після налаштування вимкніть WiFi. Від USB можна залишити — тоді датчик працює по Wi-Fi.",
      motion: "Рух", temp: "Температура", hum: "Вологість", bat: "Батарея", batLow: "низький заряд", batNote: " В · LiPo 920 мА·год", sig: "Сигнал",
      dev: "Пристрій", dSvc: "Сервісний режим", dClim: "Звіт клімату", dToff: "Поправка температури", dHoff: "Поправка вологості",
      dLed: "LED спрацювань", dIp: "IP у мережі", dBuild: "Збірка", dUpd: "Оновлено",
      dnote: "Від батареї дані й налаштування йдуть по BLE (інтеграція в HA), з увімкненим WiFi — ще й по Wi-Fi (пристрій ESPHome в HA).",
      dbg: "Налагодження", dbgBadge: "сервіс", pirRaw: "Сирий PIR", pirSub: "вихід SR602 до Detect delay і Keep time",
      pLast: "Останній імпульс PIR", pCnt: "Імпульсів з відкриття", pMm: "Найкоротший / найдовший", pWhy: "Причина пробудження", pWakes: "Пробуджень",
      pRst: "Скидання: brownout / падіння", pRstLast: "Останнє скидання", pMwin: "Вікно руху (BLE)", pCwin: "Вікно клімату (BLE)",
      pRest: "Батарея в спокої", pD0: "Напруга на D0",
      wScan: "Знайти мережі", wNet: "Мережа", wPass: "Пароль",
      wHint: "Порожнє поле «Мережа» — перепідключитися до поточної. Не підключилося за 20 с — датчик сам повернеться до попередньої мережі.",
      wReset: "Мережа з прошивки", apHead: "Пароль точки доступу датчика", apNew: "Новий пароль", apGo: "Змінити пароль точки",
      apHint: "8–63 латинські символи. Після зміни датчик перезавантажиться (~5 с), телефон відключиться від точки — підключіться знову з новим паролем.",
      apReset: "Заводське скидання без сайту: почніть з вимкненого WiFi, увімкніть його 3 рази за 10 с і після 3-го не вимикайте — мережа з прошивки та заводський пароль точки, LED блимне 6 разів.",
      occYes: "Є рух", occNo: "Тихо", occSubOn: "Звільниться через {0} після спаду PIR", occSubLast: "Останній рух {0} тому", occSubNone: "Руху немає",
      sec: "{0} с", min: "{0} хв", hmin: "{0} год {1} хв", ms: "{0} мс", keepChip: "Keep time {0}", delayChip: "Detect delay {0}",
      cool: "Прохолодно", comfy: "Комфортно", hot: "Спекотно", dry: "Сухо", normal: "Норма", humid: "Волого", offset: " · поправка {0}",
      noHa: "немає даних з HA", on: "увімкнено", off: "вимкнено", climEvery: "кожні {0} хв", volt: "{0} В",
      whyCold: "холодний старт", whyTimer: "таймер клімату", whySvc: "сервісний перемикач", whyPir: "PIR (рух)",
      rrPower: "увімкнення живлення", rrPin: "кнопка RESET", rrSw: "програмне (OTA/перезапуск)", rrPanic: "panic (падіння)",
      rrWdtInt: "watchdog (переривання)", rrWdtTask: "watchdog (задача)", rrBrown: "brownout (просідання живлення)",
      pirHigh: "HIGH (є сигнал)", pirGoing: "триває {0}",
      onl: "У мережі", onlLost: "Немає зв'язку — перепідключення…", onlConn: "Підключення…",
      wConnect: "Підключитися до мережі", wReconnect: "Перепідключитися", apCustom: "Зараз: свій пароль", apDefault: "Зараз: заводський пароль (з наклейки)",
      wNow: "Зараз: {0}", wNoRouter: "До роутера не підключено", wNoNets: "Мереж поки немає — «Знайти мережі»",
      wDone: "Готово: {0}", wFail: "Не підключилося — залишилася попередня мережа", wLost: "Зв'язок із датчиком втрачено — перевірте мережу",
      wSent: "Надіслано, підключаюся…", wSendFail: "Не вдалося надіслати", wScanning: "Шукаю мережі…", wNoFound: "Мережі не знайдено — спробуйте ще раз",
      sure: "Точно? Натисніть ще раз", apSure: "Точно змінити? Натисніть ще раз", saving: "Зберігаю…",
      apSaved: "Пароль точки збережено. Датчик перезавантажиться (~5 с) — підключіться до точки знову з новим паролем",
      apEmptyField: "Пароль точки НЕ змінено: поле «New AP Password» порожнє — введіть пароль, натисніть Enter, потім «Save AP password»",
      apLen: "Пароль точки НЕ змінено: {0} символів, потрібно 8–63", apChars: "Пароль точки НЕ змінено: лише латинські літери, цифри та знаки",
      apEnter: "Введіть новий пароль точки", apBad: "Пароль не прийнято: потрібно 8–63 латинські символи (літери, цифри, знаки, без кирилиці)",
      apNow: " — зараз {0}", apSendFail: "Не вдалося надіслати пароль на датчик", locale: "uk-UA"
    },
    ru: {
      langName: "Русский", langBtn: "Язык: русский",
      snd: "Звук при движении", sndOff: "Звук при движении: выключен", sndWait: "Звук ждёт клика — нажмите, чтобы включить", sndOn: "Звук при движении: включён",
      wifiTitle: "Wi-Fi роутера", help: "Инструкция", close: "Закрыть", tabs: "Вкладки", tabMain: "Датчик", tabDev: "Устройство",
      theme: "Тема: {0}", thAuto: "авто", thLight: "светлая", thDark: "тёмная",
      devnote: "Если датчик работает по BLE (на батарее), его настройки (Keep time, Detect delay, поправки, окна, LED) задаёт интеграция Move Sensor BLE в HA — она может вернуть своё значение при следующем пробуждении. По Wi-Fi (WiFi включён, питание от USB) меняйте здесь или в HA на устройстве ESPHome.",
      svcbar: "Включён переключатель WiFi — датчик не спит. От батареи она садится быстрее: после настройки выключите WiFi. От USB можно оставить — тогда датчик работает по Wi-Fi.",
      motion: "Движение", temp: "Температура", hum: "Влажность", bat: "Батарея", batLow: "низкий заряд", batNote: " В · LiPo 920 мА·ч", sig: "Сигнал",
      dev: "Устройство", dSvc: "Сервисный режим", dClim: "Отчёт климата", dToff: "Поправка температуры", dHoff: "Поправка влажности",
      dLed: "LED срабатываний", dIp: "IP в сети", dBuild: "Сборка", dUpd: "Обновлено",
      dnote: "На батарее данные и настройки идут по BLE (интеграция в HA), с включённым WiFi — ещё и по Wi-Fi (устройство ESPHome в HA).",
      dbg: "Отладка", dbgBadge: "сервис", pirRaw: "PIR сырой", pirSub: "выход SR602 до Detect delay и Keep time",
      pLast: "Последний импульс PIR", pCnt: "Импульсов с открытия", pMm: "Самый короткий / длинный", pWhy: "Причина пробуждения", pWakes: "Пробуждений",
      pRst: "Сбросы: brownout / падения", pRstLast: "Последний сброс", pMwin: "Окно движения (BLE)", pCwin: "Окно климата (BLE)",
      pRest: "Батарея в покое", pD0: "Напряжение на D0",
      wScan: "Найти сети", wNet: "Сеть", wPass: "Пароль",
      wHint: "Пустое поле «Сеть» — переподключиться к текущей. Не подключилось за 20 с — датчик сам вернётся к прежней сети.",
      wReset: "Сеть из прошивки", apHead: "Пароль точки доступа датчика", apNew: "Новый пароль", apGo: "Сменить пароль точки",
      apHint: "8–63 латинских символа. После смены датчик перезагрузится (~5 с), телефон отключится от точки — подключитесь заново с новым паролем.",
      apReset: "Заводской сброс без сайта: начните с выключенного WiFi, включите его 3 раза за 10 с и после 3-го не выключайте — сеть из прошивки и заводской пароль точки, LED мигнёт 6 раз.",
      occYes: "Есть движение", occNo: "Тихо", occSubOn: "Освободится через {0} после спада PIR", occSubLast: "Последнее движение {0} назад", occSubNone: "Движения нет",
      sec: "{0} с", min: "{0} мин", hmin: "{0} ч {1} мин", ms: "{0} мс", keepChip: "Keep time {0}", delayChip: "Detect delay {0}",
      cool: "Прохладно", comfy: "Комфортно", hot: "Жарко", dry: "Сухо", normal: "Норма", humid: "Влажно", offset: " · поправка {0}",
      noHa: "нет данных из HA", on: "включён", off: "выключен", climEvery: "каждые {0} мин", volt: "{0} В",
      whyCold: "холодный старт", whyTimer: "таймер климата", whySvc: "сервисный переключатель", whyPir: "PIR (движение)",
      rrPower: "включение питания", rrPin: "кнопка RESET", rrSw: "программный (OTA/перезапуск)", rrPanic: "panic (падение)",
      rrWdtInt: "watchdog (прерывания)", rrWdtTask: "watchdog (задача)", rrBrown: "brownout (просадка питания)",
      pirHigh: "HIGH (есть сигнал)", pirGoing: "идёт {0}",
      onl: "В сети", onlLost: "Нет связи — переподключение…", onlConn: "Подключение…",
      wConnect: "Подключиться к сети", wReconnect: "Переподключиться", apCustom: "Сейчас: свой пароль", apDefault: "Сейчас: заводской пароль (с наклейки)",
      wNow: "Сейчас: {0}", wNoRouter: "К роутеру не подключён", wNoNets: "Сетей пока нет — «Найти сети»",
      wDone: "Готово: {0}", wFail: "Не подключилось — осталась прежняя сеть", wLost: "Связь с датчиком потеряна — проверьте сеть",
      wSent: "Отправлено, подключаюсь…", wSendFail: "Не удалось отправить", wScanning: "Ищу сети…", wNoFound: "Сети не найдены — попробуйте ещё раз",
      sure: "Точно? Нажмите ещё раз", apSure: "Точно сменить? Нажмите ещё раз", saving: "Сохраняю…",
      apSaved: "Пароль точки сохранён. Датчик перезагрузится (~5 с) — подключитесь к точке заново с новым паролем",
      apEmptyField: "Пароль точки НЕ изменён: поле «New AP Password» пустое — введите пароль, нажмите Enter, потом «Save AP password»",
      apLen: "Пароль точки НЕ изменён: {0} символов, нужно 8–63", apChars: "Пароль точки НЕ изменён: только латинские буквы, цифры и знаки",
      apEnter: "Введите новый пароль точки", apBad: "Пароль не принят: нужно 8–63 латинских символа (буквы, цифры, знаки, без кириллицы)",
      apNow: " — сейчас {0}", apSendFail: "Не удалось отправить пароль на датчик", locale: "ru-RU"
    }
  };
  var LANGS = ["en", "uk", "ru"], LCODE = { en: "EN", uk: "UA", ru: "RU" };
  var LANG = null;
  try { LANG = localStorage.getItem("ms-lang"); } catch (e) { /* нет хранилища */ }
  if (LANGS.indexOf(LANG) < 0) {
    var nl = (navigator.language || "en").slice(0, 2).toLowerCase();
    LANG = nl === "uk" ? "uk" : nl === "ru" ? "ru" : "en";
  }
  function t(k) {
    var s = (I18N[LANG] && I18N[LANG][k]) || I18N.en[k] || k, a = arguments;
    return s.replace(/\{(\d)\}/g, function (m, i) { return a[+i + 1] !== undefined ? a[+i + 1] : m; });
  }
  // статичные подписи из panel.html: data-i — текст, data-it — title + aria-label
  function applyStatic() {
    document.documentElement.lang = LANG;
    Array.prototype.forEach.call(document.querySelectorAll("#ms [data-i]"), function (e) { e.textContent = t(e.getAttribute("data-i")); });
    Array.prototype.forEach.call(document.querySelectorAll("#ms [data-it]"), function (e) {
      var s = t(e.getAttribute("data-it")); e.title = s; e.setAttribute("aria-label", s);
    });
    var lb = $("ms-lang"); lb.textContent = LCODE[LANG]; lb.title = t("langBtn"); lb.setAttribute("aria-label", t("langBtn"));
  }

  var WHY = { "cold boot": "whyCold", "timer": "whyTimer", "service button": "whySvc", "pir": "whyPir" };

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
    b.title = !sndOn ? t("sndOff") : wait ? t("sndWait") : t("sndOn");
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
    txt("ms-occ", occ === null ? "—" : occ ? t("occYes") : t("occNo"));
    var keep = num(K.keep), del = num(K.delay);
    if (occ === true) txt("ms-occsub", t("occSubOn", keep === null ? "keep time" : t("sec", keep)));
    else if (occ === false) txt("ms-occsub", lastMotion ? t("occSubLast", ago(Date.now() - lastMotion)) : t("occSubNone"));
    else txt("ms-occsub", " ");
    txt("ms-keep", t("keepChip", keep === null ? "—" : t("sec", keep)));
    txt("ms-delay", t("delayChip", del === null ? "—" : t("ms", del)));
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
    var tc = num(K.temp), h = num(K.hum);
    txt("ms-temp", fmt(tc, 1));
    var toff = num(K.toff), hoff = num(K.hoff);
    var offTxt = function (v, u) { return v ? t("offset", (v > 0 ? "+" : "−") + Math.abs(v).toFixed(1) + " " + u) : ""; };
    txt("ms-temp-n", tc === null ? " " : (tc < 18 ? t("cool") : tc <= 26 ? t("comfy") : t("hot")) + offTxt(toff, "°C"));
    pin("ms-temp-pin", tc === null ? null : (tc - 10) / 25 * 100, tc === null ? "" : tc < 18 ? "cold" : tc <= 26 ? "ok" : "hot");
    txt("ms-hum", fmt(h, 0));
    txt("ms-hum-n", h === null ? " " : (h < 30 ? t("dry") : h <= 60 ? t("normal") : t("humid")) + offTxt(hoff, "%"));
    pin("ms-hum-pin", h, h === null ? "" : h < 30 ? "dry" : h <= 60 ? "ok" : "wet");
  }

  function renderBattery() {
    var p = num(K.bat), v = num(K.volt), low = p !== null && p <= 10, card = $("ms-batcard");
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
    txt("ms-bdbm", bd === null ? t("noHa") : Math.round(bd) + " dBm");
  }

  function renderDiag() {
    var why = str(K.why), svc = bool(K.svc), clim = num(K.clim), led = bool(K.led), d0 = num(K.d0), vf = num(K.vf), w = num(K.wakes);
    txt("ms-why", why ? (WHY[why] ? t(WHY[why]) : why) : "—");
    txt("ms-wakes", w === null ? "—" : String(Math.round(w)));
    txt("ms-svc", svc === null ? "—" : svc ? t("on") : t("off"));
    $("ms-svcbar").classList.toggle("hide", svc !== true);
    renderDbg();
    txt("ms-clim", clim === null ? "—" : clim < 1 ? t("off") : t("climEvery", clim));
    txt("ms-d0", d0 === null ? "—" : t("volt", d0.toFixed(3)));
    txt("ms-vf", vf === null ? "—" : vf.toFixed(3));
    txt("ms-led", led === null ? "—" : led ? t("on") : t("off"));
    txt("ms-devid", str(K.devid) || "—");
    var mwin = num(K.mwin), cwin = num(K.cwin), toff = num(K.toff), hoff = num(K.hoff), rest = num(K.rest);
    var sgn = function (v, d) { return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d); };
    txt("ms-toff", toff === null ? "—" : sgn(toff, 1) + " °C");
    txt("ms-hoff", hoff === null ? "—" : sgn(hoff, 1) + " %");
    txt("ms-mwin", mwin === null ? "—" : t("sec", (mwin / 1000).toFixed(1)));
    txt("ms-cwin", cwin === null ? "—" : t("sec", (cwin / 1000).toFixed(1)));
    txt("ms-rest", rest === null ? "—" : t("volt", rest.toFixed(3)));
    txt("ms-ip", str(K.ip) || "—");
    txt("ms-esv", str(K.esv) || "—");
    txt("ms-build", str(K.build) || "—");
    txt("ms-upd", lastUpd ? hms(lastUpd) : "—");
  }

  // ── отладка (скрыта: 5 нажатий на подпись внизу или /#debug) ──
  var pirPrev = null, pirRise = 0, pirLast = null, pirCnt = 0, pirMin = null, pirMax = null;
  var RR = { "power on": "rrPower", "reset pin": "rrPin", "software": "rrSw", "panic": "rrPanic",
    "watchdog (int)": "rrWdtInt", "watchdog (task)": "rrWdtTask", "brownout": "rrBrown" };
  function sec(ms) { return ms < 1000 ? t("ms", ms) : t("sec", (ms / 1000).toFixed(1)); }
  function renderDbg() {
    var p = bool(K.pir), now = Date.now();
    if (p === true && pirPrev !== true) pirRise = now;
    if (p === false && pirPrev === true && pirRise) {
      pirLast = now - pirRise; pirCnt++;
      pirMin = pirMin === null ? pirLast : Math.min(pirMin, pirLast);
      pirMax = pirMax === null ? pirLast : Math.max(pirMax, pirLast);
    }
    pirPrev = p;
    if ($("ms-dbg").classList.contains("hide")) return;
    $("ms-pirlamp").classList.toggle("on", p === true);
    txt("ms-pir", p === null ? "—" : p ? t("pirHigh") : "LOW");
    txt("ms-pirlast", p === true ? t("pirGoing", sec(now - pirRise)) : pirLast === null ? "—" : sec(pirLast));
    txt("ms-pircnt", String(pirCnt));
    txt("ms-pirmm", pirMin === null ? "—" : sec(pirMin) + " / " + sec(pirMax));
    var bo = num(K.bo), cr = num(K.crash);
    txt("ms-rst", bo === null ? "—" : bo + " / " + cr);
    $("ms-rst").classList.toggle("bad", (bo || 0) + (cr || 0) > 0);
    var rl = str(K.rlast);
    txt("ms-rstlast", rl ? (RR[rl] ? t(RR[rl]) : rl) : "—");
  }
  var dbgTaps = [];
  function setDbg(on) {
    $("ms-dbg").classList.toggle("hide", !on);
    try { localStorage.setItem("ms-debug", on ? "1" : ""); } catch (e) { /* без хранилища — до перезагрузки */ }
    if (on) renderDbg();
  }
  $("ms-foot").addEventListener("click", function () {
    var now = Date.now();
    dbgTaps = dbgTaps.filter(function (t) { return now - t < 3000; }); dbgTaps.push(now);
    if (dbgTaps.length >= 5) { dbgTaps = []; setDbg($("ms-dbg").classList.contains("hide")); }
  });
  window.addEventListener("hashchange", function () { if (location.hash === "#debug") setDbg(true); });
  try { if (localStorage.getItem("ms-debug") === "1" || location.hash === "#debug") setDbg(true); } catch (e) { if (location.hash === "#debug") setDbg(true); }

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
    txt("ms-onl-t", on ? t("onl") : lastMsg ? t("onlLost") : t("onlConn"));
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
    if (id === "text_sensor/AP password status") apStatus(j.state);
    render();
  }
  // имя из прошивки: «Move Sensor BLE 148f98» — хвост MAC (Device ID) показываем заглавными (2026-11-15)
  function onPing(e) {
    lastMsg = Date.now();
    try {
      var t = JSON.parse(e.data).title;
      if (t) txt("ms-name", t.replace(/([0-9a-f]{6})$/i, function (m) { return m.toUpperCase(); }));
    } catch (_) {}
  }

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
  function syncGo() { txt("ms-wgo", $("ms-wssid").value.trim() ? t("wConnect") : t("wReconnect")); }
  function parseNets(text) {  // "SSID: -63dB" по строке (wifi_info scan_results)
    var out = [], seen = {};
    (text || "").split("\n").forEach(function (l) {
      var m = /^(.*): (-?\d+)dB$/.exec(l.trim());
      if (m && m[1] && !seen[m[1]]) { seen[m[1]] = 1; out.push({ ssid: m[1], rssi: parseInt(m[2], 10) }); }
    });
    return out.sort(function (a, b) { return b.rssi - a.rssi; });
  }
  var netsKey = null;
  function renderAp() {  // «AP password»: default / custom (wifi_setup.yaml)
    var m = str("text_sensor/AP password");
    txt("ms-apnow", m === "custom" ? t("apCustom") : m === "default" ? t("apDefault") : "—");
  }
  function renderWifi() {
    if ($("ms-wsheet").classList.contains("hide")) return;
    renderAp();
    var now = str("text_sensor/Wi-Fi SSID");
    txt("ms-wnow", now ? t("wNow", now) : t("wNoRouter"));
    var raw = str("text_sensor/Wi-Fi Scan Results") || "", cur = $("ms-wssid").value.trim();
    if (raw + "|" + cur === netsKey) return;
    netsKey = raw + "|" + cur;
    var box = $("ms-nets"), nets = parseNets(raw);
    box.textContent = "";
    if (!nets.length) { var e = document.createElement("div"); e.className = "none"; e.textContent = t("wNoNets"); box.appendChild(e); return; }
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
          setWBusy(false); var ok = t("wDone", now); wstat(ok);
          // успех — показать «Готово» 2 с и закрыть окно (если его не открыли/не трогали заново)
          setTimeout(function () { if (!wBusy && $("ms-wstat").textContent === ok) closeWifi(); }, 2000);
        }
        else if (Date.now() - t0 < 45000) wait();
        else { setWBusy(false); wstat(lastMsg > t0 + 5000 ? t("wFail") : t("wLost")); }
      }, 2500);
    })();
  }
  $("ms-wgo").addEventListener("click", function () {
    if (wBusy) return;
    var ssid = $("ms-wssid").value.trim(), pass = $("ms-wpass").value;
    wstat(t("wSent")); setWBusy(true);
    postText("New Wi-Fi SSID", ssid)
      .then(function () { return postText("New Wi-Fi Password", ssid ? pass : ""); })
      .then(function () { return press("Reconnect to router"); })
      .then(function () { $("ms-wpass").value = ""; waitBack(Date.now(), ssid); },
            function () { setWBusy(false); wstat(t("wSendFail")); });
  });
  $("ms-wscan").addEventListener("click", function () {
    if (wBusy) return;
    var before = str("text_sensor/Wi-Fi Scan Results"), t0 = Date.now();
    wstat(t("wScanning")); setWBusy(true);
    press("Scan Wi-Fi networks").then(function () {
      (function wait() {
        setTimeout(function () {
          // фоновый скан ~2–4 с; тот же список — результат не меняется, ждём не дольше 10 с
          var nowRes = str("text_sensor/Wi-Fi Scan Results");
          if (nowRes !== before || Date.now() - t0 > 10000) {
            setWBusy(false); netsKey = null; renderWifi();
            wstat(nowRes ? "" : t("wNoFound"));
          }
          else wait();
        }, 1000);
      })();
    }, function () { setWBusy(false); wstat(t("wSendFail")); });
  });
  $("ms-wreset").addEventListener("click", function () {  // два нажатия вместо confirm()
    var b = $("ms-wreset");
    if (Date.now() > resetArmed) {
      resetArmed = Date.now() + 4000; b.classList.add("warn"); b.textContent = t("sure");
      setTimeout(function () { if (Date.now() > resetArmed) { b.classList.remove("warn"); b.textContent = t("wReset"); } }, 4100);
      return;
    }
    resetArmed = 0; b.classList.remove("warn"); b.textContent = t("wReset");
    wstat(t("wSent")); setWBusy(true);
    press("Reset Wi-Fi to firmware defaults").then(function () { waitBack(Date.now(), ""); },
      function () { setWBusy(false); wstat(t("wSendFail")); });
  });
  // ── тост: заметное сообщение на 6 с (ошибка — красный) ──
  var toastT = 0;
  function toast(msg, bad) {
    var t = $("ms-toast");
    t.textContent = msg; t.classList.toggle("bad", !!bad); t.classList.remove("hide");
    clearTimeout(toastT); toastT = setTimeout(function () { t.classList.add("hide"); }, 6000);
  }
  $("ms-toast").addEventListener("click", function () { this.classList.add("hide"); });

  // Результат «Save AP password» от датчика — тостом, в том числе когда сохраняли на штатной
  // вкладке «Устройство» или из HA. Значения первых секунд после загрузки не показываем; своё нажатие
  // на панели уже показало тост — его не дублируем.
  var apStReady = Date.now() + 4000, apSelfT = 0;   // первые 4 с — начальные состояния SSE
  function apStatus(v) {
    if (Date.now() < apStReady) return;
    if (!v || Date.now() - apSelfT < 5000) return;
    var m = /^rejected: (\d+) characters/.exec(v);
    if (v.indexOf("saved") === 0) toast(t("apSaved"), false);
    else if (v === "rejected: field is empty") toast(t("apEmptyField"), true);
    else if (m) toast(t("apLen", m[1]), true);
    else if (v.indexOf("rejected") === 0) toast(t("apChars"), true);
  }

  // ── пароль своей точки доступа: «New AP Password» → «Save AP password», два нажатия ──
  var apArmed = 0;
  $("ms-apgo").addEventListener("click", function () {
    if (wBusy) return;
    var b = $("ms-apgo"), p = $("ms-appass").value;
    if (!p) { var m0 = t("apEnter"); wstat(m0); toast(m0, true); return; }
    if (p.length < 8 || p.length > 63 || !/^[\x20-\x7e]+$/.test(p)) {
      var m1 = t("apBad") + (p.length < 8 ? t("apNow", p.length) : "");
      wstat(m1); toast(m1, true); return;
    }
    if (Date.now() > apArmed) {
      apArmed = Date.now() + 4000; b.classList.add("warn"); b.textContent = t("apSure");
      setTimeout(function () { if (Date.now() > apArmed) { b.classList.remove("warn"); b.textContent = t("apGo"); } }, 4100);
      return;
    }
    apArmed = 0; b.classList.remove("warn"); b.textContent = t("apGo");
    wstat(t("saving")); setWBusy(true); apSelfT = Date.now();
    postText("New AP Password", p)
      .then(function () { return press("Save AP password"); })
      .then(function () {
        $("ms-appass").value = ""; setWBusy(false);
        var ok = t("apSaved");
        wstat(ok); toast(ok, false);
      }, function () { setWBusy(false); wstat(t("wSendFail")); toast(t("apSendFail"), true); });
  });
  window.addEventListener("hashchange", function () { if (location.hash === "#wifi") openWifi(); });

  // ── тема: авто → светлая → тёмная (как у весов), одна на панель и штатный интерфейс ──
  var THEMES = ["auto", "light", "dark"];
  var ICON = {
    auto: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3v18" /><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/></svg>',
    light: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
    dark: '<svg viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/></svg>'
  };
  var NAME = { auto: "thAuto", light: "thLight", dark: "thDark" };
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
    b.title = t("theme", t(NAME[theme]));
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

  // ── инструкция пользователя (кнопка «?»): статья текущего языка из window.MS_MANUAL ──
  function showManLang() {
    Array.prototype.forEach.call(document.querySelectorAll("#ms-hbody article"), function (a) { a.style.display = a.lang === LANG ? "" : "none"; });
  }
  function openHelp() {
    var body = $("ms-hbody");
    if (!body.firstChild && window.MS_MANUAL) body.innerHTML = window.MS_MANUAL;
    showManLang();
    $("ms-hsheet").classList.remove("hide");
    var box = document.querySelector("#ms-hsheet .sbox"); if (box) box.scrollTop = 0;
  }
  function closeHelp() { $("ms-hsheet").classList.add("hide"); if (location.hash === "#help") history.replaceState(null, "", location.pathname); }
  $("ms-help").addEventListener("click", openHelp);
  $("ms-hclose").addEventListener("click", closeHelp);
  $("ms-hsheet").addEventListener("click", function (e) {
    if (e.target === $("ms-hsheet")) closeHelp();
    // ссылки оглавления: прокрутка внутри окна, адрес страницы не трогаем
    var a = e.target.closest && e.target.closest("a[href^='#']");
    if (a) { e.preventDefault(); var el = document.getElementById(a.getAttribute("href").slice(1)); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); }
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeHelp(); });
  window.addEventListener("hashchange", function () { if (location.hash === "#help") openHelp(); });

  // ── язык: EN → UA → RU по кругу, выбор помнит браузер ──
  function applyLang() {
    applyStatic(); syncSnd(); applyTheme(); syncGo(); renderOnline(); showManLang();
    netsKey = null; render(); txt("ms-upd", lastUpd ? hms(lastUpd) : "—");
  }
  $("ms-lang").addEventListener("click", function () {
    LANG = LANGS[(LANGS.indexOf(LANG) + 1) % LANGS.length];
    try { localStorage.setItem("ms-lang", LANG); } catch (e) { /* нет хранилища */ }
    applyLang();
  });

  // ── старт ──
  txt("ms-host", location.hostname || "move-sensor-ble.local");
  applyStatic();
  applyTheme();
  var startTab = "main";
  try { startTab = sessionStorage.getItem("ms-tab") || "main"; } catch (e) { /* нет */ }
  if (startTab === "dev") showTab("dev"); else openOwn();
  setInterval(function () { renderOnline(); renderMotion(); txt("ms-upd", lastUpd ? hms(lastUpd) : "—"); }, 1000);
  renderOnline();
  render();
  if (location.hash === "#wifi") openWifi();  // ссылка «Wi-Fi роутера» с лендинга точки доступа
  if (location.hash === "#help") openHelp();
})();
