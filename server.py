#!/usr/bin/env python3
"""WorknWerk Asistan – Monday.com Türkçe Sorgu Motoru"""
import os, re, time, threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, Response
import requests

app = Flask(__name__)

MONDAY_TOKEN = os.environ.get("MONDAY_TOKEN",
    "eyJhbGciOiJIUzI1NiJ9.eyJ0aWQiOjUzOTI2MzE2OCwiYWFpIjoxMSwidWlkIjo3MjA0MDcwNywiaWFkIjoiMjAyNS0wNy0xNVQxMzoyNjoyNy4wMDBaIiwicGVyIjoibWU6d3JpdGUiLCJhY3RpZCI6MjYwMDk4MjksInJnbiI6ImV1YzEifQ.GZDrCbzf4GhZ12Bqur3xPIbvH3n8_pEfGFWiEnfrb00")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
MONDAY_API   = "https://api.monday.com/v2"

boards_cache = {}
_lock        = threading.Lock()
last_refresh = 0.0
_loading     = False
_items_cache     = {}      # bid -> (timestamp, items)
_items_cache_ttl = 300     # 5 dakika

# ── MONDAY API ───────────────────────────────────────────────────────────────

def gql(query, timeout=30):
    r = requests.post(MONDAY_API,
        headers={"Content-Type":"application/json","Authorization":MONDAY_TOKEN},
        json={"query": query}, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if "errors" in d:
        raise Exception(d["errors"][0]["message"])
    return d["data"]

def _load_boards():
    global last_refresh, _loading
    _loading = True
    tmp, page = {}, 1
    try:
        while True:
            d = gql(f"""{{boards(limit:100,page:{page}){{
                id name state columns{{id title type}} groups{{id title}}
            }}}}""", timeout=60)
            for b in d["boards"]:
                if b["state"] == "active":
                    tmp[b["id"]] = {"name":b["name"],"columns":b["columns"],"groups":b["groups"]}
            if len(d["boards"]) < 100:
                break
            page += 1
        with _lock:
            boards_cache.clear()
            boards_cache.update(tmp)
        last_refresh = time.time()
        print(f"✓ {len(boards_cache)} pano yüklendi")
    except Exception as e:
        print(f"✗ Board load error: {e}")
    finally:
        _loading = False

def maybe_reload():
    global _loading
    if not _loading and time.time() - last_refresh > 1800:
        threading.Thread(target=_load_boards, daemon=True).start()

def fetch_items(bid):
    now = time.time()
    if bid in _items_cache:
        ts, cached = _items_cache[bid]
        if now - ts < _items_cache_ttl:
            return cached
    items, cur = [], None
    while True:
        c = f',cursor:"{cur}"' if cur else ""
        d = gql(f"""{{boards(ids:["{bid}"]){{items_page(limit:200{c}){{
            cursor items{{id name group{{id title}} column_values{{id text value}}}}
        }}}}}}""")
        page = d["boards"][0]["items_page"]
        items.extend(page["items"])
        cur = page["cursor"]
        if not cur:
            break
    _items_cache[bid] = (time.time(), items)
    return items

def col_val(item, cid):
    for cv in item["column_values"]:
        if cv["id"] == cid:
            return cv.get("text") or ""
    return ""

def parse_date(s):
    if not s: return None
    try: return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except: return None

# ── TÜRKÇE NLP ───────────────────────────────────────────────────────────────

def nrm(s):
    return (s.lower()
        .replace("ğ","g").replace("ü","u").replace("ş","s")
        .replace("ı","i").replace("ö","o").replace("ç","c")
        .replace("-"," ").replace("_"," "))

MONTHS_ALL = {
    "ocak":1,"subat":2,"mart":3,"nisan":4,"mayis":5,"haziran":6,
    "temmuz":7,"agustos":8,"eylul":9,"ekim":10,"kasim":11,"aralik":12,
    "şubat":2,"mayıs":5,"ağustos":8,"eylül":9,"kasım":11,"aralık":12,
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}

MONTH_NAMES = ["","Ocak","Şubat","Mart","Nisan","Mayıs","Haziran",
               "Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]

GRAMMAR = {
    "ne","kadar","var","yok","nedir","goster","göster","soyle","söyle","bul","ver",
    "panosunda","panosundaki","panonun","projesinde","projesindeki","formunda","formundaki",
    "tabloda","tablodaki","ayinda","ayındaki","yilinda","yılındaki","tarihinde",
    "kac","kaç","olan","ile","icin","için","bu","su","şu","bir",
    "de","da","te","ta","ve","mi","mu","mü","hangi","ait","ilgili",
    "icinde","içinde","icindeki","içindeki","neler","nasil","nasıl","listele",
}

def score_board(bn, words, qnrm):
    bnn = nrm(bn)
    # Subitems panolarına büyük ceza ver — ana panoları tercih et
    is_sub = "panosunun alt" in bnn or "alt ogeleri" in bnn
    if bnn == qnrm: return 1 if is_sub else 10000
    if qnrm and len(qnrm) > 2 and qnrm in bnn:
        base = 5000 + max(0, 100 - len(bn))
        return base // 20 if is_sub else base
    score = 0
    for w in words:
        wn = nrm(w)
        if len(wn) < 2: continue
        if wn in bnn:
            score += 20 * len(wn)
            if re.search(r"(^| )" + re.escape(wn) + r"( |$)", bnn):
                score += 10 * len(wn)
    return score // 10 if is_sub else score

def find_board(query):
    q = nrm(query.strip())
    words = [w for w in re.split(r"\W+", q) if w and len(w) > 1]
    if not words: return None
    best_id = best_b = None
    best_s  = 0
    for bid, b in boards_cache.items():
        s = score_board(b["name"], words, q)
        if s > best_s:
            best_s = s
            best_id, best_b = bid, b
    return (best_id, best_b) if best_s > 0 else None

def find_all_boards(query, min_score=30):
    """Sorguyla eşleşen TÜM panoları döndür (en yüksek skora göre filtreli)."""
    q = nrm(query.strip())
    words = [w for w in re.split(r"\W+", q) if w and len(w) > 1]
    if not words: return []
    scored = []
    top_score = 0
    for bid, b in boards_cache.items():
        s = score_board(b["name"], words, q)
        if s > 0:
            scored.append((s, bid, b))
            if s > top_score: top_score = s
    scored.sort(reverse=True)
    cutoff = max(min_score, top_score * 0.35)
    return [(bid, b) for s, bid, b in scored if s >= cutoff]

def search_boards(query=""):
    if not query:
        res = sorted(boards_cache.items(), key=lambda x: x[1]["name"])
        return [{"id":bid,"name":b["name"]} for bid,b in res]
    qn    = nrm(query)
    words = [w for w in re.split(r"\W+", qn) if w and len(w) > 1]
    scored = []
    for bid, b in boards_cache.items():
        s = score_board(b["name"], words, qn)
        if s > 0: scored.append((s, bid, b))
    scored.sort(reverse=True)
    return [{"id":bid,"name":b["name"]} for _,bid,b in scored]

# ── INTENT PARSER ────────────────────────────────────────────────────────────

def parse_intent(text):
    t  = text.strip()
    tn = nrm(t)
    now = datetime.now()

    if any(x in tn for x in ["yardim","nasil kullan","ne yapabilir","ne sorabilirim"]):
        return {"intent": "help"}

    list_kw = ["hangi pano","pano listesi","tum pano","tüm pano","panolar var",
               "panolar neler","mevcut pano","pano goster","pano göster","panolari goster"]
    matched_lkw = next((nrm(k) for k in list_kw if nrm(k) in tn), None)
    if matched_lkw:
        search = ""
        # Önce "içeren/başlayan" sonrasına bak
        for kw in ["iceren","icinde","içeren","baslayan","başlayan"]:
            if kw in tn:
                idx = tn.index(kw) + len(kw)
                rest = t[idx:].strip()
                if rest: search = rest.split()[0]
                break
        # Yoksa list keyword'ünden sonraki anlamlı kelimeleri al
        if not search:
            idx = tn.index(matched_lkw) + len(matched_lkw)
            rest_words = [w for w in re.split(r"\W+", tn[idx:]) if w and w not in GRAMMAR and len(w) > 1]
            search = " ".join(rest_words[:3])
        return {"intent": "list", "search": search}

    # Yıl
    year = None
    m = re.search(r"\b(20\d{2})\b", t)
    if m: year = int(m.group(1))
    if not year and "bu yil" in tn: year = now.year
    if "gecen yil" in tn or "geçen yıl" in t.lower():
        year = (year or now.year) - 1

    # Ay
    month = None
    for mn in sorted(MONTHS_ALL.keys(), key=len, reverse=True):
        if re.search(r"\b" + re.escape(mn) + r"\b", tn):
            month = MONTHS_ALL[mn]; break
    if month is None and "bu ay" in t.lower():
        month = now.month; year = year or now.year
    if month is None and ("gecen ay" in tn or "geçen ay" in t.lower()):
        prev = now.replace(day=1) - timedelta(days=1)
        month = prev.month; year = year or prev.year
    if month is None and ("bugun" in tn or "bugün" in t.lower() or "dun" in tn or "dün" in t.lower()):
        month = now.month; year = year or now.year

    # Metrik
    count_kw = ["kac adet","kaç adet","kac tane","kaç tane","kac kayit","kaç kayıt","kayit sayisi","kayıt sayısı"]
    # "kaç" tek başına da count sayılır
    metric = "count" if (any(nrm(k) in tn for k in count_kw) or
                         (re.search(r"\bkac\b", tn) and "tutar" not in tn and "toplam" not in tn)) else "sum"

    # Pano bul — "X panosunda Y" kalıbı varsa X'i doğrudan al
    PANO_SUFFIXES = ["panosunda","panosundaki","panonun","panosundan","formunda",
                     "formundaki","tabloda","tablodaki","listesinde","listesindeki",
                     "projesinde","projesindeki","panosunu","panosunun"]
    board_hint = None
    for sfx in PANO_SUFFIXES:
        if sfx in tn:
            idx = tn.index(sfx)
            hint = tn[:idx].strip()
            # Yıl ve ay isimlerini temizle
            hint = re.sub(r"\b20\d{2}\b", " ", hint)
            for mn in sorted(MONTHS_ALL.keys(), key=len, reverse=True):
                hint = re.sub(r"\b" + re.escape(nrm(mn)) + r"\b", " ", hint)
            hint = hint.strip()
            if hint:
                board_hint = hint
            break

    clean = re.sub(r"\b20\d{2}\b", " ", tn)
    for mn in sorted(MONTHS_ALL.keys(), key=len, reverse=True):
        clean = re.sub(r"\b" + re.escape(nrm(mn)) + r"\b", " ", clean)

    if board_hint:
        # Pano adını doğrudan "panosunda" öncesi kısımdan al
        words = [w for w in re.split(r"\W+", board_hint) if w and len(w) > 1]
    else:
        words = [w for w in re.split(r"\W+", clean) if w and w not in GRAMMAR and len(w) > 1]

    board_id = board_name = None
    if words:
        found = find_board(" ".join(words))
        if found:
            board_id, b = found
            board_name  = b["name"]

    multi_kw = ["tum panolar","tüm panolar","butun pano","bütün pano",
                "hepsini","panolardan","tum masraf","tüm masraf",
                "tum bordro","tüm bordro","tum fatura","tüm fatura"]
    is_multi = any(nrm(k) in tn for k in multi_kw)

    return {"intent":"query","board_id":board_id,"board_name":board_name,
            "year":year,"month":month,"metric":metric,"multi":is_multi}

# ── BOARD QUERY ──────────────────────────────────────────────────────────────

def fmt_money(n):
    if n == 0: return "0 ₺"
    s = f"{abs(n):,.0f}".replace(",",".")
    return ("-" if n < 0 else "") + s + " ₺"

def do_query(board_name, year=None, month=None, metric="sum"):
    maybe_reload()
    found = find_board(board_name)
    if not found:
        return {"error": f"'{board_name}' ile eşleşen pano bulunamadı."}
    bid, binfo = found

    items = fetch_items(bid)

    date_col = None
    for c in binfo["columns"]:
        if c["type"] == "date" and any(k in c["title"].lower() for k in ["tarih","date"]):
            date_col = c; break
    if not date_col:
        for c in binfo["columns"]:
            if c["type"] == "date": date_col = c; break

    num_kw   = ["tutar","matrah","toplam","fiyat","bedel","miktar","ucret","ücret","maliyet","gelir","gider","net","kdv","stopaj"]
    NUMERIC_TYPES = ("numbers", "formula", "numeric")
    NUMERIC_TYPES = ("numbers", "formula", "numeric")
    # Önce ada göre eşleşen sayısal sütunlar
    kw_cols = [c for c in binfo["columns"]
               if c["type"] in NUMERIC_TYPES and any(k in nrm(c["title"]) for k in num_kw)]
    # "Toplam X" sütunları varsa bireysel KDV breakdown'larına öncelik ver
    toplam_cols = [c for c in kw_cols if nrm(c["title"]).startswith("toplam")]
    num_cols = toplam_cols if toplam_cols else kw_cols
    # Yoksa tüm sayısal sütunlar
    if not num_cols:
        num_cols = [c for c in binfo["columns"] if c["type"] in NUMERIC_TYPES]
    # Hâlâ yoksa metin sütunlarını da dene (adına göre eşleşiyorsa)
    if not num_cols:
        num_cols = [c for c in binfo["columns"]
                    if c["type"] == "text" and any(k in nrm(c["title"]) for k in num_kw)]

    filtered, skipped = [], 0
    for item in items:
        if (year or month) and date_col:
            d = parse_date(col_val(item, date_col["id"]))
            if d is None: skipped += 1; continue
            if year  and d.year  != year:  continue
            if month and d.month != month: continue
        filtered.append(item)

    period = " ".join(filter(None,[
        str(year)  if year  else "",
        MONTH_NAMES[month] if month else "",
    ])) or "Tüm zamanlar"

    res = {"board_name":binfo["name"],"period":period,
           "total_items":len(filtered),"skipped":skipped}

    if metric == "count":
        res["count"] = len(filtered)
    else:
        sums = {}
        for col in num_cols:
            total = 0.0
            for item in filtered:
                try:
                    v = col_val(item, col["id"]).replace(",",".").strip()
                    if v: total += float(v)
                except: pass
            if total: sums[col["title"]] = round(total, 2)
        res["amounts"]     = sums
        res["grand_total"] = round(sum(sums.values()), 2)

    res["samples"] = [
        {"name":i["name"],"group":i.get("group",{}).get("title","")}
        for i in filtered[:5]
    ]
    return res

def fmt_result(res, metric):
    if "error" in res: return f"Hata: {res['error']}"
    board  = res["board_name"]
    period = res["period"]
    n      = res["total_items"]
    sk     = res.get("skipped", 0)

    if metric == "count":
        return f"**{board}**\n📅 {period}\n📊 **{n} kayıt** bulundu."

    amounts = res.get("amounts", {})
    grand   = res.get("grand_total", 0)

    if not amounts:
        note = f"\n_(Tarihi olmayan {sk} kayıt dahil edilmedi)_" if sk else ""
        return f"**{board}**\n📅 {period}\n📋 {n} kayıt — sayısal sütun bulunamadı.{note}"

    lines = [f"**{board}**", f"📅 {period} — {n} kayıt", ""]
    for col, val in amounts.items():
        lines.append(f"• {col}: **{fmt_money(val)}**")
    if len(amounts) > 1:
        lines.append(f"\n💰 **Toplam: {fmt_money(grand)}**")
    if sk:
        lines.append(f"\n_(Tarihi olmayan {sk} kayıt dahil edilmedi)_")
    if res.get("samples"):
        lines.append("\nÖrnek kayıtlar:")
        for s in res["samples"][:3]:
            g = f" [{s['group']}]" if s['group'] else ""
            lines.append(f"  – {s['name'][:45]}{g}")
    return "\n".join(lines)

def do_multi_query(board_query, year=None, month=None, metric="sum"):
    """Eşleşen TÜM panolara sorgu at, sonuçları birleştir."""
    maybe_reload()
    clean = re.sub(r"\b20\d{2}\b", " ", nrm(board_query))
    for mn in sorted(MONTHS_ALL.keys(), key=len, reverse=True):
        clean = re.sub(r"\b" + re.escape(nrm(mn)) + r"\b", " ", clean)
    words = [w for w in re.split(r"\W+", clean) if w and w not in GRAMMAR and len(w) > 1]
    if not words:
        return {"error": "Sorgu anlaşılamadı"}
    all_boards = find_all_boards(" ".join(words))
    if not all_boards:
        return {"error": f"'{board_query}' ile eşleşen pano bulunamadı"}
    results, errs = [], []
    for bid, binfo in all_boards[:15]:
        try:
            res = do_query(binfo["name"], year=year, month=month, metric=metric)
            if "error" not in res:
                results.append(res)
        except Exception as e:
            errs.append(f"{binfo['name']}: {e}")
    grand = round(sum(r.get("grand_total", 0) for r in results), 2)
    total_items = sum(r.get("total_items", 0) for r in results)
    return {"boards": results, "grand_total": grand, "total_items": total_items,
            "board_count": len(results), "errors": errs}

def fmt_multi_result(res, year, month, metric):
    if "error" in res:
        return f"Hata: {res['error']}"
    n_boards = res["board_count"]
    period = " ".join(filter(None, [
        str(year) if year else "",
        MONTH_NAMES[month] if month else ""
    ])) or "Tüm zamanlar"

    lines = [f"📊 **{period} — {n_boards} pano**", ""]
    for b in res["boards"]:
        bname = b["board_name"]
        n = b["total_items"]
        if metric == "count":
            lines.append(f"• **{bname}**: {n} kayıt")
        else:
            gt = b.get("grand_total", 0)
            amounts = b.get("amounts", {})
            if gt:
                lines.append(f"• **{bname}**: {fmt_money(gt)} ({n} kayıt)")
            elif amounts == {} and n > 0:
                lines.append(f"• **{bname}**: {n} kayıt (sayısal sütun yok)")

    if not res["boards"]:
        return f"**{period}** için hiçbir panoda kayıt bulunamadı."

    if metric == "count":
        lines.append(f"\n📋 **TOPLAM: {res['total_items']} kayıt**")
    elif res["grand_total"]:
        lines.append(f"\n💰 **GENEL TOPLAM: {fmt_money(res['grand_total'])}**")
    return "\n".join(lines)

HELP_TEXT = """**WW Asistan — Nasıl kullanılır?**

Türkçe soru sormanız yeterli:

📊 **Tutar sorguları:**
• Nisan 2026 E-Arşiv fatura tutarı?
• Bu yıl masraf formu toplamı?
• Genel Gider 2026 ne kadar harcandı?

📋 **Kayıt sayısı:**
• Masraf formunda kaç kayıt var?
• Bu ay kaç e-fatura kesildi?

📂 **Pano listesi:**
• Hangi panolar var?

🔢 **Çoklu pano toplamı:**
• Nisan 2026 Genel Giderler panolardan toplam?
• Bu yıl tüm masraf panolarından toplam?

💡 Pano adını bilmiyorsanız **"hangi panolar var?"** yazın."""

def handle_query(text):
    text = text.strip()
    if not text:
        return "Bir soru yazın. Yardım için **\"yardım\"** yazın."

    intent = parse_intent(text)

    if intent["intent"] == "help":
        return HELP_TEXT

    if intent["intent"] == "list":
        if not boards_cache:
            return "⏳ Panolar henüz yüklenmedi. Lütfen 30 saniye bekleyin ve tekrar deneyin."
        boards = search_boards(intent.get("search",""))
        search = intent.get("search","")
        header = f"**{'Eşleşen' if search else 'Tüm'} panolar ({len(boards)} adet)**"
        names  = "\n".join(f"• {b['name']}" for b in boards[:25])
        suffix = f"\n\n_({len(boards)-25} pano daha — daha spesifik arama yapın)_" if len(boards) > 25 else ""
        return f"{header}:\n{names}{suffix}"

    if not boards_cache:
        return "⏳ Panolar henüz yükleniyor (ilk açılışta ~30 sn sürebilir). Lütfen biraz bekleyin ve tekrar sorun."

    if not intent["board_id"]:
        # En yakın tahminleri bul
        clean_q = re.sub(r"\b20\d{2}\b", " ", nrm(text))
        for mn in sorted(MONTHS_ALL.keys(), key=len, reverse=True):
            clean_q = re.sub(r"\b" + re.escape(nrm(mn)) + r"\b", " ", clean_q)
        sw = [w for w in re.split(r"\W+", clean_q) if w and w not in GRAMMAR and len(w) > 1]
        close = []
        if sw:
            qn = " ".join(sw)
            for bid, b in boards_cache.items():
                s = score_board(b["name"], sw, qn)
                if s > 0:
                    close.append((s, b["name"]))
            close.sort(reverse=True)
        if close:
            sug = "\n".join(f"• {n}" for _, n in close[:4])
            return (f"Eşleşen pano bulunamadı. Bunları kastettiniz mi?\n\n{sug}"
                    f"\n\nTüm panolar için: **\"hangi panolar var?\"**")
        samples = list(boards_cache.values())[:4]
        ex = ", ".join(f'"{b["name"][:22]}"' for b in samples)
        return (f"Bu soruyla eşleşen pano bulunamadı.\n\n"
                f"Örnek panolar: {ex}\n\n"
                f"Tüm panoları görmek için: **\"hangi panolar var?\"**")

    try:
        if intent.get("multi"):
            query_str = intent["board_name"] or text
            res = do_multi_query(query_str, year=intent["year"],
                                 month=intent["month"], metric=intent["metric"])
            return fmt_multi_result(res, intent["year"], intent["month"], intent["metric"])
        else:
            res = do_query(intent["board_name"], year=intent["year"],
                           month=intent["month"], metric=intent["metric"])
            return fmt_result(res, intent["metric"])
    except Exception as e:
        return f"Sorgu hatası: {str(e)}"

# ── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/api/ping")
def ping():
    return "ok"

@app.route("/api/status")
def status():
    return jsonify({
        "boards":            len(boards_cache),
        "loading":           _loading,
        "server_key":        False,
        "password_required": bool(APP_PASSWORD),
    })

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "?q= parametresi gerekli"})
    qn = nrm(q)
    words = [w for w in re.split(r"\W+", qn) if w and len(w) > 1]
    results = []
    for bid, b in boards_cache.items():
        s = score_board(b["name"], words, qn)
        if s > 0:
            results.append({"score": s, "id": bid, "name": b["name"],
                            "cols": [{"id":c["id"],"title":c["title"],"type":c["type"]}
                                     for c in b["columns"]]})
    results.sort(key=lambda x: -x["score"])
    return jsonify({"query": q, "found": len(results), "boards": results[:20]})

@app.route("/api/chat", methods=["POST"])
def chat():
    body     = request.get_json(silent=True) or {}
    password = body.get("password", "")
    messages = body.get("messages", [])

    if APP_PASSWORD and password != APP_PASSWORD:
        return jsonify({"error": "Şifre hatalı"}), 401

    last_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            last_msg = c if isinstance(c, str) else str(c)
            break

    if not last_msg:
        return jsonify({"error": "Mesaj boş"}), 400

    try:
        reply = handle_query(last_msg)
    except Exception as e:
        reply = f"Beklenmeyen hata: {str(e)}"

    messages = list(messages) + [{"role": "assistant", "content": reply}]
    return jsonify({"reply": reply, "messages": messages})

@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "WorknWerk Asistan", "short_name": "WW Asistan",
        "description": "Monday.com Finans Asistanı",
        "start_url": "/", "display": "standalone",
        "background_color": "#0f0f17", "theme_color": "#6366f1",
        "orientation": "portrait",
        "icons": [
            {"src":"/icon/192","sizes":"192x192","type":"image/svg+xml"},
            {"src":"/icon/512","sizes":"512x512","type":"image/svg+xml"},
        ],
    })

@app.route("/icon/<int:size>")
def icon(size):
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}">'
           f'<rect width="{size}" height="{size}" rx="{size//5}" fill="#6366f1"/>'
           f'<text x="50%" y="54%" font-size="{size//2}" text-anchor="middle"'
           f' dominant-baseline="middle" fill="white">⚡</text></svg>')
    return Response(svg, mimetype="image/svg+xml")

@app.route("/sw.js")
def sw():
    js = """
self.addEventListener('install', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.map(k => caches.delete(k)))).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => {
  var url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/') || url.pathname === '/sw.js') return;
  e.respondWith(
    fetch(e.request, {cache: 'no-store'}).catch(() => new Response('Offline - lutfen internet baglantinizi kontrol edin', {status: 503}))
  );
});
"""
    return Response(js, mimetype="application/javascript",
                    headers={"Cache-Control": "no-store, no-cache"})

@app.route("/")
def index():
    resp = Response(MOBILE_HTML, content_type="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

# ── HTML ─────────────────────────────────────────────────────────────────────
MOBILE_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
<meta name="apple-mobile-web-app-title" content="WW Asistan"/>
<meta name="mobile-web-app-capable" content="yes"/>
<meta name="theme-color" content="#0f0f17"/>
<link rel="manifest" href="/manifest.json"/>
<link rel="apple-touch-icon" href="/icon/192"/>
<title>WW Asistan</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0d14;--surface:#141420;--card:#1c1c2e;--border:rgba(255,255,255,.07);
  --text:#e8e8f5;--muted:rgba(255,255,255,.38);
  --accent:#6366f1;--accent2:#818cf8;--green:#10b981;--red:#ef4444;--amber:#f59e0b;
  --teal:#06b6d4;--purple:#8b5cf6;
  --st:env(safe-area-inset-top);--sb:env(safe-area-inset-bottom);
}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  -webkit-tap-highlight-color:transparent}

/* ── HEADER ── */
.hdr{display:flex;align-items:center;gap:8px;
  padding:10px 16px;padding-top:calc(10px + var(--st));
  background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0}
.hdr-logo{font-size:14px;font-weight:800;
  background:linear-gradient(135deg,#6366f1,#a78bfa);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;flex:1}
.hdr-date{font-size:10px;color:var(--muted)}
.hdr-st{font-size:10px;display:flex;align-items:center;gap:3px;margin-left:8px}
.hdr-st.ok{color:var(--green)}.hdr-st.warn{color:var(--amber)}.hdr-st.err{color:var(--red)}
.sdot{width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}
.sdot.pulse{animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.hdr-refresh{width:30px;height:30px;border-radius:8px;border:none;
  background:rgba(255,255,255,.06);color:var(--muted);font-size:14px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;-webkit-appearance:none}
.hdr-refresh:active{background:rgba(99,102,241,.2);color:var(--accent2)}
.hdr-refresh.spin{animation:spin .6s linear}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── DASHBOARD GRID ── */
#dashboard{position:fixed;inset:0;display:flex;flex-direction:column}
.grid{flex:1;overflow-y:auto;padding:12px;
  display:grid;grid-template-columns:1fr 1fr;gap:10px;align-content:start;
  -webkit-overflow-scrolling:touch}

/* ── KPI CARD ── */
.kpi{background:var(--card);border:1px solid var(--border);border-radius:18px;
  padding:15px;position:relative;overflow:hidden;cursor:pointer;
  transition:border-color .15s,transform .1s;-webkit-appearance:none;
  text-align:left;font-family:inherit;width:100%;color:var(--text)}
.kpi:active{transform:scale(.97)}
.kpi::after{content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:var(--kc,var(--accent))}
.kpi.green{--kc:var(--green)}.kpi.teal{--kc:var(--teal)}
.kpi.amber{--kc:var(--amber)}.kpi.purple{--kc:var(--purple)}
.kpi.red{--kc:var(--red)}.kpi.blue{--kc:var(--accent)}
.kpi-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}
.kpi-icon{font-size:20px;line-height:1}
.kpi-badge{font-size:9px;background:rgba(255,255,255,.07);padding:2px 7px;
  border-radius:10px;color:var(--muted);white-space:nowrap}
.kpi-label{font-size:10px;font-weight:700;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px;line-height:1.3}
.kpi-val{font-size:24px;font-weight:800;line-height:1.15;min-height:32px;
  color:var(--text);word-break:break-all}
.kpi.green .kpi-val{color:var(--green)}.kpi.teal .kpi-val{color:var(--teal)}
.kpi.amber .kpi-val{color:var(--amber)}.kpi.purple .kpi-val{color:var(--purple)}
.kpi-val.loading{color:var(--muted);animation:shimmer 1.4s infinite}
@keyframes shimmer{0%,100%{opacity:.3}50%{opacity:.8}}
.kpi-sub{font-size:10px;color:var(--muted);margin-top:5px;line-height:1.4}

/* ── FAB ── */
.fab{position:fixed;bottom:calc(20px + var(--sb));right:16px;
  width:54px;height:54px;border-radius:50%;border:none;
  background:linear-gradient(135deg,#6366f1,#818cf8);color:#fff;font-size:22px;
  cursor:pointer;z-index:200;box-shadow:0 4px 20px rgba(99,102,241,.55);
  display:flex;align-items:center;justify-content:center;
  -webkit-appearance:none;transition:transform .15s,background .2s}
.fab:active{transform:scale(.9)}
.fab.open{background:linear-gradient(135deg,#1f2937,#374151);font-size:16px}

/* ── CHAT PANEL ── */
.cp{position:fixed;bottom:calc(84px + var(--sb));right:12px;
  width:min(390px,calc(100vw - 24px));height:min(580px,73vh);
  background:var(--surface);border:1px solid var(--border);
  border-radius:22px;box-shadow:0 8px 50px rgba(0,0,0,.7);
  display:flex;flex-direction:column;z-index:150;overflow:hidden;
  transform:scale(.9) translateY(20px);opacity:0;pointer-events:none;
  transition:transform .22s cubic-bezier(.34,1.56,.64,1),opacity .18s}
.cp.open{transform:scale(1) translateY(0);opacity:1;pointer-events:all}
.cp-hdr{display:flex;align-items:center;gap:8px;padding:11px 12px;
  background:var(--card);border-bottom:1px solid var(--border);flex-shrink:0}
.cp-av{width:32px;height:32px;border-radius:9px;flex-shrink:0;font-size:16px;
  background:linear-gradient(135deg,#6366f1,#a78bfa);
  display:flex;align-items:center;justify-content:center}
.cp-info{flex:1;min-width:0}
.cp-name{font-size:13px;font-weight:700}
.cp-st{font-size:10px;display:flex;align-items:center;gap:3px;margin-top:1px}
.cp-st.ok{color:var(--green)}.cp-st.warn{color:var(--amber)}.cp-st.err{color:var(--red)}
.cpdot{width:5px;height:5px;border-radius:50%;background:currentColor}
.cpdot.pulse{animation:pulse 2s infinite}
.cpbtn{width:28px;height:28px;border-radius:8px;border:none;
  background:rgba(255,255,255,.07);color:var(--muted);cursor:pointer;
  font-size:14px;display:flex;align-items:center;justify-content:center;
  flex-shrink:0;-webkit-appearance:none}
.cp-msgs{flex:1;overflow-y:auto;padding:10px;display:flex;flex-direction:column;
  gap:8px;-webkit-overflow-scrolling:touch}
.cp-msgs::-webkit-scrollbar{display:none}
.mrow{display:flex;align-items:flex-end;gap:6px}
.mrow.user{flex-direction:row-reverse}
.mav{width:26px;height:26px;border-radius:8px;flex-shrink:0;font-size:12px;
  display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,#6366f1,#a78bfa)}
.mav.u{background:linear-gradient(135deg,#4338ca,#6366f1)}
.bubble{max-width:84%;padding:9px 12px;border-radius:16px;
  font-size:13px;line-height:1.6;word-break:break-word}
.bubble.bot{background:var(--card);border-bottom-left-radius:3px}
.bubble.user{background:#4f46e5;border-bottom-right-radius:3px;color:#fff}
.bubble.err{background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.3)}
.btime{font-size:10px;color:rgba(255,255,255,.2);margin-top:4px;text-align:right}
.retry-btn,.copy-btn{margin-top:5px;padding:3px 9px;border-radius:7px;border:none;
  font-size:10px;cursor:pointer;-webkit-appearance:none;display:inline-block}
.retry-btn{background:rgba(99,102,241,.25);color:var(--accent2);display:block}
.copy-btn{background:rgba(255,255,255,.06);color:var(--muted)}
.copy-btn:active{background:rgba(16,185,129,.2);color:var(--green)}
.typing{background:var(--card);border-radius:16px;border-bottom-left-radius:3px;
  padding:10px 14px;display:flex;gap:4px;align-items:center}
.tdot{width:5px;height:5px;border-radius:50%;background:rgba(255,255,255,.3);
  animation:bop .9s infinite}
.tdot:nth-child(2){animation-delay:.15s}.tdot:nth-child(3){animation-delay:.3s}
@keyframes bop{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-5px)}}
.welcome{display:flex;flex-direction:column;align-items:center;justify-content:center;
  flex:1;padding:24px;text-align:center;gap:8px}
.w-ico{font-size:38px}.w-title{font-size:15px;font-weight:700}
.w-sub{font-size:12px;color:var(--muted);line-height:1.7;max-width:240px}
.cp-sugs{padding:5px 10px 3px;display:flex;gap:5px;overflow-x:auto;
  flex-shrink:0;-webkit-overflow-scrolling:touch}
.cp-sugs::-webkit-scrollbar{display:none}
.chip{white-space:nowrap;padding:5px 10px;border-radius:16px;
  border:1px solid var(--border);background:rgba(255,255,255,.04);
  color:rgba(255,255,255,.6);font-size:11px;cursor:pointer;
  flex-shrink:0;font-family:inherit;-webkit-appearance:none}
.chip:active{background:rgba(99,102,241,.2);border-color:var(--accent);color:var(--accent2)}
.cp-bar{padding:8px 10px;padding-bottom:calc(8px + var(--sb));
  background:var(--surface);border-top:1px solid var(--border);
  display:flex;gap:7px;align-items:flex-end;flex-shrink:0}
.msg-inp{flex:1;background:rgba(255,255,255,.06);border:1px solid var(--border);
  border-radius:20px;padding:9px 14px;color:var(--text);font-size:14px;
  outline:none;resize:none;max-height:90px;line-height:1.4;
  font-family:inherit;-webkit-appearance:none}
.msg-inp::placeholder{color:var(--muted)}
.msg-inp:focus{border-color:rgba(99,102,241,.5)}
.send-btn{width:40px;height:40px;min-width:40px;border-radius:50%;border:none;
  background:linear-gradient(135deg,#6366f1,#818cf8);color:#fff;font-size:18px;
  cursor:pointer;display:flex;align-items:center;justify-content:center;
  -webkit-appearance:none;transition:transform .1s,opacity .1s}
.send-btn:active{transform:scale(.88)}.send-btn:disabled{opacity:.3}
.toast{position:fixed;bottom:calc(88px + var(--sb));left:50%;
  transform:translateX(-50%) translateY(10px);
  background:#1e1e35;color:var(--text);padding:8px 16px;border-radius:10px;
  font-size:12px;border:1px solid rgba(255,255,255,.1);z-index:300;opacity:0;
  transition:all .25s;pointer-events:none;white-space:nowrap}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#loadingBar{position:fixed;top:0;left:0;height:2px;
  background:linear-gradient(90deg,#6366f1,#a78bfa);width:0%;
  transition:width .4s;z-index:400}
</style>
</head>
<body>
<div id="loadingBar"></div>

<!-- DASHBOARD -->
<div id="dashboard">
  <div class="hdr">
    <div class="hdr-logo">⚡ WorknWerk</div>
    <div class="hdr-date" id="hdrDate"></div>
    <div class="hdr-st warn" id="dashStatus">
      <div class="sdot pulse"></div>
      <span id="dashStatusTx">...</span>
    </div>
    <button class="hdr-refresh" id="refreshBtn" title="Yenile">↻</button>
  </div>

  <div class="grid" id="grid">
    <button class="kpi green" id="k0" data-q="E-Faturalar panosunda bu ay toplam matrah">
      <div class="kpi-top"><span class="kpi-icon">🧾</span><span class="kpi-badge">Bu Ay</span></div>
      <div class="kpi-label">E-Faturalar · Matrah</div>
      <div class="kpi-val loading" id="kv0">—</div>
      <div class="kpi-sub" id="ks0">yükleniyor...</div>
    </button>
    <button class="kpi teal" id="k1" data-q="E-Faturalar panosunda bu ay toplam tutar">
      <div class="kpi-top"><span class="kpi-icon">💰</span><span class="kpi-badge">Bu Ay</span></div>
      <div class="kpi-label">E-Faturalar · Tutar</div>
      <div class="kpi-val loading" id="kv1">—</div>
      <div class="kpi-sub" id="ks1">yükleniyor...</div>
    </button>
    <button class="kpi blue" id="k2" data-q="E-Arşiv Faturalar panosunda bu ay toplam matrah">
      <div class="kpi-top"><span class="kpi-icon">📂</span><span class="kpi-badge">Bu Ay</span></div>
      <div class="kpi-label">E-Arşiv · Matrah</div>
      <div class="kpi-val loading" id="kv2">—</div>
      <div class="kpi-sub" id="ks2">yükleniyor...</div>
    </button>
    <button class="kpi amber" id="k3" data-q="Genel Giderler 2026 panolardan toplam">
      <div class="kpi-top"><span class="kpi-icon">📊</span><span class="kpi-badge">2026</span></div>
      <div class="kpi-label">Genel Giderler</div>
      <div class="kpi-val loading" id="kv3">—</div>
      <div class="kpi-sub" id="ks3">yükleniyor...</div>
    </button>
    <button class="kpi purple" id="k4" data-q="E-Faturalar panosunda bu ay kaç kayıt">
      <div class="kpi-top"><span class="kpi-icon">🔢</span><span class="kpi-badge">Bu Ay</span></div>
      <div class="kpi-label">E-Faturalar · Adet</div>
      <div class="kpi-val loading" id="kv4">—</div>
      <div class="kpi-sub" id="ks4">yükleniyor...</div>
    </button>
    <button class="kpi red" id="k5" data-q="Masraf formunda bu ay kaç kayıt">
      <div class="kpi-top"><span class="kpi-icon">📝</span><span class="kpi-badge">Bu Ay</span></div>
      <div class="kpi-label">Masraf Formu</div>
      <div class="kpi-val loading" id="kv5">—</div>
      <div class="kpi-sub" id="ks5">yükleniyor...</div>
    </button>
  </div>
</div>

<!-- FAB -->
<button class="fab" id="fab" type="button">⚡</button>

<!-- CHAT PANEL -->
<div class="cp" id="cp">
  <div class="cp-hdr">
    <div class="cp-av">⚡</div>
    <div class="cp-info">
      <div class="cp-name">WW Asistan <span style="font-size:9px;opacity:.3;font-weight:400">v11</span></div>
      <div class="cp-st warn" id="hstatus">
        <div class="cpdot pulse"></div>
        <span id="hstatusText">Hazır</span>
      </div>
    </div>
    <button class="cpbtn" id="clearBtn" title="Temizle">🗑</button>
    <button class="cpbtn" id="cpClose">✕</button>
  </div>
  <div class="cp-msgs" id="msgs">
    <div class="welcome" id="welcome">
      <div class="w-ico">👋</div>
      <div class="w-title">Merhaba!</div>
      <div class="w-sub">Monday.com verilerinizi Türkçe sorgulayın. Bir karta tıklayın veya kendiniz yazın.</div>
    </div>
  </div>
  <div class="cp-sugs" id="sugs">
    <button class="chip" type="button">E-Faturalar bu ay matrah</button>
    <button class="chip" type="button">Nisan 2026 E-Faturalar</button>
    <button class="chip" type="button">Hangi panolar var?</button>
    <button class="chip" type="button">Genel Giderler panolardan toplam</button>
  </div>
  <div class="cp-bar">
    <textarea class="msg-inp" id="msgInp" placeholder="Soru sorun..." rows="1"></textarea>
    <button class="send-btn" id="sendBtn" type="button"
      onclick="sendMessage()" ontouchend="event.preventDefault();sendMessage()">↑</button>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
var BASE = location.origin;
var messages = [];
var loading = false;
var lastQuery = '';
var chatOpen = false;

// ── DATE ──
(function(){
  var months = ['Ocak','Şubat','Mart','Nisan','Mayıs','Haziran','Temmuz','Ağustos','Eylül','Ekim','Kasım','Aralık'];
  var d = new Date();
  document.getElementById('hdrDate').textContent = d.getDate() + ' ' + months[d.getMonth()] + ' ' + d.getFullYear();
})();

// ── STATUS ──
function setStatus(state, text) {
  var pairs = [
    ['dashStatus','dashStatusTx','hdr-st '],
    ['hstatus','hstatusText','cp-st ']
  ];
  for (var i = 0; i < pairs.length; i++) {
    var el = document.getElementById(pairs[i][0]);
    var tx = document.getElementById(pairs[i][1]);
    if (!el || !tx) continue;
    el.className = pairs[i][2] + state;
    tx.textContent = text;
    var dot = el.querySelector('.sdot,.cpdot');
    if (dot) { if (state === 'ok') dot.classList.remove('pulse'); else dot.classList.add('pulse'); }
  }
}

function xhrGet(url, cb) {
  var x = new XMLHttpRequest();
  x.open('GET', url, true); x.timeout = 12000;
  x.onload = function() { try { cb(null, JSON.parse(x.responseText)); } catch(e) { cb(e); } };
  x.onerror = x.ontimeout = function() { cb(new Error('timeout')); };
  x.send();
}

var _pc = 0;
function pollStatus() {
  xhrGet(BASE + '/api/status', function(err, d) {
    if (err) {
      _pc++; if (_pc < 15) { setStatus('warn', 'Bağlanıyor...'); setTimeout(pollStatus, 3000); }
      else setStatus('err', 'Bağlantı yok'); return;
    }
    _pc = 0;
    if (d.boards > 0) setStatus('ok', d.boards + ' pano ✓');
    else { setStatus('warn', d.loading ? 'Yükleniyor...' : 'Bağlandı'); setTimeout(pollStatus, 4000); }
  });
}
setInterval(function(){ var x = new XMLHttpRequest(); x.open('GET',BASE+'/api/ping',true); x.send(); }, 240000);

// ── KPI LOADING ──
function parseKPI(reply) {
  var amount = null, count = null, boards = null;
  // Grand total: "TOPLAM: 1.234 ₺" or "💰 **GENEL TOPLAM: 1.234 ₺**"
  var m = reply.match(/TOPLAM[:\\s]+\\*?\\*?([0-9]+(?:\\.[0-9]{3})*(?:,[0-9]+)?)\\s*₺/i);
  if (m) amount = m[1];
  // Single column: "• Başlık: **1.234 ₺**"
  if (!amount) { m = reply.match(/•[^:]+:\\s*\\*\\*([0-9]+(?:\\.[0-9]{3})*(?:,[0-9]+)?)\\s*₺\\*\\*/); if (m) amount = m[1]; }
  // Count: "**102 kayıt**"
  m = reply.match(/\\*\\*([0-9]+)\\s*kayıt\\*\\*/i); if (m) count = parseInt(m[1]);
  // Board count for multi: "15 pano"
  m = reply.match(/([0-9]+)\\s*pano/i); if (m) boards = parseInt(m[1]);
  return {amount:amount, count:count, boards:boards};
}

function loadKPI(idx, q) {
  var valEl = document.getElementById('kv' + idx);
  var subEl = document.getElementById('ks' + idx);
  valEl.className = 'kpi-val loading';
  valEl.textContent = '—';
  subEl.textContent = 'yükleniyor...';
  var x = new XMLHttpRequest();
  x.open('POST', BASE + '/api/chat', true);
  x.setRequestHeader('Content-Type', 'application/json');
  x.timeout = 45000;
  x.onload = function() {
    try {
      var data = JSON.parse(x.responseText);
      var kpi = parseKPI(data.reply || '');
      valEl.className = 'kpi-val';
      if (kpi.amount) {
        valEl.textContent = kpi.amount + ' ₺';
        subEl.textContent = kpi.count ? kpi.count + ' kayıt' : (kpi.boards ? kpi.boards + ' pano' : '');
      } else if (kpi.count !== null) {
        valEl.textContent = kpi.count + '';
        subEl.textContent = 'kayıt';
      } else {
        valEl.textContent = '—';
        subEl.textContent = 'veri yok';
      }
    } catch(e) { valEl.className = 'kpi-val'; valEl.textContent = '—'; subEl.textContent = 'hata'; }
  };
  x.onerror = x.ontimeout = function() {
    valEl.className = 'kpi-val'; valEl.textContent = '—'; subEl.textContent = 'bağlantı hatası';
  };
  x.send(JSON.stringify({messages:[{role:'user',content:q}]}));
}

var KPI_QUERIES = [
  'E-Faturalar panosunda bu ay toplam matrah',
  'E-Faturalar panosunda bu ay toplam tutar',
  'E-Arşiv Faturalar panosunda bu ay toplam matrah',
  'Genel Giderler 2026 panolardan toplam',
  'E-Faturalar panosunda bu ay kaç kayıt',
  'Masraf formunda bu ay kaç kayıt'
];

function loadAllKPIs() {
  var rb = document.getElementById('refreshBtn');
  rb.classList.add('spin');
  setTimeout(function(){ rb.classList.remove('spin'); }, 700);
  for (var i = 0; i < KPI_QUERIES.length; i++) {
    (function(idx, q){ setTimeout(function(){ loadKPI(idx, q); }, idx * 300); })(i, KPI_QUERIES[i]);
  }
}

document.getElementById('refreshBtn').onclick = loadAllKPIs;

// Card click → open chat + query
var cards = document.querySelectorAll('.kpi');
for (var ci = 0; ci < cards.length; ci++) {
  cards[ci].onclick = function() {
    var q = this.getAttribute('data-q');
    if (q) { openChat(); setTimeout((function(qq){ return function(){ sendMessage(qq); }; })(q), 200); }
  };
}

// ── FAB TOGGLE ──
function openChat() {
  if (chatOpen) return;
  chatOpen = true;
  document.getElementById('fab').classList.add('open');
  document.getElementById('fab').textContent = '✕';
  document.getElementById('cp').classList.add('open');
}
function closeChat() {
  chatOpen = false;
  document.getElementById('fab').classList.remove('open');
  document.getElementById('fab').textContent = '⚡';
  document.getElementById('cp').classList.remove('open');
}
document.getElementById('fab').onclick = function() { if (chatOpen) closeChat(); else openChat(); };
document.getElementById('cpClose').onclick = closeChat;

// ── LOADING BAR ──
function setLoadingBar(pct) {
  var bar = document.getElementById('loadingBar');
  bar.style.width = pct + '%';
  if (pct >= 100) setTimeout(function(){ bar.style.width = '0%'; }, 400);
}

// ── SEND ──
function removeWelcome() {
  var w = document.getElementById('welcome'); if (w) w.remove();
  document.getElementById('sugs').style.display = 'none';
}

function sendMessage(txt) {
  var inp = document.getElementById('msgInp');
  var text = (txt !== undefined ? txt : inp.value).trim();
  if (!text || loading) return;
  openChat(); removeWelcome(); lastQuery = text;
  addBubble('user', text);
  inp.value = ''; inp.style.height = 'auto';
  messages.push({role:'user', content:text});
  var tid = addTyping(); loading = true;
  document.getElementById('sendBtn').disabled = true;
  setLoadingBar(20);
  var x = new XMLHttpRequest();
  x.open('POST', BASE + '/api/chat', true);
  x.setRequestHeader('Content-Type', 'application/json');
  x.timeout = 50000;
  x.onload = function() {
    removeTyping(tid); loading = false; setLoadingBar(100);
    document.getElementById('sendBtn').disabled = false;
    try {
      var data = JSON.parse(x.responseText);
      if (data.error) { addBubble('bot', '&#10060; ' + data.error, true); }
      else {
        addBubble('bot', data.reply); messages = data.messages || messages;
        var fl = data.reply.split(String.fromCharCode(10))[0].replace(/[*]/g,'').trim();
        if (fl) setStatus('ok', fl.length > 30 ? fl.slice(0,28) + '...' : fl);
      }
    } catch(e) { addBubble('bot', '&#10060; Yanıt alınamadı.', true); }
  };
  x.onerror = function() {
    removeTyping(tid); loading = false; setLoadingBar(100);
    document.getElementById('sendBtn').disabled = false;
    addBubble('bot', '&#10060; Sunucuya ulaşılamıyor.', true);
  };
  x.ontimeout = function() {
    removeTyping(tid); loading = false; setLoadingBar(100);
    document.getElementById('sendBtn').disabled = false;
    addBubble('bot', '&#10060; Zaman aşımı. Tekrar deneyin.', true);
  };
  x.send(JSON.stringify({messages: messages.slice(-20)}));
}

// ── BUBBLES ──
function addBubble(role, text, isErr) {
  var ms = document.getElementById('msgs');
  var row = document.createElement('div'); row.className = 'mrow' + (role==='user'?' user':'');
  var av = document.createElement('div'); av.className = 'mav' + (role==='user'?' u':'');
  av.textContent = role === 'user' ? '👤' : '⚡';
  var b = document.createElement('div'); b.className = 'bubble ' + role + (isErr?' err':'');
  b.innerHTML = fmt(text);
  var t = document.createElement('div'); t.className = 'btime';
  t.textContent = new Date().toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit'});
  b.appendChild(t);
  if (isErr && lastQuery) {
    var rb = document.createElement('button'); rb.className = 'retry-btn'; rb.textContent = '↩ Tekrar dene';
    (function(q){ rb.onclick = function(){ sendMessage(q); }; })(lastQuery);
    b.appendChild(rb);
  } else if (role === 'bot' && !isErr) {
    var cb = document.createElement('button'); cb.className = 'copy-btn'; cb.textContent = '📋 Kopyala';
    (function(txt2){ cb.onclick = function() {
      if (navigator.clipboard) { navigator.clipboard.writeText(txt2).then(function(){ toast('Kopyalandı ✓'); }); }
      else { var ta = document.createElement('textarea'); ta.value=txt2; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); toast('Kopyalandı ✓'); }
    }; })(text);
    b.appendChild(cb);
  }
  row.appendChild(av); row.appendChild(b); ms.appendChild(row); ms.scrollTop = ms.scrollHeight;
}

function addTyping() {
  var ms = document.getElementById('msgs');
  var row = document.createElement('div'); var id = 'ty' + Date.now();
  row.className = 'mrow'; row.id = id;
  var av = document.createElement('div'); av.className = 'mav'; av.textContent = '⚡';
  var tp = document.createElement('div'); tp.className = 'typing';
  tp.innerHTML = '<div class="tdot"></div><div class="tdot"></div><div class="tdot"></div>';
  row.appendChild(av); row.appendChild(tp); ms.appendChild(row); ms.scrollTop = ms.scrollHeight;
  return id;
}
function removeTyping(id) { var e = document.getElementById(id); if (e) e.remove(); }

function fmt(t) {
  var s = String(t)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/[*][*]([^*]+)[*][*]/g,'<b>$1</b>');
  var nl = String.fromCharCode(10);
  var parts = s.split(nl);
  var out = [];
  for (var i = 0; i < parts.length; i++) { if (i > 0) out.push('<br>'); out.push(parts[i]); }
  return out.join('');
}

var chips = document.querySelectorAll('.chip');
for (var ki = 0; ki < chips.length; ki++) {
  chips[ki].onclick = function() { sendMessage(this.textContent.trim()); };
}

document.getElementById('msgInp').onkeydown = function(e) {
  if (e.keyCode === 13 && !e.shiftKey) { e.preventDefault(); sendMessage(); }
};
document.getElementById('msgInp').oninput = function() {
  this.style.height = 'auto'; this.style.height = Math.min(this.scrollHeight, 90) + 'px';
};

document.getElementById('clearBtn').onclick = function() {
  messages = []; lastQuery = '';
  var ms = document.getElementById('msgs'); ms.innerHTML = '';
  var w = document.createElement('div'); w.className = 'welcome'; w.id = 'welcome';
  var ico = document.createElement('div'); ico.className = 'w-ico'; ico.textContent = '👋';
  var tit = document.createElement('div'); tit.className = 'w-title'; tit.textContent = 'Merhaba!';
  var sub = document.createElement('div'); sub.className = 'w-sub';
  sub.textContent = 'Bir karta tıklayın veya soru yazın.';
  w.appendChild(ico); w.appendChild(tit); w.appendChild(sub); ms.appendChild(w);
  document.getElementById('sugs').style.display = 'flex';
};

var _tt;
function toast(msg) {
  var el = document.getElementById('toast'); el.textContent = msg; el.classList.add('show');
  clearTimeout(_tt); _tt = setTimeout(function(){ el.classList.remove('show'); }, 3000);
}

if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js').catch(function(){}); }

if (/iphone|ipad|ipod/i.test(navigator.userAgent) && !window.navigator.standalone) {
  setTimeout(function(){ toast('💡 Ana ekrana ekle: Paylaş > Ana Ekrana Ekle'); }, 6000);
}

// ── INIT ──
setStatus('ok', 'Hazır');
setTimeout(pollStatus, 500);
// Load KPIs after boards are ready (wait a bit for first load)
setTimeout(loadAllKPIs, 1500);
</script>
</body>
</html>"""

# ── STARTUP ───────────────────────────────────────────────────────────────────
def _startup():
    print("Monday.com panoları yükleniyor...")
    _load_boards()

threading.Thread(target=_startup, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"► http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
