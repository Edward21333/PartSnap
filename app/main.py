import os, json, base64, re
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DATA = BASE / "data"

app = FastAPI(title="PartSnap MVP v0.9")

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
        "version": "0.9",
        "ai_enabled": bool(os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_MODEL")),
        "regional_pricing": True
    }

def demo_result(make, model, year, engine):
    return {
        "mode":"demo",
        "summary":f"Демо-анализ для {make} {model} {year} {engine}.",
        "candidates":[
            {"name":"Шланг гидроусилителя руля","confidence":0.82,"oem_hint":"1J0 422 887 (family)","reason":"Форма похожа на магистраль ГУР; точный OEM требует каталога/VIN."},
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
      "reason":"..."
    }}
  ]
}}
Максимум 3 кандидата.
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

def ebay_search(oem: str, country: str):
    """Optional real source. Returns [] until EBAY credentials are configured."""
    import requests
    token = ebay_access_token()
    if not token:
        return []
    marketplace = {
        "DE":"EBAY_DE","LV":"EBAY_DE","LT":"EBAY_DE","EE":"EBAY_DE","PL":"EBAY_DE"
    }.get(country, "EBAY_DE")
    r = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
            "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country%3D{country}",
        },
        params={"q": oem, "limit": 20},
        timeout=12,
    )
    r.raise_for_status()
    out=[]
    for item in r.json().get("itemSummaries", []):
        price=item.get("price",{})
        ship=(item.get("shippingOptions") or [{}])[0].get("shippingCost",{})
        try:
            item_price=float(price.get("value",0))
            shipping=float(ship.get("value",0) or 0)
        except:
            continue
        if price.get("currency") != "EUR":
            continue
        out.append({
            "merchant":"eBay",
            "title":item.get("title",""),
            "item_price":item_price,
            "shipping":shipping,
            "import_fee":0.0,
            "total":round(item_price+shipping,2),
            "currency":"EUR",
            "country":item.get("itemLocation",{}).get("country",""),
            "type":"Marketplace",
            "local":item.get("itemLocation",{}).get("country","")==country,
            "delivery_days":"—",
            "url":item.get("itemWebUrl",""),
            "source":"ebay-live",
        })
    return out


class MerchantSource:
    name = "base"
    def enabled(self):
        return False
    def search(self, oem: str, country: str, postal: str = ""):
        return []

class EbaySource(MerchantSource):
    name = "ebay"
    def enabled(self):
        return bool(os.getenv("EBAY_CLIENT_ID") and os.getenv("EBAY_CLIENT_SECRET"))
    def search(self, oem: str, country: str, postal: str = ""):
        return ebay_search(oem, country) if self.enabled() else []

class OvokoSource(MerchantSource):
    name = "ovoko"
    def enabled(self):
        return bool(os.getenv("OVOKO_API_KEY"))
    def search(self, oem: str, country: str, postal: str = ""):
        # Will be implemented only after official Ovoko/RRR API access is granted.
        return []

SOURCES = [EbaySource(), OvokoSource()]

@app.get("/api/offers/search")
def offers_search(oem: str, country: str = "LV", postal: str = ""):
    countries = {c["code"]: c for c in load_json("countries.json")}
    if country not in countries:
        raise HTTPException(400, "Неподдерживаемая страна.")
    dest = countries[country]
    offers = load_json("offers_demo.json")

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
            live = source.search(oem, country, postal)
            source_status[source.name] = "ok"
            if live:
                found.extend(live)
                live_sources.append(source.name)
        except Exception:
            source_status[source.name] = "error"

    found.sort(key=lambda x: (x["total"], 0 if x.get("local") else 1))
    return {
        "destination": dest,
        "postal": postal,
        "offers": found,
        "sort": "landed_total",
        "live_sources": live_sources,
        "source_status": source_status
    }
