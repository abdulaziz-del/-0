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

WTO_KEY    = os.getenv("WTO_API_KEY", "")
CLAUDE_KEY = os.getenv("CLAUDE_API_KEY", "")
CACHE_TTL  = 3600
_cache     = {"data": [], "at": 0}
_lock      = threading.Lock()


def cache_fresh():
    return (time.time() - _cache["at"]) < CACHE_TTL and bool(_cache["data"])


def build_docs(sym, doc_link="", dol_link=""):
    enc  = requests.utils.quote(sym, safe="")
    slug = sym.replace("/", "-")
    docs = []
    if doc_link:
        for url in doc_link.split(","):
            url = url.strip()
            if url and url.startswith("http"):
                docs.append({"name": "وثيقة الإشعار الرسمية (PDF)", "url": url})
    if dol_link:
        clean   = dol_link.replace("\\", "/")
        dol_url = clean if clean.startswith("http") else "https://docs.wto.org/dol2fe/Pages/SS/directdoc.aspx?filename=" + clean
        docs.append({"name": "النص الرسمي - " + sym, "url": dol_url})
    docs.append({"name": "البحث في وثائق WTO", "url": "https://docs.wto.org/dol2fe/Pages/FE_Search/FE_S_S009-DP.aspx?language=E&CatalogueIdList=" + enc})
    docs.append({"name": "صفحة الإشعار على ePing", "url": "https://eping.wto.org/en/Notification/Details/" + slug})
    return docs


def parse_item(it):
    sym      = it.get("documentSymbol", it.get("symbol", ""))
    area     = it.get("area", "")
    ntype    = "SPS" if (area == "SPS" or "/SPS/" in sym) else "TBT"
    title_en = it.get("titlePlain", it.get("title", it.get("titleEnglish", sym)))
    prods    = it.get("productsFreeTextPlain", it.get("productsFreeText", ""))
    if isinstance(prods, str):
        prods = [p.strip() for p in re.split(r"[,;،]", prods) if p.strip()][:5]
    elif not isinstance(prods, list):
        prods = []
    date_raw = it.get("distributionDate", it.get("date", ""))
    dead_raw = it.get("commentDeadlineDate", "")
    open_val = it.get("isOpenForComments", False)
    doc_link = it.get("notifiedDocumentLink", "")
    dol_link = it.get("dolLink", "")
    return {
        "id":              sym,
        "symbol":          sym,
        "member":          it.get("notifyingMember", it.get("member", "")),
        "memberCode":      it.get("notifyingMemberCode", it.get("countryCode", it.get("memberCode", ""))),
        "date":            date_raw[:10] if date_raw and len(date_raw) >= 10 else date_raw,
        "type":            ntype,
        "title":           title_en,
        "titleEn":         title_en,
        "titleAr":         "",
        "status":          "مفتوح للتعليق" if open_val else "منتهي",
        "products":        prods,
        "commentDeadline": dead_raw[:10] if dead_raw and len(dead_raw) >= 10 else dead_raw,
        "docs":            build_docs(sym, doc_link, dol_link) if sym else [],
    }


def extract_rows(d):
    if isinstance(d, list):
        return d
    if not isinstance(d, dict):
        return []
    for key in ["items", "notifications", "rows", "data", "results", "content"]:
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
    headers  = {"Ocp-Apim-Subscription-Key": WTO_KEY, "Accept": "application/json"}
    all_data = []
    for pg in range(1, 7):
        try:
            r = requests.get(
                "https://api.wto.org/eping/notifications/search",
                headers=headers,
                params={"page": pg, "pageSize": 50, "language": 1},
                timeout=25
            )
            if r.status_code != 200:
                break
            d    = r.json()
            rows = extract_rows(d)
            if not rows:
                break
            all_data.extend([parse_item(it) for it in rows])
            total = d.get("totalCount", d.get("total", 0)) if isinstance(d, dict) else 0
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
        data = fetch_data()
        if data:
            data.sort(key=lambda x: x.get("date", ""), reverse=True)
            _cache["data"] = data
            _cache["at"]   = time.time()
            log.info("Cached " + str(len(data)) + " notifications")


def bg():
    while True:
        try:
            refresh()
        except Exception as e:
            log.error("BG: " + str(e))
        time.sleep(CACHE_TTL)


@app.route("/")
def root():
    return jsonify({"notifications": len(_cache["data"]), "api_key": bool(WTO_KEY), "claude_key": bool(CLAUDE_KEY)})


@app.route("/api/notifications")
def notifs():
    if request.args.get("refresh") == "1":
        refresh(force=True)
    data = list(_cache["data"])
    t  = request.args.get("type", "").upper()
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
    total     = len(data)
    page_data = data[(pg - 1) * rw: pg * rw]
    cached_at = datetime.fromtimestamp(_cache["at"]).isoformat() if _cache["at"] else None
    return jsonify({"notifications": page_data, "total": total, "page": pg, "rows": rw, "pages": (total + rw - 1) // rw, "cached_at": cached_at})


@app.route("/api/stats")
def stats():
    d = _cache["data"]
    return jsonify({"total": len(d), "sps": sum(1 for n in d if n["type"] == "SPS"), "tbt": sum(1 for n in d if n["type"] == "TBT"), "open": sum(1 for n in d if n["status"] == "مفتوح للتعليق")})


@app.route("/api/refresh", methods=["GET", "POST"])
def force_refresh():
    refresh(force=True)
    return jsonify({"ok": True, "total": len(_cache["data"])})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    if not CLAUDE_KEY:
        return jsonify({"error": "No Claude key", "analysis": ""})
    try:
        n     = request.get_json()
        ntype = n.get("type", "")
        lines = [
            "أنت محلل قانوني متخصص في اتفاقيات منظمة التجارة العالمية.",
            "حلّل إشعار ePing:",
            "الرمز: " + n.get("symbol", "") + " | الدولة: " + n.get("member", "") + " | النوع: " + ("SPS - تدابير صحية" if ntype == "SPS" else "TBT - عوائق تقنية"),
            "التاريخ: " + n.get("date", "") + " | موعد التعليق: " + n.get("commentDeadline", ""),
            "العنوان: " + n.get("title", ""),
            "المنتجات: " + ", ".join(n.get("products", [])),
            "",
            "=== الملخص التنفيذي ===",
            "(4-5 جمل عن جوهر الإشعار وأهميته التجارية)",
            "",
            "=== التحليل القانوني ===",
            "الأساس القانوني في اتفاقية " + ("SPS المادة 5" if ntype == "SPS" else "TBT المادة 2"),
            "التوافق مع معايير " + ("Codex / OIE / IPPC" if ntype == "SPS" else "ISO / IEC"),
            "الأثر على التجارة الدولية",
            "حقوق الدول الأعضاء في الاعتراض",
            "",
            "=== التوصيات ===",
            "3-4 توصيات عملية للدول المتضررة.",
            "اكتب بالعربية الفصحى بأسلوب قانوني احترافي.",
        ]
        prompt = "\n".join(lines)
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-2.1", "max_tokens": 1000, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        log.info("Claude analyze status: " + str(r.status_code) + " resp: " + r.text[:300])
        if r.status_code == 200:
            text = r.json()["content"][0]["text"].strip()
            return jsonify({"analysis": text})
        return jsonify({"analysis": "", "error": r.text[:300]})
    except Exception as e:
        return jsonify({"error": str(e), "analysis": ""})


@app.route("/api/translate", methods=["POST"])
def translate():
    if not CLAUDE_KEY:
        return jsonify({"error": "No Claude key", "ar": ""})
    try:
        body = request.get_json()
        text = body.get("text", "")
        if not text:
            return jsonify({"ar": ""})
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-2.1", "max_tokens": 200, "messages": [{"role": "user", "content": "ترجم هذا العنوان إلى العربية الفصحى فقط بدون أي نص إضافي:\n" + text}]},
            timeout=15
        )
        if r.status_code == 200:
            ar = r.json()["content"][0]["text"].strip()
            return jsonify({"ar": ar})
        return jsonify({"ar": ""})
    except Exception as e:
        return jsonify({"error": str(e), "ar": ""})


@app.route("/api/test-claude")
def test_claude():
    import os
    key = CLAUDE_KEY
    if not key:
        return jsonify({"error": "No key", "key_len": 0})
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-2.1", "max_tokens": 10, "messages": [{"role": "user", "content": "test"}]},
            timeout=15
        )
        return jsonify({"status": r.status_code, "key_prefix": key[:12], "key_len": len(key), "resp": r.text[:300]})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/test")
def test():
    headers = {"Ocp-Apim-Subscription-Key": WTO_KEY, "Accept": "application/json"}
    try:
        r = requests.get("https://api.wto.org/eping/notifications/search", headers=headers, params={"page": 1, "pageSize": 2, "language": 1}, timeout=15)
        if r.ok:
            d    = r.json()
            rows = extract_rows(d)
            return jsonify({"status": r.status_code, "ok": True, "rows_count": len(rows), "sample": rows[0] if rows else None})
        return jsonify({"status": r.status_code, "ok": False, "error": r.text[:500]})
    except Exception as e:
        return jsonify({"error": str(e)})


if __name__ == "__main__":
    threading.Thread(target=bg, daemon=True).start()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
