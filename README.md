# 🚗 CochesRadar España — Buscador Inteligente de Vehículos

Painel agregador (Streamlit) de anúncios de veículos **novos, km 0, seminovos e de segunda mão** à venda na Espanha.
Ele consolida 13 portais e classificados, oferece filtros granulares (lugares/plazas, etiqueta ambiental DGT,
localização hierárquica com raio em km, faixa de preço…), análise comparativa de mercado, comparador de favoritos e
exportação para Excel/CSV.

---

## ✨ Funcionalidades

| Área | O que faz |
|---|---|
| **Fontes** | Coches.net, AutoScout24.es, Coches.com, Autocasión, Motor.es, Flexicar, Clicars, OcasionPlus, Spoticar, Das WeltAuto, Milanuncios, Wallapop (API) e OLX (importação PT). Seleção múltipla na sidebar. |
| **Localização** | Cascata Comunidad Autónoma → Provincia → Municipio (17 CCAA + Ceuta e Melilla, 50 províncias, 224 municípios com coordenadas) + raio de distância (km), que inclui cidades vizinhas de outras províncias. |
| **Veículo** | Marca / modelo (33 marcas, 215 modelos), busca livre, estado (Nuevo, Km 0, Seminuevo, Segunda mano), ano, preço e km mín./máx. |
| **Família e técnica** | Plazas (2, 4, 5, 6, 7, 8, 9), combustível (Gasolina, Diésel, HEV, PHEV, EV, GLP/GNC), câmbio, carroceria, etiqueta DGT (Cero, ECO, C, B), tipo de vendedor, garantia. |
| **KPIs** | Total, preço médio, preço mínimo, km médio e ano médio. |
| **Resultados** | Grade de cards (foto, preço, ano, km, combustível, lugares, cidade, badge do portal, etiqueta DGT, botão *Ver Anuncio Original*) ou tabela interativa; ordenação por menor preço, menor km, mais recente, melhor oportunidade ou mais próximo. |
| **Análise de mercado** | Gráficos Plotly: distribuição de preços por portal, preço médio por ano, preço × km com linha de tendência e destaque de **oportunidades** (≥ 15 % abaixo do valor estimado), preço por combustível e oferta por etiqueta DGT. |
| **Comparador** | Até 4 favoritos lado a lado numa matriz técnica, com o melhor valor de cada linha destacado. |
| **Exportação** | Excel `.xlsx` formatado (cabeçalho corporativo, links clicáveis, formato €, filtros, aba *Resumen*) e CSV (`;` e vírgula decimal, compatível com o Excel em espanhol). |

---

## 🗂️ Estrutura

```
cochesradar/
├── app.py                         # Interface Streamlit (sidebar, KPIs, abas, exportação)
├── scraper_engine.py              # Conectores, coleta assíncrona, normalização, fallback, filtros, cache
├── spain_geo_cars.py              # Geografia da Espanha + catálogo de marcas/modelos + regras DGT
├── styles.css                     # Estilo executivo (carmesim #AA151B, ouro #F1BF00/#D4A017)
├── requirements.txt
├── Dockerfile                     # Deploy em Docker / Railway / Render
├── .streamlit/
│   ├── config.toml                # Tema harmonizado com a paleta
│   └── secrets.toml.example       # Modelo de chaves opcionais (proxies)
└── README.md
```

---

## ▶️ Rodar localmente

Requisitos: **Python 3.10+**.

```bash
git clone https://github.com/<seu-usuario>/cochesradar.git
```

```bash
cd cochesradar
```

```bash
python -m venv .venv && source .venv/bin/activate
```

> No Windows: `.venv\Scripts\activate`

```bash
pip install -r requirements.txt
```

```bash
streamlit run app.py
```

Abra http://localhost:8501.

Para testar sem fazer requisições aos portais (apresentações, desenvolvimento):

```bash
COCHESRADAR_MODE=demo streamlit run app.py
```

---

## ☁️ Publicar no Streamlit Community Cloud (grátis)

1. Crie um repositório no GitHub e envie **todos os arquivos** desta pasta (incluindo a pasta `.streamlit/`).
   ```bash
   git init && git add . && git commit -m "CochesRadar España"
   ```
   ```bash
   git branch -M main && git remote add origin https://github.com/<seu-usuario>/cochesradar.git && git push -u origin main
   ```
2. Acesse **https://share.streamlit.io** e entre com sua conta do GitHub.
3. Clique em **Create app → Deploy a public app from GitHub**.
4. Selecione o repositório, o branch `main` e o arquivo principal **`app.py`**.
5. (Opcional) Em **Advanced settings → Secrets**, cole as chaves de proxy (veja abaixo).
6. Clique em **Deploy**. Em 2–3 minutos o app estará on-line em `https://<nome>.streamlit.app`.

### 🐳 Docker / Railway / Render

```bash
docker build -t cochesradar .
```

```bash
docker run -p 8501:8501 -e COCHESRADAR_MODE=auto cochesradar
```

No **Railway**: *New Project → Deploy from GitHub repo*. O `Dockerfile` é detectado automaticamente e a porta `$PORT`
é respeitada. Adicione as variáveis de ambiente na aba *Variables*.

---

## 🔐 Proxies anti-bloqueio e variáveis de ambiente

Os grandes portais espanhóis usam proteções anti-bot (DataDome, Cloudflare, Akamai) que **costumam bloquear IPs de
datacenters** como os do Streamlit Cloud. O motor detecta o bloqueio (HTTP 403/429, captcha…) e, conforme o modo,
mostra dados de referência. Para maximizar os dados reais, configure um provedor (variável de ambiente **ou**
`st.secrets`):

| Variável | Uso |
|---|---|
| `ZENROWS_API_KEY` | Usa a API ZenRows (proxy residencial ES + renderização JS). Recomendado. |
| `SCRAPERAPI_KEY` | Alternativa: ScraperAPI (`country_code=es`, render). |
| `PROXY_URL` | Proxy HTTP(S) genérico, ex. `http://user:pass@host:port`. |
| `COCHESRADAR_MODE` | `auto` (padrão), `real` ou `demo`. Também pode ser alterado na sidebar. |
| `COCHESRADAR_TIMEOUT` | Timeout por requisição em segundos (padrão `12`). |
| `COCHESRADAR_CONCURRENCY` | Requisições simultâneas (padrão `6`). |

Exemplo de `.streamlit/secrets.toml` (nunca suba este arquivo para o GitHub; ele já está no `.gitignore`):

```toml
ZENROWS_API_KEY = "sua_chave"
```

---

## ⚙️ Como funciona a coleta

1. **URLs de busca por portal** são montadas a partir dos filtros (marca/modelo em *slug*, preço, ano, km…).
2. **Requisições paralelas** (`httpx.AsyncClient` + `asyncio.gather`, semáforo de concorrência), com **rotação de
   User-Agent** (`fake-useragent` + lista de reserva), cabeçalhos `es-ES`, re-tentativa com *backoff* e detecção de bloqueio.
3. **Extração genérica e resiliente** a mudanças de layout, nesta ordem: JSON da API (Wallapop) → JSON-LD
   schema.org (`Car`, `Product`, `Offer`, `ItemList`) → `__NEXT_DATA__` → estados embutidos
   (`window.__INITIAL_STATE__`, `__PRELOADED_STATE__`…) → cards HTML com preço em €.
4. **Normalização** para um esquema único (preço, ano, km, combustível, câmbio, lugares, carroceria, localização
   mapeada para a hierarquia CCAA/Província/Município, vendedor, garantia). Os campos ausentes são inferidos pelo
   catálogo, e a **etiqueta DGT** é calculada pelo combustível e ano de matrícula.
5. **Fallback inteligente** (modo `auto`): se um portal bloqueia ou não devolve dados estruturados, são gerados
   anúncios de **referência** realistas e determinísticos, coerentes com todos os filtros (lançamento de cada modelo,
   motores por marca, depreciação, perfil de vendedor do portal). Eles aparecem marcados como **"Referencia"**, e o
   botão *Ver Anuncio Original* abre a **busca real equivalente** no portal. O filtro *Solo anuncios reales* oculta esses anúncios.
6. **Filtros unificados** aplicados a todos os anúncios. O **valor de mercado** é estimado por regressão log-linear
   (preço ~ idade + log km, por modelo, ou global com preço de catálogo e eletrificação).
7. **Cache** `@st.cache_data` de 30 minutos por combinação de filtros.

---

## ⚖️ Aviso legal

Os anúncios, preços e marcas pertencem aos seus respectivos portais. Respeite os Termos de Uso e o `robots.txt` de cada
site, use frequências de consulta moderadas e verifique sempre as informações no anúncio original antes de qualquer
transação. Os dados de **referência** são estimativas ilustrativas e não correspondem a veículos reais à venda.
