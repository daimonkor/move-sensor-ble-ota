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
| **One device** (v1.15) | **Occupancy, Temperature, Humidity, Battery, Voltage** on the *Move Sensor BLE* device come from **either channel** — BLE (BTHome advert) or Wi-Fi (the sensor's ESPHome device), the newest value wins. **Link** (`ble` / `wifi`), **Signal** (dBm of that channel) and **Signal level** (%, −90 dBm = 0 %, −30 dBm = 100 %) show the channel. Settings go over Wi-Fi at once when the ESPHome device is connected, otherwise over BLE at the next wake-up |
| **Duplicates hidden** (v1.15) | The same data and settings on the ESPHome device and on the BTHome device are **hidden, not disabled** (`hidden_by: integration`, label *Move Sensor BLE*), like the built-in *Switch as X* / *Group* helpers do: hidden entities keep their state, history and automations. On the ESPHome device only Wi-Fi / service-mode entities stay visible. Voice-assistant exposure moves to the main device. Entities you show yourself stay shown. **Configure → Display**: show hidden copies / hide them again, show the panel. Removing the integration shows back what it hid |
| **Settings revision** (v1.15) | The sensor counts every real settings change from any side (its web page, ESPHome, BLE): one write = +1, after 255 comes 1, 0 only after the flash was erased. It travels in the own advert block, in the ESPHome entity *Settings revision* and as byte 18 of the BLE settings reply. A change not made by this HA is **adopted** (no more overwriting edits made on the sensor page); revision 0 → HA restores its values. An adopted motion/climate window turns *Auto advertising window* off |
| **Firmware** (update entity, v1.15) | Replaces *Flash / Build & flash / Cancel / OTA status / OTA progress / Firmware version*. Installed = build on the sensor; a new version appears when the sensor YAML tree (device YAML, `move_sensor_ble/`, `components/bthome/`, `secrets.yaml`) changed after the last flash. **Install** builds in the ESPHome Builder add-on, checks that the OTA key inside the image equals the integration key (an error pops up at once) and flashes: **over Wi-Fi at once** when the sensor's ESPHome device is connected (service mode, ESPHome OTA, ~10× faster), otherwise **over BLE** at the next wake-up; if Wi-Fi drops mid-flash it continues over BLE. The job survives an HA restart. Cancel (also during the build): service `move_sensor_ble_ota.cancel` or the panel. *Install* in ESPHome Builder itself bypasses the key check |
| **BLE TX power / OTA key** (v1.15) | TX power of the BLE advert from the firmware YAML (`bthome: tx_power`, read-only); OTA key state: *Accepted* / *Rejected by the sensor* / *No key* / *Not checked yet*. Change the key: **Configure** |
| **Panel** (v1.15) | Sidebar page *Move Sensor BLE* from the integration (standard cards: Sensor / Settings / BLE / Wi-Fi / Firmware), built from the integration's entities automatically. Toggle in **Configure → Display** |
| **Detect delay / Keep time / Climate report interval / Voltage factor (BLE)**, **Motion tracking LED (BLE)** | Sensor settings, applied at the next wake-up |
| **Settings status** / **Read settings (BLE)** | v1.15: only steady states — **In sync** (`in_sync`) / **Waiting for wake-up** (`waiting`, attributes say what) / **Not read yet** (`not_read`) / **Error** (`error`); Read settings reads the actual values from the sensor |
| **Settings change** (event, v1.15) | One history point / logbook entry per settings change: `saved_wifi`, `saved_ble` (HA wrote it), `changed_on_sensor` (edited on the sensor page or in ESPHome — HA took the value), `restored` (the sensor lost its settings — HA put its values back). Attributes: `changes` (from → to), `summary`, `channel`, `settings_revision`. Usable as an automation trigger |
| **Wrong / missing OTA key** (v1.14.1) | If the sensor rejects the key (or there is none), BLE connections to it stop instead of retrying on every wake-up, and a Repairs issue explains it. The key comes from the options «OTA key» — **this field may be empty**, then from `move_sensor_ble_ota_key` in `/config/esphome/secrets.yaml`. Connections resume when the effective key changes (checked every 10 min), after Read settings / any setting change, after a sensor reboot, or every 6 h. **Flashing with a key the sensor already rejected** fails right away with an error popup (nothing is queued); if the rejection only comes when the sensor wakes, a notification «flash did not start» appears |
| **Last motion, PIR stuck by fresh motion** (v1.14.2) | Firmware from 2026-11-16 adds its own data block to the BLE advert (Manufacturer Specific Data, company `0xFFFF`, after BTHome): Wake count, settings revision (reserved, 0 until 1.15) and a counter of **fresh** PIR triggers (a new rise after LOW; a PIR held HIGH does not count). While *occupied*, the sensor sends a packet on fresh motion at most every 2 min. **Last motion** shows the time of the last fresh trigger; **PIR stuck** turns on only when the sensor is *occupied* and there was no fresh trigger for longer than *PIR stuck after* (someone moving next to it is not a problem). With older firmware both work as before. Battery low is no longer sent (use Battery ≤ 10 %); the BTHome entities *Battery low* and *Count* stop updating. On start-up 1.14.2 also shows again any entities hidden by a newer version (rollback guard) |
| **Settings restore after reboot** (v1.14.0) | HA remembers the settings it set. After the sensor reboots (Wake count resets: USB flash, erased flash, BLE OTA) it reads them back and, if the sensor fell back to its firmware defaults, sends HA's values again in the same connection (log: `settings reset on the sensor after reboot, restoring`). **Read settings** makes the sensor's current values the new reference — press it after changing settings on the sensor's own Wi-Fi site |
| **Last seen**, **Battery days left**, **Motion events today / Wakes since midnight** | Health and daily statistics from BTHome packets |
| **PIR stuck** (+ **PIR stuck after**, min) | Problem sensor: "occupied" held longer than the threshold. After a sensor reboot (wake count reset) an "occupied" packet starts the timer again (v1.12.1) |
| **Temperature offset / Humidity offset (BLE)** (v1.12.0) | Signed correction added on the sensor to the SHT41 reading: −10…+10 °C, −20…+20 %, step 0.1. The temperature offset also recalculates relative humidity via the dew point (same water vapour, different temperature), then the humidity offset is added. Needs firmware with offsets (otherwise unavailable) |
| **Motion window / Climate window (BLE)**, **Auto advertising window** | How long the sensor keeps advertising after a packet (motion / climate). Auto (default) picks them from the median RSSI of the last 10 packets: ≥ −75 dBm → 800/1500 ms, ≥ −85 → 1500/2500, weaker → 3000/4500; a missed climate packet adds one level for 24 h. Setting a window by hand turns Auto off. Needs firmware with windows. On the Move Sensor BLE dashboard they are a separate "Радио (BLE)" section |
| **Brownout count / Crash count / Last reset reason** | Reboot diagnostics stored in the sensor's NVS |
| **Device ID** / **Identify (BLE)** (v1.13.0) | Device ID = last 3 bytes of the Wi-Fi MAC (BLE MAC − 2), e.g. `148F98` — the same ID is in the firmware name (`move-sensor-ble-148f98.local`, `name_add_mac_suffix`), the AP SSID (`MoveSensorBLE-148F98`), the sensor site header and the default entry title («Move Sensor BLE 148F98»). Identify blinks the LED for 10 s at the next wake-up (wave at the sensor to wake it) to find the physical unit |
| **Reset Wi-Fi to default (BLE)** | The sensor forgets the saved Wi-Fi network and returns to the firmware one |

Services: `move_sensor_ble_ota.flash`, `build_and_flash`, `cancel`, `read_settings`, `reset_wifi` (kept in v1.15 for automations).
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
| **Одно устройство** (v1.15) | **Occupancy, Temperature, Humidity, Battery, Voltage** на устройстве *Move Sensor BLE* — **из любого канала**: BLE (реклама BTHome) или Wi-Fi (ESPHome-устройство датчика), побеждает самое свежее значение. **Link** (`ble` / `wifi`), **Signal** (дБм этого канала) и **Signal level** (%, −90 дБм = 0 %, −30 дБм = 100 %) показывают канал. Настройки при подключённом ESPHome-устройстве уходят по Wi-Fi сразу, иначе — по BLE в ближайшее пробуждение |
| **Дубли скрыты** (v1.15) | Те же данные и настройки в ESPHome- и BTHome-устройстве **скрываются, но не выключаются** (`hidden_by: integration`, метка *Move Sensor BLE*) — как встроенные помощники *Switch as X* / *Группа*: скрытые сущности работают, история и автоматизации не ломаются. В ESPHome-устройстве на виду только Wi-Fi и сервисный режим. Голосовые помощники переезжают на основное устройство. Показанное вами вручную больше не скрывается. **Настроить → Вид**: показать скрытые копии / снова скрыть, панель в меню. Удаление интеграции возвращает то, что она скрыла |
| **Номер правки настроек** (v1.15) | Датчик считает каждую реальную правку настройки с любой стороны (свой сайт, ESPHome, BLE): одна запись = +1, после 255 идёт 1, 0 — только после стирания флеша. Номер едет в своём блоке рекламы, в ESPHome-сущности *Settings revision* и 18-м байтом ответа BLE. Правка не из этого HA **принимается** (правки на сайте датчика больше не затираются); номер 0 — HA возвращает свои значения. Принятое с датчика окно рекламы выключает *Auto advertising window* |
| **Firmware** (сущность обновления, v1.15) | Вместо *Flash / Build & flash / Cancel / OTA status / OTA progress / Firmware version*. Установлена — сборка на датчике; новая версия — когда дерево YAML датчика (YAML устройства, `move_sensor_ble/`, `components/bthome/`, `secrets.yaml`) изменилось после последней прошивки. **Установить** — сборка в аддоне ESPHome Builder, проверка, что ключ OTA в образе равен ключу интеграции (ошибка всплывает сразу), и прошивка: **по Wi-Fi сразу**, если ESPHome-устройство датчика на связи (сервисный режим, штатный OTA ESPHome, ~в 10 раз быстрее), иначе **по BLE** в ближайшее пробуждение; оборвался Wi-Fi посреди прошивки — продолжает по BLE. Задание переживает перезапуск HA. Отмена (и во время сборки) — служба `move_sensor_ble_ota.cancel` или панель. «Установить» в самом ESPHome Builder ключ не проверяет |
| **BLE TX power / OTA key** (v1.15) | Мощность BLE-рекламы из YAML прошивки (`bthome: tx_power`, только чтение); ключ OTA: *Accepted* / *Rejected by the sensor* / *No key* / *Not checked yet*. Поменять ключ — **Настроить** |
| **Панель** (v1.15) | Страница *Move Sensor BLE* в боковом меню от интеграции (стандартные карточки: Sensor / Settings / BLE / Wi-Fi / Firmware), собирается из сущностей интеграции сама. Включается в **Настроить → Вид** |
| **Detect delay / Keep time / Climate report interval / Voltage factor (BLE)** | Настройки датчика. Новое значение уходит в ближайшее окно пробуждения |
| **Motion tracking LED (BLE)** | Вкл/выкл светодиод срабатываний |
| **Temperature offset / Humidity offset (BLE)** (v1.12.0) | Плюс или минус к показаниям SHT41, считается на датчике: −10…+10 °C, −20…+20 %, шаг 0,1. Поправка температуры пересчитывает относительную влажность по точке росы (пара в воздухе столько же, температура другая), затем прибавляется поправка влажности. Нужна прошивка с поправками (иначе сущности недоступны) |
| **Motion window / Climate window (BLE)** + **Auto advertising window** | Сколько датчик рекламирует после пакета: «движение/пусто» и климат. Короче окно — меньше бодрствует, дольше батарея. **Авто** (по умолчанию): по медиане RSSI последних 10 пакетов — ≥ −75 дБм → 800/1500 мс, ≥ −85 → 1500/2500, слабее → 3000/4500 (к лучшему уровню — с запасом 3 дБ); пропущен климат-пакет (нет пакетов дольше 1,8 × Climate interval) — на сутки уровень длиннее. Ручное значение выключает авто. Нужна прошивка с окнами (иначе сущности недоступны). На дашборде Move Sensor BLE — отдельная группа «Радио (BLE)». Замер 2026-11-14: RSSI −56, прокси на настройках ESPHome по умолчанию — 0 потерь даже при 300 мс, подключение успевало при 600 мс |
| **Settings status** (v1.15) | Только устойчивые состояния: «In sync» (`in_sync`) — всё на датчике совпадает с HA; «Waiting for wake-up» (`waiting`, в атрибутах — что именно); «Not read yet» (`not_read`); «Error» (`error`) |
| **Settings change** (событие, v1.15) | Каждое изменение настроек — отдельная точка в истории и запись в журнале: `saved_wifi`, `saved_ble` (записал HA), `changed_on_sensor` (поменяли на сайте датчика или в ESPHome — HA принял), `restored` (датчик потерял настройки — HA вернул свои). Атрибуты: `changes` (было → стало), `summary`, `channel`, `settings_revision`. Годится как триггер автоматизации |
| **Read settings (BLE)** | Прочитать фактические настройки с датчика в ближайшее окно. С v1.14.0 прочитанное становится новым эталоном — нажать после правки настроек на сайте датчика по Wi-Fi |
| **Неверный ключ / нет ключа OTA** (v1.14.1) | Если датчик не принял ключ (или ключа нет), интеграция больше не подключается к нему на каждом пробуждении, а показывает сообщение в «Ремонте». Ключ — из параметров («Ключ OTA»); **поле может быть пустым**, тогда из `move_sensor_ble_ota_key` в `/config/esphome/secrets.yaml`. Подключения возобновятся сами при смене ключа (проверка раз в 10 мин), после Read settings / изменения настройки / перезагрузки датчика или раз в 6 ч. **Прошивка с ключом, который датчик уже отклонил**, сразу падает с всплывающей ошибкой (в очередь не ставится); если отказ приходит только при пробуждении — уведомление «прошивка не началась» |
| **Last motion, PIR stuck по свежему движению** (v1.14.2) | Прошивка с 2026-11-16 добавляет в BLE-рекламу свой блок данных (Manufacturer Specific Data, company `0xFFFF`, после BTHome): Wake count, номер правки настроек (резерв, 0 до 1.15) и счётчик **свежих** срабатываний PIR (новый подъём после LOW; удержание HIGH не считается). Пока «занято», датчик шлёт пакет на свежее движение не чаще раза в 2 мин. **Last motion** — время последнего свежего срабатывания; **PIR stuck** включается, только если «занято» и свежих срабатываний нет дольше *PIR stuck after* (человек шевелится рядом — не проблема). Со старой прошивкой — как раньше. Battery low больше не передаётся (ориентир — Battery ≤ 10 %); BTHome-сущности *Battery low* и *Count* перестают обновляться. При запуске 1.14.2 также показывает сущности, скрытые более новой версией (защита отката) |
| **Восстановление настроек после перезагрузки** (v1.14.0) | HA помнит заданные им настройки. После перезагрузки датчика (сброс Wake count: прошивка по USB, стёртый флеш, BLE OTA) перечитывает их и, если датчик вернулся к значениям из прошивки, в том же подключении отправляет значения HA заново (в логе `settings reset on the sensor after reboot, restoring`) |
| **Last seen** | Когда пришёл последний пакет BTHome (движение или климат). Норма — не реже Climate report interval; в атрибутах packet id, Wake count, батарея, RSSI, через какой прокси. Пишется один раз на пробуждение, а не на каждую из ~150 копий рекламы |
| **Battery days left** | Прогноз, сколько суток до разряда: тренд Battery % с последней зарядки (МНК). Нужно ≥ 1 сутки истории и падение ≥ 2 %, до этого `unknown` (в атрибутах — почему). Рост на > 5 % = зарядили, история заново |
| **Motion events today / Wakes since midnight** | Суточная статистика (сброс в полночь, перезагрузки датчика переживает): сколько раз было «занято»; пробуждений всего (по счётчику датчика «Wakes since power-on» / BTHome Count — он сбрасывается при каждой перезагрузке), с отправкой пакета и тихих (без радио). Вчерашние значения — в атрибутах |
| **PIR stuck** (+ настройка **PIR stuck after**, мин) | `problem`: «занято» держится непрерывно дольше порога (по умолчанию 30 мин). Проверяется каждую минуту, даже когда датчик молчит. Причины: отвалился провод сигнала PIR (во сне с пробуждением по LOW подтяжка вверх — пин читается HIGH), тепло/солнце/сквозняк в поле линзы, просадка питания SR602, влага. Порог — настройка интеграции, на датчик не уходит. После перезагрузки датчика (сброс Wake count: питание, OTA, сбой) «занято» считается новым событием — отсчёт заново с этого пакета, а не с момента до выключения (v1.12.1) |
| **Device ID** / **Identify (BLE)** (v1.13.0) | Device ID — хвост MAC Wi-Fi (BLE MAC − 2), напр. `148F98`. Тот же ID в имени прошивки (`move-sensor-ble-148f98.local`, `name_add_mac_suffix`), SSID точки доступа (`MoveSensorBLE-148F98`), заголовке сайта датчика, в списке при добавлении и в названии по умолчанию («Move Sensor BLE 148F98»). Identify — LED мигает 10 с в ближайшее пробуждение (помаши перед датчиком, чтобы разбудить): найти нужный из нескольких. Прошивка одна на все датчики |
| **Reset Wi-Fi to default (BLE)** | В ближайшее окно датчик забывает сохранённую сеть Wi-Fi и возвращается к сети из прошивки (LED 6 вспышек). Применяется при входе в сервисный режим или сразу, если он включён. Сервис `move_sensor_ble_ota.reset_wifi` |
| **Группы на странице устройства** (v1.11.0; имена по-английски с v1.12.3) | Имена сущностей — на английском при любом языке HA (по-русски переводит только сам BTHome свои сущности). **Controls**: Build & flash firmware, Flash firmware file, Cancel firmware update. **Sensors**: Last seen, Battery days left, Motion events today, PIR stuck. **Configuration**: всё, что уходит на датчик (Keep time, Detect delay, Climate report interval, Voltage factor, Motion LED, Advertising window: motion/climate, Auto advertising window, Temperature/Humidity offset), PIR stuck after, Read settings, Reset Wi-Fi to default. **Diagnostic**: Firmware version, Firmware update status/progress, Settings status, Wakes since midnight, Brownout resets, Firmware crashes, Last reset reason |
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

Сущность **Firmware** → «Установить» (v1.15; раньше — кнопка Build & flash), сервис `move_sensor_ble_ota.build_and_flash` или REST:

1. Интеграция через Supervisor открывает ingress-сессию аддона **ESPHome Builder** (как сам HA Core)
   и по его WebSocket-API собирает YAML (`firmware/compile`).
2. Скачивает `firmware.ota.bin`, кладёт в папку прошивок как `<yaml>-<дата-время>.ota.bin`.
3. Сразу запускает прошивку по BLE (`start: false` — только собрать).
4. Ход — в сущности **Firmware** (v1.15; раньше OTA status) и в `/status`: `building %` → `waiting` → `flashing %` → `success` → `idle` (после подтверждения новой версии).

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
| **Собрать и прошить** | `POST /api/move_sensor_ble_ota/build[?configuration=move-sensor-ble-148f98.yaml][&start=0][&timeout=180]` — отвечает сразу, сборка в фоне; или сервис `move_sensor_ble_ota.build_and_flash` (`configuration`, `start`, `timeout`) |
| Загрузить `.bin` и сразу прошить | `POST /api/move_sensor_ble_ota/upload?filename=<имя>.bin&start=1` — тело: файл (raw или multipart), токен администратора |
| Прошить | сервис `move_sensor_ble_ota.flash` (`file`, `timeout` мин, `config_entry_id`) |
| Отменить | сервис `move_sensor_ble_ota.cancel` |
| Быстрый тест (отладка) | сервис `move_sensor_ble_ota.fast_test` (`minutes`, 0 = выкл; `tx_window_ms` — окно рекламы на время теста, 0 = как в прошивке) — без движения датчик просыпается раз в 30 с; в UI сущности нет. Окно: замер, сколько рекламы хватает прокси (потери = пропуски Wake count) |
| Прочитать настройки | сервис `move_sensor_ble_ota.read_settings` |
| Изменить настройку | `number.set_value` / `switch.turn_on/off` на сущностях «… (BLE)» |
| Состояние | `GET /api/move_sensor_ble_ota/status` |

Примеры:

```bash
# собрать YAML датчика (move-sensor-ble-<device id>.yaml) и прошить по BLE (ход — GET /status)
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
