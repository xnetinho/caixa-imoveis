# Caixa CSV → JSON + TOON + Geo + Mapa

![pipeline](https://github.com/xnetinho/caixa-imoveis/actions/workflows/atualizar.yml/badge.svg)
![probe](https://github.com/xnetinho/caixa-imoveis/actions/workflows/probe.yml/badge.svg)

Baixa periodicamente, via GitHub Actions, os **CSVs oficiais de imóveis da Caixa Econômica Federal**, publica como JSON/TOON, enriquece com **coordenadas geográficas** e serve um **mapa interativo** no GitHub Pages.

## O que é publicado

| Arquivo | Conteúdo |
|---|---|
| `data/imoveis.{json,toon}` | **Global (Brasil inteiro)** — todos os imóveis (campo `uf` em cada um) |
| `data/{UF}/imoveis.{csv,json,toon}` | Por estado (27 UFs) |
| `index.html` | **Mapa interativo** (MapLibre GL JS + OpenFreeMap) publicado no GitHub Pages |

> O CSV global (`data/imoveis.csv`) foi **removido**: era o CSV "geral" da Caixa, que fica
> desatualizado. A fonte de verdade são os CSVs por-UF.

**Estado do pipeline** (não publicado no site) fica em `.github/state/`:

| Arquivo | Papel |
|---|---|
| `.github/state/_geocache.json` | Cache de geocodificação (`chave_geo → {lat,lng,fonte}`) |
| `.github/state/_geo_daily.json` | Contador diário de chamadas de geocodificação (zera ao virar o dia) |
| `.github/state/_status.json` | Resultado da última execução do pipeline (por UF) |

Fonte: `https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_{UF}.csv` — um arquivo por UF (27 no total). O download é **paralelizado em workers** (matrix do GitHub Actions), cada um baixando uma UF com `curl` puro, espalhando as requisições por IPs distintos e evitando o bloqueio Radware.

> **Por que não o arquivo "geral"?** A Caixa mantém `Lista_imoveis_geral.csv` desatualizado
> (ficou dias congelado enquanto os por-UF seguiam atualizando). Por isso o pipeline usa os
> CSVs por-UF como fonte de verdade.

## Estrutura dos dados

Cada imóvel (em `imoveis` no JSON) tem os campos canônicos:

```
id, uf, cidade, bairro, endereco, preco, valor_avaliacao, desconto,
financiamento, descricao, modalidade, link, tipo
```

`tipo` é derivado do primeiro token de `descricao` (ex.: `casa`, `apartamento`, `terreno`,
`sala comercial`, `imovel rural`…). Após a geocodificação, ganha dois campos extras:

| Campo | Descrição |
|---|---|
| `geo` | `{lat, lng, fonte}` (`fonte` = `google` ou `osm`) — presente apenas quando localizado |
| `geo_pendente` | `true` quando ainda não foi possível geocodificar o endereço |

O dataset **global** (`data/imoveis.json`) tem no topo `atualizado_em` (quando foi produzido)
e um array `ufs[]` com `data_geracao_caixa`, `coletado_em`, `fonte` e `total` **por UF**.

## Robustez (portal protegido por Radware Bot Manager)

- **Download com `curl` puro** (sem UA de navegador): padrão comprovado por probe. Headers "estilo navegador" são bloqueados (CAPTCHA). Rotacionar User-Agent **não** ajuda — é o que dispara o bloqueio.
- **Workers paralelos com `max-parallel: 6`** + **jitter** no backoff: espalha as requisições por IPs e evita padrão rítmico.
- **Validação de conteúdo antes de gravar**: assinatura "Lista de Im", cabeçalho reconhecível e ≥1 imóvel. UF bloqueada/falha **não sobrescreve** os arquivos bons.
- **Colunas dinâmicas**: mapeadas pelo nome do cabeçalho (normalizado). Colunas desconhecidas são preservadas como `extra_*`.
- **ID confiável**: extraído do link do imóvel (a coluna de ID pode vir em notação científica).

## Workflows

- **Atualizar dados Caixa** (`atualizar.yml`) — a cada 2h + manual (`workflow_dispatch`). Baixa os 27 CSVs por-UF em **workers paralelos** (`max-parallel: 6`), consolida no dataset global e commita apenas se houver mudança. UFs que falham são marcadas no `_status.json` (dado anterior preservado).
- **Enriquecer geodata** (`enriquecer-geo.yml`) — **por evento** (roda após cada atualização de dados) + manual. Geocodifica os endereços sem coordenadas respeitando a **cota diária** (`GEO_LIMITE_GOOGLE`, default `800`, configurável por env var). O contador diário fica em `.github/state/_geo_daily.json`.
- **Probe** (`probe.yml`) — diagnóstico de viabilidade (9 UFs, 2 modos de download, sem commit).
- **Deploy GitHub Pages** (`pages.yml`) — publica `index.html` a cada push de código na `master` ou execução manual. Os dados são carregados diretamente do GitHub Raw, sem depender de redeploy.

## Mapa interativo (GitHub Pages)

`index.html` é uma aplicação autocontida que carrega os dados por estado (ou o global) e renderiza:

- **Mapa** MapLibre GL JS com tiles do **OpenFreeMap** (gratuito, sem chave).
- **Clustering** nativo dos imóveis por proximidade.
- **Ícones por tipo de imóvel** (`tipo` → Apartamento/Casa/Terreno/Comercial/Outros) e **POIs por categoria**.
- **Carga por UF**: detecta o estado pelo IP (1ª visita) ou usa a última seleção (`localStorage`), carregando diretamente `data/{UF}/imoveis.json` do GitHub Raw em vez do global inteiro.
- **Detalhe do imóvel** com preço, desconto, descrição, link para a Caixa e matrícula.
- **Google Street View** embutido + **rotas** (Project OSRM) + **POIs próximos** (Overpass → Photon).

### Como publicar

1. Habilite **GitHub Pages** no repositório: *Settings → Pages → Source: GitHub Actions*.
2. O workflow `pages.yml` publica `index.html` em pushes de código na `master` ou por execução manual.

O mapa lê `data/{UF}/imoveis.json` (ou `data/imoveis.json`) diretamente de `raw.githubusercontent.com`; atualizações dos dados não dependem de redeploy do Pages.

## Uso dos dados

JSON:
```python
import json, urllib.request
d = json.load(urllib.request.urlopen(
    "https://raw.githubusercontent.com/xnetinho/caixa-imoveis/master/data/PB/imoveis.json"))
for im in d["imoveis"]:
    geo = im.get("geo") or {}
    print(im["id"], im["cidade"], im["tipo"], im["preco"], geo.get("lat"), geo.get("lng"))
```

## Estrutura

```
.github/workflows/atualizar.yml      # baixa CSVs por-UF (matrix) e consolida o global
.github/workflows/enriquecer-geo.yml # geocodificação por evento (cota diária)
.github/workflows/probe.yml          # probe de viabilidade do download
.github/workflows/pages.yml          # deploy do GitHub Pages
.github/state/_geocache.json         # cache de geocodificação
.github/state/_geo_daily.json        # contador diário de chamadas
.github/state/_status.json           # status da última execução do pipeline
scripts/pipeline.py                  # download + parse + JSON + TOON (por UF)
scripts/consolidar.py                # junta por-UF → global + reaplica geo do cache
scripts/geo.py                       # geocodificação (Google + OSM) + cache + cota diária
scripts/geo_enriquecer.py            # orquestra o enriquecimento por UF + global
index.html                           # mapa interativo (GitHub Pages)
data/{UF}/imoveis.{csv,json,toon}    # dados por UF (sempre a versão mais recente)
data/imoveis.{json,toon}             # dataset global
```
