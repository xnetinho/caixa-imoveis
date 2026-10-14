#!/usr/bin/env python3

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geo
import pipeline

DATA_DIR = "data"
UF_LISTA = ["AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT",
            "MS", "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO",
            "RR", "SC", "SP", "SE", "TO"]


def log(msg):
    print(msg, flush=True)


def carregar(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def salvar(p, obj):
    # escrita atômica: evita arquivo truncado se o processo for interrompido
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def regravar_toon(p, doc):
    chaves = doc.get("colunas") or sorted(doc["imoveis"][0].keys())
    # global (BR) não tem data_geracao_caixa — usa atualizado_em
    data = doc.get("data_geracao_caixa") or doc.get("atualizado_em", "")
    with open(p, "w", encoding="utf-8") as f:
        f.write(pipeline.gerar_toon(doc["uf"], doc["imoveis"], chaves,
                                    data, doc["fonte"]))


def _env_int(nome, default):
    v = (os.environ.get(nome) or "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        return default


def main():
    t_total = time.time()
    filtro = os.environ.get("GEO_FILTRO_UF", "").strip().upper()
    geo.GEO_LIMITE_GOOGLE = _env_int("GEO_LIMITE_GOOGLE", 800)
    geo.GEO_LIMITE_OSM = _env_int("GEO_LIMITE_OSM", 20000)
    log(f"[FASE 1/5] carregamento | filtro_uf={filtro or '(todas)'} | "
        f"chave_google={'presente' if geo.GOOGLE_KEY else 'AUSENTE'} | "
        f"limites: Google={geo.GEO_LIMITE_GOOGLE} OSM={geo.GEO_LIMITE_OSM}")
    t0 = time.time()

    docs = []
    for uf in UF_LISTA:
        if filtro and uf != filtro:
            continue
        p = os.path.join(DATA_DIR, uf, "imoveis.json")
        if os.path.exists(p):
            docs.append((uf, carregar(p)))
        else:
            log(f"[{uf}] sem imoveis.json")
    log(f"[FASE 1/5] fim: {len(docs)} UFs carregadas | {time.time()-t0:.1f}s")

    log("[FASE 2/5] montagem da fila (dedupe por endereço, aplica cache existente)")
    t0 = time.time()
    fila, vistos = [], set()
    cache_inicial = geo.carregar_cache()
    for _, doc in docs:
        for im in doc["imoveis"]:
            k = geo.chave_geo(im)
            if k in vistos:
                continue
            tem_geo = isinstance(im.get("geo"), dict) and im["geo"].get("lat")
            if tem_geo:
                cache_inicial[k] = im["geo"]
                continue
            if im.get("geo_pendente") is False:
                continue
            if im.get("endereco") and im.get("cidade"):
                vistos.add(k)
                fila.append(im)
    log(f"[FASE 2/5] fim: {len(fila)} endereços únicos pendentes | "
        f"cache atual: {len(cache_inicial)} entradas | {time.time()-t0:.1f}s")

    log(f"[FASE 3/5] enriquecimento (Google prioritário, Nominatim fallback)")
    t0 = time.time()
    if fila:
        stats_total = geo.enriquecer(fila, rotulo=filtro or "TODAS")
    else:
        stats_total = {"novas_google": 0, "novas_osm": 0, "do_cache": 0,
                        "pendentes": 0, "sem_endereco": 0}
        log("[FASE 3/5] fila vazia — nada a enriquecer")
    log(f"[FASE 3/5] fim: {stats_total} | {time.time()-t0:.1f}s")

    log("[FASE 4/5] regravação por UF")
    t0 = time.time()
    cache = geo.carregar_cache()
    chaves_processadas = {geo.chave_geo(im) for im in fila}
    ufs_regravadas = 0
    for uf, doc in docs:
        mudou = False
        for im in doc["imoveis"]:
            k = geo.chave_geo(im)
            g = cache.get(k)
            if g:
                if im.get("geo") != g or k in chaves_processadas:
                    im["geo"] = g
                    im["geo_pendente"] = False
                    mudou = True
            elif im.get("geo_pendente") is not True \
                    and im.get("endereco") and im.get("cidade"):
                im["geo_pendente"] = True
                mudou = True
        if mudou:
            salvar(os.path.join(DATA_DIR, uf, "imoveis.json"), doc)
            regravar_toon(os.path.join(DATA_DIR, uf, "imoveis.toon"), doc)
            ufs_regravadas += 1
            log(f"[{uf}] regravado")
    log(f"[FASE 4/5] fim: {ufs_regravadas}/{len(docs)} UFs regravadas | {time.time()-t0:.1f}s")

    log("[FASE 5/5] regravação do dataset global (BR)")
    t0 = time.time()
    gp = os.path.join(DATA_DIR, "imoveis.json")
    if os.path.exists(gp):
        gdoc = carregar(gp)
        mudou_global = False
        for im in gdoc["imoveis"]:
            k = geo.chave_geo(im)
            g = cache.get(k)
            if g:
                if im.get("geo") != g or k in chaves_processadas:
                    im["geo"] = g
                    im["geo_pendente"] = False
                    mudou_global = True
            elif im.get("geo_pendente") is not True \
                    and im.get("endereco") and im.get("cidade"):
                im["geo_pendente"] = True
                mudou_global = True
        if mudou_global:
            salvar(gp, gdoc)
            regravar_toon(os.path.join(DATA_DIR, "imoveis.toon"), gdoc)
            log("[BR] global regravado")
        else:
            log("[BR] global sem mudanças")
    else:
        log("[BR] data/imoveis.json não existe — pulando")
    log(f"[FASE 5/5] fim: {time.time()-t0:.1f}s")

    log(f"[GEO] TUDO CONCLUÍDO: {stats_total} | cota Google restante hoje: "
        f"{geo.cota_restante_google()}/{geo.GEO_LIMITE_GOOGLE} | "
        f"tempo total: {time.time()-t_total:.1f}s")


if __name__ == "__main__":
    main()