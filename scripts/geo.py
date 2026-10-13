#!/usr/bin/env python3

import json
import os
import random
import re
import time
import unicodedata
import urllib.parse
import urllib.request

DATA_DIR = "data"
CACHE_PATH = os.path.join(DATA_DIR, "_geocache.json")
GOOGLE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
OSM_URL = "https://nominatim.openstreetmap.org/search"
OSM_DELAY_S = 1.1
GEO_LIMITE_GOOGLE = int(os.environ.get("GEO_LIMITE_GOOGLE", "1500"))
GEO_LIMITE_OSM = int(os.environ.get("GEO_LIMITE_OSM", "2000"))
GOOGLE_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

UA_POOL = [
    "Mozilla/5.0 (Linux; Android 10; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; SM-A528B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; Redmi Note 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; moto g(60)) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; LM-Q730) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; M2012K11AC) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; SM-A325F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
]

UF_NOME = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapa", "AM": "Amazonas",
    "BA": "Bahia", "CE": "Ceara", "DF": "Distrito Federal", "ES": "Espirito Santo",
    "GO": "Goias", "MA": "Maranhao", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul",
    "MG": "Minas Gerais", "PA": "Para", "PB": "Paraiba", "PR": "Parana",
    "PE": "Pernambuco", "PI": "Piaui", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
    "RS": "Rio Grande do Sul", "RO": "Rondonia", "RR": "Roraima",
    "SC": "Santa Catarina", "SP": "Sao Paulo", "SE": "Sergipe", "TO": "Tocantins",
}

_cache = None
_google_hoje = 0
_osm_hoje = 0
_ultima_osm = 0.0


def _ua_aleatorio():
    return random.choice(UA_POOL)


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": _ua_aleatorio()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def chave_geo(im):
    def n(s):
        s = (s or "").upper().strip()
        s = re.sub(r"\s+", " ", s)
        s = re.sub(r"[.,]?\s*(S\s*/?\s*N|SN)\b", "", s)
        return s.strip(" ,.")
    return "|".join(n(im.get(k)) for k in ("uf", "cidade", "bairro", "endereco"))


def _limpar_endereco(s):
    s = re.sub(r"[,.]?\s*N[.°º]?\s*(S\s*/?\s*N|SN)\b", "", (s or ""), flags=re.I)
    s = re.sub(r",?\s*N[.°º]\s*\d+\s*,?\s*(Apto|Ap|BL|Lote|Lt|Qd|Casa|Sobrado|Fundos|Terreo|Térreo|Cj|Sl|Jo)\b.*$", "", s, flags=re.I)
    s = re.sub(r",?\s*N[.°º]\s*\d+.*$", "", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.")
    return s


def carregar_cache():
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


def salvar_cache():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False, indent=0, sort_keys=True)


def geocode_google(im):
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


def geocode_osm(im):
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


def enriquecer(imoveis):
    global _google_hoje, _osm_hoje, _ultima_osm
    cache = carregar_cache()
    stats = {"novas_google": 0, "novas_osm": 0, "do_cache": 0,
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

        r = None
        if GOOGLE_KEY and _google_hoje < GEO_LIMITE_GOOGLE:
            r = geocode_google(im)
            _google_hoje += 1
            if r:
                cache[k] = {"lat": r[0], "lng": r[1], "fonte": "google"}
                im["geo"] = cache[k]
                im["geo_pendente"] = False
                stats["novas_google"] += 1
                continue

        if r is None and _osm_hoje < GEO_LIMITE_OSM:
            espera = OSM_DELAY_S - (time.time() - _ultima_osm)
            if espera > 0:
                time.sleep(espera)
            _ultima_osm = time.time()
            r = geocode_osm(im)
            _osm_hoje += 1
            if r:
                cache[k] = {"lat": r[0], "lng": r[1], "fonte": "osm"}
                im["geo"] = cache[k]
                im["geo_pendente"] = False
                stats["novas_osm"] += 1
                continue

        im["geo"] = None
        im["geo_pendente"] = True
        stats["pendentes"] += 1

    salvar_cache()
    return stats