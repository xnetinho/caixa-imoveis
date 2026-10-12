#!/usr/bin/env python3
"""
geo.py — Enriquecimento de coordenadas (geocoding) para o pipeline Caixa.

Estratégia de custo (Google Geocoding custa US$5/1.000, free tier 10k/mês):
  1. CACHE PERSISTENTE (data/_geocache.json): endereço normalizado -> {lat,lng}.
     Imóveis repetidos ou que voltam ao CSV NÃO geram novas consultas.
  2. OSM/Nominatim PRIMEIRO (gratuito, 1 req/s com User-Agent próprio).
  3. GOOGLE SÓ COMO FALLBACK, com limite diário (GEO_LIMITE_GOOGLE, default 1500)
     — estourou, os pendentes ficam para a próxima varredura.

Nada de chave no código: GOOGLE_MAPS_API_KEY entra via env (secret do repo).
O JSON publicado contém APENAS lat/lng/fonte — nunca a chave.
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request

DATA_DIR = "data"
CACHE_PATH = os.path.join(DATA_DIR, "_geocache.json")
OSM_URL = "https://nominatim.openstreetmap.org/search"
GOOGLE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
OSM_DELAY_S = 1.05          # política Nominatim: máx 1 req/s
GEO_LIMITE_GOOGLE = int(os.environ.get("GEO_LIMITE_GOOGLE", "1500"))
GOOGLE_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")  # via secret, nunca commitado
UA = "caixa-imoveis-pipeline/1.0 (github.com/xnetinho/caixa-imoveis)"

_cache = None


# ── chave de cache: normaliza o endereço ──────────────────────────────────────

def chave_geo(im: dict) -> str:
    """Chave de cache a partir do imóvel: UF|cidade|bairro|endereço normalizado.
    Imóveis distintos no mesmo endereço compartilham a coordenada."""
    def n(s):
        s = (s or "").upper().strip()
        s = re.sub(r"\s+", " ", s)
        # remove sufixos de lote/quadra com "S/N", "SN", "S. N." (variável demais)
        s = re.sub(r"[.,]?\s*(S\s*/?\s*N|SN)\b", "", s)
        return s.strip(" ,.")
    return "|".join(n(im.get(k)) for k in ("uf", "cidade", "bairro", "endereco"))


def carregar_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    else:
        _cache = {}
    return _cache


def salvar_cache() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False, indent=0, sort_keys=True)


# ── provedores ────────────────────────────────────────────────────────────────

def _get_json(url: str, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _norm_acentos(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", (s or "")) \
        .encode("ascii", "ignore").decode()


UF_NOME = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapa", "AM": "Amazonas",
    "BA": "Bahia", "CE": "Ceara", "DF": "Distrito Federal", "ES": "Espirito Santo",
    "GO": "Goias", "MA": "Maranhao", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul",
    "MG": "Minas Gerais", "PA": "Para", "PB": "Paraiba", "PR": "Parana",
    "PE": "Pernambuco", "PI": "Piaui", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
    "RS": "Rio Grande do Sul", "RO": "Rondonia", "RR": "Roraima",
    "SC": "Santa Catarina", "SP": "Sao Paulo", "SE": "Sergipe", "TO": "Tocantins",
}


def _limpar_endereco(s: str) -> str:
    """Limpa o endereço p/ o Nominatim: mantém ACENTOS (obrigatório p/ cidades
    brasileiras), remove 'N. 123'/'N. S/N' e espaços duplicados."""
    s = re.sub(r"[,.]?\s*N[.°º]?\s*(S\s*/?\s*N|SN)\b", "", (s or ""), flags=re.I)
    s = re.sub(r",?\s*N[.°º]\s*\d+.*$", "", s)   # número no fim
    s = re.sub(r"\s+", " ", s).strip(" ,.")
    return s


def _query_osm(endereco, bairro, cidade, estado):
    partes = [p for p in (endereco, bairro, cidade, estado, "Brasil") if p]
    q = ", ".join(partes)
    url = OSM_URL + "?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 1, "countrycodes": "br"})
    try:
        data = _get_json(url)
        if data and data[0].get("lat"):
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


def geocode_osm(im: dict):
    """Nominatim com degraus de fallback (comprovado em testes):
      1. endereco + bairro + cidade + estado
      2. endereco + cidade + estado      (bairro muitas vezes quebra o match)
      3. endereco + estado
    Retorna (lat,lng) ou None."""
    endereco = _limpar_endereco(im.get("endereco") or "")
    bairro = im.get("bairro") or ""
    cidade = im.get("cidade") or ""
    estado = UF_NOME.get((im.get("uf") or "").strip().upper(), "Brasil")
    if not endereco:
        return None
    for b in (bairro, "", None):
        r = _query_osm(endereco, b or "", cidade, estado)
        if r:
            return r
    return None


def geocode_google(im: dict):
    """Google Geocoding API (chave via env). Retorna (lat,lng) ou None."""
    if not GOOGLE_KEY:
        return None
    comp = "+".join(x for x in (
        (im.get("endereco") or ""), (im.get("bairro") or ""),
        (im.get("cidade") or ""), (im.get("uf") or ""), "Brasil"
    ) if x)
    url = GOOGLE_URL + "?address=" + urllib.parse.quote(comp) + "&key=" + GOOGLE_KEY
    try:
        data = _get_json(url)
        if data.get("status") == "OK" and data.get("results"):
            loc = data["results"][0]["geometry"]["location"]
            return float(loc["lat"]), float(loc["lng"])
    except Exception:
        pass
    return None


# ── API principal ─────────────────────────────────────────────────────────────

_contador = {"osm": 0, "google": 0, "cache": 0}
_google_hoje = 0
_ultima_osm = 0.0


def enriquecer(imoveis: list) -> dict:
    """Adiciona 'geo' a cada imóvel (mutando). Retorna estatísticas.
    geo = {lat, lng, fonte: 'osm'|'google'|'cache'} ou geo_pendente=True."""
    global _google_hoje, _ultima_osm
    cache = carregar_cache()
    stats = {"novas_osm": 0, "novas_google": 0, "do_cache": 0,
             "pendentes": 0, "sem_endereco": 0}

    for im in imoveis:
        if not (im.get("endereco") and im.get("cidade")):
            im["geo"] = None
            im["geo_pendente"] = False
            stats["sem_endereco"] += 1
            continue

        k = chave_geo(im)
        hit = cache.get(k)
        if hit:
            im["geo"] = hit
            im["geo_pendente"] = False
            stats["do_cache"] += 1
            continue

        # respeita 1 req/s do Nominatim
        espera = OSM_DELAY_S - (time.time() - _ultima_osm)
        if espera > 0:
            time.sleep(espera)

        # 1ª tentativa: OSM
        _ultima_osm = time.time()
        r = geocode_osm(im)
        if r:
            cache[k] = {"lat": r[0], "lng": r[1], "fonte": "osm"}
            im["geo"] = cache[k]
            im["geo_pendente"] = False
            stats["novas_osm"] += 1
            continue

        # 2ª tentativa: Google (com limite diário)
        if GOOGLE_KEY and _google_hoje < GEO_LIMITE_GOOGLE:
            r = geocode_google(im)
            _google_hoje += 1
            if r:
                cache[k] = {"lat": r[0], "lng": r[1], "fonte": "google"}
                im["geo"] = cache[k]
                im["geo_pendente"] = False
                stats["novas_google"] += 1
                continue

        # não achou / sem orçamento — fica pendente p/ próxima varredura
        im["geo"] = None
        im["geo_pendente"] = True
        stats["pendentes"] += 1

    salvar_cache()
    return stats