#!/usr/bin/env python3

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geo
import pipeline

DATA_DIR = "data"
UF_LISTA = ["AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT",
            "MS", "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO",
            "RR", "SC", "SP", "SE", "TO"]


def carregar(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def salvar(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def regravar_toon(p, doc):
    chaves = doc.get("colunas") or sorted(doc["imoveis"][0].keys())
    with open(p, "w", encoding="utf-8") as f:
        f.write(pipeline.gerar_toon(doc["uf"], doc["imoveis"], chaves,
                                    doc["data_geracao_caixa"], doc["fonte"]))


def main():
    filtro = os.environ.get("GEO_FILTRO_UF", "").strip().upper()
    geo.GEO_LIMITE_GOOGLE = int(os.environ.get("GEO_LIMITE_GOOGLE", "1500"))
    geo.GEO_LIMITE_OSM = int(os.environ.get("GEO_LIMITE_OSM", "2000"))

    docs = []
    for uf in UF_LISTA:
        if filtro and uf != filtro:
            continue
        p = os.path.join(DATA_DIR, uf, "imoveis.json")
        if os.path.exists(p):
            docs.append((uf, carregar(p)))
        else:
            print(f"[{uf}] sem imoveis.json")

    fila, vistos = [], set()
    for _, doc in docs:
        for im in doc["imoveis"]:
            k = geo.chave_geo(im)
            if k in vistos:
                continue
            tem_geo = isinstance(im.get("geo"), dict) and im["geo"].get("lat")
            if tem_geo:
                geo.carregar_cache()[k] = im["geo"]
                continue
            if im.get("geo_pendente") is False:
                continue
            if im.get("endereco") and im.get("cidade"):
                vistos.add(k)
                fila.append(im)

    print(f"[GEO] {len(fila)} enderecos unicos a enriquecer "
          f"(Google: {geo.GEO_LIMITE_GOOGLE}, OSM: {geo.GEO_LIMITE_OSM})")

    stats_total = {"novas_google": 0, "novas_osm": 0, "do_cache": 0,
                   "pendentes": 0, "sem_endereco": 0}
    processados = 0
    for im in fila:
        if geo._google_hoje >= geo.GEO_LIMITE_GOOGLE \
                and geo._osm_hoje >= geo.GEO_LIMITE_OSM:
            print("[GEO] limites alcancados")
            break
        s = geo.enriquecer([im])
        processados += 1
        for k in stats_total:
            stats_total[k] += s[k]

    cache = geo.carregar_cache()

    for uf, doc in docs:
        mudou = False
        for im in doc["imoveis"]:
            g = cache.get(geo.chave_geo(im))
            if g and im.get("geo") != g:
                im["geo"] = g
                im["geo_pendente"] = False
                mudou = True
            elif not g and im.get("geo_pendente") is not True \
                    and im.get("endereco") and im.get("cidade"):
                im["geo_pendente"] = True
                mudou = True
        salvar(os.path.join(DATA_DIR, uf, "imoveis.json"), doc)
        regravar_toon(os.path.join(DATA_DIR, uf, "imoveis.toon"), doc)
        if mudou:
            print(f"[{uf}] regravado")

    gp = os.path.join(DATA_DIR, "imoveis.json")
    if os.path.exists(gp) and not filtro:
        gdoc = carregar(gp)
        for im in gdoc["imoveis"]:
            g = cache.get(geo.chave_geo(im))
            if g:
                im["geo"] = g
                im["geo_pendente"] = False
            elif im.get("endereco") and im.get("cidade"):
                im["geo_pendente"] = True
        salvar(gp, gdoc)
        regravar_toon(os.path.join(DATA_DIR, "imoveis.toon"), gdoc)
        print("[BR] global regravado")

    with open(os.path.join(DATA_DIR, "_geo_status.json"), "w",
              encoding="utf-8") as f:
        json.dump(stats_total, f, ensure_ascii=False, indent=1)
    print(f"[GEO] fim: {stats_total} | processados: {processados}")


if __name__ == "__main__":
    main()