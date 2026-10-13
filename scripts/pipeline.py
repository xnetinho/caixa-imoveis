                      
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
CSV_GERAL = f"{BASE}/Lista_imoveis_geral.csv"                                
DATA_DIR = "data"
DELAY_S = 3                                           
TIMEOUT_S = 120
RETRY_MAX = 4                                                                 
RETRY_BACKOFF_S = 20                                                           
CSV_SIGNATURE = b"Lista de Im"                                             
DATA_GERACAO_RE = re.compile(
    rb"Data de gera\xe7\xe3o:;\s*(\d{2}/\d{2}/\d{4})")
UFS = ["AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
       "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
       "SP", "SE", "TO"]
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
MONEY_FIELDS = {"preco", "valor_avaliacao"}
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
            chave = "extra_" + re.sub(r"[^a-z0-9]+", "_", nome).strip("_")
        mapa.append((idx, chave))
    return mapa, cols
def extrair_id(link: str, fallback: str):
    m = ID_FROM_LINK.search(link or "")
    if m:
        return m.group(1)
    t = (fallback or "").strip()
    return t if t.isdigit() else None
def processar_linhas(linhas, uf):
    """Parse de linhas de CSV (com header na posição 0 da lista).
    Retorna (imoveis, chaves). Levanta ValueError se layout desconhecido."""
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
            continue                   
        obj = {}
        for idx, chave in mapa:
            v = (cvals[idx] if idx < len(cvals) else "").strip()
            if chave in MONEY_FIELDS or chave in NUM_FIELDS:
                obj[chave] = parse_valor(v)
            else:
                obj[chave] = v if v else None
        obj["id"] = extrair_id(obj.get("link") or "", str(obj.get("id") or ""))
        if obj.get("desconto") is None and obj.get("preco")                and obj.get("valor_avaliacao") and obj["valor_avaliacao"] > obj["preco"]:
            obj["desconto"] = round(
                (1 - obj["preco"] / obj["valor_avaliacao"]) * 100, 2)
        if not obj.get("id"):
            continue                                            
        if not chaves_vistas:
            chaves_vistas = [c for _, c in mapa]
        imoveis.append(obj)
    return imoveis, chaves_vistas
def processar(raw_bytes: bytes, uf: str):
    """Valida e converte o CSV. Levanta ValueError se o conteúdo for lixo."""
    if CSV_SIGNATURE not in raw_bytes:
        raise ValueError("assinatura 'Lista de Im' ausente (CAPTCHA/bloqueio?)")
    if len(raw_bytes) < 500:
        raise ValueError(f"arquivo muito pequeno: {len(raw_bytes)} bytes")
    texto = raw_bytes.decode("cp1252", errors="replace")
    linhas = texto.replace("\r", "").split("\n")
    imoveis, chaves_vistas = processar_linhas(linhas, uf)
    if not imoveis:
        raise ValueError("nenhum imóvel extraído (CSV vazio ou bloqueado)")
    m = DATA_GERACAO_RE.search(raw_bytes)
    data_geracao = m.group(1).decode("ascii") if m else None
    if not data_geracao:
        raise ValueError("data de geração da Caixa ausente no CSV (layout mudou?)")
    return imoveis, chaves_vistas, data_geracao
def toon_cell(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, dict):
        if "lat" in v:
            return f"{v.get('lat')},{v.get('lng')};{v.get('fonte','')}"
        return '"' + json.dumps(v, ensure_ascii=False,
                                separators=(",", ":")).replace('"', '""') + '"'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else repr(v)
    s = str(v)
    if s == "" or s != s.strip() or any(c in s for c in ',\n"'):
        return '"' + s.replace('"', '""') + '"'
    return s
def gerar_toon(uf, imoveis, chaves, data_geracao, fonte):
    chaves = [c for c in chaves if c != "id"] or list(imoveis[0].keys())
    campos = ["id"] + chaves
    linhas = [
        f"meta{{uf,data_geracao_caixa,total,fonte}}:",
        f"  {uf},{toon_cell(data_geracao)},{len(imoveis)},{fonte}",
        f"imoveis[{len(imoveis)}]{{{','.join(campos)}}}:",
    ]
    for im in imoveis:
        linhas.append("  " + ",".join(toon_cell(im.get(c)) for c in campos))
    return "\n".join(linhas) + "\n"
def rodar_geral(status: dict):
    """Baixa o CSV único do Brasil inteiro e divide por UF.
    1 chamada ao servidor da Caixa em vez de 27 — reduz drasticamente o
    risco de bloqueio Radware. UF sem imóveis no arquivo é simplesmente
    pulada (não sobrescreve nada)."""
    tmpdir = tempfile.mkdtemp(prefix="caixa_geral_")
    tmp_csv = os.path.join(tmpdir, "geral.csv")
    last_err = None
    for tentativa in range(1, RETRY_MAX + 1):
        r = subprocess.run(
            ["curl", "-sS", "--max-time", str(TIMEOUT_S),
             "-o", tmp_csv, "-w", "%{http_code}", CSV_GERAL],
            capture_output=True, text=True)
        code = r.stdout.strip()
        try:
            if r.returncode != 0:
                raise ValueError(f"curl falhou: {r.stderr.strip()[:120]}")
            if code != "200":
                raise ValueError(f"HTTP {code}")
            with open(tmp_csv, "rb") as f:
                raw = f.read()
            if CSV_SIGNATURE not in raw or len(raw) < 500:
                raise ValueError("assinatura ausente (CAPTCHA/bloqueio?)")
            m = DATA_GERACAO_RE.search(raw)
            if not m:
                raise ValueError("data de geração da Caixa ausente (layout?)")
            data_geracao = m.group(1).decode("ascii")
        except ValueError as e:
            last_err = str(e)
            if tentativa < RETRY_MAX:
                wait = RETRY_BACKOFF_S * tentativa
                print(f"[GERAL] tentativa {tentativa}/{RETRY_MAX} falhou "
                      f"({last_err}) — aguardando {wait}s")
                time.sleep(wait)
                continue
            print(f"[GERAL] ❌ {last_err} — NADA sobrescrito")
            shutil.rmtree(tmpdir, ignore_errors=True)
            return False
        break
    else:
        print(f"[GERAL] ❌ {last_err or 'falha'} — NADA sobrescrito")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return False
    texto = raw.decode("cp1252", errors="replace")
    linhas = texto.replace("\r", "").split("\n")
    hidx = achar_header(linhas)
    if hidx is None:
        print("[GERAL] ❌ cabeçalho não reconhecido — NADA sobrescrito")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return False
    status_path = os.path.join(DATA_DIR, "_status.json")
    data_atual_repo = None
    if os.path.exists(status_path):
        try:
            with open(status_path, encoding="utf-8") as f:
                data_atual_repo = json.load(f).get("data_geracao_caixa")
        except Exception:
            pass
    if not data_atual_repo:
        pb = os.path.join(DATA_DIR, "PB", "imoveis.json")
        if os.path.exists(pb):
            try:
                with open(pb, encoding="utf-8") as f:
                    data_atual_repo = json.load(f).get("data_geracao_caixa")
            except Exception:
                pass
    if data_atual_repo and data_geracao <= data_atual_repo            and os.environ.get("FORCA") != "1":
        print(f"[GERAL] CSV da Caixa ({data_geracao}) não é mais recente que o "
              f"publicado ({data_atual_repo}) — nada a fazer.")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return "sem_mudanca"
    header_linha = linhas[hidx]
    por_uf = {uf: [header_linha] for uf in UFS}
    raw_linhas = texto.split("\n")
    for linha in raw_linhas[hidx + 1:]:
        if not linha.strip():
            continue
        cols = split_linha(linha)
        uf = (cols[1] if len(cols) > 1 else "").strip().upper()
        if uf not in por_uf:
            continue
        por_uf[uf].append(linha)

    ids_anteriores = set()
    gp = os.path.join(DATA_DIR, "imoveis.json")
    if os.path.exists(gp):
        try:
            with open(gp, encoding="utf-8") as f:
                for im in json.load(f).get("imoveis", []):
                    ids_anteriores.add(im.get("id"))
        except Exception:
            pass

    ok = 0
    novos_ids, removidos_ids = [], sorted(ids_anteriores)
    todos_imoveis = []
    for uf in UFS:
        imoveis, chaves = processar_linhas(por_uf[uf], uf)
        if not imoveis:
            print(f"[{uf}] sem imóveis no CSV geral — anterior preservado")
            continue
        uf_dir = os.path.join(DATA_DIR, uf)
        os.makedirs(uf_dir, exist_ok=True)
        with open(os.path.join(uf_dir, "imoveis.csv"), "w",
                  encoding="cp1252", errors="replace") as f:
            f.write(texto.split("\n")[1] + "\n")
            f.write("\n".join(por_uf[uf]) + "\n")
        with open(os.path.join(uf_dir, "imoveis.json"), "w", encoding="utf-8") as f:
            json.dump({
                "uf": uf,
                "data_geracao_caixa": data_geracao,
                "coletado_em": agora_br(),
                "fonte": CSV_GERAL,
                "total": len(imoveis),
                "colunas": chaves,
                "imoveis": imoveis,
            }, f, ensure_ascii=False, indent=1)
        with open(os.path.join(uf_dir, "imoveis.toon"), "w", encoding="utf-8") as f:
            f.write(gerar_toon(uf, imoveis, chaves, data_geracao, CSV_GERAL))
        status[uf] = {"ok": True, "total": len(imoveis),
                      "gerado_em": data_geracao}
        todos_imoveis.extend(imoveis)
        print(f"[{uf}] ✅ {len(imoveis)} imóveis")
        ok += 1

    if todos_imoveis:
        ids_atuais = {im.get("id") for im in todos_imoveis if im.get("id")}
        novos_ids = sorted(ids_atuais - ids_anteriores)
        removidos_ids = sorted(ids_anteriores - ids_atuais)
        print(f"[DIFF] novos: {len(novos_ids)} | removidos: {len(removidos_ids)}")

        os.makedirs(DATA_DIR, exist_ok=True)
        shutil.copyfile(tmp_csv, os.path.join(DATA_DIR, "imoveis.csv"))
        chaves_global = sorted({c for im in todos_imoveis for c in im})
        with open(os.path.join(DATA_DIR, "imoveis.json"), "w", encoding="utf-8") as f:
            json.dump({
                "uf": "BR",
                "data_geracao_caixa": data_geracao,
                "coletado_em": agora_br(),
                "fonte": CSV_GERAL,
                "total": len(todos_imoveis),
                "ufs": ok,
                "colunas": chaves_global,
                "imoveis": todos_imoveis,
            }, f, ensure_ascii=False, indent=1)
        with open(os.path.join(DATA_DIR, "imoveis.toon"), "w", encoding="utf-8") as f:
            f.write(gerar_toon("BR", todos_imoveis, chaves_global,
                               data_geracao, CSV_GERAL))
        status["BR"] = {"ok": True, "total": len(todos_imoveis),
                        "gerado_em": data_geracao}
        print(f"[BR] ✅ global: {len(todos_imoveis)} imóveis em {ok} UFs")

    status["_diff"] = {"novos": len(novos_ids),
                      "removidos": len(removidos_ids),
                      "novos_ids": novos_ids[:200],
                      "removidos_ids": removidos_ids[:200]}
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"[GERAL] {ok} UFs processadas de {CSV_GERAL}")
    return ok > 0
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
            imoveis, chaves, data_geracao = processar(raw, uf)                   
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
        shutil.copyfile(tmp_csv, os.path.join(uf_dir, "imoveis.csv"))
        with open(os.path.join(uf_dir, "imoveis.json"), "w", encoding="utf-8") as f:
            json.dump({
                "uf": uf,
                "data_geracao_caixa": data_geracao,
                "coletado_em": agora_br(),
                "fonte": f"{BASE}/Lista_imoveis_{uf}.csv",
                "total": len(imoveis),
                "colunas": chaves,
                "imoveis": imoveis,
            }, f, ensure_ascii=False, indent=1)
        with open(os.path.join(uf_dir, "imoveis.toon"), "w", encoding="utf-8") as f:
            f.write(gerar_toon(uf, imoveis, chaves, data_geracao,
                               f"{BASE}/Lista_imoveis_{uf}.csv"))
        status[uf] = {"ok": True, "total": len(imoveis),
                      "bytes_csv": len(raw), "gerado_em": data_geracao}
        print(f"[{uf}] ✅ {len(imoveis)} imóveis ({len(raw):,} bytes)")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return True
    status[uf] = {"ok": False, "erro": last_err or "falha"}
    print(f"[{uf}] ❌ {status[uf]['erro']} — arquivos anteriores PRESERVADOS")
    shutil.rmtree(tmpdir, ignore_errors=True)
    return False
def main():
    modo = "geral"
    args = sys.argv[1:]
    if args and args[0] == "geral":
        args = args[1:]
    alvo = [uf.upper() for uf in args if uf.upper() in UFS]
    if not args or modo == "geral" and not alvo:
        print(f"=== Pipeline Caixa CSV→JSON+TOON (modo geral) | {agora_br()} ===")
        status = {}
        ok = rodar_geral(status)
        os.makedirs(DATA_DIR, exist_ok=True)
        if ok == "sem_mudanca":
            print("=== Fim (modo geral): SEM MUDANÇA (CSV da Caixa é o mesmo) ===")
            return
        if ok:
            dg_pub = None
            gp = os.path.join(DATA_DIR, "imoveis.json")
            if os.path.exists(gp):
                try:
                    with open(gp, encoding="utf-8") as f:
                        dg_pub = json.load(f).get("data_geracao_caixa")
                except Exception:
                    pass
            diff = status.pop("_diff", {})
            ufs_ok = sum(1 for uf in UFS if status.get(uf, {}).get("ok"))
            with open(os.path.join(DATA_DIR, "_status.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "executado_em": agora_br(),
                    "modo": "geral",
                    "data_geracao_caixa": dg_pub,
                    "ufs_ok": ufs_ok,
                    "ufs_falha": len(UFS) - ufs_ok,
                    "diff": diff,
                    "status": {uf: status.get(uf, {"ok": False,
                             "erro": "não processada"}) for uf in UFS},
                }, f, ensure_ascii=False, indent=1)
        print(f"=== Fim (modo geral): {'OK' if ok else 'FALHOU'} ===")
        if not ok:
            sys.exit(1)
        return
    alvo = alvo or UFS
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
            "modo": "ufs",
            "ufs_ok": ok,
            "ufs_falha": len(alvo) - ok,
            "status": status,
        }, f, ensure_ascii=False, indent=1)
    print(f"=== Fim: {ok}/{len(alvo)} UFs OK ===")
    if ok == 0:
        sys.exit(1)                                                     
if __name__ == "__main__":
    main()