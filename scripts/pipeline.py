#!/usr/bin/env python3
"""
pipeline.py — Baixa os CSVs oficiais da Caixa (venda-imoveis.caixa.gov.br/listaweb)
por UF, converte para JSON e TOON e grava em data/{UF}/.

Regras de robustez (portal usa Radware Bot Manager):
  - Download via curl PURO (sem User-Agent de navegador): é o padrão comprovado
    que passa tanto de IP residencial quanto de runner do GitHub Actions.
    Headers "estilo navegador" são BLOQUEADOS (302 -> CAPTCHA).
  - Validação de conteúdo ANTES de gravar: assinatura "Lista de Im" + header
    reconhecível + >= 1 imóvel. UF inválida NÃO sobrescreve os arquivos bons.
  - Colunas mapeadas DINAMICAMENTE pelo nome do cabeçalho (normalizado, sem
    acento). Colunas desconhecidas entram como extras_... — se a Caixa mudar
    o layout, nada quebra e nada é perdido.

Saída por UF (sempre a versão mais recente, nomes fixos):
  data/{UF}/imoveis.csv   — CSV original, bytes intactos (Windows-1252)
  data/{UF}/imoveis.json — UTF-8, campos canonizados + extras
  data/{UF}/imoveis.toon — Token-Oriented Object Notation (tabular)
  data/_status.json      — resultado da última execução (por UF)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone

BR_TZ = timezone(timedelta(hours=-3))
BASE = "https://venda-imoveis.caixa.gov.br/listaweb"
DATA_DIR = "data"
DELAY_S = 3          # pausa entre tentativas de retry
TIMEOUT_S = 120
RETRY_MAX = 4         # tentativas por UF com backoff (Radware desafia IP após
RETRY_BACKOFF_S = 20  # poucas requisições — cada job do matrix usa IP próprio,
                      # o retry cobre desafios transitórios
CSV_SIGNATURE = b"Lista de Im"  # prefixo ASCII do título (encoding cp1252)

UFS = ["AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
       "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
       "SP", "SE", "TO"]

# Cabeçalho normalizado -> chave canônica no JSON/TOON.
# Nomes são normalizados (minúsculo, sem acento) antes de consultar.
FIELD_MAP = {
    "no do imovel": "id",
    "n do imovel": "id",
    "numero do imovel": "id",
    "uf": "uf",
    "cidade": "cidade",
    "bairro": "bairro",
    "endereco": "endereco",
    "preco": "preco",
    "valor de avaliacao": "valor_avaliacao",
    "desconto": "desconto",
    "financiamento": "financiamento",
    "descricao": "descricao",
    "modalidade de venda": "modalidade",
    "link de acesso": "link",
    "link": "link",
}
# Campos monetários (pt-BR "68.240,92" -> 68240.92)
MONEY_FIELDS = {"preco", "valor_avaliacao"}
# Campos numéricos simples ("47.51" -> 47.51)
NUM_FIELDS = {"desconto"}

ID_FROM_LINK = re.compile(r"(?:txtImovel|hdnimovel)=(\d+)")


def agora_br() -> str:
    return datetime.now(BR_TZ).strftime("%d/%m/%Y %H:%M")


def norm(s: str) -> str:
    """Normaliza um texto: sem acento, minúsculo, espaços colapsados."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def split_linha(linha: str):
    """Split por ';' respeitando aspas duplas."""
    out, cur, inq = [], [], False
    for ch in linha:
        if ch == '"':
            inq = not inq
        elif ch == ";" and not inq:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def parse_valor(txt: str):
    """'68.240,92' -> 68240.92 | '47.51' -> 47.51 | inválido -> None."""
    t = (txt or "").strip()
    if not t:
        return None
    t = t.replace(".", "").replace(",", ".") if t.count(",") >= 1 else t
    try:
        return float(t)
    except ValueError:
        return None


# ── DOWNLOAD ──────────────────────────────────────────────────────────────────

def baixar(uf: str, dest: str):
    """curl puro (sem UA custom). Retorna (http_code, erro_ou_None)."""
    url = f"{BASE}/Lista_imoveis_{uf}.csv"
    r = subprocess.run(
        ["curl", "-sS", "--max-time", str(TIMEOUT_S),
         "-o", dest, "-w", "%{http_code}", url],
        capture_output=True, text=True,
    )
    code = r.stdout.strip()
    if r.returncode != 0:
        return code or "ERR", f"curl falhou: {r.stderr.strip()[:120]}"
    return code, None


# ── VALIDAÇÃO E PARSE ─────────────────────────────────────────────────────────

def achar_header(linhas):
    """Procura (dentro das primeiras linhas) a linha de cabeçalho real."""
    for i, linha in enumerate(linhas[:20]):
        n = norm(linha)
        if "cidade" in n and ("preco" in n or "link" in n or "imovel" in n):
            if len(split_linha(linha)) >= 5:
                return i
    return None


def mapear_colunas(header_linha):
    """Retorna [(indice, chave_canonica)] a partir do cabeçalho."""
    cols = split_linha(header_linha)
    mapa = []
    for idx, raw in enumerate(cols):
        nome = norm(raw)
        if not nome:
            continue
        chave = FIELD_MAP.get(nome)
        if chave is None:
            # coluna desconhecida da Caixa -> preservada como extra_<nome>
            chave = "extra_" + re.sub(r"[^a-z0-9]+", "_", nome).strip("_")
        mapa.append((idx, chave))
    return mapa, cols


def extrair_id(link: str, fallback: str):
    m = ID_FROM_LINK.search(link or "")
    if m:
        return m.group(1)
    t = (fallback or "").strip()
    # coluna pode vir em notação científica (ex: 8,7877E+12) -> inútil
    return t if t.isdigit() else None


def processar(raw_bytes: bytes, uf: str):
    """Valida e converte o CSV. Levanta ValueError se o conteúdo for lixo."""
    if CSV_SIGNATURE not in raw_bytes:
        raise ValueError("assinatura 'Lista de Im' ausente (CAPTCHA/bloqueio?)")
    if len(raw_bytes) < 500:
        raise ValueError(f"arquivo muito pequeno: {len(raw_bytes)} bytes")

    texto = raw_bytes.decode("cp1252", errors="replace")
    linhas = texto.replace("\r", "").split("\n")
    hidx = achar_header(linhas)
    if hidx is None:
        raise ValueError("cabeçalho não reconhecido (layout mudou?)")

    mapa, cols = mapear_colunas(linhas[hidx])

    imoveis, chaves_vistas = [], []
    for linha in linhas[hidx + 1:]:
        if not linha.strip():
            continue
        cvals = split_linha(linha)
        if len(cvals) < max(idx for idx, _ in mapa) + 1:
            continue  # linha trunctada
        obj = {}
        for idx, chave in mapa:
            v = (cvals[idx] if idx < len(cvals) else "").strip()
            if chave in MONEY_FIELDS or chave in NUM_FIELDS:
                obj[chave] = parse_valor(v)
            else:
                obj[chave] = v if v else None
        # ID confiável vem do link (coluna de ID pode vir em not. científica)
        obj["id"] = extrair_id(obj.get("link") or "", str(obj.get("id") or ""))
        # desconto derivado quando ausente
        if obj.get("desconto") is None and obj.get("preco") \
                and obj.get("valor_avaliacao") and obj["valor_avaliacao"] > obj["preco"]:
            obj["desconto"] = round(
                (1 - obj["preco"] / obj["valor_avaliacao"]) * 100, 2)
        if not obj.get("id"):
            continue  # sem ID não há como deduplicar — descarta
        if not chaves_vistas:
            chaves_vistas = [c for _, c in mapa]
        imoveis.append(obj)

    if not imoveis:
        raise ValueError("nenhum imóvel extraído (CSV vazio ou bloqueado)")
    return imoveis, chaves_vistas


# ── TOON (Token-Oriented Object Notation, forma tabular) ──────────────────────

def toon_cell(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else repr(v)
    s = str(v)
    if s == "" or s != s.strip() or any(c in s for c in ',\n"'):
        return '"' + s.replace('"', '""') + '"'
    return s


def gerar_toon(uf, imoveis, chaves, gerado_em, fonte):
    chaves = [c for c in chaves if c != "id"] or list(imoveis[0].keys())
    campos = ["id"] + chaves
    linhas = [
        f"meta{{uf,gerado_em,total,fonte}}:",
        f"  {uf},{toon_cell(gerado_em)},{len(imoveis)},{fonte}",
        f"imoveis[{len(imoveis)}]{{{','.join(campos)}}}:",
    ]
    for im in imoveis:
        linhas.append("  " + ",".join(toon_cell(im.get(c)) for c in campos))
    return "\n".join(linhas) + "\n"


# ── EXECUÇÃO POR UF ────────────────────────────────────────────────────────────

def rodar_uf(uf: str, status: dict):
    """Baixa a UF com retry/backoff. Sucesso só depois de validado."""
    tmpdir = tempfile.mkdtemp(prefix=f"caixa_{uf}_")
    tmp_csv = os.path.join(tmpdir, "raw.csv")
    last_err = None
    for tentativa in range(1, RETRY_MAX + 1):
        code, err = baixar(uf, tmp_csv)
        try:
            if code != "200":
                raise ValueError(f"HTTP {code}")
            with open(tmp_csv, "rb") as f:
                raw = f.read()
            imoveis, chaves = processar(raw, uf)  # valida conteúdo
        except ValueError as e:
            last_err = str(e)
            if tentativa < RETRY_MAX:
                wait = RETRY_BACKOFF_S * tentativa
                print(f"[{uf}] tentativa {tentativa}/{RETRY_MAX} falhou "
                      f"({last_err}) — aguardando {wait}s")
                time.sleep(wait)
                continue
            break

        uf_dir = os.path.join(DATA_DIR, uf)
        os.makedirs(uf_dir, exist_ok=True)
        # Sempre a versão mais recente: nomes fixos, sobrescreve tudo
        shutil.copyfile(tmp_csv, os.path.join(uf_dir, "imoveis.csv"))
        with open(os.path.join(uf_dir, "imoveis.json"), "w", encoding="utf-8") as f:
            json.dump({
                "uf": uf,
                "gerado_em": agora_br(),
                "fonte": f"{BASE}/Lista_imoveis_{uf}.csv",
                "total": len(imoveis),
                "colunas": chaves,
                "imoveis": imoveis,
            }, f, ensure_ascii=False, indent=1)
        with open(os.path.join(uf_dir, "imoveis.toon"), "w", encoding="utf-8") as f:
            f.write(gerar_toon(uf, imoveis, chaves, agora_br(),
                               f"{BASE}/Lista_imoveis_{uf}.csv"))
        status[uf] = {"ok": True, "total": len(imoveis),
                      "bytes_csv": len(raw), "gerado_em": agora_br()}
        print(f"[{uf}] ✅ {len(imoveis)} imóveis ({len(raw):,} bytes)")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return True
    # esgotou as tentativas
    status[uf] = {"ok": False, "erro": last_err or "falha"}
    print(f"[{uf}] ❌ {status[uf]['erro']} — arquivos anteriores PRESERVADOS")
    shutil.rmtree(tmpdir, ignore_errors=True)
    return False


def main():
    alvo = sys.argv[1:] or UFS
    alvo = [uf.upper() for uf in alvo if uf.upper() in UFS]
    print(f"=== Pipeline Caixa CSV→JSON+TOON | {agora_br()} | UFs: {len(alvo)} ===")
    status = {}
    ok = 0
    for i, uf in enumerate(alvo):
        if rodar_uf(uf, status):
            ok += 1
        if i < len(alvo) - 1:
            time.sleep(DELAY_S)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "_status.json"), "w", encoding="utf-8") as f:
        json.dump({
            "executado_em": agora_br(),
            "ufs_ok": ok,
            "ufs_falha": len(alvo) - ok,
            "status": status,
        }, f, ensure_ascii=False, indent=1)

    print(f"=== Fim: {ok}/{len(alvo)} UFs OK ===")
    if ok == 0:
        sys.exit(1)  # tudo falhou -> workflow falha (não commitar nada)


if __name__ == "__main__":
    main()