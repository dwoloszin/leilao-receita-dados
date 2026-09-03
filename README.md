# Leilão Receita - Dados

Arquivo de dados dos leilões eletrônicos (SLE - Sistema de Leilão Eletrônico)
da Receita Federal do Brasil: https://www25.receita.fazenda.gov.br/sle-sociedade/portal

Os dados são coletados diariamente por um [GitHub Action](.github/workflows/daily.yml)
que roda `scraper.py`, consultando a API JSON pública do próprio portal (sem
autenticação). A cada execução, o script busca apenas o que é novo ou mudou
desde a última coleta — não refaz a varredura completa a cada vez.

## Estrutura de `output/`

Versionado no git (fonte de verdade, usado para o próprio scraper decidir o
que já foi coletado):

- `editais.json` — resposta bruta da lista de editais disponíveis (última execução)
- `editais_detalhado.json` — cada edital já visto, com sua lista de lotes
- `lotes.jsonl` — um JSON por linha, um por lote, com os itens/produtos (`itensDetalhesLote`)
- `historico_execucoes.log` — uma linha por execução, com contagem de novos/atualizados

**Não** versionado (totalmente derivado dos arquivos acima — ficaria grande
demais para o git rodando todo dia). Gerado a cada execução e publicado como
artefato do [GitHub Action](../../actions) ("dados-consolidados", baixável por
90 dias), ou gere localmente rodando `python scraper.py`:

- `leiloes_completo.json` — tudo consolidado: edital → lotes → itens
- `produtos.csv` — uma linha por item de produto, pronta para abrir em planilha
- `favoritos_encontrados.csv` — produtos que bateram com `favoritos.json`

## Lista de favoritos

Edite [`favoritos.json`](favoritos.json) com as palavras-chave que quer
monitorar (ex: `PS5`, `PLAYSTATION`, `XBOX`). A cada execução, o scraper
procura essas palavras (como palavra inteira, não trecho de código de peça)
na descrição e no tipo de cada item, e lista os achados em
`favoritos_encontrados.csv` — com link direto para o lote no portal.

## Rodando localmente

```
pip install -r requirements.txt
python scraper.py
```

Use `--force` para ignorar o cache e refazer a coleta completa, ou `--help`
para ver todas as opções (filtro por situação, por edital específico, etc.).
