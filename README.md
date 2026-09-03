# Leilão Receita - Dados

Arquivo de dados dos leilões eletrônicos (SLE - Sistema de Leilão Eletrônico)
da Receita Federal do Brasil: https://www25.receita.fazenda.gov.br/sle-sociedade/portal

Os dados são coletados diariamente por um [GitHub Action](.github/workflows/daily.yml)
que roda `scraper.py`, consultando a API JSON pública do próprio portal (sem
autenticação). A cada execução, o script busca apenas o que é novo ou mudou
desde a última coleta — não refaz a varredura completa a cada vez.

## Estrutura de `output/`

- `editais.json` — resposta bruta da lista de editais disponíveis (última execução)
- `editais_detalhado.json` — cada edital já visto, com sua lista de lotes
- `lotes.jsonl` — um JSON por linha, um por lote, com os itens/produtos (`itensDetalhesLote`)
- `leiloes_completo.json` — tudo consolidado: edital → lotes → itens
- `produtos.csv` — uma linha por item de produto, pronta para abrir em planilha
- `historico_execucoes.log` — uma linha por execução, com contagem de novos/atualizados

## Rodando localmente

```
pip install -r requirements.txt
python scraper.py
```

Use `--force` para ignorar o cache e refazer a coleta completa, ou `--help`
para ver todas as opções (filtro por situação, por edital específico, etc.).
