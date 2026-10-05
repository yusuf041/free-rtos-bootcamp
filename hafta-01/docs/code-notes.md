# Kod notları

Uygulama kodunun tamamı `firmware/<proje>/Core/Src/app.c` dosyasındadır. CubeMX'in ürettiği
`main.c` yalnız `App_Init()` ve üç görev fonksiyonunu çağırır.

## 1. Veri akışı

```
Buton ISR --(ButtonEvent)--> buttonQ --> ButtonTask --+
                                                      |  (TxMsg kopyası, 64 bayt)
TelemetryTask ----------------------------------------+--> txQ --> UartTxTask --IT--> USART2 --> PC
                                                                       ^                 |
                                                                       +-- TC kesmesi ---+
UART RX ISR (PC komutu) --> g_pending_cmd --(txQ boşalınca)--> UartTxTask çalıştırır
```

Kurallar:
- UART'ın tek sahibi UartTxTask'tır; diğer görevler UART'a dokunmaz, bu yüzden mutex gerekmez.
- Mesajlar ve olaylar kuyruğa **değer olarak** kopyalanır; içlerinde işaretçi yoktur.
- Her hata yolunun bir sayacı vardır ve deney sonunda `SUM` satırıyla raporlanır.

## 2. Buton ISR ve filtre

```c
void HAL_GPIO_EXTI_Callback(uint16_t GPIO_Pin)
{
    const uint32_t now = timer_us();               /* t0 adayı: ISR'ın İLK işi */
    ...
    const bool level = (HAL_GPIO_ReadPin(GPIOA, GPIO_PIN_0) == GPIO_PIN_SET);
    const bool quiet = !has_edge || (uint32_t)(now - last_edge_us) >= DEBOUNCE_US;
    has_edge = true;  last_edge_us = now;          /* her kenar 30 ms penceresini yeniden başlatır */

    if (!(level && quiet)) { ... cnt_btn_bounce++; return; }
    if (has_press && (uint32_t)(now - last_press_us) < LOCKOUT_US) { cnt_btn_lockout++; return; }
    ...
    rec_set(r, 0U, now);                           /* t0 */
    ButtonEvent e = { .id = id, .t0 = now };
    BaseType_t woken = pdFALSE;
    if (xQueueSendFromISR(buttonQ, &e, &woken) != pdPASS) {
        cnt_btn_q_drop++;  r->status = ST_BTN_DROP;
    }
    portYIELD_FROM_ISR(woken);                     /* ButtonTask uyandıysa hemen ona geç */
}
```

- ISR kısa tutulur: bekleme, UART, printf yok.
- Olay kimliği ve t₀ olayla birlikte kopyalanır; tek bir global zaman değişkeni kullanılmaz.
- Filtre ölçülerek tasarlandı: bu kartta bırakma gürültüsü 30 ms'den uzun sürüyor (README → Sapmalar).
- EXTI0 NVIC önceliği 5: `…FromISR` çağırdığı için `configMAX_SYSCALL_INTERRUPT_PRIORITY` (5) veya daha düşük öncelikte olmalı.

## 3. ButtonTask (t₁, t₂)

```c
for (;;) {
    xQueueReceive(buttonQ, &e, portMAX_DELAY);
    const uint32_t t1 = timer_us();                /* t1: olayı aldıktan HEMEN sonra */
    rec_set(r, 1U, t1);

    TxMsg m;
    make_msg(&m, MSG_BTN, e.id, "BTN,%u,%s,PRESSED", e.id, s->name);

    rec_set(r, 2U, timer_us());                    /* t2: xQueueSend'den HEMEN önce */
    if (xQueueSend(txQ, &m, 0) != pdPASS) {
        cnt_btn_tx_drop++;  r->status = ST_TX_DROP;
    }
}
```

## 4. TelemetryTask

```c
if (!g_tel_enabled || s->period_ms == 0U) {        /* S0 / deney bitti: BLOKLAN */
    (void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    ...
}
if (g_work_iters != 0U) {                          /* S4/S5: ek CPU işi */
    g_work_sink ^= cpu_work(g_work_iters);
}
/* TEL mesajını üret, txQ'ya bırak (timeout 0: doluysa düşür ve say) */
vTaskDelayUntil(&last, pdMS_TO_TICKS(s->period_ms));
```

- `vTaskDelayUntil` periyodu görevin çalışma süresinden bağımsız sabit tutar.
- Ek iş sabit iterasyonlu xorshift'tir; sonuç `volatile` değişkene yazılır, derleyici silemez.
  Kesmeleri kapatmaz, bekleme yapmaz.
- Kalibrasyon: açılışta 100 000 iterasyon 5 kez ölçülür, **en kısa** süre alınır; iterasyon sayısı
  deney boyunca sabittir. Gerçek iş süresi her periyotta ölçülür ve `WRK` satırıyla raporlanır.

## 5. UART gönderimi ve tamamlanma (t₃, t₄)

```c
static TxMsg tx_cur;            /* static: IT gönderimi TC'ye kadar bu tamponu okur */

static void uart_send_current(void)
{
    (void)ulTaskNotifyTake(pdTRUE, 0);             /* eski bildirim kaldıysa temizle */
    tx_cur_btn_id = (tx_cur.type == MSG_BTN) ? tx_cur.event_id : 0U;
    rec_set(r, 3U, timer_us());                    /* t3: başlatmadan HEMEN önce (yalnız BTN) */

    if (HAL_UART_Transmit_IT(&huart2, (uint8_t *)tx_cur.data, MSG_LEN) != HAL_OK) {
        cnt_tx_err++;  r->status = ST_TX_ERR;  return;
    }
    if (ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(TX_TIMEOUT_MS)) == 0U) {
        HAL_UART_AbortTransmit(&huart2);           /* 1 s gözetim: güvenle sonlandır */
        cnt_tx_timeout++;  r->status = ST_TIMEOUT;
    }
}

void HAL_UART_TxCpltCallback(UART_HandleTypeDef *huart)
{
    const uint32_t now = timer_us();               /* t4 */
    if (tx_cur_btn_id != 0U) { rec_set(r, 4U, now); r->status = ST_OK; }
    vTaskNotifyGiveFromISR(txTaskHandle, &woken);
    portYIELD_FROM_ISR(woken);
}
```

- HAL IT modunda `HAL_UART_TxCpltCallback`, **TC** (Transmission Complete) bayrağında çağrılır:
  shift register boşalmış, son stop biti hattan çıkmıştır. TXE ("DR boş") ile karıştırılmamalıdır.
- Doğrulama: ölçülen t₄−t₃ = 5,551–5,561 ms; teorik 64 × 10 bit / 115200 = 5,556 ms.

## 6. Kayıt havuzu ve zaman hesapları

- 64 elemanlı `Record recs[]`; olay kimliği 1…64 doğrudan indekstir. 64'ü aşan olay `kayit_tasma` sayacına yazılır.
- Her alanı tek bir yer yazar (t₀ ISR, t₁/t₂ ButtonTask, t₃ UartTxTask, t₄ TC kesmesi); sahiplik
  kuyruk ve bildirim çağrılarıyla devredilir, kilit gerekmez.
- `has` bit alanı hangi zamanın yazıldığını tutar. Ölçülemeyen zaman CSV'de **boş** bırakılır, asla 0 yazılmaz.
- Farklar `(uint32_t)(tk - t0)` ile alınır: TIM2 ~71,6 dakikada başa dönse de doğru sonuç verir.
- REC satırı mutlak t₀ ve dk = tk − t₀ farklarını taşır (beş mutlak zaman 63 karaktere sığmaz);
  PC mutlak zamanları `t0 + dk` ile geri hesaplar.

## 7. Komutlar ve deney sonu

- RX kesmesi komutu alınca telemetriyi ve basış kaydını hemen durdurur, komutu `g_pending_cmd`'ye yazar.
- UartTxTask, TX kuyruğu boşalınca komutu çalıştırır. Böylece "telemetriyi durdur → TX'i tamamla →
  kayıtları aktar" sırası kendiliğinden sağlanır.
- Komut TX kuyruğuna **konmaz**: S5'te kuyruk 16/16 doluyken komut kayboluyordu (bulunan hata).
- `rx_keepalive()`: gönderim sırasında gelen baytta HAL kilidi yüzünden alım yeniden kurulamazsa
  UartTxTask alımı yeniden başlatır ve `rx_yeniden` sayacını artırır.

## 8. Mesaj biçimi

Her mesaj 63 bayt ASCII + `\n` = 64 bayt; kısa metin boşlukla doldurulur, uzun metin gönderilmez
(`fmt_err`). Tam sözleşme: [`protocol.md`](protocol.md)

```c
memset(m->data, ' ', MSG_LEN - 1U);
memcpy(m->data, tmp, (size_t)n);
m->data[MSG_LEN - 1U] = '\n';
```

## 9. PC arayüzü

- Seri port ayrı bir thread'de okunur, `\n` ile çerçevelenir, 64 bayt doğrulanır ve bir kuyruğa konur.
  Pencere 50 ms'de bir kuyruğu boşaltır (kart tarafındaki ISR → kuyruk → görev kalıbının aynısı).
- Okuma döngüsü kendini `finally` içinde yeniden zamanlar; bir mesaj işlenirken hata çıksa bile
  arayüz okumayı bırakmaz (bulunan hata).
- PC saati hiçbir süre hesabında kullanılmaz; tüm zamanlar karttan gelir.
