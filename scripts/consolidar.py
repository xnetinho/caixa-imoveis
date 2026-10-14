#!/usr/bin/env python3
"""consolidar.py — Consolida os dados por-UF (produzidos pelos workers do
matrix no workflow) no dataset global (data/imoveis.{json,toon,csv}) e no
data/_status.json.

Reaplica o geo do _geocache.json ao dataset global — sem nenhuma chamada de
API — para o mapa nunca ficar sem coordenadas entre as execuções diárias da
geocodificação.

Se nenhum imóvel trouxer data de geração mais recente que a já publicada,
nada é reescrito (evita commit espúrio a cada varredura).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geo
import pipeline

DATA_DIR = "data"
UFS = pipeline.UFS


def carregar(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def salvar(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def _data_publicada():
    gp = os.path.join(DATA_DIR, "imoveis.json")
    if not os.path.exists(gp):
        return None
    try:
        return carregar(gp).get("data_geracao_caixa")
    except Exception:
        return None


def main():
    cache = geo.carregar_cache()
    todos = []
    status = {}
    data_geracao = None

    for uf in UFS:
        p = os.path.join(DATA_DIR, uf, "imoveis.json")
        if not os.path.exists(p):
            status[uf] = {"ok": False, "erro": "sem imoveis.json"}
            continue
        doc = carregar(p)
        imoveis = doc["imoveis"]
        for im in imoveis:
            g = cache.get(geo.chave_geo(im))
            if g:
                im["geo"] = g
                im["geo_pendente"] = False
        doc["imoveis"] = imoveis
        salvar(p, doc)
        todos.extend(imoveis)
        dg = doc.get("data_geracao_caixa")
        if dg:
            data_geracao = max(data_geracao or "", dg)
        status[uf] = {"ok": True, "total": len(imoveis), "gerado_em": dg}

    ufs_ok = sum(1 for s in status.values() if s.get("ok"))

    if not todos:
        print("[CONSOLIDAR] nenhum dado por-UF encontrado — abortando")
        sys.exit(1)

    # nada mais recente que o publicado → não reescreve nada
    publicada = _data_publicada()
    if publicada and data_geracao and data_geracao <= publicada \
            and os.environ.get("FORCA") != "1":
        print(f"[CONSOLIDAR] dados ({data_geracao}) não mais recentes que o "
              f"publicado ({publicada}) — nada a fazer.")
        return

    chaves = sorted({c for im in todos for c in im})

    salvar(os.path.join(DATA_DIR, "imoveis.json"), {
        "uf": "BR",
        "data_geracao_caixa": data_geracao,
        "coletado_em": pipeline.agora_br(),
        "fonte": "Lista_imoveis_{UF}.csv (por-UF)",
        "total": len(todos),
        "ufs": ufs_ok,
        "colunas": chaves,
        "imoveis": todos,
    })

    with open(os.path.join(DATA_DIR, "imoveis.toon"), "w", encoding="utf-8") as f:
        f.write(pipeline.gerar_toon("BR", todos, chaves, data_geracao, "por-UF"))

    salvar(os.path.join(DATA_DIR, "_status.json"), {
        "executado_em": pipeline.agora_br(),
        "modo": "ufs",
        "data_geracao_caixa": data_geracao,
        "ufs_ok": ufs_ok,
        "ufs_falha": len(UFS) - ufs_ok,
        "status": status,
    })

    print(f"[CONSOLIDAR] {ufs_ok}/{len(UFS)} UFs | {len(todos)} imóveis | "
          f"data {data_geracao}")


if __name__ == "__main__":
    main()
