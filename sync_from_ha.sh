#!/bin/sh
# Копирует рабочие файлы из Home Assistant (/Volumes/config) в репозиторий.
# Запускать перед коммитом:  ./sync_from_ha.sh  (CONFIG=/путь/к/config — если смонтировано иначе)
set -e
CONFIG="${CONFIG:-/Volumes/config}"
cd "$(dirname "$0")"
mkdir -p custom_components/move_sensor_ble_ota esphome/components/bthome esphome/move_sensor_ble tools
EX="--exclude=__pycache__ --exclude=*.bak* --exclude=*.tgz --exclude=*.part --exclude=.DS_Store"
rsync -a --delete $EX "$CONFIG/custom_components/move_sensor_ble_ota/" custom_components/move_sensor_ble_ota/
rsync -a --delete $EX "$CONFIG/esphome/components/bthome/" esphome/components/bthome/
rsync -a --delete $EX --exclude=site_bundle.js "$CONFIG/esphome/move_sensor_ble/" esphome/move_sensor_ble/
cp "$CONFIG/esphome/move_sensor_ble/site_bundle.js" esphome/move_sensor_ble/site_bundle.js
cp "$CONFIG/esphome/move-sensor-ble.yaml" "$CONFIG/esphome/move_sensor_rtc.h" esphome/
cp "$CONFIG/esphome/tools/ble_ota_flash.py" tools/
echo "synced from $CONFIG"
