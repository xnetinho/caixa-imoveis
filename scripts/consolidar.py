#!/usr/bin/env python3
"""consolidar.py — Consolida os dados por-UF (produzidos pelos workers do
matrix) no dataset global (data/imoveis.{json,toon}) e em .github/state/_status.json.

- Reaplica o geo do _geocache.json (sem chamadas de API) para o mapa nunca
  ficar sem coordenadas entre as execuções da geocodificação.
- Recebe, via env `FRESH_UFS` (códigos separados por vírgula), quais UFs
  baixaram com sucesso nesta execução. UFs ausentes da lista são marcadas
  como falha real (dado anterior preservado) em _status.json.
- Se nenhuma UF fresca trouxer data de geração mais recente que a publicada,
  nada é reescrito (evita commit espúrio a cada varredura).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geo
import pipeline

DATA_DIR = "data"
STATE_DIR = ".github/state"
UFS = pipeline.UFS


def carregar(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def salvar(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def _fresh_ufs():
    raw = os.environ.get("FRESH_UFS", "").strip().upper()
    if not raw:
        return None  # não informado → tratar todas como presentes
    return {u for u in raw.replace(",", " ").split() if u in UFS}


def _publicadas():
    """Retorna {uf: (data_geracao_caixa, total)} do global publicado, ou None."""
    gp = os.path.join(DATA_DIR, "imoveis.json")
    if not os.path.exists(gp):
        return None
    try:
        doc = carregar(gp)
    except Exception:
        return None
    ufs = doc.get("ufs")
    if isinstance(ufs, list) and ufs:
        return {u.get("uf"): (u.get("data_geracao_caixa"), u.get("total"))
                for u in ufs}
    dg = doc.get("data_geracao_caixa")
    return {uf: (dg, None) for uf in UFS} if dg else None


def main():
    cache = geo.carregar_cache()
    fresh = _fresh_ufs()

    todos = []
    ufs_meta = []
    status = {}
    novas_por_uf = {}
    fresh_totais = {}

    for uf in UFS:
        p = os.path.join(DATA_DIR, uf, "imoveis.json")
        if not os.path.exists(p):
            status[uf] = {"ok": False, "erro": "sem imoveis.json"}
            continue
        doc = carregar(p)
        imoveis = doc["imoveis"]
        geo.reaplicar_geo(imoveis, cache)
        salvar(p, doc)

        dg = doc.get("data_geracao_caixa")
        ufs_meta.append({
            "uf": uf,
            "data_geracao_caixa": dg,
            "coletado_em": doc.get("coletado_em"),
            "fonte": doc.get("fonte"),
            "total": len(imoveis),
        })

        if fresh is None or uf in fresh:
            status[uf] = {"ok": True, "total": len(imoveis), "gerado_em": dg}
            fresh_totais[uf] = len(imoveis)
            if dg:
                novas_por_uf[uf] = dg
        else:
            status[uf] = {"ok": False, "total": len(imoveis), "gerado_em": dg,
                          "erro": "falha no download — dado anterior preservado"}

        todos.extend(imoveis)

    ufs_ok = sum(1 for s in status.values() if s.get("ok"))

    if not todos:
        print("[CONSOLIDAR] nenhum dado por-UF encontrado — abortando")
        sys.exit(1)

    # guarda de "sem mudança": só reescreve se alguma UF fresca tiver data mais
    # nova OU total diferente do publicado (novos imóveis sem trocar a data).
    publicadas = _publicadas()
    mudou = publicadas is None
    if not mudou:
        for uf, tot in fresh_totais.items():
            dg_ant, tot_ant = publicadas.get(uf, (None, None))
            if (novas_por_uf.get(uf, "") > (dg_ant or "")) or \
               (tot_ant is not None and tot != tot_ant):
                mudou = True
                break
    if not mudou and os.environ.get("FORCA") != "1":
        print("[CONSOLIDAR] nenhuma UF trouxe dados mais recentes — nada a fazer.")
        print("changed=false")
        return

    chaves = sorted({c for im in todos for c in im})
    atualizado_em = pipeline.agora_br()

    salvar(os.path.join(DATA_DIR, "imoveis.json"), {
        "uf": "BR",
        "atualizado_em": atualizado_em,
        "fonte": "por-UF",
        "total": len(todos),
        "colunas": chaves,
        "ufs": ufs_meta,
        "imoveis": todos,
    })

    with open(os.path.join(DATA_DIR, "imoveis.toon"), "w", encoding="utf-8") as f:
        f.write(pipeline.gerar_toon("BR", todos, chaves, atualizado_em, "por-UF"))

    salvar(os.path.join(STATE_DIR, "_status.json"), {
        "executado_em": atualizado_em,
        "modo": "ufs",
        "ufs_ok": ufs_ok,
        "ufs_falha": len(UFS) - ufs_ok,
        "status": status,
    })

    print(f"[CONSOLIDAR] {ufs_ok}/{len(UFS)} UFs frescas | {len(todos)} imóveis | "
          f"{atualizado_em}")
    print("changed=true")


if __name__ == "__main__":
    main()
