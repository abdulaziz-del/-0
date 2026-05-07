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


def build_docs(sym, doc_link="", dol_link="", link_to_notif=""):
    docs = []
    if doc_link:
        urls = [u.strip() for u in doc_link.split(",") if u.strip()]
        for i, url in enumerate(urls):
            if url.startswith("http"):
                label = "تحميل PDF الرسمي" if len(urls) == 1 else "تحميل PDF (" + str(i+1) + ")"
                docs.append({"name": label, "url": url, "type": "pdf"})
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
    doc_link      = it.get("notifiedDocumentLink", "")
    dol_link      = it.get("dolLink", "")
    link_to_notif = it.get("linkToNotification", "")
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
        "docs":            build_docs(sym, doc_link, dol_link, link_to_notif) if sym else [],
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
            refresh_concerns()
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
    mc = request.args.get("member", "").lower()
    pg = max(1, int(request.args.get("page", 1)))
    rw = min(200, int(request.args.get("rows", 100)))
    if t in ("SPS", "TBT"):
        data = [n for n in data if n["type"] == t]
    if st == "open":
        data = [n for n in data if n["status"] == "مفتوح للتعليق"]
    if kw:
        data = [n for n in data if kw in n.get("title", "").lower() or kw in n.get("symbol", "").lower()]
    if mc:
        data = [n for n in data if mc in n.get("member", "").lower()]
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
        # محاولة جلب وقراءة PDF المرفق
        pdf_text = ""
        docs = n.get("docs", [])
        for doc in docs:
            url = doc.get("url", "")
            if "members.wto.org" in url and url.endswith(".pdf"):
                try:
                    pr = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                    if pr.status_code == 200 and len(pr.content) > 1000:
                        # استخراج نص بسيط من PDF
                        import re as re2
                        raw = pr.content.decode("latin-1", errors="ignore")
                        text_parts = re2.findall(r"[A-Za-z؀-ۿ][A-Za-z؀-ۿ\s,.\-:;()/]{20,}", raw)
                        if text_parts:
                            pdf_text = " ".join(text_parts[:50])[:2000]
                            break
                except:
                    pass
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
        if pdf_text:
            lines.append("")
            lines.append("نص من المستند الرسمي المرفق:")
            lines.append(pdf_text[:1000])
        prompt = "\n".join(lines)
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-opus-4-7", "max_tokens": 1000, "messages": [{"role": "user", "content": prompt}]},
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
            json={"model": "claude-opus-4-7", "max_tokens": 300, "messages": [{"role": "user", "content": "ترجم هذا العنوان إلى العربية الفصحى فقط بدون أي نص إضافي:\n" + text}]},
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
            json={"model": "claude-opus-4-7", "max_tokens": 10, "messages": [{"role": "user", "content": "test"}]},
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





# ─── قاعدة بيانات التنبيهات في الذاكرة ───
_alerts = []
_alert_id = 0

@app.route("/api/alerts", methods=["GET"])
def get_alerts():
    return jsonify({"alerts": _alerts})

@app.route("/api/alerts", methods=["POST"])
def add_alert():
    global _alert_id
    body = request.get_json() or {}
    _alert_id += 1
    alert = {
        "id": _alert_id,
        "type": body.get("type", "الكل"),
        "sector": body.get("sector", ""),
        "country": body.get("country", "جميع الدول"),
        "frequency": body.get("frequency", "فوري"),
        "active": True,
        "created": datetime.now().strftime("%Y-%m-%d")
    }
    _alerts.append(alert)
    return jsonify({"ok": True, "alert": alert})

@app.route("/api/alerts/<int:aid>", methods=["PUT"])
def toggle_alert(aid):
    for a in _alerts:
        if a["id"] == aid:
            a["active"] = not a["active"]
            return jsonify({"ok": True, "alert": a})
    return jsonify({"error": "not found"}), 404

@app.route("/api/alerts/<int:aid>", methods=["DELETE"])
def delete_alert(aid):
    global _alerts
    _alerts = [a for a in _alerts if a["id"] != aid]
    return jsonify({"ok": True})


_concerns_cache = {"data": [], "at": 0}
_concerns_lock  = threading.Lock()

def parse_concern(it):
    """تحويل بيانات الاهتمام التجاري من WTO API إلى صيغة موحدة"""
    domain    = it.get("domainId", it.get("domain", ""))
    ntype     = "SPS" if str(domain).upper() == "SPS" else "TBT"
    sym       = it.get("symbol", it.get("imsId", ""))
    title_en  = it.get("title", it.get("titleEnglish", str(sym)))
    raising   = it.get("raisingName", it.get("raisingMember", ""))
    supporting= it.get("supportingName", it.get("supportingMember", ""))
    subject   = it.get("subjectName", it.get("subject", ""))
    status    = it.get("status", it.get("reportedStatus", ""))
    first     = it.get("firstTimeRaised", it.get("firstRaised", ""))
    last      = it.get("lastTimeRaised",  it.get("lastRaised",  ""))
    times     = it.get("numberOfTimesRaised", it.get("timesRaised", 0))
    keywords  = it.get("keywords", [])
    if isinstance(keywords, list):
        kw_list = [k.get("item3", k) if isinstance(k, dict) else str(k) for k in keywords]
    else:
        kw_list = []
    docs_raw  = it.get("relatedDocuments", it.get("documents", []))
    docs_list = [d if isinstance(d, str) else d.get("symbol","") for d in docs_raw] if isinstance(docs_raw, list) else []
    return {
        "id":           str(sym),
        "symbol":       str(sym),
        "type":         ntype,
        "title":        title_en,
        "titleAr":      "",
        "raisingMember":  raising,
        "supporting":     supporting,
        "subject":        subject,
        "status":         status,
        "firstRaised":    first[:10] if first and len(first) >= 10 else first,
        "lastRaised":     last[:10]  if last  and len(last)  >= 10 else last,
        "timesRaised":    times,
        "keywords":       kw_list[:8],
        "relatedDocs":    docs_list[:5],
        "article":        "المادة 5.7" if ntype == "SPS" else "المادة 2.2",
        "agreement":      "اتفاقية SPS" if ntype == "SPS" else "اتفاقية TBT",
    }

def fetch_concerns():
    """
    جلب الاهتمامات التجارية من:
    1. ملف Excel الرسمي من WTO (مجاني، لا يحتاج API key)
    2. WTO API (إن توفّر المفتاح)
    """
    import io

    all_data = []

    # ── المحاولة الأولى: ملف Excel الرسمي من WTO ──
    xlsx_urls = [
        "https://eping.wto.org/NotificationExcelFiles/TradeConcerns_EN.xlsx",
        "https://eping.wto.org/NotificationExcelFiles/Concerns_EN.xlsx",
    ]
    for xurl in xlsx_urls:
        try:
            log.info("Downloading concerns Excel: %s", xurl)
            r = requests.get(xurl, timeout=60,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and len(r.content) > 5000:
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
                    ws = wb.active
                    rows_iter = iter(ws.rows)
                    headers_row = [str(c.value or "").strip() for c in next(rows_iter)]
                    log.info("Concerns Excel headers: %s", headers_row)
                    for row in rows_iter:
                        vals = [c.value for c in row]
                        it   = dict(zip(headers_row, vals))
                        parsed = parse_concern_excel(it, headers_row)
                        if parsed:
                            all_data.append(parsed)
                    log.info("Loaded %d concerns from Excel", len(all_data))
                    if all_data:
                        return all_data
                except ImportError:
                    # openpyxl غير متوفر، نحاول pandas
                    try:
                        import pandas as pd
                        df = pd.read_excel(io.BytesIO(r.content), engine="openpyxl")
                        for _, row in df.iterrows():
                            it = row.to_dict()
                            parsed = parse_concern_excel(it, list(df.columns))
                            if parsed:
                                all_data.append(parsed)
                        log.info("Loaded %d concerns from Excel (pandas)", len(all_data))
                        if all_data:
                            return all_data
                    except Exception as pe:
                        log.error("pandas parse error: %s", pe)
        except Exception as e:
            log.error("Concerns Excel error %s: %s", xurl, e)
            continue

    # ── المحاولة الثانية: WTO API (يحتاج مفتاح) ──
    if WTO_KEY:
        headers_api = {"Ocp-Apim-Subscription-Key": WTO_KEY, "Accept": "application/json"}
        endpoints = [
            "https://api.wto.org/eping/concerns/search",
            "https://api.wto.org/eping/v1/concerns",
            "https://api.wto.org/eping/tradeconcerns",
        ]
        for ep in endpoints:
            try:
                r = requests.get(ep, headers=headers_api,
                                 params={"page": 1, "pageSize": 100, "language": 1},
                                 timeout=25)
                log.info("Concerns API %s → %d", ep, r.status_code)
                if r.status_code == 200:
                    rows = extract_rows(r.json())
                    if rows:
                        all_data.extend([parse_concern(it) for it in rows])
                        if all_data:
                            return all_data
            except Exception as e:
                log.error("Concerns API error %s: %s", ep, e)

    return all_data


def parse_concern_excel(it, col_names):
    """تحويل صف Excel للاهتمامات التجارية إلى صيغة موحدة"""
    # خريطة الأعمدة الشائعة في ملف WTO
    def g(*keys):
        for k in keys:
            v = it.get(k)
            if v is not None and str(v).strip() not in ("", "nan", "None"):
                return str(v).strip()
        # بحث جزئي
        for col in col_names:
            for k in keys:
                if k.lower() in col.lower():
                    v = it.get(col)
                    if v is not None and str(v).strip() not in ("", "nan", "None"):
                        return str(v).strip()
        return ""

    domain  = g("Domain", "Committee", "Agreement", "Type")
    ntype   = "SPS" if "SPS" in domain.upper() else "TBT"
    sym_raw = g("IMS ID", "Id", "Symbol", "ID", "Concern ID")
    title   = g("Title", "Concern", "Subject", "Description")
    raising = g("Member Raising", "Raising Member", "Raising", "Member")
    support = g("Member Supporting", "Supporting", "Supporting Members")
    subj    = g("Subject", "Subject Matter", "Category")
    status  = g("Status", "Reported Status", "Resolution")
    first   = g("First Time Raised", "First Raised", "Date First Raised", "First Date")
    last    = g("Last Time Raised",  "Last Raised",  "Date Last Raised",  "Last Date")
    times_s = g("Number of Times Raised", "Times Raised", "Raised")
    kw_raw  = g("Keywords", "Key Words")

    if not title and not sym_raw:
        return None

    try:
        times = int(float(times_s)) if times_s else 0
    except Exception:
        times = 0

    kw_list = [k.strip() for k in re.split(r"[;,،]", kw_raw) if k.strip()][:8] if kw_raw else []

    sym = sym_raw or title[:30]
    return {
        "id":            sym,
        "symbol":        sym,
        "type":          ntype,
        "title":         title,
        "titleAr":       "",
        "raisingMember": raising,
        "supporting":    support,
        "subject":       subj,
        "status":        status,
        "firstRaised":   first[:10] if first and len(first) >= 10 else first,
        "lastRaised":    last[:10]  if last  and len(last)  >= 10 else last,
        "timesRaised":   times,
        "keywords":      kw_list,
        "relatedDocs":   [],
        "article":       "المادة 5.7" if ntype == "SPS" else "المادة 2.2",
        "agreement":     "اتفاقية SPS" if ntype == "SPS" else "اتفاقية TBT",
        "detailUrl":     "https://eping.wto.org/en/TradeConcerns/details?imsId=" + sym_raw + "&domainId=" + ntype if sym_raw else "",
    }

def refresh_concerns(force=False):
    ttl = 3600
    if not force and (time.time() - _concerns_cache["at"]) < ttl and _concerns_cache["data"]:
        return
    with _concerns_lock:
        if not force and (time.time() - _concerns_cache["at"]) < ttl and _concerns_cache["data"]:
            return
        data = fetch_concerns()
        if data:
            _concerns_cache["data"] = data
            _concerns_cache["at"]   = time.time()
            log.info("Cached %d trade concerns", len(data))

@app.route("/api/concerns", methods=["GET"])
def get_concerns():
    """جلب الاهتمامات التجارية الحقيقية من WTO ePing API"""
    if request.args.get("refresh") == "1" or not _concerns_cache["data"]:
        refresh_concerns(force=True)
    data = list(_concerns_cache["data"])
    t    = request.args.get("type", "").upper()
    kw   = request.args.get("keyword", "").lower()
    mc   = request.args.get("member", "").lower()
    st   = request.args.get("status", "").lower()
    pg   = max(1, int(request.args.get("page", 1)))
    rw   = min(100, int(request.args.get("rows", 50)))
    if t in ("SPS", "TBT"):
        data = [c for c in data if c["type"] == t]
    if kw:
        data = [c for c in data if kw in c.get("title","").lower()
                or kw in c.get("subject","").lower()
                or kw in " ".join(c.get("keywords",[])).lower()]
    if mc:
        data = [c for c in data if mc in c.get("raisingMember","").lower()
                or mc in c.get("supporting","").lower()]
    if st == "active":
        data = [c for c in data if c.get("status","").lower() in ("resolved","active","not resolved","","نشط")]
    total     = len(data)
    page_data = data[(pg-1)*rw: pg*rw]
    return jsonify({
        "concerns": page_data,
        "total":    total,
        "page":     pg,
        "pages":    (total + rw - 1) // rw,
        "cached_at": datetime.fromtimestamp(_concerns_cache["at"]).isoformat() if _concerns_cache["at"] else None
    })


@app.route("/api/analyze-concern", methods=["POST"])
def analyze_concern():
    if not CLAUDE_KEY:
        return jsonify({"error": "No Claude key", "analysis": ""})
    try:
        n = request.get_json()
        prompt = "\n".join([
            "أنت محلل قانوني متخصص في منازعات منظمة التجارة العالمية.",
            "حلّل هذا الاهتمام التجاري المُسجَّل في لجنة WTO:",
            "الرمز: " + str(n.get("symbol","")) + " | النوع: " + n.get("type","") + " | الاتفاقية: " + n.get("agreement",""),
            "العنوان: " + n.get("title",""),
            "الدولة المُثيرة: " + n.get("raisingMember",""),
            "الدول الداعمة: " + n.get("supporting",""),
            "الموضوع: " + n.get("subject",""),
            "الحالة: " + n.get("status",""),
            "عدد مرات الإثارة: " + str(n.get("timesRaised","")),
            "أول إثارة: " + n.get("firstRaised","") + " | آخر إثارة: " + n.get("lastRaised",""),
            "الكلمات المفتاحية: " + ", ".join(n.get("keywords",[])),
            "",
            "قدّم تحليلاً وفق الهيكل التالي:",
            "1. جوهر الاهتمام التجاري ومحله",
            "2. الأساس القانوني: " + n.get("article","") + " من " + n.get("agreement",""),
            "3. الدول المتضررة وحجم التأثير التجاري",
            "4. الحقوق القانونية المتاحة (DSU Article 4 - مشاورات، Panel Request)",
            "5. الموقف السعودي المقترح",
            "6. توصيات للتفاوض أو الاعتراض",
            "اكتب بالعربية الفصحى بأسلوب قانوني احترافي."
        ])
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-opus-4-7", "max_tokens": 1200, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        if r.status_code == 200:
            return jsonify({"analysis": r.json()["content"][0]["text"].strip()})
        return jsonify({"analysis": "", "error": r.text[:200]})
    except Exception as e:
        return jsonify({"error": str(e), "analysis": ""})


@app.route("/api/translate-batch", methods=["POST"])
def translate_batch_ep():
    if not CLAUDE_KEY:
        return jsonify({"translations": []})
    try:
        body = request.get_json()
        texts = body.get("texts", [])[:15]
        if not texts:
            return jsonify({"translations": []})
        numbered = "\n".join([str(i+1) + ". " + t for i, t in enumerate(texts)])
        prompt = "ترجم هذه العناوين من الانجليزية للعربية. اكتب الرقم ثم الترجمة فقط:\n" + numbered
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-opus-4-7", "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        if r.status_code == 200:
            resp = r.json()["content"][0]["text"].strip()
            result_lines = [l.strip() for l in resp.split("\n") if l.strip()]
            translations = []
            for line in result_lines:
                clean = re.sub(r"^[0-9]+[.)]\s*", "", line).strip()
                if clean:
                    translations.append(clean)
            if len(translations) == len(texts):
                return jsonify({"translations": translations})
        return jsonify({"translations": []})
    except Exception as e:
        return jsonify({"translations": [], "error": str(e)})


# ══════════════════════════════════════════════════
#  تحليل المستندات (PDF + نص)
# ══════════════════════════════════════════════════

def extract_pdf_text(content_bytes):
    """استخراج النص من PDF بطرق متعددة"""
    import io
    text = ""
    # طريقة 1: pypdf2 / pypdf
    for mod in ["pypdf", "PyPDF2"]:
        try:
            m = __import__(mod)
            reader = m.PdfReader(io.BytesIO(content_bytes))
            parts = []
            for page in reader.pages[:20]:
                try:
                    parts.append(page.extract_text() or "")
                except Exception:
                    pass
            text = "\n".join(parts).strip()
            if len(text) > 200:
                log.info("PDF extracted via %s: %d chars", mod, len(text))
                return text
        except Exception:
            pass
    # طريقة 2: استخراج نصي خام
    try:
        raw = content_bytes.decode("latin-1", errors="ignore")
        chunks = re.findall(r"[\x20-\x7E]{20,}", raw)
        text = " ".join(chunks[:300])[:6000]
        if len(text) > 100:
            return text
    except Exception:
        pass
    return text


@app.route("/api/fetch-doc", methods=["POST"])
def fetch_doc():
    """جلب محتوى مستند من URL (PDF أو HTML)"""
    body    = request.get_json() or {}
    url     = body.get("url", "").strip()
    doc_sym = body.get("symbol", "")
    if not url:
        return jsonify({"error": "No URL", "text": ""})
    try:
        headers_req = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/pdf,text/html,*/*"
        }
        r = requests.get(url, headers=headers_req, timeout=30, allow_redirects=True)
        if r.status_code != 200:
            return jsonify({"error": "HTTP " + str(r.status_code), "text": ""})
        content_type = r.headers.get("content-type", "")
        if "pdf" in content_type or url.lower().endswith(".pdf"):
            text = extract_pdf_text(r.content)
            return jsonify({"text": text[:8000], "type": "pdf", "size": len(r.content), "symbol": doc_sym})
        else:
            # HTML - استخراج النص
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.content, "html.parser")
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            text = re.sub(r"\n{3,}", "\n\n", text)
            return jsonify({"text": text[:8000], "type": "html", "symbol": doc_sym})
    except Exception as e:
        return jsonify({"error": str(e), "text": ""})


@app.route("/api/analyze-doc", methods=["POST"])
def analyze_doc():
    """تحليل مستند كامل (PDF أو نص) بالذكاء الاصطناعي"""
    if not CLAUDE_KEY:
        return jsonify({"error": "No Claude key", "analysis": ""})
    body     = request.get_json() or {}
    url      = body.get("url", "").strip()
    doc_text = body.get("text", "").strip()
    symbol   = body.get("symbol", "")
    title    = body.get("title", "")
    ntype    = body.get("type", "")
    context  = body.get("context", "notification")  # notification | concern

    # إذا لم يُرسَل نص، نحاول جلب المستند
    if not doc_text and url:
        try:
            headers_req = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, headers=headers_req, timeout=30, allow_redirects=True)
            if r.status_code == 200:
                ct = r.headers.get("content-type", "")
                if "pdf" in ct or url.lower().endswith(".pdf"):
                    doc_text = extract_pdf_text(r.content)
                else:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(r.content, "html.parser")
                    doc_text = soup.get_text(separator="\n", strip=True)[:6000]
        except Exception as e:
            log.error("Doc fetch for analysis: %s", e)

    if not doc_text:
        return jsonify({"error": "تعذّر قراءة المستند أو النص فارغ", "analysis": ""})

    if context == "concern":
        prompt = "\n".join([
            "أنت محلل قانوني متخصص في اتفاقيات منظمة التجارة العالمية (WTO).",
            "المستند المرفق يتعلق باهتمام تجاري:" ,
            "الرمز: " + symbol + " | العنوان: " + title + " | النوع: " + ntype,
            "",
            "=== نص المستند ===",
            doc_text[:5000],
            "",
            "=== التحليل المطلوب ===",
            "1. ملخص المستند: ما هو جوهر هذا المستند؟",
            "2. موقف الأطراف: ما هي مواقف الدول المعنية؟",
            "3. المتطلبات القانونية المحددة",
            "4. الأثر على التجارة الدولية وعلى المملكة العربية السعودية تحديداً",
            "5. خيارات الاستجابة المتاحة (مشاورات، تعليقات، Panel)",
            "6. التوصيات العملية",
            "اكتب بالعربية الفصحى بأسلوب قانوني احترافي.",
        ])
    else:
        prompt = "\n".join([
            "أنت محلل قانوني متخصص في اتفاقيات منظمة التجارة العالمية (WTO).",
            "المستند المرفق هو إشعار رسمي في إطار اتفاقية " + ("SPS" if ntype == "SPS" else "TBT") + ":",
            "الرمز: " + symbol + " | العنوان: " + title,
            "",
            "=== نص المستند ===",
            doc_text[:5000],
            "",
            "=== التحليل المطلوب ===",
            "1. ملخص المتطلبات: ما هي الاشتراطات الجديدة؟",
            "2. المنتجات والأسواق المتأثرة",
            "3. التحليل القانوني: التوافق مع " + ("معايير Codex/OIE/IPPC | المادة 5 SPS" if ntype == "SPS" else "معايير ISO/IEC | المادة 2 TBT"),
            "4. الأثر على صادرات المملكة العربية السعودية",
            "5. هل يستوجب تقديم تعليقات رسمية؟ وكيف؟",
            "6. التوصيات العملية (3-5 توصيات)",
            "اكتب بالعربية الفصحى بأسلوب قانوني احترافي.",
        ])

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-opus-4-7", "max_tokens": 1800,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=45
        )
        if r.status_code == 200:
            return jsonify({"analysis": r.json()["content"][0]["text"].strip()})
        return jsonify({"analysis": "", "error": r.text[:200]})
    except Exception as e:
        return jsonify({"error": str(e), "analysis": ""})


@app.route("/api/concerns/refresh", methods=["GET", "POST"])
def refresh_concerns_ep():
    refresh_concerns(force=True)
    return jsonify({"ok": True, "total": len(_concerns_cache["data"])})


if __name__ == "__main__":
    threading.Thread(target=bg, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
