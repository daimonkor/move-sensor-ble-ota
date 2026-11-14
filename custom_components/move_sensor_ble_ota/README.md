# Move Sensor BLE OTA — интеграция Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Release](https://img.shields.io/github/v/release/daimonkor/move-sensor-ble-ota)](https://github.com/daimonkor/move-sensor-ble-ota/releases)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=daimonkor&repository=move-sensor-ble-ota&category=integration)
[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=move_sensor_ble_ota)

Прошивка по воздуху (OTA) и удалённые настройки для батарейного датчика движения
**Move Sensor BLE** (Seeed XIAO ESP32-C3, ESPHome, BTHome, deep sleep) — **по Bluetooth,
через Bluetooth-прокси ESPHome**, без Wi-Fi на датчике.

Датчик почти всё время спит. Просыпается от PIR или по таймеру телеметрии (температура/
влажность), ~4.5 с шлёт BTHome-рекламу и засыпает снова. Интеграция ловит это окно,
подключается и успевает за одно подключение передать настройки и/или прошивку.

---

**English** · [Русский](#что-умеет)

## English

Over-the-air firmware updates and remote settings for the battery motion sensor **Move Sensor BLE**
(Seeed XIAO ESP32-C3, ESPHome, BTHome, deep sleep) — **over Bluetooth, through ESPHome Bluetooth proxies**,
with no Wi-Fi on the sensor.

The sensor sleeps almost all the time. It wakes on motion (PIR) or on the climate timer, advertises BTHome
for ~4.5 s and goes back to sleep. The integration catches that window, connects through a proxy and sends
pending settings and/or firmware in a single connection. If there is nothing to do, it does not touch the
proxy or the sensor. Wave a hand in front of the sensor to get a window sooner.

| Entity | Purpose |
|---|---|
| **Detect delay / Keep time / Climate report interval / Voltage factor (BLE)**, **Motion tracking LED (BLE)** | Sensor settings, applied at the next wake-up |
| **Settings status** / **Read settings (BLE)** | `applied` / `pending` / `unknown` / `error`; read the actual settings from the sensor |
| **Build & flash firmware (BLE)** | Compile `move-sensor-ble.yaml` in the ESPHome Builder add-on and flash it over BLE |
| **Flash firmware (BLE)** / **Cancel OTA** | Flash the newest `.bin` from the firmware folder / cancel |
| **OTA status / OTA progress** | `idle → building → waiting → connecting → flashing → success / error / cancelled` |
| **Firmware version (BLE)** | `vYYYY.MM.DD.HHMMSS-xxxxxxxx` (build time + 8 hex of the image SHA-256) |
| **Last seen**, **Battery days left**, **Motion events today / Wakes today** | Health and daily statistics from BTHome packets |
| **PIR stuck** (+ **PIR stuck after**, min) | Problem sensor: "occupied" held longer than the threshold |
| **Brownout count / Crash count / Last reset reason** | Reboot diagnostics stored in the sensor's NVS |
| **Reset Wi-Fi to default (BLE)** | The sensor forgets the saved Wi-Fi network and returns to the firmware one |

Services: `move_sensor_ble_ota.flash`, `build_and_flash`, `cancel`, `read_settings`, `reset_wifi`.
REST: `POST /api/move_sensor_ble_ota/upload`, `GET /api/move_sensor_ble_ota/status`,
`POST /api/move_sensor_ble_ota/build`.

**Install:** use the buttons above (Marketplace/HACS → Download → restart HA → Add integration), or copy
`move_sensor_ble_ota` to `/config/custom_components/` and restart. Then Settings → Devices & services →
**Add integration** → *Move Sensor BLE OTA* → pick the BTHome sensor; leave the key empty to read it from
`esphome/secrets.yaml`.

**Requirements:** the Bluetooth integration, at least one ESPHome Bluetooth proxy near the sensor,
and sensor firmware with `ble_ota:` in the `bthome:` block.

The detailed documentation below is in Russian.

---

## Что умеет

| Сущность на странице датчика | Назначение |
|---|---|
| **Detect delay / Keep time / Climate report interval / Voltage factor (BLE)** | Настройки датчика. Новое значение уходит в ближайшее окно пробуждения |
| **Motion tracking LED (BLE)** | Вкл/выкл светодиод срабатываний |
| **Settings status** | `applied` — датчик подтвердил; `pending` — ждёт окна (в атрибутах: что именно); `unknown` — ещё не читали; `error` |
| **Read settings (BLE)** | Прочитать фактические настройки с датчика в ближайшее окно |
| **Build & flash firmware (BLE)** | Собрать `move-sensor-ble.yaml` в аддоне ESPHome Builder и сразу прошить по BLE — одна кнопка, без скачивания файлов |
| **Flash firmware (BLE)** | Прошить самым свежим `.bin` из папки прошивок |
| **Cancel OTA** | Отменить ожидание / прошивку |
| **OTA status / OTA progress** | `idle → (building %) → waiting → connecting → flashing (%) → success` / `error` / `cancelled`; после `success` статус сразу становится `idle` / 0 %, как только датчик после перезагрузки ответил новой версией (запасной таймер — 2 мин); в атрибутах — `last_result`, `last_build`, файл, версии до/после, скорость, ошибка, число попыток |
| **Firmware version (BLE)** | Версия сборки на датчике (как «Build Version» в сервисном режиме), читается при каждом BLE-подключении; после прошивки по BLE — перечитывается при следующем пробуждении |
| **Last seen** | Когда пришёл последний пакет BTHome (движение или климат). Норма — не реже Climate report interval; в атрибутах packet id, Wake count, батарея, RSSI, через какой прокси. Пишется один раз на пробуждение, а не на каждую из ~150 копий рекламы |
| **Battery days left** | Прогноз, сколько суток до разряда: тренд Battery % с последней зарядки (МНК). Нужно ≥ 1 сутки истории и падение ≥ 2 %, до этого `unknown` (в атрибутах — почему). Рост на > 5 % = зарядили, история заново |
| **Motion events today / Wakes today** | Суточная статистика (сброс в полночь): сколько раз было «занято»; пробуждений всего (по Wake count), с отправкой пакета и тихих (без радио). Вчерашние значения — в атрибутах |
| **PIR stuck** (+ настройка **PIR stuck after**, мин) | `problem`: «занято» держится непрерывно дольше порога (по умолчанию 30 мин). Проверяется каждую минуту, даже когда датчик молчит. Причины: отвалился провод сигнала PIR (во сне с пробуждением по LOW подтяжка вверх — пин читается HIGH), тепло/солнце/сквозняк в поле линзы, просадка питания SR602, влага. Порог — настройка интеграции, на датчик не уходит |
| **Reset Wi-Fi to default (BLE)** | В ближайшее окно датчик забывает сохранённую сеть Wi-Fi и возвращается к сети из прошивки (LED 6 вспышек). Применяется при входе в сервисный режим или сразу, если он включён. Сервис `move_sensor_ble_ota.reset_wifi` |
| **Группы на странице устройства** | Управление — Build & flash / Flash / Cancel; Датчики — OTA status/progress, Last seen, Battery days left, статистика, PIR stuck; Настройка — всё по BLE, LED, PIR stuck after, Read settings, Reset Wi-Fi; Диагностика — версия, Settings status, Brownout, Crash, причина сброса |
| **Firmware version (BLE)** формат | `vГГГГ.ММ.ДД.ЧЧММСС-xxxxxxxx` — время сборки + 8 hex SHA-256 образа: версия меняется при любой правке, не только YAML. После прошивки статус уходит в `idle`, когда датчик перезагрузился (сброс Wake count) и ответил новой версией |
| **Имена сущностей BTHome** | Интеграция сама даёт понятные имена сущностям BTHome того же датчика: «Count» → **Wake count**, бинарная «Батарея» → **Battery low**. Только имя в реестре (entity_id не меняется) и только если ты не переименовал сам |
| **Brownout count / Crash count / Last reset reason** | Диагностика перезагрузок (команда DIAG, счётчики в NVS датчика): сбросы по просадке питания, panic + watchdog, причина последней загрузки «не из сна». Читаются, когда интеграция видит перезагрузку (сброс Wake count), и при каждом BLE-подключении |

Эта документация открывается в HA: кнопка **«Документация»** на странице интеграции и **«Посетить»**
на странице устройства.

**Загрузка прошивки из интерфейса:** Настройки → Устройства и службы → *Move Sensor BLE OTA* →
**Настроить → «Загрузить прошивку (.bin)»** → выбрать файл (с компьютера или телефона),
галочка «Сразу запустить прошивку по BLE».

Подходит `.bin` из ESPHome: **Install → Manual download → OTA format** (`*.ota.bin`) или
`factory.bin` — из него приложение вырезается автоматически.

---

## Как это работает

```
 HA (эта интеграция)            Bluetooth-прокси ESPHome              Датчик (ESPHome)
 ───────────────────            ────────────────────────              ────────────────
 есть работа?  ──► подписка на рекламу датчика ◄── BTHome-реклама ── проснулся (PIR/таймер)
 реклама пришла ─► подключение ─────────────────────────────────────► сон отложен
                   HELLO / AUTH (HMAC-SHA256, ключ) ◄────────────────► проверка ключа
                   настройки: SET/GET ◄──────────────────────────────► number/switch, флеш
                   прошивка: BEGIN → блоки 4 КБ + ACK → END ─────────► esp_ota_*, проверка
 success ◄──────── отключение ◄────────────────────────────────────── перезагрузка / сон
```

* Работа есть, если: заданы, но не подтверждены настройки; ещё ни разу не читались настройки;
  запрошена прошивка. Иначе интеграция ничего не делает и прокси не трогает.
* Не успели в окно (прокси слушает эфир не всё время) — ждём следующее пробуждение.
  Ускорить: помахать рукой перед датчиком.
* Защита на датчике: HMAC-авторизация ключом; принимается только образ того же проекта
  (`project_name`); порог батареи (по умолчанию 30 %); CRC-32 + проверка образа ESP-IDF;
  откат загрузчика, если новая прошивка не прошла `safe_mode`. При обрыве датчик остаётся
  на старой прошивке.
* Таблица разделов по воздуху не меняется (как и при Wi-Fi OTA) — только по USB.

### Протокол (GATT)

Сервис `6d730001-626c-6f74-4d6f-766553656e73`:
* `…0002` CTRL — write + notify: `HELLO`, `AUTH`, `INFO`, `BEGIN`, `STATUS`, `END`, `ABORT`,
  `GET/SET settings`;
* `…0003` DATA — write without response: `offset:u32 + данные`, блоки 4096 байт с подтверждением.

Подробно — в шапке `components/bthome/ble_ota.h` (прошивка) и `const.py` (интеграция).

---

## Требования

* Home Assistant с интеграцией **Bluetooth** и хотя бы одним **Bluetooth-прокси ESPHome с
  активными подключениями** (`bluetooth_proxy:` на ESP32 — включены по умолчанию), который
  слышит датчик.
* Датчик с прошивкой, где в `bthome:` есть блок `ble_ota:` (компонент `components/bthome`
  с файлами `ble_ota.*`). **Первую** такую прошивку залить по USB или Wi-Fi (сервисный режим).
* Ключ OTA: `move_sensor_ble_ota_key` в `/config/esphome/secrets.yaml` (тот же, что в прошивке).
  Если ESPHome-конфиги лежат не там — ввести ключ в настройках интеграции.

---

## Установка в один клик

1. Нажать **Open your Home Assistant instance and open a repository…** (кнопка выше) → указать адрес своего HA
   (один раз, например `http://homeassistant.local:8123`) → HA откроет Marketplace (до 2026.11 — HACS)
   с этим репозиторием → **Скачать** → перезапустить HA.
2. Нажать **Add integration** (вторая кнопка) → откроется мастер *Move Sensor BLE OTA*.
3. Выбрать датчик из списка BTHome-устройств (или ввести Bluetooth-адрес), ключ оставить пустым —
   возьмётся из `esphome/secrets.yaml`.

Без кнопок: Настройки → **Marketplace** → **Добавить по ссылке** (в HACS: ⋮ → *Custom repositories*) →
`https://github.com/daimonkor/move-sensor-ble-ota`, тип *Integration*.

Ссылки устроены так (их же можно вставлять в форум/Discord):

```
https://my.home-assistant.io/redirect/hacs_repository/?owner=daimonkor&repository=move-sensor-ble-ota&category=integration
https://my.home-assistant.io/redirect/config_flow_start/?domain=move_sensor_ble_ota
```

## Установка на свой HA (вручную)

1. Скопировать папку `move_sensor_ble_ota` в `/config/custom_components/`.
2. Перезапустить Home Assistant.
3. Настройки → Устройства и службы → **Добавить интеграцию** → *Move Sensor BLE OTA*.
4. Выбрать датчик из списка BTHome-устройств (или ввести его Bluetooth-адрес),
   ключ оставить пустым — возьмётся из `esphome/secrets.yaml`.
5. Сущности появятся на странице того же устройства, что и BTHome-датчик.

## Распространение (HACS)

Чтобы ставить на другие HA через HACS как «пользовательский репозиторий», выложить в
отдельный GitHub-репозиторий в такой структуре:

```
move-sensor-ble-ota/
├── hacs.json
├── README.md                      ← этот файл
└── custom_components/
    └── move_sensor_ble_ota/       ← вся папка интеграции
```

`hacs.json`:

```json
{
  "name": "Move Sensor BLE OTA",
  "render_readme": true,
  "homeassistant": "2025.1.0"
}
```

В `manifest.json` поменять `documentation` и добавить `issue_tracker` на адрес репозитория,
`codeowners` — свой GitHub-логин; при каждом релизе поднимать `version` и делать GitHub release.
Установка у пользователя: HACS → ⋮ → **Пользовательские репозитории** → URL репозитория,
тип *Integration* → установить → перезапустить HA → добавить интеграцию.

Прошивочная часть (`components/bthome` с `ble_ota.*`) распространяется отдельно — как
external component ESPHome (папка в конфиге или `source: github://…`).

---

## Сборка + прошивка одной командой (2026-11-14)

Кнопка **Build & flash firmware (BLE)**, сервис `move_sensor_ble_ota.build_and_flash` или REST:

1. Интеграция через Supervisor открывает ingress-сессию аддона **ESPHome Builder** (как сам HA Core)
   и по его WebSocket-API собирает YAML (`firmware/compile`).
2. Скачивает `firmware.ota.bin`, кладёт в папку прошивок как `<yaml>-<дата-время>.ota.bin`.
3. Сразу запускает прошивку по BLE (`start: false` — только собрать).
4. Ход в **OTA status**: `building %` → `waiting` → `flashing %` → `success` → `idle` (после подтверждения новой версии).

Нужно: HA под Supervisor (HAOS / Supervised) и запущенный аддон ESPHome Builder. Сборщик — тот, что
настроен в аддоне (в т.ч. удалённый Device Builder на Mac). Ошибка сборки видна в `error` у **OTA status**
и в логе HA (последние строки компилятора).

Снаружи HA (без браузера) то же делает скрипт `/config/esphome/tools/ble_ota_flash.py`
(только стандартная библиотека Python, токен `ha_token` из `esphome/secrets.yaml`):

```bash
python3 /config/esphome/tools/ble_ota_flash.py                 # собрать и прошить
python3 /config/esphome/tools/ble_ota_flash.py --compile-only  # только собрать .bin
python3 /config/esphome/tools/ble_ota_flash.py --file x.bin    # прошить готовый файл
```

## API (для автоматизаций и скриптов)

| Что | Как |
|---|---|
| **Собрать и прошить** | `POST /api/move_sensor_ble_ota/build[?configuration=move-sensor-ble.yaml][&start=0][&timeout=180]` — отвечает сразу, сборка в фоне; или сервис `move_sensor_ble_ota.build_and_flash` (`configuration`, `start`, `timeout`) |
| Загрузить `.bin` и сразу прошить | `POST /api/move_sensor_ble_ota/upload?filename=<имя>.bin&start=1` — тело: файл (raw или multipart), токен администратора |
| Прошить | сервис `move_sensor_ble_ota.flash` (`file`, `timeout` мин, `config_entry_id`) |
| Отменить | сервис `move_sensor_ble_ota.cancel` |
| Быстрый тест (отладка) | сервис `move_sensor_ble_ota.fast_test` (`minutes`, 0 = выкл; `tx_window_ms` — окно рекламы на время теста, 0 = как в прошивке) — без движения датчик просыпается раз в 30 с; в UI сущности нет. Окно: замер, сколько рекламы хватает прокси (потери = пропуски Wake count) |
| Прочитать настройки | сервис `move_sensor_ble_ota.read_settings` |
| Изменить настройку | `number.set_value` / `switch.turn_on/off` на сущностях «… (BLE)» |
| Состояние | `GET /api/move_sensor_ble_ota/status` |

Примеры:

```bash
# собрать move-sensor-ble.yaml и прошить по BLE (ход — GET /status)
curl -X POST -H "Authorization: Bearer $HA_TOKEN" \
  "http://homeassistant.local:8123/api/move_sensor_ble_ota/build"

# загрузить готовый файл и прошить
curl -X POST -H "Authorization: Bearer $HA_TOKEN" \
  --data-binary @move-sensor-ble.ota.bin \
  "http://homeassistant.local:8123/api/move_sensor_ble_ota/upload?filename=move-sensor-ble.ota.bin&start=1"
```

## Диагностика

Подробный лог:

```yaml
logger:
  logs:
    custom_components.move_sensor_ble_ota: debug
    bleak_esphome: debug
```

Типичное: `waiting` долго — датчик не просыпается или прокси его не слышит; цикл
`connecting → flashing → waiting` — датчик уходит в сон посреди прошивки (нужна прошивка с
защёлкой сна `sleep_ready()` в `go_sleep`); `Settings status: error «неверное состояние/команда»`
— на датчике прошивка без команд настроек.
