# Move Sensor BLE

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

## Установка интеграции через HACS

HACS → Integrations → ⋮ → Custom repositories → URL репозитория, тип *Integration* → установить
**Move Sensor BLE OTA** → перезапустить HA → Настройки → Устройства и службы → Добавить → Move Sensor BLE OTA.
