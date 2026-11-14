#include "ble_ota.h"

#ifdef USE_BTHOME_BLE_OTA

#include "bthome.h"
#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include "host/ble_hs.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include <esp_app_desc.h>
#include <esp_random.h>
#include <esp_ota_ops.h>
#include <esp_system.h>
#include <nvs.h>
#include "mbedtls/md.h"
#include <cmath>
#include <cstring>

namespace esphome {
namespace bthome {

static const char *const TAG = "ble_ota";

static const uint32_t OTA_BLOCK = 4096;
static const uint32_t AUTH_TIMEOUT_MS = 20000;   // подключился, но не авторизовался
static const uint32_t IDLE_TIMEOUT_MS = 60000;   // авторизован, но ничего не делает
static const uint32_t DATA_TIMEOUT_MS = 30000;   // идёт приём, данных нет

enum : uint8_t {
  CMD_HELLO = 0x01,
  CMD_AUTH = 0x02,
  CMD_BEGIN = 0x03,
  CMD_END = 0x05,
  CMD_ABORT = 0x06,
  CMD_STATUS = 0x07,
  CMD_INFO = 0x08,
  CMD_DIAG = 0x09,
  CMD_WIFI_RESET = 0x12,
  CMD_TEST_MODE = 0x13,
  CMD_GET_SETTINGS = 0x10,
  CMD_SET_SETTINGS = 0x11,
  RSP_ACK = 0x84,
};

// масштаб настроек в протоколе (u16): ms, s, min, —, фактор × 1000
static const float SCALE_VFACTOR = 1000.0f;

enum : uint8_t {
  ST_OK = 0,
  ST_NOT_AUTH = 1,
  ST_BATTERY = 2,
  ST_SIZE = 3,
  ST_BEGIN_ERR = 4,
  ST_WRITE_ERR = 5,
  ST_BAD_IMAGE = 6,
  ST_CRC = 7,
  ST_VALIDATE = 8,
  ST_BOOT_PART = 9,
  ST_STATE = 10,
};

// 6d730001-626c-6f74-4d6f-766553656e73 и далее ...0002 (CTRL), ...0003 (DATA)
static const ble_uuid128_t SVC_UUID = BLE_UUID128_INIT(
    0x73, 0x6e, 0x65, 0x53, 0x65, 0x76, 0x6f, 0x4d, 0x74, 0x6f, 0x6c, 0x62, 0x01, 0x00, 0x73, 0x6d);
static const ble_uuid128_t CTRL_UUID = BLE_UUID128_INIT(
    0x73, 0x6e, 0x65, 0x53, 0x65, 0x76, 0x6f, 0x4d, 0x74, 0x6f, 0x6c, 0x62, 0x02, 0x00, 0x73, 0x6d);
static const ble_uuid128_t DATA_UUID = BLE_UUID128_INIT(
    0x73, 0x6e, 0x65, 0x53, 0x65, 0x76, 0x6f, 0x4d, 0x74, 0x6f, 0x6c, 0x62, 0x03, 0x00, 0x73, 0x6d);

static uint16_t s_ctrl_handle = 0;
static uint16_t s_data_handle = 0;
static struct ble_gatt_chr_def s_chrs[3];
static struct ble_gatt_svc_def s_svcs[2];

BleOta *BleOta::instance = nullptr;

static inline void put_u32(uint8_t *p, uint32_t v) {
  p[0] = v & 0xFF;
  p[1] = (v >> 8) & 0xFF;
  p[2] = (v >> 16) & 0xFF;
  p[3] = (v >> 24) & 0xFF;
}
static inline uint32_t get_u32(const uint8_t *p) {
  return (uint32_t) p[0] | ((uint32_t) p[1] << 8) | ((uint32_t) p[2] << 16) | ((uint32_t) p[3] << 24);
}

// CRC-32 (IEEE, как zlib.crc32): начальное 0xFFFFFFFF, в конце инверсия
static uint32_t crc32_update(uint32_t crc, const uint8_t *data, size_t len) {
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (int k = 0; k < 8; k++)
      crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
  }
  return crc;
}

static int gatt_access(uint16_t conn_handle, uint16_t attr_handle, struct ble_gatt_access_ctxt *ctxt, void *arg) {
  BleOta *self = BleOta::instance;
  if (self == nullptr)
    return BLE_ATT_ERR_UNLIKELY;
  if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR)
    return BLE_ATT_ERR_READ_NOT_PERMITTED;
  if (attr_handle == s_ctrl_handle)
    return self->on_ctrl_write_(ctxt->om);
  if (attr_handle == s_data_handle)
    return self->on_data_write_(ctxt->om);
  return BLE_ATT_ERR_UNLIKELY;
}

int BleOta::register_gatt(const char *device_name) {
  memset(s_chrs, 0, sizeof(s_chrs));
  memset(s_svcs, 0, sizeof(s_svcs));

  s_chrs[0].uuid = &CTRL_UUID.u;
  s_chrs[0].access_cb = gatt_access;
  s_chrs[0].flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_NOTIFY;
  s_chrs[0].val_handle = &s_ctrl_handle;

  s_chrs[1].uuid = &DATA_UUID.u;
  s_chrs[1].access_cb = gatt_access;
  s_chrs[1].flags = BLE_GATT_CHR_F_WRITE_NO_RSP | BLE_GATT_CHR_F_WRITE;
  s_chrs[1].val_handle = &s_data_handle;
  // s_chrs[2] — нулевой терминатор

  s_svcs[0].type = BLE_GATT_SVC_TYPE_PRIMARY;
  s_svcs[0].uuid = &SVC_UUID.u;
  s_svcs[0].characteristics = s_chrs;
  // s_svcs[1] — нулевой терминатор

  ble_svc_gap_init();
  ble_svc_gatt_init();
  if (device_name != nullptr)
    ble_svc_gap_device_name_set(device_name);

  int rc = ble_gatts_count_cfg(s_svcs);
  if (rc != 0) {
    ESP_LOGE(TAG, "ble_gatts_count_cfg failed: %d", rc);
    return rc;
  }
  rc = ble_gatts_add_svcs(s_svcs);
  if (rc != 0) {
    ESP_LOGE(TAG, "ble_gatts_add_svcs failed: %d", rc);
    return rc;
  }
  ble_att_set_preferred_mtu(517);
  return 0;
}

int BleOta::gap_event(struct ble_gap_event *event, void *arg) {
  BleOta *self = BleOta::instance;
  if (self == nullptr)
    return 0;
  switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
      if (event->connect.status == 0) {
        self->conn_handle_.store(event->connect.conn_handle);
        self->mtu_ = 23;
        self->ev_connected_.store(true);
      } else {
        // подключение не удалось — реклама остановлена, перезапустить
        self->ev_readvertise_.store(true);
      }
      self->enable_loop_soon_any_context();
      break;
    case BLE_GAP_EVENT_DISCONNECT:
      self->conn_handle_.store(BLE_HS_CONN_HANDLE_NONE);
      self->receiving_.store(false);
      self->ev_disconnected_.store(true);
      self->enable_loop_soon_any_context();
      break;
    case BLE_GAP_EVENT_ADV_COMPLETE:
      self->ev_readvertise_.store(true);
      self->enable_loop_soon_any_context();
      break;
    case BLE_GAP_EVENT_MTU:
      self->mtu_ = event->mtu.value;
      break;
    default:
      break;
  }
  return 0;
}

// --- host task: команды кладём в почтовый ящик, разбираем в loop() ---
int BleOta::on_ctrl_write_(struct os_mbuf *om) {
  if (this->cmd_pending_.load())
    return BLE_ATT_ERR_PREPARE_QUEUE_FULL;  // клиент шлёт следующую команду только после ответа
  uint16_t len = 0;
  if (ble_hs_mbuf_to_flat(om, this->cmd_buf_, sizeof(this->cmd_buf_), &len) != 0 || len == 0)
    return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
  this->cmd_len_ = len;
  this->cmd_pending_.store(true);
  this->enable_loop_soon_any_context();
  return 0;
}

// --- host task: данные копируем в буфер блока; во флеш пишет loop() ---
int BleOta::on_data_write_(struct os_mbuf *om) {
  static uint8_t tmp[520];
  uint16_t len = 0;
  if (ble_hs_mbuf_to_flat(om, tmp, sizeof(tmp), &len) != 0 || len < 5)
    return 0;
  if (!this->receiving_.load() || this->block_ready_.load() || this->block_ == nullptr)
    return 0;  // не ждём данных / блок ещё пишется — клиент перешлёт после ACK/STATUS
  const uint32_t offset = get_u32(tmp);
  if (offset != this->rx_expected_)
    return 0;  // не тот кусок — игнор, клиент восстановится по STATUS
  uint32_t n = len - 4;
  if (this->block_len_ + n > OTA_BLOCK || offset + n > this->image_size_)
    return 0;  // кусок пересекает границу блока или конец образа — клиент так не шлёт
  memcpy(this->block_ + this->block_len_, tmp + 4, n);
  this->block_len_ += n;
  this->rx_expected_ += n;
  if (this->block_len_ == OTA_BLOCK || this->rx_expected_ == this->image_size_) {
    this->block_ready_.store(true);
    this->enable_loop_soon_any_context();
  }
  return 0;
}

void BleOta::setup() {
  // причина сброса — до всего остального: при brownout следующая просадка может прийти скоро
  this->diag_boot_();
  // буфер блока выделяем сразу: в момент OTA куча может быть фрагментирована
  this->block_ = new uint8_t[OTA_BLOCK];
  if (this->block_ == nullptr) {
    ESP_LOGE(TAG, "no memory for OTA block buffer");
    this->mark_failed();
  }
}

// 2026-11-14: счётчики загрузок «не из сна» по причине сброса. NVS (не RTC-память: она после
// brownout/panic переинициализируется) и сразу nvs_commit — следующего шанса может не быть.
// NVS к этому моменту уже инициализирован компонентом bthome (nvs_flash_init в его setup).
void BleOta::diag_boot_() {
  const esp_reset_reason_t rr = esp_reset_reason();
  nvs_handle_t h;
  if (nvs_open("ms_diag", NVS_READWRITE, &h) != ESP_OK) {
    ESP_LOGW(TAG, "diag: nvs_open failed");
    return;
  }
  size_t sz = sizeof(this->diag_);
  if (nvs_get_blob(h, "cnt", this->diag_, &sz) != ESP_OK || sz != sizeof(this->diag_))
    memset(this->diag_, 0, sizeof(this->diag_));
  uint8_t last = 0;
  if (nvs_get_u8(h, "last", &last) == ESP_OK)
    this->diag_last_ = last;
  if (rr != ESP_RST_DEEPSLEEP) {
    int idx;
    switch (rr) {
      case ESP_RST_POWERON:  idx = DIAG_POWERON; break;
      case ESP_RST_BROWNOUT: idx = DIAG_BROWNOUT; break;
      case ESP_RST_PANIC:    idx = DIAG_PANIC; break;
      case ESP_RST_INT_WDT:
      case ESP_RST_TASK_WDT:
      case ESP_RST_WDT:      idx = DIAG_WDT; break;
      case ESP_RST_SW:       idx = DIAG_SW; break;
      default:               idx = DIAG_OTHER; break;
    }
    if (this->diag_[idx] < 0xFFFF)
      this->diag_[idx]++;
    this->diag_last_ = (uint8_t) rr;
    nvs_set_blob(h, "cnt", this->diag_, sizeof(this->diag_));
    nvs_set_u8(h, "last", this->diag_last_);
    nvs_commit(h);
    ESP_LOGW(TAG, "reset reason %d: poweron %u brownout %u panic %u wdt %u sw %u other %u", (int) rr,
             this->diag_[DIAG_POWERON], this->diag_[DIAG_BROWNOUT], this->diag_[DIAG_PANIC],
             this->diag_[DIAG_WDT], this->diag_[DIAG_SW], this->diag_[DIAG_OTHER]);
  }
  nvs_close(h);
}

void BleOta::dump_config() {
  ESP_LOGCONFIG(TAG,
                "BLE OTA:\n"
                "  Service: 6d730001-626c-6f74-4d6f-766553656e73\n"
                "  Min battery: %d%%\n"
                "  Key: %s",
                this->min_battery_, this->key_.empty() ? "NOT SET" : "set");
}

bool BleOta::sleep_ready() {
  const uint32_t now = millis();
  if (!this->sleep_locked_) {
    this->sleep_locked_ = true;
    this->sleep_lock_ms_ = now;
    ESP_LOGD(TAG, "sleep lock: advertising non-connectable");
    if (this->parent_ != nullptr && !this->is_connected())
      this->parent_->restart_advertising();
    return false;
  }
  if (this->is_busy() || this->ev_connected_.load() || this->cmd_pending_.load()) {
    this->sleep_lock_ms_ = now;  // после отключения клиента снова выждать
    return false;
  }
  return now - this->sleep_lock_ms_ >= 300;
}

void BleOta::sleep_unlock() {
  if (!this->sleep_locked_)
    return;
  this->sleep_locked_ = false;
  ESP_LOGD(TAG, "sleep unlock: advertising connectable");
  if (this->parent_ != nullptr && !this->is_connected())
    this->parent_->restart_advertising();
}

bool BleOta::is_busy() const {
  return this->is_connected() || this->state_ == ST_RECEIVING || this->reboot_at_ != 0;
}

void BleOta::reply_(const uint8_t *data, size_t len) {
  uint16_t conn = this->conn_handle_.load();
  if (conn == BLE_HS_CONN_HANDLE_NONE)
    return;
  struct os_mbuf *om = ble_hs_mbuf_from_flat(data, len);
  if (om == nullptr) {
    ESP_LOGW(TAG, "no mbuf for reply");
    return;
  }
  int rc = ble_gatts_notify_custom(conn, s_ctrl_handle, om);
  if (rc != 0)
    ESP_LOGW(TAG, "notify failed: %d", rc);
}

void BleOta::terminate_() {
  uint16_t conn = this->conn_handle_.load();
  if (conn != BLE_HS_CONN_HANDLE_NONE)
    ble_gap_terminate(conn, BLE_ERR_REM_USER_CONN_TERM);
}

void BleOta::abort_ota_(const char *why) {
  if (this->state_ == ST_RECEIVING) {
    this->receiving_.store(false);
    esp_ota_abort(this->handle_);
    this->handle_ = 0;
    ESP_LOGW(TAG, "OTA aborted: %s (received %u of %u)", why, (unsigned) this->written_,
             (unsigned) this->image_size_);
  }
  this->block_len_ = 0;
  this->block_ready_.store(false);
  if (this->state_ != ST_DONE)
    this->state_ = this->is_connected() ? ST_AUTHED : ST_IDLE;
}

void BleOta::reset_session_() {
  this->abort_ota_("disconnected");
  this->state_ = (this->state_ == ST_DONE) ? ST_DONE : ST_IDLE;
  this->nonce_valid_ = false;
  this->cmd_pending_.store(false);
}

void BleOta::loop() {
  const uint32_t now = millis();

  if (this->ev_connected_.exchange(false)) {
    ESP_LOGI(TAG, "client connected (conn %u)", (unsigned) this->conn_handle_.load());
    this->state_ = ST_IDLE;
    this->nonce_valid_ = false;
    this->conn_start_ms_ = now;
    this->last_activity_ms_ = now;
    // стек остановил рекламу; продолжаем BTHome-рекламу, уже неподключаемую
    if (this->parent_ != nullptr)
      this->parent_->restart_advertising();
  }

  if (this->ev_disconnected_.exchange(false)) {
    ESP_LOGI(TAG, "client disconnected");
    this->reset_session_();
    if (this->parent_ != nullptr)
      this->parent_->restart_advertising();  // снова подключаемая
  }

  if (this->ev_readvertise_.exchange(false) && this->parent_ != nullptr && this->reboot_at_ == 0)
    this->parent_->restart_advertising();

  if (this->cmd_pending_.load()) {
    this->handle_command_();
    this->cmd_pending_.store(false);
  }

  if (this->block_ready_.load())
    this->process_block_();

  if (this->is_connected() && this->reboot_at_ == 0) {
    // время берём заново: команда/блок выше могли обновить last_activity_ms_ позже `now`,
    // и беззнаковая разность (now - last) ушла бы в 4 млрд -> ложный таймаут
    // (2026-11-14: так OTA обрывалась сразу после первого блока «no data»)
    const uint32_t t = millis();
    auto elapsed = [t](uint32_t since) -> uint32_t { return (int32_t) (t - since) > 0 ? t - since : 0; };
    if (this->state_ == ST_IDLE && elapsed(this->conn_start_ms_) > AUTH_TIMEOUT_MS) {
      ESP_LOGW(TAG, "no auth in %u s - disconnect", (unsigned) (AUTH_TIMEOUT_MS / 1000));
      this->terminate_();
    } else if (this->state_ == ST_AUTHED && elapsed(this->last_activity_ms_) > IDLE_TIMEOUT_MS) {
      ESP_LOGW(TAG, "idle - disconnect");
      this->terminate_();
    } else if (this->state_ == ST_RECEIVING && elapsed(this->last_activity_ms_) > DATA_TIMEOUT_MS) {
      this->abort_ota_("no data");
      this->terminate_();
    }
  }

  if (this->reboot_at_ != 0 && (int32_t) (now - this->reboot_at_) >= 0) {
    ESP_LOGW(TAG, "rebooting into new firmware");
    this->reboot_at_ = 0;
    this->terminate_();
    App.safe_reboot();
  }
}

void BleOta::handle_command_() {
  const uint8_t *c = this->cmd_buf_;
  const uint16_t len = this->cmd_len_;
  this->last_activity_ms_ = millis();
  uint8_t r[40];

  switch (c[0]) {
    case CMD_HELLO: {
      esp_fill_random(this->nonce_, sizeof(this->nonce_));
      this->nonce_valid_ = true;
      r[0] = 0x81;
      r[1] = ST_OK;
      memcpy(r + 2, this->nonce_, 16);
      r[18] = (this->battery_pct_ >= 0 && this->battery_pct_ <= 100) ? (uint8_t) this->battery_pct_ : 255;
      this->reply_(r, 19);
      ESP_LOGD(TAG, "HELLO (mtu %u)", (unsigned) this->mtu_);
      break;
    }
    case CMD_AUTH: {
      r[0] = 0x82;
      r[1] = ST_NOT_AUTH;
      if (len >= 17 && this->nonce_valid_ && !this->key_.empty()) {
        uint8_t mac[32];
        const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
        if (mbedtls_md_hmac(md, (const unsigned char *) this->key_.data(), this->key_.size(), this->nonce_,
                            sizeof(this->nonce_), mac) == 0) {
          uint8_t diff = 0;
          for (int i = 0; i < 16; i++)
            diff |= mac[i] ^ c[1 + i];
          if (diff == 0) {
            r[1] = ST_OK;
            if (this->state_ == ST_IDLE)
              this->state_ = ST_AUTHED;
          }
        }
      }
      this->nonce_valid_ = false;  // nonce одноразовый
      ESP_LOGI(TAG, "AUTH %s", r[1] == ST_OK ? "ok" : "FAILED");
      this->reply_(r, 2);
      if (r[1] != ST_OK)
        this->terminate_();
      break;
    }
    case CMD_INFO: {
      const esp_app_desc_t *d = esp_app_get_description();
      r[0] = 0x88;
      r[1] = ST_OK;
      const char *ver = this->build_version_.empty() ? d->version : this->build_version_.c_str();
      size_t vl = this->build_version_.empty() ? strnlen(d->version, sizeof(d->version)) : this->build_version_.size();
      size_t max = (this->mtu_ > 3 ? this->mtu_ - 3 : 20);
      if (max > sizeof(r))
        max = sizeof(r);
      if (vl > max - 2)
        vl = max - 2;
      memcpy(r + 2, ver, vl);
      this->reply_(r, 2 + vl);
      break;
    }
    case CMD_TEST_MODE: {
      r[0] = 0x93;
      r[2] = len >= 2 ? c[1] : 0;
      if (this->state_ == ST_IDLE) {
        r[1] = ST_NOT_AUTH;
      } else if (!this->test_mode_cb_) {
        r[1] = ST_STATE;
      } else {
        ESP_LOGW(TAG, "TEST_MODE %u min over BLE", (unsigned) r[2]);
        this->test_mode_cb_(r[2]);
        r[1] = ST_OK;
      }
      this->reply_(r, 3);
      break;
    }
    case CMD_WIFI_RESET: {
      r[0] = 0x92;
      if (this->state_ == ST_IDLE) {
        r[1] = ST_NOT_AUTH;
      } else if (!this->wifi_reset_cb_) {
        r[1] = ST_STATE;
      } else {
        ESP_LOGW(TAG, "WIFI_RESET over BLE");
        this->wifi_reset_cb_();
        r[1] = ST_OK;
      }
      this->reply_(r, 2);
      break;
    }
    case CMD_DIAG: {
      r[0] = 0x89;
      if (this->state_ == ST_IDLE) {
        r[1] = ST_NOT_AUTH;
        this->reply_(r, 2);
        break;
      }
      r[1] = ST_OK;
      r[2] = this->diag_last_;
      for (int i = 0; i < DIAG_N; i++) {
        r[3 + i * 2] = this->diag_[i] & 0xFF;
        r[4 + i * 2] = this->diag_[i] >> 8;
      }
      this->reply_(r, 3 + DIAG_N * 2);  // 15 байт — влезает в минимальный MTU (20)
      break;
    }
    case CMD_BEGIN: {
      r[0] = 0x83;
      r[2] = OTA_BLOCK & 0xFF;
      r[3] = (OTA_BLOCK >> 8) & 0xFF;
      if (this->state_ == ST_RECEIVING)
        this->abort_ota_("restart");
      if (this->state_ != ST_AUTHED) {
        r[1] = this->state_ == ST_IDLE ? ST_NOT_AUTH : ST_STATE;
        this->reply_(r, 4);
        break;
      }
      if (len < 9) {
        r[1] = ST_STATE;
        this->reply_(r, 4);
        break;
      }
      if (this->battery_pct_ >= 0 && this->battery_pct_ < this->min_battery_) {
        ESP_LOGW(TAG, "battery %d%% < %d%% - OTA refused", this->battery_pct_, this->min_battery_);
        r[1] = ST_BATTERY;
        this->reply_(r, 4);
        break;
      }
      const uint32_t size = get_u32(c + 1);
      this->partition_ = esp_ota_get_next_update_partition(nullptr);
      if (this->partition_ == nullptr || size < 1024 || size > this->partition_->size) {
        ESP_LOGW(TAG, "bad image size %u (partition %u)", (unsigned) size,
                 this->partition_ ? (unsigned) this->partition_->size : 0u);
        r[1] = ST_SIZE;
        this->reply_(r, 4);
        break;
      }
      esp_err_t err = esp_ota_begin(this->partition_, OTA_WITH_SEQUENTIAL_WRITES, &this->handle_);
      if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_begin: %s", esp_err_to_name(err));
        r[1] = ST_BEGIN_ERR;
        this->reply_(r, 4);
        break;
      }
      this->image_size_ = size;
      this->expected_crc_ = get_u32(c + 5);
      this->crc_ = 0xFFFFFFFFu;
      this->written_ = 0;
      this->next_log_ = 64 * 1024;
      this->block_len_ = 0;
      this->rx_expected_ = 0;
      this->block_ready_.store(false);
      this->state_ = ST_RECEIVING;
      this->receiving_.store(true);
      ESP_LOGI(TAG, "OTA begin: %u bytes -> %s", (unsigned) size, this->partition_->label);
      r[1] = ST_OK;
      this->reply_(r, 4);
      break;
    }
    case CMD_STATUS: {
      r[0] = 0x87;
      r[1] = this->state_ == ST_RECEIVING ? ST_OK : ST_STATE;
      put_u32(r + 2, this->rx_expected_);
      this->reply_(r, 6);
      break;
    }
    case CMD_END: {
      r[0] = 0x85;
      if (this->state_ != ST_RECEIVING || this->written_ != this->image_size_ || this->block_ready_.load()) {
        r[1] = ST_STATE;
        this->reply_(r, 2);
        break;
      }
      this->receiving_.store(false);
      const uint32_t crc = ~this->crc_;
      if (crc != this->expected_crc_) {
        ESP_LOGE(TAG, "CRC mismatch: got %08X, expected %08X", (unsigned) crc, (unsigned) this->expected_crc_);
        this->abort_ota_("crc");
        r[1] = ST_CRC;
        this->reply_(r, 2);
        break;
      }
      esp_err_t err = esp_ota_end(this->handle_);
      this->handle_ = 0;
      if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_end: %s", esp_err_to_name(err));
        this->state_ = ST_AUTHED;
        r[1] = ST_VALIDATE;
        this->reply_(r, 2);
        break;
      }
      err = esp_ota_set_boot_partition(this->partition_);
      if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_set_boot_partition: %s", esp_err_to_name(err));
        this->state_ = ST_AUTHED;
        r[1] = ST_BOOT_PART;
        this->reply_(r, 2);
        break;
      }
      this->state_ = ST_DONE;
      this->reboot_at_ = millis() + 1500;
      if (this->reboot_at_ == 0)
        this->reboot_at_ = 1;
      ESP_LOGI(TAG, "OTA OK (%u bytes), reboot in 1.5 s", (unsigned) this->image_size_);
      r[1] = ST_OK;
      this->reply_(r, 2);
      break;
    }
    case CMD_GET_SETTINGS:
    case CMD_SET_SETTINGS: {
      r[0] = c[0] | 0x80;
      if (this->state_ != ST_AUTHED) {
        r[1] = this->state_ == ST_IDLE ? ST_NOT_AUTH : ST_STATE;
        this->reply_(r, 2);
        break;
      }
      r[1] = ST_OK;
      if (c[0] == CMD_SET_SETTINGS) {
        if (len < 11) {
          r[1] = ST_STATE;
          this->reply_(r, 2);
          break;
        }
        this->settings_apply_(c + 2, c[1]);
      }
      size_t n = this->settings_pack_(r + 2);
      this->reply_(r, 2 + n);
      break;
    }
    case CMD_ABORT: {
      this->abort_ota_("client abort");
      r[0] = 0x86;
      r[1] = ST_OK;
      this->reply_(r, 2);
      break;
    }
    default: {
      r[0] = c[0] | 0x80;
      r[1] = ST_STATE;
      this->reply_(r, 2);
      break;
    }
  }
}

#ifdef BLE_OTA_SETTINGS
static uint16_t num_u16(number::Number *n, float scale) {
  if (n == nullptr || !n->has_state() || std::isnan(n->state))
    return 0xFFFF;
  long v = lroundf(n->state * scale);
  if (v < 0)
    v = 0;
  if (v > 0xFFFE)
    v = 0xFFFE;
  return (uint16_t) v;
}

static void num_set(number::Number *n, uint16_t raw, float scale, const char *what) {
  if (n == nullptr || raw == 0xFFFF)
    return;
  const float v = raw / scale;
  ESP_LOGI(TAG, "setting %s = %.3f (was %.3f)", what, v, n->state);
  auto call = n->make_call();
  call.set_value(v);
  call.perform();  // template number: проверка min/max, restore_value -> флеш, on_value
}
#endif

// detect_ms keep_s climate_min led vfactor*1000  — 9 байт
size_t BleOta::settings_pack_(uint8_t *out) {
#ifdef BLE_OTA_SETTINGS
  uint16_t d = num_u16(this->detect_delay_, 1.0f);
  uint16_t k = num_u16(this->keep_time_, 1.0f);
  uint16_t ci = num_u16(this->climate_interval_, 1.0f);
  uint16_t vf = num_u16(this->voltage_factor_, SCALE_VFACTOR);
  uint8_t led = this->led_ == nullptr ? 255 : (this->led_->state ? 1 : 0);
#else
  uint16_t d = 0xFFFF, k = 0xFFFF, ci = 0xFFFF, vf = 0xFFFF;
  uint8_t led = 255;
#endif
  out[0] = d & 0xFF;
  out[1] = d >> 8;
  out[2] = k & 0xFF;
  out[3] = k >> 8;
  out[4] = ci & 0xFF;
  out[5] = ci >> 8;
  out[6] = led;
  out[7] = vf & 0xFF;
  out[8] = vf >> 8;
  return 9;
}

void BleOta::settings_apply_(const uint8_t *p, uint8_t mask) {
#ifdef BLE_OTA_SETTINGS
  const uint16_t d = p[0] | (p[1] << 8);
  const uint16_t k = p[2] | (p[3] << 8);
  const uint16_t ci = p[4] | (p[5] << 8);
  const uint8_t led = p[6];
  const uint16_t vf = p[7] | (p[8] << 8);
  if (mask & 0x01)
    num_set(this->detect_delay_, d, 1.0f, "detect delay, ms");
  if (mask & 0x02)
    num_set(this->keep_time_, k, 1.0f, "keep time, s");
  if (mask & 0x04)
    num_set(this->climate_interval_, ci, 1.0f, "climate interval, min");
  if ((mask & 0x08) && this->led_ != nullptr && led <= 1) {
    ESP_LOGI(TAG, "setting motion LED = %s", led ? "ON" : "OFF");
    if (led)
      this->led_->turn_on();
    else
      this->led_->turn_off();
  }
  if (mask & 0x10)
    num_set(this->voltage_factor_, vf, SCALE_VFACTOR, "voltage factor");
#else
  ESP_LOGW(TAG, "settings not compiled in (no number/switch)");
#endif
}

void BleOta::process_block_() {
  const uint32_t n = this->block_len_;
  uint8_t r[6];
  r[0] = RSP_ACK;

  if (this->state_ != ST_RECEIVING) {
    this->block_len_ = 0;
    this->block_ready_.store(false);
    return;
  }

  // первый блок: это образ приложения ESP32 и того же проекта?
  if (this->written_ == 0) {
    bool ok = n >= 32 + 80 && this->block_[0] == 0xE9;
    if (ok) {
      const uint8_t *desc = this->block_ + 32;  // esp_image_header_t(24) + segment_header(8)
      ok = get_u32(desc) == 0xABCD5432u;        // ESP_APP_DESC_MAGIC_WORD
      if (ok) {
        const esp_app_desc_t *cur = esp_app_get_description();
        char name[33] = {0};
        char ver[33] = {0};
        memcpy(name, desc + 48, 32);  // project_name
        memcpy(ver, desc + 16, 32);   // version
        ok = strncmp(name, cur->project_name, sizeof(cur->project_name)) == 0;
        ESP_LOGI(TAG, "image: project '%s' (running '%s'), version '%s'", name, cur->project_name, ver);
      }
    }
    if (!ok) {
      ESP_LOGE(TAG, "not an app image of this project - rejected");
      this->abort_ota_("bad image");
      r[1] = ST_BAD_IMAGE;
      put_u32(r + 2, 0);
      this->reply_(r, 6);
      return;
    }
  }

  esp_err_t err = esp_ota_write(this->handle_, this->block_, n);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "esp_ota_write: %s", esp_err_to_name(err));
    this->abort_ota_("write");
    r[1] = ST_WRITE_ERR;
    put_u32(r + 2, this->written_);
    this->reply_(r, 6);
    return;
  }
  this->crc_ = crc32_update(this->crc_, this->block_, n);
  this->written_ += n;
  this->last_activity_ms_ = millis();
  this->block_len_ = 0;
  this->block_ready_.store(false);  // только теперь host task примет следующий блок

  if (this->written_ >= this->next_log_ || this->written_ == this->image_size_) {
    ESP_LOGI(TAG, "OTA %u / %u (%u%%)", (unsigned) this->written_, (unsigned) this->image_size_,
             (unsigned) (this->written_ * 100ULL / this->image_size_));
    this->next_log_ += 64 * 1024;
  }
  r[1] = ST_OK;
  put_u32(r + 2, this->written_);
  this->reply_(r, 6);
}

}  // namespace bthome
}  // namespace esphome

#endif  // USE_BTHOME_BLE_OTA
