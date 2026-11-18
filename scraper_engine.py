"""
scraper_engine.py
=================
Motor de recolección de CochesRadar España.

Responsabilidades
-----------------
1. Configuración de los portales españoles (URLs de búsqueda por marca/modelo).
2. Descarga asíncrona y paralela (httpx + asyncio) con rotación de User-Agents,
   reintentos, detección de bloqueos (403/429/captcha/DataDome/Cloudflare) y
   soporte opcional de proxies mediante variables de entorno o `st.secrets`:
       ZENROWS_API_KEY   -> https://api.zenrows.com
       SCRAPERAPI_KEY    -> https://api.scraperapi.com
       PROXY_URL         -> proxy HTTP(S) genérico (http://user:pass@host:port)
3. Extracción genérica de anuncios desde JSON-LD (schema.org Car/Product),
   `__NEXT_DATA__`, estados embebidos (`window.__INITIAL_STATE__`...) y APIs JSON.
4. Normalización a un esquema común + inferencia de campos ausentes a partir del
   catálogo (plazas, carrocería, etiqueta DGT).
5. Modo *fallback* inteligente: si un portal bloquea (habitual desde IPs de nube),
   se generan anuncios de referencia realistas y deterministas, coherentes con
   los filtros, marcados como `origen_dato = "Simulado"` y enlazados a la página
   de búsqueda REAL del portal.
6. Filtrado unificado, métricas de mercado (precio estimado / oportunidades) y
   caché `@st.cache_data`.

Modos (variable COCHESRADAR_MODE o selector en la interfaz):
    auto  -> intenta datos reales y usa fallback por portal si falla (por defecto)
    real  -> solo datos reales (sin fallback)
    demo  -> solo datos de referencia (sin peticiones de red; ideal para demos)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable
from urllib.parse import quote_plus, urlencode, urljoin

import base64

import httpx
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

import spain_geo_cars as geo

try:  # Streamlit es opcional para poder usar el motor desde scripts/tests.
    import streamlit as st

    _cache_data = st.cache_data
except Exception:  # pragma: no cover
    st = None

    def _cache_data(*_args, **_kwargs):
        def deco(fn):
            return fn

        return deco


CURRENT_YEAR = datetime.now().year
OPPORTUNITY_THRESHOLD = 15  # % por debajo del valor estimado para considerar "oportunidad"
REQUEST_TIMEOUT = float(os.getenv("COCHESRADAR_TIMEOUT", "12"))
MAX_CONCURRENCY = int(os.getenv("COCHESRADAR_CONCURRENCY", "6"))

# Columnas del esquema normalizado (orden de la tabla / exportación).
LISTING_COLUMNS = [
    "id", "portal", "titulo", "marca", "modelo", "version", "precio", "anio", "km",
    "combustible", "cambio", "carroceria", "plazas", "etiqueta_dgt", "estado",
    "vendedor", "garantia", "garantia_meses", "potencia_cv", "color", "ciudad",
    "provincia", "comunidad", "lat", "lon", "url", "imagen", "fecha_publicacion",
    "origen_dato",
]


# ---------------------------------------------------------------------------
# FILTROS
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SearchFilters:
    """Todos los criterios de búsqueda. Inmutable y hashable (clave de caché)."""

    portals: tuple[str, ...] = ()
    community: str = geo.ALL_OPTION
    province: str = geo.ALL_OPTION
    city: str = geo.ALL_OPTION
    radius_km: int = 0
    brand: str = geo.ALL_OPTION
    model: str = "Todos"
    keywords: str = ""
    year_min: int = 2005
    year_max: int = CURRENT_YEAR
    price_min: int = 0
    price_max: int = 200_000
    km_min: int = 0
    km_max: int = 400_000
    conditions: tuple[str, ...] = ()
    seats: str = "Todas"
    fuels: tuple[str, ...] = ()
    transmission: str = "Todos"
    bodies: tuple[str, ...] = ()
    dgt_label: str = "Todas"
    seller: str = "Todos"
    warranty: str = "Todos"
    include_imports: bool = True
    max_per_portal: int = 40
    mode: str = "auto"

    @property
    def has_brand(self) -> bool:
        return self.brand not in ("", geo.ALL_OPTION, None)

    @property
    def has_model(self) -> bool:
        return self.model not in ("", "Todos", None)

    @property
    def query_text(self) -> str:
        """Texto libre para portales de clasificados: 'seat leon 7 plazas'."""
        parts = []
        if self.has_brand:
            parts.append(self.brand)
        if self.has_model:
            parts.append(self.model)
        if self.keywords:
            parts.append(self.keywords)
        return " ".join(parts).strip()

    @property
    def location_active(self) -> bool:
        return any(v not in (geo.ALL_OPTION, "", None) for v in (self.community, self.province, self.city))


# ---------------------------------------------------------------------------
# PORTALES
# ---------------------------------------------------------------------------
def _bm(f: SearchFilters) -> tuple[str, str]:
    """Slugs de marca y modelo para construir URLs."""
    return (geo.slugify(f.brand) if f.has_brand else "", geo.slugify(f.model) if f.has_model else "")


@dataclass(frozen=True)
class PortalConfig:
    key: str
    name: str
    domain: str
    kind: str  # "Portal" | "Clasificados"
    color: str
    url_builder: Callable[[SearchFilters], str]
    pro_only: bool = False
    pro_share: float = 0.7
    warranty_rate: float = 0.75
    conditions: tuple[tuple[str, float], ...] = (("Segunda mano", 0.55), ("Seminuevo", 0.3), ("Km 0", 0.1), ("Nuevo", 0.05))
    country: str = "ES"
    api: bool = False  # La URL devuelve JSON (API pública)


def _cochesnet(f):
    b, m = _bm(f)
    path = "/".join(p for p in (b, m) if p)
    base = f"https://www.coches.net/{path}/segunda-mano/" if path else "https://www.coches.net/segunda-mano/"
    return base + "?" + urlencode({"MinPrice": f.price_min, "MaxPrice": f.price_max, "MinYear": f.year_min, "MaxYear": f.year_max, "MaxKms": f.km_max, "or": "-1"})


def _autoscout(f):
    b, m = _bm(f)
    path = "/".join(p for p in (b, m) if p)
    params = {"atype": "C", "cy": "E", "damaged_listing": "exclude", "desc": "0", "sort": "price", "ustate": "N,U",
              "pricefrom": f.price_min, "priceto": f.price_max, "fregfrom": f.year_min, "fregto": f.year_max, "kmfrom": f.km_min, "kmto": f.km_max}
    return f"https://www.autoscout24.es/lst/{path}?" + urlencode(params) if path else "https://www.autoscout24.es/lst?" + urlencode(params)


def _cochescom(f):
    b, m = _bm(f)
    slug = "-".join(p for p in (b, m) if p)
    return f"https://www.coches.com/coches-segunda-mano/{slug}.htm" if slug else "https://www.coches.com/coches-segunda-mano/coches-ocasion.htm"


def _autocasion(f):
    b, m = _bm(f)
    if b and m:
        return f"https://www.autocasion.com/coches-segunda-mano/{b}-ocasion/{m}"
    if b:
        return f"https://www.autocasion.com/coches-segunda-mano/{b}-ocasion"
    return "https://www.autocasion.com/coches-segunda-mano"


def _motores(f):
    b, m = _bm(f)
    path = "/".join(p for p in (b, m) if p)
    return f"https://www.motor.es/coches-segunda-mano/{path}/" if path else "https://www.motor.es/coches-segunda-mano/"


def _flexicar(f):
    b, m = _bm(f)
    path = "/".join(p for p in (b, m) if p)
    return f"https://www.flexicar.es/{path}/segunda-mano/" if path else "https://www.flexicar.es/coches-segunda-mano/"


def _clicars(f):
    b, m = _bm(f)
    slug = "-".join(p for p in (b, m) if p)
    return f"https://www.clicars.com/coches-segunda-mano-ocasion/comprar-{slug}" if slug else "https://www.clicars.com/coches-segunda-mano-ocasion"


def _ocasionplus(f):
    b, m = _bm(f)
    path = "/".join(p for p in (b, m) if p)
    return f"https://www.ocasionplus.com/coches-segunda-mano/{path}" if path else "https://www.ocasionplus.com/coches-segunda-mano"


def _spoticar(f):
    b, m = _bm(f)
    path = "/".join(p for p in (b, m) if p)
    return f"https://www.spoticar.es/coches-segunda-mano/{path}" if path else "https://www.spoticar.es/coches-segunda-mano"


def _dasweltauto(f):
    b, m = _bm(f)
    path = "/".join(p for p in (b, m) if p)
    return f"https://www.dasweltauto.es/coches-ocasion/{path}" if path else "https://www.dasweltauto.es/coches-ocasion"


def _milanuncios(f):
    params = {"s": f.query_text, "desde": f.price_min, "hasta": f.price_max, "anod": f.year_min, "anoh": f.year_max, "kilometrosh": f.km_max, "orden": "baratos"}
    return "https://www.milanuncios.com/coches-de-segunda-mano/?" + urlencode({k: v for k, v in params.items() if v not in ("", None)})


def _wallapop(f):
    """API pública de búsqueda de Wallapop (categoría 100 = Coches)."""
    loc = geo.city_index().get(geo.normalize_text(f.city)) if f.city != geo.ALL_OPTION else None
    lat, lon = (loc["lat"], loc["lon"]) if loc else (40.4168, -3.7038)
    params = {"category_ids": "100", "keywords": f.query_text or "coche", "latitude": lat, "longitude": lon,
              "min_sale_price": f.price_min, "max_sale_price": f.price_max, "min_year": f.year_min, "max_year": f.year_max,
              "max_km": f.km_max, "order_by": "price_low_to_high", "source": "search_box"}
    if f.radius_km:
        params["distance"] = int(f.radius_km * 1000)
    return "https://api.wallapop.com/api/v3/search?" + urlencode(params)


def _olx(f):
    b, m = _bm(f)
    path = "/".join(p for p in (b, m) if p)
    base = f"https://www.olx.pt/carros-motos-e-barcos/carros/{path}/" if path else "https://www.olx.pt/carros-motos-e-barcos/carros/"
    return base + "?" + urlencode({"search[filter_float_price:from]": f.price_min, "search[filter_float_price:to]": f.price_max, "search[order]": "filter_float_price:asc"})


PORTALS: dict[str, PortalConfig] = {
    p.key: p
    for p in [
        PortalConfig("cochesnet", "Coches.net", "https://www.coches.net", "Portal", "#E2001A", _cochesnet),
        PortalConfig("autoscout24", "AutoScout24.es", "https://www.autoscout24.es", "Portal", "#F5A300", _autoscout, pro_share=0.85),
        PortalConfig("cochescom", "Coches.com", "https://www.coches.com", "Portal", "#0057B8", _cochescom, pro_share=0.9,
                     conditions=(("Segunda mano", 0.35), ("Seminuevo", 0.25), ("Km 0", 0.25), ("Nuevo", 0.15))),
        PortalConfig("autocasion", "Autocasión", "https://www.autocasion.com", "Portal", "#00843D", _autocasion, pro_share=0.8),
        PortalConfig("motores", "Motor.es", "https://www.motor.es", "Portal", "#1F2937", _motores, pro_share=0.85),
        PortalConfig("flexicar", "Flexicar", "https://www.flexicar.es", "Portal", "#FF6B00", _flexicar, pro_only=True, warranty_rate=1.0,
                     conditions=(("Segunda mano", 0.45), ("Seminuevo", 0.4), ("Km 0", 0.15))),
        PortalConfig("clicars", "Clicars", "https://www.clicars.com", "Portal", "#00A3E0", _clicars, pro_only=True, warranty_rate=1.0,
                     conditions=(("Seminuevo", 0.6), ("Segunda mano", 0.3), ("Km 0", 0.1))),
        PortalConfig("ocasionplus", "OcasionPlus", "https://www.ocasionplus.com", "Portal", "#6D28D9", _ocasionplus, pro_only=True, warranty_rate=1.0,
                     conditions=(("Segunda mano", 0.5), ("Seminuevo", 0.4), ("Km 0", 0.1))),
        PortalConfig("spoticar", "Spoticar", "https://www.spoticar.es", "Portal", "#243782", _spoticar, pro_only=True, warranty_rate=1.0,
                     conditions=(("Seminuevo", 0.5), ("Km 0", 0.3), ("Segunda mano", 0.2))),
        PortalConfig("dasweltauto", "Das WeltAuto", "https://www.dasweltauto.es", "Portal", "#001E50", _dasweltauto, pro_only=True, warranty_rate=1.0,
                     conditions=(("Seminuevo", 0.55), ("Km 0", 0.25), ("Segunda mano", 0.2))),
        PortalConfig("milanuncios", "Milanuncios", "https://www.milanuncios.com", "Clasificados", "#2E7D32", _milanuncios, pro_share=0.35, warranty_rate=0.6,
                     conditions=(("Segunda mano", 0.85), ("Seminuevo", 0.12), ("Km 0", 0.03))),
        PortalConfig("wallapop", "Wallapop", "https://es.wallapop.com", "Clasificados", "#13C1AC", _wallapop, pro_share=0.15, warranty_rate=0.5,
                     conditions=(("Segunda mano", 0.92), ("Seminuevo", 0.08)), api=True),
        PortalConfig("olx", "OLX (Importación PT)", "https://www.olx.pt", "Clasificados", "#3A77FF", _olx, pro_share=0.45, warranty_rate=0.5,
                     conditions=(("Segunda mano", 0.8), ("Seminuevo", 0.2)), country="PT"),
    ]
}

# Marcas cubiertas por los portales de concesionario oficial (red de la marca).
BRAND_NETWORKS = {
    "spoticar": {"Peugeot", "Citroën", "Opel", "Fiat", "Jeep", "Alfa Romeo"},
    "dasweltauto": {"Volkswagen", "SEAT", "CUPRA", "Škoda", "Audi"},
}

# Deep-link de búsqueda humana (para anuncios simulados y fallback de Wallapop API).
def portal_search_url(portal: PortalConfig, f: SearchFilters) -> str:
    if portal.key == "wallapop":
        return "https://es.wallapop.com/app/search?" + urlencode({"category_ids": "100", "keywords": f.query_text or "coche",
                                                                   "min_sale_price": f.price_min, "max_sale_price": f.price_max})
    return portal.url_builder(f)


# ---------------------------------------------------------------------------
# USER-AGENTS, SECRETOS Y PROXIES
# ---------------------------------------------------------------------------
_FALLBACK_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
]


class UserAgentRotator:
    """Rotación de User-Agents con fake-useragent y lista de respaldo offline."""

    def __init__(self) -> None:
        self._ua = None
        try:
            from fake_useragent import UserAgent

            self._ua = UserAgent(browsers=["Chrome", "Firefox", "Edge", "Safari"], platforms=["desktop"])
        except Exception:
            self._ua = None
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._ua is not None:
                try:
                    return self._ua.random
                except Exception:
                    pass
            return random.choice(_FALLBACK_UAS)


UA_ROTATOR = UserAgentRotator()


def get_secret(name: str) -> str | None:
    """Lee primero de variables de entorno y luego de st.secrets (Streamlit Cloud)."""
    value = os.getenv(name)
    if value:
        return value
    if st is not None:
        try:
            return st.secrets.get(name)  # type: ignore[union-attr]
        except Exception:
            return None
    return None


def proxy_provider() -> str:
    if get_secret("ZENROWS_API_KEY"):
        return "ZenRows"
    if get_secret("SCRAPERAPI_KEY"):
        return "ScraperAPI"
    if get_secret("PROXY_URL"):
        return "Proxy HTTP"
    return "Directo"


def wrap_url(url: str, api: bool = False) -> str:
    """Envuelve la URL destino con la API del proveedor anti-bloqueo si está configurado."""
    zen = get_secret("ZENROWS_API_KEY")
    if zen:
        params = {"apikey": zen, "url": url, "premium_proxy": "true", "proxy_country": "es"}
        if not api:
            params["js_render"] = "true"
        return "https://api.zenrows.com/v1/?" + urlencode(params)
    sapi = get_secret("SCRAPERAPI_KEY")
    if sapi:
        params = {"api_key": sapi, "url": url, "country_code": "es"}
        if not api:
            params["render"] = "true"
        return "https://api.scraperapi.com/?" + urlencode(params)
    return url


def build_headers(api: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": UA_ROTATOR.get(),
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.6",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "DNT": "1",
    }
    if api:
        headers.update({"Accept": "application/json, text/plain, */*", "X-DeviceOS": "0", "Origin": "https://es.wallapop.com", "Referer": "https://es.wallapop.com/"})
    else:
        headers.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8", "Upgrade-Insecure-Requests": "1"})
    return headers


BLOCK_MARKERS = ("captcha", "datadome", "cf-chl", "access denied", "are you a robot", "pardon our interruption",
                 "request unsuccessful", "incapsula", "perimeterx", "px-captcha", "challenge-platform")


def is_blocked(status: int, text: str) -> bool:
    if status in (401, 403, 405, 407, 429, 503):
        return True
    head = text[:6000].lower()
    return any(m in head for m in BLOCK_MARKERS) and len(text) < 120_000


# ---------------------------------------------------------------------------
# EXTRACCIÓN GENÉRICA DE ANUNCIOS
# ---------------------------------------------------------------------------
_NUM_RE = re.compile(r"\d[\d.,\s]*")
_YEAR_RE = re.compile(r"(19[89]\d|20[0-4]\d)")


def parse_number(value: Any) -> float | None:
    """'14.990 €' -> 14990 ; '120.000 km' -> 120000 ; {'amount': 9500} -> 9500."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for k in ("amount", "value", "price", "raw", "valor", "number"):
            if k in value:
                return parse_number(value[k])
        return None
    if isinstance(value, list):
        return parse_number(value[0]) if value else None
    m = _NUM_RE.search(str(value))
    if not m:
        return None
    raw = m.group(0).replace(" ", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", raw):
        raw = raw.replace(".", "").replace(",", "")
    elif raw.count(",") == 1 and raw.count(".") == 0:
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_year(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and 1980 <= int(value) <= CURRENT_YEAR + 1:
        return int(value)
    if isinstance(value, dict):
        value = value.get("year") or value.get("value") or json.dumps(value)
    m = _YEAR_RE.search(str(value))
    return int(m.group(1)) if m else None


def _ci_get(d: dict, key: str) -> Any:
    """Acceso insensible a mayúsculas; soporta rutas 'a.b'."""
    cur: Any = d
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        lower = {str(k).lower(): v for k, v in cur.items()}
        cur = lower.get(part.lower())
        if cur is None:
            return None
    return cur


def pick(d: dict, *keys: str) -> Any:
    """Primer valor no vacío entre varias claves candidatas (también 1 nivel anidado)."""
    for key in keys:
        val = _ci_get(d, key)
        if val not in (None, "", [], {}):
            return val
    for sub in ("attributes", "type_attributes", "vehicle", "car", "specs", "characteristics", "details", "content"):
        nested = _ci_get(d, sub)
        if isinstance(nested, dict):
            for key in keys:
                val = _ci_get(nested, key)
                if val not in (None, "", [], {}):
                    return val
    return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return _as_text(value.get("name") or value.get("value") or value.get("label") or value.get("text") or value.get("title"))
    if isinstance(value, list):
        return _as_text(value[0]) if value else None
    text = str(value).strip()
    return text or None


def _as_image(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.startswith(("http", "//")) else None
    if isinstance(value, list):
        for v in value:
            img = _as_image(v)
            if img:
                return img
        return None
    if isinstance(value, dict):
        for k in ("url", "src", "large", "medium", "big", "original", "small", "contentUrl", "urls", "uri"):
            if k in value:
                img = _as_image(value[k])
                if img:
                    return img
    return None


_LISTING_PRICE_KEYS = ("price", "precio", "offers", "priceinfo", "sale_price", "pricevalue", "finalprice", "cashprice")
_LISTING_ID_KEYS = ("make", "brand", "title", "name", "model", "headline", "marca")


def looks_like_listing(d: dict) -> bool:
    keys = {str(k).lower() for k in d.keys()}
    has_price = any(k in keys for k in _LISTING_PRICE_KEYS)
    has_ident = any(k in keys for k in _LISTING_ID_KEYS)
    return has_price and has_ident and len(keys) < 160


def walk_json(obj: Any, out: list[dict], depth: int = 0) -> None:
    """Recorre un JSON arbitrario recogiendo los diccionarios con forma de anuncio."""
    if depth > 14 or len(out) > 600:
        return
    if isinstance(obj, dict):
        if looks_like_listing(obj):
            out.append(obj)
            return
        for v in obj.values():
            walk_json(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            walk_json(v, out, depth + 1)


_STATE_RE = re.compile(r"(?:window\.)?__(?:INITIAL_STATE|PRELOADED_STATE|APOLLO_STATE|NUXT_DATA|INITIAL_PROPS)__\s*=\s*", re.S)


def _balanced_json(text: str, start: int) -> str | None:
    """Extrae un bloque {...} o [...] equilibrado a partir de `start`."""
    while start < len(text) and text[start] not in "{[":
        start += 1
    if start >= len(text):
        return None
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_raw_listings(text: str) -> list[dict]:
    """Devuelve candidatos crudos desde JSON puro, JSON-LD, __NEXT_DATA__ o estados embebidos."""
    found: list[dict] = []
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            walk_json(json.loads(stripped), found)
            return found
        except json.JSONDecodeError:
            pass

    soup = BeautifulSoup(text, "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            walk_json(json.loads(script.string or script.get_text() or "{}"), found)
        except (json.JSONDecodeError, TypeError):
            continue
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data:
        try:
            walk_json(json.loads(next_data.string or "{}"), found)
        except (json.JSONDecodeError, TypeError):
            pass
    for script in soup.find_all("script"):
        content = script.string or ""
        if not content or len(content) < 200:
            continue
        for m in _STATE_RE.finditer(content):
            block = _balanced_json(content, m.end())
            if block:
                try:
                    walk_json(json.loads(block), found)
                except json.JSONDecodeError:
                    continue
    if not found:
        found.extend(_extract_html_cards(soup))
    return found


def _extract_html_cards(soup: BeautifulSoup) -> list[dict]:
    """Último recurso: tarjetas HTML con precio en € y enlace."""
    cards = []
    for node in soup.select("article, li[class*=card], div[class*=card], div[class*=Card], div[class*=item]")[:300]:
        txt = node.get_text(" ", strip=True)
        if "€" not in txt or len(txt) > 900:
            continue
        a = node.find("a", href=True)
        title_el = node.find(["h2", "h3", "h4"])
        img = node.find("img")
        price_m = re.search(r"(\d{1,3}(?:[.\s]\d{3})+|\d{3,6})\s*€", txt)
        km_m = re.search(r"(\d{1,3}(?:[.\s]\d{3})+|\d{1,6})\s*km", txt, re.I)
        if not (a and price_m and title_el):
            continue
        cards.append({
            "title": title_el.get_text(" ", strip=True),
            "price": price_m.group(1),
            "km": km_m.group(1) if km_m else None,
            "year": parse_year(txt),
            "url": a["href"],
            "image": (img.get("data-src") or img.get("src")) if img else None,
            "fuel": geo.normalize_fuel(txt),
            "location": None,
        })
    return cards


def normalize_record(raw: dict, portal: PortalConfig) -> dict | None:
    """Convierte un anuncio crudo de cualquier portal al esquema común."""
    title = _as_text(pick(raw, "title", "name", "headline", "titulo", "description"))
    brand = _as_text(pick(raw, "make", "brand", "manufacturer", "marca", "makeName"))
    model = _as_text(pick(raw, "model", "modelo", "modelName"))
    brand = geo.match_brand(brand) or geo.match_brand(title) or brand
    if not brand:
        return None
    if not model and title:
        spec_models = geo.get_models(brand)
        nt = geo.normalize_text(title)
        hits = [m for m in spec_models if geo.normalize_text(m) in nt]
        model = max(hits, key=len) if hits else None

    price = parse_number(pick(raw, "price", "precio", "offers.price", "offers", "priceInfo", "sale_price", "finalPrice", "cashPrice"))
    if price is None or price < 300 or price > 2_000_000:
        return None

    year = parse_year(pick(raw, "year", "anio", "ano", "vehicleModelDate", "productionDate", "dateVehicleFirstRegistered",
                           "firstRegistration", "registrationDate", "firstRegistrationDate", "fechaMatriculacion"))
    if not year and title:
        year = parse_year(title)
    km = parse_number(pick(raw, "km", "kms", "kilometers", "kilometros", "mileage", "mileageFromOdometer", "kilometraje", "mileageInKm"))
    fuel = geo.normalize_fuel(_as_text(pick(raw, "fuel", "fuelType", "combustible", "fuelTypeName", "engine", "fuel_type")))
    fuel = fuel or geo.normalize_fuel(title)
    trans_raw = geo.normalize_text(_as_text(pick(raw, "transmission", "gearbox", "vehicleTransmission", "cambio", "transmissionType")) or "")
    transmission = "Automático" if any(k in trans_raw for k in ("auto", "dsg", "cvt", "secuencial")) else ("Manual" if trans_raw else None)
    seats = parse_number(pick(raw, "seats", "seatingCapacity", "numberOfSeats", "plazas", "seat_count"))
    body = geo.normalize_body(_as_text(pick(raw, "bodyType", "body", "carroceria", "bodyTypeName", "body_type")))
    power = parse_number(pick(raw, "power", "potencia", "enginePower", "horsePower", "hp", "cv"))

    loc_raw = pick(raw, "location", "city", "province", "address", "provincia", "ciudad", "seller.address", "offers.seller.address")
    if isinstance(loc_raw, dict):
        loc_text = ", ".join(str(loc_raw.get(k)) for k in ("city", "addressLocality", "province", "addressRegion", "region", "name") if loc_raw.get(k))
    else:
        loc_text = _as_text(loc_raw)
    location = geo.resolve_location(loc_text)
    if portal.country == "PT":
        city = next((c for c in geo.IMPORT_LOCATIONS if loc_text and geo.normalize_text(c) in geo.normalize_text(loc_text)), "Lisboa")
        lat, lon = geo.IMPORT_LOCATIONS[city]
        location = {"ciudad": city, "provincia": geo.IMPORT_PROVINCE, "comunidad": geo.IMPORT_REGION, "lat": lat, "lon": lon}

    url = _as_text(pick(raw, "url", "link", "href", "detailUrl", "detail_url", "permalink", "offers.url"))
    slug = _as_text(pick(raw, "web_slug"))
    if slug and not url:
        url = f"https://es.wallapop.com/item/{slug}"
    url = urljoin(portal.domain + "/", url) if url else portal.domain

    seller_raw = geo.normalize_text(json.dumps(pick(raw, "sellerType", "seller", "isProfessional", "dealer", "vendedor", "offers.seller") or ""))
    if portal.pro_only or any(k in seller_raw for k in ("dealer", "profesional", "professional", "concesionario", "autodealer", "true")):
        seller = "Concesionario / Profesional"
    elif seller_raw.strip('"'):
        seller = "Particular"
    else:
        seller = "Particular" if portal.kind == "Clasificados" else "Concesionario / Profesional"

    spec = geo.get_model_spec(brand, model)
    if spec:
        body = body or spec["carroceria"]
        if seats is None and len(spec["plazas"]) == 1:
            seats = spec["plazas"][0]
        if not fuel and len(spec["combustibles"]) == 1:
            fuel = spec["combustibles"][0]
        model = model or spec["modelo"]

    km_val = int(km) if km is not None else None
    state = "Segunda mano"
    if km_val is not None and year:
        if km_val <= 100 and year >= CURRENT_YEAR - 1:
            state = "Km 0"
        elif year >= CURRENT_YEAR - 3 and km_val <= 60_000:
            state = "Seminuevo"
    warranty_raw = pick(raw, "warranty", "garantia", "hasWarranty", "warrantyMonths")
    warranty = bool(warranty_raw) or portal.pro_only

    rec = {
        "portal": portal.name,
        "titulo": title or f"{brand} {model or ''}".strip(),
        "marca": brand,
        "modelo": model or "",
        "version": "",
        "precio": float(price),
        "anio": year,
        "km": km_val,
        "combustible": fuel,
        "cambio": transmission,
        "carroceria": body,
        "plazas": int(seats) if seats else None,
        "etiqueta_dgt": geo.compute_dgt_label(fuel, year),
        "estado": state,
        "vendedor": seller,
        "garantia": "Sí" if warranty else "No",
        "garantia_meses": int(parse_number(warranty_raw) or 12) if warranty else 0,
        "potencia_cv": int(power) if power else None,
        "color": _as_text(pick(raw, "color", "colour", "exteriorColor")),
        "ciudad": location["ciudad"] if location else (loc_text or "—"),
        "provincia": location["provincia"] if location else "—",
        "comunidad": location["comunidad"] if location else "—",
        "lat": location["lat"] if location else None,
        "lon": location["lon"] if location else None,
        "url": url,
        "imagen": _as_image(pick(raw, "image", "images", "photos", "pictures", "thumbnail", "img", "mainImage", "resources")),
        "fecha_publicacion": date.today().isoformat(),
        "origen_dato": "Real",
    }
    rec["id"] = hashlib.md5(f"{portal.key}|{url}|{price}|{rec['titulo']}".encode()).hexdigest()[:12]
    if not rec["imagen"]:
        rec["imagen"] = placeholder_image(brand, rec["modelo"], portal.color)
    return rec


# ---------------------------------------------------------------------------
# IMÁGENES DE RESPALDO (SVG embebido, sin dependencias externas)
# ---------------------------------------------------------------------------
_CAR_PATHS = {
    "default": "M40 128 L62 96 Q72 82 92 80 L208 80 Q228 80 242 94 L272 122 L304 128 Q320 132 320 148 L320 162 L24 162 L24 146 Q24 130 40 128 Z",
    "SUV / 4x4": "M36 126 L54 84 Q60 70 80 70 L230 70 Q246 70 256 84 L280 120 L306 126 Q320 130 320 146 L320 164 L24 164 L24 142 Q24 128 36 126 Z",
    "Monovolumen": "M36 128 L60 76 Q68 64 90 64 L250 64 Q270 66 280 86 L296 122 L308 128 Q320 132 320 148 L320 164 L24 164 L24 144 Q24 130 36 128 Z",
    "Furgoneta": "M28 132 L44 64 Q48 52 66 52 L280 52 Q300 52 304 70 L312 128 Q320 132 320 148 L320 164 L24 164 L24 146 Q24 134 28 132 Z",
    "Coupé": "M30 134 L84 100 Q102 88 128 86 L196 86 Q222 88 246 104 L286 128 L308 132 Q320 136 320 150 L320 160 L24 160 L24 148 Q24 138 30 134 Z",
}


def placeholder_image(brand: str, model: str, color: str = "#AA151B", body: str | None = None) -> str:
    """Genera un SVG elegante (data URI) con silueta de vehículo, marca y modelo."""
    path = _CAR_PATHS.get(body or "", _CAR_PATHS["default"])
    label = f"{brand} {model}".strip().replace("&", "&amp;").replace("<", "")[:34]
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 344 220' width='344' height='220'>
<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='#F1F5F9'/><stop offset='1' stop-color='#E2E8F0'/></linearGradient></defs>
<rect width='344' height='220' fill='url(#g)'/>
<rect y='0' width='344' height='5' fill='{color}'/>
<g transform='translate(0,6)'><path d='{path}' fill='#334155' opacity='0.88'/>
<circle cx='92' cy='164' r='22' fill='#1E293B'/><circle cx='92' cy='164' r='10' fill='#CBD5E1'/>
<circle cx='256' cy='164' r='22' fill='#1E293B'/><circle cx='256' cy='164' r='10' fill='#CBD5E1'/></g>
<text x='172' y='46' text-anchor='middle' font-family='Inter,Arial,sans-serif' font-size='17' font-weight='700' fill='#1E293B'>{label}</text>
</svg>"""
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# GENERADOR DE FALLBACK (datos de referencia realistas y deterministas)
# ---------------------------------------------------------------------------
_BIG_CITIES = {"Madrid": 8, "Barcelona": 6, "Valencia": 4, "Sevilla": 3, "Málaga": 3, "Zaragoza": 2.5, "Alicante": 2.5,
               "Murcia": 2.5, "Bilbao": 2, "Palma": 2, "Las Palmas de Gran Canaria": 2, "Valladolid": 1.8, "Elche": 1.8, "Vigo": 1.6}


def _allowed_locations(f: SearchFilters, portal: PortalConfig) -> list[tuple[dict, float]]:
    """Ubicaciones (con peso) donde pueden aparecer anuncios según los filtros geográficos."""
    if portal.country == "PT":
        return [({"ciudad": c, "provincia": geo.IMPORT_PROVINCE, "comunidad": geo.IMPORT_REGION, "lat": la, "lon": lo}, 1.0)
                for c, (la, lo) in geo.IMPORT_LOCATIONS.items()]
    idx = geo.city_index()
    if f.city != geo.ALL_OPTION:
        if f.radius_km:
            return [(c, 3.0 if c["ciudad"] == f.city else 1.0) for c in geo.cities_within(f.city, f.radius_km)]
        info = idx.get(geo.normalize_text(f.city))
        return [(info, 1.0)] if info else []
    locs = []
    for info in idx.values():
        if info["comunidad"] == geo.IMPORT_REGION:
            continue
        if f.province != geo.ALL_OPTION and info["provincia"] != f.province:
            continue
        if f.community != geo.ALL_OPTION and info["comunidad"] != f.community:
            continue
        locs.append((info, _BIG_CITIES.get(info["ciudad"], 1.0)))
    return locs


def _candidate_models(f: SearchFilters, portal: PortalConfig) -> list[tuple[str, str, dict, float]]:
    """Combinaciones marca/modelo compatibles con los filtros (con peso de popularidad)."""
    kw_tokens = [geo.normalize_text(t) for t in f.keywords.split() if len(t) > 1]
    network = BRAND_NETWORKS.get(portal.key)
    out = []
    for brand, models in geo.CAR_CATALOG.items():
        if f.has_brand and brand != f.brand:
            continue
        if network and brand not in network:
            continue
        for model in models:
            if f.has_model and model != f.model:
                continue
            spec = geo.get_model_spec(brand, model)
            if f.seats != "Todas" and int(f.seats) not in spec["plazas"]:
                continue
            if f.bodies and spec["carroceria"] not in f.bodies:
                continue
            if f.fuels and not set(spec["combustibles"]) & set(f.fuels):
                continue
            if kw_tokens:
                hay = geo.normalize_text(f"{brand} {model} {spec['carroceria']} {' '.join(spec['combustibles'])} {' '.join(map(str, spec['plazas']))} plazas")
                if not all(t in hay for t in kw_tokens):
                    continue
            weight = 1.5 if brand in geo.PREMIUM_BRANDS else 3.0
            if spec["precio_nuevo"] > 90_000:
                weight = 0.5
            out.append((brand, model, spec, weight))
    return out


# Denominación de motores por grupo de marca (gasolina, diésel).
BRAND_ENGINES = {
    **{b: ("TSI", "TDI") for b in ("Volkswagen", "SEAT", "CUPRA", "Škoda")},
    "Audi": ("TFSI", "TDI"), "Renault": ("TCe", "dCi"), "Dacia": ("TCe", "dCi"), "Nissan": ("DIG-T", "dCi"),
    **{b: ("PureTech", "BlueHDi") for b in ("Peugeot", "Citroën", "Opel")},
    "Fiat": ("T-Jet", "MultiJet"), "Jeep": ("T-GDI", "MultiJet"), "Alfa Romeo": ("Turbo", "JTDm"),
    "Kia": ("T-GDi", "CRDi"), "Hyundai": ("T-GDi", "CRDi"), "Ford": ("EcoBoost", "EcoBlue"),
    "Toyota": ("VVT-i", "D-4D"), "Lexus": ("VVT-i", "D-4D"), "Honda": ("VTEC Turbo", "i-DTEC"),
    "BMW": ("i", "d"), "Mini": ("Cooper", "Cooper D"), "Mercedes-Benz": ("Turbo", "d"), "Volvo": ("T4", "D4"),
    "Mazda": ("Skyactiv-G", "Skyactiv-D"), "Land Rover": ("Si4", "D"), "Porsche": ("Turbo", "Diesel"),
    "Mitsubishi": ("MPI", "DI-D"), "Suzuki": ("Boosterjet", "DDiS"), "MG": ("T-GDI", "Diesel"),
}

# Año de lanzamiento en España de modelos recientes (evita anuncios anacrónicos).
MODEL_SINCE = {
    "Born": 2021, "Formentor": 2020, "Tavascan": 2024, "Leon": 2020, "Jogger": 2022, "Spring": 2021, "Avenger": 2023,
    "EV6": 2021, "EV9": 2023, "IONIQ 5": 2021, "Staria": 2021, "ID.3": 2020, "ID.4": 2021, "Mustang Mach-E": 2021,
    "Puma": 2020, "Austral": 2022, "Arkana": 2021, "Mokka": 2021, "Grandland": 2018, "Q4 e-tron": 2021, "i4": 2021,
    "iX": 2021, "EQA": 2021, "EX30": 2024, "Model 3": 2019, "Model Y": 2022, "MG4": 2022, "MG3": 2024, "HS": 2019,
    "ZS": 2018, "Dolphin": 2023, "Atto 3": 2022, "Seal": 2023, "Seal U DM-i": 2024, "Tang": 2022, "bZ4X": 2022,
    "CX-60": 2022, "CX-80": 2024, "CX-30": 2019, "Tonale": 2022, "Taycan": 2020, "Townstar": 2022, "#1": 2023,
    "Tarraco": 2019, "Arona": 2017, "Ateca": 2016, "Kamiq": 2019, "Scala": 2019, "Enyaq": 2021, "Karoq": 2017,
    "Kodiaq": 2017, "T-Roc": 2017, "Niro": 2016, "Kona": 2017, "C5 Aircross": 2019, "Rifter": 2018, "Combo Life": 2018,
    "Corolla": 2019, "Corolla Touring Sports": 2019, "C-HR": 2017, "Stelvio": 2017, "Giulia": 2016, "GLB": 2019,
    "Q2": 2016, "X-Trail": 2014, "Mazda2": 2015, "Mustang": 2015, "Defender": 2020, "UX": 2019, "SpaceTourer": 2016,
    "Traveller": 2016, "Proace Verso": 2016, "Ranger": 2012,
}
# Último año de comercialización de modelos descatalogados.
MODEL_UNTIL = {"Alhambra": 2022, "Sharan": 2022, "Lodgy": 2022, "C4 Picasso": 2022, "Prius+": 2021, "Zoe": 2024,
               "Fiesta": 2023, "Galaxy": 2023, "S-Max": 2023, "Insignia": 2022, "TT": 2023, "Mégane": 2024}


BRAND_TRIMS = {
    "Mercedes-Benz": ["AMG Line", "Avantgarde", "Progressive", "Business"], "BMW": ["M Sport", "Sport", "Luxury", "xLine"],
    "Audi": ["S line", "Advanced", "Black line", "Design"], "SEAT": ["Style", "Xcellence", "FR", "Reference"],
    "CUPRA": ["VZ", "Impulse", "Tribe"], "Peugeot": ["Allure", "GT", "Active", "GT Line"], "Citroën": ["Feel", "Shine", "Max", "Live"],
    "Renault": ["Intens", "Zen", "Techno", "Esprit Alpine"], "Dacia": ["Essential", "Expression", "Extreme", "Journey"],
    "Ford": ["Titanium", "ST-Line", "Trend", "Vignale"], "Nissan": ["Acenta", "N-Connecta", "Tekna"], "Toyota": ["Active", "Advance", "Style", "GR Sport"],
    "Kia": ["Concept", "Drive", "Tech", "GT-Line"], "Hyundai": ["Klass", "Maxx", "Tecno", "Style"], "Volkswagen": ["Life", "Style", "R-Line", "Advance"],
    "Škoda": ["Ambition", "Style", "Selection", "Sportline"], "Opel": ["Edition", "Elegance", "GS", "Business"], "Volvo": ["Core", "Plus", "Ultimate"],
    "Tesla": ["Gran Autonomía", "Performance", "Propulsión"], "Mazda": ["Prime-Line", "Exclusive-Line", "Homura", "Takumi"],
}


def _engine_label(brand: str, fuel: str, year: int, rng: random.Random, premium: bool, new_price: int = 30000) -> tuple[str, int]:
    gas, diesel = BRAND_ENGINES.get(brand, ("Turbo", "Diesel"))
    if new_price < 21000 and fuel in (geo.FUELS["G"], geo.FUELS["D"]):  # utilitarios: motores pequeños
        cv = rng.choice([70, 75, 85, 90, 95, 100, 110] if fuel == geo.FUELS["G"] else [75, 90, 95, 100, 110])
        disp = "1.0" if cv <= 100 else "1.2"
        return f"{'1.5' if fuel == geo.FUELS['D'] else disp} {gas if fuel == geo.FUELS['G'] else diesel} {cv}CV", cv
    if fuel == geo.FUELS["E"]:
        kwh = rng.choice([40, 50, 58, 64, 77, 82] if year >= 2020 else [24, 30, 40])
        cv = rng.choice([136, 150, 204, 231, 286, 340] if year >= 2020 else [109, 136, 150])
        return f"{kwh} kWh {cv}CV", cv
    if fuel == geo.FUELS["P"]:
        cv = rng.choice([204, 225, 245, 272, 299])
        return f"{rng.choice(['1.4', '1.5', '1.6', '2.0'])} {gas} PHEV {cv}CV", cv
    if fuel == geo.FUELS["H"]:
        cv = rng.choice([116, 136, 140, 184, 197, 218])
        return f"{rng.choice(['1.5', '1.6', '1.8', '2.0', '2.5'])} Hybrid {cv}CV", cv
    if fuel == geo.FUELS["D"]:
        cv = rng.choice([110, 115, 130, 150, 190] + ([204, 249, 286] if premium else []))
        disp = "1.5" if cv <= 120 else ("2.0" if cv <= 204 else "3.0")
        return f"{disp} {diesel} {cv}CV", cv
    if fuel == geo.FUELS["L"]:
        cv = rng.choice([90, 100, 110])
        return f"1.0 {gas} GLP {cv}CV", cv
    cv = rng.choice([95, 110, 130, 150] + ([184, 245, 300] if premium else []))
    disp = "1.0" if cv <= 110 else ("1.5" if cv <= 150 else "2.0")
    return f"{disp} {gas} {cv}CV", cv


def _fuel_available(brand: str, fuel: str, year: int) -> bool:
    """Coherencia histórica: PHEV/EV/HEV solo a partir de cierto año (salvo pioneros)."""
    if fuel == geo.FUELS["P"]:
        return year >= (2014 if brand in ("Mitsubishi", "BMW", "Volvo", "Porsche") else 2020)
    if fuel == geo.FUELS["E"]:
        return year >= (2012 if brand in ("Tesla", "Nissan", "Renault", "Smart", "BMW") else 2019)
    if fuel == geo.FUELS["H"]:
        return year >= (2005 if brand in ("Toyota", "Lexus", "Honda") else 2019)
    return True


def _sample_year_km(state: str, rng: random.Random) -> tuple[int, int]:
    if state == "Nuevo":
        return CURRENT_YEAR, rng.randint(0, 30)
    if state == "Km 0":
        return rng.choice([CURRENT_YEAR - 1, CURRENT_YEAR]), rng.randint(5, 100)
    if state == "Seminuevo":
        year = rng.randint(CURRENT_YEAR - 3, CURRENT_YEAR - 1)
        return year, int(rng.uniform(4_000, 15_000) * max(1, CURRENT_YEAR - year))
    age = min(int(rng.triangular(3, 21, 7)), 21)
    year = CURRENT_YEAR - age
    return year, int(max(15_000, rng.gauss(14_500, 4_000) * age))


def _price_for(spec: dict, state: str, year: int, km: int, fuel: str, seller: str, rng: random.Random) -> float:
    base = spec["precio_nuevo"] * rng.uniform(0.95, 1.18)
    if fuel in (geo.FUELS["P"], geo.FUELS["E"]):
        base *= 1.12
    age = max(0, CURRENT_YEAR - year)
    if state == "Nuevo":
        price = base * rng.uniform(0.93, 1.02)
    elif state == "Km 0":
        price = base * rng.uniform(0.80, 0.89)
    else:
        dep = max(0.14, (0.89 if fuel != geo.FUELS["E"] else 0.85) ** age)
        km_factor = max(0.72, 1 - (km / 1_000_000) * 0.6)
        price = base * 0.88 * dep * km_factor * rng.gauss(1.0, 0.07)
    if seller == "Particular":
        price *= rng.uniform(0.92, 0.98)
    price = max(price, 1_200)
    if seller == "Particular":
        return float(round(price / 100) * 100)
    return float(max(990, round(price / 100) * 100 - 10))  # 14.990 €


def generate_fallback(portal: PortalConfig, f: SearchFilters, n_target: int) -> list[dict]:
    """
    Genera anuncios de referencia coherentes con los filtros.
    La semilla depende de portal + filtros + día -> resultados estables durante la jornada.
    """
    seed_src = f"{portal.key}|{json.dumps(asdict(f), sort_keys=True, default=str)}|{date.today().isoformat()}"
    rng = random.Random(int(hashlib.sha256(seed_src.encode()).hexdigest()[:16], 16))

    models = _candidate_models(f, portal)
    locations = _allowed_locations(f, portal)
    if not models or not locations:
        return []
    states = [(s, w) for s, w in portal.conditions if not f.conditions or s in f.conditions]
    if not states:
        return []

    # Volumen proporcional al ámbito geográfico (un municipio tiene menos oferta que toda España).
    if portal.country != "PT":
        if f.city != geo.ALL_OPTION:
            n_target = int(n_target * (0.3 if not f.radius_km else min(1.0, 0.3 + f.radius_km / 150)))
        elif f.province != geo.ALL_OPTION:
            n_target = int(n_target * 0.55)
        elif f.community != geo.ALL_OPTION:
            n_target = int(n_target * 0.8)
    n = rng.randint(max(3, int(n_target * 0.55)), max(4, n_target))
    m_weights = [m[3] for m in models]
    l_weights = [w for _, w in locations]
    s_names, s_weights = [s for s, _ in states], [w for _, w in states]
    search_url = portal_search_url(portal, f)

    listings: list[dict] = []
    attempts = 0
    while len(listings) < n and attempts < n * 40:
        attempts += 1
        brand, model, spec, _ = rng.choices(models, weights=m_weights)[0]
        state = rng.choices(s_names, weights=s_weights)[0]
        year, km = _sample_year_km(state, rng)
        if not (f.year_min <= year <= f.year_max and f.km_min <= km <= f.km_max):
            continue
        if not (MODEL_SINCE.get(model, 1990) <= year <= MODEL_UNTIL.get(model, 2099)):
            continue
        fuels = [x for x in spec["combustibles"] if (not f.fuels or x in f.fuels) and _fuel_available(brand, x, year)]
        if not fuels:
            continue
        fuel = rng.choice(fuels)
        label = geo.compute_dgt_label(fuel, year)
        if f.dgt_label != "Todas" and label != f.dgt_label:
            continue
        premium = brand in geo.PREMIUM_BRANDS
        if fuel in (geo.FUELS["E"], geo.FUELS["H"], geo.FUELS["P"]):
            trans = "Automático"
        else:
            auto_p = 0.8 if premium else (0.25 + 0.03 * max(0, year - 2012))
            trans = "Automático" if rng.random() < min(auto_p, 0.85) else "Manual"
        if f.transmission != "Todos" and trans != f.transmission:
            continue
        seats_opts = [s for s in spec["plazas"] if f.seats == "Todas" or s == int(f.seats)]
        seats = rng.choice(seats_opts)
        if portal.pro_only:
            seller = "Concesionario / Profesional"
        else:
            seller = "Concesionario / Profesional" if rng.random() < portal.pro_share else "Particular"
        if state in ("Nuevo", "Km 0"):
            seller = "Concesionario / Profesional"
        if f.seller != "Todos" and seller != f.seller:
            continue
        if seller == "Particular":
            warranty_m = 0 if rng.random() > 0.05 else 3
        elif state in ("Nuevo", "Km 0"):
            warranty_m = rng.choice([24, 36, 60, 84]) if brand in ("Kia", "Toyota", "Hyundai", "MG") else 24
        else:
            warranty_m = rng.choice([12, 12, 18, 24]) if rng.random() < portal.warranty_rate else 0
        if f.warranty == "Con garantía incluida" and warranty_m == 0:
            continue
        price = _price_for(spec, state, year, km, fuel, seller, rng)
        if not (f.price_min <= price <= f.price_max):
            continue
        loc = rng.choices([l for l, _ in locations], weights=l_weights)[0]
        engine, cv = _engine_label(brand, fuel, year, rng, premium, spec["precio_nuevo"])
        trim = rng.choice(BRAND_TRIMS.get(brand, ["Comfort", "Business", "Sport", "Premium", "Style"]))
        title = f"{brand} {model} {engine} {trim}"
        if seats >= 7 and spec["carroceria"] in ("SUV / 4x4", "Monovolumen"):
            title += f" {seats} plazas"
        listing = {
            "portal": portal.name,
            "titulo": title,
            "marca": brand,
            "modelo": model,
            "version": f"{engine} {trim}",
            "precio": price,
            "anio": year,
            "km": km,
            "combustible": fuel,
            "cambio": trans,
            "carroceria": spec["carroceria"],
            "plazas": seats,
            "etiqueta_dgt": label,
            "estado": state,
            "vendedor": seller,
            "garantia": "Sí" if warranty_m else "No",
            "garantia_meses": warranty_m,
            "potencia_cv": cv,
            "color": rng.choice(geo.COLORS),
            "ciudad": loc["ciudad"],
            "provincia": loc["provincia"],
            "comunidad": loc["comunidad"],
            "lat": round(loc["lat"] + rng.uniform(-0.03, 0.03), 5),
            "lon": round(loc["lon"] + rng.uniform(-0.03, 0.03), 5),
            "url": search_url,
            "imagen": placeholder_image(brand, model, portal.color, spec["carroceria"]),
            "fecha_publicacion": (date.today() - timedelta(days=int(rng.triangular(0, 60, 3)))).isoformat(),
            "origen_dato": "Simulado",
        }
        listing["id"] = hashlib.md5(f"{seed_src}|{len(listings)}|{title}|{price}".encode()).hexdigest()[:12]
        listings.append(listing)
    return listings


# ---------------------------------------------------------------------------
# DESCARGA ASÍNCRONA
# ---------------------------------------------------------------------------
@dataclass
class PortalResult:
    portal: str
    key: str
    status: str  # "Real" | "Fallback" | "Demo" | "Bloqueado" | "Error" | "Sin resultados"
    count: int = 0
    elapsed: float = 0.0
    message: str = ""
    url: str = ""
    listings: list[dict] = field(default_factory=list)


async def _fetch(client: httpx.AsyncClient, url: str, api: bool, retries: int = 1) -> tuple[int, str]:
    """GET con rotación de User-Agent y reintento con backoff."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.get(wrap_url(url, api=api), headers=build_headers(api=api))
            if resp.status_code >= 500 and attempt < retries:
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
            return resp.status_code, resp.text
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            await asyncio.sleep(0.6 * (attempt + 1))
    raise last_exc or RuntimeError("Fallo de red")


async def _scrape_portal(client: httpx.AsyncClient, sem: asyncio.Semaphore, portal: PortalConfig, f: SearchFilters) -> PortalResult:
    start = time.perf_counter()
    url = portal.url_builder(f)
    result = PortalResult(portal=portal.name, key=portal.key, status="Error", url=portal_search_url(portal, f))

    def fallback(reason: str) -> PortalResult:
        if f.mode == "real":
            result.status, result.message = reason, "Sin fallback (modo solo datos reales)."
            return result
        result.listings = generate_fallback(portal, f, f.max_per_portal)
        result.status = "Demo" if f.mode == "demo" else "Fallback"
        result.message = "Datos de referencia generados." if f.mode == "demo" else f"{reason}: datos de referencia generados."
        return result

    if f.mode == "demo":
        res = fallback("Demo")
    else:
        try:
            async with sem:
                status, text = await _fetch(client, url, api=portal.api)
            if is_blocked(status, text):
                res = fallback("Bloqueado")
                res.message = f"HTTP {status} / anti-bot — " + res.message
            elif status >= 400:
                res = fallback("Error")
                res.message = f"HTTP {status} — " + res.message
            else:
                raws = extract_raw_listings(text)
                seen, records = set(), []
                for raw in raws:
                    rec = normalize_record(raw, portal)
                    if rec and rec["url"] not in seen:
                        seen.add(rec["url"] if rec["url"] != portal.domain else rec["id"])
                        records.append(rec)
                records = records[: f.max_per_portal]
                if records:
                    result.status, result.listings = "Real", records
                    result.message = f"{len(records)} anuncios extraídos ({proxy_provider()})."
                    res = result
                else:
                    res = fallback("Sin datos estructurados")
        except Exception as exc:  # red, DNS, timeout, parseo...
            res = fallback("Error")
            res.message = f"{type(exc).__name__} — " + res.message
    res.count = len(res.listings)
    res.elapsed = round(time.perf_counter() - start, 2)
    return res


async def scrape_all(f: SearchFilters) -> list[PortalResult]:
    """Lanza todos los portales seleccionados en paralelo."""
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    client_kwargs: dict[str, Any] = {"timeout": httpx.Timeout(REQUEST_TIMEOUT, connect=6.0), "follow_redirects": True, "http2": False}
    proxy = get_secret("PROXY_URL")
    if proxy and not (get_secret("ZENROWS_API_KEY") or get_secret("SCRAPERAPI_KEY")):
        client_kwargs["proxy"] = proxy
    if get_secret("ZENROWS_API_KEY") or get_secret("SCRAPERAPI_KEY"):
        client_kwargs["timeout"] = httpx.Timeout(max(REQUEST_TIMEOUT, 45.0), connect=10.0)
    portals = [PORTALS[k] for k in f.portals if k in PORTALS] or list(PORTALS.values())
    async with httpx.AsyncClient(**client_kwargs) as client:
        tasks = [_scrape_portal(client, sem, p, f) for p in portals]
        return list(await asyncio.gather(*tasks))


def run_async(coro):
    """Ejecuta una corrutina tanto si hay bucle de eventos activo como si no."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict[str, Any] = {}

    def runner():
        box["value"] = asyncio.run(coro)

    t = threading.Thread(target=runner)
    t.start()
    t.join()
    return box["value"]


# ---------------------------------------------------------------------------
# FILTRADO UNIFICADO Y MÉTRICAS DE MERCADO
# ---------------------------------------------------------------------------
def apply_filters(df: pd.DataFrame, f: SearchFilters) -> pd.DataFrame:
    """Aplica todos los filtros al DataFrame normalizado (datos reales y simulados)."""
    if df.empty:
        return df
    m = pd.Series(True, index=df.index)
    m &= df["precio"].between(f.price_min, f.price_max)
    m &= df["anio"].isna() | df["anio"].between(f.year_min, f.year_max)
    m &= df["km"].isna() | df["km"].between(f.km_min, f.km_max)
    if f.has_brand:
        m &= df["marca"] == f.brand
    if f.has_model:
        m &= df["modelo"].fillna("").map(geo.normalize_text) == geo.normalize_text(f.model)
    if f.keywords:
        text = (df["titulo"].fillna("") + " " + df["version"].fillna("") + " " + df["carroceria"].fillna("") + " "
                + df["combustible"].fillna("") + " " + df["plazas"].fillna(0).astype(int).astype(str) + " plazas").map(geo.normalize_text)
        for tok in [geo.normalize_text(t) for t in f.keywords.split() if len(t) > 1]:
            m &= text.str.contains(re.escape(tok), regex=True)
    if f.conditions:
        m &= df["estado"].isin(f.conditions)
    if f.seats != "Todas":
        m &= df["plazas"] == int(f.seats)
    if f.fuels:
        m &= df["combustible"].isin(f.fuels)
    if f.transmission != "Todos":
        m &= df["cambio"] == f.transmission
    if f.bodies:
        m &= df["carroceria"].isin(f.bodies)
    if f.dgt_label != "Todas":
        m &= df["etiqueta_dgt"] == f.dgt_label
    if f.seller != "Todos":
        m &= df["vendedor"] == f.seller
    if f.warranty == "Con garantía incluida":
        m &= df["garantia"] == "Sí"

    # --- Ubicación ---
    is_import = df["comunidad"] == geo.IMPORT_REGION
    if f.location_active:
        loc_mask = pd.Series(True, index=df.index)
        if f.city != geo.ALL_OPTION and f.radius_km:
            origin = geo.city_index().get(geo.normalize_text(f.city))
            if origin:
                dist = df.apply(lambda r: geo.haversine_km(origin["lat"], origin["lon"], r["lat"], r["lon"])
                                if pd.notna(r["lat"]) else np.inf, axis=1)
                loc_mask &= dist <= f.radius_km
        elif f.city != geo.ALL_OPTION:
            loc_mask &= df["ciudad"] == f.city
        else:
            if f.province != geo.ALL_OPTION:
                loc_mask &= df["provincia"] == f.province
            if f.community != geo.ALL_OPTION:
                loc_mask &= df["comunidad"] == f.community
        m &= loc_mask | (is_import & f.include_imports)
    elif not f.include_imports:
        m &= ~is_import
    return df[m].copy()


def add_distance(df: pd.DataFrame, f: SearchFilters) -> pd.DataFrame:
    """Columna distancia_km respecto al municipio seleccionado (si lo hay)."""
    origin = geo.city_index().get(geo.normalize_text(f.city)) if f.city != geo.ALL_OPTION else None
    if df.empty or not origin:
        df["distancia_km"] = np.nan
        return df
    df["distancia_km"] = df.apply(lambda r: round(geo.haversine_km(origin["lat"], origin["lon"], r["lat"], r["lon"]), 1)
                                  if pd.notna(r["lat"]) else np.nan, axis=1)
    return df


def enrich_market_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """
    Valor estimado de mercado mediante regresión log-lineal:
        log(precio) ~ antigüedad + log(1+km) [+ log(precio nuevo de catálogo) + electrificado]
    1) Modelos con >= 8 anuncios: ajuste propio (antigüedad + km).
    2) Resto: ajuste global que incorpora el precio de catálogo del modelo, de modo que
       se compara cada coche con vehículos de su mismo nivel de precio.
    Se marca como 'oportunidad' todo anuncio >= OPPORTUNITY_THRESHOLD % por debajo del valor estimado.
    """
    df = df.copy()
    df["precio_estimado"] = np.nan
    if df.empty:
        df["diferencia_pct"], df["oportunidad"] = np.nan, False
        return df

    ref = {}
    for b, m in df[["marca", "modelo"]].drop_duplicates().itertuples(index=False):
        spec = geo.get_model_spec(b, m)
        ref[(b, m)] = spec["precio_nuevo"] if spec else np.nan
    df["_ref"] = [ref.get((b, m), np.nan) for b, m in zip(df["marca"], df["modelo"])]
    df["_elec"] = df["combustible"].isin([geo.FUELS["E"], geo.FUELS["P"]]).astype(float)
    work = df.dropna(subset=["anio", "km", "precio"])

    def design(g: pd.DataFrame, with_ref: bool) -> np.ndarray:
        cols = [np.ones(len(g)), (CURRENT_YEAR - g["anio"].astype(float)).to_numpy(), np.log1p(g["km"].astype(float).to_numpy())]
        if with_ref:
            cols += [np.log(g["_ref"].astype(float).to_numpy()), g["_elec"].to_numpy()]
        return np.column_stack(cols)

    def fit_predict(g: pd.DataFrame, with_ref: bool) -> pd.Series:
        X = design(g, with_ref)
        coef, *_ = np.linalg.lstsq(X, np.log(g["precio"].astype(float).to_numpy()), rcond=None)
        return pd.Series(np.exp(X @ coef), index=g.index)

    # 1) Ajuste por modelo cuando hay muestra suficiente.
    for _, grp in work.groupby(["marca", "modelo"], dropna=False):
        if len(grp) >= 8 and grp["anio"].nunique() >= 3:
            df.loc[grp.index, "precio_estimado"] = fit_predict(grp, False).round(-1)
    # 2) Ajuste global con precio de catálogo para el resto.
    with_ref = work.dropna(subset=["_ref"])
    pending = with_ref[df.loc[with_ref.index, "precio_estimado"].isna()]
    if len(with_ref) >= 12 and not pending.empty:
        X = design(with_ref, True)
        coef, *_ = np.linalg.lstsq(X, np.log(with_ref["precio"].astype(float).to_numpy()), rcond=None)
        df.loc[pending.index, "precio_estimado"] = np.round(np.exp(design(pending, True) @ coef), -1)

    df["diferencia_pct"] = ((df["precio"] - df["precio_estimado"]) / df["precio_estimado"] * 100).round(1)
    df["oportunidad"] = (df["diferencia_pct"] <= -OPPORTUNITY_THRESHOLD).fillna(False)
    return df.drop(columns=["_ref", "_elec"])


# ---------------------------------------------------------------------------
# API PÚBLICA (con caché)
# ---------------------------------------------------------------------------
def search_vehicles(f: SearchFilters) -> tuple[pd.DataFrame, list[dict]]:
    """Búsqueda completa sin caché: descarga -> normaliza -> filtra -> enriquece."""
    results = run_async(scrape_all(f))
    rows = [row for r in results for row in r.listings]
    df = pd.DataFrame(rows, columns=LISTING_COLUMNS) if rows else pd.DataFrame(columns=LISTING_COLUMNS)
    for col in ("precio", "lat", "lon"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("anio", "km", "plazas", "potencia_cv", "garantia_meses"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    df = df.drop_duplicates(subset=["id"])
    df = apply_filters(df, f)
    df = add_distance(df, f)
    df = enrich_market_metrics(df).reset_index(drop=True)
    summary = [{k: v for k, v in asdict(r).items() if k != "listings"} for r in results]
    for s in summary:  # recuento tras filtros unificados
        s["count_filtered"] = int((df["portal"] == s["portal"]).sum()) if not df.empty else 0
    return df, summary


@_cache_data(ttl=1800, show_spinner=False, max_entries=64)
def search_vehicles_cached(filters_dict: dict) -> tuple[pd.DataFrame, list[dict]]:
    """Versión cacheada 30 min (clave = filtros serializados)."""
    data = dict(filters_dict)
    for k in ("portals", "conditions", "fuels", "bodies"):
        data[k] = tuple(data.get(k, ()))
    return search_vehicles(SearchFilters(**data))


def filters_to_dict(f: SearchFilters) -> dict:
    d = asdict(f)
    for k in ("portals", "conditions", "fuels", "bodies"):
        d[k] = list(d[k])
    return d


__all__ = [
    "SearchFilters", "PORTALS", "PortalConfig", "search_vehicles", "search_vehicles_cached", "filters_to_dict",
    "apply_filters", "enrich_market_metrics", "generate_fallback", "extract_raw_listings", "normalize_record",
    "proxy_provider", "placeholder_image", "CURRENT_YEAR",
]
