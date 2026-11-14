# Move Sensor BLE

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Release](https://img.shields.io/github/v/release/OWNER/move-sensor-ble-ota)](https://github.com/OWNER/move-sensor-ble-ota/releases)

[![Открыть репозиторий в Marketplace/HACS вашего Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=OWNER&repository=move-sensor-ble-ota&category=integration)
[![Добавить интеграцию Move Sensor BLE OTA](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=move_sensor_ble_ota)

Батарейный датчик движения на **Seeed XIAO ESP32-C3** (PIR SR602, SHT41, LiPo) с **BTHome v2** по BLE
и deep sleep — и интеграция Home Assistant **Move Sensor BLE OTA** к нему: прошивка и настройки по BLE
через Bluetooth-прокси, диагностика, сборка прошивки одной кнопкой.

| Папка | Что |
|---|---|
| `custom_components/move_sensor_ble_ota/` | Интеграция HA (ставится через HACS как custom repository). Подробно — [её README](custom_components/move_sensor_ble_ota/README.md) |
| `esphome/` | Прошивка датчика: `move-sensor-ble.yaml`, `move_sensor_rtc.h`, сайт/точка доступа `move_sensor_ble/`, компонент `components/bthome` (BTHome + BLE OTA, DIAG, настройки, сброс Wi-Fi, быстрый тест). Секреты — `secrets.example.yaml` |
| `tools/ble_ota_flash.py` | Сборка + прошивка по BLE из терминала, без браузера (только stdlib Python) |
| `sync_from_ha.sh` | Скопировать рабочие файлы из `/Volumes/config` в репозиторий перед коммитом |

## Работа с репозиторием

Рабочая копия живёт в Home Assistant (`/config/esphome`, `/config/custom_components`). Репозиторий — её снимок:

```sh
./sync_from_ha.sh          # забрать изменения из HA
git add -A && git commit -m "..."
```

## Установка интеграции

1. Кнопка **Open your Home Assistant instance and open a repository…** → адрес своего HA (один раз) →
   откроется Marketplace (HA 2026.11+) или HACS с этим репозиторием → **Скачать** → перезапустить HA.
2. Кнопка **Add integration** → мастер *Move Sensor BLE OTA* → выбрать датчик.

Вручную: Настройки → **Marketplace** → **Добавить по ссылке** (в HACS: ⋮ → *Custom repositories*) →
`https://github.com/OWNER/move-sensor-ble-ota`, тип *Integration*.

Прошивку датчика собрать из `esphome/` (скопировать в `/config/esphome/`, секреты по `secrets.example.yaml`).

## Публикация

`./tools/set_github.sh <github-логин>` — подставит логин вместо `OWNER` в README, доки и `manifest.json`
(`codeowners`, `issue_tracker`). Для обновлений в Marketplace — GitHub-релиз с тегом = `version` из `manifest.json`.
