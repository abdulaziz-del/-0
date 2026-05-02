import os, time, logging, threading, re, requests
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("eping")
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

def parse_item(it):
    sym   = it.get("documentSymbol", it.get("symbol", ""))
    ntype = "SPS" if "/SPS/" in sym else "TBT"
    prods = it.get("productsFreeText", "")
    if isinstance(prods, str):
        prods = [p.strip() for p in re.split(r"[,;،]", prods) if p.strip()][:5]
    return {
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
    }

def fetch_data():
   headers = {"Ocp-Apim-Subscription-Key": WTO_KEY, "Accept": "application/json"}
    all_data = []
    for pg in range(1, 7):
        try:
            r = requests.get(
                "https://api.wto.org/eping/notifications/search",
                headers=headers,
                params={"page": pg, "pageSize": 50, "language": "ar"},
                timeout=25
            )
            log.info(f"ePing API page {pg} → {r.status_code}")
            if r.status_code != 200:
                log.error(f"Error: {r.text[:300]}")
                break
            d    = r.json()
            rows = d if isinstance(d, list) else d.get("notifications", d.get("rows", d.get("items", [])))
            if not rows:
                break
            all_data.extend([parse_item(it) for it in rows])
            total = d.get("total", 0) if isinstance(d, dict) else 0
            if total and len(all_data) >= total:
                break
            time.sleep(0.5)
        except Exception as e:
            log.error(f"Fetch error: {e}")
            break
    return all_data

def refresh(force=False):
    if not force and cache_fresh(): return
    with _lock:
        if not force and cache_fresh(): return
        data = fetch_data()
        if data:
            data.sort(key=lambda x: x.get("date", ""), reverse=True)
            _cache["data"] = data
            _cache["at"]   = time.time()
            log.info(f"✓ Cached {len(data)} notifications")

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
    t  = request.args.get("type","").upper()
    st = request.args.get("status","")
    kw = request.args.get("keyword","").lower()
    pg = max(1, int(request.args.get("page",1)))
    rw = min(200, int(request.args.get("rows",100)))
    if t in ("SPS","TBT"): data=[n for n in data if n["type"]==t]
    if st=="open":          data=[n for n in data if n["status"]=="مفتوح للتعليق"]
    if kw:                  data=[n for n in data if kw in n.get("title","").lower() or kw in n.get("symbol","").lower()]
    total=len(data)
    return jsonify({"notifications":data[(pg-1)*rw:pg*rw],"total":total,"page":pg,"rows":rw,"pages":(total+rw-1)//rw,"cached_at":datetime.fromtimestamp(_cache["at"]).isoformat() if _cache["at"] else None})

@app.route("/api/stats")
def stats():
    d=_cache["data"]
    return jsonify({"total":len(d),"sps":sum(1 for n in d if n["type"]=="SPS"),"tbt":sum(1 for n in d if n["type"]=="TBT"),"open":sum(1 for n in d if n["status"]=="مفتوح للتعليق")})

@app.route("/api/refresh", methods=["GET","POST"])
def force_refresh():
    refresh(force=True)
    return jsonify({"ok":True,"total":len(_cache["data"])})

@app.route("/api/test")
def test():
    headers={"subscription-key":WTO_KEY,"Accept":"application/json"}
    try:
        r=requests.get("https://api.wto.org/eping/notifications/search",headers=headers,params={"page":1,"pageSize":3},timeout=15)
        return jsonify({"status":r.status_code,"data":r.json() if r.ok else r.text[:500]})
    except Exception as e:
        return jsonify({"error":str(e)})

if __name__=="__main__":
    threading.Thread(target=bg,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT",5000)))
