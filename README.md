# 🦔 Kirpi BTC Analiz Dashboard

Gerçek zamanlı Bitcoin analiz ve sinyal platformu. Birden fazla veri kaynağından gelen verileri birleştirerek AL / SAT / BEKLE sinyali üretir ve Kirpi'nin yorumunu sunar.

## 🚀 Canlı Demo

> Railway üzerinde çalışmaktadır.

---

## 📊 Özellikler

### Canlı Fiyat Takibi
- Her **1.5 saniyede** otomatik fiyat güncellemesi — yenilemeye gerek yok
- Fiyat artışında **yeşil**, düşüşte **kırmızı** parıltı animasyonu
- 24 saatlik yüksek / düşük / hacim bilgisi

### 🦔 Kirpi Analiz — 8 Yuvarlak Gösterge
Her gösterge **-100 ile +100** skalasında hesaplanır:

| Gösterge | Kaynak | Açıklama |
|---|---|---|
| Korku & Açgözlülük | Alternative.me | Piyasa duygu indeksi (0-100) |
| RSI 1 Saat | CryptoCompare | Kısa vadeli momentum |
| RSI 4 Saat | CryptoCompare | Orta vadeli momentum |
| MACD (1h) | CryptoCompare | Trend yönü ve gücü |
| Bollinger Bandı | CryptoCompare | Fiyatın bant içi konumu |
| Funding Rate | OKX | Futures piyasası baskısı |
| BTC Dominans | CoinGecko | BTC'nin kripto piyasasındaki payı |
| EMA Trendi | CryptoCompare | 20/50/200 EMA pozisyonu |

### Sinyal Motoru
- 8 göstergenin ortalamasına göre **AL / SAT / BEKLE** kararı
- Skor **+15 üzeri → AL**, **-15 altı → SAT**, arası → BEKLE

### 🦔 Kirpi Yorumu
- "İşlem açmak mantıklı ✅ / mantıklı değil ❌ / dikkatli ol ⚠️"
- Her karar için gerekçe satırları (RSI, MACD, F&G, Funding açıklamasıyla)

### Mum Grafik
- 15 dakika / 1 saat / 4 saat / 1 gün zaman dilimleri
- Yeşil/kırmızı Japon mum çubukları

### Pozisyon Takibi
- **Long veya Short** pozisyon aç
- Gerçek zamanlı **P&L** takibi (% ve $ olarak)
- Pozisyon kapatınca sonuç bildirimi

### 🔔 Bildirimler
- Tarayıcı bildirimi izni istendiğinde, her **5 dakikada** sinyal + motivasyon mesajı gelir
- Pozisyon açıksa P&L de bildirime eklenir
- PWA olarak ana ekrana eklendiğinde arka planda çalışır

### 📰 Haberler
- CryptoCompare'den güncel BTC haberleri
- Her haber için duygu analizi: 🟢 Olumlu / 🔴 Olumsuz / ⚫ Nötr

---

## 🛠️ Teknik Detaylar

### Veri Kaynakları
| Kaynak | Ne için | Güncelleme |
|---|---|---|
| [CryptoCompare](https://min-api.cryptocompare.com) | Fiyat + OHLCV mum verileri | 1.5 sn / 5 dk |
| [Alternative.me](https://api.alternative.me/fng/) | Korku & Açgözlülük İndeksi | 1 saat |
| [CoinGecko](https://api.coingecko.com/api/v3/global) | BTC dominans, global piyasa | 10 dk |
| [OKX](https://www.okx.com/api/v5/public/funding-rate) | Funding rate | 5 dk |
| [CryptoCompare News](https://min-api.cryptocompare.com/data/v2/news/) | BTC haberleri | 10 dk |

> **Not:** Tüm veri kaynakları ücretsiz ve API key gerektirmez.

### Teknik Göstergeler (Python ile sıfırdan)
- **RSI** (Relative Strength Index) — 14 periyot, Wilder smoothing
- **MACD** — 12/26/9 EMA bazlı, histogram normalize
- **Bollinger Bands** — 20 periyot, 2 standart sapma
- **EMA** — 20 / 50 / 200 periyot
- **ATR** — MACD normalizasyonu için kullanılır

### Mimari
```
Flask (Python)
├── /api/price          → Canlı fiyat (cache: 10sn)
├── /api/dashboard      → Tüm göstergeler paralel fetch
├── /api/signal         → Hızlı sinyal (bildirimler için)
├── /api/klines/<iv>    → Mum verileri (1m/5m/15m/1h/4h/1d)
├── /api/news           → Haberler
├── /api/position/*     → Pozisyon aç/kapat/durum
└── /api/debug          → Veri kaynaklarını test et
```

### Frontend
- Vanilla JavaScript (no framework) — iOS Safari uyumlu
- XHR tabanlı (async/await yok)
- SVG ile çizilmiş yuvarlak göstergeler
- Canvas ile mum grafik
- PWA uyumlu (ana ekrana eklenebilir)

---

## ⚡ Kurulum

### Gereksinimler
```
Python 3.10+
flask==3.1.3
requests==2.34.2
gunicorn==23.0.0
```

### Lokal Çalıştırma
```bash
git clone https://github.com/Cagdascinar/Wnw-asistan.git
cd Wnw-asistan
pip install -r requirements.txt
python server.py
# → http://localhost:5000
```

### Railway Deploy
```bash
# railway.toml zaten yapılandırılmış
railway up
```

---

## ⚠️ Yasal Uyarı

Bu uygulama **yatırım tavsiyesi değildir**. Göstergeler geçmiş veriye dayalı matematiksel hesaplamadır, gelecekteki fiyat hareketini garanti etmez. Kripto para yatırımları yüksek risk içerir.

---

## 👤 Geliştirici

**Çağdaş Çınar** — [@Cagdascinar](https://github.com/Cagdascinar)

*Claude (Anthropic) ile geliştirildi 🤖*
