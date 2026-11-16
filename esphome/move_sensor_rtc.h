#pragma once
// Состояние датчика, которое переживает deep sleep (RTC-память).
// Обнуляется только при холодном старте (подача питания / reset / прошивка).
#include <esp_attr.h>
#include <esp_sleep.h>
#include <esp_mac.h>      // esp_read_mac() — Device ID / SSID точки доступа
#include <esp_system.h>   // esp_reset_reason() — причина сброса (brownout и т.п.)
#include <esp_app_desc.h> // esp_app_get_description() — SHA образа в Build Version
#include <driver/gpio.h>
#include <sys/time.h>
#include <cstdint>
#include <cmath>

namespace ms {

static RTC_DATA_ATTR bool occupied = false;        // сейчас "занято" (ON уже отправлен)
static RTC_DATA_ATTR int64_t off_deadline_ms = 0;  // когда отправлять OFF
static RTC_DATA_ATTR uint32_t wake_count = 0;      // счётчик пробуждений
// Сброс Wi-Fi переключателем: сколько раз подряд включали сервисный режим и когда первый раз.
// Переживает сон (переключатель выключили -> датчик уснул -> включили -> проснулся).
static RTC_DATA_ATTR uint8_t svc_on_count = 0;
static RTC_DATA_ATTR int64_t svc_first_ms = 0;

// Системные часы ESP-IDF идут и во время deep sleep (RTC-таймер),
// поэтому дедлайн остаётся верным между пробуждениями.
// ВАЖНО: не добавлять в конфиг time: (sntp/homeassistant) — часы прыгнут.
static inline int64_t now_ms() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  return (int64_t) tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

// Сколько мс осталось до отправки OFF (<= 0 — пора).
static inline int64_t remaining_ms() { return off_deadline_ms - now_ms(); }

// ---- Сон, пока PIR держит HIGH (2026-11-14).
// Раньше после ON плата не спала, пока SR602 не отпустит выход (~2.5 с минимум, при движении
// дольше — ретриггер), ~25-30 мА всё это время. Теперь: если на D1 HIGH — спим с пробуждением
// по LOW (отпускание), Keep time начинаем считать с отпускания, как и раньше.
// Уровень пробуждения всегда противоположен уровню на пине в момент сна (один замер), поэтому
// любое изменение между замером и сном даёт мгновенное пробуждение — состояние не теряется.
static RTC_DATA_ATTR bool pir_held = false;            // уснули, ожидая отпускания PIR (wake on LOW)
static constexpr int64_t KEEP_PENDING = INT64_MAX;      // Keep time ещё не идёт: PIR держит HIGH
static constexpr uint32_t HELD_SAFETY_MS = 10UL * 60 * 1000;  // страховочный таймер, если PIR залип в HIGH
static inline bool keep_pending() { return off_deadline_ms == KEEP_PENDING; }

// ---- Повтор «свободно» (2026-11-15). BLE-реклама без подтверждения: если прокси пропустил
// короткое окно с OFF, HA держал «занято» до следующего климат-пакета (до Climate interval).
// Теперь через OFF_REPEAT_MS после OFF датчик просыпается ещё раз и шлёт состояние повторно.
// Новое движение или климат-пакет со «свободно» повтор отменяют. 0 = повтор не нужен.
static RTC_DATA_ATTR int64_t off_repeat_ms = 0;
static constexpr int64_t OFF_REPEAT_MS = 60000;
static inline void keep_start(int keep_s) { off_deadline_ms = now_ms() + (int64_t) keep_s * 1000; }

// ---- Свежие срабатывания PIR (2026-11-16, v1.14.2). Пока «занято», датчик раньше ничего не
// слал на повторное движение — в HA нельзя было отличить «человек шевелится» от «PIR залип».
// Теперь: свежее срабатывание = PIR снова поднялся после того, как был в LOW (новый фронт).
// Удержание HIGH (залип / страховочный таймер) свежим НЕ считается. На каждое свежее —
// motion_count++ (едет в своём блоке рекламы); пока «занято», пакет на свежее срабатывание
// уходит не чаще MOTION_REFRESH_MS. Интеграция: «Last motion» и PIR stuck по свежести.
static RTC_DATA_ATTR uint8_t motion_count = 0;      // счётчик свежих срабатываний (по кругу)
static RTC_DATA_ATTR bool pir_low_seen = false;     // PIR был в LOW после последнего засчитанного фронта
static RTC_DATA_ATTR int64_t motion_tx_ms = 0;      // когда последний раз ушёл пакет с «занято»
static constexpr int64_t MOTION_REFRESH_MS = 120000;
// засчитать срабатывание; true — оно свежее (был LOW с прошлого раза)
// ---- Номер правки настроек (v1.15, 2026-11-16). Растёт на каждую РЕАЛЬНУЮ правку настройки с любой
// стороны (сайт датчика, ESPHome API, BLE от HA): одна запись = +1 (несколько полей в одной BLE-записи —
// тоже +1). После 255 идёт 1; 0 — только у свежего NVS (после стирания флеша). Хранится в NVS
// (своя preference, sync сразу), едет в своём блоке рекламы, в ESPHome-сущности и 18-м байтом ответа
// BLE-настроек. Интеграция по нему отличает чужую правку (принять) от сброса (вернуть свои значения).
static uint8_t settings_rev = 0;
static bool rev_pending = false;   // была правка — зафиксировать (+1) в rev_commit
static bool boot_done = false;     // настройки восстановлены из флеша; дальше изменения — правки
static constexpr uint32_t SETTINGS_REV_KEY = 0x5E77A3B1u;
static inline uint8_t rev_next(uint8_t r) { return r >= 255 ? 1 : (uint8_t) (r + 1); }
// число изменилось (сравнение с запасом на float)
static inline bool setting_changed(float a, float b) {
  if (std::isnan(a) || std::isnan(b)) return !(std::isnan(a) && std::isnan(b));
  return std::fabs(a - b) > 1e-4f * (1.0f + std::fabs(b));
}

static inline bool motion_fresh_take() {
  if (!pir_low_seen) return false;
  pir_low_seen = false;
  motion_count++;
  return true;
}

// ---- Быстрый тест (2026-11-14, BLE-команда 0x13): без движения просыпаться раз в 30 с.
// До какого момента (часы RTC, переживают сон); после — само выключается.
static RTC_DATA_ATTR int64_t test_until_ms = 0;
static constexpr uint32_t TEST_WAKE_MS = 30000;
static inline bool test_mode() { return test_until_ms > now_ms(); }
// окно рекламы на время быстрого теста (0 = как в прошивке, ${tx_window}) — замер, сколько окна хватает прокси
static RTC_DATA_ATTR uint32_t test_tx_ms = 0;
static inline uint32_t tx_window_ms(uint32_t def_ms) { return (test_mode() && test_tx_ms) ? test_tx_ms : def_ms; }


// ---- Защита батареи (1.4.0, 2026-11-16). Без неё на разряженном LiPo датчик просыпался как обычно,
// пока не срабатывала защита банки (~2.5-3.0 В) — глубокий разряд сокращает жизнь аккумулятора.
//  bat_low  (<= 10 %, снимается от 15 %): климат в BAT_LOW_CLIMATE_MULT раз реже, LED срабатываний не мигает.
//  bat_crit (покой <= BAT_CRIT_V, снимается от BAT_RESUME_V — подзарядили): PIR не будит, датчик спит
//           BAT_CRIT_CHECK_MS и на проверке шлёт пакет (HA видит батарею); будит и сервисный переключатель.
static RTC_DATA_ATTR bool bat_low = false;
static RTC_DATA_ATTR bool bat_crit = false;
static constexpr float BAT_CRIT_V = 3.50f;     // ~2 % по lipo_pct
static constexpr float BAT_RESUME_V = 3.65f;   // ~7 %: подзарядили (скачок напряжения при зарядке > 0.1 В)
static constexpr uint32_t BAT_LOW_CLIMATE_MULT = 4;
static constexpr uint32_t BAT_CRIT_CHECK_MS = 6UL * 3600 * 1000;

// ---- Таймаут сервисного режима (1.4.0). Забытый включённым переключатель держал Wi-Fi (~80-100 мА),
// пока не сядет батарея. Через service_timeout_min (base.yaml) мин после входа Wi-Fi выключается, датчик
// работает как обычно и до выключения переключателя в сервисный режим не входит (будит его LOW на D2).
static RTC_DATA_ATTR bool svc_timed_out = false;
static uint32_t svc_since = 0;   // millis() входа в сервисный режим


// ---- Отчёт о климате не реже Climate report interval (2026-11-14).
// Раньше климат уходил только на переходах занято/свободно и по таймеру в режиме «свободно».
// Пока «занято» и PIR повторно срабатывал (motion again), пакетов не было вовсе — температура
// и влажность стояли 7-14 мин. Теперь время последней отправки хранится в RTC, на каждом
// пробуждении проверяем срок, а сон в режиме «занято» ограничен сроком следующего отчёта.
static RTC_DATA_ATTR int64_t climate_last_ms = 0;   // 0 = после холодного старта ещё не слали
// интервал отчёта в мс (минуты из настройки; быстрый тест — 30 с; 0 = отчёт выключен)
static inline uint32_t climate_interval_ms(float minutes) {
  if (test_mode()) return TEST_WAKE_MS;
  if (std::isnan(minutes) || minutes < 1.0f) return 0;
  // 1.4.0: батарея разряжена — отчёт реже (не дольше 4 ч)
  float m = bat_low ? minutes * BAT_LOW_CLIMATE_MULT : minutes;
  if (m > 240.0f) m = 240.0f;
  return (uint32_t) (m * 60000.0f);
}
// пора слать (допуск 2 с: пробуждение по таймеру + загрузка не должны пропустить срок)
static inline bool climate_due(uint32_t ci) {
  if (ci == 0) return false;
  if (climate_last_ms == 0) return true;
  return now_ms() - climate_last_ms >= (int64_t) ci - 2000;
}
// сколько мс до следующего отчёта (INT64_MAX — отчёт выключен)
static inline int64_t climate_left_ms(uint32_t ci) {
  if (ci == 0) return INT64_MAX;
  if (climate_last_ms == 0) return 0;
  int64_t left = climate_last_ms + (int64_t) ci - now_ms();
  return left < 0 ? 0 : left;
}
// длительность сна: не дольше, чем до отчёта о климате (минимум 1 с)
static inline uint32_t cap_sleep_ms(int64_t want_ms, uint32_t ci) {
  int64_t c = climate_left_ms(ci);
  int64_t d = want_ms < c ? want_ms : c;
  if (d < 1000) d = 1000;
  return (uint32_t) d;
}


// ---- Батарея (2026-11-14). Процент считаем по напряжению ПОКОЯ, а не под нагрузкой.
// Замер 1: на старте каждого пробуждения, ДО запуска BLE/Wi-Fi (on_boot priority 550) — почти покой.
// Замер 2+: только в сервисном режиме, под Wi-Fi — к нему добавляем просадку bat_sag,
//           которую меряем сами: (замер без радио) − (первый замер под Wi-Fi).
static RTC_DATA_ATTR float bat_vf = 0;      // сглаженное напряжение покоя, 0 = нет истории
static RTC_DATA_ATTR int8_t bat_pct = -1;   // последний отправленный %, -1 = нет истории
static RTC_DATA_ATTR float bat_factor_used = 0;  // Voltage factor, с которым посчитан bat_vf (2026-11-15)

// ---- Напряжение в пакет BTHome (2026-11-16, v1.15.5): среднее за 10 замеров (замер — раз за
// пробуждение, до радио), новое значение — только при изменении >= 0.03 В. Шум АЦП/делителя
// ±0.02 В больше не порождает новые значения в HA. Процент батареи считается по сырому замеру.
static constexpr uint8_t VBAT_AVG_N = 10;
static constexpr float VBAT_STEP = 0.03f;
static RTC_DATA_ATTR float vbat_ring[VBAT_AVG_N] = {0};
static RTC_DATA_ATTR uint8_t vbat_ring_n = 0;
static RTC_DATA_ATTR uint8_t vbat_ring_i = 0;
static RTC_DATA_ATTR float vbat_sent = 0;   // последнее значение для пакета, 0 = нет
static inline float vbat_filter(float v) {
  if (std::isnan(v) || v <= 0.5f) return vbat_sent > 0 ? vbat_sent : v;
  // скачок > 0.15 В (зарядка, другой аккумулятор) — историю начать заново
  if (vbat_sent > 0 && fabsf(v - vbat_sent) > 0.15f) { vbat_ring_n = 0; vbat_ring_i = 0; }
  vbat_ring[vbat_ring_i] = v;
  vbat_ring_i = (uint8_t) ((vbat_ring_i + 1) % VBAT_AVG_N);
  if (vbat_ring_n < VBAT_AVG_N) vbat_ring_n++;
  float sum = 0;
  for (uint8_t i = 0; i < vbat_ring_n; i++) sum += vbat_ring[i];
  const float avg = sum / vbat_ring_n;
  if (vbat_sent <= 0 || fabsf(avg - vbat_sent) >= VBAT_STEP) vbat_sent = roundf(avg * 100.0f) / 100.0f;
  return vbat_sent;
}
static uint8_t bat_phase = 0;   // 0 — до замера на старте, 1 — замер без радио, 2 — радио работает
static float bat_v_boot = 0;    // замер без радио в этом пробуждении
static float bat_sag = -1;      // просадка под Wi-Fi, В; -1 = ещё не измерена

// LiPo 1S, напряжение покоя (малый ток) -> %. Верх: 4.14 В покоя = 100 %
// (после окончания заряда 4.20 В за час-два оседает до ~4.15-4.17 — это всё ещё полный).
static inline float lipo_pct(float v) {
  static const float T[][2] = {
    {3.30f, 0},  {3.50f, 2},  {3.61f, 5},  {3.69f, 10}, {3.71f, 15}, {3.73f, 20},
    {3.75f, 25}, {3.77f, 30}, {3.79f, 35}, {3.80f, 40}, {3.82f, 45}, {3.84f, 50},
    {3.85f, 55}, {3.87f, 60}, {3.91f, 65}, {3.95f, 70}, {3.98f, 75}, {4.02f, 80},
    {4.06f, 87}, {4.10f, 94}, {4.14f, 100}};
  const int n = sizeof(T) / sizeof(T[0]);
  if (v <= T[0][0]) return 0;
  if (v >= T[n - 1][0]) return 100;
  for (int i = 1; i < n; i++)
    if (v < T[i][0])
      return T[i - 1][1] + (v - T[i - 1][0]) * (T[i][1] - T[i - 1][1]) / (T[i][0] - T[i - 1][0]);
  return 100;
}

}  // namespace ms
