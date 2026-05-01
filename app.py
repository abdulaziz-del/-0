"""
ePing Legal Platform — Backend مع WTO API الرسمي
"""
import os, json, time, logging, threading, re
from datetime import datetime
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("wto")
app  = Flask(__name__)
CORS(app)

WTO_KEY  = os.getenv("WTO_API_KEY", "")
WTO_BASE = "https://api.wto.org"
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))
_cache = {"data": [], "at": 0}
_lock  = threading.Lock()

def cache_fresh():
    return (time.time() - _cache["at"]) < CACHE_TTL and _cache["data"]

def wto_headers():
    return {"subscription-key": WTO_KEY, "Accept": "application/json"}

def build_docs(symbol):
    enc  = requests.utils.quote(symbol, safe="")
    slug = symbol.replace("/", "-")
    return [
        {"name": f"النص الرسمي – {symbol}", "url": f"https://docs.wto.org/dol2fe/Pages/SS/directdoc.aspx?filename=q:/{symbol}.pdf&Open=True"},
        {"name": "البحث في وثائق WTO", "url": f"https://docs.wto.org/dol2fe/Pages/FE_Search/FE_S_S009-DP.aspx?language=E&CatalogueIdList={enc}"},
        {"name": "صفحة الإشعار على ePing", "url": f"https://eping.wto.org/en/Notification/Details/{slug}"},
    ]

def parse_item(it, ntype):
    sym   = it.get("documentSymbol", it.get("symbol", ""))
    prods = it.get("productsFreeText", "")
    if isinstance(prods, str):
        prods = [p.strip() for p in re.split(r"[,;،]", prods) if p.strip()][:5]
    return {
        "id": sym, "symbol": sym,
        "member": it.get("notifyingMember", ""),
        "memberCode": it.get("countryCode", ""),
        "date": (it.get("distributionDate",""))[:10],
        "type": ntype,
        "title": it.get("title", sym),
        "titleEn": it.get("titleEnglish", ""),
        "status": "مفتوح للتعليق" if it.get("isOpenForComments", False) else "منتهي",
        "products": prods,
        "commentDeadline": (it.get("commentDeadlineDate",""))[:10],
        "docs": build_docs(sym) if sym else [],
    }

def fetch_wto(endpoint, params):
    try:
        r = requests.get(f"{WTO_BASE}{endpoint}", headers=wto_headers(), params=params, timeout=25)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.error(f"WTO error: {e}")
    return None

def refresh(force=False):
    if not force and cache_fresh(): return
    with _lock:
        if not force and cache_fresh(): return
        all_data = []
        for ntype, ep in [("SPS","/v1/sps/notifications"),("TBT","/v1/tbt/notifications")]:
            for page in range(1, 7):
                data = fetch_wto(ep, {"ps": 50, "p": page})
                if not data: break
                rows = data if isinstance(data, list) else data.get("notifications", data.get("rows", []))
                if not rows: break
                all_data.extend([parse_item(it, ntype) for it in rows])
                time.sleep(0.5)
        if all_data:
            all_data.sort(key=lambda x: x.get("date",""), reverse=True)
            _cache["data"] = all_data
            _cache["at"]   = time.time()
            log.info(f"Cached {len(all_data)} notifications")

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
    if request.args.get("refresh")=="1": refresh(force=True)
    data = list(_cache["data"])
    t=request.args.get("type","").upper()
    st=request.args.get("status","")
    kw=request.args.get("keyword","").lower()
    pg=max(1,int(request.args.get("page",1)))
    rw=min(200,int(request.args.get("rows",100)))
    if t in ("SPS","TBT"): data=[n for n in data if n["type"]==t]
    if st=="open": data=[n for n in data if n["status"]=="مفتوح للتعليق"]
    if kw: data=[n for n in data if kw in n.get("title","").lower() or kw in n.get("symbol","").lower()]
    total=len(data); page_data=data[(pg-1)*rw:pg*rw]
    return jsonify({"notifications":page_data,"total":total,"page":pg,"rows":rw,"pages":(total+rw-1)//rw,"cached_at":datetime.fromtimestamp(_cache["at"]).isoformat() if _cache["at"] else None})

@app.route("/api/stats")
def stats():
    d=_cache["data"]
    return jsonify({"total":len(d),"sps":sum(1 for n in d if n["type"]=="SPS"),"tbt":sum(1 for n in d if n["type"]=="TBT"),"open":sum(1 for n in d if n["status"]=="مفتوح للتعليق")})

@app.route("/api/refresh", methods=["POST","GET"])
def force_refresh():
    refresh(force=True)
    return jsonify({"ok":True,"total":len(_cache["data"])})

@app.route("/api/test")
def test():
    data=fetch_wto("/v1/sps/notifications",{"ps":3,"p":1})
    return jsonify({"ok":bool(data),"data":data})

if __name__=="__main__":
    threading.Thread(target=bg,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT",5000)))
