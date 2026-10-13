#!/usr/bin/env python3

import json
import os
import random
import re
import socket
import sys
import time
import urllib.parse
import urllib.request

_getaddrinfo_original = socket.getaddrinfo


def _getaddrinfo_ipv4_primeiro(host, port, family=0, type=0, proto=0, flags=0):
    """Força resolução IPv4 quando possível. Evita o problema de
    'happy eyeballs' ausente no urllib: se a rota IPv6 estiver quebrada
    (comum em sandboxes/containers), o urllib tenta o primeiro endereço
    retornado (IPv6, nesta versão do resolver) e fica ~20-25s travado até
    expirar o timeout antes de cair para IPv4 — multiplicado por centenas
    de chamadas geo, isso sozinho explica timeouts de job no CI."""
    try:
        return _getaddrinfo_original(host, port, socket.AF_INET, type, proto, flags)
    except socket.gaierror:
        return _getaddrinfo_original(host, port, family, type, proto, flags)


socket.getaddrinfo = _getaddrinfo_ipv4_primeiro

DATA_DIR = "data"
CACHE_PATH = os.path.join(DATA_DIR, "_geocache.json")
GOOGLE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
OSM_URL = "https://nominatim.openstreetmap.org/search"
OSM_DELAY_S = 1.1
GEO_LIMITE_GOOGLE = int(os.environ.get("GEO_LIMITE_GOOGLE", "1500"))
GEO_LIMITE_OSM = int(os.environ.get("GEO_LIMITE_OSM", "2000"))
GOOGLE_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
LOG_CADA_N = int(os.environ.get("GEO_LOG_CADA_N", "50"))

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
_google_status_contagem = {}
_google_tempo_total = 0.0
_osm_tempo_total = 0.0


def log(msg):
    print(msg, flush=True)
    sys.stdout.flush()


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
        except Exception as e:
            log(f"[GEO] ⚠️ cache corrompido ({e}) — iniciando vazio")
            _cache = {}
    else:
        _cache = {}
    return _cache


def salvar_cache():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False, indent=0, sort_keys=True)


def geocode_google(im):
    """Retorna (lat, lng) em sucesso, ou None.
    Em falha, registra o motivo exato em _google_status_contagem
    (nunca falha silenciosa: HTTPError, URLError e status != OK
    são todos contabilizados por tipo)."""
    global _google_tempo_total
    if not GOOGLE_KEY:
        _google_status_contagem["SEM_CHAVE"] = _google_status_contagem.get("SEM_CHAVE", 0) + 1
        return None
    comp = "+".join(x for x in (
        (im.get("endereco") or ""), (im.get("bairro") or ""),
        (im.get("cidade") or ""), (im.get("uf") or ""), "Brasil"
    ) if x)
    url = GOOGLE_URL + "?address=" + urllib.parse.quote(comp) + "&key=" + GOOGLE_KEY
    t0 = time.time()
    try:
        data = _get_json(url)
    except Exception as e:
        _google_tempo_total += time.time() - t0
        chave = f"EXCEPTION:{type(e).__name__}"
        _google_status_contagem[chave] = _google_status_contagem.get(chave, 0) + 1
        return None
    _google_tempo_total += time.time() - t0
    status = data.get("status", "SEM_STATUS")
    _google_status_contagem[status] = _google_status_contagem.get(status, 0) + 1
    if status == "OK" and data.get("results"):
        loc = data["results"][0]["geometry"]["location"]
        return float(loc["lat"]), float(loc["lng"])
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


def enriquecer(imoveis, rotulo=""):
    global _google_hoje, _osm_hoje, _ultima_osm, _osm_tempo_total
    cache = carregar_cache()
    stats = {"novas_google": 0, "novas_osm": 0, "do_cache": 0,
             "pendentes": 0, "sem_endereco": 0}
    total = len(imoveis)
    t_inicio = time.time()
    prefixo = f"[GEO{'/' + rotulo if rotulo else ''}]"
    log(f"{prefixo} início: {total} imóveis | limites hoje: "
        f"Google {_google_hoje}/{GEO_LIMITE_GOOGLE}, OSM {_osm_hoje}/{GEO_LIMITE_OSM}")

    for i, im in enumerate(imoveis, 1):
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
                if i % LOG_CADA_N == 0 or i == total:
                    log(f"{prefixo} {i}/{total} | cache={stats['do_cache']} "
                        f"google={stats['novas_google']} osm={stats['novas_osm']} "
                        f"pend={stats['pendentes']} | {time.time()-t_inicio:.0f}s "
                        f"| google_status={_google_status_contagem}")
                continue

        if r is None and _osm_hoje < GEO_LIMITE_OSM:
            if _osm_hoje == 0:
                log(f"{prefixo} ⚠️ entrando no fallback OSM (Google esgotado/falhou "
                    f"para este item) — status Google até agora: {_google_status_contagem}")
            espera = OSM_DELAY_S - (time.time() - _ultima_osm)
            if espera > 0:
                time.sleep(espera)
            _ultima_osm = time.time()
            t0 = time.time()
            r = geocode_osm(im)
            _osm_tempo_total += time.time() - t0
            _osm_hoje += 1
            if r:
                cache[k] = {"lat": r[0], "lng": r[1], "fonte": "osm"}
                im["geo"] = cache[k]
                im["geo_pendente"] = False
                stats["novas_osm"] += 1
                if i % LOG_CADA_N == 0 or i == total:
                    log(f"{prefixo} {i}/{total} | cache={stats['do_cache']} "
                        f"google={stats['novas_google']} osm={stats['novas_osm']} "
                        f"pend={stats['pendentes']} | {time.time()-t_inicio:.0f}s")
                continue

        im["geo"] = None
        im["geo_pendente"] = True
        stats["pendentes"] += 1
        if i % LOG_CADA_N == 0 or i == total:
            log(f"{prefixo} {i}/{total} | cache={stats['do_cache']} "
                f"google={stats['novas_google']} osm={stats['novas_osm']} "
                f"pend={stats['pendentes']} | {time.time()-t_inicio:.0f}s")

    salvar_cache()
    dur = time.time() - t_inicio
    log(f"{prefixo} fim: {stats} | {dur:.0f}s total "
        f"(google: {_google_tempo_total:.1f}s em {_google_hoje} chamadas, "
        f"osm: {_osm_tempo_total:.1f}s em {_osm_hoje} chamadas) "
        f"| status Google observados: {_google_status_contagem}")
    return stats