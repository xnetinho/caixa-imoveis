# Probe — Viabilidade de download do CSV da Caixa no GitHub Actions

Testa, **de dentro de um runner do GitHub Actions (IP de datacenter Azure)**, se o download direto dos CSVs oficiais funciona:

```
https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_{UF}.csv
```

## Por quê
O portal usa Radware Bot Manager. Confirmamos que o download funciona do IP residencial/ISP do usuário (inclusive com `curl` puro), mas IP de datacenter + requisição sem JavaScript pode ser desafiado com CAPTCHA (`validate.perfdrive.com`). Este probe fecha essa dúvida **com evidência real antes de investir na automação completa**.

## O que ele faz
- Matrix de 9 UFs (PB, SP, RJ, MG, BA, RS, PR, SC, CE), cada uma em job paralelo
- 2 tentativas por UF: (T1) `curl` puro, (T2) headers estilo navegador com `Referer`
- **Validação de conteúdo**: procura a assinatura "Lista de Im..." no arquivo — não confia só no HTTP 200 (Radware devolve 200 com HTML de CAPTCHA)
- Log mostra: HTTP code, tamanho, tipo de arquivo, título/header/1ª linha de imóvel e total de linhas
- **Não commita nada** (permissions: read) — é 100% de medição

## Como testar
1. Crie um repositório **público** novo (ex.: `caixa-csv-probe`)
2. Copie `.github/workflows/probe.yml`
3. Aba **Actions → Probe - Viabilidade download CSV Caixa → Run workflow**
4. Leia o log de cada UF: `VEREDICTO: OK` ou `BLOQUEADO`

## Como interpretar
- **Todas OK** → GitHub Actions viável; partimos pra automação completa (27 UFs → JSON+TOON)
- **Blokqueadas parcialmente** → adotar retry/delay entre UFs no pipeline real
- **Todas BLOQUEADAS** → plano B: **self-hosted runner** na sua VM (mesma conta GitHub, dados ainda publicados no repo, mas o download sai do seu IP)

## Limpeza
O schedule roda 1x/dia apenas enquanto você quiser medir; desative deletando o arquivo ou removendo o `schedule:` do frontmatter.