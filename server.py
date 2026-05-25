import os, time, threading, requests, json
from flask import Flask, jsonify, Response, request
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=8)

_cache = {}
_lock  = threading.Lock()

# ── Açık pozisyonlar (in-memory, basit) ────────────────────────────────────
_positions = {}   # session_id → {dir, entry, opened_at}

def cached(key, ttl, fn):
    with _lock:
        e = _cache.get(key)
        if e and time.time() - e["ts"] < ttl:
            return e["data"]
    try:
        data = fn()
    except Exception:
        with _lock:
            e = _cache.get(key)
        return e["data"] if e else None
    with _lock:
        _cache[key] = {"ts": time.time(), "data": data}
    return data

def GET(url, params=None, timeout=6):
    r = requests.get(url, params=params, timeout=timeout,
                     headers={"User-Agent": "BtcDash/2.0"})
    r.raise_for_status()
    return r.json()

# ── CryptoCompare fiyat (Binance yerine - cloud IP bloğu yok) ───────────────
CC = "https://min-api.cryptocompare.com/data"

def fetch_price():
    return cached("price", 10, _price)

def _price():
    d = GET(CC + "/pricemultifull", {"fsyms": "BTC", "tsyms": "USD"})
    r = d["RAW"]["BTC"]["USD"]
    return {
        "price":    float(r["PRICE"]),
        "change":   round(float(r["CHANGEPCT24HOUR"]), 2),
        "high":     float(r["HIGH24HOUR"]),
        "low":      float(r["LOW24HOUR"]),
        "vol_usdt": float(r.get("VOLUMEDAYTO", 0)),
        "open":     float(r["OPEN24HOUR"]),
    }

# interval map: "1h"→histohour, "4h"→histohour(agg=4), "1d"→histoday, "15m"→histominute(agg=15)
_CC_MAP = {
    "1m":  ("histominute", 1),
    "5m":  ("histominute", 5),
    "15m": ("histominute", 15),
    "1h":  ("histohour",   1),
    "4h":  ("histohour",   4),
    "1d":  ("histoday",    1),
}

def fetch_klines(interval="1h", limit=100):
    ttl = 60 if interval in ("1m","5m","15m") else 300
    return cached(f"kl_{interval}", ttl, lambda: _klines(interval, limit))

def _klines(interval, limit):
    ep, agg = _CC_MAP.get(interval, ("histohour", 1))
    params = {"fsym": "BTC", "tsym": "USD", "limit": limit, "aggregate": agg}
    d = GET(f"{CC}/v2/{ep}", params)
    rows = d["Data"]["Data"]
    # CryptoCompare time is Unix seconds → ms for frontend
    return [{"t": int(k["time"]) * 1000, "o": float(k["open"]),
             "h": float(k["high"]), "l": float(k["low"]),
             "c": float(k["close"]), "v": float(k["volumeto"])} for k in rows]

# ── Bybit funding rate (Binance futures yerine) ──────────────────────────────
def fetch_funding():
    return cached("funding", 300, _funding)

def _funding():
    try:
        d = GET("https://api.bybit.com/v5/market/tickers",
                {"category": "linear", "symbol": "BTCUSDT"}, timeout=5)
        r = d["result"]["list"][0]
        return {
            "rate": round(float(r.get("fundingRate", 0)) * 100, 4),
            "mark": float(r.get("markPrice", 0)),
        }
    except Exception:
        return {"rate": 0.0, "mark": 0}

# ── Fear & Greed ─────────────────────────────────────────────────────────────
def fetch_fng():
    return cached("fng", 3600, _fng)

def _fng():
    try:
        d = GET("https://api.alternative.me/fng/?limit=2", timeout=5)
        items = d["data"]
        cur = items[0]; prev = items[1] if len(items) > 1 else items[0]
        return {
            "value": int(cur["value"]),
            "label": cur["value_classification"],
            "delta": int(cur["value"]) - int(prev["value"]),
        }
    except Exception:
        return {"value": 50, "label": "Neutral", "delta": 0}

# ── CoinGecko global ─────────────────────────────────────────────────────────
def fetch_global():
    return cached("global", 600, _global)

def _global():
    try:
        d = GET("https://api.coingecko.com/api/v3/global", timeout=6)["data"]
        return {
            "btc_dom":    round(d["market_cap_percentage"].get("btc", 0), 2),
            "mcap_change": round(d.get("market_cap_change_percentage_24h_usd", 0), 2),
        }
    except Exception:
        return {"btc_dom": 0.0, "mcap_change": 0.0}

# ── Haberler (CryptoCompare – ücretsiz, API key gerekmez) ────────────────────
def fetch_news():
    return cached("news", 600, _news)

def _news():
    try:
        d = GET("https://min-api.cryptocompare.com/data/v2/news/?lang=EN&categories=BTC", timeout=6)
        items = (d.get("Data") or [])[:8]
        news = []
        for n in items:
            title = n.get("title", "")
            body  = n.get("body", "").lower()
            pos_words = ["surge", "rally", "gain", "bull", "up", "high", "growth", "rise"]
            neg_words = ["drop", "crash", "fall", "bear", "down", "low", "plunge", "sell"]
            pos = sum(1 for w in pos_words if w in body)
            neg = sum(1 for w in neg_words if w in body)
            sentiment = "positive" if pos > neg else ("negative" if neg > pos else "neutral")
            news.append({"title": title, "url": n.get("url", "#"), "sentiment": sentiment})
        return news
    except Exception:
        return []

# ── Teknik göstergeler ───────────────────────────────────────────────────────
def closes(klines): return [k["c"] for k in klines]

def rsi(prices, period=14):
    if len(prices) < period + 1: return None
    g, l = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
        g.append(max(d, 0)); l.append(max(-d, 0))
    ag = sum(g[:period]) / period; al = sum(l[:period]) / period
    for i in range(period, len(g)):
        ag = (ag*(period-1)+g[i])/period; al = (al*(period-1)+l[i])/period
    if al == 0: return 100.0
    return round(100 - 100/(1+ag/al), 2)

def ema(prices, period):
    if len(prices) < period: return []
    k = 2/(period+1); r = [sum(prices[:period])/period]
    for p in prices[period:]: r.append(p*k+r[-1]*(1-k))
    return r

def macd_vals(prices):
    ef = ema(prices, 12); es = ema(prices, 26)
    if not ef or not es: return None, None, None
    n = min(len(ef), len(es))
    ml = [f-s for f,s in zip(ef[-n:], es[-n:])]
    sg = ema(ml, 9)
    if not sg: return None, None, None
    return round(ml[-1],2), round(sg[-1],2), round(ml[-1]-sg[-1],2)

def bollinger(prices, period=20):
    if len(prices) < period: return None, None, None
    w = prices[-period:]; mid = sum(w)/period
    std = (sum((p-mid)**2 for p in w)/period)**0.5
    return round(mid+2*std,2), round(mid,2), round(mid-2*std,2)

def atr(klines, period=14):
    if len(klines) < period+1: return None
    trs = [max(klines[i]["h"]-klines[i]["l"],
               abs(klines[i]["h"]-klines[i-1]["c"]),
               abs(klines[i]["l"]-klines[i-1]["c"])) for i in range(1, len(klines))]
    a = sum(trs[:period])/period
    for t in trs[period:]: a = (a*(period-1)+t)/period
    return round(a, 2)

def ema_score(prices):
    e20 = ema(prices, 20); e50 = ema(prices, 50); e200 = ema(prices, 200)
    if not e20 or not e50 or not e200: return 0
    p = prices[-1]; sc = 0
    if p > e20[-1]: sc += 1
    if p > e50[-1]: sc += 1
    if p > e200[-1]: sc += 2
    if e20[-1] > e50[-1]: sc += 1
    if e50[-1] > e200[-1]: sc += 1
    return sc

# ── Gösterge hesaplama ────────────────────────────────────────────────────────
def build_gauges(price_data, kl1h, kl4h, kl1d, fng, funding, glb):
    c1h = closes(kl1h); c4h = closes(kl4h); c1d = closes(kl1d)
    price = price_data["price"]

    r1h = rsi(c1h); r4h = rsi(c4h)
    ml, ms, mh = macd_vals(c1h)
    bu, bm, bl = bollinger(c1h)
    atr1h = atr(kl1h) or 1
    ema_sc = ema_score(c1h)
    fund = funding["rate"] if funding else 0.0

    gauges = []

    # 1) Fear & Greed (0-100 → score -50..+50)
    fg_s = round(fng["value"] - 50, 1)
    gauges.append({"id":"fng","label":"Korku & Açgözlülük",
        "value":fng["value"],"score":fg_s,"sub":fng["label"],
        "delta":fng["delta"],"raw":None})

    # 2) RSI 1h (0-100 → score -50..+50)
    r1s = round((r1h or 50) - 50, 1)
    rsi1_lbl = "Aşırı Satım" if (r1h or 50)<30 else ("Aşırı Alım" if (r1h or 50)>70 else "Nötr")
    gauges.append({"id":"rsi1h","label":"RSI 1 Saat",
        "value":r1h or 50,"score":r1s,"sub":rsi1_lbl,"delta":None,"raw":None})

    # 3) RSI 4h
    r4s = round((r4h or 50) - 50, 1)
    rsi4_lbl = "Aşırı Satım" if (r4h or 50)<30 else ("Aşırı Alım" if (r4h or 50)>70 else "Nötr")
    gauges.append({"id":"rsi4h","label":"RSI 4 Saat",
        "value":r4h or 50,"score":r4s,"sub":rsi4_lbl,"delta":None,"raw":None})

    # 4) MACD (histogram normalize)
    mh_norm = round((mh or 0) / atr1h * 50, 1) if atr1h else 0
    mh_norm = max(-100, min(100, mh_norm))
    gauges.append({"id":"macd","label":"MACD (1h)",
        "value":mh or 0,"score":mh_norm,
        "sub":("Yükseliş" if (mh or 0)>0 else "Düşüş"),
        "delta":None,"raw":f"{mh:+.1f}" if mh is not None else "--"})

    # 5) Bollinger pozisyon (-100..+100)
    if bu and bl and bu != bl:
        bb_pos = round((price - bl) / (bu - bl) * 200 - 100, 1)
        bb_pos = max(-100, min(100, bb_pos))
        bb_lbl = f"Üst:{bu:,.0f} / Alt:{bl:,.0f}"
    else:
        bb_pos = 0; bb_lbl = "--"
    gauges.append({"id":"bb","label":"Bollinger Bandı",
        "value":round(price),"score":-bb_pos,
        "sub":bb_lbl,"delta":None,"raw":None})

    # 6) Funding Rate (-100..+100)
    fund_s = round(fund * 1000, 1); fund_s = max(-100, min(100, fund_s))
    gauges.append({"id":"funding","label":"Funding Rate",
        "value":fund,"score":-fund_s,
        "sub":("Longler öder" if fund>0 else ("Shortlar öder" if fund<0 else "Nötr")),
        "delta":None,"raw":f"{fund:+.4f}%"})

    # 7) BTC Dominans (52=nötr, >52 bullish BTC)
    dom = glb["btc_dom"] if glb else 52
    dom_s = round((dom - 52) * 5, 1); dom_s = max(-100, min(100, dom_s))
    gauges.append({"id":"dom","label":"BTC Dominans",
        "value":dom,"score":dom_s,
        "sub":f"Piyasa değ: {glb['mcap_change']:+.2f}%" if glb else "--",
        "delta":None,"raw":f"{dom:.1f}%"})

    # 8) EMA Trendi (0-6 → -100..+100)
    ema_s = round((ema_sc - 3) * 33, 0); ema_s = max(-100, min(100, int(ema_s)))
    gauges.append({"id":"ema","label":"EMA Trendi",
        "value":ema_sc,"score":ema_s,
        "sub":"EMA 20/50/200","delta":None,"raw":f"{ema_sc}/6"})

    scores = [g["score"] for g in gauges]
    total = round(sum(scores)/len(scores), 1)
    action = "AL" if total > 15 else ("SAT" if total < -15 else "BEKLE")
    color  = "buy" if action == "AL" else ("sell" if action == "SAT" else "hold")

    return {"gauges": gauges, "total_score": total,
            "action": action, "color": color}

# ── API endpoints ─────────────────────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    return jsonify({"ok": True, "app": "btc-dashboard-v3"})

@app.route("/api/debug")
def api_debug():
    results = {}
    tests = {
        "cryptocompare_price": lambda: GET(CC + "/pricemultifull", {"fsyms":"BTC","tsyms":"USD"}, timeout=8),
        "cryptocompare_klines": lambda: GET(f"{CC}/v2/histohour", {"fsym":"BTC","tsym":"USD","limit":5}, timeout=8),
        "bybit_funding": lambda: GET("https://api.bybit.com/v5/market/tickers", {"category":"linear","symbol":"BTCUSDT"}, timeout=6),
        "alternative_fng": lambda: GET("https://api.alternative.me/fng/?limit=1", timeout=5),
        "coingecko_global": lambda: GET("https://api.coingecko.com/api/v3/global", timeout=6),
    }
    for name, fn in tests.items():
        try:
            fn()
            results[name] = "OK"
        except Exception as e:
            results[name] = str(e)[:120]
    return jsonify(results)

@app.route("/api/price")
def api_price():
    try:
        return jsonify({"ok": True, "data": fetch_price()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/dashboard")
def api_dashboard():
    # Paralel fetch
    futures = {
        "price":   executor.submit(fetch_price),
        "kl1h":    executor.submit(fetch_klines, "1h", 120),
        "kl4h":    executor.submit(fetch_klines, "4h", 100),
        "kl1d":    executor.submit(fetch_klines, "1d", 60),
        "fng":     executor.submit(fetch_fng),
        "funding": executor.submit(fetch_funding),
        "global":  executor.submit(fetch_global),
    }
    results = {}
    for key, fut in futures.items():
        try:
            results[key] = fut.result(timeout=10)
        except Exception:
            results[key] = None

    price = results["price"]
    if not price:
        return jsonify({"ok": False, "error": "Fiyat verisi alınamadı — /api/debug kontrol et"}), 500

    kl1h = results["kl1h"] or []
    kl4h = results["kl4h"] or []
    kl1d = results["kl1d"] or []

    gauges = None
    if kl1h and kl4h:
        try:
            gauges = build_gauges(price, kl1h, kl4h, kl1d,
                                  results["fng"] or {"value":50,"label":"Neutral","delta":0},
                                  results["funding"] or {"rate":0.0,"mark":0},
                                  results["global"] or {"btc_dom":0.0,"mcap_change":0.0})
        except Exception as e:
            gauges = None

    return jsonify({
        "ok": True,
        "price": price,
        "gauges": gauges,
        "fng": results["fng"],
        "funding": results["funding"],
        "global": results["global"],
        "klines_1h": (kl1h[-50:] if kl1h else []),
    })

@app.route("/api/signal")
def api_signal():
    """Hızlı sinyal – bildirimler için"""
    try:
        price = fetch_price()
        kl1h  = fetch_klines("1h", 50)
        c = closes(kl1h) if kl1h else []
        r1 = rsi(c)
        ml, ms, mh = macd_vals(c)
        fng = fetch_fng()
        score = 0
        if r1 is not None:
            if r1 < 35: score += 2
            elif r1 > 65: score -= 2
        if mh is not None:
            score += 1 if mh > 0 else -1
        if fng["value"] < 30: score += 1
        elif fng["value"] > 70: score -= 1
        action = "AL" if score >= 2 else ("SAT" if score <= -2 else "BEKLE")
        return jsonify({
            "ok": True,
            "price": price["price"],
            "change": price["change"],
            "rsi_1h": r1,
            "macd_hist": mh,
            "fng": fng["value"],
            "score": score,
            "action": action,
        })
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

# ── Pozisyon API (sunucu taraflı, session_id ile) ────────────────────────────
@app.route("/api/position/open", methods=["POST"])
def pos_open():
    data = request.get_json(force=True, silent=True) or {}
    sid  = data.get("sid", "default")
    direction = data.get("direction", "long")
    try:
        price = fetch_price()["price"]
    except Exception:
        return jsonify({"ok": False, "error": "Fiyat alınamadı"}), 500
    _positions[sid] = {
        "direction": direction,
        "entry": price,
        "opened_at": time.time(),
    }
    return jsonify({"ok": True, "entry": price, "direction": direction})

@app.route("/api/position/status")
def pos_status():
    sid = request.args.get("sid", "default")
    pos = _positions.get(sid)
    if not pos:
        return jsonify({"ok": True, "open": False})
    try:
        cur = fetch_price()["price"]
    except Exception:
        return jsonify({"ok": False, "error": "Fiyat alınamadı"}), 500
    diff = cur - pos["entry"]
    if pos["direction"] == "short":
        diff = -diff
    pct = diff / pos["entry"] * 100
    elapsed = int(time.time() - pos["opened_at"])
    mins = elapsed // 60
    return jsonify({
        "ok": True, "open": True,
        "direction": pos["direction"],
        "entry": pos["entry"],
        "current": cur,
        "pnl_pct": round(pct, 3),
        "pnl_usd": round(diff, 2),
        "elapsed_min": mins,
    })

@app.route("/api/position/close", methods=["POST"])
def pos_close():
    data = request.get_json(force=True, silent=True) or {}
    sid  = data.get("sid", "default")
    pos  = _positions.pop(sid, None)
    if not pos:
        return jsonify({"ok": True, "closed": False})
    try:
        cur = fetch_price()["price"]
    except Exception:
        cur = pos["entry"]
    diff = cur - pos["entry"]
    if pos["direction"] == "short": diff = -diff
    pct = diff / pos["entry"] * 100
    return jsonify({
        "ok": True, "closed": True,
        "entry": pos["entry"], "exit": cur,
        "pnl_pct": round(pct, 3),
        "pnl_usd": round(diff, 2),
        "direction": pos["direction"],
    })

# ── HTML ─────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#08080f">
<title>BTC Analiz</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#08080f;--s1:#0e0e1c;--s2:#141426;--card:#181830;--brd:#222240;
  --txt:#eeeef8;--sub:#6868a0;--acc:#f7931a;
  --buy:#00e676;--sell:#ff3d5a;--hold:#ffb300;--r:14px;
  --font:-apple-system,BlinkMacSystemFont,'SF Pro Display',sans-serif;
}
html,body{min-height:100%;background:var(--bg);color:var(--txt);font-family:var(--font);overflow-x:hidden}

.hdr{background:var(--s1);border-bottom:1px solid var(--brd);padding:13px 16px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:99;backdrop-filter:blur(10px)}
.logo{font-size:19px;font-weight:800;display:flex;align-items:center;gap:7px}
.logo-btc{color:var(--acc)}
.hdr-r{display:flex;align-items:center;gap:9px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--buy);flex-shrink:0}
.dot.off{background:var(--sell)}
.upd-t{font-size:11px;color:var(--sub)}
.notif-btn{background:none;border:1px solid var(--brd);color:var(--sub);padding:5px 9px;border-radius:8px;font-size:12px;cursor:pointer;transition:.2s}
.notif-btn.on{color:var(--acc);border-color:var(--acc)}
.rbtn{background:none;border:1px solid var(--brd);color:var(--txt);width:30px;height:30px;border-radius:8px;font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:.15s}
.rbtn:active{opacity:.5}
@keyframes spin{to{transform:rotate(360deg)}}
.spin{animation:spin .7s linear infinite}

/* Hero */
.hero{padding:18px 16px 14px;text-align:center}
.hero-p{font-size:46px;font-weight:900;letter-spacing:-2px;line-height:1}
.hero-c{font-size:17px;font-weight:600;margin-top:5px}
.hero-c.up{color:var(--buy)}.hero-c.dn{color:var(--sell)}
.hero-m{display:flex;justify-content:center;gap:22px;margin-top:10px}
.hm{text-align:center}
.hm-v{font-size:13px;font-weight:600}
.hm-l{font-size:10px;color:var(--sub);margin-top:2px}

/* Pozisyon kartı */
.pos-card{margin:0 16px 14px;border-radius:var(--r);padding:14px 16px;border:2px solid var(--brd);display:none}
.pos-card.long-open{background:rgba(0,230,118,.08);border-color:var(--buy)}
.pos-card.short-open{background:rgba(255,61,90,.08);border-color:var(--sell)}
.pos-top{display:flex;align-items:center;justify-content:space-between}
.pos-dir-badge{font-size:12px;font-weight:700;padding:3px 10px;border-radius:20px}
.pos-dir-badge.long{background:rgba(0,230,118,.2);color:var(--buy)}
.pos-dir-badge.short{background:rgba(255,61,90,.2);color:var(--sell)}
.pos-pnl{font-size:28px;font-weight:900;line-height:1;margin-top:8px}
.pos-pnl.pos{color:var(--buy)}.pos-pnl.neg{color:var(--sell)}
.pos-info{font-size:11px;color:var(--sub);margin-top:4px}
.pos-close{background:var(--card);border:1px solid var(--brd);color:var(--txt);padding:6px 14px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;margin-top:10px}
.pos-close:active{opacity:.6}

/* Signal */
.sig{margin:0 16px 14px;border-radius:var(--r);padding:14px 18px;display:flex;align-items:center;justify-content:space-between;border:1.5px solid var(--brd)}
.sig.buy{background:rgba(0,230,118,.09);border-color:var(--buy)}
.sig.sell{background:rgba(255,61,90,.09);border-color:var(--sell)}
.sig.hold{background:rgba(255,179,0,.09);border-color:var(--hold)}
.sig-a{font-size:28px;font-weight:900;letter-spacing:1px}
.sig.buy .sig-a{color:var(--buy)}.sig.sell .sig-a{color:var(--sell)}.sig.hold .sig-a{color:var(--hold)}
.sig-r{text-align:right}
.sig-sc{font-size:26px;font-weight:800}
.sig-sl{font-size:10px;color:var(--sub);margin-top:2px}

/* Open Position button */
.open-pos-btn{display:block;margin:0 16px 16px;width:calc(100% - 32px);background:linear-gradient(135deg,#1a1a30,#22224a);border:1px solid var(--brd);color:var(--txt);padding:13px;border-radius:var(--r);font-size:14px;font-weight:700;cursor:pointer;letter-spacing:.3px;transition:.15s}
.open-pos-btn:active{opacity:.7}

/* Göstergeler */
.sec{font-size:11px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.8px;padding:0 16px 9px}
.ggrid{display:grid;grid-template-columns:repeat(2,1fr);gap:9px;padding:0 16px 16px}
@media(min-width:480px){.ggrid{grid-template-columns:repeat(3,1fr)}}
@media(min-width:700px){.ggrid{grid-template-columns:repeat(4,1fr)}}
.gc{background:var(--card);border:1px solid var(--brd);border-radius:var(--r);padding:12px 8px;display:flex;flex-direction:column;align-items:center;gap:4px}
.gc-lbl{font-size:10px;color:var(--sub);font-weight:600;text-align:center;letter-spacing:.2px}
.gc-sub{font-size:10px;color:var(--sub);text-align:center;min-height:13px}

/* Chart */
.cwrap{padding:0 16px 16px}
.chdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.tabs{display:flex;gap:5px}
.tab{background:var(--card);border:1px solid var(--brd);color:var(--sub);padding:5px 10px;border-radius:8px;font-size:12px;cursor:pointer;transition:.15s}
.tab.on{background:var(--acc);border-color:var(--acc);color:#000;font-weight:700}
canvas{border-radius:12px;background:var(--card);border:1px solid var(--brd);display:block}

/* News */
.nwrap{padding:0 16px 20px}
.ni{background:var(--card);border:1px solid var(--brd);border-radius:10px;padding:11px;margin-bottom:7px;display:flex;align-items:flex-start;gap:9px;text-decoration:none;color:var(--txt)}
.nd{width:7px;height:7px;border-radius:50%;flex-shrink:0;margin-top:5px}
.nd.positive{background:var(--buy)}.nd.negative{background:var(--sell)}.nd.neutral{background:var(--sub)}
.nt{font-size:12px;line-height:1.45}

/* Modal */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:200;display:flex;align-items:flex-end;justify-content:center;opacity:0;pointer-events:none;transition:.25s}
.modal-bg.show{opacity:1;pointer-events:all}
.modal{background:var(--s2);border:1px solid var(--brd);border-radius:20px 20px 0 0;padding:24px 20px 40px;width:100%;max-width:480px;transform:translateY(100%);transition:.3s cubic-bezier(.34,1.56,.64,1)}
.modal-bg.show .modal{transform:translateY(0)}
.modal-title{font-size:17px;font-weight:700;margin-bottom:6px}
.modal-sub{font-size:13px;color:var(--sub);margin-bottom:20px}
.modal-btns{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
.mbn{padding:18px;border-radius:14px;border:2px solid;font-size:15px;font-weight:800;cursor:pointer;letter-spacing:.5px;transition:.15s}
.mbn:active{opacity:.6}
.mbn.long{background:rgba(0,230,118,.1);border-color:var(--buy);color:var(--buy)}
.mbn.short{background:rgba(255,61,90,.1);border-color:var(--sell);color:var(--sell)}
.modal-cancel{width:100%;background:none;border:1px solid var(--brd);color:var(--sub);padding:12px;border-radius:10px;font-size:14px;cursor:pointer}

.footer{padding:14px;text-align:center;font-size:10px;color:var(--sub);border-top:1px solid var(--brd)}
</style>
</head>
<body>

<div class="hdr">
  <div class="logo">&#x1F994;<span class="logo-btc">BTC</span> Analiz</div>
  <div class="hdr-r">
    <div class="dot" id="dot"></div>
    <span class="upd-t" id="updt">--</span>
    <button class="notif-btn" id="nbtn" onclick="toggleNotifs()">&#128276;</button>
    <button class="rbtn" id="rbtn" onclick="loadAll()">&#8635;</button>
  </div>
</div>

<!-- Fiyat -->
<div class="hero">
  <div class="hero-p" id="hp">--</div>
  <div class="hero-c" id="hc">--</div>
  <div class="hero-m">
    <div class="hm"><div class="hm-v" id="hh">--</div><div class="hm-l">24h Yüksek</div></div>
    <div class="hm"><div class="hm-v" id="hl">--</div><div class="hm-l">24h Düşük</div></div>
    <div class="hm"><div class="hm-v" id="hv">--</div><div class="hm-l">Hacim ($M)</div></div>
    <div class="hm"><div class="hm-v" id="hdom">--%</div><div class="hm-l">BTC Dom.</div></div>
  </div>
</div>

<!-- Açık pozisyon -->
<div class="pos-card" id="pos-card">
  <div class="pos-top">
    <span class="pos-dir-badge" id="pos-dir-b">LONG</span>
    <button class="pos-close" onclick="closePos()">Pozisyonu Kapat</button>
  </div>
  <div class="pos-pnl" id="pos-pnl">--</div>
  <div class="pos-info" id="pos-info">--</div>
</div>

<!-- Sinyal -->
<div class="sig hold" id="sig">
  <div class="sig-a" id="sig-a">YÜKLENIYOR...</div>
  <div class="sig-r">
    <div class="sig-sc" id="sig-sc">--</div>
    <div class="sig-sl">Ort. Skor</div>
  </div>
</div>

<!-- Pozisyon Aç butonu -->
<button class="open-pos-btn" onclick="showPosModal()">&#128200; Pozisyon Aç</button>

<!-- Göstergeler -->
<div class="sec">GÖSTERGELER</div>
<div class="ggrid" id="ggrid"><div style="grid-column:1/-1;color:var(--sub);font-size:13px;padding:20px;text-align:center">Yükleniyor...</div></div>

<!-- Grafik -->
<div class="cwrap">
  <div class="chdr">
    <div class="sec" style="padding:0">GRAFİK</div>
    <div class="tabs">
      <button class="tab" onclick="sw('15m')" id="t15m">15d</button>
      <button class="tab on" onclick="sw('1h')" id="t1h">1s</button>
      <button class="tab" onclick="sw('4h')" id="t4h">4s</button>
      <button class="tab" onclick="sw('1d')" id="t1d">1G</button>
    </div>
  </div>
  <canvas id="cv"></canvas>
</div>

<!-- Haberler -->
<div class="sec">HABERLER</div>
<div class="nwrap" id="nwrap"><div style="color:var(--sub);font-size:13px">Yükleniyor...</div></div>

<div class="footer">Binance &bull; CoinGecko &bull; Alternative.me &bull; CryptoCompare &bull; Yatırım tavsiyesi değildir</div>

<!-- Pozisyon modal -->
<div class="modal-bg" id="pos-modal-bg" onclick="hidePosModal(event)">
  <div class="modal">
    <div class="modal-title">&#128200; Pozisyon Aç</div>
    <div class="modal-sub" id="modal-price">Mevcut fiyat: yükleniyor...</div>
    <div class="modal-btns">
      <button class="mbn long" onclick="openPos('long')">&#8679; LONG<br><small>Fiyat Artacak</small></button>
      <button class="mbn short" onclick="openPos('short')">&#8681; SHORT<br><small>Fiyat Düşecek</small></button>
    </div>
    <button class="modal-cancel" onclick="hidePosModal()">İptal</button>
  </div>
</div>

<script>
var curIv = '1h';
var chartData = [];
var curPrice = 0;
var notifOn = false;
var notifTimer = null;
var posTimer = null;
var SID = (localStorage.getItem('btc_sid') || ('s' + Math.random().toString(36).slice(2)));
localStorage.setItem('btc_sid', SID);

var MOTIV = [
  'Disiplin, strateji kadar önemli. Planına sadık kal.',
  'Piyasayı kontrol edemezsin, sadece kendinizi.',
  'Her düşüş bir gün görülecek fırsat olabilir.',
  'Korku ve açgözlülük indeksi düşükken akıllı para alır.',
  'Stop-loss koymayan tüccar değil, kumarbaz olur.',
  'Trend senin dostun, ona karşı savaşma.',
  'Risk yönetimi her şeyden önce gelir.',
];

function fP(n) {
  if (!n) return '--';
  return '$' + Number(n).toLocaleString('tr-TR', {minimumFractionDigits: 0, maximumFractionDigits: 0});
}
function fM(n) {
  if (!n) return '--';
  return (n / 1e6).toFixed(0) + 'M';
}
function nowT() {
  var d = new Date();
  return d.getHours().toString().padStart(2,'0') + ':' +
         d.getMinutes().toString().padStart(2,'0') + ':' +
         d.getSeconds().toString().padStart(2,'0');
}

// ── Bildirimler ─────────────────────────────────────────────────────────────
function toggleNotifs() {
  if (!('Notification' in window)) { alert('Bu tarayıcı bildirimleri desteklemiyor.'); return; }
  if (notifOn) {
    notifOn = false;
    if (notifTimer) { clearInterval(notifTimer); notifTimer = null; }
    document.getElementById('nbtn').className = 'notif-btn';
    document.getElementById('nbtn').textContent = '🔔';
    return;
  }
  Notification.requestPermission(function(perm) {
    if (perm === 'granted') {
      notifOn = true;
      document.getElementById('nbtn').className = 'notif-btn on';
      document.getElementById('nbtn').textContent = '🔔 Açık';
      sendNotif('BTC Analiz', 'Bildirimler aktif! Her 5 dakikada güncel sinyal gelecek.');
      startNotifTimer();
    } else {
      alert('Bildirim izni reddedildi. Tarayıcı ayarlarından izin ver.');
    }
  });
}

function startNotifTimer() {
  if (notifTimer) clearInterval(notifTimer);
  notifTimer = setInterval(function() {
    sendSignalNotif();
  }, 5 * 60 * 1000);
}

function sendNotif(title, body, tag) {
  if (Notification.permission !== 'granted') return;
  new Notification(title, {
    body: body,
    icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">&#8383;</text></svg>',
    tag: tag || 'btc',
    badge: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">&#8383;</text></svg>'
  });
}

function sendSignalNotif() {
  var xhr = new XMLHttpRequest();
  xhr.open('GET', '/api/signal', true);
  xhr.onreadystatechange = function() {
    if (xhr.readyState !== 4 || xhr.status !== 200) return;
    var res; try { res = JSON.parse(xhr.responseText); } catch(e) { return; }
    if (!res.ok) return;
    var motiv = MOTIV[Math.floor(Math.random() * MOTIV.length)];
    var sign = res.change >= 0 ? '+' : '';
    var msg = res.action + ' | ' + fP(res.price) + ' (' + sign + res.change.toFixed(2) + '%)';
    msg += '\nRSI: ' + (res.rsi_1h || '--') + ' | F&G: ' + res.fng;
    // Pozisyon varsa P&L ekle
    checkPosForNotif(function(posMsg) {
      if (posMsg) msg += '\n' + posMsg;
      msg += '\n\n' + motiv;
      sendNotif('₿ BTC Sinyal: ' + res.action, msg, 'btc-signal');
    });
  };
  xhr.send();
}

function checkPosForNotif(cb) {
  var xhr = new XMLHttpRequest();
  xhr.open('GET', '/api/position/status?sid=' + SID, true);
  xhr.onreadystatechange = function() {
    if (xhr.readyState !== 4 || xhr.status !== 200) { cb(null); return; }
    var res; try { res = JSON.parse(xhr.responseText); } catch(e) { cb(null); return; }
    if (!res.open) { cb(null); return; }
    var sign = res.pnl_pct >= 0 ? '+' : '';
    cb('Pozisyon ' + res.direction.toUpperCase() + ': ' + sign + res.pnl_pct.toFixed(2) + '% (' + res.elapsed_min + 'dk)');
  };
  xhr.send();
}

// ── Pozisyon ─────────────────────────────────────────────────────────────────
function showPosModal() {
  document.getElementById('modal-price').textContent = 'Mevcut fiyat: ' + fP(curPrice);
  document.getElementById('pos-modal-bg').classList.add('show');
}
function hidePosModal(e) {
  if (!e || e.target === document.getElementById('pos-modal-bg'))
    document.getElementById('pos-modal-bg').classList.remove('show');
}

function openPos(dir) {
  document.getElementById('pos-modal-bg').classList.remove('show');
  var body = JSON.stringify({sid: SID, direction: dir});
  var xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/position/open', true);
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.onreadystatechange = function() {
    if (xhr.readyState !== 4 || xhr.status !== 200) return;
    var res; try { res = JSON.parse(xhr.responseText); } catch(e) { return; }
    if (!res.ok) return;
    updatePosCard();
    startPosTimer();
    if (notifOn) {
      var dirTxt = dir === 'long' ? '↗️ LONG' : '↘️ SHORT';
      sendNotif('₿ Pozisyon Açıldı', dirTxt + ' @ ' + fP(res.entry), 'btc-pos');
    }
  };
  xhr.send(body);
}

function closePos() {
  var body = JSON.stringify({sid: SID});
  var xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/position/close', true);
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.onreadystatechange = function() {
    if (xhr.readyState !== 4 || xhr.status !== 200) return;
    var res; try { res = JSON.parse(xhr.responseText); } catch(e) { return; }
    if (posTimer) { clearInterval(posTimer); posTimer = null; }
    document.getElementById('pos-card').style.display = 'none';
    if (res.closed && notifOn) {
      var sign = res.pnl_pct >= 0 ? '+' : '';
      sendNotif('₿ Pozisyon Kapatıldı',
        res.direction.toUpperCase() + ' | ' + sign + res.pnl_pct.toFixed(2) + '% | ' + sign + res.pnl_usd.toFixed(0) + '$',
        'btc-pos-close');
    }
  };
  xhr.send(body);
}

function startPosTimer() {
  if (posTimer) clearInterval(posTimer);
  posTimer = setInterval(updatePosCard, 5000);
}

function updatePosCard() {
  var xhr = new XMLHttpRequest();
  xhr.open('GET', '/api/position/status?sid=' + SID, true);
  xhr.onreadystatechange = function() {
    if (xhr.readyState !== 4 || xhr.status !== 200) return;
    var res; try { res = JSON.parse(xhr.responseText); } catch(e) { return; }
    var card = document.getElementById('pos-card');
    if (!res.open) { card.style.display = 'none'; if (posTimer) { clearInterval(posTimer); posTimer = null; } return; }
    card.style.display = 'block';
    card.className = 'pos-card ' + res.direction + '-open';
    document.getElementById('pos-dir-b').textContent = res.direction.toUpperCase();
    document.getElementById('pos-dir-b').className = 'pos-dir-badge ' + res.direction;
    var sign = res.pnl_pct >= 0 ? '+' : '';
    var pnlEl = document.getElementById('pos-pnl');
    pnlEl.textContent = sign + res.pnl_pct.toFixed(2) + '%  (' + sign + res.pnl_usd.toFixed(0) + '$)';
    pnlEl.className = 'pos-pnl ' + (res.pnl_pct >= 0 ? 'pos' : 'neg');
    document.getElementById('pos-info').textContent =
      'Giriş: ' + fP(res.entry) + '  Şu an: ' + fP(res.current) + '  ' + res.elapsed_min + ' dakika önce';
  };
  xhr.send();
}

// ── Gauge SVG ─────────────────────────────────────────────────────────────────
function gColor(s) {
  return s > 20 ? '#00e676' : (s < -20 ? '#ff3d5a' : '#ffb300');
}
function gSVG(score, label) {
  var cx = 55, cy = 64, r = 48;
  function pt(deg) {
    var rad = deg * Math.PI / 180;
    return {x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad)};
  }
  var sp = pt(180), ep = pt(0);
  var bgArc = 'M' + sp.x + ' ' + sp.y + ' A' + r + ' ' + r + ' 0 0 1 ' + ep.x + ' ' + ep.y;
  var scoreDeg = 180 - (score + 100) / 200 * 180;
  var fp = pt(scoreDeg);
  var la = (Math.abs(180 - scoreDeg) > 180) ? 1 : 0;
  var fillArc = 'M' + sp.x + ' ' + sp.y + ' A' + r + ' ' + r + ' 0 ' + la + ' 1 ' + fp.x + ' ' + fp.y;
  var col = gColor(score);
  var disp = label !== undefined ? label : ((score > 0 ? '+' : '') + score);
  var s = '<svg viewBox="0 0 110 72" width="110" height="72" xmlns="http://www.w3.org/2000/svg">';
  s += '<path d="' + bgArc + '" fill="none" stroke="#222240" stroke-width="9" stroke-linecap="round"/>';
  s += '<path d="' + fillArc + '" fill="none" stroke="' + col + '" stroke-width="9" stroke-linecap="round"/>';
  s += '<circle cx="' + cx + '" cy="' + cy + '" r="3.5" fill="' + col + '"/>';
  s += '<text x="' + cx + '" y="' + (cy - 14) + '" text-anchor="middle" font-size="17" font-weight="800" fill="' + col + '" font-family="-apple-system,sans-serif">' + disp + '</text>';
  s += '<text x="9" y="' + (cy + 16) + '" font-size="9" fill="#6868a0" font-family="-apple-system,sans-serif">-</text>';
  s += '<text x="96" y="' + (cy + 16) + '" font-size="9" fill="#6868a0" font-family="-apple-system,sans-serif">+</text>';
  s += '</svg>';
  return s;
}

function renderGauges(gauges) {
  var grid = document.getElementById('ggrid');
  var html = '';
  for (var i = 0; i < gauges.length; i++) {
    var g = gauges[i];
    var disp = g.raw || ((g.score > 0 ? '+' : '') + g.score);
    var sub = g.raw ? g.sub : g.sub;
    if (g.delta !== null && g.delta !== undefined) {
      var ds = g.delta >= 0 ? '+' : '';
      sub = g.sub + ' (' + ds + g.delta + ')';
    }
    html += '<div class="gc">';
    html += '<div class="gc-lbl">' + g.label + '</div>';
    html += gSVG(g.score, disp);
    html += '<div class="gc-sub">' + sub + '</div>';
    html += '</div>';
  }
  grid.innerHTML = html;
}

// ── Dashboard yükle ────────────────────────────────────────────────────────
function loadDash() {
  document.getElementById('rbtn').classList.add('spin');
  var xhr = new XMLHttpRequest();
  xhr.open('GET', '/api/dashboard', true);
  xhr.timeout = 15000;
  xhr.onreadystatechange = function() {
    if (xhr.readyState !== 4) return;
    document.getElementById('rbtn').classList.remove('spin');
    if (xhr.status !== 200) {
      document.getElementById('dot').className = 'dot off';
      document.getElementById('sig-a').textContent = 'BAĞLANTI HATASI';
      return;
    }
    var res; try { res = JSON.parse(xhr.responseText); } catch(e) { return; }
    if (!res.ok) { document.getElementById('dot').className = 'dot off'; return; }
    document.getElementById('dot').className = 'dot';
    document.getElementById('updt').textContent = nowT();

    var p = res.price;
    curPrice = p.price;
    document.getElementById('hp').textContent = fP(p.price);
    var ce = document.getElementById('hc');
    ce.textContent = (p.change >= 0 ? '+' : '') + p.change.toFixed(2) + '%';
    ce.className = 'hero-c ' + (p.change >= 0 ? 'up' : 'dn');
    document.getElementById('hh').textContent = fP(p.high);
    document.getElementById('hl').textContent = fP(p.low);
    document.getElementById('hv').textContent = fM(p.vol_usdt);
    document.getElementById('hdom').textContent = (res.global ? res.global.btc_dom.toFixed(1) : '--') + '%';

    if (res.gauges) {
      var g = res.gauges;
      var sig = document.getElementById('sig');
      sig.className = 'sig ' + g.color;
      document.getElementById('sig-a').textContent = g.action;
      var ts = g.total_score;
      document.getElementById('sig-sc').textContent = (ts > 0 ? '+' : '') + ts;
      renderGauges(g.gauges);
    }

    if (res.klines_1h && res.klines_1h.length) {
      chartData = res.klines_1h;
      drawChart(chartData, '1h');
    }

    updatePosCard();
  };
  xhr.send();
}

// ── Haberler ─────────────────────────────────────────────────────────────────
function loadNews() {
  var xhr = new XMLHttpRequest();
  xhr.open('GET', '/api/news', true);
  xhr.timeout = 8000;
  xhr.onreadystatechange = function() {
    if (xhr.readyState !== 4 || xhr.status !== 200) return;
    var res; try { res = JSON.parse(xhr.responseText); } catch(e) { return; }
    if (!res.ok || !res.news.length) {
      document.getElementById('nwrap').innerHTML = '<div style="color:var(--sub);font-size:12px">Haber yüklenemedi</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < res.news.length; i++) {
      var n = res.news[i];
      html += '<a class="ni" href="' + n.url + '" target="_blank" rel="noopener">';
      html += '<div class="nd ' + n.sentiment + '"></div>';
      html += '<div class="nt">' + n.title + '</div>';
      html += '</a>';
    }
    document.getElementById('nwrap').innerHTML = html;
  };
  xhr.send();
}

// ── Grafik ────────────────────────────────────────────────────────────────────
function loadKlines(iv) {
  var xhr = new XMLHttpRequest();
  xhr.open('GET', '/api/klines/' + iv, true);
  xhr.onreadystatechange = function() {
    if (xhr.readyState !== 4 || xhr.status !== 200) return;
    var res; try { res = JSON.parse(xhr.responseText); } catch(e) { return; }
    if (!res.ok) return;
    chartData = res.data;
    drawChart(chartData, iv);
  };
  xhr.send();
}

function sw(iv) {
  curIv = iv;
  ['15m','1h','4h','1d'].forEach(function(t) {
    var el = document.getElementById('t' + t);
    if (el) el.className = 'tab' + (t === iv ? ' on' : '');
  });
  loadKlines(iv);
}

function drawChart(klines, iv) {
  var cv = document.getElementById('cv');
  var W = (cv.parentElement.clientWidth - 32) || 320;
  cv.width = W; cv.height = 200;
  var ctx = cv.getContext('2d');
  var H = 200, pad = {t:10, r:6, b:22, l:58};
  var dw = W-pad.l-pad.r, dh = H-pad.t-pad.b;
  var data = klines.slice(-60);
  if (!data.length) return;
  var hi = Math.max.apply(null, data.map(function(k){return k.h;}));
  var lo = Math.min.apply(null, data.map(function(k){return k.l;}));
  var rng = hi - lo || 1;
  function toY(p){return pad.t + dh - (p-lo)/rng*dh;}
  function toX(i){return pad.l + i*(dw/data.length) + dw/data.length*0.5;}
  ctx.strokeStyle='rgba(255,255,255,0.04)'; ctx.lineWidth=1;
  for (var r=0; r<=4; r++) {
    var y=pad.t+dh*r/4;
    ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(W-pad.r,y); ctx.stroke();
    var pv=hi-rng*r/4;
    ctx.fillStyle='#6868a0'; ctx.font='9px -apple-system,sans-serif'; ctx.textAlign='right';
    ctx.fillText(pv.toLocaleString('tr-TR',{maximumFractionDigits:0}), pad.l-3, y+4);
  }
  var cw = Math.max(dw/data.length*0.72, 1.5);
  for (var i=0; i<data.length; i++) {
    var k=data[i], x=toX(i), bull=k.c>=k.o;
    ctx.strokeStyle=bull?'#00e676':'#ff3d5a';
    ctx.fillStyle=bull?'rgba(0,230,118,.78)':'rgba(255,61,90,.78)';
    ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(x,toY(k.h)); ctx.lineTo(x,toY(k.l)); ctx.stroke();
    var yo=toY(k.o), yc=toY(k.c);
    ctx.fillRect(x-cw/2, Math.min(yo,yc), cw, Math.abs(yc-yo)||1);
  }
  ctx.fillStyle='#6868a0'; ctx.font='8px -apple-system,sans-serif'; ctx.textAlign='center';
  var step=Math.floor(data.length/5)||1;
  for (var j=0; j<data.length; j+=step) {
    var d=new Date(data[j].t);
    var lbl = (iv==='1d') ? (d.getMonth()+1)+'/'+d.getDate()
              : d.getHours().toString().padStart(2,'0')+':'+d.getMinutes().toString().padStart(2,'0');
    ctx.fillText(lbl, toX(j), H-5);
  }
}

window.onresize=function(){if(chartData.length)drawChart(chartData,curIv);};

function loadAll() {
  loadDash();
  loadNews();
}

// Açık pozisyon varsa timer başlat
updatePosCard();
startPosTimer();
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
