"""
spain_geo_cars.py
=================
Base de datos estructurada de CochesRadar España.

Contiene:
  * Jerarquía geográfica real de España: 17 Comunidades Autónomas + 2 Ciudades
    Autónomas (Ceuta y Melilla), 50 provincias y sus principales municipios con
    coordenadas aproximadas (lat, lon) para el filtro por radio de distancia.
  * Catálogo de marcas y modelos comercializados en España con su carrocería,
    plazas disponibles, combustibles y precio orientativo de nuevo (€).
  * Constantes de filtros (combustibles, carrocerías, etiquetas DGT, etc.) y
    funciones auxiliares (cascada geográfica, distancia haversine, etiqueta DGT).
"""

from __future__ import annotations

import math
import unicodedata
from functools import lru_cache

# ---------------------------------------------------------------------------
# 1. GEOGRAFÍA: Comunidad Autónoma -> Provincia -> {Ciudad: (lat, lon)}
# ---------------------------------------------------------------------------
SPAIN_GEO: dict[str, dict[str, dict[str, tuple[float, float]]]] = {
    "Andalucía": {
        "Almería": {"Almería": (36.840, -2.463), "Roquetas de Mar": (36.764, -2.615), "El Ejido": (36.776, -2.814), "Vícar": (36.831, -2.643)},
        "Cádiz": {"Cádiz": (36.527, -6.289), "Jerez de la Frontera": (36.686, -6.137), "Algeciras": (36.131, -5.454), "San Fernando": (36.464, -6.198), "El Puerto de Santa María": (36.594, -6.233)},
        "Córdoba": {"Córdoba": (37.888, -4.779), "Lucena": (37.409, -4.485), "Puente Genil": (37.389, -4.767), "Montilla": (37.587, -4.639)},
        "Granada": {"Granada": (37.177, -3.599), "Motril": (36.745, -3.518), "Almuñécar": (36.733, -3.690), "Armilla": (37.141, -3.627)},
        "Huelva": {"Huelva": (37.261, -6.945), "Lepe": (37.255, -7.204), "Almonte": (37.264, -6.517), "Isla Cristina": (37.199, -7.320)},
        "Jaén": {"Jaén": (37.779, -3.785), "Linares": (38.093, -3.636), "Úbeda": (38.013, -3.370), "Andújar": (38.039, -4.051)},
        "Málaga": {"Málaga": (36.721, -4.421), "Marbella": (36.510, -4.883), "Estepona": (36.426, -5.147), "Fuengirola": (36.540, -4.625), "Vélez-Málaga": (36.781, -4.100), "Mijas": (36.596, -4.637), "Torremolinos": (36.622, -4.500)},
        "Sevilla": {"Sevilla": (37.389, -5.984), "Dos Hermanas": (37.283, -5.921), "Alcalá de Guadaíra": (37.338, -5.839), "Utrera": (37.185, -5.781), "Écija": (37.542, -5.083)},
    },
    "Aragón": {
        "Huesca": {"Huesca": (42.140, -0.408), "Monzón": (41.910, 0.193), "Barbastro": (42.035, 0.126), "Jaca": (42.570, -0.549)},
        "Teruel": {"Teruel": (40.344, -1.106), "Alcañiz": (41.051, -0.133), "Andorra": (40.977, -0.447)},
        "Zaragoza": {"Zaragoza": (41.649, -0.889), "Calatayud": (41.353, -1.643), "Utebo": (41.714, -0.994), "Ejea de los Caballeros": (42.126, -1.137)},
    },
    "Principado de Asturias": {
        "Asturias": {"Oviedo": (43.362, -5.849), "Gijón": (43.532, -5.661), "Avilés": (43.556, -5.925), "Siero": (43.391, -5.663), "Langreo": (43.302, -5.692), "Mieres": (43.250, -5.776)},
    },
    "Illes Balears": {
        "Illes Balears": {"Palma": (39.570, 2.650), "Calvià": (39.565, 2.506), "Ibiza": (38.909, 1.432), "Manacor": (39.570, 3.209), "Maó": (39.889, 4.265), "Inca": (39.721, 2.911)},
    },
    "Canarias": {
        "Las Palmas": {"Las Palmas de Gran Canaria": (28.124, -15.430), "Telde": (27.994, -15.419), "Arrecife": (28.963, -13.548), "Puerto del Rosario": (28.500, -13.862), "San Bartolomé de Tirajana": (27.925, -15.573)},
        "Santa Cruz de Tenerife": {"Santa Cruz de Tenerife": (28.464, -16.252), "San Cristóbal de La Laguna": (28.488, -16.316), "Arona": (28.100, -16.681), "Adeje": (28.122, -16.726), "Puerto de la Cruz": (28.414, -16.548)},
    },
    "Cantabria": {
        "Cantabria": {"Santander": (43.462, -3.810), "Torrelavega": (43.349, -4.048), "Castro Urdiales": (43.385, -3.220), "Camargo": (43.408, -3.869)},
    },
    "Castilla-La Mancha": {
        "Albacete": {"Albacete": (38.994, -1.856), "Hellín": (38.510, -1.701), "Villarrobledo": (39.268, -2.601), "Almansa": (38.869, -1.097)},
        "Ciudad Real": {"Ciudad Real": (38.985, -3.927), "Puertollano": (38.687, -4.107), "Tomelloso": (39.157, -3.024), "Valdepeñas": (38.762, -3.385)},
        "Cuenca": {"Cuenca": (40.070, -2.137), "Tarancón": (40.009, -3.007), "San Clemente": (39.404, -2.429)},
        "Guadalajara": {"Guadalajara": (40.633, -3.167), "Azuqueca de Henares": (40.565, -3.267), "Alovera": (40.592, -3.248)},
        "Toledo": {"Toledo": (39.863, -4.027), "Talavera de la Reina": (39.963, -4.831), "Illescas": (40.123, -3.847), "Seseña": (40.104, -3.698)},
    },
    "Castilla y León": {
        "Ávila": {"Ávila": (40.656, -4.700), "Arévalo": (41.063, -4.718)},
        "Burgos": {"Burgos": (42.344, -3.697), "Miranda de Ebro": (42.686, -2.947), "Aranda de Duero": (41.670, -3.689)},
        "León": {"León": (42.599, -5.567), "Ponferrada": (42.546, -6.597), "San Andrés del Rabanedo": (42.612, -5.609)},
        "Palencia": {"Palencia": (42.010, -4.528), "Guardo": (42.789, -4.843), "Aguilar de Campoo": (42.795, -4.260)},
        "Salamanca": {"Salamanca": (40.970, -5.663), "Béjar": (40.386, -5.763), "Ciudad Rodrigo": (40.600, -6.532)},
        "Segovia": {"Segovia": (40.948, -4.118), "Cuéllar": (41.402, -4.314), "El Espinar": (40.718, -4.248)},
        "Soria": {"Soria": (41.764, -2.465), "Almazán": (41.486, -2.531)},
        "Valladolid": {"Valladolid": (41.652, -4.724), "Medina del Campo": (41.312, -4.914), "Laguna de Duero": (41.583, -4.723)},
        "Zamora": {"Zamora": (41.503, -5.744), "Benavente": (42.002, -5.678), "Toro": (41.521, -5.395)},
    },
    "Cataluña": {
        "Barcelona": {"Barcelona": (41.388, 2.170), "L'Hospitalet de Llobregat": (41.360, 2.100), "Badalona": (41.450, 2.247), "Terrassa": (41.563, 2.008), "Sabadell": (41.548, 2.107), "Mataró": (41.540, 2.444), "Granollers": (41.608, 2.287)},
        "Girona": {"Girona": (41.979, 2.821), "Figueres": (42.267, 2.961), "Blanes": (41.674, 2.792), "Lloret de Mar": (41.700, 2.845)},
        "Lleida": {"Lleida": (41.617, 0.620), "Balaguer": (41.790, 0.810), "Tàrrega": (41.647, 1.140), "Mollerussa": (41.630, 0.894)},
        "Tarragona": {"Tarragona": (41.119, 1.245), "Reus": (41.155, 1.107), "Tortosa": (40.812, 0.521), "Cambrils": (41.067, 1.059), "Salou": (41.077, 1.131)},
    },
    "Comunitat Valenciana": {
        "Alicante": {"Alicante": (38.345, -0.481), "Elche": (38.267, -0.698), "Torrevieja": (37.978, -0.683), "Orihuela": (38.085, -0.944), "Benidorm": (38.538, -0.131), "Alcoy": (38.698, -0.474), "Elda": (38.478, -0.792), "San Vicente del Raspeig": (38.396, -0.525), "Dénia": (38.840, 0.106), "Santa Pola": (38.191, -0.566), "Villena": (38.637, -0.866)},
        "Castellón": {"Castellón de la Plana": (39.986, -0.051), "Vila-real": (39.938, -0.101), "Burriana": (39.889, -0.085), "Vinaròs": (40.470, 0.474), "Benicarló": (40.417, 0.427)},
        "Valencia": {"Valencia": (39.470, -0.376), "Torrent": (39.437, -0.465), "Gandia": (38.968, -0.181), "Paterna": (39.503, -0.440), "Sagunto": (39.680, -0.273), "Alzira": (39.151, -0.435), "Mislata": (39.475, -0.418)},
    },
    "Extremadura": {
        "Badajoz": {"Badajoz": (38.879, -6.970), "Mérida": (38.916, -6.344), "Don Benito": (38.956, -5.862), "Almendralejo": (38.683, -6.408), "Villanueva de la Serena": (38.976, -5.797)},
        "Cáceres": {"Cáceres": (39.475, -6.372), "Plasencia": (40.031, -6.088), "Navalmoral de la Mata": (39.892, -5.541), "Coria": (39.985, -6.536)},
    },
    "Galicia": {
        "A Coruña": {"A Coruña": (43.362, -8.411), "Santiago de Compostela": (42.878, -8.545), "Ferrol": (43.484, -8.233), "Narón": (43.503, -8.190), "Oleiros": (43.333, -8.316)},
        "Lugo": {"Lugo": (43.012, -7.556), "Monforte de Lemos": (42.522, -7.514), "Viveiro": (43.662, -7.594)},
        "Ourense": {"Ourense": (42.336, -7.864), "Verín": (41.941, -7.436), "O Barco de Valdeorras": (42.416, -6.982)},
        "Pontevedra": {"Vigo": (42.240, -8.721), "Pontevedra": (42.431, -8.645), "Vilagarcía de Arousa": (42.596, -8.766), "Redondela": (42.283, -8.610)},
    },
    "Comunidad de Madrid": {
        "Madrid": {"Madrid": (40.417, -3.704), "Móstoles": (40.322, -3.865), "Alcalá de Henares": (40.482, -3.364), "Fuenlabrada": (40.284, -3.794), "Leganés": (40.328, -3.764), "Getafe": (40.306, -3.733), "Alcorcón": (40.346, -3.825), "Torrejón de Ardoz": (40.456, -3.480), "Las Rozas de Madrid": (40.493, -3.874), "Alcobendas": (40.547, -3.642), "Pozuelo de Alarcón": (40.435, -3.814), "Majadahonda": (40.473, -3.872)},
    },
    "Región de Murcia": {
        "Murcia": {"Murcia": (37.992, -1.131), "Cartagena": (37.605, -0.986), "Lorca": (37.677, -1.700), "Molina de Segura": (38.054, -1.208), "Alcantarilla": (37.969, -1.217), "San Javier": (37.806, -0.837)},
    },
    "Comunidad Foral de Navarra": {
        "Navarra": {"Pamplona": (42.812, -1.646), "Tudela": (42.062, -1.604), "Barañáin": (42.806, -1.677), "Estella-Lizarra": (42.671, -2.032)},
    },
    "País Vasco": {
        "Álava": {"Vitoria-Gasteiz": (42.846, -2.672), "Llodio": (43.143, -2.962), "Amurrio": (43.053, -3.001)},
        "Gipuzkoa": {"San Sebastián": (43.318, -1.981), "Irun": (43.338, -1.789), "Errenteria": (43.312, -1.899), "Eibar": (43.184, -2.471)},
        "Bizkaia": {"Bilbao": (43.263, -2.935), "Barakaldo": (43.297, -2.989), "Getxo": (43.356, -3.011), "Portugalete": (43.321, -3.020), "Basauri": (43.236, -2.886)},
    },
    "La Rioja": {
        "La Rioja": {"Logroño": (42.465, -2.445), "Calahorra": (42.305, -1.965), "Arnedo": (42.228, -2.101), "Haro": (42.577, -2.847)},
    },
    "Ceuta (Ciudad Autónoma)": {
        "Ceuta": {"Ceuta": (35.889, -5.321)},
    },
    "Melilla (Ciudad Autónoma)": {
        "Melilla": {"Melilla": (35.292, -2.938)},
    },
}

# Ciudades usadas para anuncios de importación (OLX Portugal).
IMPORT_LOCATIONS: dict[str, tuple[float, float]] = {
    "Lisboa": (38.722, -9.139),
    "Porto": (41.158, -8.629),
    "Braga": (41.545, -8.426),
    "Faro": (37.019, -7.930),
    "Coimbra": (40.203, -8.410),
}
IMPORT_PROVINCE = "Importación · Portugal"
IMPORT_REGION = "Importación (OLX Portugal)"

ALL_OPTION = "Todas"

# ---------------------------------------------------------------------------
# 2. CONSTANTES DE FILTROS
# ---------------------------------------------------------------------------
FUELS: dict[str, str] = {
    "G": "Gasolina",
    "D": "Diésel",
    "H": "Híbrido (HEV)",
    "P": "Híbrido Enchufable (PHEV)",
    "E": "Eléctrico (EV)",
    "L": "GLP / GNC",
}
FUEL_OPTIONS = list(FUELS.values())

TRANSMISSIONS = ["Manual", "Automático"]

BODY_TYPES: dict[str, str] = {
    "BER": "Berlina",
    "FAM": "Familiar / Ranchera",
    "SUV": "SUV / 4x4",
    "MON": "Monovolumen",
    "COM": "Compacto",
    "COU": "Coupé",
    "FUR": "Furgoneta",
}
BODY_OPTIONS = list(BODY_TYPES.values())

SEAT_OPTIONS = ["Todas", 2, 4, 5, 6, 7, 8, 9]

DGT_LABELS = ["Todas", "Cero Emisiones", "ECO", "C", "B"]
DGT_COLORS = {
    "Cero Emisiones": "#1D4ED8",
    "ECO": "#15803D",
    "C": "#16A34A",
    "B": "#CA8A04",
    "Sin etiqueta": "#64748B",
}

SELLER_OPTIONS = ["Todos", "Particular", "Concesionario / Profesional"]
WARRANTY_OPTIONS = ["Todos", "Con garantía incluida"]
CONDITION_OPTIONS = ["Nuevo", "Km 0", "Seminuevo", "Segunda mano"]

COLORS = ["Blanco", "Negro", "Gris", "Plata", "Azul", "Rojo", "Granate", "Verde", "Beige", "Marrón", "Naranja"]

# ---------------------------------------------------------------------------
# 3. CATÁLOGO DE MARCAS / MODELOS
#    modelo: (carrocería, [plazas posibles], "códigos combustible", precio nuevo €)
# ---------------------------------------------------------------------------
CAR_CATALOG: dict[str, dict[str, tuple[str, list[int], str, int]]] = {
    "Alfa Romeo": {"Giulia": ("BER", [5], "G,D", 48000), "Stelvio": ("SUV", [5], "G,D", 55000), "Tonale": ("SUV", [5], "H,P,D", 37000)},
    "Audi": {"A1": ("COM", [5], "G", 23000), "A3": ("COM", [5], "G,D,P", 32000), "A4": ("BER", [5], "D,G", 45000), "A4 Avant": ("FAM", [5], "D,G", 47000), "A6": ("BER", [5], "D,P", 60000), "Q2": ("SUV", [5], "G,D", 30000), "Q3": ("SUV", [5], "G,D,P", 40000), "Q5": ("SUV", [5], "D,P,G", 55000), "Q7": ("SUV", [7], "D,P", 80000), "Q4 e-tron": ("SUV", [5], "E", 50000), "TT": ("COU", [2, 4], "G", 45000)},
    "BMW": {"Serie 1": ("COM", [5], "G,D", 33000), "Serie 2 Gran Tourer": ("MON", [5, 7], "D,G", 36000), "Serie 3": ("BER", [5], "G,D,P", 45000), "Serie 3 Touring": ("FAM", [5], "G,D,P", 47000), "Serie 4 Coupé": ("COU", [4], "G,D", 52000), "Serie 5": ("BER", [5], "D,P,G,E", 62000), "X1": ("SUV", [5], "G,D,P,E", 42000), "X3": ("SUV", [5], "D,G,P", 55000), "X5": ("SUV", [5, 7], "D,P", 80000), "i4": ("BER", [5], "E", 58000), "iX": ("SUV", [5], "E", 85000), "Z4": ("COU", [2], "G", 52000)},
    "BYD": {"Dolphin": ("COM", [5], "E", 30000), "Atto 3": ("SUV", [5], "E", 38000), "Seal": ("BER", [5], "E", 46000), "Seal U DM-i": ("SUV", [5], "P", 40000), "Tang": ("SUV", [7], "E", 72000)},
    "Citroën": {"C3": ("COM", [5], "G,D", 17000), "C4": ("COM", [5], "G,D,E", 25000), "C5 Aircross": ("SUV", [5], "G,D,P", 32000), "C4 Picasso": ("MON", [5, 7], "D,G", 26000), "Berlingo": ("FUR", [5, 7], "D,G,E", 25000), "SpaceTourer": ("MON", [8, 9], "D", 45000)},
    "CUPRA": {"Born": ("COM", [5], "E", 40000), "Formentor": ("SUV", [5], "G,P", 36000), "Leon": ("COM", [5], "G,P", 33000), "Tavascan": ("SUV", [5], "E", 47000)},
    "Dacia": {"Sandero": ("COM", [5], "G,L", 14000), "Duster": ("SUV", [5], "G,D,L,H", 20000), "Jogger": ("MON", [5, 7], "G,L,H", 19000), "Lodgy": ("MON", [5, 7], "D,G,L", 15000), "Spring": ("COM", [4], "E", 17000)},
    "Fiat": {"500": ("COM", [4], "G,H,E", 17000), "Panda": ("COM", [4, 5], "H,G", 14000), "Tipo": ("COM", [5], "G,D", 19000), "500X": ("SUV", [5], "G,D", 24000), "Doblò": ("FUR", [5, 7], "D,G", 23000), "Ducato": ("FUR", [2, 9], "D", 38000)},
    "Ford": {"Fiesta": ("COM", [5], "G,D", 17000), "Focus": ("COM", [5], "G,D", 24000), "Puma": ("SUV", [5], "G", 25000), "Kuga": ("SUV", [5], "G,D,P,H", 33000), "Mustang": ("COU", [4], "G", 55000), "Mustang Mach-E": ("SUV", [5], "E", 50000), "S-Max": ("MON", [7], "D,H", 40000), "Galaxy": ("MON", [7], "D,H", 42000), "Tourneo Custom": ("MON", [8, 9], "D,P", 48000), "Transit Connect": ("FUR", [2, 5], "D", 24000), "Ranger": ("SUV", [5], "D", 40000)},
    "Honda": {"Jazz": ("COM", [5], "H", 23000), "Civic": ("COM", [5], "H,G", 32000), "HR-V": ("SUV", [5], "H", 30000), "CR-V": ("SUV", [5, 7], "H,P", 45000)},
    "Hyundai": {"i10": ("COM", [4, 5], "G", 14000), "i20": ("COM", [5], "G", 18000), "i30": ("COM", [5], "G,D", 24000), "Kona": ("SUV", [5], "G,H,E", 26000), "Tucson": ("SUV", [5], "G,D,H,P", 33000), "Santa Fe": ("SUV", [7], "H,P,D", 50000), "IONIQ 5": ("SUV", [5], "E", 47000), "Staria": ("MON", [7, 9], "D", 45000)},
    "Jeep": {"Avenger": ("SUV", [5], "G,E", 25000), "Renegade": ("SUV", [5], "G,P,H", 28000), "Compass": ("SUV", [5], "G,D,P", 35000), "Wrangler": ("SUV", [4, 5], "G,P", 60000)},
    "Kia": {"Picanto": ("COM", [4, 5], "G", 13000), "Ceed": ("COM", [5], "G,D", 24000), "Niro": ("SUV", [5], "H,P,E", 31000), "Sportage": ("SUV", [5], "G,D,H,P", 33000), "Sorento": ("SUV", [7], "H,P,D", 48000), "Carens": ("MON", [7], "D,G", 24000), "EV6": ("SUV", [5], "E", 50000), "EV9": ("SUV", [6, 7], "E", 75000)},
    "Land Rover": {"Range Rover Evoque": ("SUV", [5], "D,P,H", 50000), "Discovery Sport": ("SUV", [5, 7], "D,P", 50000), "Discovery": ("SUV", [7], "D", 75000), "Defender": ("SUV", [5, 6, 7], "D,P,G", 75000)},
    "Lexus": {"UX": ("SUV", [5], "H,E", 38000), "NX": ("SUV", [5], "H,P", 55000), "RX": ("SUV", [5, 7], "H,P", 75000), "ES": ("BER", [5], "H", 50000)},
    "Mazda": {"Mazda2": ("COM", [5], "G,H", 18000), "Mazda3": ("COM", [5], "G", 26000), "CX-30": ("SUV", [5], "G", 29000), "CX-5": ("SUV", [5], "G,D", 34000), "CX-60": ("SUV", [5], "P,D", 50000), "CX-80": ("SUV", [6, 7], "P,D", 60000), "MX-5": ("COU", [2], "G", 32000)},
    "Mercedes-Benz": {"Clase A": ("COM", [5], "G,D,P", 35000), "Clase C": ("BER", [5], "D,G,P", 48000), "Clase C Estate": ("FAM", [5], "D,G,P", 50000), "Clase E": ("BER", [5], "D,P", 65000), "GLA": ("SUV", [5], "G,D,P", 42000), "GLB": ("SUV", [5, 7], "D,G", 45000), "GLC": ("SUV", [5], "D,P,G", 60000), "GLE": ("SUV", [5, 7], "D,P", 85000), "EQA": ("SUV", [5], "E", 50000), "Clase V": ("MON", [6, 7, 8], "D,E", 65000), "Vito": ("FUR", [2, 8, 9], "D", 40000), "SL": ("COU", [2, 4], "G", 120000)},
    "MG": {"MG3": ("COM", [5], "H", 19000), "MG4": ("COM", [5], "E", 31000), "ZS": ("SUV", [5], "G,E", 22000), "HS": ("SUV", [5], "G,P", 30000)},
    "Mini": {"Cooper": ("COM", [4], "G,E", 27000), "Countryman": ("SUV", [5], "G,D,P,E", 36000)},
    "Mitsubishi": {"Space Star": ("COM", [5], "G", 13000), "ASX": ("SUV", [5], "G,H", 25000), "Eclipse Cross": ("SUV", [5], "P,G", 34000), "Outlander": ("SUV", [5, 7], "P", 45000)},
    "Nissan": {"Micra": ("COM", [5], "G", 16000), "Leaf": ("COM", [5], "E", 33000), "Juke": ("SUV", [5], "G,H", 23000), "Qashqai": ("SUV", [5], "G,H", 31000), "X-Trail": ("SUV", [5, 7], "G,H", 40000), "Townstar": ("FUR", [5, 7], "G,E", 25000), "Navara": ("SUV", [5], "D", 38000)},
    "Opel": {"Corsa": ("COM", [5], "G,D,E", 18000), "Astra": ("COM", [5], "G,D,P", 25000), "Insignia": ("BER", [5], "D,G", 35000), "Mokka": ("SUV", [5], "G,E", 25000), "Grandland": ("SUV", [5], "G,D,P", 32000), "Combo Life": ("MON", [5, 7], "D,G", 25000), "Zafira": ("MON", [7, 8, 9], "D,G,E", 38000)},
    "Peugeot": {"208": ("COM", [5], "G,D,E", 19500), "308": ("COM", [5], "G,D,P", 27000), "308 SW": ("FAM", [5], "G,D,P", 29000), "508": ("BER", [5], "D,P,G", 42000), "2008": ("SUV", [5], "G,D,E", 24000), "3008": ("SUV", [5], "G,D,P,H", 36000), "5008": ("SUV", [7], "G,D,H", 40000), "Rifter": ("FUR", [5, 7], "D,G,E", 27000), "Traveller": ("MON", [8, 9], "D", 48000)},
    "Porsche": {"911": ("COU", [2, 4], "G", 130000), "Macan": ("SUV", [5], "G,E", 75000), "Cayenne": ("SUV", [5], "G,P", 95000), "Taycan": ("BER", [4, 5], "E", 100000)},
    "Renault": {"Clio": ("COM", [5], "G,D,H,L", 18500), "Zoe": ("COM", [5], "E", 30000), "Mégane": ("COM", [5], "G,D", 25000), "Captur": ("SUV", [5], "G,H,P,L", 23000), "Arkana": ("SUV", [5], "H,G", 30000), "Austral": ("SUV", [5], "G,H", 33000), "Espace": ("SUV", [5, 7], "H", 44000), "Scenic": ("MON", [5, 7], "D,G,E", 30000), "Kangoo": ("FUR", [2, 5, 7], "D,G,E", 24000), "Trafic": ("FUR", [2, 6, 8, 9], "D", 35000)},
    "SEAT": {"Ibiza": ("COM", [5], "G,L", 19000), "León": ("COM", [5], "G,D,P,L", 25000), "León Sportstourer": ("FAM", [5], "G,D,P", 27000), "Arona": ("SUV", [5], "G,L", 22000), "Ateca": ("SUV", [5], "G,D", 29000), "Tarraco": ("SUV", [5, 7], "G,D,P", 38000), "Alhambra": ("MON", [7], "G,D", 36000)},
    "Škoda": {"Fabia": ("COM", [5], "G", 18000), "Scala": ("COM", [5], "G", 22000), "Octavia": ("COM", [5], "G,D,P,L", 27000), "Octavia Combi": ("FAM", [5], "G,D,P", 29000), "Superb": ("FAM", [5], "D,G,P", 40000), "Kamiq": ("SUV", [5], "G", 24000), "Karoq": ("SUV", [5], "G,D", 30000), "Kodiaq": ("SUV", [5, 7], "G,D,P", 40000), "Enyaq": ("SUV", [5], "E", 45000)},
    "Smart": {"fortwo": ("COM", [2], "E", 22000), "#1": ("SUV", [5], "E", 40000)},
    "Suzuki": {"Ignis": ("COM", [5], "H", 16000), "Swift": ("COM", [5], "H", 17000), "Vitara": ("SUV", [5], "H", 25000), "S-Cross": ("SUV", [5], "H", 28000), "Jimny": ("SUV", [4], "G", 24000)},
    "Tesla": {"Model 3": ("BER", [5], "E", 42000), "Model Y": ("SUV", [5, 7], "E", 45000), "Model S": ("BER", [5], "E", 95000), "Model X": ("SUV", [5, 6, 7], "E", 105000)},
    "Toyota": {"Yaris": ("COM", [5], "H", 21000), "Corolla": ("COM", [5], "H", 28000), "Corolla Touring Sports": ("FAM", [5], "H", 30000), "C-HR": ("SUV", [5], "H,P", 31000), "RAV4": ("SUV", [5], "H,P", 39000), "bZ4X": ("SUV", [5], "E", 48000), "Land Cruiser": ("SUV", [5, 7], "D", 60000), "Prius+": ("MON", [7], "H", 30000), "Proace Verso": ("MON", [8, 9], "D,E", 46000)},
    "Volkswagen": {"Polo": ("COM", [5], "G", 20000), "Golf": ("COM", [5], "G,D,P,H", 29000), "ID.3": ("COM", [5], "E", 39000), "Passat": ("FAM", [5], "D,P,G", 42000), "T-Roc": ("SUV", [5], "G,D", 29000), "Tiguan": ("SUV", [5, 7], "G,D,P", 38000), "ID.4": ("SUV", [5], "E", 45000), "Touran": ("MON", [5, 7], "G,D", 33000), "Sharan": ("MON", [7], "D,G", 38000), "Multivan": ("MON", [6, 7], "D,P", 60000), "Caddy": ("FUR", [5, 7], "D,G,L", 28000), "Transporter": ("FUR", [2, 6, 8, 9], "D", 42000)},
    "Volvo": {"EX30": ("SUV", [5], "E", 37000), "XC40": ("SUV", [5], "G,P,E", 40000), "XC60": ("SUV", [5], "P,H,D", 55000), "XC90": ("SUV", [7], "P,H,D", 80000), "V60": ("FAM", [5], "P,D,H", 48000), "V90": ("FAM", [5], "D,P", 60000)},
}

PREMIUM_BRANDS = {"Audi", "BMW", "Mercedes-Benz", "Porsche", "Lexus", "Land Rover", "Volvo", "Tesla", "Alfa Romeo"}

# Versiones/acabados habituales para enriquecer títulos.
TRIM_LEVELS = ["Style", "Business", "Advance", "Sport", "Executive", "Premium", "Comfort", "Xcellence", "FR", "GT Line", "Titanium", "Allure", "Life", "Tekna", "Elegance", "Black Edition", "AMG Line", "M Sport", "S line"]


# ---------------------------------------------------------------------------
# 4. FUNCIONES AUXILIARES
# ---------------------------------------------------------------------------
def normalize_text(value: str) -> str:
    """Minúsculas sin acentos: 'Castellón' -> 'castellon'."""
    if value is None:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(value))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def slugify(value: str) -> str:
    """Slug para URLs de portales: 'Mercedes-Benz Clase C' -> 'mercedes-benz-clase-c'."""
    text = normalize_text(value)
    out, prev_dash = [], False
    for ch in text:
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")


def get_communities() -> list[str]:
    """Lista ordenada de Comunidades / Ciudades Autónomas."""
    return sorted(SPAIN_GEO.keys(), key=normalize_text)


def get_provinces(community: str | None = None) -> list[str]:
    """Provincias de una comunidad (o todas si community es None / 'Todas')."""
    if community and community != ALL_OPTION:
        return sorted(SPAIN_GEO.get(community, {}).keys(), key=normalize_text)
    return sorted((p for c in SPAIN_GEO.values() for p in c), key=normalize_text)


def get_cities(province: str | None = None, community: str | None = None) -> list[str]:
    """Municipios de una provincia; si no hay provincia, de la comunidad o de toda España."""
    if province and province != ALL_OPTION:
        for provs in SPAIN_GEO.values():
            if province in provs:
                return sorted(provs[province].keys(), key=normalize_text)
        return []
    cities = []
    for comm, provs in SPAIN_GEO.items():
        if community and community != ALL_OPTION and comm != community:
            continue
        for cs in provs.values():
            cities.extend(cs.keys())
    return sorted(set(cities), key=normalize_text)


@lru_cache(maxsize=1)
def city_index() -> dict[str, dict]:
    """Índice plano normalizado: nombre ciudad -> {ciudad, provincia, comunidad, lat, lon}."""
    index: dict[str, dict] = {}
    for comm, provs in SPAIN_GEO.items():
        for prov, cities in provs.items():
            for city, (lat, lon) in cities.items():
                index[normalize_text(city)] = {"ciudad": city, "provincia": prov, "comunidad": comm, "lat": lat, "lon": lon}
    for city, (lat, lon) in IMPORT_LOCATIONS.items():
        index.setdefault(normalize_text(city), {"ciudad": city, "provincia": IMPORT_PROVINCE, "comunidad": IMPORT_REGION, "lat": lat, "lon": lon})
    return index


@lru_cache(maxsize=1)
def province_index() -> dict[str, dict]:
    """Índice normalizado de provincias -> {provincia, comunidad, capital, lat, lon} (1ª ciudad listada)."""
    index = {}
    for comm, provs in SPAIN_GEO.items():
        for prov, cities in provs.items():
            capital, (lat, lon) = next(iter(cities.items()))
            index[normalize_text(prov)] = {"provincia": prov, "comunidad": comm, "capital": capital, "lat": lat, "lon": lon}
    return index


def resolve_location(text: str | None) -> dict | None:
    """Intenta mapear un texto libre de ubicación ('Elche (Alicante)', 'Madrid') a la jerarquía."""
    if not text:
        return None
    norm = normalize_text(text)
    cities, provinces = city_index(), province_index()
    if norm in cities:
        return dict(cities[norm])
    # Fragmentos separados por coma, paréntesis o guion
    for token in [t.strip() for t in norm.replace("(", ",").replace(")", ",").replace(" - ", ",").split(",")]:
        if token in cities:
            return dict(cities[token])
        if token in provinces:
            p = provinces[token]
            return {"ciudad": p["capital"], "provincia": p["provincia"], "comunidad": p["comunidad"], "lat": p["lat"], "lon": p["lon"]}
    # Búsqueda por contención
    for key, val in cities.items():
        if len(key) > 3 and key in norm:
            return dict(val)
    for key, p in provinces.items():
        if len(key) > 3 and key in norm:
            return {"ciudad": p["capital"], "provincia": p["provincia"], "comunidad": p["comunidad"], "lat": p["lat"], "lon": p["lon"]}
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia ortodrómica en km entre dos coordenadas."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def cities_within(city: str, radius_km: float) -> list[dict]:
    """Todos los municipios de la base situados a <= radius_km del municipio dado."""
    origin = city_index().get(normalize_text(city))
    if not origin:
        return []
    out = []
    for info in city_index().values():
        if info["comunidad"] == IMPORT_REGION:
            continue
        d = haversine_km(origin["lat"], origin["lon"], info["lat"], info["lon"])
        if d <= radius_km:
            out.append({**info, "distancia_km": round(d, 1)})
    return out


def get_brands() -> list[str]:
    return sorted(CAR_CATALOG.keys(), key=normalize_text)


def get_models(brand: str | None) -> list[str]:
    if not brand or brand == ALL_OPTION:
        return []
    return sorted(CAR_CATALOG.get(brand, {}).keys(), key=normalize_text)


def get_model_spec(brand: str | None, model: str | None) -> dict | None:
    """Ficha del catálogo para (marca, modelo), con coincidencia aproximada del modelo."""
    if not brand:
        return None
    brand_key = next((b for b in CAR_CATALOG if normalize_text(b) == normalize_text(brand)), None)
    if not brand_key:
        return None
    models = CAR_CATALOG[brand_key]
    model_key = None
    if model:
        nm = normalize_text(model)
        model_key = next((m for m in models if normalize_text(m) == nm), None)
        if not model_key:  # 'golf variant 2.0 tdi' -> 'Golf'
            candidates = [m for m in models if normalize_text(m) in nm or nm in normalize_text(m)]
            model_key = max(candidates, key=len) if candidates else None
    if not model_key:
        return None
    body, seats, fuels, price = models[model_key]
    return {
        "marca": brand_key,
        "modelo": model_key,
        "carroceria": BODY_TYPES[body],
        "plazas": seats,
        "combustibles": [FUELS[f] for f in fuels.split(",")],
        "precio_nuevo": price,
    }


def match_brand(text: str | None) -> str | None:
    """Detecta la marca del catálogo contenida en un texto libre."""
    if not text:
        return None
    norm = normalize_text(text)
    aliases = {"mercedes": "Mercedes-Benz", "vw": "Volkswagen", "skoda": "Škoda", "citroen": "Citroën", "land-rover": "Land Rover"}
    for brand in sorted(CAR_CATALOG, key=len, reverse=True):
        if normalize_text(brand) in norm:
            return brand
    for alias, brand in aliases.items():
        if alias in norm.split() or norm.startswith(alias):
            return brand
    return None


def normalize_fuel(text: str | None) -> str | None:
    """Mapea textos de portales ('diesel', 'Híbrido enchufable', 'electric'...) al vocabulario interno."""
    if not text:
        return None
    t = normalize_text(text)
    if any(k in t for k in ("enchufable", "plug", "phev")):
        return FUELS["P"]
    if any(k in t for k in ("electr", "bev", "ev ")) or t in ("ev", "e"):
        return FUELS["E"]
    if any(k in t for k in ("hibrid", "hybrid", "hev", "mhev")):
        return FUELS["H"]
    if any(k in t for k in ("glp", "gnc", "gas natural", "lpg", "cng", "autogas")):
        return FUELS["L"]
    if any(k in t for k in ("diesel", "gasoil", "gasoleo", "tdi", "hdi", "dci", "crdi")):
        return FUELS["D"]
    if any(k in t for k in ("gasolina", "petrol", "benzin", "tsi", "tfsi")):
        return FUELS["G"]
    return None


def normalize_body(text: str | None) -> str | None:
    if not text:
        return None
    t = normalize_text(text)
    rules = [
        (("suv", "4x4", "todoterreno", "crossover", "pick"), "SUV"),
        (("monovol", "minivan", "mpv"), "MON"),
        (("familiar", "ranchera", "touring", "estate", "avant", "sw", "combi", "break"), "FAM"),
        (("coupe", "cabrio", "descapotable", "roadster", "deportivo"), "COU"),
        (("furgon", "comercial", "van", "combi"), "FUR"),
        (("berlina", "sedan", "saloon"), "BER"),
        (("compacto", "utilitario", "hatchback", "pequeno"), "COM"),
    ]
    for keys, code in rules:
        if any(k in t for k in keys):
            return BODY_TYPES[code]
    return None


def compute_dgt_label(fuel: str | None, year: int | None) -> str:
    """
    Etiqueta ambiental DGT según combustible y año de matriculación
    (criterio general de la DGT para turismos).
    """
    if not fuel or not year:
        return "Sin etiqueta"
    if fuel in (FUELS["E"], FUELS["P"]):
        return "Cero Emisiones"
    if fuel in (FUELS["H"], FUELS["L"]):
        return "ECO"
    if fuel == FUELS["G"]:
        if year >= 2006:
            return "C"
        if year >= 2001:
            return "B"
        return "Sin etiqueta"
    if fuel == FUELS["D"]:
        if year >= 2015:
            return "C"
        if year >= 2006:
            return "B"
        return "Sin etiqueta"
    return "Sin etiqueta"


def geo_summary() -> dict[str, int]:
    """Recuento de la base geográfica (para el README / pie de página)."""
    regions = [c for c in SPAIN_GEO if "Ciudad Autónoma" not in c]
    provinces = [p for c in regions for p in SPAIN_GEO[c]]
    cities = sum(len(cs) for c in SPAIN_GEO.values() for cs in c.values())
    return {"comunidades": len(regions), "ciudades_autonomas": len(SPAIN_GEO) - len(regions), "provincias": len(provinces), "municipios": cities}
