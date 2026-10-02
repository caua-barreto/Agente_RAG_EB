"""
pipeline.py — PreparAI
========================
Extrai o conteúdo de manuais militares (Somente no padrão de MANUAL do EB) em PDF e os exporta como XML
estruturado para uso pelo motor de busca BM25 do assistente.

Uso direto:
    python scripts/pipeline.py

Objetivo é:

LImpar o documento e remover erros/strings desnecessárias
Extrair título, índice, capítulos, seções,etc

converter para xml estruturado a partir de dicionário.

Os PDFs devem estar em  ../Arquivos/
Os XMLs serão gerados em ../Gerados/
"""

import re
import os
from pathlib import Path
from collections import OrderedDict

import fitz          # PyMuPDF
from lxml import etree

# ── Pastas padrão (relativas ao diretório raiz do projeto) ─────────────────
_RAIZ = Path(__file__).resolve().parent.parent
PASTA_ARQUIVOS = _RAIZ / "Arquivos"
PASTA_GERADOS  = _RAIZ / "Gerados"


# =============================================================================
# UTILIDADES GERAIS
# =============================================================================

#remove arquivos invalidos

def sanitizar(nome: str, max_len: int = 200) -> str:
    """Remove caracteres inválidos para nomes de arquivo/pasta e trunca."""
    nome = re.sub(r'[/\\:*?"<>|]', "", nome).strip()
    return nome[:max_len]


def descobrir_pdfs(pasta: Path = PASTA_ARQUIVOS) -> dict[str, Path]:
    """
    Varre a pasta de entrada e retorna um dicionário {nome_sem_ext: Path}.
    """
    if not pasta.exists():
        raise FileNotFoundError(f"Pasta de PDFs não encontrada: {pasta}")

    pdfs = {p.stem: p for p in sorted(pasta.glob("*.pdf"))}
    print(f"📂 {len(pdfs)} PDF(s) encontrado(s) em '{pasta}'")
    return pdfs


# =============================================================================
# ETAPA 1 — EXTRAÇÃO DE TÍTULO
# =============================================================================

#Titulo está sempre nesse range de 4 e 7

def extrair_titulo_pdf(caminho_pdf: Path) -> str:
    """
    Extrai o título do manual a partir das primeiras linhas da página 1.
    Os manuais do EB seguem o padrão: linhas 4–7 contêm o título completo.
    """
    pdf = fitz.open(str(caminho_pdf))
    texto_pagina_1 = pdf[0].get_text("text")
    pdf.close()

    linhas = [
        re.sub(r"\s+", " ", l).strip()
        for l in texto_pagina_1.splitlines()
        if re.sub(r"\s+", " ", l).strip()
    ]

    if len(linhas) >= 7:
        titulo = " | ".join(linhas[3:7])
    elif len(linhas) >= 4:
        titulo = " ".join(linhas[3:])
    else:
        titulo = " | ".join(linhas)

    return re.sub(r"\s{2,}", " ", titulo).strip()


# =============================================================================
# ETAPA 2 — EXTRAÇÃO E PARSE DO ÍNDICE
# =============================================================================

# Extraindo cabeçalho

def _eh_conteudo_real(linha_strip: str) -> bool:
    """Verifica se uma linha é cabeçalho de conteúdo real (não entrada de índice)."""
    if "…" in linha_strip or "..." in linha_strip:
        return False
    m = re.match(r"^\d+\.\d+\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-ZÁÉÍÓÚÂÊÔÃÕÇ]+)\b", linha_strip)
    return bool(m and len(m.group(1)) >= 3)

# Extraindo o indice de assuntos que serão convertidos no formato
def extrair_indice_pdf(caminho_pdf: Path) -> str:
    """
    Extrai o texto do índice entre 'ÍNDICE DE ASSUNTOS' e o início
    do Capítulo I real (primeira linha de conteúdo efetivo).
    """
    pdf = fitz.open(str(caminho_pdf))
    indice = ""
    capturando = False

    for pagina in pdf:
        texto = pagina.get_text("text")
        if not capturando:
            if "NDICE DE ASSUNTOS" in texto:
                capturando = True
                idx = texto.find("NDICE DE ASSUNTOS")
                indice += texto[idx:] + "\n"
        else:
            for linha in texto.splitlines():
                if _eh_conteudo_real(linha.strip()):
                    pdf.close()
                    return indice
            indice += texto + "\n"

    pdf.close()
    return indice

# removendo marcações soltas
def _preprocessar_indice(indice_texto: str) -> str:
    """
    Normaliza o texto do índice:
    - Une linhas quebradas de títulos de capítulo e seção
    - Remove marcadores de página soltos ('Pag', 'Pág')
    - Cola numerações de página ao item anterior
    """
    linhas = indice_texto.splitlines()
    linhas_juntas = []
    i = 0
    while i < len(linhas):
        ls = linhas[i].strip()
        if not ls:
            i += 1
            continue
        # "CAPÍTULO" sozinho na linha → une com a próxima
        if re.match(r"^CAP[ÍI]TULO$", ls) and i + 1 < len(linhas):
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
        if ls in ("Pag", "Pág", "PAG"):
            continue
        # Numeração de página cola no item anterior
        if re.match(r"^\d+-\d+$", ls) and resultado:
            resultado[-1] += " " + ls
            continue
        eh_nova_entrada = bool(
            re.match(r"CAP[ÍI]TULO\s+[IVXLCDM]+", ls)
            or re.match(r"\d+\.\d+\s+", ls)
            or re.match(r"ANEXO\s+[A-Z]", ls)
            or re.match(r"^(PREF[ÁA]CIO|GLOSS[ÁA]RIO|REFER[ÊE]NCIAS)", ls, re.IGNORECASE)
            or "NDICE DE ASSUNTOS" in ls
        )
        if eh_nova_entrada:
            resultado.append(ls)
        elif resultado:
            resultado[-1] += " " + ls

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
        if not linha or "NDICE DE ASSUNTOS" in linha:
            continue

        # Capítulo
        m = re.match(r"CAP[ÍI]TULO\s+([IVXLCDM]+)\s*[–\-—]\s*(.+)", linha)
        if m:
            titulo = re.sub(r"[\.…]+[\s\d\-]*$", "", m.group(2)).strip()
            pag_m = re.search(r"(\d+-\d+)\s*$", linha)
            estrutura.append({
                "tipo": "capitulo", "num": m.group(1),
                "titulo": titulo, "pagina": pag_m.group(1) if pag_m else None,
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
                    "tipo": "secao", "num": num,
                    "titulo": titulo, "pagina": pag_m.group(1) if pag_m else None,
                })
            continue

        # Anexo (ex: ANEXO A — Tabela de Avaliação)
        m = re.match(r"ANEXO\s+([A-Z])\s*[–\-—]\s*(.+)", linha)
        if m:
            titulo = re.sub(r"[\.…]+[\s\d\-]*$", "", m.group(2)).strip()
            estrutura.append({
                "tipo": "anexo", "num": f"Anexo {m.group(1)}",
                "titulo": titulo, "pagina": None,
            })
            continue

        # Seções especiais: Prefácio, Glossário, Referências
        for prefixo in ["PREFÁCIO", "PREFACIO", "GLOSSÁRIO", "GLOSSARIO",
                         "REFERÊNCIAS", "REFERENCIAS"]:
            if linha.upper().startswith(prefixo):
                pag_m = re.search(r"(\d+-\d+)\s*$", linha)
                estrutura.append({
                    "tipo": "especial", "num": "",
                    "titulo": prefixo.title(),
                    "pagina": pag_m.group(1) if pag_m else None,
                })
                break

    return estrutura


# =============================================================================
# ETAPA 3 — MAPEAMENTO E EXTRAÇÃO DE TEXTO POR PÁGINA
# =============================================================================

#lÊ rodapés das páginas
def mapear_paginas(caminho_pdf: Path) -> dict[str, int]:
    """
    Lê os rodapés de numeração do tipo '1-5', '2-12' de cada página
    e retorna {numeracao_manual: indice_pagina_pdf}.
    """
    pdf = fitz.open(str(caminho_pdf))
    mapa: dict[str, int] = {}
    for i, pagina in enumerate(pdf):
        texto = pagina.get_text("text")
        for linha in reversed(texto.splitlines()):
            ls = linha.strip()
            if re.match(r"^(\d+-\d+)$", ls):
                mapa[ls] = i
                break
    pdf.close()
    return mapa


def _pag_range(
    idx: int, estrutura: list[dict], mapa_paginas: dict[str, int], pdf_len: int
) -> tuple[int | None, int | None]:
    """Calcula o intervalo de páginas [inicio, fim) para o item na posição idx."""
    item = estrutura[idx]
    pag_inicio = mapa_paginas.get(item.get("pagina")) if item.get("pagina") else None
    if pag_inicio is None:
        return None, None

    pag_fim = None
    for prox in estrutura[idx + 1:]:
        pp = mapa_paginas.get(prox.get("pagina")) if prox.get("pagina") else None
        if pp is not None and pp > pag_inicio:
            pag_fim = pp
            break

    if pag_fim is None:
        pag_fim = min(pag_inicio + 20, pdf_len)

    return pag_inicio, pag_fim

#remove artefatos comunns desses manuais como marcações e codigos EB70-MC-10.xxx, numeração de páginas isoladas e excesso de linhas em branco
def _limpar_texto(texto: str) -> str:
    """
    Remove artefatos comuns dos PDFs militares:
    - Códigos de manual (EB70-MC-10.xxx)
    - Numerações de página isoladas (1-5, 2-12)
    - Excesso de linhas em branco
    """
    texto = re.sub(r"EB\d{2}-MC-\d{2}\.\d+", "", texto)
    texto = re.sub(r"^\s*\d+-\d+\s*$", "", texto, flags=re.MULTILINE)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def _extrair_texto_paginas(pdf, pag_inicio: int, pag_fim: int) -> str:
    """Concatena e limpa o texto de um intervalo de páginas do PDF aberto (fitz).
    pag_inicio é inclusivo, pag_fim é exclusivo. Nunca ultrapassa o tamanho do PDF.
    """
    texto = ""
    for p in range(pag_inicio, min(pag_fim, len(pdf))):
        texto += pdf[p].get_text("text") + "\n"
    return _limpar_texto(texto)


# =============================================================================
# ETAPA 4 — CONSTRUÇÃO DO DICIONÁRIO ESTRUTURADO
# =============================================================================

def construir_documento(
    caminho_pdf: Path, estrutura: list[dict], mapa_paginas: dict[str, int]
) -> OrderedDict:
    """
    Retorna um dicionário aninhado com o conteúdo completo do manual:
        {titulo_capitulo: {titulo_secao: texto, ...}, ...}
    """
    pdf = fitz.open(str(caminho_pdf))
    documento: OrderedDict = OrderedDict()
    cap_atual = None

    for idx, item in enumerate(estrutura):
        if item["tipo"] == "capitulo":
            cap_atual = item["titulo"]
            documento[cap_atual] = OrderedDict()

        elif item["tipo"] == "secao":
            pag_i, pag_f = _pag_range(idx, estrutura, mapa_paginas, len(pdf))
            texto = (
                _extrair_texto_paginas(pdf, pag_i, pag_f)
                if pag_i is not None
                else "(Página não localizada)"
            )
            if cap_atual and cap_atual in documento:
                documento[cap_atual][item["titulo"]] = texto
            else:
                documento[item["titulo"]] = texto

        elif item["tipo"] in ("anexo", "especial"):
            pag_i, pag_f = _pag_range(idx, estrutura, mapa_paginas, len(pdf))
            documento[item["titulo"]] = (
                _extrair_texto_paginas(pdf, pag_i, pag_f)
                if pag_i is not None
                else "(Conteúdo não extraído)"
            )

    pdf.close()
    return documento


# =============================================================================
# ETAPA 5 — EXPORTAÇÃO DE IMAGENS
# =============================================================================

def exportar_imagens(
    caminho_pdf: Path,
    titulo_doc: str,
    estrutura: list[dict],
    mapa_paginas: dict[str, int],
    base_dir: Path,
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

            pag_i, pag_f = _pag_range(idx, estrutura, mapa_paginas, len(pdf))
            if pag_i is not None:
                for p in range(pag_i, pag_f):
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
# ETAPA 6 — EXPORTAÇÃO PARA XML
# =============================================================================

def exportar_para_xml(
    caminho_pdf: Path,
    titulo_doc: str,
    estrutura: list[dict],
    mapa_paginas: dict[str, int],
    pasta_saida: Path,
) -> Path:
    """
    Converte o conteúdo do manual para um arquivo XML estruturado:

        <documento>
          <metadados>
            <titulo>...</titulo>
            <arquivo>...</arquivo>
          </metadados>
          <capitulo numero="I" titulo="...">
            <secao numero="1.1" titulo="...">
              <texto>...</texto>
            </secao>
          </capitulo>
          <anexo letra="A" titulo="...">
            <texto>...</texto>
          </anexo>
        </documento>
    """
    pdf = fitz.open(str(caminho_pdf))
    root = etree.Element("documento")

    # Metadados
    meta = etree.SubElement(root, "metadados")
    etree.SubElement(meta, "titulo").text  = titulo_doc
    etree.SubElement(meta, "arquivo").text = Path(caminho_pdf).name

    cap_node = None

    for idx, item in enumerate(estrutura):
        if item["tipo"] == "capitulo":
            cap_node = etree.SubElement(root, "capitulo")
            cap_node.set("numero", item["num"])
            cap_node.set("titulo",  item["titulo"])

        elif item["tipo"] == "secao":
            pag_i, pag_f = _pag_range(idx, estrutura, mapa_paginas, len(pdf))
            secao_node = etree.SubElement(
                cap_node if cap_node is not None else root, "secao"
            )
            secao_node.set("numero", item["num"])
            secao_node.set("titulo",  item["titulo"])
            texto_node = etree.SubElement(secao_node, "texto")
            texto_node.text = (
                _extrair_texto_paginas(pdf, pag_i, pag_f)
                if pag_i is not None
                else "(Página não localizada)"
            )

        elif item["tipo"] == "anexo":
            pag_i, pag_f = _pag_range(idx, estrutura, mapa_paginas, len(pdf))
            anexo_node = etree.SubElement(root, "anexo")
            letra_m = re.search(r"[A-Z]$", item.get("num", ""))
            anexo_node.set("letra",  letra_m.group(0) if letra_m else "")
            anexo_node.set("titulo", item["titulo"])
            texto_node = etree.SubElement(anexo_node, "texto")
            texto_node.text = (
                _extrair_texto_paginas(pdf, pag_i, pag_f)
                if pag_i is not None
                else "(Conteúdo não extraído)"
            )

        elif item["tipo"] == "especial":
            pag_i, pag_f = _pag_range(idx, estrutura, mapa_paginas, len(pdf))
            esp_node = etree.SubElement(root, "secao_especial")
            esp_node.set("titulo", item["titulo"])
            texto_node = etree.SubElement(esp_node, "texto")
            texto_node.text = (
                _extrair_texto_paginas(pdf, pag_i, pag_f)
                if pag_i is not None
                else "(Conteúdo não extraído)"
            )

    pdf.close()

    pasta_saida.mkdir(parents=True, exist_ok=True)
    nome_arquivo = sanitizar(titulo_doc) + ".xml"
    caminho_xml  = pasta_saida / nome_arquivo
    etree.ElementTree(root).write(
        str(caminho_xml),
        encoding="UTF-8",
        xml_declaration=True,
        pretty_print=True,
    )
    return caminho_xml


# =============================================================================
# PIPELINE COMPLETO — processa um único PDF
# =============================================================================

def processar_pdf(caminho_pdf: Path, pasta_saida: Path = PASTA_GERADOS) -> dict:
    """
    Executa todas as etapas da pipeline em um único PDF:
      1. Extrai título
      2. Extrai e parseia o índice
      3. Mapeia numeração de páginas
      4. Exporta imagens por seção
      5. Exporta XML estruturado

    Retorna um dicionário com os metadados do processamento.
    """
    print(f"\n{'='*60}")
    print(f"📄 Processando: {caminho_pdf.name}")
    print(f"{'='*60}")

    titulo = extrair_titulo_pdf(caminho_pdf)
    print(f"  📌 Título: {titulo}")

    indice = extrair_indice_pdf(caminho_pdf)
    if not indice.strip():
        print("  ⚠️  Índice não encontrado — arquivo ignorado.")
        return {"arquivo": caminho_pdf.name, "status": "sem_indice"}

    estrutura = parsear_indice(indice)
    mapa      = mapear_paginas(caminho_pdf)

    cap_count = sum(1 for i in estrutura if i["tipo"] == "capitulo")
    sec_count = sum(1 for i in estrutura if i["tipo"] == "secao")
    print(f"  📋 {cap_count} capítulo(s), {sec_count} seção(ões)")
    print(f"  🗺️  {len(mapa)} página(s) mapeada(s)")

    pasta_imgs, n_imgs = exportar_imagens(
        caminho_pdf, titulo, estrutura, mapa, PASTA_GERADOS.parent / "Imagens"
    )
    print(f"  🖼️  {n_imgs} imagem(ns) exportada(s) → {pasta_imgs}")

    caminho_xml = exportar_para_xml(caminho_pdf, titulo, estrutura, mapa, pasta_saida)
    print(f"  ✅ XML gerado: {caminho_xml.name}  "
          f"({caminho_xml.stat().st_size / 1024:.1f} KB)")

    return {
        "arquivo":    caminho_pdf.name,
        "titulo":     titulo,
        "capitulos":  cap_count,
        "secoes":     sec_count,
        "paginas":    len(mapa),
        "imagens":    n_imgs,
        "xml":        str(caminho_xml),
        "status":     "ok",
    }


# =============================================================================
# EXECUÇÃO DIRETA
# =============================================================================

def main():
    print("=" * 60)
    print("  PREPARAI — PIPELINE DE EXTRAÇÃO DE MANUAIS")
    print("=" * 60)

    pdfs = descobrir_pdfs(PASTA_ARQUIVOS)
    if not pdfs:
        print("Nenhum PDF encontrado. Coloque os arquivos na pasta 'Arquivos/'.")
        return

    resultados = []
    for nome, caminho in pdfs.items():
        try:
            resultado = processar_pdf(caminho, PASTA_GERADOS)
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
