/*
 * app.c — Ödev 01 uygulama katmanı
 * Adım 12: t0..t4 ölçüm kayıtları + PC komutları + S4/S5 kalibreli CPU işi
 *           + çalışma anında baud rate değişimi + UART alımı için canlı tutma
 *
 * Veri akışı:
 *   Buton ISR --(ButtonEvent)--> buttonQ --> ButtonTask --+
 *                                                         |  (TxMsg kopyası)
 *   TelemetryTask ----------------------------------------+--> txQ --> UartTxTask --IT--> USART2 --> PC
 *                                                                          ^                 |
 *                                                                          +-- TC kesmesi ---+
 *   UART RX ISR (PC komutu) --> g_pending_cmd --(txQ boşalınca)--> UartTxTask çalıştırır
 *
 * Ölçüm noktaları (TIM2, 1 µs):
 *   t0  Buton ISR, kenar kabul edilince
 *   t1  ButtonTask, olayı kuyruktan aldıktan hemen sonra
 *   t2  ButtonTask, xQueueSend çağrısından hemen önce
 *   t3  UartTxTask, HAL_UART_Transmit_IT çağrısından hemen önce
 *   t4  HAL_UART_TxCpltCallback (UART TC = son bit hattan çıktı)
 *
 * Kurallar:
 *   - UART'ın TEK sahibi UartTxTask'tır.
 *   - Kayıt kartı (Record) zincir boyunca elden ele geçer; her alanı tek bir
 *     yer yazar. Sahiplik devri kuyruk/bildirim çağrılarıyla olur.
 *   - Hiçbir hata sessizce yutulmaz: her hata yolunun bir sayacı vardır.
 */

#include "app.h"

#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "main.h"
#include "FreeRTOS.h"
#include "task.h"
#include "queue.h"

extern UART_HandleTypeDef huart2;
extern TIM_HandleTypeDef  htim2;   /* 32 bit, 1 MHz serbest sayaç = mikrosaniye saati */

/* ------------------------------------------------------------------------- */
/* Sabitler (ödev standardı)                                                 */
/* ------------------------------------------------------------------------- */
#define MSG_LEN          64U    /* Her mesaj tam 64 bayt: 63 karakter + '\n'          */
#define TXQ_LEN          16U    /* TX kuyruğu: 16 mesaj, FIFO                         */
#define BTNQ_LEN         8U     /* Buton kuyruğu: 8 olay                              */
#define REC_LEN          64U    /* Kayıt havuzu: en az 64 olay                        */
#define TX_TIMEOUT_MS    1000U  /* Deney gözetim süresi: 1 s                          */
#define DEBOUNCE_US      30000U /* Sessizlik penceresi: önceki 30 ms'de kenar yoksa  */
#define CMD_POLL_MS      20U     /* UartTxTask'ın bekleyen komuta bakma aralığı        */
#define CAL_ITERS        100000U /* Kalibrasyonda ölçülen iterasyon sayısı */
#define LOCKOUT_US       250000U /* Kabul edilen basıştan sonra 250 ms yeni basış yok.
                                    Prosedür basışlar arası >= 500 ms ister; ölçümde
                                    bırakma anında 116 ve 120 ms'de sahte basış görüldü. */

/* ------------------------------------------------------------------------- */
/* Tipler                                                                    */
/* ------------------------------------------------------------------------- */
typedef enum {
    MSG_TEL = 0,   /* telemetri                                   */
    MSG_BTN,       /* buton yanıtı (ÖLÇÜLEN mesaj)                */
    MSG_INF,       /* senaryo bilgisi                             */
    MSG_REC,       /* ölçüm kaydı (deney sonu)                    */
    MSG_SUM,       /* sayaç özeti                                 */
    MSG_END,       /* aktarım bitti                               */
    MSG_WRK,       /* ek CPU işinin gerçek süresi                 */
    MSG_BAU        /* baud değişimi bildirimi                     */
} MsgType;

/* Kuyruğa DEĞER olarak kopyalanan mesaj. İçinde işaretçi yok. */
typedef struct {
    char     data[MSG_LEN];
    uint8_t  type;
    uint16_t event_id;        /* BTN: olay kimliği · diğer: 0 */
} TxMsg;

/* ISR'den ButtonTask'a giden olay (değer olarak kopyalanır) */
typedef struct {
    uint16_t id;
    uint32_t t0;
} ButtonEvent;

typedef struct {
    const char *name;
    uint32_t    period_ms;    /* 0 = telemetri kapalı                    */
    uint32_t    work_us;      /* her periyottaki ek CPU işi (sonraki adım) */
} Scenario;

/* Olay durumu */
enum {
    ST_PENDING = 0,  /* zincir henüz tamamlanmadı                     */
    ST_OK,           /* t4'e kadar ulaştı                             */
    ST_BTN_DROP,     /* buton kuyruğu doluydu (ISR)                   */
    ST_TX_DROP,      /* TX kuyruğu doluydu (ButtonTask)               */
    ST_TX_ERR,       /* HAL_UART_Transmit_IT başlatılamadı            */
    ST_TIMEOUT       /* TC 1 s içinde gelmedi                         */
};

/* Bir buton olayının kayıt kartı */
typedef struct {
    uint32_t t[5];            /* t0..t4 (µs)                                     */
    uint8_t  has;             /* hangi zamanlar yazıldı: bit k = tk var           */
    uint8_t  status;          /* ST_xxx                                          */
} Record;

/* ------------------------------------------------------------------------- */
/* Senaryo tablosu (protocol.md ile aynı)                                    */
/* ------------------------------------------------------------------------- */
static const Scenario k_scenarios[6] = {
    { "S0",   0U,    0U },
    { "S1", 100U,    0U },
    { "S2",  20U,    0U },
    { "S3",  10U,    0U },
    { "S4",  10U, 2000U },
    { "S5",  10U, 5000U },
};

/* ------------------------------------------------------------------------- */
/* RTOS nesneleri                                                            */
/* ------------------------------------------------------------------------- */
static QueueHandle_t txQ;
static QueueHandle_t buttonQ;
static TaskHandle_t  txTaskHandle;
static TaskHandle_t  telTaskHandle;

/* ------------------------------------------------------------------------- */
/* Paylaşılan durum                                                          */
/* ------------------------------------------------------------------------- */
static volatile uint8_t  g_scenario    = 1U;
static volatile uint32_t g_baud        = 115200U; /* açılışta CubeMX ayarı; ödev standardı */

/* PC komutu 'a'..'f' -> baud. 115200 ödev standardıdır; diğerleri ek deney içindir. */
static const uint32_t k_bauds[6] = { 9600U, 57600U, 115200U, 230400U, 460800U, 921600U };
static volatile bool     g_tel_enabled = false; /* telemetri üretilsin mi           */
static volatile bool     g_armed       = false; /* basışlar olay üretsin mi         */
static volatile uint32_t g_tel_gen     = 0U;    /* her senaryo başında artar         */
static volatile uint16_t btn_next_id   = 0U;    /* son verilen olay kimliği          */
static volatile uint32_t tel_seq       = 0U;
static volatile UBaseType_t txq_hwm    = 0U;    /* TX kuyruğu en yüksek doluluk      */

/* Ek CPU işi (S4/S5) */
static volatile uint32_t g_cal_us     = 1U;     /* CAL_ITERS iterasyonun en kısa süresi (µs) */
static volatile uint32_t g_work_iters = 0U;     /* bu senaryoda her periyottaki iterasyon    */
static volatile uint32_t g_work_sink;           /* sonuç buraya yazılır: derleyici işi silemez */
static volatile uint32_t wrk_n, wrk_sum, wrk_min, wrk_max;  /* gerçek iş süresi istatistiği */

static Record  recs[REC_LEN];                   /* kayıt havuzu                      */
static uint8_t rx_byte;                         /* PC'den gelen komut baytı          */

/* UartTxTask'ın o an gönderdiği mesaj.
   static: IT gönderimi arka planda bu tamponu okur; TC'ye kadar yerinde kalmalı. */
static TxMsg tx_cur;
static volatile uint16_t tx_cur_btn_id = 0U;    /* gönderilen BTN ise olay id'si, değilse 0 */
static volatile char     g_pending_cmd = 0;     /* PC'den gelen, henüz işlenmemiş komut */

/* ------------------------------------------------------------------------- */
/* Sayaçlar                                                                  */
/* ------------------------------------------------------------------------- */
static volatile uint32_t cnt_tel_sent;
static volatile uint32_t cnt_tel_drop;
static volatile uint32_t cnt_fmt_err;
static volatile uint32_t cnt_tx_err;
static volatile uint32_t cnt_tx_timeout;
static volatile uint32_t cnt_btn_accepted;
static volatile uint32_t cnt_btn_bounce;
static volatile uint32_t cnt_btn_release;
static volatile uint32_t cnt_btn_ignored;   /* deney dışında yapılan basış      */
static volatile uint32_t cnt_btn_lockout;   /* 250 ms kilit içinde gelen basış  */
static volatile uint32_t cnt_btn_q_drop;
static volatile uint32_t cnt_btn_tx_drop;
static volatile uint32_t cnt_rec_ovf;       /* 64'ten fazla olay: kaydedilemedi */
static volatile uint32_t cnt_cmd_drop;      /* işlenmeden üzerine yazılan komut */
static volatile uint32_t cnt_uart_err;      /* UART alım hatası                 */
static volatile uint32_t cnt_rx_rearm;      /* kapanmış alımın görevden yeniden kurulması */

/* ------------------------------------------------------------------------- */
/* Yardımcılar                                                               */
/* ------------------------------------------------------------------------- */

/* Mikrosaniye saati. Farklar her zaman uint32_t çıkarmayla alınır (taşmaya dayanıklı). */
static inline uint32_t timer_us(void)
{
    return TIM2->CNT;
}

/* Olay id'sine göre kayıt kartı. id 1..64 dışındaysa NULL. */
static Record *rec_get(uint16_t id)
{
    return (id >= 1U && id <= REC_LEN) ? &recs[id - 1U] : NULL;
}

static void rec_set(Record *r, unsigned k, uint32_t now)
{
    r->t[k] = now;
    r->has |= (uint8_t)(1U << k);
}

static const char *status_str(uint8_t st)
{
    switch (st) {
    case ST_OK:       return "ok";
    case ST_BTN_DROP: return "btn_drop";
    case ST_TX_DROP:  return "tx_drop";
    case ST_TX_ERR:   return "tx_err";
    default:          return "timeout";   /* ST_TIMEOUT ve tamamlanmamış (PENDING) */
    }
}

/* TX kuyruğunun gördüğü en yüksek doluluğu not et (istatistik). */
static void note_txq_level(void)
{
    UBaseType_t n = uxQueueMessagesWaiting(txQ);
    if (n > txq_hwm) {
        txq_hwm = n;
    }
}

/* 64 baytlık mesaj: metin + boşluk dolgusu + '\n'. 63'ü aşarsa KESMEZ, hata sayar. */
static bool make_msg(TxMsg *m, MsgType type, uint16_t id, const char *fmt, ...)
{
    char tmp[MSG_LEN];

    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(tmp, sizeof tmp, fmt, ap);
    va_end(ap);

    if (n < 0 || n > (int)(MSG_LEN - 1U)) {
        cnt_fmt_err++;
        return false;
    }

    memset(m->data, ' ', MSG_LEN - 1U);
    memcpy(m->data, tmp, (size_t)n);
    m->data[MSG_LEN - 1U] = '\n';
    m->type     = (uint8_t)type;
    m->event_id = id;
    return true;
}

/* ------------------------------------------------------------------------- */
/* Ek CPU işi: sabit iterasyonlu xorshift. Sonuç volatile değişkene yazıldığı */
/* için optimizasyonla silinmez. Kesmeleri KAPATMAZ, bekleme YAPMAZ.          */
/* ------------------------------------------------------------------------- */
static uint32_t __attribute__((noinline)) cpu_work(uint32_t iters)
{
    uint32_t x = 2463534242U;
    for (uint32_t i = 0U; i < iters; i++) {
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
    }
    return x;
}

/* Açılışta bir kez: CAL_ITERS iterasyonu 5 kez ölç, EN KISA süreyi al.
   (Araya kesme girerse süre uzar; en kısa ölçüm en temiz olanıdır.) */
static void calibrate_work(void)
{
    uint32_t best = UINT32_MAX;
    for (unsigned k = 0U; k < 5U; k++) {
        const uint32_t a = timer_us();
        g_work_sink ^= cpu_work(CAL_ITERS);
        const uint32_t d = timer_us() - a;
        if (d < best) {
            best = d;
        }
    }
    g_cal_us = (best == 0U) ? 1U : best;
}

/* İstenen süre (µs) için gereken iterasyon sayısı */
static uint32_t iters_for_us(uint32_t us)
{
    return (uint32_t)(((uint64_t)CAL_ITERS * us) / g_cal_us);
}

/* ------------------------------------------------------------------------- */
/* Başlatma                                                                  */
/* ------------------------------------------------------------------------- */
void App_Init(void)
{
    txQ = xQueueCreate(TXQ_LEN, sizeof(TxMsg));
    configASSERT(txQ != NULL);

    buttonQ = xQueueCreate(BTNQ_LEN, sizeof(ButtonEvent));
    configASSERT(buttonQ != NULL);

    HAL_TIM_Base_Start(&htim2);   /* CubeMX ayarlar ama BAŞLATMAZ */
}

/* ------------------------------------------------------------------------- */
/* TelemetryTask — yüksek öncelik                                            */
/* ------------------------------------------------------------------------- */
void App_TelemetryTask(void)
{
    telTaskHandle = xTaskGetCurrentTaskHandle();

    /* En yüksek öncelikli task olduğumuz için açılışta ilk biz çalışırız:
       kalibrasyon, deney başlamadan biter. */
    calibrate_work();

    TickType_t last   = xTaskGetTickCount();
    uint32_t   my_gen = g_tel_gen;

    for (;;) {
        const Scenario *s = &k_scenarios[g_scenario];

        /* Telemetri kapalıysa (S0 ya da deney bitti) BLOKLAN, boş döngüde dönme.
           UartTxTask yeni senaryoyu başlatınca bildirimle uyandırır. */
        if (!g_tel_enabled || s->period_ms == 0U) {
            (void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
            last   = xTaskGetTickCount();
            my_gen = g_tel_gen;
            continue;
        }

        /* Senaryo değiştiyse periyot tabanını sıfırla
           (yoksa vTaskDelayUntil kaçırılan periyotları art arda telafi eder). */
        if (my_gen != g_tel_gen) {
            my_gen = g_tel_gen;
            last   = xTaskGetTickCount();
        }

        /* S4/S5: ek CPU işi. Gerçek süresini (duvar saati) de ölçüyoruz;
           araya giren kesmeler bu süreye dahildir. */
        if (g_work_iters != 0U) {
            const uint32_t a = timer_us();
            g_work_sink ^= cpu_work(g_work_iters);
            const uint32_t d = timer_us() - a;
            wrk_n++;
            wrk_sum += d;
            if (d < wrk_min) { wrk_min = d; }
            if (d > wrk_max) { wrk_max = d; }
        }

        TxMsg m;
        uint32_t now_ms = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
        if (make_msg(&m, MSG_TEL, 0U, "TEL,%lu,%s,%lu",
                     (unsigned long)(++tel_seq), s->name, (unsigned long)now_ms)) {
            if (xQueueSend(txQ, &m, 0) == pdPASS) {
                cnt_tel_sent++;
                note_txq_level();
            } else {
                cnt_tel_drop++;
            }
        }

        vTaskDelayUntil(&last, pdMS_TO_TICKS(s->period_ms));
    }
}

/* ------------------------------------------------------------------------- */
/* ButtonTask — orta öncelik                                                 */
/* ------------------------------------------------------------------------- */
void App_ButtonTask(void)
{
    ButtonEvent e;

    for (;;) {
        xQueueReceive(buttonQ, &e, portMAX_DELAY);
        const uint32_t t1 = timer_us();            /* t1: olayı aldıktan HEMEN sonra */

        Record *r = rec_get(e.id);
        if (r != NULL) {
            rec_set(r, 1U, t1);
        }

        const Scenario *s = &k_scenarios[g_scenario];
        TxMsg m;
        if (!make_msg(&m, MSG_BTN, e.id, "BTN,%u,%s,PRESSED",
                      (unsigned)e.id, s->name)) {
            continue;
        }

        if (r != NULL) {
            rec_set(r, 2U, timer_us());            /* t2: xQueueSend'den HEMEN önce */
        }

        if (xQueueSend(txQ, &m, 0) != pdPASS) {
            cnt_btn_tx_drop++;
            if (r != NULL) {
                r->status = ST_TX_DROP;
            }
        } else {
            note_txq_level();
        }
    }
}

/* ------------------------------------------------------------------------- */
/* UartTxTask yardımcıları                                                   */
/* ------------------------------------------------------------------------- */

/* tx_cur'u IT ile gönder, TC gelene kadar (en fazla 1 s) bekle.
   BTN mesajıysa t3'ü burada, t4'ü TC kesmesinde kaydeder. */
static void uart_send_current(void)
{
    (void)ulTaskNotifyTake(pdTRUE, 0);             /* eski bildirim kaldıysa temizle */

    const uint16_t id = (tx_cur.type == MSG_BTN) ? tx_cur.event_id : 0U;
    Record *r = rec_get(id);
    tx_cur_btn_id = id;

    if (r != NULL) {
        rec_set(r, 3U, timer_us());                /* t3: başlatmadan HEMEN önce */
    }

    if (HAL_UART_Transmit_IT(&huart2, (uint8_t *)tx_cur.data, MSG_LEN) != HAL_OK) {
        cnt_tx_err++;
        tx_cur_btn_id = 0U;
        if (r != NULL) {
            r->status = ST_TX_ERR;
        }
        return;
    }

    if (ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(TX_TIMEOUT_MS)) == 0U) {
        HAL_UART_AbortTransmit(&huart2);
        cnt_tx_timeout++;
        tx_cur_btn_id = 0U;
        if (r != NULL) {
            r->status = ST_TIMEOUT;
        }
    }
}

/* Yeni senaryo başlat: sıfırla, INF gönder, telemetriyi ve basışları aç. */
static void start_scenario(uint8_t scn)
{
    /* Kritik bölge: sıfırlama sırasında buton ISR'ı sayaçlara dokunmasın */
    taskENTER_CRITICAL();
    g_scenario  = scn;
    btn_next_id = 0U;
    tel_seq     = 0U;
    txq_hwm     = 0U;
    memset(recs, 0, sizeof recs);
    cnt_tel_sent = cnt_tel_drop = cnt_fmt_err = cnt_tx_err = cnt_tx_timeout = 0U;
    cnt_btn_accepted = cnt_btn_bounce = cnt_btn_release = cnt_btn_ignored = 0U;
    cnt_btn_lockout = 0U;
    cnt_btn_q_drop = cnt_btn_tx_drop = cnt_rec_ovf = cnt_cmd_drop = cnt_uart_err = 0U;
    cnt_rx_rearm = 0U;
    g_work_iters = iters_for_us(k_scenarios[scn].work_us);   /* deney boyunca SABİT */
    wrk_n = 0U; wrk_sum = 0U; wrk_min = UINT32_MAX; wrk_max = 0U;
    taskEXIT_CRITICAL();

    const Scenario *s = &k_scenarios[scn];
    if (make_msg(&tx_cur, MSG_INF, 0U, "INF,%s,%lu,%lu,%lu,%lu,%lu", s->name,
                 (unsigned long)s->period_ms, (unsigned long)s->work_us,
                 (unsigned long)g_work_iters, (unsigned long)g_cal_us,
                 (unsigned long)g_baud)) {
        uart_send_current();
    }

    g_armed = true;
    g_tel_gen++;
    g_tel_enabled = true;
    xTaskNotifyGive(telTaskHandle);                /* telemetri uyuyorsa uyandır */
}

/* Deney sonu: kayıtları REC satırları olarak, ardından SUM ve END gönder.
   Bu noktada telemetri durdurulmuş ve TX kuyruğundaki önceki her şey gönderilmiştir. */
static void dump_records(void)
{
    const char *sn = k_scenarios[g_scenario].name;
    uint16_t n = btn_next_id;
    if (n > REC_LEN) {
        n = REC_LEN;
    }

    for (uint16_t id = 1U; id <= n; id++) {
        const Record *r = &recs[id - 1U];

        /* d1..d4 = tk - t0. Ölçülemeyen zaman BOŞ bırakılır, asla 0 yazılmaz. */
        char d[4][11];
        for (unsigned k = 1U; k <= 4U; k++) {
            if (r->has & (1U << k)) {
                snprintf(d[k - 1U], sizeof d[0], "%lu",
                         (unsigned long)(uint32_t)(r->t[k] - r->t[0]));
            } else {
                d[k - 1U][0] = '\0';
            }
        }

        if (make_msg(&tx_cur, MSG_REC, 0U, "REC,%s,%u,%lu,%s,%s,%s,%s,%s",
                     sn, (unsigned)id, (unsigned long)r->t[0],
                     d[0], d[1], d[2], d[3], status_str(r->status))) {
            uart_send_current();
        }
    }

    /* Ek CPU işinin GERÇEK süresi: iterasyon, ölçüm sayısı, min / ortalama / max (µs) */
    {
        const uint32_t n_w = wrk_n;
        if (make_msg(&tx_cur, MSG_WRK, 0U, "WRK,%s,%lu,%lu,%lu,%lu,%lu", sn,
                     (unsigned long)g_work_iters, (unsigned long)n_w,
                     (unsigned long)(n_w ? wrk_min : 0U),
                     (unsigned long)(n_w ? wrk_sum / n_w : 0U),
                     (unsigned long)wrk_max)) {
            uart_send_current();
        }
    }

    if (make_msg(&tx_cur, MSG_SUM, 0U,
                 "SUM,%s,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu", sn,
                 (unsigned long)cnt_btn_accepted, (unsigned long)cnt_btn_bounce,
                 (unsigned long)cnt_btn_q_drop,   (unsigned long)cnt_btn_tx_drop,
                 (unsigned long)cnt_tx_err,       (unsigned long)cnt_tx_timeout,
                 (unsigned long)cnt_rec_ovf,      (unsigned long)cnt_tel_sent,
                 (unsigned long)cnt_tel_drop,     (unsigned long)txq_hwm,
                 (unsigned long)cnt_fmt_err,      (unsigned long)cnt_btn_lockout,
                 (unsigned long)cnt_rx_rearm)) {
        uart_send_current();
    }

    if (make_msg(&tx_cur, MSG_END, 0U, "END,%u", (unsigned)n)) {
        uart_send_current();
    }
}

/* Baud değişimi (yalnız UartTxTask'tan çağrılır; UART'ın sahibi o).
   1) ESKİ hızla "BAU,<yeni>,SWITCH" gönder ve bitmesini bekle -> PC portunu değiştirir
   2) UART'ı yeni hızla yeniden kur, alımı yeniden başlat
   3) PC'ye zaman tanı, YENİ hızla "BAU,<yeni>,OK" gönder
   4) Aynı senaryoyu yeni hızla yeniden başlat (önceki deneyin kayıtları SİLİNİR). */
static void change_baud(uint32_t baud)
{
    if (make_msg(&tx_cur, MSG_BAU, 0U, "BAU,%lu,SWITCH", (unsigned long)baud)) {
        uart_send_current();                       /* TC'ye kadar bekler: hat boş */
    }

    HAL_UART_AbortReceive(&huart2);
    huart2.Init.BaudRate = baud;
    if (HAL_UART_Init(&huart2) != HAL_OK) {
        cnt_uart_err++;
        huart2.Init.BaudRate = g_baud;             /* başarısızsa eski hıza dön */
        (void)HAL_UART_Init(&huart2);
    } else {
        g_baud = baud;
    }
    HAL_UART_Receive_IT(&huart2, &rx_byte, 1U);

    vTaskDelay(pdMS_TO_TICKS(300));                /* PC portunu değiştirsin */
    if (make_msg(&tx_cur, MSG_BAU, 0U, "BAU,%lu,OK", (unsigned long)g_baud)) {
        uart_send_current();
    }

    /* Aynı senaryoyu yeni hızla baştan başlat (sayaçlar sıfırlanır, INF gönderilir) */
    start_scenario(g_scenario);
}

/* UART alımını canlı tut.
   STM32 HAL tuzağı: HAL_UART_Transmit_IT çalışırken huart kilitlidir (__HAL_LOCK). Tam o
   anda bir bayt gelirse, RX kesmesindeki HAL_UART_Receive_IT kilidi dolu bulur ve HAL_BUSY
   döner; alım bir daha kurulmaz ve kart komutları duymaz olur. Bu fonksiyon UartTxTask'tan
   sık sık çağrılır; alım kapalıysa yeniden kurar ve sayar. */
static void rx_keepalive(void)
{
    if (huart2.RxState == HAL_UART_STATE_READY) {
        if (HAL_UART_Receive_IT(&huart2, &rx_byte, 1U) == HAL_OK) {
            cnt_rx_rearm++;
        }
    }
}

static void handle_command(char c)
{
    if (c == 'D') {
        dump_records();
    } else if (c >= '0' && c <= '5') {
        start_scenario((uint8_t)(c - '0'));
    } else if (c >= 'a' && c <= 'f') {
        change_baud(k_bauds[c - 'a']);
    }
}

/* ------------------------------------------------------------------------- */
/* UartTxTask — düşük öncelik, UART'ın TEK sahibi                            */
/* ------------------------------------------------------------------------- */
void App_UartTxTask(void)
{
    txTaskHandle = xTaskGetCurrentTaskHandle();

    /* PC komutlarını dinlemeye başla (1 bayt, kesmeyle) */
    HAL_UART_Receive_IT(&huart2, &rx_byte, 1U);

    /* Açılışta varsayılan senaryo: S1 */
    start_scenario(1U);

    for (;;) {
        /* Kuyrukta mesaj varsa gönder. Bekleme süreli: komut kontrolü için ara ara uyan. */
        rx_keepalive();                            /* komut alımı kapanmışsa aç */

        if (xQueueReceive(txQ, &tx_cur, pdMS_TO_TICKS(CMD_POLL_MS)) == pdPASS) {
            uart_send_current();
            continue;
        }

        /* Kuyruk BOŞ: bekleyen bir PC komutu varsa şimdi çalıştır.
           Komut gelince telemetri ve basış kaydı durdurulduğu için kuyruk mutlaka boşalır;
           yani "önce TX'i bitir, sonra komutu işle" kuralı kendiliğinden sağlanır. */
        const char c = g_pending_cmd;
        if (c != 0) {
            g_pending_cmd = 0;
            handle_command(c);
        }
    }
}

/* ------------------------------------------------------------------------- */
/* HAL callback — UART TC (son bit hattan çıktı)                             */
/* ------------------------------------------------------------------------- */
void HAL_UART_TxCpltCallback(UART_HandleTypeDef *huart)
{
    const uint32_t now = timer_us();               /* t4 adayı: İLK iş */

    if (huart->Instance != USART2) {
        return;
    }

    const uint16_t id = tx_cur_btn_id;
    if (id != 0U) {
        Record *r = rec_get(id);
        if (r != NULL) {
            rec_set(r, 4U, now);                   /* t4 */
            r->status = ST_OK;
        }
        tx_cur_btn_id = 0U;
    }

    BaseType_t woken = pdFALSE;
    vTaskNotifyGiveFromISR(txTaskHandle, &woken);
    portYIELD_FROM_ISR(woken);
}

/* ------------------------------------------------------------------------- */
/* HAL callback — UART RX: PC'den 1 bayt komut geldi                         */
/*   Telemetriyi ve basış kaydını HEMEN durdurur, komutu g_pending_cmd'ye    */
/*   yazar. UartTxTask, TX kuyruğu boşalınca komutu çalıştırır.              */
/* ------------------------------------------------------------------------- */
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance != USART2) {
        return;
    }

    const char c = (char)rx_byte;
    HAL_UART_Receive_IT(&huart2, &rx_byte, 1U);    /* bir sonraki bayt için yeniden kur */

    if (c != 'D' && (c < '0' || c > '5') && (c < 'a' || c > 'f')) {
        return;                                    /* tanımsız komut: yok say */
    }

    g_tel_enabled = false;                         /* yeni TEL üretimini durdur */
    g_armed       = false;                         /* yeni basış kaydını durdur */

    /* Komutu KUYRUĞA KOYMUYORUZ: S5'te kuyruk 16/16 dolu olabilir ve komut kaybolurdu
       (eski sürümdeki "donma" hatası). Ayrı bir değişkene yazıyoruz; UartTxTask kuyruk
       boşalınca çalıştırır. Önceki bekleyen komut varsa yenisi onun yerini alır. */
    if (g_pending_cmd != 0) {
        cnt_cmd_drop++;                            /* üzerine yazılan (işlenmemiş) komut */
    }
    g_pending_cmd = c;
}

/* UART alım hatası (ör. overrun): say ve alımı yeniden kur */
void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance != USART2) {
        return;
    }
    cnt_uart_err++;
    HAL_UART_Receive_IT(&huart2, &rx_byte, 1U);
}

/* ------------------------------------------------------------------------- */
/* HAL callback — EXTI (PA0, yükselen + düşen kenar)                         */
/*   "Sessizlik" filtresi:                                                   */
/*     BASIŞ kabulü = pin 1 okunuyor VE önceki 30 ms içinde HİÇ kenar yok.   */
/*   Neden? Ölçümde bırakma gürültüsünün 30 ms'den uzun sürdüğü görüldü      */
/*   (S3, olay 2 -> 3 arası 116 ms). Gürültü kenarlarının önünde hep başka   */
/*   kenarlar olduğu için hiçbiri "sessizlikten sonra" gelmez. Gerçek        */
/*   basışın ilk kenarı ise uzun bir sessizlikten sonra gelir ve HEMEN       */
/*   kabul edilir: t0 gecikmez.                                              */
/*   Her kenar 30 ms'lik pencereyi yeniden başlatır.                         */
/* ------------------------------------------------------------------------- */
void HAL_GPIO_EXTI_Callback(uint16_t GPIO_Pin)
{
    const uint32_t now = timer_us();               /* t0 adayı: İLK iş */

    if (GPIO_Pin != GPIO_PIN_0) {
        return;
    }

    static bool     is_pressed = false;            /* sadece istatistik için */
    static bool     has_edge   = false;
    static uint32_t last_edge_us;

    const bool level = (HAL_GPIO_ReadPin(GPIOA, GPIO_PIN_0) == GPIO_PIN_SET);
    const bool quiet = !has_edge ||
                       (uint32_t)(now - last_edge_us) >= DEBOUNCE_US;

    /* Kabul edilsin edilmesin, HER kenar sessizlik penceresini yeniden başlatır */
    has_edge     = true;
    last_edge_us = now;

    if (!(level && quiet)) {
        if (!level && is_pressed) {
            is_pressed = false;                    /* bırakma (olay üretmez) */
            cnt_btn_release++;
        } else {
            cnt_btn_bounce++;                      /* titreşim / gürültü */
        }
        return;
    }

    /* Sessizlikten sonra gelen basış kenarı */
    is_pressed = true;

    /* Basışlar arası kilit: önceki kabul edilen basıştan 250 ms geçmediyse olay üretme */
    static bool     has_press = false;
    static uint32_t last_press_us;
    if (has_press && (uint32_t)(now - last_press_us) < LOCKOUT_US) {
        cnt_btn_lockout++;
        return;
    }
    has_press     = true;
    last_press_us = now;

    /* Kabul edilen BASIŞ */
    if (!g_armed) {
        cnt_btn_ignored++;                         /* deney açık değil: olay üretme */
        return;
    }
    cnt_btn_accepted++;

    const uint16_t id = ++btn_next_id;
    Record *r = rec_get(id);
    if (r != NULL) {
        r->has    = 0U;
        r->status = ST_PENDING;
        rec_set(r, 0U, now);                       /* t0 */
    } else {
        cnt_rec_ovf++;                             /* 64 kayıt doldu */
    }

    ButtonEvent e = { .id = id, .t0 = now };

    BaseType_t woken = pdFALSE;
    if (xQueueSendFromISR(buttonQ, &e, &woken) != pdPASS) {
        cnt_btn_q_drop++;
        if (r != NULL) {
            r->status = ST_BTN_DROP;
        }
    }
    portYIELD_FROM_ISR(woken);
}
