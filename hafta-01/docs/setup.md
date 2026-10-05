# Kurulum

## Araçlar

| Araç | Sürüm |
|---|---|
| STM32CubeIDE | _(Help → About'tan yaz)_ |
| STM32CubeF4 firmware paketi | _(CubeMX → Project Manager'dan yaz)_ |
| FreeRTOS | CubeIDE ile gelen, CMSIS-RTOS v2 arayüzü |
| Python | 3.14 |
| pyserial / matplotlib | _(`pip show pyserial matplotlib`)_ |
| İşletim sistemi | Windows |

## Kablolama

| CP2102 | Discovery | Not |
|---|---|---|
| TXD | PA3 (USART2 RX) | Çapraz bağlanır |
| RXD | PA2 (USART2 TX) | |
| GND | GND | Ortak referans şart |
| 3V3 / 5V | — | Bağlanmaz; kart USB'den beslenir |

Kart, ST-LINK mini-USB kablosuyla PC'ye ayrıca bağlıdır (programlama ve güç).

## CubeMX ayarları

| Bölüm | Ayar |
|---|---|
| RCC | HSE: Crystal/Ceramic Resonator, HCLK 168 MHz (APB1 timer clock 84 MHz) |
| SYS | Debug: Serial Wire · Timebase Source: **TIM6** (SysTick FreeRTOS'a ait) |
| USART2 | Asynchronous, 115200, 8 bit, None, 1 stop · NVIC: USART2 global interrupt ✔ |
| PA0 | GPIO_EXTI0, Rising/Falling edge, No pull · NVIC: EXTI line0 ✔ |
| TIM2 | Internal clock, Prescaler 83, Counter Period 4294967295, kesme yok |
| NVIC | Priority Group: 4 bit preemption · EXTI0 = 5, USART2 = 5 · SysTick/PendSV = 15 |
| NVIC → Code generation | EXTI0 ve USART2 için: Generate Enable in Init, Generate IRQ handler, Call HAL handler ✔ |
| FREERTOS | CMSIS_V2 · defaultTask silindi · 3 görev (dinamik) |
| FREERTOS görevleri | TelemetryTask AboveNormal 256 · ButtonTask Normal 256 · UartTxTask BelowNormal **512** (word) |
| FREERTOS config | LIBRARY_LOWEST_INTERRUPT_PRIORITY 15 · LIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY 5 · CHECK_FOR_STACK_OVERFLOW Option2 |
| FREERTOS advanced | USE_NEWLIB_REENTRANT Enabled |
| Kullanılmayanlar | USB_OTG_FS / USB_HOST kapalı |

Tick: 1 kHz (1 ms). Derleme: Debug (`-O0`).

## main.c bağlantıları

```c
/* USER CODE BEGIN Includes */
#include "app.h"
/* USER CODE END Includes */

/* USER CODE BEGIN RTOS_QUEUES */
App_Init();
/* USER CODE END RTOS_QUEUES */

/* Görev gövdeleri yalnız uygulama fonksiyonunu çağırır */
App_TelemetryTask();   /* StartTelemetryTask içinde */
App_ButtonTask();      /* StartButtonTask içinde    */
App_UartTxTask();      /* StartUartTxTask içinde    */
```
