#pragma once
// Своя точка доступа датчика Move Sensor BLE (2026-11-11) — копия hive_ap.h весов
// (esp32_c3_plus_honey_weight/hive_ap.h, тот же приём, что components/deye_link/ap_keeper.h у батареи):
//  • AP живёт, пока включён СЕРВИСНЫЙ режим (переключатель на D2), параллельно с роутером (APSTA).
//    В рабочем режиме Wi-Fi выключен целиком, tick() не вызывается (interval в YAML под service_mode).
//    Штатный wifi ESPHome гасит AP после коннекта к роутеру — поднимаем обратно.
//    SSID/пароль — из wifi: → ap: (secrets), не дублируем.
//  • Свой DNS (UDP 53): любой домен → IP точки. Это триггерит «Войти в сеть» на телефоне.
//    Штатный captive_portal не ставим: он показывал бы свою страницу вместо сайта датчика.
//  • Лендинг: GET на ЧУЖОЙ домен и проверочные адреса телефонов → страница со ссылкой на
//    http://<IP точки>/ (сайт датчика) и /#wifi (окно «Wi-Fi роутера»). IP и *.local — к сайту.
//  • Адрес 100.64.4.1 (не частный диапазон) — иначе Android не показывает окно входа (грабли весов).

#include <cstring>
#include <functional>
#include <string>
#include <esp_wifi.h>
#include <esp_netif.h>
#include <lwip/sockets.h>
#include "dhcpserver/dhcpserver.h"
#include "esphome/core/application.h"
#include "esphome/core/log.h"
#include "esphome/core/hal.h"
#include "esphome/components/web_server_base/web_server_base.h"
#include "esphome/components/wifi/wifi_component.h"

namespace ms_ap {

using esphome::web_server_idf::AsyncWebHandler;
using esphome::web_server_idf::AsyncWebServerRequest;

static const char *const TAG = "ms_ap";

// ── точка доступа: держать APSTA (копия приёма deye_wifi::ApKeeper) ─────────
class Keeper {
 public:
  std::function<void(std::string &ssid, std::string &pass)> credentials;

  void tick() {
    wifi_mode_t mode;
    if (esp_wifi_get_mode(&mode) != ESP_OK) return;  // Wi-Fi ещё не запущен
    if (mode == WIFI_MODE_APSTA || mode == WIFI_MODE_AP) return;
    if (!this->credentials) return;
    std::string ssid, pass;
    this->credentials(ssid, pass);
    if (ssid.empty()) return;
    ESP_LOGI(TAG, "AP was off (stock wifi) - forcing APSTA, SSID %s", ssid.c_str());
    esp_wifi_set_mode(WIFI_MODE_APSTA);
    wifi_config_t c;
    memset(&c, 0, sizeof(c));
    memcpy(c.ap.ssid, ssid.c_str(), ssid.size() > 32 ? 32 : ssid.size());
    c.ap.ssid_len = ssid.size() > 32 ? 32 : ssid.size();
    c.ap.channel = 1;  // при подключённом STA радио всё равно идёт на канал роутера
    c.ap.max_connection = 3;
    c.ap.beacon_interval = 100;
    if (pass.size() >= 8) {
      c.ap.authmode = WIFI_AUTH_WPA2_PSK;
      memcpy(c.ap.password, pass.c_str(), pass.size() > 63 ? 63 : pass.size());
    } else {
      c.ap.authmode = WIFI_AUTH_OPEN;
    }
    esp_wifi_set_config(WIFI_IF_AP, &c);
    esp_netif_t *ap = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
    if (ap != nullptr) esp_netif_dhcps_start(ap);  // «уже запущен» — не ошибка
  }
};

// ── DNS: любой A-запрос → IP точки доступа ───────────────────────────────
class Dns {
 public:
  void tick(uint32_t ip_be) {
    if (this->sock_ < 0) {
      this->sock_ = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
      if (this->sock_ < 0) return;
      sockaddr_in a{};
      a.sin_family = AF_INET;
      a.sin_port = htons(53);
      a.sin_addr.s_addr = htonl(INADDR_ANY);
      if (::bind(this->sock_, (sockaddr *) &a, sizeof(a)) != 0) {
        ESP_LOGW(TAG, "DNS bind :53 failed");
        ::close(this->sock_);
        this->sock_ = -1;
        return;
      }
      ::fcntl(this->sock_, F_SETFL, O_NONBLOCK);
      ESP_LOGI(TAG, "DNS on :53 (any name -> AP IP)");
    }
    uint8_t b[300];
    for (int k = 0; k < 4; k++) {  // до 4 запросов за тик
      sockaddr_in from{};
      socklen_t fl = sizeof(from);
      int n = ::recvfrom(this->sock_, b, sizeof(b) - 16, 0, (sockaddr *) &from, &fl);
      if (n < 12) return;
      if ((b[2] & 0x80) || b[4] != 0 || b[5] != 1) continue;  // не запрос или не 1 вопрос
      int p = 12;
      while (p < n && b[p] != 0) p += b[p] + 1;  // QNAME
      if (p + 5 > n) continue;
      const uint16_t qtype = (uint16_t) (b[p + 1] << 8 | b[p + 2]);
      {  // Диагностика (VERBOSE; включить — logger: logs: ms_ap: VERBOSE): имя, тип, кто спросил
        char name[80];
        int q = 12, o = 0;
        while (q < p && o < (int) sizeof(name) - 2) {
          int l = b[q++];
          for (int k = 0; k < l && q < p && o < (int) sizeof(name) - 2; k++) name[o++] = (char) b[q++];
          if (q < p) name[o++] = '.';
        }
        name[o] = '\0';
        ESP_LOGV(TAG, "DNS %s type %u from " IPSTR, name, (unsigned) qtype, IP2STR((esp_ip4_addr_t *) &from.sin_addr.s_addr));
      }
      int end = p + 5;  // 0 + QTYPE + QCLASS
      b[2] = 0x81;      // ответ, RD
      b[3] = 0x80;      // RA, NOERROR
      b[6] = 0;
      b[7] = qtype == 1 ? 1 : 0;  // ANCOUNT: только для A (AAAA и прочее — пусто)
      b[8] = b[9] = b[10] = b[11] = 0;
      if (qtype == 1) {
        const uint8_t ans[16] = {0xC0, 0x0C, 0, 1, 0, 1, 0, 0, 0, 30, 0, 4,
                                 (uint8_t) (ip_be), (uint8_t) (ip_be >> 8), (uint8_t) (ip_be >> 16), (uint8_t) (ip_be >> 24)};
        memcpy(b + end, ans, sizeof(ans));
        end += sizeof(ans);
      }
      ::sendto(this->sock_, b, end, 0, (sockaddr *) &from, fl);
    }
  }

 protected:
  int sock_{-1};
};

// ── лендинг на чужие домены ──────────────────────────────────────────────
static const char LANDING[] =
    "<!DOCTYPE html><html><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">"
    "<title>Move Sensor BLE</title><style>"
    ":root{color-scheme:light dark}body{margin:0;min-height:100vh;display:grid;place-items:center;"
    "font:16px/1.45 system-ui,-apple-system,Roboto,sans-serif;background:#f4f5f8;color:#1b2230}"
    "@media(prefers-color-scheme:dark){body{background:#0b0d12;color:#f2f4f8}.c{background:rgba(255,255,255,.05)!important}}"
    ".c{background:#fff;border-radius:24px;padding:28px;max-width:340px;margin:16px;text-align:center}"
    ".l{width:48px;height:48px;border-radius:14px;background:#3d7bff;display:inline-grid;place-items:center;margin-bottom:12px}"
    "h1{font-size:20px;margin:0 0 8px}p{margin:0 0 20px;opacity:.7;font-size:14px}"
    "a{display:block;background:#3d7bff;color:#fff;text-decoration:none;font-weight:700;padding:14px;border-radius:14px;-webkit-tap-highlight-color:transparent}"
    "a.s{background:transparent;color:inherit;border:1px solid rgba(127,127,127,.35);margin-top:10px;font-weight:600}"
    ".o{margin-top:18px;font-size:13px;opacity:.75;text-align:left}.o b{display:block;margin-bottom:4px}"
    ".o a{display:inline;background:none;color:#3d7bff;padding:0;font-weight:600;border-radius:0}.o div{margin:3px 0}"
    "</style></head><body><div class=c><div class=l><svg width=26 height=26 viewBox=\"0 0 24 24\" fill=#fff><circle cx=13.5 cy=3.6 r=2 />"
    "<path d=\"M9.8 8.9 7 23h2.1l1.8-8 2.1 2v6h2v-7.5l-2.1-2 .6-3A7.3 7.3 0 0 0 19 13v-2a5 5 0 0 1-4.3-2.4l-1-1.6a2 2 0 0 0-2.5-.8L6 8.3V13h2V9.6z\"/></svg></div>"
    "<h1>Move Sensor BLE</h1><p id=t>Connected to the sensor's own Wi-Fi.</p>"
    "<a id=b href=\"http://%IP%/\" target=_blank>Open sensor panel</a>"
    "<a id=w class=s href=\"http://%IP%/#wifi\" target=_blank>Router Wi-Fi</a>"
    // все адреса сайта (2026-11-14): точка датчика, IP в домашней сети (если подключён), имя .local
    "<div class=o><b id=oa>All addresses</b>"
    "<div><span id=o1>Sensor Wi-Fi</span>: <a href=\"http://%IP%/\" target=_blank>http://%IP%/</a></div>"
    "%STA_ROW%"
    "<div><span id=o3>Home network (mDNS)</span>: <a href=\"http://%HOST%.local/\" target=_blank>http://%HOST%.local/</a></div>"
    "</div></div>"
    "<script>var L=(navigator.language||'en').slice(0,2),T={uk:['Ви підключені до Wi-Fi датчика.','Відкрити панель датчика','Wi-Fi роутера','Усі адреси','Wi-Fi датчика','Домашня мережа','Домашня мережа (mDNS)'],"
    "ru:['Вы подключены к Wi-Fi датчика.','Открыть панель датчика','Wi-Fi роутера','Все адреса','Wi-Fi датчика','Домашняя сеть','Домашняя сеть (mDNS)']};"
    "if(T[L]){var x=T[L],g=function(i){return document.getElementById(i)};t.textContent=x[0];b.textContent=x[1];w.textContent=x[2];g('oa').textContent=x[3];g('o1').textContent=x[4];if(g('o2'))g('o2').textContent=x[5];g('o3').textContent=x[6];}</script>"
    "</body></html>";

inline bool is_foreign_host(const std::string &h) {
  if (h.empty()) return false;
  std::string host = h.substr(0, h.find(':'));
  bool ip = !host.empty();
  for (char c : host)
    if (!((c >= '0' && c <= '9') || c == '.')) { ip = false; break; }
  if (ip) return false;  // 100.64.4.1, 192.168.1.109 — к сайту
  if (host.size() > 6 && host.compare(host.size() - 6, 6, ".local") == 0) return false;
  return true;
}

class Landing : public AsyncWebHandler {
 public:
  bool canHandle(AsyncWebServerRequest *request) const override {
    if (request->method() != HTTP_GET) return false;
    auto host = request->get_header("Host");
    {  // Диагностика (VERBOSE): что запрашивают (кроме опроса сайта)
      char lb[AsyncWebServerRequest::URL_BUF_SIZE];
      auto lu = request->url_to(lb);
      if (strncmp(lu.c_str(), "/sensor/", 8) != 0 && strncmp(lu.c_str(), "/text_sensor/", 13) != 0)
        ESP_LOGV(TAG, "HTTP GET host=%s url=%s", host.has_value() ? host.value().c_str() : "-", lu.c_str());
    }
    if (host.has_value() && is_foreign_host(host.value())) return true;
    // Проверочные адреса телефонов/ПК — на любом хосте (как onNotFound у робота).
    char buf[AsyncWebServerRequest::URL_BUF_SIZE];
    auto url = request->url_to(buf);
    static const char *const PROBES[] = {"/generate_204", "/gen_204", "/hotspot-detect.html", "/connecttest.txt",
                                         "/ncsi.txt", "/success.txt", "/canonical.html", "/library/test/success.html"};
    for (const char *p : PROBES)
      if (strcmp(url.c_str(), p) == 0) return true;
    return strcmp(url.c_str(), "/captive-api") == 0 || strcmp(url.c_str(), "/welcome") == 0;
  }
  void handleRequest(AsyncWebServerRequest *request) override {
    char ubuf[AsyncWebServerRequest::URL_BUF_SIZE];
    auto u = request->url_to(ubuf);
    if (strcmp(u.c_str(), "/captive-api") == 0) {  // RFC 8908 Captive Portal API
      char ip[16] = "100.64.4.1";
      esp_netif_ip_info_t ii;
      esp_netif_t *apn = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
      if (apn != nullptr && esp_netif_get_ip_info(apn, &ii) == ESP_OK && ii.ip.addr != 0)
        snprintf(ip, sizeof(ip), IPSTR, IP2STR(&ii.ip));
      char json[96];
      snprintf(json, sizeof(json), "{\"captive\":true,\"user-portal-url\":\"http://%s/welcome\"}", ip);
      auto *r = request->beginResponse(200, "application/captive+json", std::string(json));
      r->addHeader("Cache-Control", "no-store");
      request->send(r);
      return;
    }
    std::string html(LANDING);
    char ip[16] = "100.64.4.1";
    esp_netif_ip_info_t info;
    esp_netif_t *ap = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
    if (ap != nullptr && esp_netif_get_ip_info(ap, &info) == ESP_OK && info.ip.addr != 0)
      snprintf(ip, sizeof(ip), IPSTR, IP2STR(&info.ip));
    for (size_t at; (at = html.find("%IP%")) != std::string::npos;) html.replace(at, 4, ip);
    // IP в домашней сети — строка только если датчик подключён к роутеру
    std::string sta_row;
    esp_netif_ip_info_t si;
    esp_netif_t *sta = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (sta != nullptr && esp_netif_get_ip_info(sta, &si) == ESP_OK && si.ip.addr != 0) {
      char sip[16];
      snprintf(sip, sizeof(sip), IPSTR, IP2STR(&si.ip));
      sta_row = std::string("<div><span id=o2>Home network</span>: <a href=\"http://") + sip +
                "/\" target=_blank>http://" + sip + "/</a></div>";
    }
    for (size_t at; (at = html.find("%STA_ROW%")) != std::string::npos;) html.replace(at, 9, sta_row);
    const std::string host = esphome::App.get_name();
    for (size_t at; (at = html.find("%HOST%")) != std::string::npos;) html.replace(at, 6, host);
    auto *r = request->beginResponse(200, "text/html", html);
    r->addHeader("Cache-Control", "no-store");
    request->send(r);
  }
};

// DHCP точки доступа (2026-11-01): штатно ESPHome кладёт опцию 114 «Captive Portal URI» (RFC 8910)
// только при captive_portal (wifi_component_esp_idf.cpp, wifi_ap_ip_config_) — мы его убрали, и Android
// показывал «без доступа в Интернет» вместо «Войти в сеть». Задаём сами: DNS-сервер = IP точки
// (наш DNS) и URI = http://<IP>/. Опции ставятся при остановленном DHCP-сервере.
// Адрес точки доступа (2026-11-01, эксперимент): НЕ из частных диапазонов. Android, получив по DNS для
// connectivitycheck.gstatic.com частный адрес (192.168.x/10.x/172.16-31.x), не доверяет ответу и HTTP-
// проверку не шлёт вовсе («без доступа в Интернет», без окна входа) — видно в логах DNS/HTTP 2026-11-01.
// 100.64.0.0/10 — общее адресное пространство операторов (RFC 6598), частным Android его не считает.
// Откат: вернуть 192,168,4,1 здесь и в wifi: ap: manual_ip.
static const uint8_t AP_IP_B[4] = {100, 64, 4, 1};
inline uint32_t ap_ip_addr() { return ESP_IP4TOADDR(AP_IP_B[0], AP_IP_B[1], AP_IP_B[2], AP_IP_B[3]); }

inline bool g_dhcp_done = false;
inline void configure_dhcp(esp_netif_t *ap, esp_netif_ip_info_t info) {
  // RFC 8910/8908: опция 114 указывает на Captive Portal API (JSON), а не на HTML — по HTML
  // Android 11+ портал не распознавал («без доступа в Интернет», проверено на телефоне 2026-11-01).
  static char uri[40];  // хранится указатель, не копия
  snprintf(uri, sizeof(uri), "http://" IPSTR "/captive-api", IP2STR(&info.ip));
  esp_netif_dhcps_stop(ap);
  if (info.ip.addr != ap_ip_addr()) {  // точка поднята с адресом по умолчанию — ставим свой
    info.ip.addr = ap_ip_addr();
    info.gw.addr = ap_ip_addr();
    info.netmask.addr = ESP_IP4TOADDR(255, 255, 255, 0);
    esp_netif_set_ip_info(ap, &info);
    snprintf(uri, sizeof(uri), "http://" IPSTR "/captive-api", IP2STR(&info.ip));
  }
  dhcps_lease_t lease;  // как у ESPHome: .100–.110 своей подсети
  lease.enable = true;
  lease.start_ip.addr = ESP_IP4TOADDR(AP_IP_B[0], AP_IP_B[1], AP_IP_B[2], 100);
  lease.end_ip.addr = ESP_IP4TOADDR(AP_IP_B[0], AP_IP_B[1], AP_IP_B[2], 110);
  esp_netif_dhcps_option(ap, ESP_NETIF_OP_SET, ESP_NETIF_REQUESTED_IP_ADDRESS, &lease, sizeof(lease));
  uint8_t offer_dns = 0x03;  // OFFER_ROUTER | OFFER_DNS (dhcpserver.h) — битовая маска, шлюз не терять
  esp_netif_dhcps_option(ap, ESP_NETIF_OP_SET, ESP_NETIF_DOMAIN_NAME_SERVER, &offer_dns, sizeof(offer_dns));
  esp_netif_dns_info_t dns{};
  dns.ip.type = ESP_IPADDR_TYPE_V4;
  dns.ip.u_addr.ip4.addr = info.ip.addr;
  esp_netif_set_dns_info(ap, ESP_NETIF_DNS_MAIN, &dns);
  esp_err_t e = esp_netif_dhcps_option(ap, ESP_NETIF_OP_SET, ESP_NETIF_CAPTIVEPORTAL_URI, uri, strlen(uri));
  esp_netif_dhcps_start(ap);
  ESP_LOGI(TAG, "DHCP: DNS + captive URI %s (%s)", uri, e == ESP_OK ? "ok" : "option 114 failed");
}

// Скан сетей БЕЗ разрыва связи с роутером (2026-11-11). Штатный start_scanning() переводит
// wifi ESPHome в STA_SCANNING → после скана переподключение: роутер отваливается, сайт теряет
// связь (так было на кнопке «Найти сети»). Здесь — фоновый скан ESP-IDF: между каналами радио
// возвращается на свой (coex_background_scan, как у роуминг-скана ESPHome), ассоциация остаётся.
// Результаты всё равно забирает обработчик WIFI_EVENT_SCAN_DONE ESPHome и раздаёт слушателям —
// wifi_info scan_results («Wi-Fi Scan Results») обновляется сам. Состояние wifi ESPHome не трогаем.
// Не подключены к роутеру — ESPHome и так сканирует в цикле подключения, тогда штатный скан.
inline void scan_networks() {
  auto *w = esphome::wifi::global_wifi_component;
  if (w == nullptr) return;
  if (!w->is_connected()) {
    ESP_LOGI(TAG, "scan: not connected to router - stock scan");
    w->start_scanning();
    return;
  }
  wifi_scan_config_t c{};
  c.show_hidden = false;
  c.scan_type = WIFI_SCAN_TYPE_ACTIVE;
  c.scan_time.active.min = 60;
  c.scan_time.active.max = 120;
#ifdef CONFIG_SOC_WIFI_SUPPORTED
  c.coex_background_scan = true;
#endif
  esp_err_t e = esp_wifi_scan_start(&c, false);
  ESP_LOGI(TAG, "scan: background scan while connected - %s", e == ESP_OK ? "started" : esp_err_to_name(e));
}

inline Keeper keeper;
inline Dns dns;
inline Landing *landing = nullptr;

// on_boot: откуда брать SSID/пароль (wifi: → ap:).
inline void setup(std::function<void(std::string &, std::string &)> creds) {
  keeper.credentials = std::move(creds);
}

// interval 250ms: держать AP, отвечать DNS, один раз зарегистрировать лендинг.
inline void tick() {
  static uint32_t last_keep = 0;
  if (millis() - last_keep >= 1000) {
    last_keep = millis();
    keeper.tick();
  }
  if (esphome::wifi::global_wifi_component == nullptr) return;
  wifi_mode_t mode;
  if (esp_wifi_get_mode(&mode) != ESP_OK || (mode != WIFI_MODE_APSTA && mode != WIFI_MODE_AP)) {
    g_dhcp_done = false;  // AP пропадала (wifi.configure и т.п.) — после подъёма настроить DHCP заново
    return;
  }
  if (landing == nullptr && esphome::web_server_base::global_web_server_base != nullptr) {
    landing = new Landing();
    esphome::web_server_base::global_web_server_base->add_handler_without_auth(landing);
    ESP_LOGI(TAG, "captive landing on foreign hosts");
  }
  esp_netif_ip_info_t info;
  esp_netif_t *ap = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
  if (ap != nullptr && esp_netif_get_ip_info(ap, &info) == ESP_OK && info.ip.addr != 0) {
    if (!g_dhcp_done || info.ip.addr != ap_ip_addr()) {
      g_dhcp_done = true;
      configure_dhcp(ap, info);
      esp_netif_get_ip_info(ap, &info);
    }
    dns.tick(info.ip.addr);
  }
}

}  // namespace ms_ap
