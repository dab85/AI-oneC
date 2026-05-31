import os
import json
import re
import hashlib
import httpx
import requests
import urllib.parse
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="1C ↔ LM Studio Bridge")


# ======== Кастомный JSON с ensure_ascii=False ========
class UnicodeJSONResponse(JSONResponse):
    """JSONResponse, который НЕ эскейпит кириллицу в \\uXXXX."""
    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
        ).encode("utf-8")

# ======== Конфигурация ========
C1_ODATA_URL = os.getenv("C1_ODATA_URL", "http://localhost/ut/odata/standard.odata")
C1_USERNAME = os.getenv("C1_USERNAME", "da")
C1_PASSWORD = os.getenv("C1_PASSWORD", "123")
LMSTUDIO_URL = os.getenv("LMSTUDIO_URL", "http://localhost:1234/v1/chat/completions")
LMSTUDIO_MODEL = os.getenv("LMSTUDIO_MODEL", "local-model")

# ======== КЭШ ========
_cache: Dict[str, Any] = {}
_cache_ttl_seconds = 300


def _cache_key(request: "QueryRequest") -> str:
    raw = f"{request.user_question}:{request.entity_hint}:{request.max_results}"
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


def _get_cached(key: str) -> Optional[Any]:
    if key not in _cache:
        return None
    entry = _cache[key]
    if datetime.now() > entry["expires"]:
        del _cache[key]
        return None
    return entry["data"]


def _set_cached(key: str, data: Any, ttl: int = _cache_ttl_seconds):
    _cache[key] = {
        "data": data,
        "expires": datetime.now() + timedelta(seconds=ttl)
    }


@app.get("/cache/stats")
async def cache_stats():
    total = len(_cache)
    expired = sum(1 for v in _cache.values() if datetime.now() > v["expires"])
    return {"total_entries": total, "expired": expired, "active": total - expired, "ttl_seconds": _cache_ttl_seconds}


@app.post("/cache/clear")
async def cache_clear():
    count = len(_cache)
    _cache.clear()
    return {"cleared": count}


# ======== Модели данных ========
class QueryRequest(BaseModel):
    """Полный запрос: вопрос + опциональная подсказка сущности."""
    user_question: str
    entity_hint: Optional[str] = None  # Необязательно — автоопределение по тексту
    max_results: int = 50


class AskRequest(BaseModel):
    """Простейший запрос: только текст вопроса.
    Сущность определяется автоматически по ключевым словам.
    Примеры:
      {"q": "покажи номенклатуру со словом Круг"}
      {"q": "реализации за май 2024"}
      {"q": "контрагенты на букву В"}
      {"q": "все склады"}
    """
    q: str


class CreateRequest(BaseModel):
    entity: str
    data: Dict[str, Any]


class UpdateRequest(BaseModel):
    entity: str
    ref_key: str
    data: Dict[str, Any]


class SimpleTestRequest(BaseModel):
    """Простой запрос: адрес 1С OData + фильтр/параметры в теле.
    Всё в body — никаких кириллических параметров в URL-пути."""
    url: Optional[str] = None          # Полный URL (для прямого запроса)
    entity: Optional[str] = None       # Имя сущности (альтернатива url)
    filter: Optional[str] = None       # Сырой $filter
    top: int = 10


# ======== Схема полей по сущностям ========
ENTITY_FIELDS = {
    "Catalog_Номенклатура": ["Description", "Code", "Артикул", "DeletionMark", "IsFolder"],
    "Catalog_Контрагенты": ["Description", "DeletionMark"],  # НЕТ Code! НЕТ IsFolder!
    "Catalog_Склады": ["Description", "DeletionMark", "IsFolder"],
    "Catalog_Организации": ["Description", "Code", "DeletionMark", "IsFolder"],
    "Document_РеализацияТоваровУслуг": [
        "Number", "Date", "DeletionMark", "Posted",
        "СуммаДокумента", "Статус", "ХозяйственнаяОперация",
        "Организация_Key", "Контрагент_Key", "Склад_Key",
    ],
    "Document_ПоступлениеТоваровУслуг": ["Number", "Date", "DeletionMark", "Posted"],
}

SELECT_FIELDS = {
    "Catalog_Номенклатура": ["Description", "Code", "Артикул"],
    "Catalog_Контрагенты": ["Description"],
    "Catalog_Склады": ["Description"],
    "Catalog_Организации": ["Description", "Code"],
    "Document_РеализацияТоваровУслуг": ["Number", "Date", "Posted", "СуммаДокумента"],
    "Document_ПоступлениеТоваровУслуг": ["Number", "Date", "Posted"],
}

ISFOLDER_ENTITIES = {"Catalog_Номенклатура", "Catalog_Склады", "Catalog_Организации"}

# ======== Автоопределение сущности по тексту вопроса ========
ENTITY_KEYWORDS: Dict[str, List[str]] = {
    "Catalog_Номенклатура": [
        "номенклатур", "товар", "продукц", "позици", "артикул", "номенклат",
    ],
    "Catalog_Контрагенты": [
        "контрагент", "клиент", "покупател", "поставщик", "партнёр", "партнер",
    ],
    "Catalog_Склады": [
        "склад", "магазин", "помещени",
    ],
    "Catalog_Организации": [
        "организаци", "компани", "фирм", "юрлиц", "юридическ",
    ],
    "Document_РеализацияТоваровУслуг": [
        "реализаци", "продаж", "отгрузк", "реализац",
    ],
    "Document_ПоступлениеТоваровУслуг": [
        "поступлен", "приход", "закупк", "приобретен",
    ],
}


def detect_entity(question: str) -> Optional[str]:
    """Определяет сущность 1С по ключевым словам в вопросе.
    Возвращает имя сущности или None."""
    q_lower = question.lower()
    matches: List[Tuple[int, str]] = []  # (score, entity)

    for entity, keywords in ENTITY_KEYWORDS.items():
        for kw in keywords:
            if kw in q_lower:
                # Длинное совпадение = более точное
                matches.append((len(kw), entity))
                break  # Одно совпадение на сущность достаточно

    if not matches:
        return None

    # Сортируем по длине ключевого слова (самое длинное = самое точное)
    matches.sort(key=lambda x: x[0], reverse=True)
    best = matches[0][1]
    print(f"[ENTITY DETECTED] '{question[:60]}' → {best}")
    return best


# ======== Клиент 1С OData ========
class C1ODataClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.auth = (C1_USERNAME, C1_PASSWORD)
        self.session.headers.update({"Accept": "application/json"})
        self.session.timeout = 30

    def _build_url(self, entity: str, params: Dict[str, str]) -> str:
        """Собирает URL с параметрами, кодируя пробелы как %20."""
        base = f"{C1_ODATA_URL.rstrip('/')}/{entity}"
        parts = []
        for key, value in params.items():
            encoded_value = urllib.parse.quote(str(value), safe="='(),%")
            parts.append(f"{key}={encoded_value}")
        return f"{base}?{'&'.join(parts)}"

    def _do_request(self, entity: str, params: Dict[str, str]) -> Tuple[int, str]:
        """Выполняет HTTP-запрос к 1С OData по сущности."""
        url = self._build_url(entity, params)
        print(f"[1C URL] {url[:400]}")
        resp = self.session.get(url)
        resp.encoding = 'utf-8'  # Принудительно UTF-8 для кириллицы
        return resp.status_code, resp.text

    def _do_raw_request(self, url: str) -> Tuple[int, str]:
        """Выполняет HTTP-запрос по прямому URL."""
        print(f"[1C RAW URL] {url[:400]}")
        resp = self.session.get(url)
        resp.encoding = 'utf-8'  # Принудительно UTF-8 для кириллицы
        return resp.status_code, resp.text

    def query(self, entity: str,
              select: Optional[List[str]] = None,
              filter_expr: Optional[str] = None,
              top: int = 100,
              orderby: Optional[str] = None) -> List[Dict]:
        """Запрос к 1С OData с ПРОГРЕССИВНЫМ RETRY."""
        variants = self._build_retry_variants(entity, select, filter_expr, top, orderby)

        last_error = "неизвестная ошибка"

        for i, (variant_name, params) in enumerate(variants):
            try:
                status, body = self._do_request(entity, params)

                if status == 200:
                    data = json.loads(body)
                    results = data.get("value", [])
                    if i > 0:
                        print(f"[RETRY SUCCESS] Вариант #{i} ({variant_name}) сработал! Возвращено {len(results)} записей")
                    else:
                        print(f"[1C OK] Возвращено {len(results)} записей")
                    return results

                error_preview = body[:300] if body else "(пустой ответ)"
                print(f"[1C ERROR] Вариант #{i} ({variant_name}): HTTP {status} — {error_preview}")
                last_error = f"HTTP {status}: {error_preview}"

                if status in (401, 403):
                    break

            except json.JSONDecodeError as e:
                print(f"[1C ERROR] Вариант #{i} ({variant_name}): JSON parse error — {e}")
                last_error = f"JSON parse error: {e}"
            except Exception as e:
                print(f"[1C ERROR] Вариант #{i} ({variant_name}): {e}")
                last_error = str(e)

        raise ValueError(f"Все варианты запроса к 1С не удались. Последняя ошибка: {last_error}")

    def _build_retry_variants(self, entity: str,
                               select: Optional[List[str]],
                               filter_expr: Optional[str],
                               top: int,
                               orderby: Optional[str]) -> List[Tuple[str, Dict[str, str]]]:
        """Строит список вариантов запроса от полного к простому."""
        variants = []
        seen_params = set()

        def add_variant(name: str, params: Dict[str, str]):
            key = json.dumps(params, sort_keys=True)
            if key not in seen_params:
                seen_params.add(key)
                variants.append((name, params))

        # --- Вариант 0: Полный запрос ---
        params0: Dict[str, str] = {"$format": "json", "$top": str(top)}
        if select:
            params0["$select"] = ",".join(select)
        if filter_expr:
            params0["$filter"] = filter_expr
        if orderby:
            params0["$orderby"] = orderby
        add_variant("полный", params0)

        # --- Вариант 1: $select без DeletionMark/IsFolder ---
        if select:
            biz_select = [f for f in select if f not in ("DeletionMark", "IsFolder")]
            if biz_select and biz_select != select:
                params1: Dict[str, str] = {"$format": "json", "$top": str(top)}
                params1["$select"] = ",".join(biz_select)
                if filter_expr:
                    params1["$filter"] = filter_expr
                add_variant("select_бизнес", params1)

        # --- Вариант 2: Без $select ---
        params2: Dict[str, str] = {"$format": "json", "$top": str(top)}
        if filter_expr:
            params2["$filter"] = filter_expr
        add_variant("без_select", params2)

        # --- Варианты дат (упрощённо после тестирования) ---
        # Подтверждённый рабочий формат: Date ge datetime'2021-01-01T00:00:00'
        # datetime'...' — ЕДИНСТВЕННЫЙ формат, который 1С OData принимает и корректно фильтрует
        # '...' без datetime — парсится, но возвращает 0 результатов
        # голая дата — 400 ошибка парсера
        if filter_expr and re.search(r"Date\s+(?:ge|le|gt|lt|eq)", filter_expr, re.IGNORECASE):
            # Вариант: убрать фильтр по дате (оставить другие условия)
            filter_no_date = filter_expr
            filter_no_date = re.sub(r"Date\s+ge\s+\S+\s+and\s+", '', filter_no_date, flags=re.IGNORECASE)
            filter_no_date = re.sub(r"\s+and\s+Date\s+le\s+\S+", '', filter_no_date, flags=re.IGNORECASE)
            filter_no_date = re.sub(r"Date\s+(?:ge|le)\s+\S+", '', filter_no_date, flags=re.IGNORECASE)
            filter_no_date = re.sub(r"^\s*and\s+", '', filter_no_date)
            filter_no_date = re.sub(r"\s+and\s*$", '', filter_no_date)
            filter_no_date = filter_no_date.strip()

            if filter_no_date and filter_no_date != filter_expr:
                p_nd: Dict[str, str] = {"$format": "json", "$top": str(top)}
                p_nd["$filter"] = filter_no_date
                add_variant("без_фильтра_дат", p_nd)

        # --- substringof → like ---
        if filter_expr and "substringof" in filter_expr.lower():
            filter_like = self._convert_substringof_to_like(filter_expr)
            if filter_like != filter_expr:
                p4: Dict[str, str] = {"$format": "json", "$top": str(top)}
                p4["$filter"] = filter_like
                add_variant("like_вместо_substringof", p4)

        # --- Убираем DeletionMark ---
        if filter_expr and "DeletionMark" in filter_expr:
            simplified = self._remove_filter_part(filter_expr, r"DeletionMark\s+eq\s+false")
            if simplified and simplified != filter_expr:
                p5: Dict[str, str] = {"$format": "json", "$top": str(top)}
                p5["$filter"] = simplified
                add_variant("без_DeletionMark", p5)

        # --- Убираем IsFolder ---
        if filter_expr and "IsFolder" in filter_expr:
            simplified = self._remove_filter_part(filter_expr, r"IsFolder\s+eq\s+false")
            if simplified and simplified != filter_expr:
                p6: Dict[str, str] = {"$format": "json", "$top": str(top)}
                p6["$filter"] = simplified
                add_variant("без_IsFolder", p6)

        # --- Только текстовый поиск ---
        if filter_expr:
            text_match = re.search(
                r"(?:substringof\([^)]+\)\s+eq\s+true|like\([^)]+\)\s+eq\s+true)",
                filter_expr, flags=re.IGNORECASE
            )
            if text_match:
                text_filter = text_match.group(0)
                if text_filter != filter_expr:
                    p7: Dict[str, str] = {"$format": "json", "$top": str(top)}
                    p7["$filter"] = text_filter
                    add_variant("только_текстовый_поиск", p7)

        # --- Фоллбэк: только entity + top ---
        p8: Dict[str, str] = {"$format": "json", "$top": str(top)}
        add_variant("только_entity_top", p8)

        return variants

    def _convert_substringof_to_like(self, filter_expr: str) -> str:
        def replace_substringof(match):
            value = match.group(1)
            field = match.group(2)
            return f"like({field},'%{value}%') eq true"
        return re.sub(
            r"substringof\(\s*'([^']+)',\s*(\w+)\s*\)\s+eq\s+true",
            replace_substringof, filter_expr, flags=re.IGNORECASE
        )

    def _remove_filter_part(self, filter_expr: str, pattern: str) -> str:
        result = filter_expr
        result = re.sub(r'\s+and\s+' + pattern, '', result, flags=re.IGNORECASE)
        result = re.sub(pattern + r'\s+and\s+', '', result, flags=re.IGNORECASE)
        result = re.sub(pattern, '', result, flags=re.IGNORECASE)
        return result.strip()

    def get_by_key(self, entity: str, ref_key: str) -> Dict:
        url = f"{C1_ODATA_URL}/{entity}(guid'{ref_key}')?$format=json"
        resp = self.session.get(url)
        resp.raise_for_status()
        return resp.json()

    def create(self, entity: str, data: Dict) -> Dict:
        url = f"{C1_ODATA_URL}/{entity}"
        resp = self.session.post(url, json=data, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        return resp.json()

    def update(self, entity: str, ref_key: str, data: Dict):
        url = f"{C1_ODATA_URL}/{entity}(guid'{ref_key}')"
        resp = self.session.patch(url, json=data, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        return True

    def delete(self, entity: str, ref_key: str):
        url = f"{C1_ODATA_URL}/{entity}(guid'{ref_key}')"
        resp = self.session.delete(url)
        resp.raise_for_status()
        return True

    def get_metadata(self) -> str:
        url = f"{C1_ODATA_URL}/$metadata"
        resp = self.session.get(url)
        resp.raise_for_status()
        return resp.text

    def close(self):
        self.session.close()


# ======== LLM-клиент ========
class LMStudioClient:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=120.0)

    async def chat(self, system_prompt: str, user_prompt: str, max_tokens: int = 3000) -> str:
        payload = {
            "model": LMSTUDIO_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens
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
                print(f"[LLM WARNING] Ответ обрезан по max_tokens={max_tokens}")

            return content

        except httpx.HTTPStatusError as e:
            print(f"[LLM ERROR] {e.response.status_code}: {e.response.text[:500]}")
            print("[LLM FALLBACK] Trying user-only...")
            payload["messages"] = [{"role": "user", "content": f"{system_prompt}\n\n{user_prompt}"}]

            try:
                resp = await self.client.post(LMSTUDIO_URL, json=payload)
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
            except Exception as e2:
                print(f"[LLM FALLBACK ERROR] {e2}")
                print("[LLM FALLBACK 2] Trying minimal...")
                payload["messages"] = [{"role": "user", "content": user_prompt}]
                payload["max_tokens"] = 300
                resp = await self.client.post(LMSTUDIO_URL, json=payload)
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]

    async def close(self):
        await self.client.aclose()


# ======== СИСТЕМНЫЙ ПРОМПТ (v9) ========
SYSTEM_PROMPT = """Ты генератор OData-запросов для 1С УТ 11. Отвечай ТОЛЬКО одной строкой JSON.

ПРАВИЛА:
1. select: только бизнес-поля. НЕ включай DeletionMark, IsFolder в select.
2. filter: DeletionMark eq false — всегда. IsFolder eq false — ТОЛЬКО где указано ниже.
3. Поиск по тексту: substringof('текст',Field) eq true — ЕДИНСТВЕННЫЙ рабочий способ.
   Если просят «на букву В» — пиши substringof('В',Description).
   Если просят «содержит Круг» — пиши substringof('Круг',Description).
4. Формат дат: Date ge datetime'2024-05-01T00:00:00' and Date le datetime'2024-05-31T23:59:59'
   ОБЯЗАТЕЛЬНО datetime'...' вокруг даты! Это литерал типа datetime для 1С.
   БЕЗ Z на конце. Пример: Date ge datetime'2024-05-01T00:00:00'
5. ЗАПРЕЩЕНО: startswith, endswith, year(), month(), day(), contains(), like()
6. Document_*: НЕ указывай select (1C вернёт все поля сама)

Сущности:
- Catalog_Номенклатура → select: [Description,Code,Артикул], IsFolder eq false: ДА
- Catalog_Контрагенты → select: [Description], IsFolder: НЕТ! Code: НЕТ!
- Catalog_Склады → select: [Description], IsFolder eq false: ДА
- Catalog_Организации → select: [Description,Code], IsFolder eq false: ДА
- Document_РеализацияТоваровУслуг → без select, IsFolder: НЕТ
  Ключевые поля: Number, Date, Posted, СуммаДокумента, Статус, Контрагент_Key, Склад_Key
- Document_ПоступлениеТоваровУслуг → без select, IsFolder: НЕТ

Примеры:
{"entity":"Catalog_Номенклатура","select":["Description","Code","Артикул"],"filter":"substringof('Круг',Description) eq true and DeletionMark eq false and IsFolder eq false","top":100}
{"entity":"Catalog_Контрагенты","select":["Description"],"filter":"substringof('В',Description) eq true and DeletionMark eq false","top":100}
{"entity":"Document_РеализацияТоваровУслуг","filter":"Date ge datetime'2024-05-01T00:00:00' and Date le datetime'2024-05-31T23:59:59' and DeletionMark eq false","top":100}
{"entity":"Catalog_Склады","select":["Description"],"filter":"DeletionMark eq false and IsFolder eq false","top":100}
{"entity":"Catalog_Организации","select":["Description","Code"],"filter":"DeletionMark eq false and IsFolder eq false","top":100}

Только JSON."""


# ======== Парсер JSON ========
def extract_json(text: str) -> Dict:
    # Убираем reasoning-блоки разных моделей
    text = re.sub(r'<think_>.*?</think_>', '', text, flags=re.DOTALL)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'⋞.*?⋟', '', text, flags=re.DOTALL)
    text = re.sub(r'elas.*?(?=\{)', '', text, flags=re.DOTALL)  # Убираем 'elas' блоки перед JSON

    blocks = re.findall(r'```(?:json)?\s*(.*?)```', text, re.DOTALL)
    for block in blocks:
        block = block.strip()
        if block.startswith('{') and block.endswith('}'):
            try:
                return json.loads(block)
            except:
                pass

    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end+1])
        except:
            pass

    for match in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL):
        try:
            return json.loads(match.group())
        except:
            pass

    print(f"[JSON FALLBACK] Trying regex extraction from: {text[:200]}")
    entity_match = re.search(r'"entity"\s*:\s*"([^"]+)"', text)
    filter_match = re.search(r'"filter"\s*:\s*"([^"]+)"', text)
    select_match = re.search(r'"select"\s*:\s*(\[[^\]]*\])', text)
    top_match = re.search(r'"top"\s*:\s*(\d+)', text)

    if entity_match and filter_match:
        result = {
            "entity": entity_match.group(1),
            "filter": filter_match.group(1),
            "top": int(top_match.group(1)) if top_match else 100
        }
        if select_match:
            try:
                result["select"] = json.loads(select_match.group(1))
            except:
                pass
        print(f"[JSON FALLBACK] Extracted: {result}")
        return result

    raise ValueError(f"Не удалось извлечь JSON. Ответ: {text[:300]}")


# ======== Очистка резюме ========
def clean_summary(text: str) -> str:
    # Убираем reasoning-блоки
    text = re.sub(r'⋞.*?⋟', '', text, flags=re.DOTALL)
    text = re.sub(r'<think_>.*?</think_>', '', text, flags=re.DOTALL)
    text = re.sub(r'elas.*?(?=\n)', '', text, flags=re.DOTALL)
    text = re.sub(r'\s*\ticker .*?', '', text, flags=re.DOTALL)
    # Убираем типичные «рассуждения» LLM
    text = re.sub(r'(?i)пользователь просит.*?(?=\n|$)', '', text)
    text = re.sub(r'(?i)согласно логике.*?(?=\n|$)', '', text)
    text = re.sub(r'(?i)я должен.*?(?=\n|$)', '', text)
    text = re.sub(r'(?i)согласно инструкций.*?(?=\n|$)', '', text)
    text = re.sub(r'(?i)смотрю на.*?(?=\n|$)', '', text)
    text = re.sub(r'(?i)нужно использовать.*?(?=\n|$)', '', text)
    text = re.sub(r'(?i)используем.*?сущность.*?(?=\n|$)', '', text)

    lines = [l.strip() for l in text.split('\n') if l.strip()]
    # Ищем самую содержательную строку с фактами
    for line in reversed(lines):
        if len(line) > 10 and any(c.isdigit() for c in line):
            return line
    if lines:
        return lines[-1]
    return text.strip()


# ======== API Endpoints ========

@app.get("/")
async def root():
    return {
        "service": "1C ↔ LM Studio Bridge",
        "version": "10.1",
        "endpoints": [
            "POST /ask — проще простого: {\"q\": \"покажи номенклатуру со словом Круг\"}",
            "POST /query — NL-запрос к 1С через LLM (с entity_hint)",
            "POST /test — прямой тест: url или entity+filter в теле",
            "GET /metadata — метаданные OData",
            "POST /create — создать объект",
            "PATCH /update — обновить объект",
            "DELETE /delete/{entity}/{ref_key} — удалить",
            "GET /entity/{entity}/{ref_key} — получить по GUID",
            "GET /cache/stats — статистика кэша",
            "POST /cache/clear — очистить кэш"
        ]
    }


@app.post("/query")
async def natural_language_query(request: QueryRequest):
    start_time = datetime.now()

    # Автоопределение сущности если entity_hint не задан
    entity_hint = request.entity_hint
    if not entity_hint:
        entity_hint = detect_entity(request.user_question)

    cache_key_val = _cache_key(request)
    cached = _get_cached(cache_key_val)
    if cached:
        print(f"[CACHE HIT] {cache_key_val[:8]}...")
        cached["execution_time_ms"] = 0.5
        cached["cached"] = True
        return UnicodeJSONResponse(content=cached, media_type="application/json; charset=utf-8")

    c1_client = C1ODataClient()
    llm_client = LMStudioClient()

    try:
        hint_instruction = ""
        if entity_hint:
            hint_instruction = f"\nИспользуй ТОЛЬКО сущность '{entity_hint}'."

        user_prompt = f"Вопрос: {request.user_question}\nПодсказка: {entity_hint or 'авто'}{hint_instruction}\n\nOData запрос (только JSON):"

        llm_response = await llm_client.chat(SYSTEM_PROMPT, user_prompt)
        print(f"[LLM RAW] {llm_response[:500]}")

        query_plan = extract_json(llm_response)
        print(f"[PARSED] {json.dumps(query_plan, ensure_ascii=False)}")

        # === ЗАЩИТЫ ===
        if entity_hint:
            original_entity = query_plan.get("entity", "")
            if original_entity != entity_hint:
                print(f"[ENTITY FIXED] '{original_entity}' -> '{entity_hint}'")
                query_plan["entity"] = entity_hint

        entity = query_plan.get("entity", "")
        filter_expr = query_plan.get("filter", "")
        select = query_plan.get("select")

        if not entity or not re.match(r'^(Catalog_|Document_)[А-Яа-яЁё\w]+$', entity):
            if entity_hint:
                entity = entity_hint
                query_plan["entity"] = entity
            else:
                raise ValueError(f"Не удалось определить сущность. Укажите в запросе: номенклатура, контрагенты, склады, организации, реализации или поступления.")

        # Документы: без select, без IsFolder
        if entity.startswith("Document_"):
            if "select" in query_plan:
                del query_plan["select"]
                select = None
                print(f"[SELECT FIXED] Removed $select for document")
            if "IsFolder" in filter_expr:
                filter_expr = re.sub(r'\s*and\s+IsFolder\s+eq\s+false', '', filter_expr, flags=re.IGNORECASE)
                filter_expr = re.sub(r'IsFolder\s+eq\s+false\s*and\s*', '', filter_expr, flags=re.IGNORECASE)
                filter_expr = re.sub(r'IsFolder\s+eq\s+false', '', filter_expr, flags=re.IGNORECASE)
                query_plan["filter"] = filter_expr.strip()

        # IsFolder только для разрешённых сущностей
        if entity not in ISFOLDER_ENTITIES and "IsFolder" in filter_expr:
            filter_expr = re.sub(r'\s*and\s+IsFolder\s+eq\s+false', '', filter_expr, flags=re.IGNORECASE)
            filter_expr = re.sub(r'IsFolder\s+eq\s+false\s*and\s*', '', filter_expr, flags=re.IGNORECASE)
            filter_expr = re.sub(r'IsFolder\s+eq\s+false', '', filter_expr, flags=re.IGNORECASE)
            query_plan["filter"] = filter_expr.strip()

        # Фильтруем select
        if select:
            known = ENTITY_FIELDS.get(entity, [])
            safe_select = [f for f in select if f in known and f not in ("DeletionMark", "IsFolder")]
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

        # year()/month() → диапазон дат
        filter_expr = query_plan.get("filter", "")
        if "year(" in filter_expr.lower() or "month(" in filter_expr.lower():
            year_match = re.search(r'year\(Date\)\s+eq\s+(\d{4})', filter_expr, re.IGNORECASE)
            month_match = re.search(r'month\(Date\)\s+eq\s+(\d{1,2})', filter_expr, re.IGNORECASE)
            if year_match and month_match:
                year = int(year_match.group(1))
                month = int(month_match.group(1))
                if month == 12:
                    next_month = datetime(year + 1, 1, 1)
                else:
                    next_month = datetime(year, month + 1, 1)
                last_day = (next_month - timedelta(days=1)).day
                new_filter = f"Date ge datetime'{year:04d}-{month:02d}-01T00:00:00' and Date le datetime'{year:04d}-{month:02d}-{last_day:02d}T23:59:59'"
                other = re.sub(r'year\(Date\)\s+eq\s+\d{4}\s+and\s+', '', filter_expr, flags=re.IGNORECASE)
                other = re.sub(r'month\(Date\)\s+eq\s+\d{1,2}\s+and\s+', '', other, flags=re.IGNORECASE)
                other = re.sub(r'and\s+year\(Date\)\s+eq\s+\d{4}', '', other, flags=re.IGNORECASE)
                other = re.sub(r'and\s+month\(Date\)\s+eq\s+\d{1,2}', '', other, flags=re.IGNORECASE)
                other = other.strip()
                if other:
                    new_filter += f" and {other}"
                query_plan["filter"] = new_filter
            elif year_match:
                year = int(year_match.group(1))
                new_filter = f"Date ge datetime'{year:04d}-01-01T00:00:00' and Date le datetime'{year:04d}-12-31T23:59:59'"
                other = re.sub(r'year\(Date\)\s+eq\s+\d{4}', '', filter_expr, flags=re.IGNORECASE).strip(' and ')
                if other:
                    new_filter += f" and {other}"
                query_plan["filter"] = new_filter

        # === НОВАЯ ЗАЩИТА: добавляем datetime'...' если LLM выдал голую дату ===
        filter_expr = query_plan.get("filter", "")
        if filter_expr:
            # Убираем Z на конце дат
            filter_expr = re.sub(r"(\d{2}:\d{2}:\d{2})Z", r"\1", filter_expr)

            # Если в фильтре есть Date ge/le с голой датой (без datetime'), оборачиваем
            # Паттерн: Date ge YYYY-MM-DDTHH:MM:SS (не preceded by datetime')
            filter_expr = re.sub(
                r"(Date\s+(?:ge|le|gt|lt|eq)\s+)(?!datetime')(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
                r"\1datetime'\2'",
                filter_expr, flags=re.IGNORECASE
            )

            # Если Date ge 'YYYY-MM-DDTHH:MM:SS' (в кавычках без datetime), заменяем
            filter_expr = re.sub(
                r"(Date\s+(?:ge|le|gt|lt|eq)\s+)'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})'",
                r"\1datetime'\2'",
                filter_expr, flags=re.IGNORECASE
            )

            query_plan["filter"] = filter_expr

        # startswith/endswith/like → substringof
        filter_expr = query_plan.get("filter", "")
        if filter_expr:
            orig = filter_expr
            filter_expr = re.sub(r"startswith\((\w+),\s*'([^']+)'\)\s+eq\s+true", r"substringof('\2',\1) eq true", filter_expr, flags=re.IGNORECASE)
            filter_expr = re.sub(r"endswith\((\w+),\s*'([^']+)'\)\s+eq\s+true", r"substringof('\2',\1) eq true", filter_expr, flags=re.IGNORECASE)
            filter_expr = re.sub(r"like\((\w+),\s*'%([^']+)%'\)\s+eq\s+true", r"substringof('\2',\1) eq true", filter_expr, flags=re.IGNORECASE)
            filter_expr = re.sub(r"like\((\w+),\s*'([^']+)%'\)\s+eq\s+true", r"substringof('\2',\1) eq true", filter_expr, flags=re.IGNORECASE)
            filter_expr = re.sub(r"like\((\w+),\s*'%([^']+)'\)\s+eq\s+true", r"substringof('\2',\1) eq true", filter_expr, flags=re.IGNORECASE)
            if filter_expr != orig:
                query_plan["filter"] = filter_expr

        # Контрагенты: без Code
        if entity == "Catalog_Контрагенты" and select and "Code" in select:
            select = [f for f in select if f != "Code"]
            query_plan["select"] = select or ["Description"]

        print(f"[FILTER FINAL] {query_plan.get('filter', '')}")

        # === ВЫЗОВ 1С ===
        results = c1_client.query(
            entity=query_plan["entity"],
            select=query_plan.get("select"),
            filter_expr=query_plan.get("filter"),
            top=query_plan.get("top", request.max_results),
            orderby=query_plan.get("orderby")
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

        return UnicodeJSONResponse(content=response_data, media_type="application/json; charset=utf-8")

    except Exception as e:
        import traceback
        print(f"[ERROR] {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        c1_client.close()
        await llm_client.close()


# ======== ПРОСТОЙ ТЕСТОВЫЙ ЭНДПОИНТ ========

@app.post("/ask")
async def ask_simple(request: AskRequest):
    """Самый простой эндпоинт. Просто текст вопроса — сущность определяется автоматически.

    Примеры запросов (PowerShell):
      $utf8 = New-Object System.Text.UTF8Encoding($false)

      $r = Invoke-RestMethod -Uri 'http://localhost:8000/ask' -Method Post `
        -ContentType 'application/json; charset=utf-8' `
        -Body $utf8.GetBytes('{"q": "покажи номенклатуру со словом Круг"}')

      # Выводить списком:
      $r.results_list

      # Или всё сразу:
      $r | Select-Object results_list, count, llm_summary | Format-List

    Ключевые слова для автоопределения:
      номенклатура/товар → Catalog_Номенклатура
      контрагент/клиент  → Catalog_Контрагенты
      склад              → Catalog_Склады
      организация/фирма  → Catalog_Организации
      реализация/продажа → Document_РеализацияТоваровУслуг
      поступление/закупка → Document_ПоступлениеТоваровУслуг
    """
    # Перенаправляем на /query с автоопределением сущности
    query_req = QueryRequest(user_question=request.q)
    return await natural_language_query(query_req)


@app.post("/test")
async def test_odata(request: SimpleTestRequest):
    """Простой тестовый эндпоинт. Всё в теле запроса — никаких кириллических параметров в URL.

    Вариант 1 — передать полный URL:
      $body = @{url='http://localhost/ut/odata/standard.odata/Document_РеализацияТоваровУслуг?$format=json&$top=1'} | ConvertTo-Json
      $utf8 = New-Object System.Text.UTF8Encoding($false)
      Invoke-RestMethod -Uri 'http://localhost:8000/test' -Method Post -ContentType 'application/json; charset=utf-8' -Body $utf8.GetBytes($body)

    Вариант 2 — передать entity + filter:
      $body = @{entity='Document_РеализацияТоваровУслуг'; filter="Date ge datetime'2021-01-01T00:00:00'"; top=3} | ConvertTo-Json
      Invoke-RestMethod -Uri 'http://localhost:8000/test' -Method Post -ContentType 'application/json; charset=utf-8' -Body $utf8.GetBytes($body)

    TIP: Если кириллица отображается кракозябрами в PowerShell:
      [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
      $OutputEncoding = [System.Text.Encoding]::UTF8
      chcp 65001
      $r = Invoke-RestMethod ...; $r | ConvertTo-Json -Depth 5 | Out-String
    """
    client = C1ODataClient()
    try:
        # Если передан полный URL — используем его напрямую
        if request.url:
            status, body = client._do_raw_request(request.url)
            try:
                data = json.loads(body)
                results = data.get("value", data)
                return UnicodeJSONResponse(
                    content={"status": status, "url": request.url,
                             "count": len(results) if isinstance(results, list) else "N/A",
                             "results": results},
                    media_type="application/json; charset=utf-8"
                )
            except:
                return UnicodeJSONResponse(
                    content={"status": status, "url": request.url, "raw": body[:2000]},
                    media_type="application/json; charset=utf-8"
                )

        # Если передана entity — строим URL
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
                    content={"status": status, "entity": request.entity,
                             "filter": request.filter, "url": url[:500],
                             "count": len(results), "results": results},
                    media_type="application/json; charset=utf-8"
                )
            except:
                return UnicodeJSONResponse(
                    content={"status": status, "url": url[:500], "raw": body[:2000]},
                    media_type="application/json; charset=utf-8"
                )

        raise HTTPException(status_code=400, detail="Укажите 'url' или 'entity' в теле запроса")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


@app.get("/metadata")
async def get_metadata():
    client = C1ODataClient()
    try:
        xml_text = client.get_metadata()
        entities = []
        for match in re.finditer(r'EntityType\s+Name="([^"]+)"', xml_text):
            entities.append(match.group(1))
        return {"odata_url": C1_ODATA_URL, "entities": sorted(entities), "count": len(entities)}
    finally:
        client.close()


@app.get("/entity/{entity}/{ref_key}")
async def get_entity_by_key(
    entity: str = Path(..., description="Имя сущности"),
    ref_key: str = Path(..., description="GUID объекта")
):
    client = C1ODataClient()
    try:
        return client.get_by_key(entity, ref_key)
    finally:
        client.close()


@app.post("/create")
async def create_entity(request: CreateRequest):
    client = C1ODataClient()
    try:
        result = client.create(request.entity, request.data)
        _cache.clear()
        return {"success": True, "created": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


@app.patch("/update")
async def update_entity(request: UpdateRequest):
    client = C1ODataClient()
    try:
        client.update(request.entity, request.ref_key, request.data)
        _cache.clear()
        return {"success": True, "updated": request.ref_key}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


@app.delete("/delete/{entity}/{ref_key}")
async def delete_entity(entity: str = Path(...), ref_key: str = Path(...)):
    client = C1ODataClient()
    try:
        client.delete(entity, ref_key)
        _cache.clear()
        return {"success": True, "deleted": ref_key}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


# ======== Вспомогательные функции ========

def _format_results_list(entity: str, results: List[Dict]) -> List[str]:
    """Форматирует результаты 1С в читаемый список строк.
    Каталоги: 'Description  [Code]'
    Документы: '№Number  от Date  сумма=СуммаДокумента  Статус'
    """
    if not results:
        return []

    lines = []
    first = results[0]

    if "Description" in first:
        # Каталог
        for r in results:
            desc = r.get("Description", "").strip()
            code = r.get("Code", "").strip() if r.get("Code") else ""
            if code:
                lines.append(f"{desc}  [{code}]")
            else:
                lines.append(desc)
    else:
        # Документ
        for r in results:
            num = r.get("Number", "?")
            dt = r.get("Date", "?")[:10] if r.get("Date") else "?"
            summ = r.get("СуммаДокумента", r.get("СуммаВзаиморасчетов", ""))
            posted = "✓" if r.get("Posted") else "✗"
            status = r.get("Статус", "")
            summ_str = f"сумма={summ}" if summ and summ != 0 else ""
            parts = [f"№{num}", f"от {dt}"]
            if summ_str:
                parts.append(summ_str)
            if status:
                parts.append(status)
            parts.append(posted)
            lines.append("  ".join(parts))

    return lines


async def _generate_summary(llm: LMStudioClient, question: str, results: List[Dict]) -> str:
    if not results:
        return "По запросу ничего не найдено."
    # Для каталогов — Description, для документов — Number + Date + СуммаДокумента
    first = results[0]
    if "Description" in first:
        names = [r.get("Description", "") for r in results[:10]]
        names_str = "; ".join(n for n in names if n)
        extra = f" Всего: {len(results)}."
        examples = names_str + extra
    else:
        # Документ: показываем номер, дату, сумму
        doc_parts = []
        for r in results[:10]:
            num = r.get("Number", "?")
            dt = r.get("Date", "?")[:10] if r.get("Date") else "?"
            summ = r.get("СуммаДокумента", r.get("СуммаВзаиморасчетов", "?"))
            posted = "Проведён" if r.get("Posted") else "Не проведён"
            status = r.get("Статус", "")
            doc_parts.append(f"№{num} от {dt} сумма={summ} ({posted}) {status}")
        extra = f" Всего: {len(results)}."
        examples = "; ".join(doc_parts) + extra
    prompt = f"Вопрос: {question}\nНайдено: {len(results)} записей. Примеры: {examples}\n\nКраткое резюме на русском (1-2 предложения):"
    system = "Аналитик 1С. Краткое резюме на русском языке с фактами и числами. Без рассуждений. Без размышлений. Без тегов."
    return await llm.chat(system, prompt, max_tokens=1000)


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
