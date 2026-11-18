"""
CochesRadar España — Buscador Inteligente de Vehículos
=======================================================
Aplicación Streamlit que agrega anuncios de vehículos (nuevos, km 0, seminuevos y
segunda mano) de los principales portales y clasificados de España.

Ejecutar en local:
    streamlit run app.py
"""

from __future__ import annotations

import html
import io
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import scraper_engine as se
import spain_geo_cars as geo

APP_DIR = Path(__file__).parent
MAX_FAVORITES = 4
CARDS_PER_PAGE = 12

st.set_page_config(
    page_title="CochesRadar España — Buscador Inteligente de Vehículos",
    page_icon="🚗",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# UTILIDADES DE FORMATO
# ---------------------------------------------------------------------------
def fmt_int(value) -> str:
    """12345.6 -> '12.346' (formato español)."""
    if value is None or (isinstance(value, float) and np.isnan(value)) or pd.isna(value):
        return "—"
    return f"{float(value):,.0f}".replace(",", ".")


def fmt_eur(value) -> str:
    return "—" if pd.isna(value) else f"{fmt_int(value)} €"


def fmt_km(value) -> str:
    return "—" if pd.isna(value) else f"{fmt_int(value)} km"


def esc(value) -> str:
    return html.escape("" if value is None or (not isinstance(value, str) and pd.isna(value)) else str(value))


def load_css() -> None:
    css_path = APP_DIR / "styles.css"
    if css_path.exists():
        st.markdown(f"<style>{css_path.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# ESTADO DE SESIÓN
# ---------------------------------------------------------------------------
MODE_LABELS = {
    "auto": "Automático (real + referencia)",
    "real": "Solo datos reales",
    "demo": "Demostración (sin red)",
}

DEFAULTS = {
    "f_portals": list(se.PORTALS.keys()),
    "f_community": geo.ALL_OPTION,
    "f_province": geo.ALL_OPTION,
    "f_city": geo.ALL_OPTION,
    "f_radius": 0,
    "f_brand": geo.ALL_OPTION,
    "f_model": "Todos",
    "f_keywords": "",
    "f_years": (2010, se.CURRENT_YEAR),
    "f_price_min": 0,
    "f_price_max": 150_000,
    "f_km_min": 0,
    "f_km_max": 300_000,
    "f_conditions": [],
    "f_seats": "Todas",
    "f_fuels": [],
    "f_transmission": "Todos",
    "f_bodies": [],
    "f_dgt": "Todas",
    "f_seller": "Todos",
    "f_warranty": "Todos",
    "f_imports": True,
    "f_max": 40,
    "f_mode": os.getenv("COCHESRADAR_MODE", "auto") if os.getenv("COCHESRADAR_MODE", "auto") in MODE_LABELS else "auto",
}


def init_state() -> None:
    for key, value in DEFAULTS.items():
        st.session_state.setdefault(key, value)
    st.session_state.setdefault("favorites", {})  # id -> registro completo
    st.session_state.setdefault("results", None)
    st.session_state.setdefault("summary", [])
    st.session_state.setdefault("last_filters", None)
    st.session_state.setdefault("last_run", None)
    st.session_state.setdefault("elapsed", 0.0)
    st.session_state.setdefault("page", 1)


def reset_filters() -> None:
    for key, value in DEFAULTS.items():
        st.session_state[key] = value
    st.session_state["page"] = 1


def ensure_option(key: str, options: list) -> None:
    """Si el valor guardado ya no está entre las opciones (cascada), vuelve a la primera."""
    if st.session_state.get(key) not in options:
        st.session_state[key] = options[0]


def toggle_favorite(record: dict) -> None:
    favs: dict = st.session_state["favorites"]
    rid = record["id"]
    if rid in favs:
        favs.pop(rid)
    elif len(favs) >= MAX_FAVORITES:
        st.session_state["fav_warning"] = True
    else:
        favs[rid] = record


def remove_favorite(rid: str) -> None:
    st.session_state["favorites"].pop(rid, None)


# ---------------------------------------------------------------------------
# SIDEBAR: FILTROS
# ---------------------------------------------------------------------------
def side_title(text: str) -> None:
    st.markdown(f"<div class='cr-side-title'>{esc(text)}</div>", unsafe_allow_html=True)


def build_sidebar() -> tuple[se.SearchFilters, bool]:
    sb = st.sidebar
    with sb:
        st.markdown("<div class='cr-side-brand'>CochesRadar <span>España</span></div>", unsafe_allow_html=True)
        st.caption("Configure la búsqueda y pulse **Buscar vehículos**.")
        search_clicked = st.button("Buscar vehículos", type="primary", width="stretch")

        # ---------------- Portales ----------------
        side_title("Fuentes de datos")
        st.multiselect(
            "Portales a consultar",
            options=list(se.PORTALS.keys()),
            format_func=lambda k: f"{se.PORTALS[k].name} · {se.PORTALS[k].kind}",
            key="f_portals",
            help="Por defecto se consultan todos. Puede limitar la búsqueda a los que prefiera.",
        )
        c1, c2 = st.columns(2)
        c1.button("Todos", on_click=lambda: st.session_state.update(f_portals=list(se.PORTALS.keys())), width="stretch")
        c2.button("Solo portales", on_click=lambda: st.session_state.update(
            f_portals=[k for k, p in se.PORTALS.items() if p.kind == "Portal"]), width="stretch")
        st.selectbox("Modo de datos", options=list(MODE_LABELS), format_func=MODE_LABELS.get, key="f_mode",
                     help="Automático: intenta extraer anuncios reales y, si un portal bloquea la petición, "
                          "muestra datos de referencia enlazados a la búsqueda real del portal.")

        # ---------------- Ubicación (cascada) ----------------
        side_title("Ubicación")
        communities = [geo.ALL_OPTION] + geo.get_communities()
        ensure_option("f_community", communities)
        st.selectbox("Comunidad Autónoma", communities, key="f_community")

        provinces = [geo.ALL_OPTION] + (geo.get_provinces(st.session_state["f_community"])
                                        if st.session_state["f_community"] != geo.ALL_OPTION else geo.get_provinces())
        ensure_option("f_province", provinces)
        st.selectbox("Provincia", provinces, key="f_province")

        cities = [geo.ALL_OPTION] + geo.get_cities(st.session_state["f_province"], st.session_state["f_community"])
        ensure_option("f_city", cities)
        st.selectbox("Ciudad / Municipio", cities, key="f_city")
        city_selected = st.session_state["f_city"] != geo.ALL_OPTION
        st.select_slider("Radio de distancia", options=[0, 10, 25, 50, 75, 100, 150, 200, 300],
                         format_func=lambda v: "Solo el municipio" if v == 0 else f"{v} km",
                         key="f_radius", disabled=not city_selected,
                         help="Disponible al elegir un municipio. Incluye localidades cercanas de otras provincias.")
        st.checkbox("Incluir importación (OLX Portugal)", key="f_imports",
                    help="Los anuncios de importación se muestran aunque estén fuera de la ubicación elegida.")

        # ---------------- Vehículo ----------------
        side_title("Vehículo")
        brands = [geo.ALL_OPTION] + geo.get_brands()
        ensure_option("f_brand", brands)
        st.selectbox("Marca", brands, key="f_brand")
        models = ["Todos"] + geo.get_models(st.session_state["f_brand"])
        ensure_option("f_model", models)
        st.selectbox("Modelo", models, key="f_model", disabled=st.session_state["f_brand"] == geo.ALL_OPTION)
        st.text_input("Búsqueda libre", key="f_keywords", placeholder="p. ej. 7 plazas, híbrido, Tarraco…")
        st.multiselect("Estado", geo.CONDITION_OPTIONS, key="f_conditions", placeholder="Todos")
        st.slider("Año (desde – hasta)", 1995, se.CURRENT_YEAR, key="f_years")
        p1, p2 = st.columns(2)
        p1.number_input("Precio desde (€)", min_value=0, max_value=500_000, step=1_000, key="f_price_min")
        p2.number_input("Precio hasta (€)", min_value=0, max_value=500_000, step=1_000, key="f_price_max")
        k1, k2 = st.columns(2)
        k1.number_input("Km desde", min_value=0, max_value=1_000_000, step=5_000, key="f_km_min")
        k2.number_input("Km hasta", min_value=0, max_value=1_000_000, step=5_000, key="f_km_max")

        # ---------------- Especificaciones ----------------
        side_title("Especificaciones y familia")
        st.selectbox("Número de plazas", geo.SEAT_OPTIONS, key="f_seats",
                     format_func=lambda v: "Todas" if v == "Todas" else f"{v} plazas")
        st.multiselect("Combustible", geo.FUEL_OPTIONS, key="f_fuels", placeholder="Todos")
        st.selectbox("Cambio", ["Todos"] + geo.TRANSMISSIONS, key="f_transmission")
        st.multiselect("Carrocería", geo.BODY_OPTIONS, key="f_bodies", placeholder="Todas")
        st.selectbox("Etiqueta ambiental DGT", geo.DGT_LABELS, key="f_dgt")
        st.selectbox("Tipo de vendedor", geo.SELLER_OPTIONS, key="f_seller")
        st.selectbox("Garantía", geo.WARRANTY_OPTIONS, key="f_warranty")

        side_title("Avanzado")
        st.slider("Máx. anuncios por portal", 10, 80, step=5, key="f_max")
        st.button("Restablecer filtros", on_click=reset_filters, width="stretch")
        st.caption(f"Proxy anti-bloqueo: **{se.proxy_provider()}**")

    ss = st.session_state
    price_min, price_max = sorted((int(ss["f_price_min"]), int(ss["f_price_max"])))
    km_min, km_max = sorted((int(ss["f_km_min"]), int(ss["f_km_max"])))
    filters = se.SearchFilters(
        portals=tuple(ss["f_portals"]) or tuple(se.PORTALS.keys()),
        community=ss["f_community"],
        province=ss["f_province"],
        city=ss["f_city"],
        radius_km=int(ss["f_radius"]) if ss["f_city"] != geo.ALL_OPTION else 0,
        brand=ss["f_brand"],
        model=ss["f_model"] if ss["f_brand"] != geo.ALL_OPTION else "Todos",
        keywords=ss["f_keywords"].strip(),
        year_min=int(ss["f_years"][0]),
        year_max=int(ss["f_years"][1]),
        price_min=price_min,
        price_max=price_max,
        km_min=km_min,
        km_max=km_max,
        conditions=tuple(ss["f_conditions"]),
        seats=str(ss["f_seats"]),
        fuels=tuple(ss["f_fuels"]),
        transmission=ss["f_transmission"],
        bodies=tuple(ss["f_bodies"]),
        dgt_label=ss["f_dgt"],
        seller=ss["f_seller"],
        warranty=ss["f_warranty"],
        include_imports=bool(ss["f_imports"]),
        max_per_portal=int(ss["f_max"]),
        mode=ss["f_mode"],
    )
    return filters, search_clicked


# ---------------------------------------------------------------------------
# BÚSQUEDA
# ---------------------------------------------------------------------------
def run_search(filters: se.SearchFilters) -> None:
    names = ", ".join(se.PORTALS[k].name for k in filters.portals)
    with st.status(f"Consultando {len(filters.portals)} portales…", expanded=False) as status:
        st.write(names)
        start = datetime.now()
        df, summary = se.search_vehicles_cached(se.filters_to_dict(filters))
        elapsed = (datetime.now() - start).total_seconds()
        status.update(label=f"Búsqueda completada: {len(df)} vehículos en {elapsed:.1f} s", state="complete")
    st.session_state.update(results=df, summary=summary, last_filters=filters, last_run=datetime.now(),
                            elapsed=elapsed, page=1)


# ---------------------------------------------------------------------------
# CABECERA, ESTADO Y KPIs
# ---------------------------------------------------------------------------
def render_header() -> None:
    st.markdown(
        f"""<div class="cr-header">
<div class="cr-eyebrow">Inteligencia de mercado automotriz · España</div>
<h1>CochesRadar España <span>— Buscador Inteligente de Vehículos</span></h1>
<p>Agregador de {len(se.PORTALS)} portales y clasificados · Nuevos, Km 0, seminuevos y segunda mano ·
Filtros por plazas, etiqueta DGT, ubicación y precio · Análisis comparativo de mercado</p>
</div>""",
        unsafe_allow_html=True,
    )


def render_status(filters: se.SearchFilters) -> None:
    summary = st.session_state["summary"]
    if not summary:
        return
    real = sum(1 for s in summary if s["status"] == "Real")
    ref = sum(1 for s in summary if s["status"] in ("Fallback", "Demo"))
    off = len(summary) - real - ref
    run = st.session_state["last_run"]
    changed = st.session_state["last_filters"] != filters
    pieces = [
        f"<span><span class='cr-dot ok'></span><b>{real}</b> con datos reales</span>",
        f"<span><span class='cr-dot warn'></span><b>{ref}</b> con datos de referencia</span>",
    ]
    if off:
        pieces.append(f"<span><span class='cr-dot off'></span><b>{off}</b> sin respuesta</span>")
    pieces += [
        f"<span>Modo: <b>{esc(MODE_LABELS[st.session_state['last_filters'].mode])}</b></span>",
        f"<span>Conexión: <b>{esc(se.proxy_provider())}</b></span>",
        f"<span>Duración: <b>{st.session_state['elapsed']:.1f} s</b></span>",
        f"<span>Última consulta: <b>{run.strftime('%d/%m/%Y %H:%M') if run else '—'}</b></span>",
    ]
    if changed:
        pieces.append("<span style='color:#AA151B;font-weight:600'>Filtros modificados · pulse «Buscar vehículos»</span>")
    st.markdown(f"<div class='cr-status'>{''.join(pieces)}</div>", unsafe_allow_html=True)

    if ref and st.session_state["last_filters"].mode == "demo":
        st.markdown(
            "<div class='cr-notice'><b>Modo demostración:</b> no se realizan peticiones a los portales. Se muestran anuncios "
            "de referencia realistas (marcados como «Referencia») y el botón <i>Ver Anuncio Original</i> abre la búsqueda "
            "equivalente en el portal real. Cambie a modo «Automático» para consultar datos reales.</div>",
            unsafe_allow_html=True,
        )
    elif ref:
        st.markdown(
            "<div class='cr-notice'><b>Datos de referencia:</b> algunos portales bloquean las peticiones automáticas "
            "desde servidores en la nube. Para esos portales se muestran anuncios de referencia realistas (marcados como "
            "«Referencia») y el botón <i>Ver Anuncio Original</i> abre la búsqueda equivalente en el portal real. "
            "Configure <code>ZENROWS_API_KEY</code> o <code>SCRAPERAPI_KEY</code> para maximizar los datos reales.</div>",
            unsafe_allow_html=True,
        )
    with st.expander("Detalle de la consulta por portal"):
        sdf = pd.DataFrame(summary)[["portal", "status", "count", "count_filtered", "elapsed", "message", "url"]]
        sdf.columns = ["Portal", "Estado", "Recibidos", "Tras filtros", "Tiempo (s)", "Detalle", "Búsqueda en el portal"]
        st.dataframe(sdf, hide_index=True, width="stretch",
                     column_config={"Búsqueda en el portal": st.column_config.LinkColumn(display_text="Abrir")})


def render_kpis(df: pd.DataFrame) -> None:
    n = len(df)
    real_n = int((df["origen_dato"] == "Real").sum()) if n else 0
    deals = int(df["oportunidad"].fillna(False).sum()) if n else 0
    kpis = [
        ("Vehículos encontrados", fmt_int(n), f"{real_n} reales · {deals} oportunidades", True),
        ("Precio medio", fmt_eur(df["precio"].mean()) if n else "—", f"Mediana {fmt_eur(df['precio'].median())}" if n else "", False),
        ("Precio mínimo", fmt_eur(df["precio"].min()) if n else "—", f"Máximo {fmt_eur(df['precio'].max())}" if n else "", False),
        ("Kilometraje medio", fmt_km(df["km"].mean()) if n else "—", f"Mediana {fmt_km(df['km'].median())}" if n else "", False),
        ("Año medio", f"{df['anio'].mean():.1f}".replace(".", ",") if n and df["anio"].notna().any() else "—",
         f"Rango {int(df['anio'].min())}–{int(df['anio'].max())}" if n and df["anio"].notna().any() else "", False),
    ]
    items = "".join(
        f"<div class='cr-kpi{' primary' if primary else ''}'><div class='cr-kpi-label'>{esc(label)}</div>"
        f"<div class='cr-kpi-value'>{esc(value)}</div><div class='cr-kpi-sub'>{esc(sub)}</div></div>"
        for label, value, sub, primary in kpis
    )
    st.markdown(f"<div class='cr-kpis'>{items}</div>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# EXPORTACIÓN
# ---------------------------------------------------------------------------
EXPORT_COLUMNS = {
    "portal": "Portal", "titulo": "Título", "marca": "Marca", "modelo": "Modelo", "precio": "Precio (€)",
    "precio_estimado": "Precio estimado (€)", "diferencia_pct": "Dif. vs mercado (%)", "anio": "Año", "km": "Km",
    "combustible": "Combustible", "cambio": "Cambio", "carroceria": "Carrocería", "plazas": "Plazas",
    "etiqueta_dgt": "Etiqueta DGT", "estado": "Estado", "vendedor": "Vendedor", "garantia": "Garantía",
    "garantia_meses": "Garantía (meses)", "potencia_cv": "Potencia (CV)", "color": "Color", "ciudad": "Ciudad",
    "provincia": "Provincia", "comunidad": "Comunidad", "distancia_km": "Distancia (km)",
    "fecha_publicacion": "Publicado", "origen_dato": "Origen dato", "url": "Enlace",
}


def export_frame(df: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in EXPORT_COLUMNS if c in df.columns]
    out = df[cols].rename(columns=EXPORT_COLUMNS)
    out["Origen dato"] = out["Origen dato"].replace({"Simulado": "Referencia"})
    return out


def build_csv(df: pd.DataFrame) -> bytes:
    """CSV compatible con Excel en español (separador ';' y coma decimal)."""
    return export_frame(df).to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig")


def build_excel(df: pd.DataFrame, filters: se.SearchFilters, summary: list[dict]) -> bytes:
    """Excel formateado: cabecera corporativa, enlaces clicables, formatos y hoja resumen."""
    data = export_frame(df)
    wb = Workbook()
    ws = wb.active
    ws.title = "Resultados"

    crimson, gold = "AA151B", "D4A017"
    head_font = Font(bold=True, color="FFFFFF", name="Calibri", size=11)
    head_fill = PatternFill("solid", fgColor=crimson)
    gold_border = Border(bottom=Side(style="medium", color=gold))
    thin = Border(bottom=Side(style="thin", color="E2E8F0"))
    deal_fill = PatternFill("solid", fgColor="FFF7DB")

    headers = list(data.columns)
    ws.append(headers)
    for idx, _ in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=idx)
        cell.font, cell.fill, cell.border = head_font, head_fill, gold_border
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 30

    link_col = headers.index("Enlace") + 1
    money_cols = {headers.index(h) + 1 for h in ("Precio (€)", "Precio estimado (€)") if h in headers}
    int_cols = {headers.index(h) + 1 for h in ("Km", "Distancia (km)") if h in headers}
    pct_col = headers.index("Dif. vs mercado (%)") + 1 if "Dif. vs mercado (%)" in headers else None
    deals = df["oportunidad"].fillna(False).tolist() if "oportunidad" in df else [False] * len(data)

    for r_idx, row in enumerate(data.itertuples(index=False), start=2):
        for c_idx, value in enumerate(row, start=1):
            if value is pd.NA or (isinstance(value, float) and np.isnan(value)):
                value = None
            elif isinstance(value, (np.integer,)):
                value = int(value)
            elif isinstance(value, (np.floating,)):
                value = float(value)
            cell = ws.cell(row=r_idx, column=c_idx, value=value)
            cell.border = thin
            if c_idx in money_cols:
                cell.number_format = '#,##0 "€"'
            elif c_idx in int_cols:
                cell.number_format = "#,##0"
            elif c_idx == pct_col:
                cell.number_format = '+0.0"%";-0.0"%"'
            if deals[r_idx - 2]:
                cell.fill = deal_fill
        link_cell = ws.cell(row=r_idx, column=link_col)
        if link_cell.value:
            url = str(link_cell.value)
            link_cell.value = "Ver anuncio"
            link_cell.hyperlink = url
            link_cell.font = Font(color="1D4ED8", underline="single")

    widths = {"Título": 46, "Enlace": 14, "Comunidad": 22, "Vendedor": 24, "Combustible": 22, "Carrocería": 18, "Portal": 18}
    for idx, header in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = widths.get(header, max(11, len(header) + 3))
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(1, len(data) + 1)}"

    # --- Hoja Resumen ---
    rs = wb.create_sheet("Resumen")
    rs["A1"] = "CochesRadar España — Informe de búsqueda"
    rs["A1"].font = Font(bold=True, size=16, color=crimson)
    rs["A2"] = f"Generado el {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    rs["A2"].font = Font(italic=True, color="64748B")
    rows = [
        ("Vehículos", len(df)),
        ("Precio medio (€)", round(float(df["precio"].mean()), 0) if len(df) else None),
        ("Precio mínimo (€)", float(df["precio"].min()) if len(df) else None),
        ("Km medio", round(float(df["km"].mean()), 0) if len(df) and df["km"].notna().any() else None),
        ("Año medio", round(float(df["anio"].mean()), 1) if len(df) and df["anio"].notna().any() else None),
        ("Oportunidades (≥15 % bajo mercado)", int(df["oportunidad"].fillna(False).sum()) if len(df) else 0),
    ]
    rs["A4"], rs["A4"].font = "Indicadores", Font(bold=True, color=crimson, size=12)
    for i, (k, v) in enumerate(rows, start=5):
        rs.cell(row=i, column=1, value=k).font = Font(bold=True)
        c = rs.cell(row=i, column=2, value=v)
        if "€" in k:
            c.number_format = '#,##0 "€"'
    start = 5 + len(rows) + 1
    rs.cell(row=start, column=1, value="Filtros aplicados").font = Font(bold=True, color=crimson, size=12)
    labels = {
        "portals": "Portales", "community": "Comunidad", "province": "Provincia", "city": "Municipio", "radius_km": "Radio (km)",
        "brand": "Marca", "model": "Modelo", "keywords": "Búsqueda libre", "year_min": "Año desde", "year_max": "Año hasta",
        "price_min": "Precio desde", "price_max": "Precio hasta", "km_min": "Km desde", "km_max": "Km hasta",
        "conditions": "Estado", "seats": "Plazas", "fuels": "Combustible", "transmission": "Cambio", "bodies": "Carrocería",
        "dgt_label": "Etiqueta DGT", "seller": "Vendedor", "warranty": "Garantía", "include_imports": "Incluye importación",
        "mode": "Modo de datos",
    }
    fd = se.filters_to_dict(filters)
    for i, (k, label) in enumerate(labels.items(), start=start + 1):
        v = fd.get(k)
        if k == "portals":
            v = ", ".join(se.PORTALS[p].name for p in v)
        elif isinstance(v, list):
            v = ", ".join(v) or "Todos"
        elif k == "mode":
            v = MODE_LABELS.get(v, v)
        rs.cell(row=i, column=1, value=label).font = Font(bold=True)
        rs.cell(row=i, column=2, value=str(v))
    start = start + len(labels) + 2
    rs.cell(row=start, column=1, value="Estado por portal").font = Font(bold=True, color=crimson, size=12)
    for j, h in enumerate(["Portal", "Estado", "Anuncios tras filtros", "Detalle"], start=1):
        c = rs.cell(row=start + 1, column=j, value=h)
        c.font, c.fill = head_font, head_fill
    for i, s in enumerate(summary, start=start + 2):
        rs.cell(row=i, column=1, value=s["portal"])
        rs.cell(row=i, column=2, value="Referencia" if s["status"] in ("Fallback", "Demo") else s["status"])
        rs.cell(row=i, column=3, value=s.get("count_filtered", 0))
        rs.cell(row=i, column=4, value=s["message"])
    rs.column_dimensions["A"].width, rs.column_dimensions["B"].width = 36, 48
    rs.column_dimensions["C"].width, rs.column_dimensions["D"].width = 22, 70

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def render_export(df: pd.DataFrame, filters: se.SearchFilters) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    c1, c2, _ = st.columns([1, 1, 3])
    c1.download_button("Descargar Excel (.xlsx)", data=build_excel(df, filters, st.session_state["summary"]),
                       file_name=f"cochesradar_{stamp}.xlsx", type="primary", width="stretch",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", disabled=df.empty)
    c2.download_button("Descargar CSV", data=build_csv(df), file_name=f"cochesradar_{stamp}.csv",
                       mime="text/csv", width="stretch", disabled=df.empty)


# ---------------------------------------------------------------------------
# PESTAÑA 1: RESULTADOS
# ---------------------------------------------------------------------------
SORT_OPTIONS = {
    "Menor precio": ("precio", True),
    "Mayor precio": ("precio", False),
    "Menor kilometraje": ("km", True),
    "Más reciente (año)": ("anio", False),
    "Publicación más reciente": ("fecha_publicacion", False),
    "Mejor oportunidad": ("diferencia_pct", True),
    "Más cercano": ("distancia_km", True),
}


def sort_df(df: pd.DataFrame, option: str) -> pd.DataFrame:
    col, asc = SORT_OPTIONS[option]
    if col not in df or df[col].isna().all():
        col, asc = "precio", True
    return df.sort_values([col, "precio"], ascending=[asc, True], na_position="last")


def card_html(row: pd.Series) -> str:
    portal = next((p for p in se.PORTALS.values() if p.name == row["portal"]), None)
    pcolor = portal.color if portal else "#AA151B"
    dgt = row["etiqueta_dgt"] or "Sin etiqueta"
    dgt_color = geo.DGT_COLORS.get(dgt, "#64748B")
    delta = ""
    if pd.notna(row.get("diferencia_pct")):
        d = float(row["diferencia_pct"])
        cls = "down" if d < 0 else "up"
        delta = f"<span class='cr-delta {cls}'>{d:+.0f}% vs mercado</span>".replace(".", ",")
    chips = [
        f"<span class='cr-chip'>{esc(row['anio']) if pd.notna(row['anio']) else '—'}</span>",
        f"<span class='cr-chip'>{esc(fmt_km(row['km']))}</span>",
        f"<span class='cr-chip'>{esc(row['combustible'] or '—')}</span>",
        f"<span class='cr-chip seats'>{esc(row['plazas']) if pd.notna(row['plazas']) else '—'} plazas</span>",
        f"<span class='cr-chip'>{esc(row['cambio'] or '—')}</span>",
    ]
    dist = f" · {fmt_int(row['distancia_km'])} km" if pd.notna(row.get("distancia_km")) else ""
    warranty = f"Garantía {int(row['garantia_meses'])} meses" if row["garantia"] == "Sí" and pd.notna(row["garantia_meses"]) and row["garantia_meses"] else "Sin garantía"
    deal_badge = "<span class='cr-badge deal'>Oportunidad</span>" if bool(row.get("oportunidad")) else ""
    ref_badge = "<span class='cr-badge ref'>Referencia</span>" if row["origen_dato"] == "Simulado" else ""
    return (
        f"<div class='cr-card'><div class='cr-card-img'>"
        f"<img src='{esc(row['imagen'])}' alt='{esc(row['titulo'])}' loading='lazy'>"
        f"<span class='cr-badge portal' style='background:{pcolor}'>{esc(row['portal'])}</span>"
        f"<span class='cr-badge dgt' style='background:{dgt_color}'>{esc(dgt)}</span>{deal_badge}{ref_badge}</div>"
        f"<div class='cr-card-body'><div class='cr-card-title' title='{esc(row['titulo'])}'>{esc(row['titulo'])}</div>"
        f"<div class='cr-price-row'><span class='cr-price'>{esc(fmt_eur(row['precio']))}</span>{delta}</div>"
        f"<div class='cr-specs'>{''.join(chips)}</div>"
        f"<div class='cr-loc'>{esc(row['ciudad'])}, {esc(row['provincia'])}{esc(dist)}</div>"
        f"<div class='cr-meta'>{esc(row['estado'])} · {esc(row['vendedor'])} · {esc(warranty)}</div>"
        f"</div></div>"
    )


def render_cards(df: pd.DataFrame) -> None:
    total_pages = max(1, int(np.ceil(len(df) / CARDS_PER_PAGE)))
    st.session_state["page"] = min(max(1, st.session_state["page"]), total_pages)
    page = st.session_state["page"]
    chunk = df.iloc[(page - 1) * CARDS_PER_PAGE: page * CARDS_PER_PAGE]
    favs = st.session_state["favorites"]

    for start in range(0, len(chunk), 3):
        cols = st.columns(3, gap="medium")
        for col, (_, row) in zip(cols, chunk.iloc[start:start + 3].iterrows()):
            with col:
                st.markdown(card_html(row), unsafe_allow_html=True)
                b1, b2 = st.columns([3, 2])
                b1.link_button("Ver Anuncio Original", row["url"], type="primary", width="stretch")
                is_fav = row["id"] in favs
                b2.button("★ Quitar" if is_fav else "☆ Comparar", key=f"fav_{row['id']}", width="stretch",
                          on_click=toggle_favorite, args=(row.to_dict(),),
                          help="Añadir / quitar del comparador (máx. 4)")

    n1, n2, n3 = st.columns([1, 2, 1])
    n1.button("‹ Anterior", disabled=page <= 1, width="stretch",
              on_click=lambda: st.session_state.update(page=st.session_state["page"] - 1))
    n2.markdown(f"<div style='text-align:center;padding-top:8px;color:#64748B;font-size:.85rem'>"
                f"Página <b>{page}</b> de <b>{total_pages}</b> · {len(df)} vehículos</div>", unsafe_allow_html=True)
    n3.button("Siguiente ›", disabled=page >= total_pages, width="stretch",
              on_click=lambda: st.session_state.update(page=st.session_state["page"] + 1))


def render_table(df: pd.DataFrame) -> None:
    view = df.copy()
    view["origen_dato"] = view["origen_dato"].replace({"Simulado": "Referencia"})
    view["fav"] = view["id"].isin(st.session_state["favorites"].keys())
    cols = ["fav", "imagen", "titulo", "precio", "diferencia_pct", "anio", "km", "combustible", "cambio", "plazas",
            "carroceria", "etiqueta_dgt", "estado", "vendedor", "garantia", "ciudad", "provincia", "distancia_km",
            "portal", "origen_dato", "url", "id"]
    if view["distancia_km"].isna().all():
        cols.remove("distancia_km")
    edited = st.data_editor(
        view[cols],
        hide_index=True,
        width="stretch",
        height=560,
        disabled=[c for c in cols if c != "fav"],
        column_order=[c for c in cols if c != "id"],
        key="results_table",
        column_config={
            "fav": st.column_config.CheckboxColumn("Comparar", help="Marque hasta 4 vehículos", width="small"),
            "imagen": st.column_config.ImageColumn("Foto", width="small"),
            "titulo": st.column_config.TextColumn("Vehículo", width="large"),
            "precio": st.column_config.NumberColumn("Precio (€)", format="localized"),
            "diferencia_pct": st.column_config.NumberColumn("vs mercado", format="%+.0f%%"),
            "anio": st.column_config.NumberColumn("Año", format="%d"),
            "km": st.column_config.NumberColumn("Km", format="localized"),
            "combustible": "Combustible", "cambio": "Cambio",
            "plazas": st.column_config.NumberColumn("Plazas", format="%d"),
            "carroceria": "Carrocería", "etiqueta_dgt": "DGT", "estado": "Estado", "vendedor": "Vendedor",
            "garantia": "Garantía", "ciudad": "Ciudad", "provincia": "Provincia",
            "distancia_km": st.column_config.NumberColumn("Distancia", format="%.0f km"),
            "portal": "Portal", "origen_dato": "Origen",
            "url": st.column_config.LinkColumn("Anuncio", display_text="Ver anuncio"),
        },
    )
    # Sincroniza las casillas de la tabla con el comparador.
    selected = set(edited.loc[edited["fav"], "id"])
    favs: dict = st.session_state["favorites"]
    current = set(favs.keys())
    visible_ids = set(view["id"])
    added = [i for i in selected - current]
    removed = [i for i in (current & visible_ids) - selected]
    if added or removed:
        for rid in removed:
            favs.pop(rid, None)
        for rid in added:
            if len(favs) >= MAX_FAVORITES:
                st.session_state["fav_warning"] = True
                break
            favs[rid] = df.loc[df["id"] == rid].iloc[0].to_dict()
        st.session_state.pop("results_table", None)
        st.rerun()


def render_results_tab(df: pd.DataFrame) -> None:
    c1, c2, c3, c4 = st.columns([1.3, 1.6, 1.4, 2])
    view_mode = c1.segmented_control("Vista", ["Tarjetas", "Tabla"], default="Tarjetas", key="view_mode")
    sort_opts = [o for o in SORT_OPTIONS if o != "Más cercano" or df["distancia_km"].notna().any()]
    sort_by = c2.selectbox("Ordenar por", sort_opts, key="sort_by", on_change=lambda: st.session_state.update(page=1))
    only_deals = c3.toggle("Solo oportunidades", key="only_deals", help="Precio ≥ 15 % por debajo del valor estimado de mercado.",
                           on_change=lambda: st.session_state.update(page=1))
    only_real = c4.toggle("Solo anuncios reales", key="only_real", on_change=lambda: st.session_state.update(page=1))

    view = df
    if only_deals:
        view = view[view["oportunidad"].fillna(False)]
    if only_real:
        view = view[view["origen_dato"] == "Real"]
    view = sort_df(view, sort_by)

    if st.session_state.pop("fav_warning", False):
        st.warning(f"El comparador admite como máximo {MAX_FAVORITES} vehículos. Quite alguno antes de añadir otro.")
    if view.empty:
        st.markdown("<div class='cr-empty'>No hay vehículos que cumplan los criterios actuales.<br>"
                    "Amplíe el rango de precio, año o kilometraje, o elija más portales.</div>", unsafe_allow_html=True)
        return
    if view_mode == "Tabla":
        render_table(view)
    else:
        render_cards(view)


# ---------------------------------------------------------------------------
# PESTAÑA 2: ANÁLISIS DE MERCADO
# ---------------------------------------------------------------------------
CHART_FONT = dict(family="Inter, Segoe UI, sans-serif", size=12, color="#1E293B")


def style_fig(fig: go.Figure, height: int = 380) -> go.Figure:
    fig.update_layout(
        template="plotly_white", font=CHART_FONT, height=height, margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="#FFFFFF", plot_bgcolor="#FFFFFF", hoverlabel=dict(bgcolor="#FFFFFF", font_size=12),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, title=None),
        separators=",.",
    )
    fig.update_xaxes(gridcolor="#EEF2F6", linecolor="#E2E8F0", zeroline=False)
    fig.update_yaxes(gridcolor="#EEF2F6", linecolor="#E2E8F0", zeroline=False)
    return fig


def render_analysis_tab(df: pd.DataFrame) -> None:
    if len(df) < 3:
        st.markdown("<div class='cr-empty'>Se necesitan al menos 3 vehículos para el análisis de mercado.</div>", unsafe_allow_html=True)
        return

    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.markdown("<div class='cr-section-title'>Distribución de precios por portal</div>", unsafe_allow_html=True)
        order = df.groupby("portal")["precio"].median().sort_values().index.tolist()
        fig = go.Figure()
        for portal in order:
            fig.add_trace(go.Box(y=df.loc[df["portal"] == portal, "precio"], name=portal, boxpoints="outliers",
                                 marker=dict(color="#AA151B", size=4, opacity=.55), line=dict(color="#AA151B", width=1.4),
                                 fillcolor="rgba(170,21,27,.10)", hovertemplate="%{y:,.0f} €<extra>" + portal + "</extra>"))
        fig.update_layout(showlegend=False)
        fig.update_xaxes(tickangle=-35)
        fig.update_yaxes(title="Precio (€)", tickformat=",.0f")
        st.plotly_chart(style_fig(fig), width="stretch", config={"displaylogo": False})

    with c2:
        st.markdown("<div class='cr-section-title'>Precio medio por año de matriculación</div>", unsafe_allow_html=True)
        by_year = (df.dropna(subset=["anio"]).groupby("anio")
                   .agg(media=("precio", "mean"), mediana=("precio", "median"), n=("precio", "size")).reset_index())
        fig = go.Figure()
        fig.add_trace(go.Bar(x=by_year["anio"], y=by_year["media"], name="Precio medio", marker_color="#AA151B",
                             customdata=by_year["n"], hovertemplate="Año %{x}<br>Media %{y:,.0f} €<br>%{customdata} anuncios<extra></extra>"))
        fig.add_trace(go.Scatter(x=by_year["anio"], y=by_year["mediana"], name="Mediana", mode="lines+markers",
                                 line=dict(color="#D4A017", width=2.5), marker=dict(size=6),
                                 hovertemplate="Año %{x}<br>Mediana %{y:,.0f} €<extra></extra>"))
        fig.update_xaxes(title="Año", dtick=1 if len(by_year) <= 16 else 2)
        fig.update_yaxes(title="Precio (€)", tickformat=",.0f")
        st.plotly_chart(style_fig(fig), width="stretch", config={"displaylogo": False})

    st.markdown("<div class='cr-section-title'>Relación precio × kilometraje · oportunidades bajo la media</div>", unsafe_allow_html=True)
    sc = df.dropna(subset=["km", "precio"]).copy()
    sc["km"] = sc["km"].astype(float)
    sc["tipo"] = np.where(sc["oportunidad"].fillna(False), "Oportunidad (≥15 % bajo mercado)", "Resto de anuncios")
    sc["dif"] = sc["diferencia_pct"].map(lambda v: f"{v:+.1f} %".replace(".", ",") if pd.notna(v) else "—")
    fig = px.scatter(sc, x="km", y="precio", color="tipo", symbol="tipo",
                     color_discrete_map={"Oportunidad (≥15 % bajo mercado)": "#AA151B", "Resto de anuncios": "#94A3B8"},
                     custom_data=["titulo", "portal", "anio", "dif", "ciudad"], opacity=.8)
    fig.update_traces(marker=dict(size=8, line=dict(width=.5, color="#FFFFFF")),
                      hovertemplate="<b>%{customdata[0]}</b><br>%{y:,.0f} € · %{x:,.0f} km · %{customdata[2]}"
                                    "<br>%{customdata[1]} · %{customdata[4]}<br>vs mercado: %{customdata[3]}<extra></extra>")
    if len(sc) >= 5 and sc["km"].nunique() > 2:
        coef = np.polyfit(sc["km"], np.log(sc["precio"]), 1)
        xs = np.linspace(sc["km"].min(), sc["km"].max(), 80)
        fig.add_trace(go.Scatter(x=xs, y=np.exp(np.polyval(coef, xs)), mode="lines", name="Tendencia de mercado",
                                 line=dict(color="#D4A017", width=3, dash="dash"), hoverinfo="skip"))
    fig.update_xaxes(title="Kilometraje (km)", tickformat=",.0f")
    fig.update_yaxes(title="Precio (€)", tickformat=",.0f")
    st.plotly_chart(style_fig(fig, 460), width="stretch", config={"displaylogo": False})

    c3, c4 = st.columns(2, gap="large")
    with c3:
        st.markdown("<div class='cr-section-title'>Precio medio por combustible</div>", unsafe_allow_html=True)
        fu = df.groupby("combustible", dropna=True).agg(media=("precio", "mean"), n=("precio", "size")).reset_index().sort_values("media")
        fig = go.Figure(go.Bar(x=fu["media"], y=fu["combustible"], orientation="h", marker_color="#AA151B",
                               customdata=fu["n"], text=fu["media"].map(fmt_eur), textposition="outside", cliponaxis=False,
                               hovertemplate="%{y}<br>%{x:,.0f} €<br>%{customdata} anuncios<extra></extra>"))
        fig.update_xaxes(title="Precio medio (€)", tickformat=",.0f")
        st.plotly_chart(style_fig(fig, 340), width="stretch", config={"displaylogo": False})
    with c4:
        st.markdown("<div class='cr-section-title'>Oferta por etiqueta ambiental DGT</div>", unsafe_allow_html=True)
        lab = df["etiqueta_dgt"].fillna("Sin etiqueta").value_counts().reindex(["Cero Emisiones", "ECO", "C", "B", "Sin etiqueta"]).dropna()
        fig = go.Figure(go.Bar(x=lab.index, y=lab.values, marker_color=[geo.DGT_COLORS[k] for k in lab.index],
                               text=lab.values, textposition="outside", cliponaxis=False,
                               hovertemplate="%{x}: %{y} anuncios<extra></extra>"))
        fig.update_yaxes(title="Anuncios")
        st.plotly_chart(style_fig(fig, 340), width="stretch", config={"displaylogo": False})

    deals = df[df["oportunidad"].fillna(False)].sort_values("diferencia_pct").head(15)
    st.markdown(f"<div class='cr-section-title'>Top oportunidades detectadas ({int(df['oportunidad'].fillna(False).sum())})</div>",
                unsafe_allow_html=True)
    if deals.empty:
        st.caption("No se han detectado anuncios claramente por debajo del valor de mercado con los filtros actuales.")
    else:
        st.dataframe(
            deals[["titulo", "precio", "precio_estimado", "diferencia_pct", "anio", "km", "ciudad", "portal", "url"]],
            hide_index=True, width="stretch",
            column_config={
                "titulo": st.column_config.TextColumn("Vehículo", width="large"),
                "precio": st.column_config.NumberColumn("Precio (€)", format="localized"),
                "precio_estimado": st.column_config.NumberColumn("Valor estimado (€)", format="localized"),
                "diferencia_pct": st.column_config.NumberColumn("Diferencia", format="%+.1f%%"),
                "anio": st.column_config.NumberColumn("Año", format="%d"),
                "km": st.column_config.NumberColumn("Km", format="localized"),
                "ciudad": "Ciudad", "portal": "Portal",
                "url": st.column_config.LinkColumn("Anuncio", display_text="Ver anuncio"),
            },
        )
    st.caption("Valor estimado: regresión log-lineal (precio ~ antigüedad + log km) por marca/modelo; cuando hay pocos "
               "anuncios del modelo se usa un ajuste global que incorpora el precio de catálogo y la electrificación.")


# ---------------------------------------------------------------------------
# PESTAÑA 3: COMPARADOR DE FAVORITOS
# ---------------------------------------------------------------------------
def render_compare_tab() -> None:
    favs: dict = st.session_state["favorites"]
    if not favs:
        st.markdown("<div class='cr-empty'>Aún no hay vehículos en el comparador.<br>"
                    "Pulse «☆ Comparar» en las tarjetas o marque la casilla en la tabla (máximo 4).</div>",
                    unsafe_allow_html=True)
        return
    records = list(favs.values())
    cols = st.columns(MAX_FAVORITES, gap="medium")
    for col, rec in zip(cols, records):
        with col:
            st.markdown(f"<div class='cr-fav-head'><img src='{esc(rec['imagen'])}'><div class='t'>{esc(rec['titulo'])}</div>"
                        f"<div class='p'>{esc(fmt_eur(rec['precio']))}</div></div>", unsafe_allow_html=True)
            a, b = st.columns(2)
            a.link_button("Anuncio", rec["url"], width="stretch")
            b.button("Quitar", key=f"rm_{rec['id']}", on_click=remove_favorite, args=(rec["id"],), width="stretch")

    fields = [
        ("Precio", "precio", "min", fmt_eur),
        ("Valor estimado de mercado", "precio_estimado", None, fmt_eur),
        ("Diferencia vs mercado", "diferencia_pct", "min", lambda v: "—" if pd.isna(v) else f"{v:+.1f} %".replace(".", ",")),
        ("Año", "anio", "max", lambda v: "—" if pd.isna(v) else str(int(v))),
        ("Kilometraje", "km", "min", fmt_km),
        ("Potencia", "potencia_cv", "max", lambda v: "—" if pd.isna(v) else f"{int(v)} CV"),
        ("Plazas", "plazas", "max", lambda v: "—" if pd.isna(v) else str(int(v))),
        ("Combustible", "combustible", None, str),
        ("Cambio", "cambio", None, str),
        ("Carrocería", "carroceria", None, str),
        ("Etiqueta DGT", "etiqueta_dgt", None, str),
        ("Estado", "estado", None, str),
        ("Garantía (meses)", "garantia_meses", "max", lambda v: "—" if pd.isna(v) or not v else f"{int(v)} meses"),
        ("Vendedor", "vendedor", None, str),
        ("Ubicación", "ciudad", None, str),
        ("Provincia", "provincia", None, str),
        ("Portal", "portal", None, str),
        ("Origen del dato", "origen_dato", None, lambda v: "Referencia" if v == "Simulado" else str(v)),
    ]
    names = [f"{i + 1}. {r['marca']} {r['modelo']}" for i, r in enumerate(records)]
    matrix, best_mask = {}, {}
    for label, key, best, fmt in fields:
        values = [r.get(key) for r in records]
        matrix[label] = [fmt(v) if v is not None and not (isinstance(v, float) and np.isnan(v)) else "—" for v in values]
        mask = [False] * len(values)
        numeric = [float(v) if v is not None and not pd.isna(v) else None for v in values] if best else []
        valid = [v for v in numeric if v is not None]
        if best and len(valid) >= 2 and len(set(valid)) > 1:
            target = min(valid) if best == "min" else max(valid)
            mask = [v == target for v in numeric]
        best_mask[label] = mask
    table = pd.DataFrame(matrix, index=names).T

    def highlight(data: pd.DataFrame) -> pd.DataFrame:
        styles = pd.DataFrame("", index=data.index, columns=data.columns)
        for label, mask in best_mask.items():
            for j, flag in enumerate(mask):
                if flag:
                    styles.iloc[data.index.get_loc(label), j] = "background-color:#FFF7DB;color:#8A1116;font-weight:700"
        return styles

    st.markdown("<div class='cr-section-title'>Matriz técnica comparativa</div>", unsafe_allow_html=True)
    st.dataframe(table.style.apply(highlight, axis=None), width="stretch", height=35 * (len(fields) + 1) + 3)
    st.caption("Resaltado en dorado: mejor valor de cada fila (menor precio/km, mayor año/potencia/garantía).")
    if len(records) >= 2:
        cheapest = min(records, key=lambda r: r["precio"])
        st.markdown(f"**Resumen:** el más económico es **{esc(cheapest['titulo'])}** ({fmt_eur(cheapest['precio'])}).")
    st.button("Vaciar comparador", on_click=lambda: st.session_state["favorites"].clear())


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    load_css()
    init_state()
    filters, clicked = build_sidebar()
    render_header()

    if clicked or st.session_state["results"] is None:
        run_search(filters)

    df: pd.DataFrame = st.session_state["results"]
    render_status(filters)
    render_kpis(df)

    n_fav = len(st.session_state["favorites"])
    tab1, tab2, tab3 = st.tabs(["Resultados", "Análisis de mercado", f"Comparador favoritos ({n_fav}/{MAX_FAVORITES})"])
    with tab1:
        render_export(df, st.session_state["last_filters"])
        render_results_tab(df)
    with tab2:
        render_analysis_tab(df)
    with tab3:
        render_compare_tab()

    g = geo.geo_summary()
    st.markdown(
        f"<div class='cr-footer'>CochesRadar España · {g['comunidades']} Comunidades Autónomas, {g['ciudades_autonomas']} Ciudades Autónomas, "
        f"{g['provincias']} provincias y {g['municipios']} municipios · Los precios y datos pertenecen a sus respectivos portales. "
        f"Verifique siempre la información en el anuncio original.</div>",
        unsafe_allow_html=True,
    )


main()
