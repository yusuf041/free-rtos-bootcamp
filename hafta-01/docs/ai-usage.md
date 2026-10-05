# Yapay zekâ kullanımı

> **Taslak.** Bu dosyayı kendi cümlelerinle gözden geçir; özellikle "nasıl kontrol ettim" ve
> "neyi değiştirdim" kısımları senin deneyimin olmalı.

Bu ödevde Claude (Anthropic) ile birlikte çalıştım.

## Hangi işlerde destek aldım?

- Ödevin parçalara ayrılması ve iş sırasının planlanması.
- Kart ile PC arasındaki mesaj sözleşmesinin (`docs/protocol.md`) taslağı.
- CubeMX ayarlarının (saat, USART2, EXTI0, TIM2, NVIC, FreeRTOS) adım adım açıklanması.
- Firmware uygulama katmanı (`app.c` / `app.h`): görevler, kuyruklar, ISR, ölçüm kayıtları, CPU işi, baud değişimi.
- PC arayüzü (`uart_monitor.py`, `uart_protocol.py`) ve grafikler.
- Ölçüm sonuçlarının yorumlanması ve proje raporu.

## Üretilen kodu nasıl kontrol ettim?

- Her adımı kartta çalıştırıp arayüzde ya da debugger'da (Live Expressions) sayaçlara bakarak doğruladım.
- Ölçüm zincirini teorik değerle karşılaştırdım: ölçülen t₄−t₃ ≈ 5,55 ms, hesaplanan 64 × 10 / 115200 = 5,556 ms.
- TEL zaman damgalarının tam periyotla (100 ms) arttığını kontrol ettim.
- Buton filtresini her değişiklikte 10 basışla test edip kabul / titreşim / bırakma sayaçlarını karşılaştırdım.
- S5 @ 230400 ölçümüyle kararlılık koşulunun tahminini doğruladım.

## Hangi önerileri değiştirdim ya da düzeltildi?

- İlk buton filtresi (yalnız 30 ms) bu kartta yetmedi: 10 basışta 25 olay ölçtüm. Basılı tutma ve
  oynatma denemeleriyle bırakma gürültüsünü gösterdim; filtre iki kez değiştirildi.
- Dahili pull-down önerisini denedim; tek denemede iyileşme görmedim, no pull ile devam ettim.
- S5'te D komutundan sonra kayıtların gelmediğini ve arayüzün donduğunu fark ettim; nedenleri
  (dolu kuyrukta kaybolan komut, durmuş okuma döngüsü) bulunup düzeltildi.
- Arayüzdeki grafiklerin okunaklı olmadığını belirttim; zaman çizelgesi ve olay kayıtları sekmesi
  bu geri bildirimle yeniden tasarlandı. Baud seçimi ve aralık açıklamaları benim isteğimle eklendi.
- _(kendi eklemelerin)_
