import os, time, logging, threading, re, requests
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("wto")
app = Flask(__name__)
CORS(app)

WTO_KEY   = os.getenv("WTO_API_KEY", "")
CACHE_TTL = 3600
_cache    = {"data": [], "at": 0}
_lock     = threading.Lock()

def cache_fresh():
    return (time.time() - _cache["at"]) < CACHE_TTL and _cache["data"]

def build_docs(sym):
    enc  = requests.utils.quote(sym, safe="")
    slug = sym.replace("/", "-")
    return [
        {"name": f"النص الرسمي – {sym}", "url": f"https://docs.wto.org/dol2fe/Pages/SS/directdoc.aspx?filename=q:/{sym}.pdf&Open=True"},
        {"name": "البحث في وثائق WTO",   "url": f"https://docs.wto.org/dol2fe/Pages/FE_Search/FE_S_S009-DP.aspx?language=E&CatalogueIdList={enc}"},
        {"name": "صفحة ePing",           "url": f"https://eping.wto.org/en/Notification/Details/{slug}"},
    ]

def fetch_data():
    all_data = []
    headers  = {"Ocp-Apim-Subscription-Key": WTO_KEY, "Accept": "application/json"}

    # endpoints المختلفة للتجربة
    endpoints = [
        ("https://api.wto.org/sps/v1/notifications",   "SPS"),
        ("https://api.wto.org/tbt/v1/notifications",   "TBT"),
        ("https://api.wto.org/v1/sps/notifications",   "SPS"),
        ("https://api.wto.org/v1/tbt/notifications",   "TBT"),
    ]

    for url, ntype in endpoints:
        try:
            r = requests.get(url, headers=headers, params={"ps": 50, "p": 1}, timeout=20)
            log.info(f"{url} → {r.status_code}")
            if r.status_code == 200:
                d    = r.json()
                rows = d if isinstance(d, list) else d.get("notifications", d.get("rows", d.get("items", [])))
                if rows:
                    for it in rows:
                        sym  = it.get("documentSymbol", it.get("symbol", ""))
                        prods = it.get("productsFreeText", "")
                        if isinstance(prods, str):
                            prods = [p.strip() for p in re.split(r"[,;،]", prods) if p.strip()][:5]
                        all_data.append({
                            "id":              sym,
                            "symbol":          sym,
                            "member":          it.get("notifyingMember", it.get("member", "")),
                            "memberCode":      it.get("countryCode", it.get("memberCode", "")),
                            "date":            (it.get("distributionDate", it.get("date", "")))[:10],
                            "type":            ntype,
                            "title":           it.get("title", sym),
                            "titleEn":         it.get("titleEnglish", ""),
                            "status":          "مفتوح للتعليق" if it.get("isOpenForComments", False) else "منتهي",
                            "products":        prods,
                            "commentDeadline": (it.get("commentDeadlineDate", ""))[:10],
                            "docs":            build_docs(sym) if sym else [],
                        })
                    log.info(f"✓ {ntype}: {len(rows)} items from {url}")
                    break
        except Exception as e:
            log.error(f"{url}: {e}")

    return all_data

def refresh(force=False):
    if not force and cache_fresh(): return
    with _lock:
        if not force and cache_fresh(): return
        log.info("Fetching from WTO API...")
        data = fetch_data()
        if data:
            data.sort(key=lambda x: x.get("date", ""), reverse=True)
            _cache["data"] = data
            _cache["at"]   = time.time()
            log.info(f"✓ Cached {len(data)} notifications")
        else:
            log.warning("No data — check API key/endpoints in Render logs")

def bg():
    while True:
        try: refresh()
        except: pass
        time.sleep(CACHE_TTL)

@app.route("/")
def root():
    return jsonify({"notifications": len(_cache["data"]), "api_key": bool(WTO_KEY)})

@app.route("/api/notifications")
def notifs():
    if request.args.get("refresh") == "1": refresh(force=True)
    data = list(_cache["data"])
    t  = request.args.get("type", "").upper()
    st = request.args.get("status", "")
    kw = request.args.get("keyword", "").lower()
    pg = max(1, int(request.args.get("page", 1)))
    rw = min(200, int(request.args.get("rows", 100)))
    if t in ("SPS","TBT"): data = [n for n in data if n["type"]==t]
    if st == "open":        data = [n for n in data if n["status"]=="مفتوح للتعليق"]
    if kw:                  data = [n for n in data if kw in n.get("title","").lower() or kw in n.get("symbol","").lower()]
    total = len(data)
    return jsonify({"notifications": data[(pg-1)*rw:pg*rw], "total": total, "page": pg, "rows": rw, "pages": (total+rw-1)//rw, "cached_at": datetime.fromtimestamp(_cache["at"]).isoformat() if _cache["at"] else None})

@app.route("/api/stats")
def stats():
    d = _cache["data"]
    return jsonify({"total": len(d), "sps": sum(1 for n in d if n["type"]=="SPS"), "tbt": sum(1 for n in d if n["type"]=="TBT"), "open": sum(1 for n in d if n["status"]=="مفتوح للتعليق")})

@app.route("/api/refresh", methods=["GET","POST"])
def force_refresh():
    refresh(force=True)
    return jsonify({"ok": True, "total": len(_cache["data"])})

@app.route("/api/test")
def test():
    headers = {"Ocp-Apim-Subscription-Key": WTO_KEY, "Accept": "application/json"}
    results = {}
    for url in ["https://api.wto.org/sps/v1/notifications", "https://api.wto.org/v1/sps/notifications"]:
        try:
            r = requests.get(url, headers=headers, params={"ps":3,"p":1}, timeout=15)
            results[url] = {"status": r.status_code, "data": r.json() if r.ok else r.text[:200]}
        except Exception as e:
            results[url] = {"error": str(e)}
    return jsonify(results)

if __name__ == "__main__":
    threading.Thread(target=bg, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
