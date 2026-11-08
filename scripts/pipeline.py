"""
pipeline.py — PreparAI
========================
Extrai o conteúdo de manuais militares (padrão de Manual de Campanha do EB) em PDF
e os exporta como XML estruturado para uso pelo motor de busca BM25 do assistente.

Características:
  - Extração 100% vetorial/nativa de texto (sem OCR e sem exportação de imagens).
  - Cache incremental automático: pula PDFs que já possuem XML gerado em `Gerados/`.
  - Extração enriquecida de metadados (Nome do Manual, Código EB, Edição e Ano).
  - Recorte cirúrgico de texto entre cabeçalhos de seção na mesma página.

Uso direto:
    python scripts/pipeline.py            # processa apenas PDFs novos (cache incremental)
    python scripts/pipeline.py --forcar   # reprocessa todos os PDFs do zero
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as _stdlib_et
from pathlib import Path

# ── Pastas padrão (relativas ao diretório raiz do projeto) ─────────────────
_RAIZ = Path(__file__).resolve().parent.parent
PASTA_ARQUIVOS = _RAIZ / "Arquivos"
PASTA_GERADOS = _RAIZ / "Gerados"
ARQUIVO_CATALOGO_BDEX = _RAIZ / "webscraping" / "Coleta" / "manuais_campanha.json"

# Permite importar pacotes da .venv local caso o script seja chamado pelo python do sistema
_VENV_SITE = _RAIZ / ".venv" / "lib" / "python3.12" / "site-packages"
if _VENV_SITE.exists() and str(_VENV_SITE) not in sys.path:
    sys.path.insert(0, str(_VENV_SITE))

# ── Importação resiliente de PyMuPDF (fitz) com fallback para pypdfium2 ────
try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None
    import pypdfium2 as _pdfium

    class _PdfiumPageAdapter:
        def __init__(self, page):
            self._page = page

        def get_text(self, mode: str = "text") -> str:
            tp = self._page.get_textpage()
            texto = tp.get_text_range()
            return texto.replace("\r\n", "\n").replace("\r", "\n")

    class _PdfiumDocAdapter:
        def __init__(self, path: str):
            self._doc = _pdfium.PdfDocument(path)

        def __len__(self) -> int:
            return len(self._doc)

        def __getitem__(self, idx: int) -> _PdfiumPageAdapter:
            return _PdfiumPageAdapter(self._doc[idx])

        def __iter__(self):
            for i in range(len(self._doc)):
                yield _PdfiumPageAdapter(self._doc[i])

        def close(self) -> None:
            self._doc.close()

    class _FitzFallbackNamespace:
        @staticmethod
        def open(path: str) -> _PdfiumDocAdapter:
            return _PdfiumDocAdapter(path)

    fitz = _FitzFallbackNamespace()

# ── Importação resiliente de lxml com fallback para xml.etree.ElementTree ──
try:
    from lxml import etree
    _USA_LXML = True
except Exception:
    etree = _stdlib_et
    _USA_LXML = False


# =============================================================================
# UTILIDADES GERAIS E CACHE INCREMENTAL
# =============================================================================

def sanitizar(nome: str, max_len: int = 200) -> str:
    """Remove caracteres inválidos para nomes de arquivo e trunca."""
    nome = re.sub(r'[/\\:*?"<>|]', "", nome).strip()
    return nome[:max_len]


def carregar_catalogo_bdex() -> dict[str, dict]:
    """
    Carrega o catálogo JSON gerado pelo webscraping.py (se existir) e indexa
    por `nome_arquivo` para enriquecer os metadados dos XMLs.
    """
    if not ARQUIVO_CATALOGO_BDEX.exists():
        return {}
    try:
        with open(ARQUIVO_CATALOGO_BDEX, "r", encoding="utf-8") as f:
            itens = json.load(f)
        return {
            item["nome_arquivo"]: item
            for item in itens
            if isinstance(item, dict) and item.get("nome_arquivo")
        }
    except Exception:
        return {}


def mapear_xmls_existentes(pasta_saida: Path = PASTA_GERADOS) -> dict[str, Path]:
    """
    Lê os metadados dos XMLs já gerados em `Gerados/` e retorna um dicionário
    mapeando tanto o nome do PDF original quanto o título/código do manual
    para o caminho do XML, evitando retrabalho e duplicatas.
    """
    processados: dict[str, Path] = {}
    if not pasta_saida.exists():
        return processados

    for xml_path in pasta_saida.glob("*.xml"):
        if xml_path.stat().st_size == 0:
            continue
        try:
            arvore = etree.parse(str(xml_path))
            raiz = arvore.getroot()
            no_arq = raiz.find(".//metadados/arquivo")
            no_ed = raiz.find(".//metadados/edicao")
            no_tit = raiz.find(".//metadados/titulo")
            # Garante que o XML possui os metadados completos da estrutura atual
            if no_arq is not None and no_arq.text and no_ed is not None:
                processados[no_arq.text.strip()] = xml_path
                if no_tit is not None and no_tit.text:
                    processados[f"TITULO::{no_tit.text.strip()}"] = xml_path
        except Exception:
            continue

    return processados


def descobrir_pdfs(pasta: Path = PASTA_ARQUIVOS) -> dict[str, Path]:
    """
    Varre a pasta de entrada (ignorando a subpasta Revogados/) e retorna
    um dicionário {nome_sem_ext: Path}.
    """
    if not pasta.exists():
        raise FileNotFoundError(f"Pasta de PDFs não encontrada: {pasta}")

    pdfs = {p.stem: p for p in sorted(pasta.glob("*.pdf"))}
    print(f"📂 {len(pdfs)} PDF(s) vigente(s) encontrado(s) em '{pasta}'")
    return pdfs


# =============================================================================
# ETAPA 1 — EXTRAÇÃO DE METADADOS DA CAPA (TÍTULO, EDIÇÃO, ANO E CÓDIGO)
# =============================================================================

_TERMOS_INSTITUCIONAIS_CAPA = (
    "MINISTÉRIO DA DEFESA",
    "MINISTERIO DA DEFESA",
    "EXÉRCITO BRASILEIRO",
    "EXERCITO BRASILEIRO",
    "COMANDO DE OPERAÇÕES TERRESTRES",
    "COMANDO DE OPERACOES TERRESTRES",
    "ESTADO-MAIOR DO EXÉRCITO",
    "ESTADO-MAIOR DO EXERCITO",
    "SECRETARIA-GERAL DO EXÉRCITO",
    "DEPARTAMENTO DE EDUCAÇÃO E CULTURA DO EXÉRCITO",
    "COMANDO LOGÍSTICO",
    "MANUAL DE CAMPANHA",
)


def _formatar_nome_manual(texto: str) -> str:
    """Formata o nome do manual em Title Case preservando preposições em minúsculas."""
    if not texto.isupper():
        return texto
    preposicoes = {"de", "da", "do", "das", "dos", "e", "em", "na", "no", "nas", "nos", "para", "com", "a", "o", "as", "os"}
    palavras = texto.title().split()
    resultado = []
    for idx, p in enumerate(palavras):
        if idx > 0 and p.lower() in preposicoes:
            resultado.append(p.lower())
        else:
            resultado.append(p)
    return " ".join(resultado)


def _extrair_codigo_eb(texto: str) -> str:
    """Extrai e padroniza códigos de manuais (ex: EB70-MC-10.375, C-20-10, MC 4.55)."""
    m_eb = re.search(
        r"\b(EB\d{2})\s*[-–_]?\s*([A-Z]{2})\s*[-–_]?\s*(\d{2})[\.\-_]?(\d{3})\b",
        texto,
        re.IGNORECASE,
    )
    if m_eb:
        return f"{m_eb.group(1).upper()}-{m_eb.group(2).upper()}-{m_eb.group(3)}.{m_eb.group(4)}"
    m_c = re.search(r"\bC\s*[-–]\s*(\d+)\s*[-–]\s*(\d+)\b", texto, re.IGNORECASE)
    if m_c:
        return f"C-{m_c.group(1)}-{m_c.group(2)}"
    m_mc = re.search(r"\bMC\s*[-–_]?\s*(\d+[\.\-]\d+)\b", texto, re.IGNORECASE)
    if m_mc:
        return f"MC {m_mc.group(1).replace('-', '.')}"
    return ""


def extrair_metadados_capa(caminho_pdf: Path, meta_bdex: dict | None = None) -> dict[str, str]:
    """
    Extrai os metadados estruturados do manual a partir da capa (página 1),
    da última página e do catálogo da BDEx (se disponível):
      - titulo: string completa para o nome do arquivo XML
      - nome_manual: nome limpo do manual (ex: 'Treinamento Físico Militar')
      - codigo: código oficial EB (ex: 'EB70-MC-10.375')
      - edicao: edição normalizada (ex: '5ª Ed.')
      - ano: ano de publicação (ex: '2021')
    """
    pdf = fitz.open(str(caminho_pdf))
    texto_pagina_1 = pdf[0].get_text("text") if len(pdf) > 0 else ""
    texto_ultima_pag = pdf[len(pdf) - 1].get_text("text") if len(pdf) > 1 else ""
    pdf.close()

    linhas_brutas = [
        re.sub(r"\s+", " ", l).strip()
        for l in texto_pagina_1.splitlines()
        if re.sub(r"\s+", " ", l).strip()
    ]

    # Une número de edição quebrado de seu ordinal (ex: "1" + "a Edição" ou "1" + "aEdição")
    linhas = []
    i = 0
    while i < len(linhas_brutas):
        atual = linhas_brutas[i]
        if (
            re.match(r"^\d+$", atual)
            and i + 1 < len(linhas_brutas)
            and re.match(r"^[aªº]\s*(Edi[çc][ãa]o)?\b", linhas_brutas[i + 1], re.IGNORECASE)
        ):
            linhas.append(f"{atual}{linhas_brutas[i + 1]}")
            i += 2
        else:
            linhas.append(atual)
            i += 1

    texto_capa_unido = "\n".join(linhas)

    # 1. Código do manual (capa -> nome do arquivo PDF)
    codigo = _extrair_codigo_eb(texto_capa_unido) or _extrair_codigo_eb(caminho_pdf.stem)

    # 2. Edição (ex: "4ª Edição" -> "4ª Ed.", "1a Edição" -> "1ª Ed.")
    m_ed = re.search(r"\b(\d+)\s*[ªºa]?\s*Edi[çc][ãa]o\b", texto_capa_unido, re.IGNORECASE)
    if m_ed:
        edicao = f"{m_ed.group(1)}ª Ed."
    elif re.search(r"Edi[çc][ãa]o\s+Experimental", texto_capa_unido, re.IGNORECASE):
        edicao = "Ed. Experimental"
    else:
        edicao = "Ed. Única"

    # 3. Ano de publicação (capa -> catálogo BDEx -> última página)
    anos_capa = re.findall(r"\b(19\d{2}|20\d{2})\b", texto_capa_unido)
    if anos_capa:
        ano = anos_capa[-1]
    elif meta_bdex and meta_bdex.get("data") and str(meta_bdex["data"])[:4].isdigit():
        ano = str(meta_bdex["data"])[:4]
    else:
        anos_fim = re.findall(r"\b(19\d{2}|20\d{2})\b", texto_ultima_pag)
        ano = anos_fim[-1] if anos_fim else "S/D"

    # 4. Nome limpo do Manual (prioriza título curado do catálogo BDEx quando disponível)
    if meta_bdex and meta_bdex.get("titulo") and meta_bdex["titulo"].strip():
        nome_manual = _formatar_nome_manual(meta_bdex["titulo"].strip().upper())
    else:
        candidatos_nome = []
        for l in linhas:
            l_up = l.upper()
            if any(termo in l_up for termo in _TERMOS_INSTITUCIONAIS_CAPA):
                continue
            if _extrair_codigo_eb(l_up):
                continue
            if re.search(r"\b\d*\s*[ªºa]?\s*Edi[çc][ãa]o\b", l, re.IGNORECASE):
                continue
            if re.match(r"^(19\d{2}|20\d{2}|[-–—_\s]+)$", l):
                continue
            candidatos_nome.append(l)

        if candidatos_nome:
            nome_manual = _formatar_nome_manual(" ".join(candidatos_nome))
        else:
            nome_manual = _formatar_nome_manual(caminho_pdf.stem)

    # 5. Título completo estruturado e livre de colisão para o XML
    if codigo:
        titulo_completo = f"{codigo} | {nome_manual} | {edicao} ({ano})"
    else:
        titulo_completo = f"{nome_manual} | {edicao} ({ano})"

    return {
        "titulo": titulo_completo,
        "nome_manual": nome_manual,
        "codigo": codigo,
        "edicao": edicao,
        "ano": ano,
    }


# =============================================================================
# ETAPA 2 — EXTRAÇÃO E PARSE DO ÍNDICE
# =============================================================================

def _eh_conteudo_real(linha_strip: str) -> bool:
    """Verifica se uma linha é cabeçalho de conteúdo real (não entrada de índice)."""
    if "…" in linha_strip or "..." in linha_strip:
        return False
    # No índice, a linha termina com numeração de página (ex: "1.1 FINALIDADE 1-1")
    if re.search(r"\b(\d+-\d+|[A-Z]-\d+)\s*$", linha_strip):
        return False
    m = re.match(r"^\d+\.\d+\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-ZÁÉÍÓÚÂÊÔÃÕÇ]+)\b", linha_strip)
    return bool(m and len(m.group(1)) >= 3)


def extrair_indice_pdf(caminho_pdf: Path) -> str:
    """
    Extrai o texto do índice entre 'ÍNDICE DE ASSUNTOS' (ou 'SUMÁRIO') e o início
    do Capítulo I real (primeira linha de conteúdo efetivo).
    """
    pdf = fitz.open(str(caminho_pdf))
    indice = ""
    capturando = False

    for pagina in pdf:
        texto = pagina.get_text("text")
        if not capturando:
            for marcador in ("NDICE DE ASSUNTOS", "SUMÁRIO", "SUMARIO"):
                if marcador in texto.upper():
                    capturando = True
                    idx = texto.upper().find(marcador)
                    indice += texto[idx:] + "\n"
                    break
        else:
            for linha in texto.splitlines():
                if _eh_conteudo_real(linha.strip()):
                    pdf.close()
                    return indice
            indice += texto + "\n"

    pdf.close()
    return indice


def _preprocessar_indice(indice_texto: str) -> str:
    """
    Normaliza o texto do índice:
    - Une linhas quebradas de títulos de capítulo, anexo e seção
    - Remove marcadores de página soltos ('Pag', 'Pág')
    - Cola numerações de página (ex: '1-1', 'A-1') ao item anterior
    """
    indice_texto = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffe\uffff\xad]", "", indice_texto)
    linhas = indice_texto.splitlines()
    linhas_juntas = []
    i = 0
    while i < len(linhas):
        ls = linhas[i].strip()
        if not ls:
            i += 1
            continue
        # "CAPÍTULO" ou "ANEXO" sozinho na linha → une com a próxima
        if re.match(r"^(CAP[ÍI]TULO|ANEXO)$", ls, re.IGNORECASE) and i + 1 < len(linhas):
            linhas_juntas.append(ls + " " + linhas[i + 1].strip())
            i += 2
            continue
        # Número de seção sozinho → une com próxima
        if re.match(r"^\d+\.\d+$", ls) and i + 1 < len(linhas):
            linhas_juntas.append(ls + " " + linhas[i + 1].strip())
            i += 2
            continue
        linhas_juntas.append(ls)
        i += 1

    resultado = []
    for ls in linhas_juntas:
        if ls in ("Pag", "Pág", "PAG", "PÁG"):
            continue
        # Numeração de página isolada (ex: 1-5 ou A-1) cola no item anterior
        if re.match(r"^(\d+-\d+|[A-Z]-\d+)$", ls) and resultado:
            resultado[-1] += " " + ls
            continue

        # Caso uma linha contenha um ANEXO colado no final de outra entrada, separa
        partes_anexo = re.split(r"\s+(?=ANEXO\s+[A-Z]\s*[–\-—])", ls)
        for parte in partes_anexo:
            parte = parte.strip()
            if not parte:
                continue
            eh_nova_entrada = bool(
                re.match(r"CAP[ÍI]TULO\s+[IVXLCDM\d]+", parte, re.IGNORECASE)
                or re.match(r"\d+\.\d+\s+", parte)
                or re.match(r"ANEXO\s+[A-Z]\b", parte, re.IGNORECASE)
                or re.match(r"^(PREF[ÁA]CIO|GLOSS[ÁA]RIO|REFER[ÊE]NCIAS)", parte, re.IGNORECASE)
                or "NDICE DE ASSUNTOS" in parte.upper()
            )
            if eh_nova_entrada:
                resultado.append(parte)
            elif resultado:
                resultado[-1] += " " + parte

    return "\n".join(resultado)


def parsear_indice(indice_texto: str) -> list[dict]:
    """
    Converte o texto do índice em uma lista estruturada de itens:
        [{'tipo': 'capitulo'|'secao'|'anexo'|'especial',
          'num': str, 'titulo': str, 'pagina': str|None}, ...]
    """
    indice_texto = _preprocessar_indice(indice_texto)
    estrutura = []

    for linha in indice_texto.splitlines():
        linha = linha.strip()
        if not linha or "NDICE DE ASSUNTOS" in linha.upper():
            continue

        # Capítulo
        m = re.match(r"CAP[ÍI]TULO\s+([IVXLCDM\d]+)\s*[–\-—]?\s*(.+)", linha, re.IGNORECASE)
        if m:
            titulo = re.sub(r"[\.…]+[\s\d\-]*$", "", m.group(2)).strip()
            pag_m = re.search(r"(\d+-\d+)\s*$", linha)
            estrutura.append({
                "tipo": "capitulo",
                "num": m.group(1).upper(),
                "titulo": titulo,
                "pagina": pag_m.group(1) if pag_m else None,
            })
            continue

        # Seção numerada (ex: 2.3 Aquecimento Dinâmico)
        m = re.match(r"(\d+\.\d+)\s+(.+)", linha)
        if m:
            num, resto = m.group(1), m.group(2)
            pag_m = re.search(r"(\d+-\d+)\s*$", resto)
            titulo = re.sub(r"[\.…]+[\s\d\-]*$", "", resto).strip()
            if titulo:
                estrutura.append({
                    "tipo": "secao",
                    "num": num,
                    "titulo": titulo,
                    "pagina": pag_m.group(1) if pag_m else None,
                })
            continue

        # Anexo (ex: ANEXO A — Tabela de Avaliação ... A-1)
        m = re.match(r"ANEXO\s+([A-Z])\s*[–\-—]?\s*(.+)", linha, re.IGNORECASE)
        if m:
            letra = m.group(1).upper()
            resto = m.group(2)
            pag_m = re.search(r"([A-Z]-\d+|\d+-\d+)\s*$", resto)
            titulo = re.sub(r"[\.…]+[\s\w\-]*$", "", resto).strip()
            estrutura.append({
                "tipo": "anexo",
                "num": f"Anexo {letra}",
                "letra": letra,
                "titulo": titulo,
                "pagina": pag_m.group(1) if pag_m else f"{letra}-1",
            })
            continue

        # Seções especiais: Prefácio, Glossário, Referências
        for prefixo in ["PREFÁCIO", "PREFACIO", "GLOSSÁRIO", "GLOSSARIO",
                        "REFERÊNCIAS", "REFERENCIAS"]:
            if linha.upper().startswith(prefixo):
                pag_m = re.search(r"([A-Z]?-\d+|\d+-\d+)\s*$", linha)
                titulo_esp = "Prefácio" if "PREF" in prefixo else ("Glossário" if "GLOSS" in prefixo else "Referências")
                chave_esp = f"@{titulo_esp.upper()}"
                if not any(e["tipo"] == "especial" and e["titulo"] == titulo_esp for e in estrutura):
                    estrutura.append({
                        "tipo": "especial",
                        "num": "",
                        "titulo": titulo_esp,
                        "pagina": pag_m.group(1) if pag_m else chave_esp,
                    })
                break

    return estrutura


# =============================================================================
# ETAPA 3 — MAPEAMENTO E EXTRAÇÃO PRECISA DE TEXTO POR SEÇÃO
# =============================================================================

def mapear_paginas(caminho_pdf: Path) -> dict[str, int]:
    """
    Lê os rodapés/cabeçalhos de numeração do tipo '1-5', '2-12', 'A-1', 'B-3'
    de cada página e retorna {numeracao_manual: indice_pagina_pdf}.
    """
    pdf = fitz.open(str(caminho_pdf))
    mapa: dict[str, int] = {}
    for i, pagina in enumerate(pdf):
        texto = pagina.get_text("text")
        linhas = [l.strip() for l in texto.splitlines() if l.strip()]

        # 1. Procura numeração padrão (ex: 1-1, 3-15, A-1, B-2) no rodapé/topo
        for ls in reversed(linhas):
            if re.match(r"^(\d+-\d+|[A-Z]-\d+)$", ls):
                mapa.setdefault(ls, i)
                break

        # 2. Mapeia início de Anexos e Seções Especiais pelo cabeçalho na página (após pág 3)
        if i >= 2 and linhas:
            topo = " ".join(linhas[:5]).upper()
            if "NDICE DE ASSUNTOS" not in topo and "SUMÁRIO" not in topo:
                m_anexo = re.search(r"\bANEXO\s+([A-Z])\b", topo)
                if m_anexo:
                    mapa.setdefault(f"{m_anexo.group(1)}-1", i)
                for esp in ("PREFÁCIO", "PREFACIO", "GLOSSÁRIO", "GLOSSARIO", "REFERÊNCIAS", "REFERENCIAS"):
                    if re.search(rf"\b{esp}\b", topo):
                        chave = "@PREFÁCIO" if "PREF" in esp else ("@GLOSSÁRIO" if "GLOSS" in esp else "@REFERÊNCIAS")
                        mapa.setdefault(chave, i)

    pdf.close()
    return mapa


def _pag_range(
    idx: int, estrutura: list[dict], mapa_paginas: dict[str, int], pdf_len: int
) -> tuple[int | None, int | None, dict | None]:
    """
    Calcula o intervalo de páginas e identifica o próximo item estrutural
    para delimitar onde a seção atual termina.
    """
    item = estrutura[idx]
    chave_pag = item.get("pagina")
    pag_inicio = mapa_paginas.get(chave_pag) if chave_pag else None
    if pag_inicio is None:
        return None, None, None

    pag_prox = None
    prox_item = None
    for prox in estrutura[idx + 1:]:
        if prox["tipo"] == "capitulo":
            continue
        pp = mapa_paginas.get(prox.get("pagina")) if prox.get("pagina") else None
        if pp is not None and pp >= pag_inicio:
            pag_prox = pp
            prox_item = prox
            break

    return pag_inicio, pag_prox, prox_item


def _padrao_inicio_item(item: dict) -> re.Pattern | None:
    """Cria uma Regex que localiza a linha de título/cabeçalho de uma seção ou anexo."""
    if item["tipo"] == "secao" and item.get("num"):
        num_esc = re.escape(item["num"])
        return re.compile(rf"(?:^|\n)\s*{num_esc}\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ0-9]", re.MULTILINE)
    if item["tipo"] == "anexo" and item.get("letra"):
        letra = re.escape(item["letra"])
        return re.compile(rf"(?:^|\n)\s*ANEXO\s+{letra}\b", re.IGNORECASE | re.MULTILINE)
    if item["tipo"] == "especial" and item.get("titulo"):
        tit = re.escape(item["titulo"].upper())
        return re.compile(rf"(?:^|\n)\s*{tit}\b", re.IGNORECASE | re.MULTILINE)
    return None


def _limpar_texto(texto: str) -> str:
    """
    Remove artefatos comuns dos PDFs militares e caracteres de controle inválidos em XML 1.0:
    - Corrige hífens de quebra de linha codificados como \ufffe ou \xad
    - Remove caracteres de controle proibidos na especificação XML 1.0
    - Remove códigos de manual (EB70-MC-10.xxx, EB20-MC-10.xxx)
    - Remove numerações de página isoladas (1-5, 2-12, A-1)
    - Remove excesso de linhas em branco
    """
    texto = re.sub(r"(?<=\w)[\ufffe\xad](?=\w)", "-", texto)
    texto = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffe\uffff\xad]", "", texto)
    texto = re.sub(r"EB\d{2}-[A-Z]{2}-\d{2}\.\d+", "", texto)
    texto = re.sub(r"^\s*(\d+-\d+|[A-Z]-\d+)\s*$", "", texto, flags=re.MULTILINE)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def _extrair_texto_item(
    pdf,
    idx: int,
    estrutura: list[dict],
    mapa_paginas: dict[str, int],
) -> str | None:
    """
    Extrai o texto exato de uma seção, anexo ou seção especial, recortando
    entre o cabeçalho do item atual e o cabeçalho do próximo item.
    """
    pag_inicio, pag_prox, prox_item = _pag_range(idx, estrutura, mapa_paginas, len(pdf))
    if pag_inicio is None:
        return None

    item_atual = estrutura[idx]

    if pag_prox is not None:
        pag_leitura_fim = min(pag_prox + 1, len(pdf))
        pag_fallback_fim = max(pag_inicio + 1, min(pag_prox, len(pdf)))
    else:
        pag_leitura_fim = min(pag_inicio + 15, len(pdf))
        pag_fallback_fim = pag_leitura_fim

    textos_por_pagina = [
        pdf[p].get_text("text")
        for p in range(pag_inicio, pag_leitura_fim)
    ]
    texto_completo = "\n".join(textos_por_pagina)

    # 1. Recorta o início a partir do cabeçalho da seção atual
    padrao_atual = _padrao_inicio_item(item_atual)
    pos_inicio = 0
    if padrao_atual is not None:
        m_ini = padrao_atual.search(texto_completo)
        if m_ini:
            pos_inicio = m_ini.start()
            if texto_completo[pos_inicio] == "\n":
                pos_inicio += 1

    # 2. Recorta o fim exatamente antes do cabeçalho da próxima seção
    texto_a_partir_inicio = texto_completo[pos_inicio:]
    cortou_no_proximo = False

    if prox_item is not None:
        padrao_prox = _padrao_inicio_item(prox_item)
        if padrao_prox is not None:
            offset_busca = min(len(texto_a_partir_inicio), 25)
            m_fim = padrao_prox.search(texto_a_partir_inicio[offset_busca:])
            if m_fim:
                pos_fim = offset_busca + m_fim.start()
                texto_a_partir_inicio = texto_a_partir_inicio[:pos_fim]
                cortou_no_proximo = True

    if not cortou_no_proximo and pag_prox is not None and pag_prox > pag_inicio:
        texto_fallback = "\n".join(textos_por_pagina[: pag_fallback_fim - pag_inicio])
        if pos_inicio < len(texto_fallback):
            texto_a_partir_inicio = texto_fallback[pos_inicio:]
        else:
            texto_a_partir_inicio = texto_fallback

    return _limpar_texto(texto_a_partir_inicio)


# =============================================================================
# ETAPA 4 — EXPORTAÇÃO PARA XML
# =============================================================================

def exportar_para_xml(
    caminho_pdf: Path,
    meta_capa: dict[str, str],
    estrutura: list[dict],
    mapa_paginas: dict[str, int],
    pasta_saida: Path,
    meta_bdex: dict | None = None,
) -> Path:
    """
    Converte o conteúdo do manual para um arquivo XML estruturado com metadados
    de nome, código, edição e ano para citações oficiais.
    """
    pdf = fitz.open(str(caminho_pdf))
    root = etree.Element("documento")

    # Metadados enriquecidos
    meta = etree.SubElement(root, "metadados")
    etree.SubElement(meta, "titulo").text = meta_capa["titulo"]
    etree.SubElement(meta, "nome_manual").text = meta_capa["nome_manual"]
    etree.SubElement(meta, "codigo").text = meta_capa["codigo"]
    etree.SubElement(meta, "edicao").text = meta_capa["edicao"]
    etree.SubElement(meta, "ano").text = meta_capa["ano"]
    etree.SubElement(meta, "arquivo").text = Path(caminho_pdf).name
    if meta_bdex:
        if meta_bdex.get("autor"):
            etree.SubElement(meta, "autor").text = str(meta_bdex["autor"])
        if meta_bdex.get("handle"):
            etree.SubElement(meta, "handle").text = str(meta_bdex["handle"])

    cap_node = None

    for idx, item in enumerate(estrutura):
        if item["tipo"] == "capitulo":
            cap_node = etree.SubElement(root, "capitulo")
            cap_node.set("numero", item["num"])
            cap_node.set("titulo", item["titulo"])

        elif item["tipo"] == "secao":
            secao_node = etree.SubElement(
                cap_node if cap_node is not None else root, "secao"
            )
            secao_node.set("numero", item["num"])
            secao_node.set("titulo", item["titulo"])
            texto_extraido = _extrair_texto_item(pdf, idx, estrutura, mapa_paginas)
            texto_node = etree.SubElement(secao_node, "texto")
            texto_node.text = (
                texto_extraido
                if texto_extraido
                else "(Página não localizada)"
            )

        elif item["tipo"] == "anexo":
            anexo_node = etree.SubElement(root, "anexo")
            letra_m = re.search(r"[A-Z]$", item.get("num", ""))
            anexo_node.set("letra", letra_m.group(0) if letra_m else "")
            anexo_node.set("titulo", item["titulo"])
            texto_extraido = _extrair_texto_item(pdf, idx, estrutura, mapa_paginas)
            texto_node = etree.SubElement(anexo_node, "texto")
            texto_node.text = (
                texto_extraido
                if texto_extraido
                else "(Conteúdo não extraído)"
            )

        elif item["tipo"] == "especial":
            esp_node = etree.SubElement(root, "secao_especial")
            esp_node.set("titulo", item["titulo"])
            texto_extraido = _extrair_texto_item(pdf, idx, estrutura, mapa_paginas)
            texto_node = etree.SubElement(esp_node, "texto")
            texto_node.text = (
                texto_extraido
                if texto_extraido
                else "(Conteúdo não extraído)"
            )

    pdf.close()

    pasta_saida.mkdir(parents=True, exist_ok=True)
    nome_arquivo = sanitizar(meta_capa["titulo"]) + ".xml"
    caminho_xml = pasta_saida / nome_arquivo

    arvore = etree.ElementTree(root)
    if _USA_LXML:
        arvore.write(
            str(caminho_xml),
            encoding="UTF-8",
            xml_declaration=True,
            pretty_print=True,
        )
    else:
        _stdlib_et.indent(arvore, space="  ")
        arvore.write(
            str(caminho_xml),
            encoding="UTF-8",
            xml_declaration=True,
        )
    return caminho_xml


# =============================================================================
# PIPELINE COMPLETO — processa um único PDF
# =============================================================================

def processar_pdf(
    caminho_pdf: Path,
    pasta_saida: Path = PASTA_GERADOS,
    catalogo_bdex: dict[str, dict] | None = None,
) -> dict:
    """
    Executa as etapas da pipeline em um único PDF:
      1. Extrai metadados da capa (título, nome, código, edição e ano)
      2. Extrai e parseia o índice
      3. Mapeia numeração de páginas (incluindo anexos A-1, B-1)
      4. Exporta XML estruturado com recorte exato por seção
    """
    print(f"\n{'='*60}")
    print(f"📄 Processando: {caminho_pdf.name}")
    print(f"{'='*60}")

    meta_bdex = (catalogo_bdex or {}).get(caminho_pdf.name)
    meta_capa = extrair_metadados_capa(caminho_pdf, meta_bdex=meta_bdex)
    print(
        f"  📌 Manual : {meta_capa['nome_manual']} "
        f"| Edição: {meta_capa['edicao']} ({meta_capa['ano']})"
    )

    indice = extrair_indice_pdf(caminho_pdf)
    if not indice.strip():
        print("  ⚠️  Índice não encontrado — arquivo ignorado.")
        return {"arquivo": caminho_pdf.name, "status": "sem_indice"}

    estrutura = parsear_indice(indice)
    mapa = mapear_paginas(caminho_pdf)

    cap_count = sum(1 for i in estrutura if i["tipo"] == "capitulo")
    sec_count = sum(1 for i in estrutura if i["tipo"] == "secao")
    print(f"  📋 {cap_count} capítulo(s), {sec_count} seção(ões)")
    print(f"  🗺️  {len(mapa)} página(s) mapeada(s)")

    if not mapa:
        print("  ⚠️  Nenhuma página mapeada (PDF incompleto ou fora do padrão) — ignorado.")
        return {"arquivo": caminho_pdf.name, "status": "sem_paginas"}

    caminho_xml = exportar_para_xml(
        caminho_pdf, meta_capa, estrutura, mapa, pasta_saida, meta_bdex=meta_bdex
    )
    print(
        f"  ✅ XML gerado: {caminho_xml.name}  "
        f"({caminho_xml.stat().st_size / 1024:.1f} KB)"
    )

    return {
        "arquivo": caminho_pdf.name,
        "titulo": meta_capa["titulo"],
        "nome_manual": meta_capa["nome_manual"],
        "edicao": meta_capa["edicao"],
        "ano": meta_capa["ano"],
        "capitulos": cap_count,
        "secoes": sec_count,
        "paginas": len(mapa),
        "xml": str(caminho_xml),
        "status": "ok",
    }


# =============================================================================
# EXECUÇÃO DIRETA
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Pipeline de extração de Manuais de Campanha (PDF para XML)."
    )
    parser.add_argument(
        "--forcar",
        action="store_true",
        help="Força o reprocessamento de todos os PDFs, ignorando o cache incremental de XMLs.",
    )
    args, _ = parser.parse_known_args()

    print("=" * 60)
    print("  PREPARAI — PIPELINE DE EXTRAÇÃO DE MANUAIS")
    print("=" * 60)

    catalogo_bdex = carregar_catalogo_bdex()
    if catalogo_bdex:
        print(f"📚 Catálogo BDEx carregado ({len(catalogo_bdex)} manuais catalogados).")

    pdfs = descobrir_pdfs(PASTA_ARQUIVOS)
    if not pdfs:
        print("Nenhum PDF encontrado. Execute 'python webscraping/webscraping.py' ou coloque os arquivos em 'Arquivos/'.")
        return

    xmls_existentes = {} if args.forcar else mapear_xmls_existentes(PASTA_GERADOS)

    resultados = []
    pulados = 0
    for nome, caminho in pdfs.items():
        if caminho.name in xmls_existentes:
            xml_ja_pronto = xmls_existentes[caminho.name]
            print(f"⏭️  Pulando '{caminho.name}' — já processado em '{xml_ja_pronto.name}'")
            resultados.append({"arquivo": caminho.name, "status": "cache"})
            pulados += 1
            continue

        if not args.forcar:
            meta_bdex = (catalogo_bdex or {}).get(caminho.name)
            meta_previa = extrair_metadados_capa(caminho, meta_bdex=meta_bdex)
            chave_tit = f"TITULO::{meta_previa['titulo']}"
            if chave_tit in xmls_existentes:
                xml_ja_pronto = xmls_existentes[chave_tit]
                print(f"⏭️  Pulando '{caminho.name}' — manual equivalente já em '{xml_ja_pronto.name}'")
                resultados.append({"arquivo": caminho.name, "status": "cache"})
                pulados += 1
                continue

        try:
            resultado = processar_pdf(
                caminho,
                PASTA_GERADOS,
                catalogo_bdex=catalogo_bdex,
            )
            if resultado.get("status") == "ok" and not args.forcar:
                xml_gerado = Path(resultado["xml"])
                xmls_existentes[caminho.name] = xml_gerado
                xmls_existentes[f"TITULO::{resultado['titulo']}"] = xml_gerado
            resultados.append(resultado)
        except Exception as e:
            print(f"  ❌ Erro ao processar '{nome}': {e}")
            resultados.append({"arquivo": nome, "status": f"erro: {e}"})

    # Resumo final
    print(f"\n{'='*60}")
    print("  RESUMO FINAL")
    print(f"{'='*60}")
    ok = [r for r in resultados if r.get("status") == "ok"]
    print(
        f"  ✅ {len(ok)} novo(s) processado(s) | "
        f"⏭️ {pulados} em cache (pulados) | "
        f"Total: {len(resultados)} PDF(s)"
    )
    print(f"\n  XMLs ativos em '{PASTA_GERADOS}/':")
    for xml_file in sorted(PASTA_GERADOS.glob("*.xml")):
        print(f"    📝 {xml_file.name}  ({xml_file.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
