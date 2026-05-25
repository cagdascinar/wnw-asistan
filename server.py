import os, time, threading, requests, json
from flask import Flask, jsonify, Response

app = Flask(__name__)

_cache = {}
_lock  = threading.Lock()

def cached(key, ttl, fn):
    with _lock:
        e = _cache.get(key)
        if e and time.time() - e["ts"] < ttl:
            return e["data"]
    data = fn()
    with _lock:
        _cache[key] = {"ts": time.time(), "data": data}
    return data

def get(url, params=None, timeout=8):
    r = requests.get(url, params=params, timeout=timeout,
                     headers={"User-Agent": "BtcDashboard/1.0"})
    r.raise_for_status()
    return r.json()

# ── Binance ─────────────────────────────────────────────────────────────────
B = "https://api.binance.com/api/v3"
BF = "https://fapi.binance.com/fapi/v1"

def fetch_price():
    return cached("price", 5, lambda: _fetch_price())

def _fetch_price():
    d = get(B + "/ticker/24hr", {"symbol": "BTCUSDT"})
    return {
        "price":  float(d["lastPrice"]),
        "change": float(d["priceChangePercent"]),
        "high":   float(d["highPrice"]),
        "low":    float(d["lowPrice"]),
        "vol":    float(d["volume"]),
        "vol_usdt": float(d["quoteVolume"]),
        "open":   float(d["openPrice"]),
    }

def fetch_klines(interval="1h", limit=100):
    key = f"kl_{interval}"
    ttl = 60 if interval in ("1m","5m","15m") else 300
    return cached(key, ttl, lambda: _fetch_klines(interval, limit))

def _fetch_klines(interval, limit):
    raw = get(B + "/klines", {"symbol": "BTCUSDT", "interval": interval, "limit": limit})
    return [{"t": k[0], "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in raw]

def fetch_funding():
    return cached("funding", 300, lambda: _fetch_funding())

def _fetch_funding():
    try:
        d = get(BF + "/premiumIndex", {"symbol": "BTCUSDT"})
        return {
            "rate": float(d.get("lastFundingRate", 0)) * 100,
            "mark": float(d.get("markPrice", 0)),
            "index": float(d.get("indexPrice", 0)),
        }
    except:
        return {"rate": 0, "mark": 0, "index": 0}

def fetch_long_short():
    return cached("ls", 300, lambda: _fetch_long_short())

def _fetch_long_short():
    try:
        d = get(BF + "/globalLongShortAccountRatio",
                {"symbol": "BTCUSDT", "period": "1h", "limit": 1})
        r = float(d[0]["longShortRatio"])
        lp = float(d[0]["longAccount"]) * 100
        sp = float(d[0]["shortAccount"]) * 100
        return {"ratio": r, "long_pct": lp, "short_pct": sp}
    except:
        return {"ratio": 1.0, "long_pct": 50, "short_pct": 50}

def fetch_open_interest():
    return cached("oi", 300, lambda: _fetch_oi())

def _fetch_oi():
    try:
        d = get(BF + "/openInterest", {"symbol": "BTCUSDT"})
        return {"oi": float(d.get("openInterest", 0))}
    except:
        return {"oi": 0}

# ── Fear & Greed ─────────────────────────────────────────────────────────────
def fetch_fng():
    return cached("fng", 3600, lambda: _fetch_fng())

def _fetch_fng():
    try:
        d = get("https://api.alternative.me/fng/?limit=2")
        items = d["data"]
        cur = items[0]
        prev = items[1] if len(items) > 1 else items[0]
        return {
            "value": int(cur["value"]),
            "label": cur["value_classification"],
            "prev":  int(prev["value"]),
            "delta": int(cur["value"]) - int(prev["value"]),
        }
    except:
        return {"value": 50, "label": "Neutral", "prev": 50, "delta": 0}

# ── CoinGecko ────────────────────────────────────────────────────────────────
def fetch_global():
    return cached("global", 600, lambda: _fetch_global())

def _fetch_global():
    try:
        d = get("https://api.coingecko.com/api/v3/global")["data"]
        return {
            "btc_dom": round(d["market_cap_percentage"].get("btc", 0), 2),
            "total_mcap": d["total_market_cap"].get("usd", 0),
            "total_vol": d["total_volume"].get("usd", 0),
            "mcap_change": round(d.get("market_cap_change_percentage_24h_usd", 0), 2),
            "active_coins": d.get("active_cryptocurrencies", 0),
        }
    except:
        return {"btc_dom": 0, "total_mcap": 0, "total_vol": 0, "mcap_change": 0, "active_coins": 0}

# ── Haberler ─────────────────────────────────────────────────────────────────
def fetch_news():
    return cached("news", 600, lambda: _fetch_news())

def _fetch_news():
    try:
        d = get("https://cryptopanic.com/api/v1/posts/",
                {"auth_token": "free", "currencies": "BTC", "public": "true", "limit": 8})
        results = d.get("results", [])
        news = []
        for item in results[:8]:
            title = item.get("title", "")
            votes = item.get("votes", {})
            pos = votes.get("positive", 0)
            neg = votes.get("negative", 0)
            sentiment = "neutral"
            if pos > neg + 2: sentiment = "positive"
            elif neg > pos + 2: sentiment = "negative"
            news.append({
                "title": title,
                "sentiment": sentiment,
                "url": item.get("url", "#"),
                "created": item.get("created_at", ""),
            })
        return news
    except:
        return []

# ── Teknik göstergeler ───────────────────────────────────────────────────────
def closes(klines): return [k["c"] for k in klines]

def rsi(prices, period=14):
    if len(prices) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period-1) + gains[i]) / period
        al = (al * (period-1) + losses[i]) / period
    if al == 0: return 100.0
    return round(100 - 100 / (1 + ag/al), 2)

def ema(prices, period):
    if len(prices) < period: return []
    k = 2 / (period+1)
    r = [sum(prices[:period]) / period]
    for p in prices[period:]: r.append(p*k + r[-1]*(1-k))
    return r

def macd_vals(prices):
    ef = ema(prices, 12); es = ema(prices, 26)
    if not ef or not es: return None, None, None
    mn = min(len(ef), len(es))
    ml = [f-s for f,s in zip(ef[-mn:], es[-mn:])]
    sg = ema(ml, 9)
    if not sg: return None, None, None
    return round(ml[-1],2), round(sg[-1],2), round(ml[-1]-sg[-1],2)

def bollinger(prices, period=20):
    if len(prices) < period: return None, None, None
    w = prices[-period:]
    mid = sum(w)/period
    std = (sum((p-mid)**2 for p in w)/period)**0.5
    return round(mid+2*std,2), round(mid,2), round(mid-2*std,2)

def atr(klines, period=14):
    if len(klines) < period+1: return None
    trs = []
    for i in range(1, len(klines)):
        h,l,pc = klines[i]["h"], klines[i]["l"], klines[i-1]["c"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    a = sum(trs[:period])/period
    for t in trs[period:]: a = (a*(period-1)+t)/period
    return round(a, 2)

def ema_score(prices):
    e20 = ema(prices, 20); e50 = ema(prices, 50); e200 = ema(prices, 200)
    if not e20 or not e50 or not e200: return 0
    p = prices[-1]
    score = 0
    if p > e20[-1]: score += 1
    if p > e50[-1]: score += 1
    if p > e200[-1]: score += 2
    if e20[-1] > e50[-1]: score += 1
    if e50[-1] > e200[-1]: score += 1
    return score  # 0-6

# ── Ana sinyal motoru ────────────────────────────────────────────────────────
def build_gauges(price_data, kl1h, kl4h, kl1d, fng, funding, ls, glb):
    c1h = closes(kl1h); c4h = closes(kl4h); c1d = closes(kl1d)
    price = price_data["price"]

    r1h = rsi(c1h); r4h = rsi(c4h); r1d = rsi(c1d)
    ml, ms, mh = macd_vals(c1h)
    bu, bm, bl = bollinger(c1h)
    atr1h = atr(kl1h)
    ema_sc = ema_score(c1h)
    fng_v = fng["value"]
    fund = funding["rate"]
    ls_ratio = ls["long_pct"] - 50  # -50..+50, pozitif=long ağır

    gauges = []

    # 1) Fear & Greed  (0-100, 50=nötr)
    fg_score = fng_v - 50  # -50..+50
    gauges.append({
        "id": "fng",
        "label": "Korku & Açgözlülük",
        "value": fng_v,
        "score": fg_score,
        "min": 0, "max": 100, "mid": 50,
        "sub": fng["label"],
        "delta": fng["delta"],
        "unit": "",
    })

    # 2) RSI 1h  (0-100, 50=nötr)
    r1_score = (r1h - 50) if r1h is not None else 0
    gauges.append({
        "id": "rsi1h",
        "label": "RSI 1 Saat",
        "value": r1h if r1h is not None else 50,
        "score": round(r1_score, 1),
        "min": 0, "max": 100, "mid": 50,
        "sub": "Aşırı satım<30 | Alım>70",
        "delta": None,
        "unit": "",
    })

    # 3) RSI 4h
    r4_score = (r4h - 50) if r4h is not None else 0
    gauges.append({
        "id": "rsi4h",
        "label": "RSI 4 Saat",
        "value": r4h if r4h is not None else 50,
        "score": round(r4_score, 1),
        "min": 0, "max": 100, "mid": 50,
        "sub": "Trend gücü",
        "delta": None,
        "unit": "",
    })

    # 4) MACD histogram (-100..+100 ölçeklenmiş)
    macd_norm = 0
    if mh is not None and atr1h:
        macd_norm = round(mh / atr1h * 50, 1)
        macd_norm = max(-100, min(100, macd_norm))
    gauges.append({
        "id": "macd",
        "label": "MACD (1h)",
        "value": mh if mh is not None else 0,
        "score": macd_norm,
        "min": -100, "max": 100, "mid": 0,
        "sub": f"Sinyal: {ms}" if ms is not None else "--",
        "delta": None,
        "unit": "",
        "raw_label": f"{mh:+.1f}" if mh is not None else "--",
    })

    # 5) Funding Rate (-100..+100, 0=nötr)
    fund_score = round(fund * 500, 1)  # %0.02 → +10
    fund_score = max(-100, min(100, fund_score))
    gauges.append({
        "id": "funding",
        "label": "Funding Rate",
        "value": round(fund, 4),
        "score": fund_score,
        "min": -100, "max": 100, "mid": 0,
        "sub": "Pozitif=long öder",
        "delta": None,
        "unit": "%",
        "raw_label": f"{fund:+.4f}%",
    })

    # 6) Long/Short Ratio  (-50..+50)
    ls_score = round(ls_ratio, 1)
    gauges.append({
        "id": "ls",
        "label": "Long/Short",
        "value": round(ls["long_pct"], 1),
        "score": ls_score,
        "min": -50, "max": 50, "mid": 0,
        "sub": f"Long %{ls['long_pct']:.1f} / Short %{ls['short_pct']:.1f}",
        "delta": None,
        "unit": "%",
        "raw_label": f"L:{ls['long_pct']:.0f}% S:{ls['short_pct']:.0f}%",
    })

    # 7) BTC Dominance (30-75 arası normal)
    dom = glb["btc_dom"]
    dom_score = round((dom - 52) * 3, 1)  # 52=nötr
    dom_score = max(-100, min(100, dom_score))
    gauges.append({
        "id": "dom",
        "label": "BTC Dominans",
        "value": dom,
        "score": dom_score,
        "min": 30, "max": 75, "mid": 52,
        "sub": f"Global mcap değ: {glb['mcap_change']:+.2f}%",
        "delta": None,
        "unit": "%",
    })

    # 8) EMA Trend Skoru (0-6 → -100..+100)
    ema_norm = round((ema_sc - 3) * 33, 0)
    ema_norm = max(-100, min(100, ema_norm))
    gauges.append({
        "id": "ema",
        "label": "EMA Trendi",
        "value": ema_sc,
        "score": ema_norm,
        "min": 0, "max": 6, "mid": 3,
        "sub": "EMA 20/50/200",
        "delta": None,
        "unit": "/6",
    })

    # Genel sinyal
    scores = [g["score"] for g in gauges]
    total = sum(scores) / len(scores)
    if total > 15: action, color = "AL", "buy"
    elif total < -15: action, color = "SAT", "sell"
    else: action, color = "BEKLE", "hold"

    return {
        "gauges": gauges,
        "total_score": round(total, 1),
        "action": action,
        "color": color,
    }

# ── API ──────────────────────────────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    return jsonify({"ok": True, "app": "btc-dashboard"})

@app.route("/api/dashboard")
def api_dashboard():
    try:
        price  = fetch_price()
        kl1h   = fetch_klines("1h", 120)
        kl4h   = fetch_klines("4h", 100)
        kl1d   = fetch_klines("1d", 60)
        fng    = fetch_fng()
        fund   = fetch_funding()
        ls     = fetch_long_short()
        glb    = fetch_global()
        gauges = build_gauges(price, kl1h, kl4h, kl1d, fng, fund, ls, glb)
        return jsonify({"ok": True, "price": price, "gauges": gauges,
                        "funding": fund, "ls": ls, "global": glb, "fng": fng})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/news")
def api_news():
    try:
        return jsonify({"ok": True, "news": fetch_news()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/klines/<interval>")
def api_klines(interval):
    if interval not in {"1m","5m","15m","1h","4h","1d"}:
        return jsonify({"ok": False, "error": "invalid"}), 400
    try:
        return jsonify({"ok": True, "data": fetch_klines(interval, 100)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── HTML ─────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#080812">
<title>BTC Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#080812;--s1:#0f0f1e;--s2:#161628;--card:#1a1a2e;--border:#252540;
  --text:#eeeef8;--sub:#7070a0;--accent:#f7931a;
  --buy:#00e676;--sell:#ff3d5a;--hold:#ffb300;
  --r:16px;
}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Segoe UI',sans-serif;overflow-x:hidden}

/* ── Header ── */
.hdr{background:var(--s1);border-bottom:1px solid var(--border);padding:12px 16px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:50}
.hdr-logo{font-size:20px;font-weight:800;display:flex;align-items:center;gap:8px}
.hdr-logo span{color:var(--accent)}
.hdr-right{display:flex;align-items:center;gap:10px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--buy)}
.dot.off{background:var(--sell)}
.upd{font-size:11px;color:var(--sub)}
.rbtn{background:none;border:1px solid var(--border);color:var(--text);width:30px;height:30px;border-radius:8px;font-size:15px;cursor:pointer;display:flex;align-items:center;justify-content:center}
.rbtn:active{opacity:.6}
@keyframes spin{to{transform:rotate(360deg)}}
.spinning{animation:spin .7s linear infinite}

/* ── Price hero ── */
.hero{padding:20px 16px 16px;text-align:center}
.hero-price{font-size:44px;font-weight:800;letter-spacing:-2px;line-height:1}
.hero-chg{font-size:18px;font-weight:600;margin-top:6px}
.hero-chg.up{color:var(--buy)} .hero-chg.down{color:var(--sell)}
.hero-meta{display:flex;justify-content:center;gap:24px;margin-top:12px}
.hm-item{text-align:center}
.hm-val{font-size:13px;font-weight:600}
.hm-lbl{font-size:10px;color:var(--sub);margin-top:2px}

/* ── Signal banner ── */
.sig-banner{margin:0 16px 16px;border-radius:14px;padding:16px 20px;display:flex;align-items:center;justify-content:space-between;border:1.5px solid transparent}
.sig-banner.buy{background:rgba(0,230,118,.1);border-color:var(--buy)}
.sig-banner.sell{background:rgba(255,61,90,.1);border-color:var(--sell)}
.sig-banner.hold{background:rgba(255,179,0,.1);border-color:var(--hold)}
.sig-action{font-size:30px;font-weight:900;letter-spacing:2px}
.sig-banner.buy .sig-action{color:var(--buy)}
.sig-banner.sell .sig-action{color:var(--sell)}
.sig-banner.hold .sig-action{color:var(--hold)}
.sig-score-wrap{text-align:right}
.sig-score-val{font-size:28px;font-weight:800}
.sig-score-lbl{font-size:11px;color:var(--sub)}

/* ── Gauge grid ── */
.sec-title{font-size:11px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.8px;padding:0 16px 10px}
.gauge-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;padding:0 16px 16px}

@media(min-width:480px){.gauge-grid{grid-template-columns:repeat(3,1fr)}}
@media(min-width:700px){.gauge-grid{grid-template-columns:repeat(4,1fr)}}

.gauge-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px 10px;display:flex;flex-direction:column;align-items:center;gap:6px;cursor:pointer;transition:.15s}
.gauge-card:active{opacity:.8}
.g-label{font-size:11px;color:var(--sub);font-weight:500;text-align:center}
.g-sub{font-size:10px;color:var(--sub);text-align:center;margin-top:2px;min-height:14px}

/* ── SVG Gauge ── */
.g-svg{width:110px;height:70px;overflow:visible}

/* ── Mum grafik ── */
.chart-wrap{padding:0 16px 16px}
.chart-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.tabs{display:flex;gap:5px}
.tab{background:var(--card);border:1px solid var(--border);color:var(--sub);padding:5px 10px;border-radius:8px;font-size:12px;cursor:pointer}
.tab.on{background:var(--accent);border-color:var(--accent);color:#000;font-weight:700}
canvas{border-radius:12px;background:var(--card);border:1px solid var(--border);display:block}

/* ── Haberler ── */
.news-wrap{padding:0 16px 20px}
.news-item{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:8px;display:flex;align-items:flex-start;gap:10px;text-decoration:none;color:var(--text)}
.news-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;margin-top:5px}
.news-dot.positive{background:var(--buy)}
.news-dot.negative{background:var(--sell)}
.news-dot.neutral{background:var(--sub)}
.news-title{font-size:13px;line-height:1.4}
.news-loading{color:var(--sub);font-size:13px;padding:10px 0}

/* ── Footer ── */
.footer{padding:16px;text-align:center;font-size:11px;color:var(--sub);border-top:1px solid var(--border)}
</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-logo">&#8383; <span>BTC</span> Analiz</div>
  <div class="hdr-right">
    <div class="dot" id="dot"></div>
    <span class="upd" id="upd">--</span>
    <button class="rbtn" id="rbtn" onclick="loadAll()">&#8635;</button>
  </div>
</div>

<div class="hero">
  <div class="hero-price" id="hp">--</div>
  <div class="hero-chg" id="hc">--</div>
  <div class="hero-meta">
    <div class="hm-item"><div class="hm-val" id="hh">--</div><div class="hm-lbl">24h Yüksek</div></div>
    <div class="hm-item"><div class="hm-val" id="hl">--</div><div class="hm-lbl">24h Düşük</div></div>
    <div class="hm-item"><div class="hm-val" id="hv">--</div><div class="hm-lbl">Hacim (M$)</div></div>
    <div class="hm-item"><div class="hm-val" id="hd">--%</div><div class="hm-lbl">BTC Dom.</div></div>
  </div>
</div>

<div class="sig-banner hold" id="sig">
  <div class="sig-action" id="sig-a">YÜKLENIYOR</div>
  <div class="sig-score-wrap">
    <div class="sig-score-val" id="sig-s">--</div>
    <div class="sig-score-lbl">Ortalama Skor</div>
  </div>
</div>

<div class="sec-title">GÖSTERGELER</div>
<div class="gauge-grid" id="gauge-grid"></div>

<div class="chart-wrap">
  <div class="chart-hdr">
    <div class="sec-title" style="padding:0">GRAFİK</div>
    <div class="tabs">
      <button class="tab" onclick="sw('15m')" id="t15m">15m</button>
      <button class="tab on" onclick="sw('1h')" id="t1h">1s</button>
      <button class="tab" onclick="sw('4h')" id="t4h">4s</button>
      <button class="tab" onclick="sw('1d')" id="t1d">1G</button>
    </div>
  </div>
  <canvas id="cv"></canvas>
</div>

<div class="sec-title">HABERLER</div>
<div class="news-wrap" id="news-wrap">
  <div class="news-loading">Haberler yükleniyor...</div>
</div>

<div class="footer">Binance &bull; CoinGecko &bull; Alternative.me &bull; Yatırım tavsiyesi değildir</div>

<script>
var curIv = '1h';
var chartKlines = [];

function fmtP(n) {
  if (n == null) return '--';
  return '$' + Number(n).toLocaleString('tr-TR', {minimumFractionDigits: 0, maximumFractionDigits: 0});
}
function fmtM(n) {
  if (!n) return '--';
  return (n / 1e6).toFixed(0) + 'M';
}

function now() {
  var d = new Date();
  return d.getHours().toString().padStart(2,'0') + ':' +
         d.getMinutes().toString().padStart(2,'0') + ':' +
         d.getSeconds().toString().padStart(2,'0');
}

// ── SVG Gauge ──────────────────────────────────────────────────────────────
// Yarım daire: sol=-100, orta=0, sağ=+100
// score: -100..+100
function gaugeColor(score) {
  if (score > 20) return '#00e676';
  if (score < -20) return '#ff3d5a';
  return '#ffb300';
}

function buildGaugeSVG(score, rawLabel) {
  var W = 110, H = 70;
  var cx = 55, cy = 62, r = 46;

  // Arc: -180° (sol) → 0° (sağ), centre bottom
  // angle for score: map -100..+100 to -180..0 (degrees from right, but we go left arc)
  // In SVG coords: start angle = 180° (left), end = 0° (right)
  // score -100 → angle 180, score 0 → angle 90 (top), score +100 → 0

  function polar(deg) {
    var rad = deg * Math.PI / 180;
    return {
      x: cx + r * Math.cos(rad),
      y: cy + r * Math.sin(rad)
    };
  }

  // Background arc: 180° → 0° (full semicircle top half)
  var startDeg = 180;
  var endDeg = 0;
  var startP = polar(startDeg);
  var endP = polar(endDeg);
  var bgArc = 'M ' + startP.x + ' ' + startP.y + ' A ' + r + ' ' + r + ' 0 0 1 ' + endP.x + ' ' + endP.y;

  // Fill arc: from 180 to score angle
  var scoreDeg = 180 - (score + 100) / 200 * 180;
  var scoreP = polar(scoreDeg);
  var largeArc = (scoreDeg - startDeg) > 180 ? 1 : 0;

  // If score < 0: arc goes from startP to scoreP (short)
  // If score > 0: arc goes from startP → scoreP (longer)
  var sweepAngle = Math.abs(180 - scoreDeg);
  var la = sweepAngle > 180 ? 1 : 0;
  var fillArc = 'M ' + startP.x + ' ' + startP.y + ' A ' + r + ' ' + r + ' 0 ' + la + ' 1 ' + scoreP.x + ' ' + scoreP.y;

  var col = gaugeColor(score);
  var displayVal = rawLabel !== undefined ? rawLabel : (score > 0 ? '+' + score : '' + score);

  var svg = '<svg class="g-svg" viewBox="0 0 110 70" xmlns="http://www.w3.org/2000/svg">';
  // bg track
  svg += '<path d="' + bgArc + '" fill="none" stroke="#252540" stroke-width="8" stroke-linecap="round"/>';
  // fill
  svg += '<path d="' + fillArc + '" fill="none" stroke="' + col + '" stroke-width="8" stroke-linecap="round" opacity="0.9"/>';
  // center dot
  svg += '<circle cx="' + cx + '" cy="' + cy + '" r="3" fill="' + col + '"/>';
  // value text
  svg += '<text x="' + cx + '" y="' + (cy - 12) + '" text-anchor="middle" font-size="16" font-weight="800" fill="' + col + '" font-family="-apple-system,sans-serif">' + displayVal + '</text>';
  // min/max labels
  svg += '<text x="8" y="' + (cy + 14) + '" font-size="9" fill="#7070a0" font-family="-apple-system,sans-serif">-</text>';
  svg += '<text x="97" y="' + (cy + 14) + '" font-size="9" fill="#7070a0" font-family="-apple-system,sans-serif">+</text>';
  svg += '</svg>';
  return svg;
}

function renderGauges(gauges) {
  var grid = document.getElementById('gauge-grid');
  var html = '';
  for (var i = 0; i < gauges.length; i++) {
    var g = gauges[i];
    var rl = g.raw_label !== undefined ? g.raw_label : null;
    var scoreDisp = g.score > 0 ? '+' + g.score : '' + g.score;
    html += '<div class="gauge-card">';
    html += '<div class="g-label">' + g.label + '</div>';
    html += buildGaugeSVG(g.score, scoreDisp);
    if (g.delta !== null && g.delta !== undefined) {
      var dsign = g.delta >= 0 ? '+' : '';
      html += '<div class="g-sub">' + g.sub + ' (' + dsign + g.delta + ')</div>';
    } else {
      html += '<div class="g-sub">' + (g.raw_label || g.sub) + '</div>';
    }
    html += '</div>';
  }
  grid.innerHTML = html;
}

// ── Dashboard yükle ────────────────────────────────────────────────────────
function loadDash() {
  document.getElementById('rbtn').classList.add('spinning');
  var xhr = new XMLHttpRequest();
  xhr.open('GET', '/api/dashboard', true);
  xhr.onreadystatechange = function() {
    if (xhr.readyState !== 4) return;
    document.getElementById('rbtn').classList.remove('spinning');
    if (xhr.status !== 200) { document.getElementById('dot').className = 'dot off'; return; }
    var res;
    try { res = JSON.parse(xhr.responseText); } catch(e) { return; }
    if (!res.ok) { document.getElementById('dot').className = 'dot off'; return; }

    document.getElementById('dot').className = 'dot';
    document.getElementById('upd').textContent = now();

    // Price
    var p = res.price;
    document.getElementById('hp').textContent = fmtP(p.price);
    var chgEl = document.getElementById('hc');
    chgEl.textContent = (p.change >= 0 ? '+' : '') + p.change.toFixed(2) + '%';
    chgEl.className = 'hero-chg ' + (p.change >= 0 ? 'up' : 'down');
    document.getElementById('hh').textContent = fmtP(p.high);
    document.getElementById('hl').textContent = fmtP(p.low);
    document.getElementById('hv').textContent = fmtM(p.vol_usdt);
    document.getElementById('hd').textContent = (res.global ? res.global.btc_dom.toFixed(1) : '--') + '%';

    // Signal
    var g = res.gauges;
    var sig = document.getElementById('sig');
    sig.className = 'sig-banner ' + g.color;
    document.getElementById('sig-a').textContent = g.action;
    var ts = g.total_score;
    document.getElementById('sig-s').textContent = (ts > 0 ? '+' : '') + ts;

    // Gauges
    renderGauges(g.gauges);

    // Chart (1h klines)
    loadKlines(curIv);
  };
  xhr.send();
}

// ── Haberler ───────────────────────────────────────────────────────────────
function loadNews() {
  var xhr = new XMLHttpRequest();
  xhr.open('GET', '/api/news', true);
  xhr.onreadystatechange = function() {
    if (xhr.readyState !== 4 || xhr.status !== 200) return;
    var res;
    try { res = JSON.parse(xhr.responseText); } catch(e) { return; }
    if (!res.ok || !res.news.length) {
      document.getElementById('news-wrap').innerHTML = '<div class="news-loading">Haber bulunamadı</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < res.news.length; i++) {
      var n = res.news[i];
      html += '<a class="news-item" href="' + n.url + '" target="_blank">';
      html += '<div class="news-dot ' + n.sentiment + '"></div>';
      html += '<div class="news-title">' + n.title + '</div>';
      html += '</a>';
    }
    document.getElementById('news-wrap').innerHTML = html;
  };
  xhr.send();
}

// ── Klines / Chart ─────────────────────────────────────────────────────────
function loadKlines(iv) {
  var xhr = new XMLHttpRequest();
  xhr.open('GET', '/api/klines/' + iv, true);
  xhr.onreadystatechange = function() {
    if (xhr.readyState !== 4 || xhr.status !== 200) return;
    var res;
    try { res = JSON.parse(xhr.responseText); } catch(e) { return; }
    if (!res.ok) return;
    chartKlines = res.data;
    drawChart(chartKlines, iv);
  };
  xhr.send();
}

function sw(iv) {
  curIv = iv;
  var tabs = ['15m','1h','4h','1d'];
  for (var i = 0; i < tabs.length; i++) {
    var el = document.getElementById('t' + tabs[i]);
    if (el) el.className = 'tab' + (tabs[i] === iv ? ' on' : '');
  }
  loadKlines(iv);
}

function drawChart(klines, iv) {
  var cv = document.getElementById('cv');
  var W = cv.parentElement.clientWidth - 32;
  cv.width = W; cv.height = 200;
  var ctx = cv.getContext('2d');
  var H = 200;
  var pad = {t:12, r:8, b:24, l:60};
  var dw = W - pad.l - pad.r;
  var dh = H - pad.t - pad.b;
  var data = klines.slice(-60);
  if (!data.length) return;
  var hi = Math.max.apply(null, data.map(function(k){return k.h;}));
  var lo = Math.min.apply(null, data.map(function(k){return k.l;}));
  var rng = hi - lo || 1;
  function toY(p){ return pad.t + dh - (p-lo)/rng*dh; }
  function toX(i){ return pad.l + i*(dw/data.length) + dw/data.length*0.5; }

  // Grid
  ctx.strokeStyle = 'rgba(255,255,255,0.04)'; ctx.lineWidth = 1;
  for (var r = 0; r <= 4; r++) {
    var y = pad.t + dh*r/4;
    ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(W-pad.r,y); ctx.stroke();
    var pv = hi - rng*r/4;
    ctx.fillStyle = '#7070a0'; ctx.font = '9px -apple-system,sans-serif'; ctx.textAlign = 'right';
    ctx.fillText(pv.toLocaleString('tr-TR',{maximumFractionDigits:0}), pad.l-4, y+4);
  }
  var cw = Math.max(dw/data.length*0.72, 1.5);
  for (var i = 0; i < data.length; i++) {
    var k = data[i];
    var x = toX(i);
    var bull = k.c >= k.o;
    ctx.strokeStyle = bull ? '#00e676' : '#ff3d5a';
    ctx.fillStyle = bull ? 'rgba(0,230,118,.75)' : 'rgba(255,61,90,.75)';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, toY(k.h)); ctx.lineTo(x, toY(k.l)); ctx.stroke();
    var yo = toY(k.o), yc = toY(k.c);
    ctx.fillRect(x-cw/2, Math.min(yo,yc), cw, Math.abs(yc-yo)||1);
  }
  // Time labels
  ctx.fillStyle = '#7070a0'; ctx.font = '8px -apple-system,sans-serif'; ctx.textAlign = 'center';
  var step = Math.floor(data.length/5)||1;
  for (var j = 0; j < data.length; j += step) {
    var d = new Date(data[j].t);
    var lbl = iv === '1d' ? (d.getMonth()+1)+'/'+d.getDate()
              : d.getHours().toString().padStart(2,'0')+':'+d.getMinutes().toString().padStart(2,'0');
    ctx.fillText(lbl, toX(j), H-6);
  }
}

window.onresize = function() { if (chartKlines.length) drawChart(chartKlines, curIv); };

function loadAll() {
  loadDash();
  loadNews();
}

loadAll();
setInterval(loadAll, 30000);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
