# Caixa CSV → JSON + TOON

![pipeline](https://github.com/xnetinho/caixa-imoveis/actions/workflows/atualizar.yml/badge.svg)
![probe](https://github.com/xnetinho/caixa-imoveis/actions/workflows/probe.yml/badge.svg)

Baixa diariamente, via GitHub Actions, os **CSVs oficiais de imóveis da Caixa Econômica Federal** e publica:

| Arquivo | Conteúdo |
|---|---|
| `data/imoveis.{csv,json,toon}` | **Global (Brasil inteiro)** — todos os ~21k imóveis em um só dataset (cada imóvel com campo `uf`) |
| `data/{UF}/imoveis.{csv,json,toon}` | Por estado (27 UFs) |
| `data/_status.json` | Status da última execução (por UF + global) |

Sempre **a versão mais recente** (nomes fixos, sobrescreve) + `data/_status.json` com o resultado da última execução.

Fonte: `https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_geral.csv` (Brasil inteiro em **1 arquivo**, ~7,7 MB / ~21k imóveis — 1 chamada ao servidor em vez de 27, eliminando o bloqueio Radware de requisições sequenciais). Fallback: downloads por UF (`Lista_imoveis_{UF}.csv`) se o geral falhar.

## Robustez (portal protegido por Radware Bot Manager)

- **Download com `curl` puro** (sem UA de navegador): padrão comprovado por probe (9/9 UFs OK em runner do GitHub Actions + IP residencial). Headers "estilo navegador" são bloqueados (CAPTCHA).
- **Validação de conteúdo antes de gravar**: assinatura "Lista de Im", cabeçalho reconhecível e ≥1 imóvel. UF bloqueada/falha **não sobrescreve** os arquivos bons.
- **Colunas dinâmicas**: mapeadas pelo nome do cabeçalho (normalizado). Se a Caixa mudar o layout, colunas desconhecidas são preservadas como `extra_*` — nada quebra, nada se perde.
- **ID confiável**: extraído do link do imóvel (a coluna de ID pode vir em notação científica no Excel).
- Delay de 3s entre UFs; falha total (0/27) derruba o job em vez de commitar lixo.

## Workflows

- **Atualizar dados Caixa** (`atualizar.yml`) — diário 09:37 BRT + manual (`workflow_dispatch`). Baixa, converte, valida e commita apenas se houver mudança.
- **Probe** (`probe.yml`) — diagnóstico de viabilidade (9 UFs, 2 modos de download, sem commit). Desative removendo o `schedule:` quando não quiser mais medir.

## Uso dos dados

JSON:
```python
import json, urllib.request
d = json.load(urllib.request.urlopen(
    "https://raw.githubusercontent.com/xnetinho/caixa-imoveis/master/data/PB/imoveis.json"))
for im in d["imoveis"]:
    print(im["id"], im["cidade"], im["preco"], im["desconto"])
```

TOON (formato):
```
meta{uf,gerado_em,total,fonte}:
  PB,10/10/2026 17:21,684,https://...
imoveis[684]{id,uf,cidade,bairro,endereco,preco,valor_avaliacao,desconto,financiamento,descricao,modalidade,link}:
  8444420618601,PB,ALHANDRA,LOT ASA BRANCA,"RUA VIA DE ACESSO 01, N. SN, LT 16, QD A",68240.92,130000,47.51,Não,"Casa, ...",Venda Direta Online,https://...
```

## Estrutura

```
.github/workflows/atualizar.yml   # pipeline diário
.github/workflows/probe.yml        # probe de viabilidade
scripts/pipeline.py                # download + parse + JSON + TOON
data/{UF}/imoveis.{csv,json,toon}  # dados (sempre a versão mais recente)
data/_status.json                  # status da última execução
```