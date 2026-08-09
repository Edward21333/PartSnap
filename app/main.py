import os, json, base64, re
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DATA = BASE / "data"

app = FastAPI(title="PartSnap MVP v0.16")

def api_error(code: str, message: str, retryable: bool = False, status: int = 500):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=status,
        content={"ok": False, "error": {"code": code, "message": message, "retryable": retryable}}
    )

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

def load_json(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))

@app.get("/")
def root():
    return FileResponse(STATIC / "index.html")

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": "0.16",
        "ai_enabled": bool(os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_MODEL")),
        "regional_pricing": True
    }

def demo_result(make, model, year, engine):
    return {
        "mode":"demo",
        "summary":f"Демо-анализ для {make} {model} {year} {engine}.",
        "candidates":[
            {"name":"Шланг гидроусилителя руля","confidence":0.82,"oem_hint":"1J0 422 887 (family)","part_class":"hose","search_query_en":"power steering hose 1J0 422 887","reason":"Форма похожа на магистраль ГУР; точный OEM требует каталога/VIN."},
            {"name":"Обратный шланг ГУР","confidence":0.55,"oem_hint":"","reason":"Похожая категория, нужна сверка."}
        ]
    }

@app.post("/api/analyze")
async def analyze(
    image: Optional[UploadFile] = File(None),
    make: str = Form(""), model: str = Form(""), year: str = Form(""),
    engine: str = Form(""), vin: str = Form("")
):
    api_key = os.getenv("OPENAI_API_KEY")
    model_name = os.getenv("OPENAI_MODEL")
    if not api_key or not model_name or image is None:
        return demo_result(make, model, year, engine)

    raw = await image.read()
    mime = image.content_type or "image/jpeg"
    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    prompt = f"""
Ты модуль PartSnap. Проанализируй фото автомобильной детали.
Автомобиль: {make} {model}, {year}, двигатель {engine}, VIN {vin or 'не указан'}.

Очень важно:
- не выдумывай OEM/артикул;
- если на детали реально видна маркировка, перепиши её максимально точно;
- если номер не читается полностью, укажи только видимую часть;
- различай "visible_marking" (что реально видно на фото) и "oem_hint" (вероятный номер/семейство, только если есть основания);
- если точная совместимость не подтверждена, это нормально.

Верни только JSON:
{{
  "summary":"...",
  "visible_marking":"что реально читается на детали или пустая строка",
  "candidates":[
    {{
      "name":"...",
      "confidence":0.0,
      "oem_hint":"...",
      "part_class":"battery|wiper|cv_joint|brake|filter|lamp|sensor|hose|other",
      "search_query_en":"короткий поисковый запрос НА АНГЛИЙСКОМ/НЕМЕЦКОМ без лишних слов, например Bosch S4 Autobatterie",
      "specs":{{
        "voltage_v":null,
        "capacity_ah":null,
        "cca_a":null,
        "length_mm":null,
        "width_mm":null,
        "height_mm":null
      }},
      "reason":"..."
    }}
  ]
}}
Максимум 3 кандидата.
Для аккумулятора обязательно part_class="battery".
Если на наклейке аккумулятора видны Voltage, Ah, CCA/EN или размеры — заполни specs.
Никогда не угадывай specs по автомобилю: только то, что реально видно на фото/маркировке.
Для дворников part_class="wiper".
Для ШРУСа/гранаты part_class="cv_joint".
"""
    try:
        resp = client.responses.create(
            model=model_name,
            input=[{"role":"user","content":[
                {"type":"input_text","text":prompt},
                {"type":"input_image","image_url":data_url}
            ]}]
        )
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "429" in msg or "credit_balance_exhausted" in low or "insufficient_quota" in low:
            return api_error("AI_CREDITS", "Лимит AI временно исчерпан. Попробуйте позже.", False, 503)
        if "401" in msg or "authentication" in low or "api key" in low:
            return api_error("AI_AUTH", "Сервис распознавания временно недоступен.", False, 503)
        if "timeout" in low:
            return api_error("AI_TIMEOUT", "Распознавание заняло слишком много времени. Попробуйте ещё раз.", True, 504)
        return api_error("AI_ERROR", "Не удалось распознать деталь. Попробуйте другое фото.", True, 502)

    text = re.sub(r"^```json\s*|\s*```$", "", resp.output_text.strip(), flags=re.I|re.S).strip()
    try:
        parsed = json.loads(text)
    except Exception:
        return api_error("AI_PARSE", "Не удалось обработать ответ распознавания.", True, 502)
    parsed["mode"]="ai"
    parsed["ok"]=True
    return parsed



@app.get("/api/vin/decode")
def decode_vin(vin: str):
    """
    Lightweight VIN helper using NHTSA vPIC as a public decoder.
    It helps prefill vehicle identity, but it is NOT a parts-fitment catalogue.
    """
    import requests
    vin = (vin or "").strip().upper()
    if len(vin) != 17:
        return api_error("VIN_LENGTH", "VIN должен содержать 17 символов.", False, 400)

    try:
        r = requests.get(
            f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValuesExtended/{vin}",
            params={"format": "json"},
            timeout=10,
        )
        r.raise_for_status()
        rows = r.json().get("Results", [])
        row = rows[0] if rows else {}
    except Exception:
        return api_error("VIN_SERVICE", "Не удалось проверить VIN. Введите данные автомобиля вручную.", True, 502)

    make = (row.get("Make") or "").strip()
    model = (row.get("Model") or "").strip()
    year = (row.get("ModelYear") or "").strip()
    displacement = (row.get("DisplacementL") or "").strip()
    fuel = (row.get("FuelTypePrimary") or "").strip()
    engine = " ".join([x for x in [displacement + "L" if displacement else "", fuel] if x]).strip()

    decoded = bool(make or model or year)
    return {
        "ok": True,
        "decoded": decoded,
        "vin": vin,
        "vehicle": {
            "make": make,
            "model": model,
            "year": year,
            "engine": engine,
        },
        "message": "VIN распознан." if decoded else "VIN принят, но публичный декодер не вернул достаточно данных. Заполните автомобиль вручную."
    }


class FitmentResolver:
    name = "base"
    def enabled(self):
        return False
    def resolve(self, vehicle: dict, part_name: str, identifier: str):
        return None

class TecDocResolver(FitmentResolver):
    name = "tecdoc"
    def enabled(self):
        return bool(os.getenv("TECDOC_API_KEY") and os.getenv("TECDOC_API_URL"))
    def resolve(self, vehicle: dict, part_name: str, identifier: str):
        # Intentionally not implemented until official API credentials/schema are supplied.
        return None

class OvokoFitmentResolver(FitmentResolver):
    name = "ovoko"
    def enabled(self):
        return bool(os.getenv("OVOKO_API_KEY"))
    def resolve(self, vehicle: dict, part_name: str, identifier: str):
        # Will be implemented after official partner API access.
        return None

FITMENT_RESOLVERS = [TecDocResolver(), OvokoFitmentResolver()]

@app.post("/api/part/resolve")
def resolve_part(
    make: str = Form(""),
    model: str = Form(""),
    year: str = Form(""),
    engine: str = Form(""),
    vin: str = Form(""),
    part_name: str = Form(""),
    ai_oem_hint: str = Form(""),
    visible_marking: str = Form(""),
    manual_oem: str = Form("")
):
    def clean(v: str):
        return (v or "").strip()

    vehicle = {
        "make": clean(make),
        "model": clean(model),
        "year": clean(year),
        "engine": clean(engine),
        "vin": clean(vin).upper(),
    }

    manual = clean(manual_oem)
    marking = clean(visible_marking)
    hint = clean(ai_oem_hint)

    identifier = ""
    identifier_source = "none"
    if manual:
        identifier = manual
        identifier_source = "manual"
    elif marking:
        identifier = marking
        identifier_source = "visible_marking"
    elif hint:
        identifier = hint
        identifier_source = "ai_hint"

    resolver_status = {}
    verified_result = None
    for resolver in FITMENT_RESOLVERS:
        resolver_status[resolver.name] = "disabled"
        if not resolver.enabled():
            continue
        resolver_status[resolver.name] = "enabled"
        try:
            result = resolver.resolve(vehicle, part_name, identifier)
            if result:
                verified_result = result
                resolver_status[resolver.name] = "resolved"
                break
        except Exception:
            resolver_status[resolver.name] = "error"

    if verified_result:
        return {
            "status": "verified",
            "verified": True,
            "identifier": verified_result.get("identifier", identifier),
            "identifier_source": verified_result.get("source", "external_catalog"),
            "part_name": verified_result.get("part_name", part_name),
            "message": "Совместимость подтверждена внешним каталогом.",
            "resolver_status": resolver_status,
            "vehicle": vehicle
        }

    if not identifier:
        return {
            "status": "unresolved",
            "verified": False,
            "identifier": "",
            "identifier_source": "none",
            "part_name": part_name,
            "message": "Точный OEM/артикул не найден. Сфотографируйте маркировку крупнее или введите номер вручную.",
            "resolver_status": resolver_status,
            "vehicle": vehicle
        }

    if identifier_source == "manual":
        msg = "Артикул введён пользователем, но совместимость с этим автомобилем пока не подтверждена внешним каталогом."
    elif identifier_source == "visible_marking":
        msg = "Маркировка считана с фото, но это ещё не означает, что она является OEM. Нужна проверка по внешнему каталогу/VIN."
    else:
        msg = "Есть только AI-подсказка по номеру. Точная совместимость не подтверждена."

    return {
        "status": "identifier_unverified",
        "verified": False,
        "identifier": identifier,
        "identifier_source": identifier_source,
        "part_name": part_name,
        "message": msg,
        "resolver_status": resolver_status,
        "vehicle": vehicle
    }

@app.post("/api/catalog/match")
def catalog_match(
    make: str = Form(""), model: str = Form(""), year: str = Form(""),
    engine: str = Form(""), vin: str = Form(""), part_name: str = Form(""), oem_hint: str = Form("")
):
    catalog = load_json("catalog_demo.json")
    try: y = int(year) if year else 0
    except: y = 0
    matches=[]
    for row in catalog:
        if row["make"].lower()!=make.lower() or row["model"].lower()!=model.lower():
            continue
        if y and not (row["year_from"]<=y<=row["year_to"]):
            continue
        hay=(" ".join([row["part_name"]]+row.get("keywords",[]))).lower()
        words=[w for w in re.split(r"\W+",part_name.lower()) if len(w)>3]
        if any(w in hay for w in words):
            matches.append({"part_name":row["part_name"],"oem":row["oem"],"fitment_note":row["fitment_note"],"source":"demo-local"})
    if not matches and oem_hint:
        matches.append({"part_name":part_name,"oem":oem_hint,"fitment_note":"Совпадение из AI-подсказки; внешний каталог ещё не подключён.","source":"ai-hint"})
    return {"summary":"Найдены кандидаты в demo-каталоге.","matches":matches[:5]}


def ebay_access_token():
    """Get an eBay application token using client credentials when configured."""
    import requests, base64 as b64
    client_id = os.getenv("EBAY_CLIENT_ID")
    client_secret = os.getenv("EBAY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    creds = b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type":"client_credentials","scope":"https://api.ebay.com/oauth/api_scope"},
        timeout=12,
    )
    r.raise_for_status()
    return r.json()["access_token"]

def _norm_words(text: str):
    import re
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 2]



def _num_or_none(v):
    try:
        return float(v)
    except Exception:
        return None


def _classify_offer(title: str, part_class: str, search_query: str, vehicle: dict):
    """
    Classify marketplace result by WHAT the listing actually sells.

    Buckets:
    - exact: same part type
    - likely: plausible analogue / same functional part
    - accessory: related consumable / kit / cover / grease / clamp etc.
    - similar: adjacent assembly, e.g. full driveshaft instead of CV joint
    - other: weakly related marketplace result

    Vehicle is only a hard filter when explicitly verified.
    """
    t = (title or "").lower()
    make = (vehicle.get("make") or "").lower()
    model = (vehicle.get("model") or "").lower()
    vehicle_verified = bool(vehicle.get("verified"))
    q = (search_query or "").lower()

    exact_terms = {
        "cv_joint": [
            "cv joint", "outer joint", "outer cv", "außengelenk", "aussengelenk",
            "gelenksatz", "gleichlaufgelenk", "homokinetic joint", "constant velocity joint"
        ],
        "battery": ["battery", "batterie", "autobatterie", "starterbatterie", "akku"],
        "wiper": ["wiper blade", "wiper", "scheibenwischer", "wischblatt"],
        "brake": ["brake pad", "brake disc", "brake rotor", "bremsbelag", "bremsscheibe"],
        "filter": ["filter"],
        "lamp": ["headlight", "taillight", "scheinwerfer", "rückleuchte", "leuchte"],
        "sensor": ["sensor", "geber", "fühler"],
        "hose": ["hose", "schlauch", "leitung"],
    }

    related_assembly_terms = {
        "cv_joint": ["antriebswelle", "driveshaft", "drive shaft", "gelenkwelle", "half shaft", "axle shaft"],
        "battery": ["battery tray"],
        "wiper": ["wiper arm", "wiper motor"],
        "brake": ["caliper", "brake carrier"],
        "filter": ["filter housing"],
        "lamp": ["lamp holder"],
        "sensor": ["sensor harness"],
        "hose": ["pipe", "line"],
    }

    accessory_terms = {
        "cv_joint": [
            "boot kit", "cv boot", "joint boot", "gaiter", "manschette", "faltenbalg",
            "grease", "fett", "lubricant", "clamp", "clip", "strap", "band", "ring",
            "dust cover", "repair kit", "seal kit"
        ],
        "battery": ["terminal clamp", "battery terminal", "battery holder", "battery strap", "battery cover"],
        "wiper": ["adapter", "clip", "connector", "refill rubber"],
        "brake": ["hardware kit", "spring kit", "sensor wire", "wear sensor"],
        "filter": ["housing cap", "seal", "o-ring"],
        "lamp": ["bulb", "socket", "holder", "adapter"],
        "sensor": ["cable", "connector", "plug", "harness"],
        "hose": ["clamp", "connector", "fitting"],
    }

    hard_reject_terms = {
        "cv_joint": ["wheel bearing", "radlager", "abs sensor"],
        "battery": ["wiper", "blade", "lamp", "bulb", "filter"],
        "wiper": ["battery", "batterie", "filter"],
    }

    exact = any(x in t for x in exact_terms.get(part_class, []))
    related = any(x in t for x in related_assembly_terms.get(part_class, []))
    accessory = any(x in t for x in accessory_terms.get(part_class, []))
    hard_reject = any(x in t for x in hard_reject_terms.get(part_class, []))

    vehicle_hits = 0
    if make and make in t:
        vehicle_hits += 1
    if model and model in t:
        vehicle_hits += 2

    other_vehicle_terms = [
        "jeep","wrangler","pajero","mitsubishi","touareg","volvo","s60","passat",
        "golf","transporter","caravelle","multivan","audi","bmw","mercedes","ford",
        "toyota","nissan","honda","skoda","seat","peugeot","renault","fiat",
        "ssangyong","rexton","chrysler","dodge","plymouth","mini"
    ]
    mentions_other_vehicle = any(v in t for v in other_vehicle_terms)
    matches_requested_vehicle = bool(vehicle_hits)

    q_words = [w for w in _norm_words(q) if len(w) >= 3]
    q_hits = sum(1 for w in q_words if w in t)

    if hard_reject:
        return {"bucket": "other", "score": 5, "label": "Другой товар", "vehicle_status": "n/a"}

    # Accessories must win over "exact" keyword matches.
    # Example: "Outer CV Joint Grease" contains "CV Joint" but sells grease, not the joint.
    if accessory:
        return {"bucket": "accessory", "score": 30, "label": "Сопутствующий товар", "vehicle_status": "n/a"}

    if vehicle_verified and make and model and mentions_other_vehicle and not matches_requested_vehicle:
        return {"bucket": "other", "score": 0, "label": "Для другой машины", "vehicle_status": "mismatch"}

    if exact and vehicle_verified and vehicle_hits >= 2:
        return {"bucket": "exact", "score": 100, "label": "Сама деталь · авто совпадает", "vehicle_status": "matched"}

    if exact:
        if vehicle_verified:
            return {"bucket": "likely", "score": 84 + min(vehicle_hits * 4, 8), "label": "Возможный аналог", "vehicle_status": "unconfirmed"}
        return {"bucket": "exact", "score": 88, "label": "Сама деталь · применимость не проверена", "vehicle_status": "hint_only"}

    if related:
        return {
            "bucket": "similar",
            "score": 50 if vehicle_hits else 40,
            "label": "Смежная сборка / похожая деталь",
            "vehicle_status": "hint_only" if not vehicle_verified else "unconfirmed"
        }

    if q_hits >= 2:
        return {
            "bucket": "likely",
            "score": 65,
            "label": "Возможный аналог · применимость не проверена" if not vehicle_verified else "Возможный аналог",
            "vehicle_status": "hint_only" if not vehicle_verified else "unconfirmed"
        }

    return {"bucket": "other", "score": 10, "label": "Остальной результат eBay", "vehicle_status": "n/a"}


def _extract_battery_specs(text: str):
    """Best-effort parser from marketplace title. No invented values."""
    import re
    t = (text or "").replace(",", ".")
    out = {"voltage_v": None, "capacity_ah": None, "cca_a": None}

    m = re.search(r"\b(6|12|24)\s*V\b", t, re.I)
    if m:
        out["voltage_v"] = float(m.group(1))

    # avoid accidentally treating 470A as Ah
    m = re.search(r"\b(\d{2,3}(?:\.\d+)?)\s*Ah\b", t, re.I)
    if m:
        out["capacity_ah"] = float(m.group(1))

    # Common forms: 470A, 680 A EN, CCA 760, 760CCA.
    candidates = []
    for pat in [
        r"\bCCA\s*[:\-]?\s*(\d{3,4})\b",
        r"\b(\d{3,4})\s*CCA\b",
        r"\b(\d{3,4})\s*A\s*(?:EN|CCA)\b",
        r"\b(\d{3,4})\s*A\b",
    ]:
        mm = re.search(pat, t, re.I)
        if mm:
            try:
                val = float(mm.group(1))
                if 200 <= val <= 1500:
                    candidates.append(val)
            except Exception:
                pass
    if candidates:
        out["cca_a"] = candidates[0]
    return out

def _battery_spec_match(required: dict, offered: dict):
    """
    Compatibility score for batteries.
    Score is 0..100 and is based ONLY on specs actually known on both sides.
    This is not official vehicle fitment verification.
    """
    rv = _num_or_none((required or {}).get("voltage_v"))
    rah = _num_or_none((required or {}).get("capacity_ah"))
    rcca = _num_or_none((required or {}).get("cca_a"))

    ov = _num_or_none((offered or {}).get("voltage_v"))
    oah = _num_or_none((offered or {}).get("capacity_ah"))
    occa = _num_or_none((offered or {}).get("cca_a"))

    weighted = []
    reasons = []
    hard_mismatch = False

    # Voltage: critical.
    if rv is not None and ov is not None:
        if abs(rv - ov) < 0.1:
            weighted.append((100, 35))
            reasons.append("Voltage совпадает")
        else:
            weighted.append((0, 35))
            reasons.append("Voltage не совпадает")
            hard_mismatch = True

    # Capacity Ah: very important, but allow modest tolerance.
    if rah is not None and oah is not None:
        diff = abs(oah - rah) / max(rah, 1)
        if diff <= 0.03:
            s = 100
            reasons.append("Ah почти точно совпадает")
        elif diff <= 0.07:
            s = 90
            reasons.append("Ah близко")
        elif diff <= 0.12:
            s = 70
            reasons.append("Ah допустимо близко")
        elif diff <= 0.20:
            s = 35
            reasons.append("Ah заметно отличается")
        else:
            s = 0
            reasons.append("Ah сильно отличается")
            hard_mismatch = True
        weighted.append((s, 40))

    # CCA: higher is generally less concerning than materially lower.
    if rcca is not None and occa is not None:
        ratio = occa / max(rcca, 1)
        if 0.95 <= ratio <= 1.25:
            s = 100
            reasons.append("CCA подходит")
        elif 0.85 <= ratio < 0.95:
            s = 65
            reasons.append("CCA немного ниже")
        elif ratio > 1.25:
            s = 80
            reasons.append("CCA выше исходного")
        else:
            s = 20
            reasons.append("CCA заметно ниже")
        weighted.append((s, 25))

    if not weighted:
        return {
            "score": None,
            "status": "unknown",
            "reasons": [],
            "known_specs": 0,
            "hard_mismatch": False
        }

    total_weight = sum(w for _, w in weighted)
    score = round(sum(s*w for s, w in weighted) / total_weight)

    if hard_mismatch or score < 45:
        status = "mismatch"
    elif score >= 90:
        status = "strong"
    elif score >= 70:
        status = "good"
    else:
        status = "partial"

    return {
        "score": score,
        "status": status,
        "reasons": reasons,
        "known_specs": len(weighted),
        "hard_mismatch": hard_mismatch
    }


def _extract_model_tokens(text: str):
    """
    Extract short alphanumeric model codes that matter for exact matching:
    S4, S5, H7, AGM80, 4F0, etc.
    """
    import re
    tokens = re.findall(r"\b[A-Za-z]{1,4}\d{1,4}[A-Za-z0-9-]*\b|\b\d{1,4}[A-Za-z]{1,4}\b", text or "")
    return [t.upper() for t in tokens if len(t) >= 2]

def _relevance_score(title: str, search_query: str, part_class: str):
    title_words = set(_norm_words(title))
    q_words = set(_norm_words(search_query))
    score = len(title_words & q_words) * 2

    positive = {
        "battery": {"battery","batterie","akku","accu","starter"},
        "wiper": {"wiper","wipers","blade","blades","scheibenwischer"},
        "cv_joint": {"cv","joint","gelenk","antriebswelle","driveshaft","outer","inner"},
        "brake": {"brake","brakes","pad","pads","disc","rotor","bremse","brems"},
        "filter": {"filter","oilfilter","airfilter","kraftstofffilter"},
        "lamp": {"lamp","light","headlight","taillight","scheinwerfer","leuchte"},
        "sensor": {"sensor","geber","fühler"},
        "hose": {"hose","schlauch","pipe","leitung"},
    }
    negative = {
        "battery": {"wiper","blade","scheibenwischer","filter","lamp","bulb"},
        "wiper": {"battery","batterie","akku","filter"},
        "cv_joint": {"battery","batterie","wiper","blade","filter"},
        "brake": {"battery","batterie","wiper","blade"},
        "filter": {"battery","batterie","wiper","blade"},
    }

    pos = positive.get(part_class, set())
    neg = negative.get(part_class, set())
    score += sum(3 for w in pos if w in title_words)
    score -= sum(6 for w in neg if w in title_words)

    query_models = _extract_model_tokens(search_query)
    title_upper = (title or "").upper()
    if query_models:
        matched = [m for m in query_models if m in title_upper]
        missing = [m for m in query_models if m not in title_upper]
        score += 8 * len(matched)
        # For distinctive short model names like S4, absence should strongly penalize.
        score -= 7 * len(missing)

    return score


def _infer_part_class(text: str):
    t = (text or "").lower()
    rules = [
        ("battery", ["аккумуля", "battery", "batterie", "starterbatterie", "akku"]),
        ("wiper", ["дворник", "щетк", "wiper", "scheibenwischer", "wischblatt"]),
        ("cv_joint", ["шрус", "гранат", "cv joint", "gelenk", "antriebswelle"]),
        ("brake", ["тормоз", "колодк", "диск", "brake", "bremse", "brems"]),
        ("filter", ["фильтр", "filter"]),
        ("lamp", ["фара", "фонарь", "ламп", "headlight", "taillight", "scheinwerfer", "leuchte"]),
        ("sensor", ["датчик", "sensor", "geber", "fühler"]),
        ("hose", ["шланг", "hose", "schlauch", "leitung"]),
    ]
    for cls, words in rules:
        if any(w in t for w in words):
            return cls
    return "other"

def _build_ebay_query(oem: str, search_query: str, part_class: str):
    """
    Never trust the AI search fields blindly.
    If they are missing, build a compact marketplace query from the visible marking/OEM.
    German terms are useful because LV/LT/EE currently search EBAY_DE.
    """
    base = (oem or "").strip()
    supplied = (search_query or "").strip()

    # If supplied query is mostly Cyrillic or too generic, prefer our own query.
    ascii_letters = sum(ch.isascii() and ch.isalpha() for ch in supplied)
    non_ascii_letters = sum((not ch.isascii()) and ch.isalpha() for ch in supplied)
    supplied_is_marketplace_friendly = bool(supplied) and ascii_letters >= non_ascii_letters and len(supplied) <= 100

    suffix = {
        "battery": "Autobatterie Starterbatterie",
        "wiper": "Scheibenwischer Wischerblatt",
        "cv_joint": "Gleichlaufgelenk Antriebswelle",
        "brake": "Bremse Bremsbeläge",
        "filter": "Autofilter",
        "lamp": "Scheinwerfer Autolampe",
        "sensor": "Autosensor",
        "hose": "Autoschlauch Leitung",
        "other": "Autoteil",
    }.get(part_class, "Autoteil")

    if supplied_is_marketplace_friendly:
        # Still append the class term if the query is only a model/marking like "BOSCH S4".
        low = supplied.lower()
        class_words = {
            "battery": ["battery","batterie","akku"],
            "wiper": ["wiper","wischer"],
            "cv_joint": ["cv","joint","gelenk"],
            "brake": ["brake","brem"],
            "filter": ["filter"],
            "lamp": ["lamp","light","scheinwerfer"],
            "sensor": ["sensor"],
            "hose": ["hose","schlauch","leitung"],
        }.get(part_class, [])
        if class_words and not any(w in low for w in class_words):
            return f"{supplied} {suffix}".strip()
        return supplied

    return f"{base} {suffix}".strip()

_EBAY_DIAGNOSTICS = {}

def _ebay_category_for_part(part_class: str):
    # eBay.de category IDs. Keep conservative: only map categories we have tested.
    return {
        "battery": "179846",   # Batterien fürs Auto
    }.get(part_class, "")


def _log_ebay_error(mode: str, q: str, category_id: str, compatibility_filter: str, exc):
    try:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        body = getattr(getattr(exc, "response", None), "text", "")
        print("EBAY ERROR")
        print(f"mode={mode}")
        print(f"status={status}")
        print(f"query={q}")
        print(f"category_id={category_id}")
        print(f"compatibility_filter={compatibility_filter}")
        if body:
            print(f"response={body[:3000]}")
        else:
            print(f"exception={exc}")
    except Exception:
        pass

def _query_variants(oem: str, search_query: str, part_class: str):
    variants = []
    def add(v):
        v = (v or "").strip()
        if v and v not in variants:
            variants.append(v)

    add(search_query)
    add(oem)
    add((oem or "").replace(" ", ""))

    if part_class == "cv_joint":
        add("outer CV joint")
        add("Rzeppa outer CV joint")
        add("VW Touareg outer CV joint")
    elif part_class == "battery":
        add(f"{oem} Autobatterie".strip())
        add(f"{oem} car battery".strip())
    else:
        add(f"{oem} auto part".strip())

    return variants[:5]

def _ebay_request(token, marketplace, country, postal, q, category_id="", compatibility_filter="", seller_countries=None, limit=50):
    import requests
    params = {"q": q, "limit": limit}
    if category_id:
        params["category_ids"] = category_id
    if compatibility_filter:
        params["compatibility_filter"] = compatibility_filter
    if seller_countries:
        params["filter"] = "itemLocationCountry:" + "|".join("{" + c + "}" for c in seller_countries)

    r = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
            "X-EBAY-C-ENDUSERCTX": (
                f"contextualLocation=country%3D{country}"
                + (f"%2Czip%3D{postal}" if postal else "")
            ),
        },
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("itemSummaries", [])

def ebay_search(oem: str, country: str, postal: str = "", search_query: str = "", part_class: str = "", vehicle: dict | None = None, required_specs: dict | None = None):
    """
    Cascade search:
    1) precise category + compatibility
    2) precise category without compatibility
    3) broad automotive category without compatibility
    4) relevance filter removes obvious junk
    """
    token = ebay_access_token()
    if not token:
        return []

    vehicle = vehicle or {}
    required_specs = required_specs or {}
    marketplace = {
        "DE":"EBAY_DE","LV":"EBAY_DE","LT":"EBAY_DE","EE":"EBAY_DE","PL":"EBAY_DE"
    }.get(country, "EBAY_DE")

    inferred_class = part_class if part_class and part_class != "other" else _infer_part_class(search_query)
    if inferred_class == "other":
        inferred_class = _infer_part_class(oem)
    part_class = inferred_class

    base_q = _build_ebay_query(oem, search_query, part_class)
    query_variants = _query_variants(oem, base_q, part_class)
    precise_category = _ebay_category_for_part(part_class)
    broad_category = "6030"

    year = (vehicle.get("year") or "").strip()
    make = (vehicle.get("make") or "").strip()
    model = (vehicle.get("model") or "").strip()
    engine = (vehicle.get("engine") or "").strip()

    compat = []
    if year: compat.append(f"Year:{year}")
    if make: compat.append(f"Make:{make}")
    if model: compat.append(f"Model:{model}")
    if engine: compat.append(f"Engine:{engine}")
    vehicle_verified = bool(vehicle.get("verified"))
    compatibility_filter = ";".join(compat) if vehicle_verified and len(compat) >= 2 else ""

    eu_countries = ["DE","PL","LT","LV","EE","NL","BE","FR","IT","ES","AT","CZ","SK","SE","FI","DK"]
    attempts = []
    if precise_category and compatibility_filter:
        attempts.append(("precise+compat+eu", precise_category, compatibility_filter, eu_countries))
    if precise_category:
        attempts.append(("precise+eu", precise_category, "", eu_countries))
    attempts.append(("broad+eu", broad_category, "", eu_countries))
    # Last-resort global search; these results will rank below EU offers.
    attempts.append(("global", precise_category or broad_category, "", None))

    collected = {}
    attempt_log = []
    successful_query = ""

    stop_search = False
    for q in query_variants:
        for mode, category_id, compat_filter, seller_countries in attempts:
            try:
                items = _ebay_request(
                    token=token,
                    marketplace=marketplace,
                    country=country,
                    postal=postal,
                    q=q,
                    category_id=category_id,
                    compatibility_filter=compat_filter,
                    seller_countries=seller_countries,
                    limit=50,
                )
                attempt_log.append({"mode": mode, "query": q, "count": len(items)})
            except Exception as exc:
                _log_ebay_error(mode, q, category_id, compat_filter, exc)
                attempt_log.append({"mode": mode, "query": q, "count": 0, "error": True})
                continue

            if items and not successful_query:
                successful_query = q

            for item in items:
                iid = item.get("itemId") or item.get("legacyItemId") or item.get("itemWebUrl") or item.get("title")
                if iid and iid not in collected:
                    collected[iid] = item

            if mode.startswith("precise") and len(collected) >= 10:
                stop_search = True
                break
        if stop_search or len(collected) >= 20:
            break

    out = []
    for item in collected.values():
        price = item.get("price", {})
        ship = (item.get("shippingOptions") or [{}])[0].get("shippingCost", {})
        try:
            item_price = float(price.get("value", 0))
            shipping = float(ship.get("value", 0) or 0)
        except Exception:
            continue
        if price.get("currency") != "EUR":
            continue

        title = item.get("title", "")
        relevance = _relevance_score(title, successful_query or base_q, part_class)

        query_models = _extract_model_tokens(successful_query or base_q)
        title_upper = title.upper()
        if query_models and not all(m in title_upper for m in query_models):
            # Exact model code requested (e.g. S4) -> drop different Bosch batteries.
            continue

        offered_specs = {}
        spec_match = {"score": None, "status": "unknown", "reasons": [], "known_specs": 0, "hard_mismatch": False}
        if part_class == "battery":
            offered_specs = _extract_battery_specs(title)
            spec_match = _battery_spec_match(required_specs, offered_specs)
            # Only remove a clear hard mismatch. Unknown/partial results remain,
            # but rank below better-specified offers.
            if spec_match.get("hard_mismatch"):
                continue
            if spec_match.get("score") is not None:
                relevance += int(spec_match["score"] / 10)

        if relevance < 2:
            continue

        image = (item.get("image") or {}).get("imageUrl", "")
        compat_match = item.get("compatibilityMatch", "")
        compat_props = item.get("compatibilityProperties", []) or []

        semantic_match = _classify_offer(
            title,
            part_class,
            successful_query or base_q,
            vehicle
        )

        # Low-confidence marketplace results are kept for the collapsed "other" group.
        out.append({
            "merchant":"eBay",
            "title":title,
            "item_price":item_price,
            "shipping":shipping,
            "import_fee":0.0,
            "total":round(item_price+shipping,2),
            "currency":"EUR",
            "country":item.get("itemLocation",{}).get("country",""),
            "type":"Marketplace",
            "local":item.get("itemLocation",{}).get("country","")==country,
            "eu_seller": item.get("itemLocation",{}).get("country","") in {"LV","LT","EE","DE","PL","NL","BE","FR","IT","ES","AT","CZ","SK","SE","FI","DK","IE","PT","GR","LU","SI","HR","HU","RO","BG"},
            "delivery_days":"—",
            "url":item.get("itemWebUrl",""),
            "image_url":image,
            "source":"ebay-live",
            "relevance_score":relevance,
            "compatibility_match":compat_match,
            "compatibility_properties":compat_props,
            "search_attempts": attempt_log,
            "search_query_used": successful_query or base_q,
            "part_class_used": part_class,
            "offered_specs": offered_specs,
            "spec_match": spec_match,
            "semantic_match": semantic_match,
        })

    rank = {"EXACT": 0, "POSSIBLE": 1, "": 2}
    semantic_rank = {"exact": 0, "likely": 1, "similar": 2, "accessory": 3, "other": 4, "reject": 5}

    def fit_bucket(x):
        sm = x.get("spec_match") or {}
        score = sm.get("score")
        return (0, -score) if isinstance(score, (int, float)) else (1, 0)

    out.sort(key=lambda x: (
        semantic_rank.get((x.get("semantic_match") or {}).get("bucket","similar"), 5),
        0 if x.get("eu_seller") else 1,
        *fit_bucket(x),
        rank.get(x.get("compatibility_match",""), 3),
        -int((x.get("semantic_match") or {}).get("score",0)),
        -x["relevance_score"],
        x["total"]
    ))
    _EBAY_DIAGNOSTICS[(country, oem, search_query, part_class)] = {
        "attempts": attempt_log,
        "query_used": successful_query or base_q,
        "part_class_used": part_class,
            "offered_specs": offered_specs,
            "spec_match": spec_match,
        "raw_unique": len(collected),
        "after_relevance": len(out),
    }
    return out[:20]

class MerchantSource:
    name = "base"
    def enabled(self):
        return False
    def search(self, oem: str, country: str, postal: str = "", search_query: str = "", part_class: str = "", vehicle: dict | None = None, required_specs: dict | None = None):
        return []

class EbaySource(MerchantSource):
    name = "ebay"
    def enabled(self):
        return bool(os.getenv("EBAY_CLIENT_ID") and os.getenv("EBAY_CLIENT_SECRET"))
    def search(self, oem: str, country: str, postal: str = "", search_query: str = "", part_class: str = "", vehicle: dict | None = None, required_specs: dict | None = None):
        return ebay_search(oem, country, postal, search_query, part_class, vehicle, required_specs) if self.enabled() else []

class OvokoSource(MerchantSource):
    name = "ovoko"
    def enabled(self):
        return bool(os.getenv("OVOKO_API_KEY"))
    def search(self, oem: str, country: str, postal: str = ""):
        # Will be implemented only after official Ovoko/RRR API access is granted.
        return []

SOURCES = [EbaySource(), OvokoSource()]

@app.get("/api/offers/search")
def offers_search(oem: str, country: str = "LV", postal: str = "", search_query: str = "", part_class: str = "", make: str = "", model: str = "", year: str = "", engine: str = "", vehicle_verified: bool = False, voltage_v: str = "", capacity_ah: str = "", cca_a: str = ""):
    countries = {c["code"]: c for c in load_json("countries.json")}
    if country not in countries:
        raise HTTPException(400, "Неподдерживаемая страна.")
    dest = countries[country]
    offers = load_json("offers_demo.json")
    required_specs = {
        "voltage_v": (voltage_v or "").strip(),
        "capacity_ah": (capacity_ah or "").strip(),
        "cca_a": (cca_a or "").strip(),
    }

    norm = oem.lower().strip()
    found=[]
    for o in offers:
        if not (norm in o["oem"].lower() or o["oem"].lower() in norm):
            continue
        if country not in o.get("ships_to", []):
            continue
        shipping = float(o.get("shipping_by_country", {}).get(country, 9999))
        if shipping >= 9999:
            continue
        item = float(o["item_price"])
        import_fee = 0.0

        # Demo rule: merchants outside EU can incur estimated import/VAT.
        if o.get("country") not in {"LV","LT","EE","DE","PL","FR","IT","ES","NL","BE","AT","FI","SE","DK","CZ","SK","HU","RO","BG","HR","SI","IE","PT","GR","LU","CY","MT"} and dest.get("eu"):
            import_fee = round(item * float(o.get("import_fee_rate", 0)), 2)

        total = round(item + shipping + import_fee, 2)
        found.append({
            **o,
            "shipping": shipping,
            "import_fee": import_fee,
            "total": total,
            "local": o.get("country") == country,
            "delivery_days": o.get("delivery_days", {}).get(country, "—")
        })
    live_sources = []
    source_status = {}
    for source in SOURCES:
        source_status[source.name] = "disabled"
        if not source.enabled():
            continue
        try:
            live = source.search(
                oem, country, postal, search_query, part_class,
                {"make":make,"model":model,"year":year,"engine":engine,"verified":vehicle_verified},
                required_specs
            )
            source_status[source.name] = "ok"
            if live:
                found.extend(live)
                live_sources.append(source.name)
        except Exception:
            source_status[source.name] = "error"

    def global_rank(x):
        sr = {"exact":0,"likely":1,"similar":2,"accessory":3,"other":4,"reject":5}
        semantic = x.get("semantic_match") or {}
        sm = x.get("spec_match") or {}
        score = sm.get("score")
        known_fit = 0 if isinstance(score, (int, float)) else 1
        fit_score = -(score if isinstance(score, (int, float)) else 0)
        cm = x.get("compatibility_match","")
        cr = {"EXACT":0,"POSSIBLE":1,"":2}.get(cm,3)
        eu = 0 if x.get("eu_seller") else 1
        rel = -int(x.get("relevance_score",0))
        return (
            sr.get(semantic.get("bucket","similar"),5),
            eu,
            known_fit,
            fit_score,
            cr,
            -int(semantic.get("score",0)),
            rel,
            x["total"]
        )
    found.sort(key=global_rank)
    return {
        "destination": dest,
        "postal": postal,
        "offers": found,
        "group_counts": {
            "part": sum(1 for x in found if (x.get("semantic_match") or {}).get("bucket") == "exact"),
            "analogue": sum(1 for x in found if (x.get("semantic_match") or {}).get("bucket") == "likely"),
            "similar": sum(1 for x in found if (x.get("semantic_match") or {}).get("bucket") == "similar"),
            "accessory": sum(1 for x in found if (x.get("semantic_match") or {}).get("bucket") == "accessory"),
            "other": sum(1 for x in found if (x.get("semantic_match") or {}).get("bucket") in {"other","reject"}),
        },
        "best_relevant_price": next((
            x["total"] for x in found
            if (x.get("semantic_match") or {}).get("bucket") in {"exact","likely"}
        ), None),
        "sort": "semantic_group_then_fit_then_landed_total",
        "live_sources": live_sources,
        "source_status": source_status,
        "search_attempts": next((x.get("search_attempts", []) for x in found if x.get("source")=="ebay-live"), []),
        "ebay_diagnostics": _EBAY_DIAGNOSTICS.get((country, oem, search_query, part_class), {}),
        "required_specs": required_specs,
        "vehicle_verified": vehicle_verified
    }
