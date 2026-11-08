#!/usr/bin/env python3
"""
webscraping.py — PreparAI (Coleta de Manuais de Campanha — BDEx)
=================================================================
Baixa automaticamente os Manuais de Campanha do Exército Brasileiro a partir da
Biblioteca Digital do Exército (BDEx - https://bdex.eb.mil.br/jspui/), alimentando
a pasta `Arquivos/` que o `scripts/pipeline.py` consome.

Fluxo de execução:
  1. Lê o facet 'Tipo de Documento' da busca (opcional via --listar-tipos)
  2. Pagina a busca filtrada por 'Manual de Campanha' e coleta metadados e handles
  3. Visita a página de cada item e localiza o link do PDF (bitstream), com retomada via JSON
  4. Salva o catálogo completo em `webscraping/Coleta/manuais_campanha.json`
  5. Remove duplicatas (priorizando a edição mais recente) e classifica revogados
  6. Baixa os PDFs — vigentes em `Arquivos/`, revogados em `Arquivos/Revogados/`
  7. Verifica a integridade dos arquivos no disco e limpa nomes codificados
  8. (Opcional via --pipeline) Executa o `scripts/pipeline.py` para gerar os XMLs em `Gerados/`

Uso direto:
    python webscraping/webscraping.py
    python webscraping/webscraping.py --ano-minimo 2010
    python webscraping/webscraping.py --limite 5 --pipeline
    python webscraping/webscraping.py --manutencao
"""

import argparse
import http.cookiejar
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

# Permite importar pacotes da .venv local caso o script seja chamado pelo python do sistema
_VENV_SITE = Path(__file__).resolve().parent.parent / ".venv" / "lib" / "python3.12" / "site-packages"
if _VENV_SITE.exists() and str(_VENV_SITE) not in sys.path:
    sys.path.insert(0, str(_VENV_SITE))

from bs4 import BeautifulSoup

try:
    import requests
except ImportError:
    requests = None


# =============================================================================
# 1. CONFIGURAÇÃO E CAMINHOS ANCORADOS NA RAIZ DO PROJETO
# =============================================================================

URL = "https://bdex.eb.mil.br"
RPP = 100                      # resultados por página (máximo aceito pelo DSpace)
PAUSA_PADRAO = 1.0             # segundos entre requisições
ANO_MINIMO_PADRAO = 2010       # recorte padrão para manuais modernos (EB20/EB70)

PARAMS_MANUAL = {              # formato para APLICAR um filtro novo
    "filterquery": "Manual de Campanha",
    "filtername": "typeArchive",
    "filtertype": "equals",
}

PARAMS_BUSCA = {               # formato do filtro JÁ ATIVO (usado na paginação)
    "query": "",
    "filter_field_1": "typeArchive",
    "filter_type_1": "equals",
    "filter_value_1": "Manual de Campanha",
    "sort_by": "score",
    "order": "desc",
    "rpp": RPP,
    "etal": 0,
}


def achar_raiz(marcador: str = ".git") -> Path:
    """
    Sobe na árvore de diretórios a partir da localização deste script
    até encontrar a pasta que contém o marcador (.git ou configuracoes.yaml).
    """
    base_script = Path(__file__).resolve().parent
    for pasta in [base_script, *base_script.parents]:
        if (pasta / marcador).exists() or (pasta / "configuracoes.yaml").exists():
            return pasta
    return base_script.parent


RAIZ = achar_raiz()
PASTA_PDF = RAIZ / "Arquivos"
PASTA_REVOGADOS = PASTA_PDF / "Revogados"
PASTA_SCRAPING = RAIZ / "webscraping"
PASTA_COLETA = PASTA_SCRAPING / "Coleta"
ARQUIVO_JSON = PASTA_COLETA / "manuais_campanha.json"


def preparar_diretorios() -> None:
    """Garante a existência das pastas de saída."""
    for pasta in (PASTA_PDF, PASTA_REVOGADOS, PASTA_COLETA):
        pasta.mkdir(parents=True, exist_ok=True)


# =============================================================================
# 2. SESSÃO HTTP (requests.Session com fallback nativo urllib)
# =============================================================================

class _UrllibResponse:
    """Adaptador compatível com requests.Response usando urllib da biblioteca padrão."""

    def __init__(self, resp: urllib.response.addinfourl, stream: bool = False):
        self._resp = resp
        self.status_code = getattr(resp, "status", 200)
        self.headers = {k.lower(): v for k, v in resp.headers.items()}
        self.encoding = "utf-8"
        self._stream = stream
        self._body = None if stream else resp.read()

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}")

    @property
    def text(self) -> str:
        if self._body is None:
            self._body = self._resp.read()
        return self._body.decode(self.encoding or "utf-8", errors="replace")

    def iter_content(self, chunk_size: int = 65536):
        while True:
            pedaco = self._resp.read(chunk_size)
            if not pedaco:
                break
            yield pedaco

    def close(self) -> None:
        self._resp.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class _UrllibSession:
    """Fallback para requests.Session caso o pacote requests não esteja instalado."""

    def __init__(self):
        self.headers = {}
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))

    def get(self, url: str, params: dict | None = None, timeout: int = 60, stream: bool = False):
        if params:
            query = urllib.parse.urlencode(params)
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{query}"
        req = urllib.request.Request(url, headers=self.headers)
        resp = self._opener.open(req, timeout=timeout)
        return _UrllibResponse(resp, stream=stream)


def criar_sessao():
    """Cria e configura a sessão HTTP com User-Agent identificável."""
    sess = requests.Session() if requests is not None else _UrllibSession()
    sess.headers.update({
        "User-Agent": "Pesquisa-Academica"
    })
    return sess


sessao = criar_sessao()


# =============================================================================
# 3. FUNÇÕES AUXILIARES
# =============================================================================

def sem_acento(texto: str) -> str:
    """Remove acentos para comparação insensível a acentuação."""
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


def ano_de(doc: dict) -> int:
    """Ano do documento como int. Devolve 0 quando ausente ou inválido."""
    ano = (doc.get("data") or "")[:4]
    return int(ano) if ano.isdigit() else 0


# Caracteres proibidos em nomes de arquivo no Windows/Linux
RE_INVALIDOS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def nome_seguro(nome: str, max_len: int = 150) -> str:
    """Decodifica o percent-encoding e remove caracteres inválidos."""
    nome = unquote(nome)
    nome = RE_INVALIDOS.sub("_", nome)
    nome = re.sub(r"\s+", " ", nome).strip()

    # Remove espaços soltos imediatamente antes da extensão (ex: "Manual .pdf" -> "Manual.pdf")
    nome = re.sub(r"\s+\.([a-zA-Z0-9]{2,5})$", r".\1", nome)

    # Trunca preservando a extensão
    if len(nome) > max_len:
        base, _, ext = nome.rpartition(".")
        if base and len(ext) <= 5:
            nome = base[: max_len - len(ext) - 1].rstrip() + "." + ext
        else:
            nome = nome[:max_len]

    return nome or "sem_nome.pdf"


# Nota importante: não usamos \b porque '_' faz parte de \w em Regex, o que
# impedia detectar arquivos como 'EB20-MC-10.204_revogado.pdf'.
RE_REVOGADO = re.compile(r"(?<![A-Z])REVOGAD[OA]S?(?![A-Z])")


def esta_revogado(doc: dict) -> bool:
    """Procura a marca de revogação no nome do arquivo e no título."""
    for campo in (doc.get("nome_arquivo") or "", doc.get("titulo") or ""):
        if RE_REVOGADO.search(sem_acento(campo).upper()):
            return True
    return False


# =============================================================================
# 4. FUNÇÕES DE COLETA
# =============================================================================

def ler_facet_tipos(pagina: int = 0) -> list[tuple[str, str, int]]:
    """
    Lê uma página do facet 'Tipo de Documento'.
    Devolve lista de (exibido, valor, contagem). Lista vazia se não houver facet.
    """
    r = sessao.get(
        f"{URL}/jspui/simple-search",
        params={"query": "", "typeArchive_page": pagina},
        timeout=60,
    )
    r.raise_for_status()
    r.encoding = "utf-8"

    bloco = BeautifulSoup(r.text, "html.parser").find("div", id="facet_typeArchive")
    if bloco is None:
        return []

    resultado = []
    for li in bloco.find_all("li", class_="list-group-item"):
        link = li.find("a")
        badge = li.find("span", class_="badge")

        # A linha do "próximo >" tem link mas não tem badge
        if link is None or badge is None:
            continue

        # O texto visível pode vir truncado; o valor íntegro está no href
        params = parse_qs(urlparse(link["href"]).query)
        valor = params.get("filterquery", [None])[0]
        if valor is None:
            continue

        resultado.append((
            link.get_text(strip=True),
            valor,
            int(badge.get_text(strip=True)),
        ))

    return resultado


RE_TOTAL = re.compile(r"^Resultado \d+-\d+ de (\d+)")


def contar_total(soup: BeautifulSoup) -> int | None:
    """Extrai o total de resultados do texto 'Resultado 1-10 de 273.'"""
    for txt in soup.stripped_strings:
        m = RE_TOTAL.match(txt)
        if m:
            return int(m.group(1))
    return None


def extrair_resultados(soup: BeautifulSoup) -> list[dict]:
    """
    Extrai os documentos de uma página de resultados.
    Devolve lista de dicts com data, titulo, autor e handle.
    """
    tabela = soup.find("table", class_="table")
    if tabela is None:
        return []

    docs = []
    for tr in tabela.find_all("tr"):
        # A linha de cabeçalho só tem <th>, então não passa daqui
        td_titulo = tr.find("td", attrs={"headers": "t2"})
        if td_titulo is None:
            continue

        link = td_titulo.find("a")
        if link is None:
            continue

        td_data = tr.find("td", attrs={"headers": "t1"})
        td_autor = tr.find("td", attrs={"headers": "t3"})

        docs.append({
            "data": td_data.get_text(strip=True) if td_data else None,
            "titulo": link.get_text(strip=True),
            "autor": td_autor.get_text(strip=True) if td_autor else None,
            "handle": link["href"],
        })

    return docs


def achar_pdf(handle: str) -> tuple[str | None, str | None]:
    """Dado o handle de um item, devolve (url_pdf, nome_arquivo) ou (None, None)."""
    url_item = urljoin(URL, handle)
    r = sessao.get(url_item, timeout=60)
    r.raise_for_status()
    r.encoding = "utf-8"

    soup = BeautifulSoup(r.text, "html.parser")

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/bitstream/" in href and href.lower().endswith(".pdf"):
            # urljoin cobre href relativo E absoluto
            url_pdf = urljoin(url_item, href)
            nome = nome_seguro(href.rsplit("/", 1)[-1])
            return url_pdf, nome

    return None, None


def baixar(url_pdf: str, nome_arquivo: str, pasta_destino: Path) -> tuple[Path | None, str]:
    """Baixa um PDF com cache, verificação de tipo e arquivo temporário."""
    destino = pasta_destino / nome_arquivo

    if destino.exists() and destino.stat().st_size > 0:
        return destino, "já existe"

    with sessao.get(url_pdf, timeout=300, stream=True) as resp:
        resp.raise_for_status()

        # Uma página de erro HTML com status 200 seria salva como .pdf corrompido
        tipo = resp.headers.get("content-type", "")
        if "pdf" not in tipo.lower():
            return None, f"não é PDF ({tipo})"

        # Grava num temporário: conexão interrompida não deixa .pdf truncado
        temp = destino.with_name(destino.name + ".parcial")
        with open(temp, "wb") as f:
            for pedaco in resp.iter_content(chunk_size=65536):
                f.write(pedaco)
        temp.replace(destino)

    return destino, f"{destino.stat().st_size / 1024 / 1024:.1f} MB"


# =============================================================================
# 5. ETAPAS DO FLUXO DE EXECUÇÃO
# =============================================================================

def listar_catalogo_tipos(pausa: float = PAUSA_PADRAO) -> list[tuple[str, str, int]]:
    """Percorre as páginas do facet para listar todos os tipos disponíveis no acervo."""
    print("\n" + "=" * 70)
    print("ETAPA 1 — Catálogo de Tipos de Documento na BDEx")
    print("=" * 70)

    todos_tipos = []
    vistos = set()

    for pagina in range(20):
        lote = ler_facet_tipos(pagina)
        if not lote:
            break

        novos = [t for t in lote if t[1] not in vistos]
        if not novos:  # página repetida: acabou a paginação
            break

        vistos.update(t[1] for t in novos)
        todos_tipos.extend(novos)
        time.sleep(pausa)

    print(f"{len(todos_tipos)} tipos | soma: {sum(t[2] for t in todos_tipos)} documentos\n")
    for exibido, valor, n in sorted(todos_tipos, key=lambda t: -t[2]):
        marca = "  <<<" if "manual de campanha" in valor.lower() else ""
        print(f"{n:>6}  {valor}{marca}")

    return todos_tipos


def carregar_catalogo_existente() -> list[dict]:
    """Carrega o catálogo salvo em JSON no disco, se existir."""
    if ARQUIVO_JSON.exists():
        try:
            with open(ARQUIVO_JSON, "r", encoding="utf-8") as f:
                dados = json.load(f)
            if isinstance(dados, list):
                return dados
        except Exception as e:
            print(f"  ⚠️ Aviso: não foi possível ler {ARQUIVO_JSON.name} ({e}).")
    return []


def salvar_catalogo(manuais: list[dict], silencioso: bool = False) -> None:
    """Salva o catálogo de manuais em JSON com acentuação preservada."""
    PASTA_COLETA.mkdir(parents=True, exist_ok=True)
    with open(ARQUIVO_JSON, "w", encoding="utf-8") as f:
        json.dump(manuais, f, ensure_ascii=False, indent=2)
    if not silencioso:
        print(f"💾 Catálogo salvo: {len(manuais)} registros em {ARQUIVO_JSON}")


def coletar_lista_manuais(pausa: float = PAUSA_PADRAO) -> list[dict]:
    """Pagina a busca filtrada por 'Manual de Campanha' e retorna a lista de itens."""
    print("\n" + "=" * 70)
    print("ETAPA 2 — Listagem dos Manuais de Campanha na BDEx")
    print("=" * 70)

    r = sessao.get(f"{URL}/jspui/simple-search", params=PARAMS_MANUAL, timeout=60)
    r.raise_for_status()
    r.encoding = "utf-8"

    total = contar_total(BeautifulSoup(r.text, "html.parser"))
    if not total:
        raise RuntimeError("Não foi possível identificar o total de manuais na página da BDEx.")

    print(f"{total} manuais de campanha no acervo\n")

    manuais = []
    for start in range(0, total, RPP):
        p = dict(PARAMS_BUSCA)
        p["start"] = start

        r = sessao.get(f"{URL}/jspui/simple-search", params=p, timeout=60)
        r.raise_for_status()
        r.encoding = "utf-8"

        lote = extrair_resultados(BeautifulSoup(r.text, "html.parser"))
        manuais.extend(lote)
        print(f"  start={start:>3}: {len(lote)} documentos")

        time.sleep(pausa)

    unicos = len({m["handle"] for m in manuais})
    print(f"\n{len(manuais)} coletados | {unicos} handles únicos")
    if unicos != len(manuais):
        print("  ⚠️  Há repetição entre páginas — deduplicando por handle.")
        vistos_h = set()
        manuais_unicos = []
        for m in manuais:
            if m["handle"] not in vistos_h:
                vistos_h.add(m["handle"])
                manuais_unicos.append(m)
        manuais = manuais_unicos

    return manuais


def localizar_pdfs(manuais: list[dict], pausa: float = PAUSA_PADRAO) -> list[dict]:
    """
    Visita a página de cada item para achar o link do arquivo PDF.
    Reaproveita entradas já resolvidas no catálogo local e salva progresso em disco.
    """
    print("\n" + "=" * 70)
    print("ETAPA 3 — Localização dos Links de PDF (Bitstreams)")
    print("=" * 70)

    # Mescla informações de url_pdf já resolvidas em execução anterior
    catalogo_antigo = {
        d["handle"]: d
        for d in carregar_catalogo_existente()
        if isinstance(d, dict) and d.get("handle") and "url_pdf" in d
    }

    reaproveitados = 0
    for doc in manuais:
        antigo = catalogo_antigo.get(doc["handle"])
        if antigo and antigo.get("url_pdf"):
            doc["url_pdf"] = antigo["url_pdf"]
            doc["nome_arquivo"] = nome_seguro(antigo.get("nome_arquivo") or "")
            if "caminho_local" in antigo:
                doc["caminho_local"] = antigo["caminho_local"]
            reaproveitados += 1

    if reaproveitados:
        print(f"♻️  {reaproveitados}/{len(manuais)} links de PDF reaproveitados do cache local.")

    falhas = []
    novas_requisicoes = 0

    try:
        for i, doc in enumerate(manuais, 1):
            if doc.get("url_pdf"):
                continue

            try:
                doc["url_pdf"], doc["nome_arquivo"] = achar_pdf(doc["handle"])
                if doc["url_pdf"] is None:
                    falhas.append((doc["handle"], (doc.get("titulo") or "")[:40], "sem PDF anexado"))
            except Exception as e:
                doc["url_pdf"] = doc["nome_arquivo"] = None
                falhas.append((doc["handle"], (doc.get("titulo") or "")[:40], str(e)[:60]))

            novas_requisicoes += 1
            if i % 25 == 0:
                print(f"  Progresso: {i}/{len(manuais)}")
                salvar_catalogo(manuais, silencioso=True)

            time.sleep(pausa)
    except KeyboardInterrupt:
        print("\n⚠️ Interrupção detectada! Salvando progresso parcial no catálogo...")
        salvar_catalogo(manuais)
        raise

    com_pdf = sum(1 for d in manuais if d.get("url_pdf"))
    print(f"\n{com_pdf}/{len(manuais)} com PDF localizado | {len(falhas)} falhas")
    for handle, titulo, motivo in falhas:
        print(f"  {handle}  {titulo}  — {motivo}")

    salvar_catalogo(manuais)
    return manuais


def selecionar_e_classificar(
    manuais: list[dict], ano_minimo: int = ANO_MINIMO_PADRAO
) -> tuple[list[dict], list[dict]]:
    """
    Remove duplicatas (priorizando o registro mais recente quando há colisão de nome)
    e separa os manuais em vigentes e revogados a partir de `ano_minimo`.
    """
    print("\n" + "=" * 70)
    print(f"ETAPA 4 — Deduplicação e Classificação (Ano >= {ano_minimo})")
    print("=" * 70)

    nomes = Counter(d.get("nome_arquivo") for d in manuais if d.get("nome_arquivo"))
    duplicados = {n for n, c in nomes.items() if c > 1}

    if duplicados:
        print(f"{len(duplicados)} nomes duplicados no catálogo:")
        for nome in sorted(duplicados):
            print(f"\n  {nome}")
            for d in manuais:
                if d.get("nome_arquivo") == nome:
                    print(f"    {d.get('handle')}  {d.get('data')}  {(d.get('titulo') or '')[:45]}")

    # Ordena do ano mais recente para o mais antigo ao deduplicar, evitando que
    # um registro antigo (ex: 1998) descarte a edição atualizada (ex: 2025) de mesmo nome.
    manuais_ordenados = sorted(manuais, key=lambda d: ano_de(d), reverse=True)

    vistos = set()
    selecao = []

    for d in manuais_ordenados:
        if not d.get("url_pdf"):
            continue
        if ano_de(d) < ano_minimo:
            continue
        if d["nome_arquivo"] in vistos:
            d["duplicata"] = True
            continue

        vistos.add(d["nome_arquivo"])
        d["revogado"] = esta_revogado(d)
        d["duplicata"] = False
        selecao.append(d)

    vigentes = [d for d in selecao if not d["revogado"]]
    revogados = [d for d in selecao if d["revogado"]]

    print(f"\n{'-'*70}")
    print(f"{len(selecao)} manuais de {ano_minimo} em diante (após deduplicar)")
    print(f"  {len(vigentes)} vigentes  →  {PASTA_PDF.name}/")
    print(f"  {len(revogados)} revogados →  {PASTA_PDF.name}/{PASTA_REVOGADOS.name}/\n")

    if revogados:
        print("Documentos classificados como revogados:")
        for d in sorted(revogados, key=lambda x: x.get("data") or ""):
            print(f"  {d.get('data')}  {(d.get('nome_arquivo') or '')[:60]}")

    return vigentes, revogados


def baixar_selecao(
    vigentes: list[dict],
    revogados: list[dict],
    manuais_catalogo: list[dict],
    pausa: float = PAUSA_PADRAO,
    limite: int | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    """Baixa os PDFs vigentes e revogados para suas respectivas pastas."""
    print("\n" + "=" * 70)
    print("ETAPA 5 — Download dos Manuais em PDF")
    print("=" * 70)

    tarefas = [(d, PASTA_PDF) for d in vigentes] + [
        (d, PASTA_REVOGADOS) for d in revogados
    ]

    if limite is not None and limite > 0:
        print(f"ℹ️  Limite aplicado: processando apenas os primeiros {limite} arquivos.\n")
        tarefas = tarefas[:limite]

    baixados, erros = 0, []

    try:
        for i, (doc, pasta) in enumerate(tarefas, 1):
            ja_existia = (pasta / doc["nome_arquivo"]).exists() and (pasta / doc["nome_arquivo"]).stat().st_size > 0
            try:
                caminho, status = baixar(doc["url_pdf"], doc["nome_arquivo"], pasta)
                if caminho is None:
                    erros.append((doc["nome_arquivo"], status))
                else:
                    doc["caminho_local"] = str(caminho)
                    baixados += 1
            except Exception as e:
                status = "ERRO"
                erros.append((doc["nome_arquivo"], str(e)[:70]))

            print(f"[{i:>3}/{len(tarefas)}] {doc['nome_arquivo'][:48]:<48} {status}")
            # Só aguarda pausa se houve requisição de rede real
            if not ja_existia:
                time.sleep(pausa)
    except KeyboardInterrupt:
        print("\n⚠️ Download interrompido pelo usuário! Salvando estado atual no catálogo...")
        salvar_catalogo(manuais_catalogo)
        raise

    print(f"\n{'-'*70}")
    print(f"{baixados}/{len(tarefas)} baixados/confirmados | {len(erros)} erros")
    for nome, motivo in erros:
        print(f"  ❌ {nome}: {motivo}")

    salvar_catalogo(manuais_catalogo)
    return baixados, erros


def manutencao_renomear_antigos(aplicar: bool = True) -> tuple[int, int]:
    """
    Renomeia arquivos que foram salvos com percent-encoding (%20, %c3%a7) em
    execuções antigas. Se o arquivo decodificado já existir e for íntegro,
    remove a cópia antiga com nome codificado para evitar duplicidade.
    """
    print("\n" + "=" * 70)
    print("MANUTENÇÃO — Limpeza de Nomes Codificados e Arquivos Parciais")
    print("=" * 70)

    # Remove arquivos .parcial truncados
    parciais = list(PASTA_PDF.rglob("*.parcial"))
    for p in parciais:
        print(f"  🗑️  Removendo download incompleto: {p.name}")
        if aplicar:
            p.unlink(missing_ok=True)

    renomeados, removidos_duplicados = 0, 0

    for pasta in (PASTA_PDF, PASTA_REVOGADOS):
        if not pasta.exists():
            continue
        for p in sorted(pasta.glob("*.pdf")):
            novo = nome_seguro(p.name)
            if novo == p.name:
                continue

            destino = p.with_name(novo)
            if destino.exists() and destino.stat().st_size > 0:
                print(f"  ♻️  Versão legível já existe, removendo duplicata codificada: {p.name[:55]}")
                if aplicar:
                    p.unlink(missing_ok=True)
                removidos_duplicados += 1
                continue

            print(f"  ✏️  {p.name[:55]}\n      → {novo}")
            if aplicar:
                p.rename(destino)
            renomeados += 1

    # Também move para Revogados caso algum arquivo revogado tenha ficado na pasta principal
    for p in sorted(PASTA_PDF.glob("*.pdf")):
        if esta_revogado({"nome_arquivo": p.name, "titulo": p.stem}):
            destino_rev = PASTA_REVOGADOS / p.name
            print(f"  📦 Movendo revogado para {PASTA_REVOGADOS.name}/: {p.name}")
            if aplicar:
                if destino_rev.exists():
                    p.unlink(missing_ok=True)
                else:
                    p.rename(destino_rev)

    acao = "renomeados" if aplicar else "seriam renomeados"
    print(f"\nResumo da manutenção: {renomeados} {acao} | {removidos_duplicados} duplicatas codificadas removidas")
    return renomeados, removidos_duplicados


def verificar_downloads() -> None:
    """Confere o que foi efetivamente para o disco e sinaliza problemas."""
    print("\n" + "=" * 70)
    print("ETAPA 6 — Verificação Final no Disco")
    print("=" * 70)

    pdfs = sorted(PASTA_PDF.glob("*.pdf"))
    revs = sorted(PASTA_REVOGADOS.glob("*.pdf"))

    tam = lambda ps: sum(p.stat().st_size for p in ps) / 1024**3

    print(f"{len(pdfs):>4} PDFs vigentes  ({tam(pdfs):.2f} GB)  em {PASTA_PDF}")
    print(f"{len(revs):>4} PDFs revogados ({tam(revs):.2f} GB)  em {PASTA_REVOGADOS}")

    parciais = list(PASTA_PDF.rglob("*.parcial"))
    if parciais:
        print(f"\n⚠️  {len(parciais)} downloads incompletos encontrados:")
        for p in parciais:
            print(f"  {p.name}")

    codificados = [p.name for p in pdfs + revs if "%" in p.name]
    if codificados:
        print(f"\n⚠️  {len(codificados)} nomes ainda codificados (rode com --manutencao):")
        for n in codificados[:10]:
            print(f"  {n}")
        if len(codificados) > 10:
            print(f"  ... e mais {len(codificados) - 10}")
    else:
        print("\n✅ Todos os nomes de arquivos estão legíveis e prontos para o Pipeline!")


# =============================================================================
# CLI E PONTO DE ENTRADA
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Coleta automática de Manuais de Campanha do Exército Brasileiro (BDEx)."
    )
    parser.add_argument(
        "--ano-minimo",
        type=int,
        default=ANO_MINIMO_PADRAO,
        help=f"Ano mínimo de publicação para filtrar manuais (padrão: {ANO_MINIMO_PADRAO}; use 0 para todos).",
    )
    parser.add_argument(
        "--pausa",
        type=float,
        default=PAUSA_PADRAO,
        help=f"Intervalo em segundos entre requisições HTTP (padrão: {PAUSA_PADRAO}s).",
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=None,
        help="Limita a quantidade de PDFs baixados (útil para testes rápidos, ex: --limite 5).",
    )
    parser.add_argument(
        "--listar-tipos",
        action="store_true",
        help="Lista todos os tipos de documento existentes no facet da BDEx antes de iniciar.",
    )
    parser.add_argument(
        "--atualizar-catalogo",
        action="store_true",
        help="Força uma nova consulta de paginação à BDEx mesmo que o catálogo JSON já exista.",
    )
    parser.add_argument(
        "--apenas-catalogo",
        action="store_true",
        help="Coleta e salva apenas o catálogo JSON sem fazer o download dos PDFs.",
    )
    parser.add_argument(
        "--manutencao",
        action="store_true",
        help="Executa apenas a manutenção de nomes codificados (%%20) e arquivos .parcial na pasta Arquivos/.",
    )
    parser.add_argument(
        "--pipeline",
        action="store_true",
        help="Executa automaticamente o scripts/pipeline.py após concluir o download dos PDFs.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    preparar_diretorios()

    print("=" * 70)
    print("  PREPARAI — WEB SCRAPING DE MANUAIS DE CAMPANHA (BDEx)")
    print("=" * 70)
    print(f"  Raiz do projeto : {RAIZ}")
    print(f"  PDFs vigentes   : {PASTA_PDF}")
    print(f"  PDFs revogados  : {PASTA_REVOGADOS}")
    print(f"  Catálogo JSON   : {ARQUIVO_JSON}")

    if args.manutencao:
        manutencao_renomear_antigos(aplicar=True)
        verificar_downloads()
        return

    if args.listar_tipos:
        listar_catalogo_tipos(pausa=args.pausa)

    # Se o catálogo já existe e está completo, podemos reutilizá-lo para evitar centenas de requisições
    catalogo_existente = carregar_catalogo_existente()
    catalogo_completo = (
        bool(catalogo_existente)
        and all("url_pdf" in d for d in catalogo_existente)
        and not args.atualizar_catalogo
    )

    if catalogo_completo:
        print(f"\n♻️  Catálogo existente carregado ({len(catalogo_existente)} registros).")
        print("   (Use --atualizar-catalogo para forçar nova varredura na BDEx)")
        manuais = catalogo_existente
    else:
        manuais = coletar_lista_manuais(pausa=args.pausa)
        manuais = localizar_pdfs(manuais, pausa=args.pausa)

    vigentes, revogados = selecionar_e_classificar(manuais, ano_minimo=args.ano_minimo)

    if not args.apenas_catalogo:
        baixar_selecao(
            vigentes,
            revogados,
            manuais,
            pausa=args.pausa,
            limite=args.limite,
        )
        manutencao_renomear_antigos(aplicar=True)
        verificar_downloads()

    if args.pipeline:
        print("\n" + "=" * 70)
        print("ETAPA EXTRA — Executando Pipeline de Extração (PDF → XML)")
        print("=" * 70)
        sys.path.insert(0, str(RAIZ / "scripts"))
        import pipeline as modulo_pipeline
        modulo_pipeline.main()


if __name__ == "__main__":
    main()
