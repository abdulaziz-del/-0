"""
ePing Legal Platform — Backend كامل للنشر على Railway
"""
import os, json, time, logging, threading, re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("eping")

app = Flask(__name__)
CORS(app)

BASE      = "https://eping.wto.org"
EMAIL     = os.getenv("EPING_EMAIL", "")
PASSWORD  = os.getenv("EPING_PASSWORD", "")
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/en/Search/AllInformation",
}

_cache = {"data": [], "at": 0}
_lock  = threading.Lock()

def cache_fresh():
    return (time.time() - _cache["at"]) < CACHE_TTL and _cache["data"]

class EPing:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)

    def login(self):
        if not EMAIL or not PASSWORD:
            return False
        try:
            r = self.s.get(f"{BASE}/en/Account/Login", timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            tok  = soup.find("input", {"name": "__RequestVerificationToken"})
            r2   = self.s.post(f"{BASE}/en/Account/Login", timeout=15, data={
                "Email": EMAIL, "Password": PASSWORD,
                "__RequestVerificationToken": tok["value"] if tok else ""
            }, allow_redirects=True)
            return "Account/Login" not in r2.url
        except Exception as e:
            log.error(f"Login error: {e}")
            return False

    def search(self, page=1, rows=50, ntype="", keyword="", open_only=False):
        payload = {
            "page": page, "rows": rows,
            "sidx": "distributionDate", "sord": "desc",
            "freeText": keyword,
            "agreementIds": {"SPS":[1],"TBT":[2]}.get(ntype.upper(),[]),
            "isOpenForComments": open_only,
            "memberIds": [], "fromDate": "", "toDate": "",
        }
        for ep in ["/api/Notification/Search", "/api/notifications/search", "/Search/GetNotifications"]:
            try:
                r = self.s.post(f"{BASE}{ep}", json=payload, timeout=20)
                if r.status_code == 200 and r.headers.get("Content-Type","").startswith("application/json"):
                    d = r.json()
                    rows_data = d.get("rows", d.get("notifications", d if isinstance(d,list) else []))
                    if rows_data:
                        return self._parse(rows_data), d.get("total", len(rows_data))
            except: pass
        return self._scrape_html(page, rows, ntype, keyword), 0

    def _scrape_html(self, page, rows, ntype, keyword):
        try:
            params = {"page": page, "freeText": keyword}
            if ntype == "SPS": params["agreementIds"] = 1
            elif ntype == "TBT": params["agreementIds"] = 2
            r = self.s.get(f"{BASE}/en/Search/AllInformation", params=params, timeout=20)
            soup = BeautifulSoup(r.text, "html.parser")
            for sc in soup.find_all("script"):
                txt = sc.string or ""
                m = re.search(r'"notifications"\s*:\s*(\[.*?\])', txt, re.S)
                if m:
                    try: return self._parse(json.loads(m.group(1)))
                    except: pass
            return []
        except Exception as e:
            log.error(f"Scrape error: {e}")
            return []

    def doc_links(self, symbol):
        return [
            {"name": f"النص الرسمي – {symbol}", "url": f"https://docs.wto.org/dol2fe/Pages/SS/directdoc.aspx?filename=q:/{symbol}.pdf&Open=True"},
            {"name": "البحث في وثائق WTO", "url": f"https://docs.wto.org/dol2fe/Pages/FE_Search/FE_S_S009-DP.aspx?language=E&CatalogueIdList={requests.utils.quote(symbol)}"},
            {"name": "صفحة الإشعار على ePing", "url": f"{BASE}/en/Notification/Details/{symbol.replace('//','-')}"}
        ]

    def _parse(self, rows):
        out = []
        for it in rows:
            sym = it.get("documentSymbol", it.get("symbol",""))
            ntype = "SPS" if ("/SPS/" in sym or it.get("agreementId")==1) else "TBT"
            open_ = it.get("isOpenForComments", False)
            prods = it.get("productsFreeText", it.get("products",""))
            if isinstance(prods, str):
                prods = [p.strip() for p in re.split(r"[,;،]", prods) if p.strip()][:5]
            raw_date = it.get("distributionDate", it.get("date",""))
            raw_dead = it.get("commentDeadlineDate", it.get("commentDeadline",""))
            out.append({
                "id": sym, "symbol": sym,
                "member": it.get("notifyingMember", it.get("member","")),
                "memberCode": it.get("countryCode", it.get("memberCode","")),
                "date": raw_date[:10] if len(raw_date)>=10 else raw_date,
                "type": ntype,
                "title": BeautifulSoup(it.get("title",""), "html.parser").get_text().strip(),
                "titleEn": it.get("titleEn",""),
                "status": "مفتوح للتعليق" if open_ else "منتهي",
                "products": prods,
                "hs_codes": it.get("hsCodes",[]),
                "commentDeadline": raw_dead[:10] if len(raw_dead)>=10 else raw_dead,
                "docs": self.doc_links(sym) if sym else [],
            })
        return out

    def fetch_all(self, max_pages=6, rows=50, **kw):
        all_ = []
        for p in range(1, max_pages+1):
            batch, total = self.search(page=p, rows=rows, **kw)
            all_.extend(batch)
            if not batch or len(all_) >= total: break
            time.sleep(0.7)
        return all_

def refresh(force=False):
    if not force and cache_fresh(): return
    with _lock:
        if not force and cache_fresh(): return
        ep = EPing()
        if EMAIL and PASSWORD: ep.login()
        data = ep.fetch_all(max_pages=6, rows=50)
        if data:
            _cache["data"] = data
            _cache["at"] = time.time()

def bg_refresh():
    while True:
        try: refresh()
        except: pass
        time.sleep(CACHE_TTL)

@app.route("/")
def root():
    return jsonify({"name": "ePing API", "notifications": len(_cache["data"])})

@app.route("/api/notifications")
def notifs():
    if request.args.get("refresh") == "1":
        refresh(force=True)
    data = list(_cache["data"])
    t  = request.args.get("type","").upper()
    st = request.args.get("status","")
    kw = request.args.get("keyword","").lower()
    pg = int(request.args.get("page",1))
    rw = int(request.args.get("rows",100))
    if t in ("SPS","TBT"): data = [n for n in data if n["type"]==t]
    if st == "open": data = [n for n in data if n["status"]=="مفتوح للتعليق"]
    if kw: data = [n for n in data if kw in n.get("title","").lower() or kw in n.get("symbol","").lower()]
    total = len(data)
    data = data[(pg-1)*rw : pg*rw]
    return jsonify({"notifications": data, "total": total, "page": pg, "rows": rw, "pages": (total+rw-1)//rw, "cached_at": datetime.fromtimestamp(_cache["at"]).isoformat() if _cache["at"] else None})

@app.route("/api/stats")
def stats():
    d = _cache["data"]
    return jsonify({"total": len(d), "sps": sum(1 for n in d if n["type"]=="SPS"), "tbt": sum(1 for n in d if n["type"]=="TBT"), "open": sum(1 for n in d if n["status"]=="مفتوح للتعليق")})

@app.route("/api/refresh", methods=["POST"])
def force_refresh():
    refresh(force=True)
    return jsonify({"ok": True, "total": len(_cache["data"])})

if __name__ == "__main__":
    threading.Thread(target=bg_refresh, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
