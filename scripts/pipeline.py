"""
pipeline.py — PreparAI
========================
Extrai o conteúdo de manuais militares (padrão de Manual de Campanha do EB) em PDF
e os exporta como XML estruturado para uso pelo motor de busca BM25 do assistente.

Integra-se automaticamente com o catálogo gerado por `webscraping/webscraping.py`
(`webscraping/Coleta/manuais_campanha.json`) quando disponível, enriquecendo os
metadados do XML.

Uso direto:
    python scripts/pipeline.py
    python scripts/pipeline.py --sem-imagens
    python scripts/pipeline.py --ocr          # ativa fallback OCR p/ páginas escaneadas

Os PDFs vigentes devem estar em:  Arquivos/
Os XMLs serão gerados em:         Gerados/
"""

import argparse
import io
import json
import re
import shutil
import sys
import xml.etree.ElementTree as _stdlib_et
from pathlib import Path

# ── Pastas padrão (relativas ao diretório raiz do projeto) ─────────────────
_RAIZ = Path(__file__).resolve().parent.parent
PASTA_ARQUIVOS = _RAIZ / "Arquivos"
PASTA_GERADOS = _RAIZ / "Gerados"
PASTA_IMAGENS = _RAIZ / "Imagens"
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

        def get_images(self, full: bool = True):
            return []

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

# ── Configuração de OCR Fallback (desativado por padrão por performance) ───
USAR_OCR_FALLBACK = False
_OCR_AVISADO = False


# =============================================================================
# UTILIDADES GERAIS E EXTRAÇÃO DE TEXTO (COM FALLBACK OCR OPCIONAL)
# =============================================================================

def sanitizar(nome: str, max_len: int = 200) -> str:
    """Remove caracteres inválidos para nomes de arquivo/pasta e trunca."""
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


def _extrair_texto_pagina_com_ocr_opcional(pagina) -> str:
    """
    Extrai o texto nativo da página via PyMuPDF / PDFium.
    Se o texto vier vazio (página puramente escaneada) e `USAR_OCR_FALLBACK`
    estiver ativo (`--ocr`), tenta extrair via pytesseract + Pillow caso o
    binário `tesseract` esteja instalado no sistema operacional.
    """
    global _OCR_AVISADO
    texto = pagina.get_text("text")
    if texto.strip() or not USAR_OCR_FALLBACK:
        return texto

    if shutil.which("tesseract") is None:
        if not _OCR_AVISADO:
            print(
                "  ⚠️  Aviso OCR: página sem camada de texto, mas o binário 'tesseract' "
                "não está instalado no sistema (instale 'tesseract-ocr' e 'tesseract-ocr-por')."
            )
            _OCR_AVISADO = True
        return texto

    try:
        import pytesseract
        from PIL import Image

        if hasattr(pagina, "get_pixmap"):
            pix = pagina.get_pixmap(dpi=250)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            return pytesseract.image_to_string(img, lang="por")
    except Exception as e:
        if not _OCR_AVISADO:
            print(f"  ⚠️  Aviso OCR: falha ao executar pytesseract ({e}).")
            _OCR_AVISADO = True
    return texto


# =============================================================================
# ETAPA 1 — EXTRAÇÃO DE TÍTULO
# =============================================================================

def extrair_titulo_pdf(caminho_pdf: Path, meta_bdex: dict | None = None) -> str:
    """
    Extrai o título do manual a partir das primeiras linhas da página 1.
    Os manuais do EB seguem o padrão: linhas 4–7 contêm o título completo.
    Usa o título catalogado na BDEx como fallback caso a página 1 esteja sem texto.
    """
    pdf = fitz.open(str(caminho_pdf))
    texto_pagina_1 = _extrair_texto_pagina_com_ocr_opcional(pdf[0]) if len(pdf) > 0 else ""
    pdf.close()

    linhas_brutas = [
        re.sub(r"\s+", " ", l).strip()
        for l in texto_pagina_1.splitlines()
        if re.sub(r"\s+", " ", l).strip()
    ]

    # Une número de edição quebrado de seu ordinal (ex: "1" + "a Edição" -> "1a Edição")
    linhas = []
    i = 0
    while i < len(linhas_brutas):
        atual = linhas_brutas[i]
        if re.match(r"^\d+$", atual) and i + 1 < len(linhas_brutas) and re.match(r"^[aªº]\b", linhas_brutas[i + 1], re.IGNORECASE):
            linhas.append(f"{atual}{linhas_brutas[i + 1]}")
            i += 2
        else:
            linhas.append(atual)
            i += 1

    if len(linhas) >= 7:
        titulo = " | ".join(linhas[3:7])
    elif len(linhas) >= 4:
        titulo = " ".join(linhas[3:])
    elif linhas:
        titulo = " | ".join(linhas)
    elif meta_bdex and meta_bdex.get("titulo"):
        titulo = meta_bdex["titulo"]
    else:
        titulo = caminho_pdf.stem

    return re.sub(r"\s{2,}", " ", titulo).strip()


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
        texto = _extrair_texto_pagina_com_ocr_opcional(pagina)
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
                # Nos manuais EB, o Anexo A começa na página 'A-1', Anexo B em 'B-1', etc.
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
                # Evita duplicar seções especiais caso o início do Prefácio tenha sido capturado após o sumário
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
    Também mapeia marcadores especiais (@PREFÁCIO, @GLOSSÁRIO, @REFERÊNCIAS)
    e cabeçalhos de ANEXO caso não possuam rodapé padrão.
    """
    pdf = fitz.open(str(caminho_pdf))
    mapa: dict[str, int] = {}
    for i, pagina in enumerate(pdf):
        texto = _extrair_texto_pagina_com_ocr_opcional(pagina)
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
    Retorna (pag_inicio, pag_proxima_secao, proximo_item).
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
    - Corrige hífens de quebra de linha codificados como \ufffe ou \xad (ex: coluna\ufffebase -> coluna-base)
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
    Extrai o texto exato de uma seção, anexo ou seção especial:
    - Quando várias seções dividem a mesma página (ex: 1.1, 1.2, 1.3 na pág 1-1)
      ou quando uma seção termina no meio da página onde a próxima começa,
      recorta o texto exatamente entre o cabeçalho da seção atual e o cabeçalho
      da próxima seção.
    - Caso o cabeçalho não seja localizado via Regex (variação tipográfica),
      faz fallback seguro para o intervalo de páginas.
    """
    pag_inicio, pag_prox, prox_item = _pag_range(idx, estrutura, mapa_paginas, len(pdf))
    if pag_inicio is None:
        return None

    item_atual = estrutura[idx]

    # Determina até qual página ler para buscar o fim da seção atual
    if pag_prox is not None:
        # Lemos até pag_prox (inclusive) para capturar o final da seção atual
        # caso ela invada o topo da página onde começa a próxima seção
        pag_leitura_fim = min(pag_prox + 1, len(pdf))
        pag_fallback_fim = max(pag_inicio + 1, min(pag_prox, len(pdf)))
    else:
        pag_leitura_fim = min(pag_inicio + 15, len(pdf))
        pag_fallback_fim = pag_leitura_fim

    textos_por_pagina = [
        _extrair_texto_pagina_com_ocr_opcional(pdf[p])
        for p in range(pag_inicio, pag_leitura_fim)
    ]
    texto_completo = "\n".join(textos_por_pagina)

    # 1. Recorta o início a partir do cabeçalho da seção atual (ex: "1.2 CONCEITO...")
    padrao_atual = _padrao_inicio_item(item_atual)
    pos_inicio = 0
    if padrao_atual is not None:
        m_ini = padrao_atual.search(texto_completo)
        if m_ini:
            pos_inicio = m_ini.start()
            if texto_completo[pos_inicio] == "\n":
                pos_inicio += 1

    # 2. Recorta o fim exatamente antes do cabeçalho da próxima seção (ex: "1.3 OBJETIVOS...")
    texto_a_partir_inicio = texto_completo[pos_inicio:]
    cortou_no_proximo = False

    if prox_item is not None:
        padrao_prox = _padrao_inicio_item(prox_item)
        if padrao_prox is not None:
            # Pula os primeiros caracteres do próprio título atual para buscar o próximo
            offset_busca = min(len(texto_a_partir_inicio), 25)
            m_fim = padrao_prox.search(texto_a_partir_inicio[offset_busca:])
            if m_fim:
                pos_fim = offset_busca + m_fim.start()
                texto_a_partir_inicio = texto_a_partir_inicio[:pos_fim]
                cortou_no_proximo = True

    # Se não encontrou o cabeçalho da próxima seção na página pag_prox e pag_prox > pag_inicio,
    # descarta o texto da página pag_prox para não vazar a próxima seção inteira
    if not cortou_no_proximo and pag_prox is not None and pag_prox > pag_inicio:
        texto_fallback = "\n".join(textos_por_pagina[: pag_fallback_fim - pag_inicio])
        if pos_inicio < len(texto_fallback):
            texto_a_partir_inicio = texto_fallback[pos_inicio:]
        else:
            texto_a_partir_inicio = texto_fallback

    return _limpar_texto(texto_a_partir_inicio)


# =============================================================================
# ETAPA 4 — EXPORTAÇÃO DE IMAGENS
# =============================================================================

def exportar_imagens(
    caminho_pdf: Path,
    titulo_doc: str,
    estrutura: list[dict],
    mapa_paginas: dict[str, int],
    base_dir: Path = PASTA_IMAGENS,
) -> tuple[Path, int]:
    """
    Exporta as imagens embutidas no PDF para pastas organizadas por seção.
    Retorna (pasta_raiz_do_documento, total_de_imagens_salvas).
    """
    pdf = fitz.open(str(caminho_pdf))
    pasta_doc = base_dir / sanitizar(titulo_doc)
    pasta_doc.mkdir(parents=True, exist_ok=True)

    cap_pasta: Path | None = None
    total_imgs = 0

    for idx, item in enumerate(estrutura):
        if item["tipo"] == "capitulo":
            nome = sanitizar(f"CAPÍTULO {item['num']} - {item['titulo']}")
            cap_pasta = pasta_doc / nome
            cap_pasta.mkdir(parents=True, exist_ok=True)

        elif item["tipo"] == "secao":
            nome = sanitizar(f"{item['num']} {item['titulo']}")
            pasta_secao = (cap_pasta / nome) if cap_pasta else (pasta_doc / nome)
            pasta_secao.mkdir(parents=True, exist_ok=True)

            pag_i, pag_prox, _ = _pag_range(idx, estrutura, mapa_paginas, len(pdf))
            if pag_i is not None and hasattr(fitz, "Pixmap"):
                pag_f = max(pag_i + 1, pag_prox) if pag_prox is not None else min(pag_i + 15, len(pdf))
                for p in range(pag_i, min(pag_f, len(pdf))):
                    for j, img_info in enumerate(pdf[p].get_images(full=True)):
                        xref = img_info[0]
                        try:
                            pix = fitz.Pixmap(pdf, xref)
                            if pix.n > 4:  # CMYK → RGB
                                pix = fitz.Pixmap(fitz.csRGB, pix)
                            img_path = pasta_secao / f"pagina_{p+1}_img_{j+1}.png"
                            pix.save(str(img_path))
                            total_imgs += 1
                        except Exception:
                            pass  # ignora imagens corrompidas

        elif item["tipo"] in ("anexo", "especial"):
            (pasta_doc / sanitizar(item["titulo"])).mkdir(parents=True, exist_ok=True)

    pdf.close()
    return pasta_doc, total_imgs


# =============================================================================
# ETAPA 5 — EXPORTAÇÃO PARA XML
# =============================================================================

def exportar_para_xml(
    caminho_pdf: Path,
    titulo_doc: str,
    estrutura: list[dict],
    mapa_paginas: dict[str, int],
    pasta_saida: Path,
    meta_bdex: dict | None = None,
) -> Path:
    """
    Converte o conteúdo do manual para um arquivo XML estruturado.
    """
    pdf = fitz.open(str(caminho_pdf))
    root = etree.Element("documento")

    # Metadados
    meta = etree.SubElement(root, "metadados")
    etree.SubElement(meta, "titulo").text = titulo_doc
    etree.SubElement(meta, "arquivo").text = Path(caminho_pdf).name
    if meta_bdex:
        if meta_bdex.get("data"):
            etree.SubElement(meta, "data").text = str(meta_bdex["data"])
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
    nome_arquivo = sanitizar(titulo_doc) + ".xml"
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
    extrair_imgs: bool = True,
    catalogo_bdex: dict[str, dict] | None = None,
) -> dict:
    """
    Executa todas as etapas da pipeline em um único PDF:
      1. Extrai título (com fallback para catálogo BDEx)
      2. Extrai e parseia o índice
      3. Mapeia numeração de páginas (incluindo anexos A-1, B-1)
      4. Exporta imagens por seção (opcional)
      5. Exporta XML estruturado com recorte exato por seção
    """
    print(f"\n{'='*60}")
    print(f"📄 Processando: {caminho_pdf.name}")
    print(f"{'='*60}")

    meta_bdex = (catalogo_bdex or {}).get(caminho_pdf.name)
    titulo = extrair_titulo_pdf(caminho_pdf, meta_bdex=meta_bdex)
    print(f"  📌 Título: {titulo}")

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

    n_imgs = 0
    if extrair_imgs:
        pasta_imgs, n_imgs = exportar_imagens(
            caminho_pdf, titulo, estrutura, mapa, PASTA_IMAGENS
        )
        print(f"  🖼️  {n_imgs} imagem(ns) exportada(s) → {pasta_imgs}")

    caminho_xml = exportar_para_xml(
        caminho_pdf, titulo, estrutura, mapa, pasta_saida, meta_bdex=meta_bdex
    )
    print(
        f"  ✅ XML gerado: {caminho_xml.name}  "
        f"({caminho_xml.stat().st_size / 1024:.1f} KB)"
    )

    return {
        "arquivo": caminho_pdf.name,
        "titulo": titulo,
        "capitulos": cap_count,
        "secoes": sec_count,
        "paginas": len(mapa),
        "imagens": n_imgs,
        "xml": str(caminho_xml),
        "status": "ok",
    }


# =============================================================================
# EXECUÇÃO DIRETA
# =============================================================================

def main():
    global USAR_OCR_FALLBACK

    parser = argparse.ArgumentParser(
        description="Pipeline de extração de Manuais de Campanha (PDF para XML)."
    )
    parser.add_argument(
        "--sem-imagens",
        action="store_true",
        help="Pula a etapa de exportação de imagens PNG para acelerar o processamento.",
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="Ativa fallback de OCR (pytesseract) para páginas escaneadas sem texto nativo.",
    )
    args, _ = parser.parse_known_args()
    USAR_OCR_FALLBACK = args.ocr

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

    resultados = []
    for nome, caminho in pdfs.items():
        try:
            resultado = processar_pdf(
                caminho,
                PASTA_GERADOS,
                extrair_imgs=not args.sem_imagens,
                catalogo_bdex=catalogo_bdex,
            )
            resultados.append(resultado)
        except Exception as e:
            print(f"  ❌ Erro ao processar '{nome}': {e}")
            resultados.append({"arquivo": nome, "status": f"erro: {e}"})

    # Resumo final
    print(f"\n{'='*60}")
    print("  RESUMO FINAL")
    print(f"{'='*60}")
    ok = [r for r in resultados if r.get("status") == "ok"]
    print(f"  ✅ {len(ok)}/{len(resultados)} arquivo(s) processado(s) com sucesso")
    print(f"\n  XMLs gerados em '{PASTA_GERADOS}/':")
    for xml_file in sorted(PASTA_GERADOS.glob("*.xml")):
        print(f"    📝 {xml_file.name}  ({xml_file.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
