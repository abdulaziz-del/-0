import os
import time
import logging
import threading
import re
import requests
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("eping")
app = Flask(__name__)
CORS(app)

WTO_KEY = os.getenv("WTO_API_KEY", "")
CACHE_TTL = 3600
_cache = {"data": [], "at": 0}
_lock = threading.Lock()


def cache_fresh():
    return (time.time() - _cache["at"]) < CACHE_TTL and bool(_cache["data"])


def build_docs(sym):
    enc = requests.utils.quote(sym, safe="")
    slug = sym.replace("/", "-")
    return [
        {"name": "النص الرسمي - " + sym, "url": "https://docs.wto.org/dol2fe/Pages/SS/directdoc.aspx?filename=q:/" + sym + ".pdf&Open=True"},
        {"name": "البحث في وثائق WTO", "url": "https://docs.wto.org/dol2fe/Pages/FE_Search/FE_S_S009-DP.aspx?language=E&CatalogueIdList=" + enc},
        {"name": "صفحة ePing", "url": "https://eping.wto.org/en/Notification/Details/" + slug},
    ]


def parse_item(it):
    sym = it.get("documentSymbol", it.get("symbol", ""))
    area = it.get("area", "")
    if area == "SPS" or "/SPS/" in sym:
        ntype = "SPS"
    else:
        ntype = "TBT"
    prods = it.get("productsFreeTextPlain", it.get("productsFreeText", ""))
    if isinstance(prods, str):
        prods = [p.strip() for p in re.split(r"[,;،]", prods) if p.strip()][:5]
    elif not isinstance(prods, list):
        prods = []
    date_raw = it.get("distributionDate", it.get("date", ""))
    dead_raw = it.get("commentDeadlineDate", "")
    open_val = it.get("isOpenForComments", False)
    return {
        "id": sym,
        "symbol": sym,
        "member": it.get("notifyingMember", it.get("member", "")),
        "memberCode": it.get("notifyingMemberCode", it.get("countryCode", it.get("memberCode", ""))),
        "date": date_raw[:10] if date_raw and len(date_raw) >= 10 else date_raw,
        "type": ntype,
        "title": it.get("titlePlain", it.get("title", sym)),
        "titleEn": it.get("titlePlain", it.get("titleEnglish", "")),
        "status": "مفتوح للتعليق" if open_val else "منتهي",
        "products": prods,
        "commentDeadline": dead_raw[:10] if dead_raw and len(dead_raw) >= 10 else dead_raw,
        "docs": build_docs(sym) if sym else [],
    }


def extract_rows(d):
    if isinstance(d, list):
        return d
    if not isinstance(d, dict):
        return []
    log.info("Response keys: " + str(list(d.keys())))
    for key in ["notifications", "rows", "items", "data", "results", "content"]:
        val = d.get(key)
        if val is not None:
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                for k2 in ["items", "notifications", "rows", "data"]:
                    v2 = val.get(k2)
                    if isinstance(v2, list):
                        return v2
    return []


def fetch_data():
    headers = {
        "Ocp-Apim-Subscription-Key": WTO_KEY,
        "Accept": "application/json"
    }
    all_data = []
    for pg in range(1, 7):
        try:
            r = requests.get(
                "https://api.wto.org/eping/notifications/search",
                headers=headers,
                params={"page": pg, "pageSize": 50, "language": 1},
                timeout=25
            )
            log.info("ePing API page " + str(pg) + " status: " + str(r.status_code))
            if r.status_code != 200:
                log.error("Error: " + r.text[:300])
                break
            d = r.json()
            rows = extract_rows(d)
            if not rows:
                log.info("No rows found in response")
                break
            all_data.extend([parse_item(it) for it in rows])
            total = 0
            if isinstance(d, dict):
                total = d.get("total", d.get("totalCount", d.get("count", 0)))
            if total and len(all_data) >= total:
                break
            time.sleep(0.5)
        except Exception as e:
            log.error("Fetch error: " + str(e))
            break
    return all_data


def refresh(force=False):
    if not force and cache_fresh():
        return
    with _lock:
        if not force and cache_fresh():
            return
        log.info("Fetching from ePing API...")
        data = fetch_data()
        if data:
            data.sort(key=lambda x: x.get("date", ""), reverse=True)
            _cache["data"] = data
            _cache["at"] = time.time()
            log.info("Cached " + str(len(data)) + " notifications")
        else:
            log.warning("No data returned from ePing API")


def bg():
    while True:
        try:
            refresh()
        except Exception as e:
            log.error("BG error: " + str(e))
        time.sleep(CACHE_TTL)


@app.route("/")
def root():
    return jsonify({
        "notifications": len(_cache["data"]),
        "api_key": bool(WTO_KEY)
    })


@app.route("/api/notifications")
def notifs():
    if request.args.get("refresh") == "1":
        refresh(force=True)
    data = list(_cache["data"])
    t = request.args.get("type", "").upper()
    st = request.args.get("status", "")
    kw = request.args.get("keyword", "").lower()
    pg = max(1, int(request.args.get("page", 1)))
    rw = min(200, int(request.args.get("rows", 100)))
    if t in ("SPS", "TBT"):
        data = [n for n in data if n["type"] == t]
    if st == "open":
        data = [n for n in data if n["status"] == "مفتوح للتعليق"]
    if kw:
        data = [n for n in data if kw in n.get("title", "").lower() or kw in n.get("symbol", "").lower()]
    total = len(data)
    page_data = data[(pg - 1) * rw: pg * rw]
    cached_at = datetime.fromtimestamp(_cache["at"]).isoformat() if _cache["at"] else None
    return jsonify({
        "notifications": page_data,
        "total": total,
        "page": pg,
        "rows": rw,
        "pages": (total + rw - 1) // rw,
        "cached_at": cached_at
    })


@app.route("/api/stats")
def stats():
    d = _cache["data"]
    return jsonify({
        "total": len(d),
        "sps": sum(1 for n in d if n["type"] == "SPS"),
        "tbt": sum(1 for n in d if n["type"] == "TBT"),
        "open": sum(1 for n in d if n["status"] == "مفتوح للتعليق")
    })


@app.route("/api/refresh", methods=["GET", "POST"])
def force_refresh():
    refresh(force=True)
    return jsonify({"ok": True, "total": len(_cache["data"])})


@app.route("/api/test")
def test():
    headers = {
        "Ocp-Apim-Subscription-Key": WTO_KEY,
        "Accept": "application/json"
    }
    try:
        r = requests.get(
            "https://api.wto.org/eping/notifications/search",
            headers=headers,
            params={"page": 1, "pageSize": 2, "language": 1},
            timeout=15
        )
        if r.ok:
            d = r.json()
            rows = extract_rows(d)
            return jsonify({
                "status": r.status_code,
                "ok": True,
                "keys": list(d.keys()) if isinstance(d, dict) else str(type(d)),
                "rows_count": len(rows),
                "sample": rows[0] if rows else None
            })
        else:
            return jsonify({"status": r.status_code, "ok": False, "error": r.text[:500]})
    except Exception as e:
        return jsonify({"error": str(e)})


if __name__ == "__main__":
    threading.Thread(target=bg, daemon=True).start()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
