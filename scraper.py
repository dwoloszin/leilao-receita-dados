#!/usr/bin/env python3
"""
Scraper do Sistema de Leilão Eletrônico (SLE) da Receita Federal.
https://www25.receita.fazenda.gov.br/sle-sociedade/portal

Coleta todos os editais (leilões) disponíveis, os lotes de cada edital e os
itens/produtos de cada lote, usando a API JSON pública que alimenta a SPA
Angular do portal (nenhuma autenticação é necessária).

Endpoints usados:
  GET api/editais-disponiveis                                -> lista de editais por situação
  GET api/edital/{unidade}/{numero}/{exercicio}               -> detalhe do edital + lista de lotes
  GET api/lote/{unidade}/{numero}/{exercicio}/{lote}           -> detalhe do lote (itens/produtos)

Modo padrão (incremental): a cada execução, a lista de editais é sempre
rebuscada (1 requisição barata), mas o detalhe de um edital só é buscado de
novo se algo relevante mudou (situação, datas, quantidade de lotes) em relação
ao que já está salvo em editais_detalhado.json; e o detalhe de um lote só é
buscado de novo se ele for novo ou se sua situação/avisos/erratas/valor
mudaram em relação ao que já está salvo em lotes.jsonl. Dados de execuções
anteriores nunca são descartados: o resultado final é a união do que já
existia com o que mudou/apareceu agora. Use --force para ignorar o cache e
refazer a varredura completa.

Saídas (em --output-dir, padrão "./output"):
  editais.json             -> resposta bruta de api/editais-disponiveis (da última execução)
  editais_detalhado.json   -> cada edital já visto, com sua lista de lotes (sem detalhe de item)
  lotes.jsonl              -> um JSON por linha, um por lote, com itensDetalhesLote (log append-only)
  leiloes_completo.json    -> tudo consolidado: edital -> lotes -> itens
  produtos.csv             -> uma linha por item de produto (achatado), pronta para planilha
  historico_execucoes.log  -> uma linha por execução, com contagens (novos/atualizados/etc.)
"""
import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

BASE_URL = "https://www25.receita.fazenda.gov.br/sle-sociedade/"

SITUACOES = {
    1: "Em Preparação",
    2: "Disponibilizado",
    3: "Aberto para Proposta",
    4: "Fechado para Proposta",
    5: "Aberta Sessão Pública (em classificação)",
    6: "Aberta Sessão Pública (classificação encerrada)",
    7: "Aberta Sessão para Lance",
    8: "Fechada Sessão para Lance",
    9: "Fechada Sessão para Lance (lotes adjudicados)",
    10: "Encerrada Sessão Pública",
    11: "Encerrado",
    12: "Homologado",
    13: "Sessão Pública Suspensa",
    14: "Cancelado",
    15: "Encerrada Sessão Pública (ata publicada)",
    16: "Excluído",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; leilao-receita-scraper/1.0)",
    "Accept": "application/json, text/plain, */*",
}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def get_json(session: requests.Session, path: str, params=None, tries=5, delay=0.3, timeout=30):
    url = BASE_URL + path
    last_exc = None
    for attempt in range(1, tries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            if resp.status_code == 200:
                time.sleep(delay)
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(delay * attempt * 2)
                continue
            if 400 <= resp.status_code < 500:
                # Erro de regra de negócio (ex: lote em estado que a API recusa
                # detalhar agora) — não é transitório, tentar de novo não ajuda.
                try:
                    detalhe = resp.json().get("message")
                except (ValueError, AttributeError):
                    detalhe = resp.text[:200]
                raise RuntimeError(f"{resp.status_code} ao buscar {url}: {detalhe}")
            resp.raise_for_status()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_exc = exc
            time.sleep(delay * attempt)
    raise RuntimeError(f"Falha ao buscar {url} params={params}: {last_exc}")


def parse_edle(edle: str):
    unidade, numero, exercicio = edle.split("/")
    return unidade, numero, exercicio


def url_lote(edle: str, nr_atribuido) -> str:
    return f"https://www25.receita.fazenda.gov.br/sle-sociedade/portal/edital/{edle}/lote/{nr_atribuido}"


def carregar_favoritos(path: str):
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    return [p.strip().upper() for p in config.get("palavrasChave", []) if p.strip()]


def listar_editais_disponiveis(session: requests.Session, delay: float):
    data = get_json(session, "api/editais-disponiveis", delay=delay)
    editais = []
    vistos = set()
    for situacao in data.get("situacoes", []):
        codigo_situacao = situacao.get("situacao")
        for item in situacao.get("lista", []) or []:
            edle = item.get("edle")
            if edle in vistos:
                continue
            vistos.add(edle)
            item = dict(item)
            item["situacaoCodigo"] = codigo_situacao
            item["situacaoLabel"] = SITUACOES.get(codigo_situacao, str(codigo_situacao))
            editais.append(item)
    return data, editais


def detalhar_edital(session: requests.Session, edle: str, delay: float):
    unidade, numero, exercicio = parse_edle(edle)
    return get_json(session, f"api/edital/{unidade}/{numero}/{exercicio}", delay=delay)


def detalhar_lote(session: requests.Session, edle: str, nr_atribuido, delay: float):
    unidade, numero, exercicio = parse_edle(edle)
    return get_json(session, f"api/lote/{unidade}/{numero}/{exercicio}/{nr_atribuido}", delay=delay)


def _edital_mudou(fresh: dict, cached: dict) -> bool:
    """Compara o resumo vindo de api/editais-disponiveis com o detalhe já salvo."""
    if cached is None:
        return True
    campos = ("dataInicioPropostas", "dataFimPropostas", "dataAberturaLances")
    for campo in campos:
        if fresh.get(campo) != cached.get(campo):
            return True
    if fresh.get("codigoSituacao") != cached.get("situacaoCodigo"):
        return True
    if fresh.get("lotes") is not None and fresh.get("lotes") != len(cached.get("listaLotes") or []):
        return True
    return False


def _lote_mudou(fresh_resumo: dict, cached_detalhe: dict) -> bool:
    """Compara o resumo do lote (dentro do detalhe do edital) com o detalhe de lote já salvo.

    Só usa campos que existem em ambas as respostas (api/edital/.../ e
    api/lote/.../): "valorAvaliacao", por exemplo, só existe no primeiro, então
    comparar esse campo aqui sempre daria "mudou" e anularia o cache.
    """
    if cached_detalhe is None:
        return True
    campos = ("situacaoLote", "nrAvisos", "nrErratas", "valorMinimo")
    for campo in campos:
        if fresh_resumo.get(campo) != cached_detalhe.get(campo):
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description="Scraper do SLE Sociedade (Receita Federal)")
    ap.add_argument("--output-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"))
    ap.add_argument("--workers", type=int, default=4, help="Downloads simultâneos de detalhes de lote")
    ap.add_argument("--delay", type=float, default=0.25, help="Pausa (s) após cada requisição bem-sucedida")
    ap.add_argument("--situacoes", default=None, help="Códigos de situação a incluir, separados por vírgula (ex: 2,3,7). Padrão: todas")
    ap.add_argument("--edle", default=None, help="Processar apenas um edital específico (formato unidade/numero/exercicio)")
    ap.add_argument("--limit-editais", type=int, default=None, help="Limitar quantidade de editais verificados (teste)")
    ap.add_argument("--limit-lotes", type=int, default=None, help="Limitar quantidade de lotes com detalhe buscado nesta execução (teste)")
    ap.add_argument("--only-editais", action="store_true", help="Não buscar detalhe de lote (mais rápido, sem itens de produto)")
    ap.add_argument("--force", action="store_true", help="Ignora o cache e refaz a varredura completa (edital e lote)")
    ap.add_argument(
        "--favoritos-file",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "favoritos.json"),
        help="Arquivo JSON com a lista de palavras-chave a monitorar (ex: PS5, Playstation)",
    )
    args = ap.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    session = make_session()

    editais_detalhado_path = os.path.join(args.output_dir, "editais_detalhado.json")
    lotes_path = os.path.join(args.output_dir, "lotes.jsonl")

    edital_cache = {}
    if not args.force and os.path.exists(editais_detalhado_path):
        with open(editais_detalhado_path, encoding="utf-8") as f:
            for detalhe in json.load(f):
                edital_cache[detalhe.get("edle")] = detalhe
    lote_cache = {} if args.force else _carregar_lotes_jsonl(lotes_path)

    print("Buscando lista de editais disponíveis (api/editais-disponiveis)...")
    raw, editais = listar_editais_disponiveis(session, args.delay)

    with open(os.path.join(args.output_dir, "editais.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

    if args.situacoes:
        codigos = {int(c) for c in args.situacoes.split(",")}
        editais = [e for e in editais if e.get("situacaoCodigo") in codigos]

    if args.edle:
        editais = [e for e in editais if e.get("edle") == args.edle]

    if args.limit_editais:
        editais = editais[: args.limit_editais]

    print(f"{len(editais)} edital(is) selecionado(s) nesta execução (de {len(edital_cache)} já conhecidos no total). Verificando mudanças...")

    contagem_edital = {"novo": 0, "atualizado": 0, "sem_mudanca": 0}
    editais_selecionados = []

    for i, edital in enumerate(editais, 1):
        edle = edital["edle"]
        cached = edital_cache.get(edle)
        if args.force or _edital_mudou(edital, cached):
            try:
                detalhe = detalhar_edital(session, edle, args.delay)
            except RuntimeError as exc:
                print(f"  [{i}/{len(editais)}] ERRO ao detalhar edital {edle}: {exc}", file=sys.stderr)
                continue
            detalhe["situacaoCodigo"] = edital.get("situacaoCodigo")
            detalhe["situacaoLabel"] = edital.get("situacaoLabel")
            status = "novo" if cached is None else "atualizado"
            contagem_edital[status] += 1
            edital_cache[edle] = detalhe
            qt_lotes = len(detalhe.get("listaLotes") or [])
            print(f"  [{i}/{len(editais)}] {edle} - {status} - {detalhe.get('orgao')} / {detalhe.get('cidade')} - {qt_lotes} lote(s)")
        else:
            contagem_edital["sem_mudanca"] += 1
            detalhe = cached
        # Mesmo quando o edital não mudou, seus lotes ainda podem não ter sido
        # buscados (ex.: execução anterior interrompida) — por isso todo
        # edital selecionado entra na checagem de lotes, não só os que mudaram.
        editais_selecionados.append(detalhe)

    editais_detalhados = list(edital_cache.values())
    with open(editais_detalhado_path, "w", encoding="utf-8") as f:
        json.dump(editais_detalhados, f, ensure_ascii=False, indent=2)

    print(
        f"Editais: {contagem_edital['novo']} novo(s), {contagem_edital['atualizado']} atualizado(s), "
        f"{contagem_edital['sem_mudanca']} sem mudança (reaproveitado do cache)."
    )

    favoritos = carregar_favoritos(args.favoritos_file)

    if args.only_editais:
        print("Opção --only-editais ativada: pulando detalhe de lotes/produtos.")
        _consolidar(args.output_dir, editais_detalhados, lote_cache, favoritos)
        _registrar_execucao(args.output_dir, contagem_edital, {"novo": 0, "atualizado": 0, "sem_mudanca": 0, "erro": 0})
        print("Concluído.")
        return

    pendentes = []
    contagem_lote = {"novo": 0, "atualizado": 0, "sem_mudanca": 0}
    for edital in editais_selecionados:
        edle = edital["edle"]
        for lote in edital.get("listaLotes") or []:
            nr = lote.get("nrAtribuido")
            key = (edle, str(nr))
            cached_lote = lote_cache.get(key)
            if args.force or _lote_mudou(lote, cached_lote):
                status = "novo" if cached_lote is None else "atualizado"
                contagem_lote[status] += 1
                pendentes.append((edle, nr))
            else:
                contagem_lote["sem_mudanca"] += 1

    if args.limit_lotes:
        pendentes = pendentes[: args.limit_lotes]

    total_lotes = sum(len(e.get("listaLotes") or []) for e in editais_detalhados)
    print(
        f"Lotes: {total_lotes} no total conhecidos, {contagem_lote['novo']} novo(s), "
        f"{contagem_lote['atualizado']} mudaram, {contagem_lote['sem_mudanca']} sem mudança (não serão rebuscados)."
    )
    if pendentes and args.workers > 1:
        print(f"Buscando {len(pendentes)} lote(s) agora, com {args.workers} downloads simultâneos, ~{args.delay}s de pausa por requisição.")

    write_lock = threading.Lock()
    contador = {"ok": 0, "erro": 0}
    total = len(pendentes)

    def worker(edle, nr_atribuido):
        try:
            detalhe = detalhar_lote(session, edle, nr_atribuido, args.delay)
            return edle, nr_atribuido, detalhe, None
        except RuntimeError as exc:
            return edle, nr_atribuido, None, str(exc)

    if pendentes:
        with open(lotes_path, "a", encoding="utf-8") as fout, ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(worker, edle, nr) for edle, nr in pendentes]
            for i, fut in enumerate(as_completed(futures), 1):
                edle, nr_atribuido, detalhe, erro = fut.result()
                if erro:
                    contador["erro"] += 1
                    print(f"  [{i}/{total}] ERRO lote {edle}/{nr_atribuido}: {erro}", file=sys.stderr)
                    continue
                contador["ok"] += 1
                with write_lock:
                    fout.write(json.dumps(detalhe, ensure_ascii=False) + "\n")
                    fout.flush()
                if i % 25 == 0 or i == total:
                    print(f"  [{i}/{total}] lotes processados (ok={contador['ok']}, erro={contador['erro']})")

    print("Consolidando resultados...")
    lotes_detalhe = _carregar_lotes_jsonl(lotes_path)
    if pendentes:
        _compactar_lotes_jsonl(lotes_path, lotes_detalhe)
    _consolidar(args.output_dir, editais_detalhados, lotes_detalhe, favoritos)
    contagem_lote["erro"] = contador["erro"]
    _registrar_execucao(args.output_dir, contagem_edital, contagem_lote)
    print("Concluído.")


def _registrar_execucao(output_dir: str, contagem_edital: dict, contagem_lote: dict):
    linha = {
        "quando": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "editais": contagem_edital,
        "lotes": contagem_lote,
    }
    with open(os.path.join(output_dir, "historico_execucoes.log"), "a", encoding="utf-8") as f:
        f.write(json.dumps(linha, ensure_ascii=False) + "\n")


def _compactar_lotes_jsonl(path: str, lotes_detalhe: dict):
    """Reescreve lotes.jsonl com apenas o registro mais recente de cada lote.

    lotes.jsonl é usado como log de append durante a execução (mais seguro em
    caso de interrupção), mas sem compactação ele cresceria para sempre a cada
    mudança de situação de um lote — um problema real quando o arquivo é
    versionado em um repositório git que roda diariamente. Compactar no final
    de cada execução mantém o tamanho do arquivo proporcional à quantidade de
    lotes existentes, não à quantidade de atualizações ao longo do tempo.
    """
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for rec in lotes_detalhe.values():
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp_path, path)


def _carregar_lotes_jsonl(path: str):
    resultado = {}
    if not os.path.exists(path):
        return resultado
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            resultado[(rec.get("edle"), str(rec.get("nrAtribuido")))] = rec
    return resultado


def _consolidar(output_dir: str, editais_detalhados, lotes_detalhe: dict, favoritos=None):
    favoritos = favoritos or []
    completo = []
    linhas_csv = []
    linhas_favoritos = []

    for edital in editais_detalhados:
        edle = edital.get("edle")
        edital_out = dict(edital)
        lotes_out = []
        for lote in edital.get("listaLotes") or []:
            nr = lote.get("nrAtribuido")
            lote_out = dict(lote)
            detalhe = lotes_detalhe.get((edle, str(nr)))
            itens = []
            if detalhe:
                itens = detalhe.get("itensDetalhesLote") or []
                lote_out["itensDetalhesLote"] = itens
            lotes_out.append(lote_out)

            registros = itens if itens else [{}]
            for item in registros:
                linha = _linha_csv(edital, lote_out, item)
                linhas_csv.append(linha)
                palavra = _achar_favorito(favoritos, lote_out, item)
                if palavra:
                    linha_favorito = dict(linha)
                    linha_favorito["palavraChave"] = palavra
                    linhas_favoritos.append(linha_favorito)
        edital_out["listaLotes"] = lotes_out
        completo.append(edital_out)

    with open(os.path.join(output_dir, "leiloes_completo.json"), "w", encoding="utf-8") as f:
        json.dump(completo, f, ensure_ascii=False, indent=2)

    colunas = [
        "edital", "edle", "situacaoLabel", "orgao", "cidade",
        "dataInicioPropostas", "dataFimPropostas", "dataAberturaLances",
        "lote", "tipoLote", "situacaoLote", "valorMinimo", "valorAvaliacao",
        "descricaoItem", "quantidade", "unidadeMedida", "recintoArmazenador", "nrReferencia", "url",
    ]
    with open(os.path.join(output_dir, "produtos.csv"), "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=colunas)
        writer.writeheader()
        for linha in linhas_csv:
            writer.writerow(linha)

    with open(os.path.join(output_dir, "favoritos_encontrados.csv"), "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=colunas + ["palavraChave"])
        writer.writeheader()
        for linha in linhas_favoritos:
            writer.writerow(linha)

    print(f"  {len(completo)} edital(is), {len(linhas_csv)} linha(s) de produto/lote em produtos.csv")
    if favoritos:
        print(f"  {len(linhas_favoritos)} item(ns) batendo com a lista de favoritos ({', '.join(favoritos)}) em favoritos_encontrados.csv")


def _achar_favorito(favoritos, lote: dict, item: dict):
    if not favoritos:
        return None
    texto = " ".join(str(v) for v in (lote.get("tipo"), item.get("descricao")) if v).upper()
    for palavra in favoritos:
        # \b evita falso positivo tipo "PS5" batendo dentro de um código de
        # peça como "LPS523" — exige que a palavra apareça isolada no texto.
        if re.search(r"\b" + re.escape(palavra) + r"\b", texto):
            return palavra
    return None


def _linha_csv(edital: dict, lote: dict, item: dict):
    return {
        "edital": edital.get("edital"),
        "edle": edital.get("edle"),
        "situacaoLabel": edital.get("situacaoLabel"),
        "orgao": edital.get("orgao"),
        "cidade": edital.get("cidade"),
        "dataInicioPropostas": edital.get("dataInicioPropostas"),
        "dataFimPropostas": edital.get("dataFimPropostas"),
        "dataAberturaLances": edital.get("dataAberturaLances"),
        "lote": lote.get("nrAtribuido"),
        "tipoLote": lote.get("tipo"),
        "situacaoLote": lote.get("situacaoLote"),
        "valorMinimo": lote.get("valorMinimo"),
        "valorAvaliacao": lote.get("valorAvaliacao"),
        "descricaoItem": item.get("descricao"),
        "quantidade": item.get("quantidade"),
        "unidadeMedida": item.get("unMedida"),
        "recintoArmazenador": item.get("recintoArmazenador"),
        "nrReferencia": item.get("nrReferencia"),
        "url": url_lote(edital.get("edle"), lote.get("nrAtribuido")),
    }


if __name__ == "__main__":
    main()
