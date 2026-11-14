#pragma once
// BLE OTA для Move Sensor BLE (2026-11-14).
// GATT-сервис поверх NimBLE, который поднимает компонент bthome.
// Пока датчик не спит, реклама подключаемая; клиент (интеграция Home Assistant
// move_sensor_ble_ota через Bluetooth-прокси) подключается в окно пробуждения,
// проходит HMAC-авторизацию и заливает образ приложения (firmware.bin / OTA format).
//
// Протокол (все числа little-endian):
//   CTRL (write + notify) — команды и ответы:
//     0x01 HELLO                    -> 0x81 st nonce[16] battery(0..100, 255 = неизвестно)
//     0x02 AUTH  hmac[16]           -> 0x82 st      hmac = первые 16 байт HMAC-SHA256(key, nonce)
//     0x03 BEGIN size:u32 crc32:u32 -> 0x83 st block:u16
//     0x05 END                      -> 0x85 st      (затем перезагрузка через 1.5 с)
//     0x06 ABORT                    -> 0x86 0
//     0x07 STATUS                   -> 0x87 st received:u32
//     0x08 INFO                     -> 0x88 st "версия сборки" (Build Version, иначе версия ESPHome)
//     0x09 DIAG (после авторизации)  -> 0x89 st last_reason:u8 poweron:u16 brownout:u16 panic:u16
//                                         watchdog:u16 software:u16 other:u16            (2026-11-14)
//       Счётчики загрузок «не из сна» по причине сброса (esp_reset_reason), хранятся в NVS
//       (namespace ms_diag), переживают и brownout, и снятие питания. last_reason — esp_reset_reason_t
//       последней такой загрузки. watchdog = INT_WDT + TASK_WDT + WDT; software = SW (OTA, restart);
//       other = всё прочее (reset pin, USB, JTAG, unknown...). Пробуждения из deep sleep не считаются.
//     0x12 WIFI_RESET (после авторизации) -> 0x92 st                                 (2026-11-14)
//       Сбросить сохранённую сеть Wi-Fi на сеть из прошивки. Выполняет колбэк из YAML
//       (set_wifi_reset_callback); нет колбэка -> st 10.
//     0x13 TEST_MODE minutes:u8 [tx_window_ms:u16] (после авторизации) -> 0x93 st minutes  (2026-11-14)
//       tx_window_ms (необязательно, 0 = как в прошивке): окно рекламы после пакета на время теста
//       Быстрый тест: без движения просыпаться раз в 30 с, а не по Climate interval; само
//       выключается через minutes мин (0 = выключить). Колбэк из YAML (set_test_mode_callback).
//   DATA (write without response): offset:u32 + данные. Принимается только кусок с
//     offset == уже принятому. Блок = 4096 байт; после записи блока во флеш —
//     notify в CTRL: 0x84 st written:u32. Клиент шлёт следующий блок только после ACK.
//   Настройки (2026-11-14, после авторизации; значения u16, 0xFFFF = нет значения, led 255 = нет):
//     0x10 GET                                   -> 0x90 st detect_ms keep_s climate_min led vfactor*1000 motion_win_ms climate_win_ms
//     0x11 SET mask detect_ms keep_s climate_min led vfactor*1000 [motion_win_ms climate_win_ms]
//                                                -> 0x91 st (фактические значения после применения, как 0x90)
//       mask: bit0 detect delay, bit1 keep time, bit2 climate interval, bit3 LED, bit4 voltage factor,
//             bit5 motion window, bit6 climate window (окна — только если пришли все 13 байт; 2026-11-14)
//       применяются через number/switch из YAML — сохраняются во флеш (restore_value) как из сервисного режима
//   st: 0 ok, 1 нет авторизации, 2 батарея ниже порога, 3 неверный размер,
//       4 ошибка esp_ota_begin, 5 ошибка записи, 6 чужой/битый образ,
//       7 CRC не совпал, 8 образ не прошёл проверку, 9 не удалось выбрать раздел,
//       10 неверное состояние/команда

#include "esphome/core/defines.h"

#ifdef USE_BTHOME_BLE_OTA

#include "esphome/core/component.h"
#include "host/ble_hs.h"
#include <esp_ota_ops.h>
#include <atomic>
#include <functional>
#include <string>

#if defined(USE_NUMBER) && defined(USE_SWITCH)
#define BLE_OTA_SETTINGS 1
#include "esphome/components/number/number.h"
#include "esphome/components/switch/switch.h"
#endif

namespace esphome {
namespace bthome {

class BTHome;

class BleOta : public Component {
 public:
  BleOta() { instance = this; }

  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_BLUETOOTH; }

  void set_parent(BTHome *parent) { this->parent_ = parent; }
  void set_key(const std::string &key) { this->key_ = key; }
  void set_min_battery(int pct) { this->min_battery_ = pct; }
  // текущий заряд, %, из YAML; -1 = неизвестно (тогда порог не проверяется)
  void set_battery_percent(int pct) { this->battery_pct_ = pct; }
  // строка версии сборки (как «Build Version» в сервисном режиме) — отдаётся по INFO
  void set_build_version(const std::string &v) { this->build_version_ = v; }
  // сброс Wi-Fi на сеть из прошивки по команде 0x12 — логика в YAML (флаг + save_wifi_sta)
  void set_wifi_reset_callback(std::function<void()> cb) { this->wifi_reset_cb_ = std::move(cb); }
  void set_test_mode_callback(std::function<void(uint8_t, uint16_t)> cb) { this->test_mode_cb_ = std::move(cb); }

#ifdef BLE_OTA_SETTINGS
  // настройки, которые HA может менять через окно BLE
  void set_detect_delay(number::Number *n) { this->detect_delay_ = n; }
  void set_keep_time(number::Number *n) { this->keep_time_ = n; }
  void set_climate_interval(number::Number *n) { this->climate_interval_ = n; }
  void set_voltage_factor(number::Number *n) { this->voltage_factor_ = n; }
  void set_led(switch_::Switch *s) { this->led_ = s; }
  void set_motion_window(number::Number *n) { this->motion_window_ = n; }
  void set_climate_window(number::Number *n) { this->climate_window_ = n; }
#endif

  // занят: есть подключение, идёт приём или ждём перезагрузку — спать нельзя
  bool is_busy() const;
  bool is_connected() const { return this->conn_handle_.load() != BLE_HS_CONN_HANDLE_NONE; }

  // Защёлка перед сном (go_sleep): первый вызов делает рекламу неподключаемой; true, когда
  // прошло >= 300 мс без подключения (успело бы прийти уже идущее) — можно спать.
  // Пока клиент подключён — false; после его отключения снова true через 300 мс.
  bool sleep_ready();
  // снять защёлку (новое окно: go_sleep перезапущен движением)
  void sleep_unlock();
  bool is_sleep_locked() const { return this->sleep_locked_; }

  // вызывается компонентом bthome после nimble_port_init(), до старта host task
  static int register_gatt(const char *device_name);
  // GAP-события рекламы/подключения (host task)
  static int gap_event(struct ble_gap_event *event, void *arg);

  static BleOta *instance;

  // доступ из GATT callback (host task)
  int on_ctrl_write_(struct os_mbuf *om);
  int on_data_write_(struct os_mbuf *om);

 protected:
  enum State : uint8_t { ST_IDLE = 0, ST_AUTHED, ST_RECEIVING, ST_DONE };

  void handle_command_();
  void process_block_();
  void reply_(const uint8_t *data, size_t len);
  void abort_ota_(const char *why);
  void terminate_();
  void reset_session_();
  size_t settings_pack_(uint8_t *out);
  void diag_boot_();  // посчитать причину сброса этой загрузки (NVS)
  void settings_apply_(const uint8_t *p, uint8_t mask, size_t len);

#ifdef BLE_OTA_SETTINGS
  number::Number *detect_delay_{nullptr};
  number::Number *keep_time_{nullptr};
  number::Number *climate_interval_{nullptr};
  number::Number *voltage_factor_{nullptr};
  switch_::Switch *led_{nullptr};
  number::Number *motion_window_{nullptr};
  number::Number *climate_window_{nullptr};
#endif

  BTHome *parent_{nullptr};
  std::string key_;
  std::string build_version_;
  std::function<void()> wifi_reset_cb_;
  std::function<void(uint8_t, uint16_t)> test_mode_cb_;
  int min_battery_{30};
  int battery_pct_{-1};

  // диагностика сбросов (DIAG): poweron, brownout, panic, watchdog, software, other
  enum { DIAG_POWERON = 0, DIAG_BROWNOUT, DIAG_PANIC, DIAG_WDT, DIAG_SW, DIAG_OTHER, DIAG_N };
  uint16_t diag_[DIAG_N]{};
  uint8_t diag_last_{0};

  // общее с host task
  std::atomic<uint16_t> conn_handle_{BLE_HS_CONN_HANDLE_NONE};
  std::atomic<bool> ev_connected_{false};
  std::atomic<bool> ev_disconnected_{false};
  std::atomic<bool> ev_readvertise_{false};
  std::atomic<bool> cmd_pending_{false};
  uint8_t cmd_buf_[40];
  uint16_t cmd_len_{0};
  std::atomic<bool> receiving_{false};
  std::atomic<bool> block_ready_{false};
  uint8_t *block_{nullptr};
  std::atomic<uint32_t> block_len_{0};
  std::atomic<uint32_t> rx_expected_{0};  // offset следующего ожидаемого байта
  uint16_t mtu_{23};

  // только main loop
  bool sleep_locked_{false};
  uint32_t sleep_lock_ms_{0};
  State state_{ST_IDLE};
  uint8_t nonce_[16];
  bool nonce_valid_{false};
  uint32_t conn_start_ms_{0};
  uint32_t last_activity_ms_{0};
  uint32_t image_size_{0};
  uint32_t expected_crc_{0};
  uint32_t crc_{0};
  uint32_t written_{0};
  uint32_t next_log_{0};
  uint32_t reboot_at_{0};
  esp_ota_handle_t handle_{0};
  const esp_partition_t *partition_{nullptr};
};

}  // namespace bthome
}  // namespace esphome

#endif  // USE_BTHOME_BLE_OTA
