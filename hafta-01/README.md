# Hafta 01 · Yük Altında Buton Yanıtı

STM32F407 Discovery üzerinde FreeRTOS (CMSIS-RTOS v2) ile üç görevli bir uygulama ve UART üzerinden
telemetri alan bir PC arayüzü. Butona her basışta kart, yanıtın yolculuğunu beş noktada (t₀…t₄)
1 µs çözünürlükle ölçer. Telemetri hızı ve CPU yükü değiştirilerek altı senaryoda (S0–S5)
gecikmenin **hangi aşamada** değiştiği incelenir.

## İçerik

| Klasör | İçerik |
|---|---|
| `firmware/` | STM32CubeIDE projesi (`.ioc`, `Core/`, `Drivers/`, `Middlewares/`, `FreeRTOSConfig.h`). Uygulama kodu: `Core/Src/app.c`, `Core/Inc/app.h` |
| `interface/` | PC arayüzü: `uart_monitor.py` (pencere), `uart_protocol.py` (ayrıştırma, CSV, özet), `list_ports.py` |
| `measurements/` | Ham ölçümler: `S0.csv … S5.csv`, `summary.csv`, `raw/*.log` |
| `analysis/` | `report.md`, grafik üreten betik ve `plots/` |
| `docs/` | `protocol.md` (64 baytlık mesaj sözleşmesi), `setup.md`, `code-notes.md`, `ai-usage.md` |

## Donanım

- Kart: STM32F407G-DISC1, 168 MHz (HSE)
- UART: USART2, PA2 = TX, PA3 = RX, **115200 8N1**, CP2102 USB-TTL dönüştürücü
- Buton: PA0 (mavi buton), EXTI0, yükselen + düşen kenar, harici pull-down
- µs saati: TIM2, 32 bit, prescaler 83 → 1 MHz

Bağlantı ve araç sürümleri: [`docs/setup.md`](docs/setup.md)

## Görevler

| Görev | Öncelik (şartname / CMSIS v2) | Sorumluluk |
|---|---|---|
| TelemetryTask | 3 / `osPriorityAboveNormal` | Periyodik TEL mesajı; S4/S5'te kalibre edilmiş ek CPU işi |
| ButtonTask | 2 / `osPriorityNormal` | Buton olayını alır, `BTN,<id>,<senaryo>,PRESSED` yanıtını üretir |
| UartTxTask | 1 / `osPriorityBelowNormal` | UART'ın **tek sahibi**: TX kuyruğunu IT ile gönderir, PC komutlarını işler |

Kuyruklar: buton kuyruğu 8 olay, TX kuyruğu 16 mesaj (FIFO). Mesajlar değer olarak kopyalanır.
Gönderim `HAL_UART_Transmit_IT` ile yapılır; görev UART **TC** kesmesine kadar bloklanır (en fazla 1 s).

## Ölçüm noktaları

| Nokta | Nerede |
|---|---|
| t₀ | Buton ISR girişi, filtrenin kabul ettiği kenar |
| t₁ | ButtonTask, olayı kuyruktan aldıktan hemen sonra |
| t₂ | ButtonTask, `xQueueSend` çağrısından hemen önce |
| t₃ | UartTxTask, `HAL_UART_Transmit_IT` çağrısından hemen önce |
| t₄ | `HAL_UART_TxCpltCallback` (UART TC: son bit hattan çıktı) |

R = t₄ − t₀, deney deadline'ı 20 ms. Ayrıntılar: [`docs/code-notes.md`](docs/code-notes.md)

## Derleme ve yükleme

1. STM32CubeIDE'de **File → Import → Existing Projects into Workspace** ile `firmware/` içindeki projeyi aç.
2. **Debug** yapılandırmasıyla derle (tüm ölçümler bu derlemeyle yapıldı, `-O0`).
3. ST-LINK ile karta yükle.

## Arayüzü çalıştırma

```
pip install pyserial matplotlib
python interface/uart_monitor.py
```

Port olarak USB-TTL dönüştürücüyü (ör. COM3) seç, **115200** ile **Bağlan**.
Kart her açılışta 115200 ile başlar; baud yalnız bağlandıktan sonra arayüzden değiştirilir.

## Ölçüm prosedürü

Her senaryo için:

1. Arayüzde senaryo butonuna bas (S0…S5). Kart kayıtları ve sayaçları sıfırlar, `INF` gönderir.
2. 5 saniye ısınma (arayüzde sayaç var).
3. En az **30** basış, aralarında en az **0,5 s**, ritmi değiştirerek.
4. **Deneyi bitir (D)**: telemetri durur, TX kuyruğu boşalır, kart `REC`/`WRK`/`SUM`/`END` satırlarını gönderir.
5. Arayüz `measurements/<senaryo>.csv`, `summary.csv` ve `raw/<senaryo>_<tarih>.log` dosyalarını yazar.

| Senaryo | Telemetri | Ek CPU işi |
|---|---|---|
| S0 | Kapalı | Yok |
| S1 | 10 Hz | Yok |
| S2 | 50 Hz | Yok |
| S3 | 100 Hz | Yok |
| S4 | 100 Hz | ~2 ms |
| S5 | 100 Hz | ~5 ms |

115200 dışındaki baud ile yapılan ek ölçümler `S3_b230400.csv` gibi ayrı adla kaydedilir.

## Şartnameden sapmalar

- **Buton filtresi:** Şartnamedeki "ilk kenarı kabul et, 30 ms içindekileri at" kuralı bu kartta yetmedi
  (bırakma gürültüsü 30 ms'den uzun sürüyor; 10 basışta 25 olay ölçüldü). Kullanılan kural: basış kenarı,
  **önceki 30 ms'de hiç kenar yoksa** kabul edilir; ayrıca kabul edilen basıştan sonraki **250 ms** içinde
  yeni basış üretilmez. Atılan kenarlar `titresim`, kilide takılanlar `kilit` sayacıyla raporlanır.
  Gerçek basışın ilk kenarı gecikmeden kabul edildiği için t₀ etkilenmez.
- **PC komutları:** Şartnamede zorunlu değil; senaryo seçimi ve kayıt aktarımı için tek baytlık komutlar
  UART RX kesmesiyle alınır. Ek görev yoktur; komutu UartTxTask işler.

## Sonuçlar

Rapor ve grafikler: [`analysis/report.md`](analysis/report.md)

## Yapay zekâ kullanımı

[`docs/ai-usage.md`](docs/ai-usage.md)
