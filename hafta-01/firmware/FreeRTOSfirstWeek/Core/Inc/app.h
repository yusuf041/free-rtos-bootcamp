#ifndef APP_H
#define APP_H

/*
 * app.h — Ödev 01 uygulama katmanı
 *
 * CubeMX'in ürettiği task fonksiyonları (StartTelemetryTask vb.)
 * sadece buradaki fonksiyonları çağırır. Uygulama mantığının tamamı app.c'dedir.
 */

void App_Init(void);          /* Kuyrukları oluşturur. Task'lar çalışmadan ÖNCE çağrılmalı. */

void App_TelemetryTask(void); /* Öncelik: yüksek (osPriorityAboveNormal) — geri dönmez */
void App_ButtonTask(void);    /* Öncelik: orta   (osPriorityNormal)      — geri dönmez */
void App_UartTxTask(void);    /* Öncelik: düşük  (osPriorityBelowNormal) — geri dönmez */

#endif /* APP_H */
