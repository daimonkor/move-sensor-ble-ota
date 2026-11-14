#pragma once
// Состояние датчика, которое переживает deep sleep (RTC-память).
// Обнуляется только при холодном старте (подача питания / reset / прошивка).
#include <esp_attr.h>
#include <esp_sleep.h>
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
static inline void keep_start(int keep_s) { off_deadline_ms = now_ms() + (int64_t) keep_s * 1000; }

// ---- Быстрый тест (2026-11-14, BLE-команда 0x13): без движения просыпаться раз в 30 с.
// До какого момента (часы RTC, переживают сон); после — само выключается.
static RTC_DATA_ATTR int64_t test_until_ms = 0;
static constexpr uint32_t TEST_WAKE_MS = 30000;
static inline bool test_mode() { return test_until_ms > now_ms(); }
// окно рекламы на время быстрого теста (0 = как в прошивке, ${tx_window}) — замер, сколько окна хватает прокси
static RTC_DATA_ATTR uint32_t test_tx_ms = 0;
static inline uint32_t tx_window_ms(uint32_t def_ms) { return (test_mode() && test_tx_ms) ? test_tx_ms : def_ms; }


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
  return (uint32_t) (minutes * 60000.0f);
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
static RTC_DATA_ATTR bool bat_low = false;  // флаг "батарея разряжена" (с гистерезисом)
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
