# Caixa CSV → JSON + TOON + Geo + Mapa

![pipeline](https://github.com/xnetinho/caixa-imoveis/actions/workflows/atualizar.yml/badge.svg)
![probe](https://github.com/xnetinho/caixa-imoveis/actions/workflows/probe.yml/badge.svg)

Baixa periodicamente, via GitHub Actions, os **CSVs oficiais de imóveis da Caixa Econômica Federal**, publica como JSON/TOON, enriquece com **coordenadas geográficas** e serve um **mapa interativo** no GitHub Pages.

## O que é publicado

| Arquivo | Conteúdo |
|---|---|
| `data/imoveis.{csv,json,toon}` | **Global (Brasil inteiro)** — todos os ~21k imóveis (campo `uf` em cada um) |
| `data/{UF}/imoveis.{csv,json,toon}` | Por estado (27 UFs) |
| `data/_geocache.json` | Cache de geocodificação (`chave_geo → {lat,lng,fonte}`), deduplicado por endereço |
| `data/_status.json` | Resultado da última execução do pipeline (por UF + global + diff) |
| `data/_geo_status.json` | Contadores da última execução de geocodificação |
| `index.html` | **Mapa interativo** (MapLibre GL JS + OpenFreeMap) publicado no GitHub Pages |

Sempre **a versão mais recente** (nomes fixos, sobrescreve).

Fonte: `https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_{UF}.csv` — um arquivo por UF (27 no total). O download é **paralelizado em workers** (matrix do GitHub Actions), cada um baixando uma UF com `curl` puro, espalhando as requisições por IPs distintos e evitando o bloqueio Radware.

> **Por que não o arquivo "geral"?** A Caixa mantém `Lista_imoveis_geral.csv` desatualizado
> (ficou dias congelado enquanto os por-UF seguiam atualizando). Por isso o pipeline usa os
> CSVs por-UF como fonte de verdade.

## Estrutura dos dados

Cada imóvel (em `imoveis` no JSON) tem os campos canônicos:

```
id, uf, cidade, bairro, endereco, preco, valor_avaliacao, desconto,
financiamento, descricao, modalidade, link
```

Após a geocodificação, ganha dois campos extras:

| Campo | Descrição |
|---|---|
| `geo` | `{lat, lng, fonte}` (`fonte` = `google` ou `osm`) — presente apenas quando localizado |
| `geo_pendente` | `true` quando ainda não foi possível geocodificar o endereço |

> **Status atual da geocodificação:** o enriquecimento é **incremental** e respeita cotas diárias
> (Google prioritário, Nominatim/OSM como fallback). Nem todos os ~21k imóveis têm `geo` ainda —
> o mapa mostra apenas os que já têm coordenadas. A cobertura cresce a cada ciclo do workflow.

## Robustez (portal protegido por Radware Bot Manager)

- **Download com `curl` puro** (sem UA de navegador): padrão comprovado por probe. Headers "estilo navegador" são bloqueados (CAPTCHA).
- **Validação de conteúdo antes de gravar**: assinatura "Lista de Im", cabeçalho reconhecível e ≥1 imóvel. UF bloqueada/falha **não sobrescreve** os arquivos bons.
- **Colunas dinâmicas**: mapeadas pelo nome do cabeçalho (normalizado). Colunas desconhecidas são preservadas como `extra_*`.
- **ID confiável**: extraído do link do imóvel (a coluna de ID pode vir em notação científica).
- **Geocodificação resiliente**: cache persistente em `_geocache.json`; Google é usado até a cota diária e, em falha real (status/exceção), cai para o OSM — nunca por esgotamento de cota (isso deixaria milhares de itens "pendentes" para o próximo ciclo em vez de estourar o timeout do job).

## Workflows

- **Atualizar dados Caixa** (`atualizar.yml`) — a cada 2h + manual (`workflow_dispatch`). Baixa os 27 CSVs por-UF em **workers paralelos** (matrix), consolida no dataset global e commita apenas se houver mudança.
- **Enriquecer geodata** (`enriquecer-geo.yml`) — **1x/dia** (cron) + manual. Geocodifica os imóveis sem coordenadas até a cota diária (Google `800` por padrão, OSM como fallback), cobrindo o passivo aos poucos e os imóveis novos de cada dia.
- **Probe** (`probe.yml`) — diagnóstico de viabilidade (9 UFs, 2 modos de download, sem commit).
- **Deploy GitHub Pages** (`pages.yml`) — publica `index.html` + `data/` a cada push na `master`.

## Mapa interativo (GitHub Pages)

`index.html` é uma aplicação autocontida que carrega `data/imoveis.json` diretamente e renderiza:

- **Mapa** MapLibre GL JS com tiles do **OpenFreeMap** (gratuito, sem chave).
- **Clustering** nativo dos imóveis por proximidade.
- **Painel de busca/filtro** por UF, cidade e preço máximo.
- **Detalhe do imóvel** com preço, desconto, descrição e link para a Caixa.
- **Google Street View** embutido da localização do imóvel.
- **Rotas** (Project OSRM) a partir da localização do usuário ou de um ponto marcado no mapa.
- **POIs próximos** (escolas, hospitais, comércio…) via Overpass API, com fallback Photon.

### Como publicar

1. Habilite **GitHub Pages** no repositório: *Settings → Pages → Source: GitHub Actions*.
2. O workflow `pages.yml` publica o site a cada push na `master`.
3. O arquivo `.nojekyll` na raiz evita que o Jekyll ignore os `data/_*.json`.

O mapa lê `data/imoveis.json` por caminho relativo (funciona no Pages a partir da raiz); há fallback via jsDelivr para `https://cdn.jsdelivr.net/gh/xnetinho/caixa-imoveis@master/data/imoveis.json`.

## Uso dos dados

JSON:
```python
import json, urllib.request
d = json.load(urllib.request.urlopen(
    "https://raw.githubusercontent.com/xnetinho/caixa-imoveis/master/data/PB/imoveis.json"))
for im in d["imoveis"]:
    geo = im.get("geo") or {}
    print(im["id"], im["cidade"], im["preco"], geo.get("lat"), geo.get("lng"))
```

## Estrutura

```
.github/workflows/atualizar.yml      # baixa CSVs por-UF (matrix) e consolida o global
.github/workflows/enriquecer-geo.yml # geocodificação diária (1x/dia, cota 800)
.github/workflows/probe.yml          # probe de viabilidade do download
.github/workflows/pages.yml          # deploy do GitHub Pages
scripts/pipeline.py                  # download + parse + JSON + TOON (por UF)
scripts/consolidar.py                # junta por-UF → global + reaplica geo do cache
scripts/geo.py                       # geocodificação (Google + OSM) + cache
scripts/geo_enriquecer.py            # orquestra o enriquecimento por UF + global
index.html                           # mapa interativo (GitHub Pages)
data/{UF}/imoveis.{csv,json,toon}    # dados por UF (sempre a versão mais recente)
data/imoveis.{csv,json,toon}         # dataset global
data/_geocache.json                  # cache de geocodificação
data/_status.json                    # status da última execução do pipeline
data/_geo_status.json                # status da última geocodificação
```
