#!/usr/bin/env python3
"""
1C <-> LM Studio Bridge  v11
FastAPI: natural language -> 1C OData queries

Key improvements over v10:
1. Tabular parts (Document_*_TabularName) are EXCLUDED from entity detection
2. System prompt is built PER-ENTITY (compact, fits in 4096 ctx)
3. Document keywords have PRIORITY over Catalog keywords
4. Two-step Ref_Key resolution: find GUID in catalog, then filter document
5. Auto-exclude DeletionMark and IsFolder by default
"""

import os
import json
import re
import hashlib
import httpx
import requests
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="1C-LM Studio Bridge v11")


# ======== Custom JSON with ensure_ascii=False ========
class UnicodeJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
        ).encode("utf-8")


# ======== Configuration ========
C1_ODATA_URL = os.getenv("C1_ODATA_URL", "http://localhost/ut/odata/standard.odata")
C1_USERNAME = os.getenv("C1_USERNAME", "da")
C1_PASSWORD = os.getenv("C1_PASSWORD", "123")
LMSTUDIO_URL = os.getenv("LMSTUDIO_URL", "http://localhost:1234/v1/chat/completions")
LMSTUDIO_MODEL = os.getenv("LMSTUDIO_MODEL", "local-model")

METADATA_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "1c_metadata_cache.json"
)


# ======== Query Cache ========
_cache: Dict[str, Any] = {}
_cache_ttl_seconds = 300


def _cache_key(request: "QueryRequest") -> str:
    raw = f"{request.user_question}:{request.entity_hint}:{request.max_results}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _get_cached(key: str) -> Optional[Any]:
    if key not in _cache:
        return None
    entry = _cache[key]
    if datetime.now() > entry["expires"]:
        del _cache[key]
        return None
    return entry["data"]


def _set_cached(key: str, data: Any, ttl: int = _cache_ttl_seconds):
    _cache[key] = {"data": data, "expires": datetime.now() + timedelta(seconds=ttl)}


@app.get("/cache/stats")
async def cache_stats():
    total = len(_cache)
    expired = sum(1 for v in _cache.values() if datetime.now() > v["expires"])
    return {
        "total_entries": total,
        "expired": expired,
        "active": total - expired,
        "ttl_seconds": _cache_ttl_seconds,
    }


@app.post("/cache/clear")
async def cache_clear():
    count = len(_cache)
    _cache.clear()
    return {"cleared": count}


# ======== Data Models ========
class QueryRequest(BaseModel):
    user_question: str
    entity_hint: Optional[str] = None
    max_results: int = 50


class AskRequest(BaseModel):
    q: str


class SimpleTestRequest(BaseModel):
    url: Optional[str] = None
    entity: Optional[str] = None
    filter: Optional[str] = None
    top: int = 10


# ======================================================================
# AUTO-DISCOVERY OF ENTITIES FROM 1C $METADATA
# ======================================================================

# Russian business term synonyms for auto-detection
_SYNONYM_MAP: Dict[str, List[str]] = {
    "номенклатур": ["товар", "продукц", "позици", "артикул"],
    "контрагент": ["клиент", "покупател", "поставщик", "партнёр", "партнер", "заказчик"],
    "склад": ["магазин", "помещен", "местохранен"],
    "организаци": ["компани", "фирм", "юрлиц", "юридическ"],
    "реализац": ["продаж", "отгрузк", "реализ"],
    "поступлен": ["приход", "закупк", "приобретен"],
    "счет": ["инвойс", "фактур"],
    "заказ": ["заявк"],
    "платеж": ["оплат", "перевод"],
    "сотрудник": ["работник", "персонал", "служащ", "физлиц"],
    "договор": ["контракт", "соглашен"],
    "проект": ["задач"],
    "касса": ["кассов"],
    "валют": ["курс"],
    "характеристик": ["свойств", "параметр", "атрибут"],
    "единиц": ["измерен", "мера"],
    "статья": ["затрат", "расход", "доход"],
    "центр": ["подразделен", "отдел", "филиал"],
    "вид": ["тип", "категори", "групп"],
    "статус": ["состояни"],
    "серия": ["парти", "лот"],
}

# System fields — do not include in SELECT
_SYSTEM_FIELDS = {
    "Ref_Key", "DataVersion", "DeletionMark", "IsFolder",
    "Predefined", "PredefinedDataName", "Owner_Key", "Parent_Key",
    "LineNumber", "Ref", "Type",
}

# Global configs — populated from metadata
ENTITY_FIELDS: Dict[str, List[str]] = {}
SELECT_FIELDS: Dict[str, List[str]] = {}
ISFOLDER_ENTITIES: set = set()
ENTITY_KEYWORDS: Dict[str, List[str]] = {}
ALL_ENTITY_NAMES: List[str] = []


def _is_tabular_part(entity_name: str) -> bool:
    """Check if an entity is a tabular part (table inside a document/catalog).
    Tabular parts have format: Document_DocName_TabularName
    i.e., more than one underscore-separated Russian segment after the type prefix.

    Examples:
      Document_РеализацияТоваровУслуг -> False (main document)
      Document_РеализацияТоваровУслуг_Товары -> True (tabular part)
      Catalog_Номенклатура -> False (main catalog)
    """
    parts = entity_name.split("_", 1)
    if len(parts) < 2:
        return False
    # After removing "Document_" or "Catalog_", check if there's another "_"
    # that separates a Russian word (tabular part name)
    remainder = parts[1]
    # Tabular parts look like: ОсновнойДокумент_ИмяТабличнойЧасти
    # where _ИмяТабличнойЧасти starts with a Russian capital letter
    # We check: does the remainder contain an underscore followed by [А-ЯЁ]?
    if re.search(r"_[А-ЯЁ]", remainder):
        return True
    return False


def _generate_keywords(entity_name: str) -> List[str]:
    """Generate keywords for entity detection from its name.
    Catalog_Номенклатура -> ['номенклатур', 'номенклатура', 'товар', ...]
    """
    parts = entity_name.split("_", 1)
    if len(parts) < 2:
        return []

    russian_part = parts[1]

    # Split CamelCase by Russian capital letters
    words = re.findall(r"[А-ЯЁ][а-яёА-ЯЁ]*", russian_part)
    if not words:
        words = [russian_part]

    keywords = []
    for word in words:
        w_lower = word.lower()
        keywords.append(w_lower)
        # Stemming: remove 1-2 chars for word form matching
        if len(w_lower) > 4:
            keywords.append(w_lower[:-1])
        if len(w_lower) > 5:
            keywords.append(w_lower[:-2])

    # Add synonyms
    for kw in list(keywords):
        for stem, syns in _SYNONYM_MAP.items():
            if stem in kw or kw.startswith(stem):
                keywords.extend(syns)

    # Deduplicate, preserving order
    seen = set()
    result = []
    for k in keywords:
        if k not in seen and len(k) >= 2:
            seen.add(k)
            result.append(k)

    return result


def _build_select_fields(entity_name: str, all_fields: List[str]) -> List[str]:
    """Auto-detect business fields for SELECT."""
    is_catalog = entity_name.startswith("Catalog_")
    is_document = entity_name.startswith("Document_")

    if is_catalog:
        select = []
        if "Description" in all_fields:
            select.append("Description")
        if "Code" in all_fields:
            select.append("Code")
        for f in all_fields:
            if f in select or f in _SYSTEM_FIELDS:
                continue
            if f.endswith("_Key") or "@" in f or f.startswith("Predefined"):
                continue
            if f in ("DeletionMark", "IsFolder", "Owner", "Parent", "Description", "Code"):
                continue
            if re.search(r"[А-Яа-яЁё]", f):
                select.append(f)
        return select[:6]

    elif is_document:
        select = []
        for f in ["Number", "Date", "Posted", "СуммаДокумента", "Статус"]:
            if f in all_fields:
                select.append(f)
        for f in all_fields:
            if f in select or f in _SYSTEM_FIELDS:
                continue
            if f.endswith("_Key") or "@" in f or f.startswith("Predefined"):
                continue
            if f in ("DeletionMark", "IsFolder", "Posted", "Number", "Date"):
                continue
            if re.search(r"[А-Яа-яЁё]", f) and f.startswith("Сумма"):
                select.append(f)
        return select[:5]

    return []


def _parse_metadata_xml(xml_text: str) -> Dict[str, List[str]]:
    """Parse 1C OData $metadata XML. Returns {EntityName: [field1, field2, ...]}.
    EXCLUDES tabular parts.
    """
    root = ET.fromstring(xml_text)
    entities: Dict[str, List[str]] = {}

    for elem in root.iter():
        tag = elem.tag
        if "}" in tag:
            tag = tag.split("}", 1)[1]

        if tag == "EntityType":
            name = elem.get("Name", "")
            if not name.startswith(("Catalog_", "Document_")):
                continue
            # EXCLUDE tabular parts
            if _is_tabular_part(name):
                continue

            fields = []
            for prop in elem:
                prop_tag = prop.tag
                if "}" in prop_tag:
                    prop_tag = prop_tag.split("}", 1)[1]
                if prop_tag == "Property":
                    prop_name = prop.get("Name", "")
                    if prop_name and not prop_name.endswith("@navigationLinkUrl"):
                        fields.append(prop_name)

            if fields:
                entities[name] = fields

    return entities


def load_entity_config(force_refresh: bool = False) -> bool:
    """Load entity configuration: from cache or from 1C $metadata.
    Returns True if successful."""
    global ENTITY_FIELDS, SELECT_FIELDS, ISFOLDER_ENTITIES, ENTITY_KEYWORDS, ALL_ENTITY_NAMES

    # 1. Try loading from cache file
    if not force_refresh and os.path.exists(METADATA_CACHE_FILE):
        try:
            with open(METADATA_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            age = (
                datetime.now()
                - datetime.fromisoformat(cached.get("timestamp", "2000-01-01T00:00:00"))
            ).total_seconds()
            if age < 86400:
                ENTITY_FIELDS = cached.get("entity_fields", {})
                SELECT_FIELDS = cached.get("select_fields", {})
                ISFOLDER_ENTITIES = set(cached.get("isfolder_entities", []))
                ENTITY_KEYWORDS = cached.get("entity_keywords", {})
                ALL_ENTITY_NAMES = cached.get("all_entity_names", [])
                print(
                    f"[METADATA] Loaded from cache: {len(ALL_ENTITY_NAMES)} entities (age {int(age/60)} min)"
                )
                return True
        except Exception as e:
            print(f"[METADATA] Cache read error: {e}")

    # 2. Request $metadata from 1C
    try:
        session = requests.Session()
        session.auth = (C1_USERNAME, C1_PASSWORD)
        resp = session.get(f"{C1_ODATA_URL}/$metadata", timeout=30)
        resp.raise_for_status()
        xml_text = resp.text
        print(f"[METADATA] Got XML from 1C ({len(xml_text)} bytes)")
    except Exception as e:
        print(f"[METADATA] Cannot get $metadata from 1C: {e}")
        if not ENTITY_FIELDS:
            _apply_fallback_config()
        return False

    # Parse XML (tabular parts are excluded inside _parse_metadata_xml)
    raw_entities = _parse_metadata_xml(xml_text)
    print(f"[METADATA] Found {len(raw_entities)} entities (Catalog_* + Document_*, tabular parts excluded)")

    entity_fields = {}
    select_fields = {}
    isfolder_entities = set()
    entity_keywords = {}
    all_names = []

    for name, fields in sorted(raw_entities.items()):
        all_names.append(name)
        entity_fields[name] = fields

        if "IsFolder" in fields:
            isfolder_entities.add(name)

        sel = _build_select_fields(name, fields)
        if sel:
            select_fields[name] = sel

        kws = _generate_keywords(name)
        if kws:
            entity_keywords[name] = kws

    ENTITY_FIELDS = entity_fields
    SELECT_FIELDS = select_fields
    ISFOLDER_ENTITIES = isfolder_entities
    ENTITY_KEYWORDS = entity_keywords
    ALL_ENTITY_NAMES = all_names

    # Save cache
    try:
        cache_data = {
            "timestamp": datetime.now().isoformat(),
            "entity_fields": entity_fields,
            "select_fields": select_fields,
            "isfolder_entities": sorted(isfolder_entities),
            "entity_keywords": entity_keywords,
            "all_entity_names": all_names,
        }
        with open(METADATA_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        print(f"[METADATA] Cache saved to {METADATA_CACHE_FILE}")
    except Exception as e:
        print(f"[METADATA] Cache write error: {e}")

    return True


def _apply_fallback_config():
    """Minimal fallback if 1C is unavailable."""
    global ENTITY_FIELDS, SELECT_FIELDS, ISFOLDER_ENTITIES, ENTITY_KEYWORDS, ALL_ENTITY_NAMES

    ENTITY_FIELDS = {
        "Catalog_Номенклатура": ["Description", "Code", "Артикул", "DeletionMark", "IsFolder"],
        "Catalog_Контрагенты": ["Description", "DeletionMark"],
        "Catalog_Склады": ["Description", "DeletionMark", "IsFolder"],
        "Catalog_Организации": ["Description", "Code", "DeletionMark", "IsFolder"],
        "Document_РеализацияТоваровУслуг": [
            "Number", "Date", "DeletionMark", "Posted",
            "СуммаДокумента", "Статус", "Контрагент_Key", "Организация_Key", "Склад_Key",
        ],
        "Document_ПоступлениеТоваровУслуг": [
            "Number", "Date", "DeletionMark", "Posted",
            "Контрагент_Key", "Организация_Key", "Склад_Key",
        ],
    }
    SELECT_FIELDS = {
        "Catalog_Номенклатура": ["Description", "Code", "Артикул"],
        "Catalog_Контрагенты": ["Description"],
        "Catalog_Склады": ["Description"],
        "Catalog_Организации": ["Description", "Code"],
    }
    ISFOLDER_ENTITIES = {"Catalog_Номенклатура", "Catalog_Склады", "Catalog_Организации"}
    ENTITY_KEYWORDS = {
        "Catalog_Номенклатура": ["номенклатур", "товар", "продукц", "позици", "артикул"],
        "Catalog_Контрагенты": ["контрагент", "клиент", "покупател", "поставщик", "партнёр"],
        "Catalog_Склады": ["склад", "магазин"],
        "Catalog_Организации": ["организаци", "компани", "фирм", "юрлиц"],
        "Document_РеализацияТоваровУслуг": ["реализац", "продаж", "отгрузк"],
        "Document_ПоступлениеТоваровУслуг": ["поступлен", "приход", "закупк"],
    }
    ALL_ENTITY_NAMES = sorted(ENTITY_FIELDS.keys())
    print("[METADATA] Using fallback config (6 entities)")


# Load on module startup
load_entity_config()


# ======== Key field map: _Key -> Catalog_* ========
KEY_FIELD_MAP: Dict[str, str] = {}


def _build_key_field_map():
    """Build map: field_without_Key -> Catalog_entity.
    E.g. Document_РеализацияТоваровУслуг has Контрагент_Key ->
         Контрагент -> Catalog_Контрагенты
    """
    global KEY_FIELD_MAP
    key_map: Dict[str, str] = {}
    for entity_name, fields in ENTITY_FIELDS.items():
        if not entity_name.startswith("Document_"):
            continue
        for f in fields:
            if f.endswith("_Key") and not f.startswith(("Ref", "Owner", "Parent")):
                stem = f[:-4]  # Контрагент_Key -> Контрагент
                candidates = [
                    f"Catalog_{stem}ы",
                    f"Catalog_{stem}",
                    f"Catalog_{stem}а",
                    f"Catalog_{stem}и",
                ]
                if stem.endswith("я"):
                    candidates.append(f"Catalog_{stem[:-1]}и")
                    candidates.append(f"Catalog_{stem[:-1]}е")
                if stem.endswith("а"):
                    candidates.append(f"Catalog_{stem[:-1]}ы")
                if len(stem) > 3:
                    candidates.append(f"Catalog_{stem[:-1]}ы")
                    candidates.append(f"Catalog_{stem[:-1]}и")
                    candidates.append(f"Catalog_{stem[:-2]}ии")
                for c in candidates:
                    if c in ENTITY_FIELDS:
                        key_map[stem] = c
                        break
                else:
                    for cat in ENTITY_FIELDS:
                        if cat.startswith("Catalog_") and stem in cat:
                            key_map[stem] = cat
                            break
    KEY_FIELD_MAP = key_map
    if key_map:
        print(f"[KEY MAP] Document links: {json.dumps(key_map, ensure_ascii=False)}")


# ======== ENTITY DETECTION (with Document priority) ========

def detect_entity(question: str) -> Optional[str]:
    """Detect 1C entity by keywords. Documents have PRIORITY over Catalogs.

    When both a document keyword (e.g. "реализации") and a catalog keyword
    (e.g. "контрагент") are present, the DOCUMENT wins because it's the
    primary object of the query. The catalog reference is used as a filter
    (resolved later via _resolve_ref_filter).
    """
    q_lower = question.lower()
    matches: List[Tuple[int, str, str]] = []  # (score, entity, type)

    for entity, keywords in ENTITY_KEYWORDS.items():
        for kw in keywords:
            if kw in q_lower:
                score = len(kw)
                # Document_* gets +1000 bonus
                if entity.startswith("Document_"):
                    score += 1000
                matches.append(
                    (score, entity, "Document" if entity.startswith("Document_") else "Catalog")
                )
                break  # Only first matching keyword per entity

    if not matches:
        return None

    matches.sort(key=lambda x: x[0], reverse=True)
    best = matches[0][1]
    detected_type = matches[0][2]
    print(f"[ENTITY DETECTED] '{question[:60]}' -> {best} ({detected_type}, score={matches[0][0]})")
    return best


# ======================================================================
# COMPACT PER-ENTITY SYSTEM PROMPT (fits in 4096 context)
# ======================================================================

def _build_entity_prompt(entity: str) -> str:
    """Build a COMPACT system prompt for ONE specific entity.
    This keeps the prompt small enough for models with 4096 token context.
    """
    fields = ENTITY_FIELDS.get(entity, [])
    select = SELECT_FIELDS.get(entity, [])
    has_isfolder = entity in ISFOLDER_ENTITIES

    # Build reference hints for documents
    ref_hints = ""
    if entity.startswith("Document_"):
        ref_lines = []
        for f in fields:
            if f.endswith("_Key") and not f.startswith(("Ref", "Owner", "Parent")):
                stem = f[:-4]
                catalog = KEY_FIELD_MAP.get(stem, f"Catalog_{stem}ы")
                ref_lines.append(f"  {f} -> lookup in {catalog}")
        if ref_lines:
            ref_hints = (
                "\nСсылки на справочники (НЕ пиши substringof для _Key полей! "
                "Пиши substringof('текст',ИмяБез_Key) — система сама найдёт GUID):\n"
                + "\n".join(ref_lines)
            )

    isfolder_rule = ""
    if has_isfolder:
        isfolder_rule = "\nОбязательно: IsFolder eq false в фильтре"
    else:
        isfolder_rule = "\nIsFolder НЕ существует для этой сущности — НЕ добавлять!"

    select_str = ", ".join(select) if select else "не указывать"

    prompt = f"""Ты генератор OData-запросов для 1С УТ 11. Только JSON.

Сущность: {entity}
Поля: {', '.join(fields)}
$select: {select_str}
{isfolder_rule}{ref_hints}

ПРАВИЛА:
1. DeletionMark eq false — всегда
2. Поиск текста: substringof('текст',Field) eq true
3. Даты: datetime'YYYY-MM-DDTHH:MM:SS' (внутри кавычек, без Z)
4. ЗАПРЕЩЕНО: startswith, endswith, year(), month(), day(), contains(), like()
5. Document_*: НЕ указывай select
6. Фильтр по справочнику: substringof('ЖДС',Контрагент) — НЕ Description и НЕ Контрагент_Key

Примеры:
- номенклатура где Круг: {{"entity":"Catalog_Номенклатура","select":["Description","Code"],"filter":"substringof('Круг',Description) eq true and DeletionMark eq false","top":100}}
- реализации за май 2024: {{"entity":"Document_РеализацияТоваровУслуг","filter":"Date ge datetime'2024-05-01T00:00:00' and Date lt datetime'2024-06-01T00:00:00' and DeletionMark eq false","top":100}}
- реализации где контрагент ЖДС: {{"entity":"Document_РеализацияТоваровУслуг","filter":"substringof('ЖДС',Контрагент) eq true and DeletionMark eq false","top":100}}

Только JSON."""
    return prompt


# Build key field map on startup
_build_key_field_map()


# ======== Ref_Key resolution (two-step query) ========

async def _resolve_ref_filter(
    c1_client: "C1ODataClient", doc_entity: str, filter_expr: str
) -> str:
    """If filter contains substringof('text',CatalogField) and the document
    has CatalogField_Key — find GUID in catalog and substitute.

    Example: substringof('ЖДС',Контрагент) -> Контрагент_Key eq guid'abc-123'
    """
    if not KEY_FIELD_MAP:
        return filter_expr

    pattern = r"substringof\(\s*'([^']+)',\s*(\w+)\s*\)\s+eq\s+true"
    matches = list(re.finditer(pattern, filter_expr, re.IGNORECASE))

    if not matches:
        return filter_expr

    doc_fields = ENTITY_FIELDS.get(doc_entity, [])
    new_filter = filter_expr

    for match in reversed(matches):
        search_text = match.group(1)
        field_name = match.group(2)

        # Check: does the document have field_name_Key?
        key_field = f"{field_name}_Key"
        if key_field not in doc_fields:
            continue

        # Check: do we know the catalog for this field?
        catalog_entity = KEY_FIELD_MAP.get(field_name)
        if not catalog_entity:
            continue

        # Search in catalog
        print(f"[REF LOOKUP] Looking for {field_name}='{search_text}' in {catalog_entity}")
        try:
            catalog_filter = (
                f"substringof('{search_text}',Description) eq true "
                f"and DeletionMark eq false"
            )
            if catalog_entity in ISFOLDER_ENTITIES:
                catalog_filter += " and IsFolder eq false"
            catalog_results = c1_client.query(
                entity=catalog_entity,
                select=["Description", "Ref_Key"],
                filter_expr=catalog_filter,
                top=10,
            )
        except Exception as e:
            print(f"[REF LOOKUP ERROR] {e}")
            continue

        if not catalog_results:
            print(f"[REF LOOKUP] '{search_text}' not found in {catalog_entity}")
            continue

        ref_keys = [r["Ref_Key"] for r in catalog_results if r.get("Ref_Key")]
        if not ref_keys:
            continue

        names = [r.get("Description", "") for r in catalog_results[:3]]
        print(f"[REF LOOKUP] Found: {', '.join(names)} -> {len(ref_keys)} GUID(s)")

        # Build replacement filter
        if len(ref_keys) == 1:
            key_filter = f"{key_field} eq guid'{ref_keys[0]}'"
        else:
            parts = [f"{key_field} eq guid'{k}'" for k in ref_keys[:5]]
            key_filter = f"({' or '.join(parts)})"

        original_part = match.group(0)
        new_filter = new_filter.replace(original_part, key_filter, 1)

    # Clean up dangling and/or
    new_filter = re.sub(r"\s+", " ", new_filter).strip()
    new_filter = re.sub(r"\s+and\s+and\s+", " and ", new_filter)
    new_filter = re.sub(r"^and\s+", "", new_filter)
    new_filter = re.sub(r"\s+and$", "", new_filter)

    return new_filter


# ======== 1C OData Client ========
class C1ODataClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.auth = (C1_USERNAME, C1_PASSWORD)
        self.session.headers.update({"Accept": "application/json"})
        self.session.timeout = 30

    def _build_url(self, entity: str, params: Dict[str, str]) -> str:
        base = f"{C1_ODATA_URL.rstrip('/')}/{entity}"
        parts = []
        for key, value in params.items():
            encoded_value = urllib.parse.quote(str(value), safe="='(),%")
            parts.append(f"{key}={encoded_value}")
        return f"{base}?{'&'.join(parts)}"

    def _do_request(self, entity: str, params: Dict[str, str]) -> Tuple[int, str]:
        url = self._build_url(entity, params)
        print(f"[1C URL] {url[:400]}")
        resp = self.session.get(url)
        resp.encoding = "utf-8"
        return resp.status_code, resp.text

    def query(
        self,
        entity: str,
        select: Optional[List[str]] = None,
        filter_expr: Optional[str] = None,
        top: int = 100,
        orderby: Optional[str] = None,
    ) -> List[Dict]:
        variants = self._build_retry_variants(entity, select, filter_expr, top, orderby)
        last_error = "unknown error"

        for i, (variant_name, params) in enumerate(variants):
            try:
                status, body = self._do_request(entity, params)
                if status == 200:
                    data = json.loads(body)
                    results = data.get("value", [])
                    if i > 0:
                        print(
                            f"[RETRY SUCCESS] Variant #{i} ({variant_name}) worked! {len(results)} records"
                        )
                    else:
                        print(f"[1C OK] Returned {len(results)} records")
                    return results

                error_preview = body[:300] if body else "(empty)"
                print(
                    f"[1C ERROR] Variant #{i} ({variant_name}): HTTP {status} - {error_preview}"
                )
                last_error = f"HTTP {status}: {error_preview}"
                if status in (401, 403):
                    break
            except json.JSONDecodeError as e:
                print(f"[1C ERROR] Variant #{i} ({variant_name}): JSON parse - {e}")
                last_error = f"JSON parse error: {e}"
            except Exception as e:
                print(f"[1C ERROR] Variant #{i} ({variant_name}): {e}")
                last_error = str(e)

        raise ValueError(f"All 1C query variants failed. Last error: {last_error}")

    def _build_retry_variants(
        self,
        entity: str,
        select: Optional[List[str]],
        filter_expr: Optional[str],
        top: int,
        orderby: Optional[str],
    ) -> List[Tuple[str, Dict[str, str]]]:
        variants = []
        seen_params = set()

        def add_variant(name: str, params: Dict[str, str]):
            key = json.dumps(params, sort_keys=True)
            if key not in seen_params:
                seen_params.add(key)
                variants.append((name, params))

        # Full query
        params0: Dict[str, str] = {"$format": "json", "$top": str(top)}
        if select:
            params0["$select"] = ",".join(select)
        if filter_expr:
            params0["$filter"] = filter_expr
        if orderby:
            params0["$orderby"] = orderby
        add_variant("full", params0)

        # Without system fields in select
        if select:
            biz_select = [f for f in select if f not in ("DeletionMark", "IsFolder")]
            if biz_select and biz_select != select:
                params1: Dict[str, str] = {"$format": "json", "$top": str(top)}
                params1["$select"] = ",".join(biz_select)
                if filter_expr:
                    params1["$filter"] = filter_expr
                add_variant("select_business", params1)

        # Without $select
        params2: Dict[str, str] = {"$format": "json", "$top": str(top)}
        if filter_expr:
            params2["$filter"] = filter_expr
        add_variant("no_select", params2)

        # Without date filter
        if filter_expr and re.search(r"Date\s+(?:ge|le|gt|lt|eq)", filter_expr, re.IGNORECASE):
            filter_no_date = filter_expr
            filter_no_date = re.sub(
                r"Date\s+ge\s+\S+\s+and\s+", "", filter_no_date, flags=re.IGNORECASE
            )
            filter_no_date = re.sub(
                r"\s+and\s+Date\s+le\s+\S+", "", filter_no_date, flags=re.IGNORECASE
            )
            filter_no_date = re.sub(
                r"Date\s+(?:ge|le)\s+\S+", "", filter_no_date, flags=re.IGNORECASE
            )
            filter_no_date = re.sub(r"^\s*and\s+", "", filter_no_date)
            filter_no_date = re.sub(r"\s+and\s*$", "", filter_no_date)
            filter_no_date = filter_no_date.strip()
            if filter_no_date and filter_no_date != filter_expr:
                p_nd: Dict[str, str] = {"$format": "json", "$top": str(top)}
                p_nd["$filter"] = filter_no_date
                add_variant("no_date_filter", p_nd)

        # substringof -> like
        if filter_expr and "substringof" in filter_expr.lower():
            filter_like = self._convert_substringof_to_like(filter_expr)
            if filter_like != filter_expr:
                p4: Dict[str, str] = {"$format": "json", "$top": str(top)}
                p4["$filter"] = filter_like
                add_variant("like_instead_of_substringof", p4)

        # Remove DeletionMark
        if filter_expr and "DeletionMark" in filter_expr:
            simplified = self._remove_filter_part(
                filter_expr, r"DeletionMark\s+eq\s+false"
            )
            if simplified and simplified != filter_expr:
                p5: Dict[str, str] = {"$format": "json", "$top": str(top)}
                p5["$filter"] = simplified
                add_variant("no_DeletionMark", p5)

        # Remove IsFolder
        if filter_expr and "IsFolder" in filter_expr:
            simplified = self._remove_filter_part(
                filter_expr, r"IsFolder\s+eq\s+false"
            )
            if simplified and simplified != filter_expr:
                p6: Dict[str, str] = {"$format": "json", "$top": str(top)}
                p6["$filter"] = simplified
                add_variant("no_IsFolder", p6)

        # Text search only
        if filter_expr:
            text_match = re.search(
                r"(?:substringof\([^)]+\)\s+eq\s+true|like\([^)]+\)\s+eq\s+true)",
                filter_expr,
                flags=re.IGNORECASE,
            )
            if text_match and text_match.group(0) != filter_expr:
                p7: Dict[str, str] = {"$format": "json", "$top": str(top)}
                p7["$filter"] = text_match.group(0)
                add_variant("text_search_only", p7)

        # Bare entity fallback
        p8: Dict[str, str] = {"$format": "json", "$top": str(top)}
        add_variant("entity_top_only", p8)

        return variants

    def _convert_substringof_to_like(self, filter_expr: str) -> str:
        def replace_substringof(match):
            return f"like({match.group(2)},'%{match.group(1)}%') eq true"

        return re.sub(
            r"substringof\(\s*'([^']+)',\s*(\w+)\s*\)\s+eq\s+true",
            replace_substringof,
            filter_expr,
            flags=re.IGNORECASE,
        )

    def _remove_filter_part(self, filter_expr: str, pattern: str) -> str:
        result = filter_expr
        result = re.sub(r"\s+and\s+" + pattern, "", result, flags=re.IGNORECASE)
        result = re.sub(pattern + r"\s+and\s+", "", result, flags=re.IGNORECASE)
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)
        return result.strip()

    def close(self):
        self.session.close()


# ======== LLM Client ========
class LMStudioClient:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=120.0)

    async def chat(
        self, system_prompt: str, user_prompt: str, max_tokens: int = 1500
    ) -> str:
        payload = {
            "model": LMSTUDIO_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        try:
            resp = await self.client.post(LMSTUDIO_URL, json=payload)
            print(f"[LLM HTTP] Status: {resp.status_code}")
            if resp.status_code != 200:
                print(f"[LLM ERROR BODY] {resp.text[:500]}")
            resp.raise_for_status()
            response_data = resp.json()
            content = response_data["choices"][0]["message"]["content"]
            finish_reason = response_data["choices"][0].get("finish_reason", "")
            if finish_reason == "length":
                print(f"[LLM WARNING] Response truncated at max_tokens={max_tokens}")
            return content
        except httpx.HTTPStatusError as e:
            print(f"[LLM ERROR] {e.response.status_code}: {e.response.text[:500]}")
            # Fallback: combine prompts into user message
            payload["messages"] = [
                {"role": "user", "content": f"{system_prompt}\n\n{user_prompt}"}
            ]
            payload["max_tokens"] = min(max_tokens, 800)
            try:
                resp = await self.client.post(LMSTUDIO_URL, json=payload)
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
            except Exception as e2:
                print(f"[LLM FALLBACK ERROR] {e2}")
                # Last resort: just user message, very short
                payload["messages"] = [{"role": "user", "content": user_prompt}]
                payload["max_tokens"] = 300
                resp = await self.client.post(LMSTUDIO_URL, json=payload)
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]

    async def close(self):
        await self.client.aclose()


# ======== JSON Parser ========
def extract_json(text: str) -> Dict:
    # Strip thinking/reasoning blocks
    text = re.sub(r"<think_>.*?</think_>", "", text, flags=re.DOTALL)
    text = re.sub(r"<![CDATA[.*?]]>", "", text, flags=re.DOTALL)

    # Try code blocks first
    blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    for block in blocks:
        block = block.strip()
        if block.startswith("{") and block.endswith("}"):
            try:
                return json.loads(block)
            except Exception:
                pass

    # Try finding JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass

    # Try nested JSON regex
    for match in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL):
        try:
            return json.loads(match.group())
        except Exception:
            pass

    # Last resort: extract entity and filter via regex
    entity_match = re.search(r'"entity"\s*:\s*"([^"]+)"', text)
    filter_match = re.search(r'"filter"\s*:\s*"([^"]+)"', text)
    select_match = re.search(r'"select"\s*:\s*(\[[^\]]*\])', text)
    top_match = re.search(r'"top"\s*:\s*(\d+)', text)

    if entity_match and filter_match:
        result = {
            "entity": entity_match.group(1),
            "filter": filter_match.group(1),
            "top": int(top_match.group(1)) if top_match else 100,
        }
        if select_match:
            try:
                result["select"] = json.loads(select_match.group(1))
            except Exception:
                pass
        return result

    raise ValueError(f"Cannot extract JSON. Response: {text[:300]}")


# ======== Summary Cleaner ========
def clean_summary(text: str) -> str:
    text = re.sub(r"<think_>.*?</think_>", "", text, flags=re.DOTALL)
    text = re.sub(r" elas.*?(?=\n)", "", text, flags=re.DOTALL)

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in reversed(lines):
        if len(line) > 10 and any(c.isdigit() for c in line):
            return line
    if lines:
        return lines[-1]
    return text.strip()


# ======== Result Formatting ========
def _format_results_list(entity: str, results: List[Dict]) -> List[str]:
    if not results:
        return []
    lines = []
    first = results[0]
    if "Description" in first:
        for r in results:
            desc = r.get("Description", "").strip()
            code = r.get("Code", "").strip() if r.get("Code") else ""
            lines.append(f"{desc}  [{code}]" if code else desc)
    else:
        for r in results:
            num = r.get("Number", "?")
            dt = r.get("Date", "?")[:10] if r.get("Date") else "?"
            summ = r.get("СуммаДокумента", r.get("СуммаВзаиморасчетов", ""))
            posted = "+" if r.get("Posted") else "-"
            status = r.get("Статус", "")
            parts = [f"#{num}", f"from {dt}"]
            if summ and summ != 0:
                parts.append(f"sum={summ}")
            if status:
                parts.append(status)
            parts.append(posted)
            lines.append("  ".join(parts))
    return lines


# ======== API Endpoints ========

@app.get("/")
async def root():
    return {
        "service": "1C-LM Studio Bridge",
        "version": "11.0",
        "entities_loaded": len(ALL_ENTITY_NAMES),
        "entities_sample": ALL_ENTITY_NAMES[:10],
        "endpoints": [
            'POST /ask - simple: {"q": "show nomenclature with word Krug"}',
            "POST /query - NL query to 1C via LLM (with entity_hint)",
            "POST /test - direct test: url or entity+filter",
            "GET /metadata - list all entities from $metadata",
            "POST /metadata/refresh - refresh metadata cache",
            "GET /cache/stats - cache statistics",
            "POST /cache/clear - clear cache",
        ],
    }


@app.get("/metadata")
async def get_metadata():
    return {
        "total_entities": len(ALL_ENTITY_NAMES),
        "entities": ALL_ENTITY_NAMES,
        "entity_fields": {k: v for k, v in ENTITY_FIELDS.items()},
        "select_fields": {k: v for k, v in SELECT_FIELDS.items()},
        "isfolder_entities": sorted(ISFOLDER_ENTITIES),
        "keywords_sample": {k: v for k, v in list(ENTITY_KEYWORDS.items())[:10]},
        "key_field_map": KEY_FIELD_MAP,
    }


@app.post("/metadata/refresh")
async def metadata_refresh():
    success = load_entity_config(force_refresh=True)
    _build_key_field_map()
    return {
        "success": success,
        "entities_count": len(ALL_ENTITY_NAMES),
        "entities": ALL_ENTITY_NAMES,
        "key_field_map": KEY_FIELD_MAP,
    }


@app.post("/query")
async def natural_language_query(request: QueryRequest):
    start_time = datetime.now()

    entity_hint = request.entity_hint
    if not entity_hint:
        entity_hint = detect_entity(request.user_question)

    if not entity_hint:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot detect entity. Try adding entity_hint. "
            f"Available keywords: {', '.join(kw for kws in list(ENTITY_KEYWORDS.values())[:10] for kw in kws[:2])}",
        )

    cache_key_val = _cache_key(request)
    cached = _get_cached(cache_key_val)
    if cached:
        cached["execution_time_ms"] = 0.5
        cached["cached"] = True
        return UnicodeJSONResponse(
            content=cached, media_type="application/json; charset=utf-8"
        )

    c1_client = C1ODataClient()
    llm_client = LMStudioClient()

    try:
        # BUILD COMPACT PER-ENTITY PROMPT (not the global one!)
        system_prompt = _build_entity_prompt(entity_hint)

        hint_instruction = f"\nUse ONLY entity '{entity_hint}'."
        user_prompt = (
            f"Question: {request.user_question}\n"
            f"Hint: {entity_hint}{hint_instruction}\n\n"
            f"OData query (JSON only):"
        )

        llm_response = await llm_client.chat(system_prompt, user_prompt)
        print(f"[LLM RAW] {llm_response[:500]}")

        query_plan = extract_json(llm_response)
        print(f"[PARSED] {json.dumps(query_plan, ensure_ascii=False)}")

        # === SAFEGUARDS ===

        # Force entity to the detected one
        original_entity = query_plan.get("entity", "")
        if original_entity != entity_hint:
            print(f"[ENTITY FIXED] '{original_entity}' -> '{entity_hint}'")
            query_plan["entity"] = entity_hint

        entity = query_plan.get("entity", "")
        filter_expr = query_plan.get("filter", "")
        select = query_plan.get("select")

        if not entity or not re.match(r"^(Catalog_|Document_)[А-Яа-яЁё\w]+$", entity):
            entity = entity_hint
            query_plan["entity"] = entity

        # Validate entity exists
        if entity not in ENTITY_FIELDS and entity not in ALL_ENTITY_NAMES:
            print(f"[ENTITY UNKNOWN] '{entity}' - not in metadata, trying anyway")

        # Documents: no select, no IsFolder
        if entity.startswith("Document_"):
            if "select" in query_plan:
                del query_plan["select"]
                select = None
            if "IsFolder" in filter_expr:
                filter_expr = re.sub(
                    r"\s*and\s+IsFolder\s+eq\s+false", "", filter_expr, flags=re.IGNORECASE
                )
                filter_expr = re.sub(
                    r"IsFolder\s+eq\s+false\s*and\s*", "", filter_expr, flags=re.IGNORECASE
                )
                filter_expr = re.sub(r"IsFolder\s+eq\s+false", "", filter_expr, flags=re.IGNORECASE)
                query_plan["filter"] = filter_expr.strip()

        # IsFolder only for entities that have it
        if entity not in ISFOLDER_ENTITIES and "IsFolder" in filter_expr:
            filter_expr = re.sub(
                r"\s*and\s+IsFolder\s+eq\s+false", "", filter_expr, flags=re.IGNORECASE
            )
            filter_expr = re.sub(
                r"IsFolder\s+eq\s+false\s*and\s*", "", filter_expr, flags=re.IGNORECASE
            )
            filter_expr = re.sub(r"IsFolder\s+eq\s+false", "", filter_expr, flags=re.IGNORECASE)
            query_plan["filter"] = filter_expr.strip()

        # Filter select fields
        if select:
            known = ENTITY_FIELDS.get(entity, [])
            safe_select = (
                [f for f in select if f in known and f not in ("DeletionMark", "IsFolder")]
                if known
                else select
            )
            if safe_select:
                query_plan["select"] = safe_select
                select = safe_select
            else:
                default_select = SELECT_FIELDS.get(entity)
                if default_select:
                    query_plan["select"] = default_select
                    select = default_select
                else:
                    if "select" in query_plan:
                        del query_plan["select"]
                    select = None

        # year()/month() -> date range
        filter_expr = query_plan.get("filter", "")
        if "year(" in filter_expr.lower() or "month(" in filter_expr.lower():
            year_match = re.search(r"year\(Date\)\s+eq\s+(\d{4})", filter_expr, re.IGNORECASE)
            month_match = re.search(
                r"month\(Date\)\s+eq\s+(\d{1,2})", filter_expr, re.IGNORECASE
            )
            if year_match and month_match:
                year = int(year_match.group(1))
                month = int(month_match.group(1))
                if month == 12:
                    next_month = datetime(year + 1, 1, 1)
                else:
                    next_month = datetime(year, month + 1, 1)
                last_day = (next_month - timedelta(days=1)).day
                new_filter = (
                    f"Date ge datetime'{year:04d}-{month:02d}-01T00:00:00' "
                    f"and Date le datetime'{year:04d}-{month:02d}-{last_day:02d}T23:59:59'"
                )
                other = re.sub(
                    r"year\(Date\)\s+eq\s+\d{4}\s+and\s+", "", filter_expr, flags=re.IGNORECASE
                )
                other = re.sub(r"month\(Date\)\s+eq\s+\d{1,2}\s+and\s+", "", other, flags=re.IGNORECASE)
                other = re.sub(r"and\s+year\(Date\)\s+eq\s+\d{4}", "", other, flags=re.IGNORECASE)
                other = re.sub(r"and\s+month\(Date\)\s+eq\s+\d{1,2}", "", other, flags=re.IGNORECASE)
                other = other.strip()
                if other:
                    new_filter += f" and {other}"
                query_plan["filter"] = new_filter
            elif year_match:
                year = int(year_match.group(1))
                new_filter = (
                    f"Date ge datetime'{year:04d}-01-01T00:00:00' "
                    f"and Date le datetime'{year:04d}-12-31T23:59:59'"
                )
                other = re.sub(r"year\(Date\)\s+eq\s+\d{4}", "", filter_expr, flags=re.IGNORECASE).strip(" and ")
                if other:
                    new_filter += f" and {other}"
                query_plan["filter"] = new_filter

        # datetime'...' auto-fix
        filter_expr = query_plan.get("filter", "")
        if filter_expr:
            filter_expr = re.sub(r"(\d{2}:\d{2}:\d{2})Z", r"\1", filter_expr)
            filter_expr = re.sub(
                r"(Date\s+(?:ge|le|gt|lt|eq)\s+)(?!datetime')(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
                r"\1datetime'\2'",
                filter_expr,
                flags=re.IGNORECASE,
            )
            filter_expr = re.sub(
                r"(Date\s+(?:ge|le|gt|lt|eq)\s+)'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})'",
                r"\1datetime'\2'",
                filter_expr,
                flags=re.IGNORECASE,
            )
            query_plan["filter"] = filter_expr

        # startswith/endswith/like -> substringof
        filter_expr = query_plan.get("filter", "")
        if filter_expr:
            orig = filter_expr
            filter_expr = re.sub(
                r"startswith\((\w+),\s*'([^']+)'\)\s+eq\s+true",
                r"substringof('\2',\1) eq true",
                filter_expr,
                flags=re.IGNORECASE,
            )
            filter_expr = re.sub(
                r"endswith\((\w+),\s*'([^']+)'\)\s+eq\s+true",
                r"substringof('\2',\1) eq true",
                filter_expr,
                flags=re.IGNORECASE,
            )
            filter_expr = re.sub(
                r"like\((\w+),\s*'%([^']+)%'\)\s+eq\s+true",
                r"substringof('\2',\1) eq true",
                filter_expr,
                flags=re.IGNORECASE,
            )
            if filter_expr != orig:
                query_plan["filter"] = filter_expr

        print(f"[FILTER FINAL] {query_plan.get('filter', '')}")

        # === REF_KEY RESOLUTION ===
        filter_expr = query_plan.get("filter", "")
        if entity.startswith("Document_") and filter_expr and KEY_FIELD_MAP:
            resolved_filter = await _resolve_ref_filter(c1_client, entity, filter_expr)
            if resolved_filter != filter_expr:
                print(f"[REF RESOLVED] {filter_expr}")
                print(f"[REF RESOLVED] -> {resolved_filter}")
                query_plan["filter"] = resolved_filter
                filter_expr = resolved_filter

        # === CALL 1C ===
        results = c1_client.query(
            entity=query_plan["entity"],
            select=query_plan.get("select"),
            filter_expr=query_plan.get("filter"),
            top=query_plan.get("top", request.max_results),
            orderby=query_plan.get("orderby"),
        )

        summary_raw = await _generate_summary(llm_client, request.user_question, results)
        summary_clean = clean_summary(summary_raw)

        execution_time = (datetime.now() - start_time).total_seconds() * 1000
        results_list = _format_results_list(entity, results)

        response_data = {
            "sql_like_query": json.dumps(query_plan, ensure_ascii=False),
            "odata_url": _build_odata_url(query_plan),
            "results_list": results_list,
            "count": len(results),
            "llm_summary": summary_clean,
            "execution_time_ms": execution_time,
            "cached": False,
            "raw_results": results,
        }

        _set_cached(cache_key_val, response_data)
        return UnicodeJSONResponse(
            content=response_data, media_type="application/json; charset=utf-8"
        )

    except Exception as e:
        import traceback

        print(f"[ERROR] {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        c1_client.close()
        await llm_client.close()


@app.post("/ask")
async def ask_simple(request: AskRequest):
    query_req = QueryRequest(user_question=request.q)
    return await natural_language_query(query_req)


@app.post("/test")
async def test_odata(request: SimpleTestRequest):
    client = C1ODataClient()
    try:
        if request.url:
            status, body = client._do_raw_request(request.url)
            try:
                data = json.loads(body)
                results = data.get("value", data)
                return UnicodeJSONResponse(
                    content={
                        "status": status,
                        "url": request.url,
                        "count": len(results) if isinstance(results, list) else "N/A",
                        "results": results,
                    },
                    media_type="application/json; charset=utf-8",
                )
            except Exception:
                return UnicodeJSONResponse(
                    content={"status": status, "url": request.url, "raw": body[:2000]},
                    media_type="application/json; charset=utf-8",
                )

        if request.entity:
            params: Dict[str, str] = {"$format": "json", "$top": str(request.top)}
            if request.filter:
                params["$filter"] = request.filter
            url = client._build_url(request.entity, params)
            status, body = client._do_raw_request(url)
            try:
                data = json.loads(body)
                results = data.get("value", [])
                return UnicodeJSONResponse(
                    content={
                        "status": status,
                        "entity": request.entity,
                        "filter": request.filter,
                        "url": url[:500],
                        "count": len(results),
                        "results": results,
                    },
                    media_type="application/json; charset=utf-8",
                )
            except Exception:
                return UnicodeJSONResponse(
                    content={"status": status, "url": url[:500], "raw": body[:2000]},
                    media_type="application/json; charset=utf-8",
                )

        raise HTTPException(status_code=400, detail="Specify 'url' or 'entity'")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


# ======== Helper Functions ========


async def _generate_summary(
    llm: LMStudioClient, question: str, results: List[Dict]
) -> str:
    if not results:
        return "Nothing found."

    first = results[0]
    if "Description" in first:
        names = [r.get("Description", "") for r in results[:10]]
        names_str = "; ".join(n for n in names if n)
        examples = names_str + f" Total: {len(results)}."
    else:
        doc_parts = []
        for r in results[:10]:
            num = r.get("Number", "?")
            dt = r.get("Date", "?")[:10] if r.get("Date") else "?"
            summ = r.get("СуммаДокумента", r.get("СуммаВзаиморасчетов", ""))
            posted = "Posted" if r.get("Posted") else "Not posted"
            status = r.get("Статус", "")
            doc_parts.append(f"#{num} from {dt} sum={summ} ({posted}) {status}")
        examples = "; ".join(doc_parts) + f" Total: {len(results)}."

    prompt = (
        f"Question: {question}\n"
        f"Found: {len(results)} records. Examples: {examples}\n\n"
        f"Brief summary in Russian (1-2 sentences):"
    )
    system = "1C analyst. Brief summary in Russian with facts and numbers. No reasoning. No tags."
    return await llm.chat(system, prompt, max_tokens=500)


def _build_odata_url(plan: Dict) -> str:
    base = f"{C1_ODATA_URL}/{plan['entity']}"
    parts = ["$format=json"]
    if "select" in plan:
        parts.append(f"$select={','.join(plan['select'])}")
    if "filter" in plan:
        parts.append(f"$filter={plan['filter']}")
    if "top" in plan:
        parts.append(f"$top={plan['top']}")
    if "orderby" in plan:
        parts.append(f"$orderby={plan['orderby']}")
    return f"{base}?{'&'.join(parts)}"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
