#!/usr/bin/env node
// Сборка сайта датчика движения в ОДИН файл для прошивки (по образцу сайта весов,
// esp32_c3_plus_honey_weight/site/build.js):
//   node move_sensor_ble/site/build.js [выход]   (по умолчанию ../site_bundle.js)
// site_bundle.js берёт YAML: web_server: js_include → отдаётся как /0.js (ESPHome сам жмёт gzip).
// Руками site_bundle.js не править — править panel.html / app.css / app.js и пересобирать.
//
// Состав: стили + разметка панели (вставляются перед <esp-app>), app.js и штатный интерфейс
// ESPHome v3 (vendor/esphome_www_v3.js — копия из сайта весов, без интернета CDN не нужен),
// завёрнутый в window.MS_espApp(): запускается только при открытии вкладки «Устройство».
// YAML: js_url: "" (не тянуть www.js с CDN), НЕ local: true (главная тогда не грузит /0.js).
const fs = require('fs'), path = require('path');
const dir = __dirname;
const out = path.resolve(process.argv[2] || path.join(dir, '..', 'site_bundle.js'));
const rd = f => fs.readFileSync(path.join(dir, f), 'utf8');
// На SMB-шаре файл сразу после записи может прочитаться обрезанным.
const tail = (f, re) => { if (!re.test(rd(f).trimEnd())) throw new Error(f + ': обрезан — пересоберите через пару секунд'); };
tail('app.css', /\}$/); tail('panel.html', />$/); tail('app.js', /\}\)\(\);$/);
// www.js в firstUpdated подменяет favicon на свой «домик» — гасим, остаётся наша иконка.
const WWW_ICON = "n&&(n.href=`data:image/svg+xml,";
const wwwRaw = rd('vendor/esphome_www_v3.js');
if (wwwRaw.split(WWW_ICON).length !== 2) throw new Error('vendor www.js: не нашёл подмену favicon — проверьте WWW_ICON');
const www = wwwRaw.replace(WWW_ICON, "0&&(n.href=`data:image/svg+xml,");
if (/^\s*(import|export)\s/m.test(www)) throw new Error('vendor www.js стал ES-модулем — обёртка в функцию не подойдёт');

const ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24" rx="6" fill="#3d7bff"/>' +
  '<g transform="translate(3 2.5) scale(.78)" fill="#fff"><circle cx="13.5" cy="3.6" r="2"/>' +
  '<path d="M9.8 8.9 7 23h2.1l1.8-8 2.1 2v6h2v-7.5l-2.1-2 .6-3A7.3 7.3 0 0 0 19 13v-2a5 5 0 0 1-4.3-2.4l-1-1.6a2 2 0 0 0-2.5-.8L6 8.3V13h2V9.6z"/></g></svg>';

const parts = [
  '// site_bundle.js — СГЕНЕРИРОВАНО move_sensor_ble/site/build.js. Не править руками.\n' +
  '// Сайт датчика (web_server js_include → /0.js): вкладки «Датчик» (своя панель) и «Устройство» (штатный ESPHome).\n',
  '(function(){\n' +
  ' if (document.getElementById("ms")) return;\n' +
  ' var h = document.head;\n' +
  ' [].forEach.call(h.querySelectorAll("link[rel~=icon]"), function (l) { l.remove(); });\n' +
  ' var add = function (tag, attrs) { var e = document.createElement(tag); for (var k in attrs) e.setAttribute(k, attrs[k]); h.appendChild(e); };\n' +
  ' add("meta", { name: "viewport", content: "width=device-width,initial-scale=1,viewport-fit=cover" });\n' +
  ' add("meta", { name: "color-scheme", content: "light dark" });\n' +
  ' add("meta", { name: "theme-color", content: "#f4f5f8", media: "(prefers-color-scheme: light)" });\n' +
  ' add("meta", { name: "theme-color", content: "#0b0d12", media: "(prefers-color-scheme: dark)" });\n' +
  ' add("link", { rel: "icon", type: "image/svg+xml", href: "data:image/svg+xml," + encodeURIComponent(' + JSON.stringify(ICON) + ') });\n' +
  ' add("meta", { name: "apple-mobile-web-app-title", content: "Move Sensor" });\n' +
  ' document.title = "Move Sensor BLE";\n' +
  ' var st = document.createElement("style"); st.textContent = ' + JSON.stringify(rd('app.css')) + '; h.appendChild(st);\n' +
  ' var box = document.createElement("div"); box.innerHTML = ' + JSON.stringify(rd('panel.html')) + ';\n' +
  ' var ms = box.querySelector("#ms"), app = document.querySelector("esp-app");\n' +
  ' document.body.insertBefore(ms, app || document.body.firstChild);\n' +
  ' if (app) app.style.display = "none";\n' +
  '})();\n',
  'window.MS_espApp = function () {\n' +
  ' if (window.MS_espApp.done) return;\n' +
  ' window.MS_espApp.done = true;\n' +
  ' // ── vendor/esphome_www_v3.js (esphome-webserver v3, oi.esphome.io/v3/www.js) ──\n' +
  ' (function () {\n' + www + '\n })();\n' +
  '};\n',
  '// ── app.js ──\n' + rd('app.js')
];
fs.writeFileSync(out, parts.join('\n'));
console.log('site_bundle: ' + out + ' (' + fs.statSync(out).size + ' байт)');
