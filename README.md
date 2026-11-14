# Move Sensor BLE

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Release](https://img.shields.io/github/v/release/daimonkor/move-sensor-ble-ota)](https://github.com/daimonkor/move-sensor-ble-ota/releases)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=daimonkor&repository=move-sensor-ble-ota&category=integration)
[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=move_sensor_ble_ota)

**English** · [Русский](#русский)

A battery-powered PIR motion sensor on a **Seeed XIAO ESP32-C3** (SR602 PIR, SHT41, LiPo) running ESPHome
with **BTHome v2** over BLE and deep sleep — plus a Home Assistant custom integration,
**Move Sensor BLE OTA**, that flashes firmware and changes settings **over Bluetooth through ESPHome
Bluetooth proxies**. The sensor never needs Wi-Fi in normal operation.

### Features

- **BLE OTA**: flash a new firmware over Bluetooth during the sensor's short wake window (button, service or REST).
- **Build & flash in one click**: the integration compiles `move-sensor-ble.yaml` in the ESPHome Builder add-on
  and flashes the result over BLE — no file downloads.
- **Settings over BLE**: detect delay, keep time, climate report interval, voltage factor, LED.
  Changes are queued and applied at the next wake-up; the sensor confirms them.
- **Diagnostics**: firmware version, brownout / crash counters, last reset reason (stored in NVS on the sensor).
- **Health sensors**: last seen, battery days left (trend since last charge), motion events / wakes per day,
  *PIR stuck* problem sensor.
- **Wi-Fi reset over BLE**: return the service-mode Wi-Fi to the firmware default.
- **Docs inside Home Assistant**: the integration serves its own documentation page.

### Install

1. Click **Open your Home Assistant instance and open a repository…** above → Home Assistant opens
   **Marketplace** (HA 2026.11+) or HACS with this repository → **Download** → restart Home Assistant.
2. Click **Add integration** → the *Move Sensor BLE OTA* setup flow opens → pick the BTHome sensor.

Manually: Settings → **Marketplace** → **Add from link** (HACS: ⋮ → *Custom repositories*) →
`https://github.com/daimonkor/move-sensor-ble-ota`, type *Integration*.

Requirements: the **Bluetooth** integration and at least one **ESPHome Bluetooth proxy** near the sensor
(active connections are on by default). The sensor firmware must include `ble_ota:` in the `bthome:` block.

### Repository layout

| Path | What |
|---|---|
| `custom_components/move_sensor_ble_ota/` | Home Assistant integration ([detailed docs, Russian](custom_components/move_sensor_ble_ota/README.md)) |
| `esphome/` | Sensor firmware: `move-sensor-ble.yaml`, `move_sensor_rtc.h`, service-mode web/AP `move_sensor_ble/`, external component `components/bthome` (BTHome + BLE OTA, DIAG, settings, Wi-Fi reset). Secrets template — `secrets.example.yaml` |
| `tools/ble_ota_flash.py` | Build + BLE flash from a terminal (stdlib Python only) |
| `sync_from_ha.sh` | Copy the firmware files (`esphome/`, `tools/`) from Home Assistant `/config` into this repo |

Issues and pull requests are disabled — this is a personal project published as-is.

---

## Русский

Батарейный датчик движения на **Seeed XIAO ESP32-C3** (PIR SR602, SHT41, LiPo) с **BTHome v2** по BLE
и deep sleep — и интеграция Home Assistant **Move Sensor BLE OTA** к нему: прошивка и настройки по BLE
через Bluetooth-прокси, диагностика, сборка прошивки одной кнопкой.

| Папка | Что |
|---|---|
| `custom_components/move_sensor_ble_ota/` | Интеграция HA (ставится через HACS как custom repository). Подробно — [её README](custom_components/move_sensor_ble_ota/README.md) |
| `esphome/` | Прошивка датчика: `move-sensor-ble.yaml`, `move_sensor_rtc.h`, сайт/точка доступа `move_sensor_ble/`, компонент `components/bthome` (BTHome + BLE OTA, DIAG, настройки, сброс Wi-Fi, быстрый тест). Секреты — `secrets.example.yaml` |
| `tools/ble_ota_flash.py` | Сборка + прошивка по BLE из терминала, без браузера (только stdlib Python) |
| `sync_from_ha.sh` | Скопировать файлы прошивки (`esphome/`, `tools/`) из `/Volumes/config` в репозиторий |

## Работа с репозиторием

- **Интеграция** (`custom_components/`) — источник здесь. Правка → коммит → поднять `version` в `manifest.json` и
  значок версии в `docs/index*.html` → push → GitHub-релиз с тегом `vX.Y.Z`. В HA обновление приходит через
  **Marketplace** (Настройки → Marketplace → Move Sensor BLE OTA → Обновить), без ручного копирования в `/config`.
- **Прошивка** (`esphome/`) — рабочая копия в `/config/esphome`. Перед коммитом: `./sync_from_ha.sh`.

## Установка интеграции

1. Кнопка **Open your Home Assistant instance and open a repository…** → адрес своего HA (один раз) →
   откроется Marketplace (HA 2026.11+) или HACS с этим репозиторием → **Скачать** → перезапустить HA.
2. Кнопка **Add integration** → мастер *Move Sensor BLE OTA* → выбрать датчик.

Вручную: Настройки → **Marketplace** → **Добавить по ссылке** (в HACS: ⋮ → *Custom repositories*) →
`https://github.com/daimonkor/move-sensor-ble-ota`, тип *Integration*.

Прошивку датчика собрать из `esphome/` (скопировать в `/config/esphome/`, секреты по `secrets.example.yaml`).

## Публикация

`./tools/set_github.sh <github-логин>` — подставит логин вместо `OWNER` в README, доки и `manifest.json`
(`codeowners`). Для обновлений в Marketplace — GitHub-релиз с тегом = `version` из `manifest.json`.
