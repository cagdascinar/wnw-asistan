#!/usr/bin/env python3
import os, json, re, base64
from datetime import datetime
from flask import Flask, request, jsonify, Response
import requests

app = Flask(__name__)

MONDAY_TOKEN  = os.environ.get("MONDAY_TOKEN", "eyJhbGciOiJIUzI1NiJ9.eyJ0aWQiOjUzOTI2MzE2OCwiYWFpIjoxMSwidWlkIjo3MjA0MDcwNywiaWFkIjoiMjAyNS0wNy0xNVQxMzoyNjoyNy4wMDBaIiwicGVyIjoibWU6d3JpdGUiLCJhY3RpZCI6MjYwMDk4MjksInJnbiI6ImV1YzEifQ.GZDrCbzf4GhZ12Bqur3xPIbvH3n8_pEfGFWiEnfrb00")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")   # Opsiyonel: sunucu taraflı key
APP_PASSWORD  = os.environ.get("APP_PASSWORD", "")         # Opsiyonel: şifre koruması
MONDAY_API    = "https://api.monday.com/v2"
CLAUDE_API    = "https://api.anthropic.com/v1/messages"

boards_cache = {}

# ── MONDAY ─────────────────────────────────────────────────────────────────
def monday_gql(query):
    r = requests.post(MONDAY_API,
        headers={"Content-Type":"application/json","Authorization":MONDAY_TOKEN},
        json={"query": query}, timeout=30)
    data = r.json()
    if "errors" in data:
        raise Exception(data["errors"][0]["message"])
    return data["data"]

def load_all_boards():
    global boards_cache
    boards_cache = {}
    page = 1
    while True:
        data = monday_gql(f"""{{
            boards(limit:100, page:{page}) {{
                id name state
                columns {{ id title type }}
                groups {{ id title color }}
            }}
        }}""")
        batch = [b for b in data["boards"] if b["state"] == "active"]
        for b in batch:
            boards_cache[b["id"]] = {"name":b["name"],"columns":b["columns"],"groups":b["groups"]}
        if len(data["boards"]) < 100:
            break
        page += 1
    return len(boards_cache)

def find_board(name_q):
    q = name_q.lower().strip()
    for bid,b in boards_cache.items():
        if b["name"].lower() == q: return bid, b
    matches = [(bid,b) for bid,b in boards_cache.items() if q in b["name"].lower()]
    if matches:
        matches.sort(key=lambda x: len(x[1]["name"]))
        return matches[0]
    words = q.split()
    best, best_score = None, 0
    for bid,b in boards_cache.items():
        score = sum(1 for w in words if w in b["name"].lower())
        if score > best_score: best_score, best = score, (bid,b)
    return best if best_score else None

def fetch_items(board_id, cursor=None, acc=None):
    if acc is None: acc = []
    cur = f', cursor:"{cursor}"' if cursor else ""
    data = monday_gql(f"""{{
        boards(ids:["{board_id}"]) {{
            items_page(limit:200{cur}) {{
                cursor
                items {{ id name group{{id title}} column_values{{id text value}} }}
            }}
        }}
    }}""")
    page = data["boards"][0]["items_page"]
    acc.extend(page["items"])
    if page["cursor"]: return fetch_items(board_id, page["cursor"], acc)
    return acc

def get_cv(item, col_id):
    for cv in item["column_values"]:
        if cv["id"] == col_id: return cv["text"] or ""
    return ""

def parse_date(text):
    if not text: return None
    try: return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except: return None

def month_tr(n):
    return ["","Ocak","Şubat","Mart","Nisan","Mayıs","Haziran",
            "Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"][int(n)] if n else ""

# ── TOOLS ──────────────────────────────────────────────────────────────────
def tool_list_boards(query=""):
    q = query.lower()
    results = [{"id":bid,"name":b["name"]} for bid,b in boards_cache.items()
               if not q or q in b["name"].lower()]
    results.sort(key=lambda x: x["name"])
    return results[:40]

def tool_query_board(board_name, year=None, month=None, group_name=None, metric="sum"):
    found = find_board(board_name)
    if not found:
        return {"error": f"'{board_name}' adında pano bulunamadı. list_boards ile kontrol et."}
    board_id, board_info = found

    items = fetch_items(board_id)

    date_col = None
    for c in board_info["columns"]:
        if c["type"] == "date" and any(k in c["title"].lower() for k in ["tarih","date"]):
            date_col = c; break
    if not date_col:
        for c in board_info["columns"]:
            if c["type"] == "date": date_col = c; break

    num_cols = []
    num_kw = ["tutar","matrah","toplam","fiyat","bedel","gelir","gider","miktar","ücret"]
    for c in board_info["columns"]:
        if c["type"] == "numbers" and any(k in c["title"].lower() for k in num_kw):
            num_cols.append(c)
    if not num_cols:
        num_cols = [c for c in board_info["columns"] if c["type"] == "numbers"]

    filtered = []
    for item in items:
        if group_name:
            grp = item.get("group",{}).get("title","").lower()
            if group_name.lower() not in grp: continue
        if (year or month) and date_col:
            d = parse_date(get_cv(item, date_col["id"]))
            if d is None: continue
            if year and d.year != int(year): continue
            if month and d.month != int(month): continue
        filtered.append(item)

    result = {
        "board_name": board_info["name"],
        "total_items": len(filtered),
        "period": f"{year or ''} {month_tr(month)}".strip() or "Tüm zamanlar",
        "group_filter": group_name or "Tümü",
    }

    if metric == "count":
        result["count"] = len(filtered)
    else:
        sums = {}
        for col in num_cols:
            total = 0
            for item in filtered:
                try: total += float(get_cv(item, col["id"]).replace(",",".") or "0")
                except: pass
            if total != 0: sums[col["title"]] = round(total, 2)
        result["amounts"] = sums
        result["grand_total"] = round(sum(sums.values()), 2)

    result["sample_items"] = [
        {"name":i["name"],"group":i.get("group",{}).get("title","")}
        for i in filtered[:5]
    ]
    return result

def tool_get_groups(board_name):
    found = find_board(board_name)
    if not found: return {"error":"Pano bulunamadı"}
    bid, b = found
    return {"board_name":b["name"],"groups":b["groups"]}

def tool_get_columns(board_name):
    found = find_board(board_name)
    if not found: return {"error":"Pano bulunamadı"}
    bid, b = found
    skip = ("subtasks","mirror","board_relation","file")
    return {"board_name":b["name"],"columns":[{"id":c["id"],"title":c["title"],"type":c["type"]}
            for c in b["columns"] if c["type"] not in skip]}

TOOLS = [
    {"name":"list_boards","description":"Monday.com'daki panoları listeler. Hangi panonun var olduğunu anlamak için kullan.",
     "input_schema":{"type":"object","properties":{"query":{"type":"string","description":"İsteğe bağlı filtre"}}}},
    {"name":"query_board","description":"Bir panodan veri sorgular: tutarlar, sayılar, tarih/grup filtresi.",
     "input_schema":{"type":"object","required":["board_name"],"properties":{
         "board_name":{"type":"string"},"year":{"type":"integer"},"month":{"type":"integer"},
         "group_name":{"type":"string"},"metric":{"type":"string","enum":["sum","count"]}}}},
    {"name":"get_board_groups","description":"Panonun gruplarını listeler.",
     "input_schema":{"type":"object","required":["board_name"],"properties":{"board_name":{"type":"string"}}}},
    {"name":"get_board_columns","description":"Panonun sütun yapısını döndürür.",
     "input_schema":{"type":"object","required":["board_name"],"properties":{"board_name":{"type":"string"}}}},
]

SYSTEM = """Sen WorknWerk yaratıcı ajansının akıllı finans asistanısın. Monday.com verilerine erişerek Türkçe soruları yanıtlıyorsun.

KURALLAR:
- Cevapları her zaman Türkçe ver
- Para birimini ₺ ile göster, binlik ayraç kullan (125.430 ₺)
- Hangi panodan veri çektiğini belirt
- Belirsiz sorularda önce list_boards ile panoları bul

ŞİRKET BAĞLAMI (WorknWerk):
- E-Faturalar = müşterilere kesilen faturalar → GELİR
- E-Arşiv Faturalar = tedarikçiden gelen faturalar → GİDER
- Masraf & Masraf Giriş Formu = personel masrafları → GİDER
- Genel Giderler = işletme giderleri → GİDER
- Karlılık = Gelir Matrahı - Gider Matrahı"""

# ── ROUTES ──────────────────────────────────────────────────────────────────
@app.route("/api/chat", methods=["POST"])
def chat():
    body      = request.json
    password  = body.get("password","")
    user_key  = body.get("api_key","")
    messages  = body.get("messages",[])

    if APP_PASSWORD and password != APP_PASSWORD:
        return jsonify({"error":"Şifre hatalı"}), 401

    key = ANTHROPIC_KEY or user_key
    if not key:
        return jsonify({"error":"Anthropic API key gerekli"}), 400

    for _ in range(8):
        resp = requests.post(CLAUDE_API,
            headers={"x-api-key":key,"anthropic-version":"2023-06-01","content-type":"application/json"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":2048,
                  "system":SYSTEM,"tools":TOOLS,"messages":messages},
            timeout=60).json()

        if "error" in resp:
            return jsonify({"error":resp["error"].get("message","API hatası")}), 500

        content     = resp.get("content",[])
        stop_reason = resp.get("stop_reason","")
        messages.append({"role":"assistant","content":content})

        if stop_reason == "end_turn":
            text = " ".join(c["text"] for c in content if c.get("type")=="text")
            return jsonify({"reply":text,"messages":messages})

        if stop_reason == "tool_use":
            results = []
            for blk in content:
                if blk.get("type") != "tool_use": continue
                name, inp, tid = blk["name"], blk["input"], blk["id"]
                try:
                    if   name == "list_boards":      r = tool_list_boards(inp.get("query",""))
                    elif name == "query_board":      r = tool_query_board(inp["board_name"],inp.get("year"),inp.get("month"),inp.get("group_name"),inp.get("metric","sum"))
                    elif name == "get_board_groups":  r = tool_get_groups(inp["board_name"])
                    elif name == "get_board_columns": r = tool_get_columns(inp["board_name"])
                    else: r = {"error":f"Bilinmeyen: {name}"}
                except Exception as e: r = {"error":str(e)}
                results.append({"type":"tool_result","tool_use_id":tid,"content":json.dumps(r,ensure_ascii=False)})
            messages.append({"role":"user","content":results})
        else:
            text = " ".join(c.get("text","") for c in content if c.get("type")=="text")
            return jsonify({"reply":text or "Hata oluştu.","messages":messages})

    return jsonify({"reply":"Çok karmaşık sorgu, daha basit sorun.","messages":messages})

@app.route("/api/status")
def status():
    return jsonify({
        "boards": len(boards_cache),
        "server_key": bool(ANTHROPIC_KEY),
        "password_required": bool(APP_PASSWORD)
    })

@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "WorknWerk Asistan",
        "short_name": "WW Asistan",
        "description": "Monday.com Finans Asistanı",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f0f17",
        "theme_color": "#6366f1",
        "orientation": "portrait",
        "icons": [
            {"src":"/icon/192","sizes":"192x192","type":"image/png"},
            {"src":"/icon/512","sizes":"512x512","type":"image/png"}
        ]
    })

@app.route("/icon/<int:size>")
def icon(size):
    # SVG icon rendered as PNG-compatible SVG
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">
      <rect width="{size}" height="{size}" rx="{size//5}" fill="#6366f1"/>
      <text x="50%" y="54%" font-size="{size//2}" text-anchor="middle" dominant-baseline="middle" fill="white">⚡</text>
    </svg>'''
    return Response(svg, mimetype="image/svg+xml")

@app.route("/sw.js")
def service_worker():
    sw = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => e.respondWith(fetch(e.request).catch(() => caches.match(e.request))));
"""
    return Response(sw, mimetype="application/javascript")

@app.route("/")
def index():
    return MOBILE_HTML

# ── HTML ───────────────────────────────────────────────────────────────────
MOBILE_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
<meta name="apple-mobile-web-app-title" content="WW Asistan"/>
<meta name="mobile-web-app-capable" content="yes"/>
<meta name="theme-color" content="#6366f1"/>
<link rel="manifest" href="/manifest.json"/>
<link rel="apple-touch-icon" href="/icon/192"/>
<title>WorknWerk Asistan</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f0f17;--surface:#1a1a28;--card:#22223a;--border:rgba(255,255,255,0.08);
  --text:#e8e8f5;--muted:rgba(255,255,255,0.45);
  --accent:#6366f1;--accent2:#818cf8;--green:#10b981;--user-bubble:#4f46e5;
  --safe-top:env(safe-area-inset-top);--safe-bot:env(safe-area-inset-bottom);
}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;-webkit-tap-highlight-color:transparent}

/* SETUP */
#setup{position:fixed;inset:0;background:var(--bg);display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;z-index:100;padding-top:calc(24px + var(--safe-top));padding-bottom:calc(24px + var(--safe-bot))}
.setup-logo{font-size:52px;margin-bottom:10px}
.setup-title{font-size:24px;font-weight:800;background:linear-gradient(135deg,#6366f1,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:6px}
.setup-sub{font-size:13px;color:var(--muted);margin-bottom:28px;text-align:center;line-height:1.6;max-width:300px}
.setup-card{width:100%;max-width:380px;background:var(--surface);border-radius:22px;border:1px solid var(--border);padding:24px;box-shadow:0 20px 60px rgba(0,0,0,0.4)}
.setup-label{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-bottom:7px;display:block}
.setup-input{width:100%;padding:13px 14px;border-radius:12px;border:1px solid var(--border);background:rgba(255,255,255,0.05);color:var(--text);font-size:15px;outline:none;margin-bottom:14px;-webkit-appearance:none}
.setup-input:focus{border-color:var(--accent)}
.setup-btn{width:100%;padding:15px;border-radius:13px;border:none;background:linear-gradient(135deg,#6366f1,#818cf8);color:#fff;font-size:16px;font-weight:700;cursor:pointer;-webkit-appearance:none;transition:opacity .2s}
.setup-btn:active{opacity:.85}
.setup-hint{font-size:11px;color:rgba(255,255,255,0.22);text-align:center;margin-top:14px;line-height:1.7}
.setup-hint a{color:var(--accent2);text-decoration:none}

/* iOS INSTALL BANNER */
.install-banner{display:none;background:linear-gradient(135deg,rgba(99,102,241,.15),rgba(167,139,250,.1));border:1px solid rgba(99,102,241,.25);border-radius:14px;padding:13px 16px;margin-bottom:20px;width:100%;max-width:380px}
.install-banner.show{display:block}
.install-title{font-size:13px;font-weight:700;color:var(--accent2);margin-bottom:4px}
.install-steps{font-size:12px;color:var(--muted);line-height:1.8}

/* CHAT */
#chat-screen{position:fixed;inset:0;display:none;flex-direction:column}

.chat-header{display:flex;align-items:center;gap:10px;padding:12px 16px;padding-top:calc(12px + var(--safe-top));background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0}
.chat-avatar{width:38px;height:38px;border-radius:11px;background:linear-gradient(135deg,#6366f1,#a78bfa);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
.chat-info{flex:1}
.chat-name{font-size:15px;font-weight:700}
.chat-status{font-size:11px;color:var(--green);display:flex;align-items:center;gap:4px;margin-top:1px}
.online-dot{width:6px;height:6px;border-radius:50%;background:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.hbtn{width:34px;height:34px;border-radius:9px;border:none;background:rgba(255,255,255,0.07);color:var(--muted);cursor:pointer;font-size:15px;display:flex;align-items:center;justify-content:center;-webkit-appearance:none}

.messages{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px;-webkit-overflow-scrolling:touch}
.messages::-webkit-scrollbar{display:none}

.msg-row{display:flex;align-items:flex-end;gap:7px}
.msg-row.user{flex-direction:row-reverse}
.mavatar{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,#6366f1,#a78bfa);display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0}
.mavatar.u{background:linear-gradient(135deg,#4338ca,#6366f1)}
.bubble{max-width:80%;padding:11px 14px;border-radius:18px;font-size:14px;line-height:1.55;word-break:break-word}
.bubble.bot{background:var(--card);border-bottom-left-radius:4px}
.bubble.user{background:var(--user-bubble);border-bottom-right-radius:4px;color:#fff}
.btime{font-size:10px;color:rgba(255,255,255,0.25);margin-top:5px;text-align:right}

.typing{background:var(--card);border-radius:18px;border-bottom-left-radius:4px;padding:13px 16px;display:flex;gap:4px;align-items:center}
.tdot{width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,0.35);animation:bop .9s infinite}
.tdot:nth-child(2){animation-delay:.15s}
.tdot:nth-child(3){animation-delay:.3s}
@keyframes bop{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}

.suggestions{padding:8px 12px 4px;display:flex;gap:7px;overflow-x:auto;flex-shrink:0;-webkit-overflow-scrolling:touch}
.suggestions::-webkit-scrollbar{display:none}
.chip{white-space:nowrap;padding:7px 13px;border-radius:20px;border:1px solid var(--border);background:rgba(255,255,255,0.04);color:rgba(255,255,255,0.65);font-size:12px;cursor:pointer;-webkit-appearance:none;flex-shrink:0}
.chip:active{background:rgba(99,102,241,.2);border-color:var(--accent);color:var(--accent2)}

.input-bar{padding:10px 12px;padding-bottom:calc(10px + var(--safe-bot));background:var(--surface);border-top:1px solid var(--border);display:flex;gap:8px;align-items:flex-end;flex-shrink:0}
.msg-input{flex:1;background:rgba(255,255,255,0.06);border:1px solid var(--border);border-radius:22px;padding:11px 16px;color:var(--text);font-size:15px;outline:none;resize:none;max-height:100px;line-height:1.4;font-family:inherit;-webkit-appearance:none}
.msg-input::placeholder{color:var(--muted)}
.msg-input:focus{border-color:rgba(99,102,241,.5)}
.send-btn{width:44px;height:44px;border-radius:50%;border:none;background:linear-gradient(135deg,#6366f1,#818cf8);color:#fff;font-size:20px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;-webkit-appearance:none}
.send-btn:active{transform:scale(.9)}
.send-btn:disabled{opacity:.35}

/* PASSWORD MODAL */
#pwModal{position:fixed;inset:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:200;padding:24px;backdrop-filter:blur(8px)}
#pwModal.show{display:flex}
.pw-card{background:var(--surface);border-radius:20px;border:1px solid var(--border);padding:28px;width:100%;max-width:320px}
.pw-title{font-size:17px;font-weight:700;margin-bottom:4px}
.pw-sub{font-size:13px;color:var(--muted);margin-bottom:20px}

.toast{position:fixed;bottom:calc(80px + var(--safe-bot));left:50%;transform:translateX(-50%) translateY(10px);background:#1e1e35;color:var(--text);padding:9px 18px;border-radius:10px;font-size:13px;border:1px solid rgba(255,255,255,.1);z-index:999;opacity:0;transition:all .3s;pointer-events:none;white-space:nowrap}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

.welcome{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;padding:32px;text-align:center;gap:8px;pointer-events:none}
.w-emoji{font-size:48px}
.w-title{font-size:18px;font-weight:700}
.w-sub{font-size:13px;color:var(--muted);line-height:1.6;max-width:260px}
</style>
</head>
<body>

<!-- SETUP -->
<div id="setup">
  <div class="setup-logo">⚡</div>
  <div class="setup-title">WW Asistan</div>
  <div class="setup-sub">Monday.com verilerinizi Türkçe soru sorarak sorgulayın</div>

  <div class="install-banner" id="installBanner">
    <div class="install-title">📲 Uygulamayı Yükle (iOS)</div>
    <div class="install-steps">
      1. Alt çubukta <b>Paylaş</b> butonuna dokun<br>
      2. <b>"Ana Ekrana Ekle"</b> seçeneğini seç<br>
      3. Uygulama gibi açılır ✓
    </div>
  </div>

  <div class="setup-card" id="setupCard">
    <div id="apiKeySection">
      <label class="setup-label">Anthropic API Key <span style="color:rgba(255,255,255,.2)">(gerekmiyorsa boş bırak)</span></label>
      <input class="setup-input" id="apiKeyInput" type="password" placeholder="sk-ant-api03-..." autocomplete="off"/>
    </div>
    <div id="passwordSection" style="display:none">
      <label class="setup-label">Uygulama Şifresi</label>
      <input class="setup-input" id="pwInput" type="password" placeholder="Şifrenizi girin"/>
    </div>
    <button class="setup-btn" onclick="startChat()">Başla →</button>
    <div class="setup-hint">
      API key için <a href="https://console.anthropic.com" target="_blank">console.anthropic.com</a><br>
      Bilgileriniz yalnızca bu cihazda saklanır.
    </div>
  </div>
</div>

<!-- CHAT -->
<div id="chat-screen">
  <div class="chat-header">
    <div class="chat-avatar">⚡</div>
    <div class="chat-info">
      <div class="chat-name">WW Asistan</div>
      <div class="chat-status"><div class="online-dot"></div><span id="boardCount">Bağlanıyor...</span></div>
    </div>
    <button class="hbtn" onclick="clearChat()">🗑</button>
    <button class="hbtn" onclick="resetKey()">⚙️</button>
  </div>

  <div class="messages" id="messages">
    <div class="welcome" id="welcome">
      <div class="w-emoji">👋</div>
      <div class="w-title">Merhaba!</div>
      <div class="w-sub">Monday.com'daki verileriniz hakkında Türkçe soru sorabilirsiniz.</div>
    </div>
  </div>

  <div class="suggestions" id="suggestions">
    <div class="chip" onclick="sendSug(this)">Nisan 2026 E-Arşiv tutarı nedir?</div>
    <div class="chip" onclick="sendSug(this)">Bu yıl toplam E-Fatura matrahı?</div>
    <div class="chip" onclick="sendSug(this)">Genel Gider 2026'da ne kadar harcandı?</div>
    <div class="chip" onclick="sendSug(this)">Masraf formunda kaç kayıt var?</div>
    <div class="chip" onclick="sendSug(this)">Hangi panolar mevcut?</div>
  </div>

  <div class="input-bar">
    <textarea class="msg-input" id="msgInput" placeholder="Soru sorun..." rows="1"
      onkeydown="handleKey(event)" oninput="autoResize(this)"></textarea>
    <button class="send-btn" id="sendBtn" onclick="sendMessage()">↑</button>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const BASE = window.location.origin;
let apiKey   = localStorage.getItem('ww_key') || '';
let password = localStorage.getItem('ww_pw')  || '';
let messages = [];
let loading  = false;
let serverKey = false;
let pwRequired = false;

// iOS install banner
const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
const isStandalone = window.navigator.standalone;
if (isIOS && !isStandalone) {
  document.getElementById('installBanner').classList.add('show');
}

// Check server status first
async function checkStatus() {
  try {
    const r = await fetch(BASE + '/api/status');
    const d = await r.json();
    serverKey  = d.server_key;
    pwRequired = d.password_required;
    return d;
  } catch { return null; }
}

async function init() {
  const st = await checkStatus();
  if (!st) { document.getElementById('setup').style.display = 'flex'; return; }

  if (st.server_key) {
    // Sunucuda key var, setup gerekmez
    if (!st.password_required) {
      showChat(st.boards); return;
    }
    // Şifre gerekiyor ama daha önce kaydedilmiş
    if (password) { showChat(st.boards); return; }
    document.getElementById('apiKeySection').style.display = 'none';
    document.getElementById('passwordSection').style.display = 'block';
  } else {
    // Kullanıcının key'i var mı?
    if (apiKey) {
      if (!st.password_required || password) { showChat(st.boards); return; }
    }
    if (st.password_required) document.getElementById('passwordSection').style.display = 'block';
  }
  document.getElementById('setup').style.display = 'flex';
}

async function startChat() {
  const st = await checkStatus();
  if (!st) { toast('Sunucuya bağlanılamıyor'); return; }

  if (st.server_key) {
    if (st.password_required) {
      const p = document.getElementById('pwInput').value.trim();
      if (!p) { toast('Şifre girin'); return; }
      password = p; localStorage.setItem('ww_pw', p);
    }
    showChat(st.boards); return;
  }

  const k = document.getElementById('apiKeyInput').value.trim();
  if (!k) { toast('API key girin'); return; }
  if (!k.startsWith('sk-ant')) { toast('Geçerli API key girin (sk-ant-...)'); return; }
  apiKey = k; localStorage.setItem('ww_key', k);

  if (st.password_required) {
    const p = document.getElementById('pwInput').value.trim();
    if (!p) { toast('Şifre girin'); return; }
    password = p; localStorage.setItem('ww_pw', p);
  }
  showChat(st.boards);
}

function showChat(boardCount) {
  document.getElementById('setup').style.display = 'none';
  const cs = document.getElementById('chat-screen');
  cs.style.display = 'flex';
  document.getElementById('boardCount').textContent = (boardCount||'?') + ' pano yüklü';
}

function resetKey() {
  localStorage.removeItem('ww_key');
  localStorage.removeItem('ww_pw');
  apiKey = ''; password = ''; messages = [];
  document.getElementById('chat-screen').style.display = 'none';
  document.getElementById('setup').style.display = 'flex';
  clearChat();
}

function clearChat() {
  messages = [];
  document.getElementById('messages').innerHTML =
    '<div class="welcome" id="welcome"><div class="w-emoji">👋</div><div class="w-title">Merhaba!</div><div class="w-sub">Monday.com\'daki verileriniz hakkında Türkçe soru sorabilirsiniz.</div></div>';
  document.getElementById('suggestions').style.display = 'flex';
}

// ── SEND ──────────────────────────────────────────────────────────────────
function sendSug(el) { document.getElementById('msgInput').value = el.textContent; sendMessage(); }
function handleKey(e) { if (e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage();} }
function autoResize(el) { el.style.height='auto'; el.style.height=Math.min(el.scrollHeight,100)+'px'; }

async function sendMessage() {
  const input = document.getElementById('msgInput');
  const text  = input.value.trim();
  if (!text || loading) return;

  const w = document.getElementById('welcome');
  if (w) w.remove();
  document.getElementById('suggestions').style.display = 'none';

  addBubble('user', text);
  input.value = ''; input.style.height = 'auto';
  messages.push({role:'user', content:text});

  const tid = addTyping();
  loading = true;
  document.getElementById('sendBtn').disabled = true;

  try {
    const r = await fetch(BASE + '/api/chat', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({api_key:apiKey, password:password, messages:[...messages]})
    });
    const data = await r.json();
    removeTyping(tid);
    if (data.error) {
      addBubble('bot', '❌ ' + data.error);
      if (data.error.includes('Şifre')) { localStorage.removeItem('ww_pw'); password=''; }
    } else {
      addBubble('bot', data.reply);
      messages = data.messages;
    }
  } catch(e) {
    removeTyping(tid);
    addBubble('bot', '❌ Sunucuya ulaşılamıyor.');
  } finally {
    loading = false;
    document.getElementById('sendBtn').disabled = false;
  }
}

// ── UI ─────────────────────────────────────────────────────────────────────
function addBubble(role, text) {
  const msgs = document.getElementById('messages');
  const row  = document.createElement('div');
  row.className = 'msg-row ' + (role==='user'?'user':'');
  const av = document.createElement('div');
  av.className = 'mavatar ' + (role==='user'?'u':'');
  av.textContent = role==='user'?'👤':'⚡';
  const b = document.createElement('div');
  b.className = 'bubble ' + role;
  b.innerHTML = fmt(text);
  const t = document.createElement('div');
  t.className = 'btime';
  t.textContent = new Date().toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit'});
  b.appendChild(t);
  row.appendChild(av); row.appendChild(b);
  msgs.appendChild(row);
  msgs.scrollTop = msgs.scrollHeight;
}

function fmt(text) {
  return text
    .replace(/[*][*](.*?)[*][*]/g,'<b>$1</b>')
    .replace(/[*](.*?)[*]/g,'<em>$1</em>')
    .replace(/`(.*?)`/g,'<code style="background:rgba(255,255,255,.1);padding:1px 5px;border-radius:4px;font-size:12px">$1</code>')
    .replace(/\n/g,'<br>');
}

function addTyping() {
  const msgs = document.getElementById('messages');
  const row = document.createElement('div');
  row.className = 'msg-row';
  const id = 'ty' + Date.now(); row.id = id;
  row.innerHTML = '<div class="mavatar">⚡</div><div class="typing"><div class="tdot"></div><div class="tdot"></div><div class="tdot"></div></div>';
  msgs.appendChild(row); msgs.scrollTop = msgs.scrollHeight;
  return id;
}
function removeTyping(id) { const e=document.getElementById(id); if(e)e.remove(); }

let _tt;
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.classList.add('show');
  clearTimeout(_tt); _tt = setTimeout(()=>el.classList.remove('show'),3000);
}

// ── SERVICE WORKER ─────────────────────────────────────────────────────────
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(()=>{});
}

init();
</script>
</body>
</html>"""

# Gunicorn veya direkt çalıştırmada panoları yükle
import threading
def _load():
    print("Monday.com panoları yükleniyor...")
    count = load_all_boards()
    print(f"✓ {count} pano yüklendi.")
threading.Thread(target=_load, daemon=True).start()

if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 5050))
    print(f"► http://localhost:{port}")
    app.run(host=host, port=port, debug=False)
