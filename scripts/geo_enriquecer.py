#!/usr/bin/env python3
"""
geo_enriquecer.py — Enriquecimento de coordenadas sob demanda.

Lê os JSONs publicados em data/ (UFs + global), enriquece os imóveis que
ainda não têm geo (cache → OSM → Google), regrava JSONs e TOONs alterados
e atualiza o cache. Não baixa nada da Caixa — só geodata.

Env:
  GEO_FILTRO_UF        UF alvo (vazio = todas)
  GEO_LIMITE_OSM       teto de consultas OSM nesta execução (default 20000)
  GEO_LIMITE_GOOGLE    teto de consultas Google (default 1500)
  GOOGLE_MAPS_API_KEY  chave via secret
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geo  # noqa: E402  (reusa cache/provedores)
import pipeline  # noqa: E402  (reusa gerar_toon)

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
    limite_osm = int(os.environ.get("GEO_LIMITE_OSM", "20000"))

    # 1. carrega docs das UFs alvo
    docs = []
    for uf in UF_LISTA:
        if filtro and uf != filtro:
            continue
        p = os.path.join(DATA_DIR, uf, "imoveis.json")
        if os.path.exists(p):
            docs.append((uf, carregar(p)))
        else:
            print(f"[{uf}] sem imoveis.json — pulando")

    # 2. seleciona imóveis SEM geo (ou pendentes), por chave de endereço única
    fila, vistos = [], set()
    for _, doc in docs:
        for im in doc["imoveis"]:
            k = geo.chave_geo(im)
            if k in vistos:
                continue
            tem_geo = isinstance(im.get("geo"), dict) and im["geo"].get("lat")
            pendente = im.get("geo_pendente")
            if tem_geo or pendente is False:
                if tem_geo:
                    # repovoa o cache com o que já tem geo (caso cache vazio)
                    geo.carregar_cache()[k] = im["geo"]
                continue
            if im.get("endereco") and im.get("cidade"):
                vistos.add(k)
                fila.append(im)
    print(f"[GEO] {len(fila)} endereços únicos a enriquecer "
          f"(limite OSM: {limite_osm}, Google: {geo.GEO_LIMITE_GOOGLE})")

    # 3. enriquece respeitando limites
    stats_total = {"novas_osm": 0, "novas_google": 0, "do_cache": 0,
                   "pendentes": 0, "sem_endereco": 0}
    processados = 0
    for im in fila:
        if stats_total["novas_osm"] >= limite_osm:
            print(f"[GEO] limite OSM alcançado — restante fica pendente")
            break
        s = geo.enriquecer([im])
        processados += 1
        for k in stats_total:
            stats_total[k] += s[k]

    # 4. aplica o geo resultante a TODAS as ocorrências (por chave de endereço)
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
        p_json = os.path.join(DATA_DIR, uf, "imoveis.json")
        salvar(p_json, doc)
        regravar_toon(os.path.join(DATA_DIR, uf, "imoveis.toon"), doc)
        if mudou:
            print(f"[{uf}] JSON/TOON regravados")

    # 5. global (quando sem filtro)
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