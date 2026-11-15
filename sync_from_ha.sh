#!/bin/sh
# Копирует ПРОШИВКУ (esphome/, tools/) из Home Assistant (/Volumes/config) в репозиторий.
# Интеграция custom_components/ живёт здесь: её правят в репозитории и ставят в HA через Marketplace (релиз = версия).
# Запускать перед коммитом:  ./sync_from_ha.sh  (CONFIG=/путь/к/config — если смонтировано иначе)
set -e
CONFIG="${CONFIG:-/Volumes/config}"
cd "$(dirname "$0")"
mkdir -p esphome/components/bthome esphome/move_sensor_ble tools
EX="--exclude=__pycache__ --exclude=*.bak* --exclude=*.tgz --exclude=*.part --exclude=.DS_Store --exclude=.smbdelete* --exclude=._* --exclude=_to_delete --exclude=*.old_*"
rsync -a --delete $EX "$CONFIG/esphome/components/bthome/" esphome/components/bthome/
rsync -a --delete $EX --exclude=site_bundle.js "$CONFIG/esphome/move_sensor_ble/" esphome/move_sensor_ble/
cp "$CONFIG/esphome/move_sensor_ble/site_bundle.js" esphome/move_sensor_ble/site_bundle.js
cp "$CONFIG"/esphome/move-sensor-ble-*.yaml "$CONFIG/esphome/move_sensor_rtc.h" esphome/   # 1.13.1: YAML на каждый датчик
cp "$CONFIG/esphome/tools/ble_ota_flash.py" tools/
echo "synced from $CONFIG"
