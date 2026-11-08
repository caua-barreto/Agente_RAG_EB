import math
import os
import re
import sys
import glob
import unicodedata
import xml.etree.ElementTree as _stdlib_et
from pathlib import Path

# Permite importar pacotes da .venv local caso o script seja chamado pelo python do sistema
_VENV_SITE = Path(__file__).resolve().parent / ".venv" / "lib" / "python3.12" / "site-packages"
if _VENV_SITE.exists() and str(_VENV_SITE) not in sys.path:
    sys.path.insert(0, str(_VENV_SITE))

import yaml
import dotenv

try:
    from lxml import etree
except Exception:
    etree = _stdlib_et

try:
    from rank_bm25 import BM25Okapi
except Exception:
    class BM25Okapi:
        """Implementação pura em Python do algoritmo BM25Okapi (fallback sem dependência C/numpy)."""

        def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75, epsilon: float = 0.25):
            self.k1 = k1
            self.b = b
            self.epsilon = epsilon
            self.corpus_size = len(corpus)
            self.doc_len = [len(doc) for doc in corpus]
            self.avgdl = (sum(self.doc_len) / self.corpus_size) if self.corpus_size else 1.0
            self.doc_freqs: list[dict[str, int]] = []
            self.idf: dict[str, float] = {}

            nd: dict[str, int] = {}
            for doc in corpus:
                freqs: dict[str, int] = {}
                for w in doc:
                    freqs[w] = freqs.get(w, 0) + 1
                self.doc_freqs.append(freqs)
                for w in freqs:
                    nd[w] = nd.get(w, 0) + 1

            idf_sum = 0.0
            negatives = []
            for word, freq in nd.items():
                idf = math.log(self.corpus_size - freq + 0.5) - math.log(freq + 0.5)
                self.idf[word] = idf
                idf_sum += idf
                if idf < 0:
                    negatives.append(word)

            avg_idf = (idf_sum / len(self.idf)) if self.idf else 0.0
            eps = self.epsilon * avg_idf
            for word in negatives:
                self.idf[word] = eps

        def get_scores(self, query: list[str]) -> list[float]:
            scores = [0.0] * self.corpus_size
            for q in query:
                idf = self.idf.get(q, 0.0)
                if not idf:
                    continue
                for i, freqs in enumerate(self.doc_freqs):
                    qf = freqs.get(q, 0)
                    if qf:
                        dl = self.doc_len[i]
                        scores[i] += idf * (
                            qf * (self.k1 + 1.0)
                            / (qf + self.k1 * (1.0 - self.b + self.b * dl / self.avgdl))
                        )
            return scores

        def get_top_n(self, query: list[str], documents: list, n: int = 5) -> list:
            scores = self.get_scores(query)
            indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:n]
            return [documents[i] for i in indices]


# =====================================================================
# 1. CARREGAMENTO DE VARIÁVEIS E CONFIGURAÇÕES
# =====================================================================

dotenv.load_dotenv()  # Carregando a API do DeepSeek

API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not API_KEY or API_KEY == "coloque_sua_chave_aqui_sem_aspas":
    raise RuntimeError(
        "DEEPSEEK_API_KEY não configurada. "
        "Abra o arquivo .env e coloque sua chave antes de rodar."
    )

_cliente_openai = None


def _obter_cliente():
    """Inicializa o cliente OpenAI/DeepSeek sob demanda."""
    global _cliente_openai
    if _cliente_openai is None:
        from openai import OpenAI
        _cliente_openai = OpenAI(api_key=API_KEY, base_url="https://api.deepseek.com")
    return _cliente_openai


# Carrega as configurações do arquivo YAML (configs)
_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_DIR, "configuracoes.yaml")
with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)


# =====================================================================
# 2. ESTRUTURAS DE DADOS, CATÁLOGO DE MANUAIS E MEMÓRIA FIFO
# =====================================================================
banco_de_trechos: list = []
catalogo_manuais: list[dict] = []
historico_conversa: list[dict[str, str]] = []


class TrechoMilitar:
    """Armazena um recorte estruturado de manual com seus metadados."""

    def __init__(
        self,
        manual_titulo: str,
        capitulo: str,
        secao: str,
        texto: str,
        edicao: str = "Ed. Única",
        ano: str = "S/D",
        codigo: str = "",
    ):
        self.manual_titulo = manual_titulo
        self.capitulo = capitulo
        self.secao = secao
        self.texto = texto
        self.edicao = edicao
        self.ano = ano
        self.codigo = codigo

    @property
    def edicao_formatada(self) -> str:
        """Retorna a edição combinada com o ano: ex. '5ª Ed. (2021)'."""
        if self.ano and self.ano != "S/D":
            return f"{self.edicao} ({self.ano})"
        return self.edicao

    def formatar_para_llm(self) -> str:
        """Retorna o trecho formatado para injeção no contexto do LLM."""
        return (
            f"[Manual: {self.manual_titulo} | Edição: {self.edicao_formatada} | Cap: {self.capitulo} | Sec: {self.secao}]\n"
            f"{self.texto}\n"
        )


def limpar_historico() -> None:
    """Zera o histórico de mensagens da conversa atual (memória FIFO)."""
    historico_conversa.clear()


def _registrar_turno_memoria(pergunta: str, resposta: str) -> None:
    """Adiciona o turno (user + assistant) ao histórico FIFO respeitando max_mensagens."""
    max_msg = int(config.get("memoria", {}).get("max_mensagens", 4))
    if max_msg <= 0:
        return
    historico_conversa.append({"role": "user", "content": pergunta})
    historico_conversa.append({"role": "assistant", "content": resposta})
    while len(historico_conversa) > max_msg:
        historico_conversa.pop(0)


# =====================================================================
# 3. LEITURA DOS ARQUIVOS XML E CATÁLOGO DE DOCUMENTOS ATIVOS
# =====================================================================

_PADRAO_REVOGADO = re.compile(r"(?<![A-Z])REVOGAD[OA]S?(?![A-Z])", re.IGNORECASE)


def _texto_valido(texto: str | None) -> str | None:
    """Filtra nós vazios ou marcadores de falha do Pipeline."""
    if not texto:
        return None
    limpo = texto.strip()
    if (
        not limpo
        or limpo.startswith("(Página não localizada)")
        or limpo.startswith("(Conteúdo não extraído)")
    ):
        return None
    return limpo


def _limpar_nome_manual(nome: str) -> str:
    """Remove resíduos de edição/código do nome do manual e formata com nitidez."""
    if not nome:
        return "Manual Militar"
    # Remove sufixos residuais como "- 1 - aEdição", "| 4ª Edição | 2019", etc.
    limpo = re.sub(r"\s*[-|]\s*\d+\s*[-|]?\s*[ªºa]?\s*Edi[çc][ãa]o.*$", "", nome, flags=re.IGNORECASE)
    limpo = re.sub(r"\s*\b\d+\s*[ªºa]?\s*Edi[çc][ãa]o\b.*$", "", limpo, flags=re.IGNORECASE)
    limpo = re.sub(r"\s*[-|]\s*(19\d{2}|20\d{2})\b.*$", "", limpo)
    limpo = re.sub(r"\s+-\s+", " ", limpo)
    limpo = re.sub(r"\s{2,}", " ", limpo).strip(" -|")
    if limpo.isupper():
        preposicoes = {"de", "da", "do", "das", "dos", "e", "em", "na", "no", "nas", "nos", "para", "com", "a", "o", "as", "os"}
        palavras = limpo.title().split()
        limpo = " ".join(
            p.lower() if idx > 0 and p.lower() in preposicoes else p
            for idx, p in enumerate(palavras)
        )
    return limpo or nome


def _extrair_metadados_xml(raiz, caminho: str) -> dict[str, str]:
    """Extrai nome_manual, codigo, edicao e ano do bloco <metadados> do XML."""
    def _ler_tag(tag: str) -> str:
        no = raiz.find(f".//metadados/{tag}")
        return no.text.strip() if no is not None and no.text else ""

    titulo_completo = _ler_tag("titulo") or Path(caminho).stem
    nome_manual = _ler_tag("nome_manual")
    codigo = _ler_tag("codigo")
    edicao = _ler_tag("edicao")
    ano = _ler_tag("ano")

    # Fallback para XMLs antigos que possuam apenas <titulo>
    if not edicao:
        m_ed = re.search(r"\b(\d+)\s*[ªºa]?\s*Edi[çc][ãa]o\b", titulo_completo, re.IGNORECASE)
        edicao = f"{m_ed.group(1)}ª Ed." if m_ed else "Ed. Única"

    if not ano:
        m_ano = re.findall(r"\b(19\d{2}|20\d{2})\b", titulo_completo)
        ano = m_ano[-1] if m_ano else "S/D"

    if not codigo:
        m_cod = re.search(
            r"\b(EB\d{2}-[A-Z]{2}-\d{2}\.\d+|C-\d+-\d+|MC\s*\d+[\.\-]\d+)\b",
            titulo_completo,
            re.IGNORECASE,
        )
        codigo = m_cod.group(1).upper() if m_cod else "EB-MC"

    if not nome_manual:
        partes = [p.strip() for p in titulo_completo.split("|") if p.strip()]
        candidatos = [
            p for p in partes
            if not any(t in p.upper() for t in ("COMANDO DE", "ESTADO-MAIOR", "MANUAL DE CAMPANHA", "SECRETARIA-GERAL", "DEPARTAMENTO DE"))
            and not re.search(r"\b\d+\s*[ªºa]?\s*Edi[çc][ãa]o\b", p, re.IGNORECASE)
            and not re.match(r"^(19\d{2}|20\d{2})$", p)
        ]
        nome_manual = " ".join(candidatos) if candidatos else titulo_completo

    nome_manual = _limpar_nome_manual(nome_manual)

    return {
        "titulo_completo": titulo_completo,
        "nome_manual": nome_manual,
        "codigo": codigo or "EB-MC",
        "edicao": edicao,
        "ano": ano,
    }


def carregar_xmls() -> None:
    """Lê todos os XMLs ativos da pasta configurada e popula banco_de_trechos e catalogo_manuais."""
    banco_de_trechos.clear()
    catalogo_manuais.clear()
    carregar_siglas()

    pasta_xml = config["caminhos"]["pasta_xml"]
    pasta_xml_abs = os.path.join(_DIR, pasta_xml)
    arquivos_xml = sorted(glob.glob(os.path.join(pasta_xml_abs, "*.xml")))

    if not arquivos_xml:
        print(f"AVISO: Nenhum arquivo XML encontrado em '{pasta_xml_abs}'.")
        return

    print(f"Lendo base de dados... {len(arquivos_xml)} arquivo(s) XML encontrado(s).")

    for caminho in arquivos_xml:
        nome_arq = os.path.basename(caminho)
        # Ignora manuais revogados caso algum esteja na pasta de XMLs
        if _PADRAO_REVOGADO.search(nome_arq):
            continue

        try:
            arvore = etree.parse(caminho)
        except Exception as e:
            print(f"  AVISO: XML malformado ignorado → {caminho} ({e})")
            continue

        raiz = arvore.getroot()
        meta = _extrair_metadados_xml(raiz, caminho)

        if _PADRAO_REVOGADO.search(meta["titulo_completo"]) or _PADRAO_REVOGADO.search(meta["nome_manual"]):
            continue

        nome_manual = meta["nome_manual"]
        edicao = meta["edicao"]
        ano = meta["ano"]
        codigo = meta["codigo"]

        qtd_capitulos = 0
        qtd_secoes_validas = 0

        # Percorre a árvore preservando a hierarquia (compatível com lxml e ElementTree)
        for filho in raiz:
            if filho.tag == "capitulo":
                qtd_capitulos += 1
                cap_num = filho.get("numero", "")
                cap_tit = filho.get("titulo", "Capítulo Indefinido")
                cap_nome = f"{cap_num} - {cap_tit}".strip(" -") if cap_num else cap_tit
                for secao in filho.findall("secao"):
                    sec_num = secao.get("numero", "")
                    sec_tit = secao.get("titulo", "Seção Indefinida")
                    secao_nome = f"{sec_num} {sec_tit}".strip()
                    no_txt = secao.find("texto")
                    texto = _texto_valido(no_txt.text if no_txt is not None else None)
                    if texto:
                        qtd_secoes_validas += 1
                        banco_de_trechos.append(
                            TrechoMilitar(nome_manual, cap_nome, secao_nome, texto, edicao=edicao, ano=ano, codigo=codigo)
                        )

            elif filho.tag == "secao":
                sec_num = filho.get("numero", "")
                sec_tit = filho.get("titulo", "Seção Indefinida")
                secao_nome = f"{sec_num} {sec_tit}".strip()
                no_txt = filho.find("texto")
                texto = _texto_valido(no_txt.text if no_txt is not None else None)
                if texto:
                    qtd_secoes_validas += 1
                    banco_de_trechos.append(
                        TrechoMilitar(nome_manual, "Sem Capítulo", secao_nome, texto, edicao=edicao, ano=ano, codigo=codigo)
                    )

            elif filho.tag == "anexo":
                letra = filho.get("letra", "")
                cap_nome = f"Anexo {letra}".strip()
                secao_nome = filho.get("titulo", "Anexo")
                no_txt = filho.find("texto")
                texto = _texto_valido(no_txt.text if no_txt is not None else None)
                if texto:
                    qtd_secoes_validas += 1
                    banco_de_trechos.append(
                        TrechoMilitar(nome_manual, cap_nome, secao_nome, texto, edicao=edicao, ano=ano, codigo=codigo)
                    )

            elif filho.tag == "secao_especial":
                secao_nome = filho.get("titulo", "Seção Especial")
                no_txt = filho.find("texto")
                texto = _texto_valido(no_txt.text if no_txt is not None else None)
                if texto:
                    qtd_secoes_validas += 1
                    banco_de_trechos.append(
                        TrechoMilitar(nome_manual, "Seção Especial", secao_nome, texto, edicao=edicao, ano=ano, codigo=codigo)
                    )

        if qtd_secoes_validas > 0:
            edicao_completa = f"{edicao} ({ano})" if ano and ano != "S/D" else edicao
            catalogo_manuais.append(
                {
                    "id": nome_arq,
                    "nome_manual": nome_manual,
                    "titulo_completo": meta["titulo_completo"],
                    "codigo": codigo,
                    "edicao": edicao,
                    "ano": ano,
                    "edicao_completa": edicao_completa,
                    "arquivo": nome_arq,
                    "capitulos": qtd_capitulos,
                    "secoes": qtd_secoes_validas,
                    "status": "ativo",
                }
            )

    catalogo_manuais.sort(key=lambda m: m["nome_manual"].lower())
    print(
        f"Extração concluída! {len(catalogo_manuais)} manual(is) ativo(s) e "
        f"{len(banco_de_trechos)} recortes carregados."
    )


def obter_catalogo_manuais() -> dict:
    """Retorna o catálogo estruturado de manuais ativos indexados."""
    return {
        "total_manuais": len(catalogo_manuais),
        "total_trechos": len(banco_de_trechos),
        "total_siglas": sum(1 for v in SIGLAS_MILITARES.values() if v),
        "manuais": catalogo_manuais,
    }


# =====================================================================
# 4. MOTOR DE BUSCA BM25 — com normalização Unicode e siglas externas
# =====================================================================

_SIGLAS_PADRAO: dict[str, list[str]] = {
    "tfm": ["treinamento", "fisico", "militar"],
    "taf": ["teste", "avaliacao", "fisica"],
    "ou": ["ordem", "unida"],
    "eb": ["exercito", "brasileiro"],
    "om": ["organizacao", "militar"],
    "glo": ["garantia", "lei", "ordem"],
}

SIGLAS_MILITARES: dict[str, list[str]] = dict(_SIGLAS_PADRAO)
motor_bm25 = None  # instância global do BM25


def _normalizar(texto: str) -> str:
    """
    Remove acentos via decomposição NFKD, converte para minúsculas e limpa
    pontuação colada às palavras (ex: 'TFM?' -> 'tfm'), garantindo que a
    expansão de siglas e o match de tokens no BM25 funcionem com precisão.
    """
    nfkd = unicodedata.normalize("NFKD", texto.lower())
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", " ", sem_acento)


def carregar_siglas() -> dict[str, list[str]]:
    """
    Carrega o dicionário de siglas militares a partir do arquivo externo
    configurado em configuracoes.yaml (padrão: siglas.yaml).
    """
    global SIGLAS_MILITARES
    arq_siglas = config.get("caminhos", {}).get("arquivo_siglas", "siglas.yaml")
    caminho_siglas = os.path.join(_DIR, arq_siglas)

    if not os.path.exists(caminho_siglas):
        print(f"AVISO: Arquivo de siglas '{caminho_siglas}' não encontrado. Usando padrão interno.")
        SIGLAS_MILITARES = dict(_SIGLAS_PADRAO)
        return SIGLAS_MILITARES

    try:
        with open(caminho_siglas, "r", encoding="utf-8") as f:
            dados = yaml.safe_load(f) or {}
        fonte = dados.get("siglas", dados) if isinstance(dados, dict) else {}
        siglas_carregadas: dict[str, list[str]] = {}
        for chave, valores in fonte.items():
            chave_norm = _normalizar(str(chave)).strip()
            if not chave_norm:
                continue
            if isinstance(valores, list):
                termos = []
                for v in valores:
                    termos.extend(_normalizar(str(v)).split())
                siglas_carregadas[chave_norm] = termos
            elif isinstance(valores, str):
                siglas_carregadas[chave_norm] = _normalizar(valores).split()
            else:
                siglas_carregadas[chave_norm] = []

        if siglas_carregadas:
            SIGLAS_MILITARES = siglas_carregadas
    except Exception as e:
        print(f"AVISO: Erro ao ler '{caminho_siglas}' ({e}). Usando siglas padrão.")
        SIGLAS_MILITARES = dict(_SIGLAS_PADRAO)

    return SIGLAS_MILITARES


# Inicializa as siglas ao importar o módulo
carregar_siglas()


def _expandir_siglas(tokens: list[str]) -> list[str]:
    """
    Para cada token que seja uma sigla conhecida, adiciona os termos
    equivalentes sem remover a sigla original da query.
    """
    resultado = []
    for token in tokens:
        resultado.append(token)
        expansao = SIGLAS_MILITARES.get(token, [])
        resultado.extend(expansao)
    return resultado


def preparar_motor_de_busca() -> None:
    """Indexa todos os trechos no BM25 com tokenização normalizada."""
    global motor_bm25
    if not banco_de_trechos:
        print("AVISO: banco_de_trechos vazio. Motor de busca não inicializado.")
        return

    textos_tokenizados = []
    for trecho in banco_de_trechos:
        texto_rico = (
            f"{trecho.manual_titulo} {trecho.codigo} {trecho.edicao} {trecho.ano} "
            f"{trecho.capitulo} {trecho.secao} {trecho.texto}"
        )
        tokens = _normalizar(texto_rico).split()
        textos_tokenizados.append(tokens)

    motor_bm25 = BM25Okapi(textos_tokenizados)
    print(f"Motor de busca BM25 calibrado e pronto ({len(SIGLAS_MILITARES)} siglas ativas).")


def buscar_trechos_relevantes(pergunta: str, top_n: int | None = None) -> list[TrechoMilitar]:
    """
    Retorna os top_n trechos mais relevantes para a pergunta usando BM25.
    Aplica normalização Unicode, remoção de pontuação e expansão de siglas na query.
    """
    if motor_bm25 is None:
        return []

    if top_n is None:
        top_n = int(config.get("busca", {}).get("top_n", 10))

    tokens_base = _normalizar(pergunta).split()
    tokens_expandidos = _expandir_siglas(tokens_base)

    resultados = motor_bm25.get_top_n(tokens_expandidos, banco_de_trechos, n=top_n)

    vistos: set[int] = set()
    unicos: list[TrechoMilitar] = []
    for trecho in resultados:
        if id(trecho) not in vistos:
            vistos.add(id(trecho))
            unicos.append(trecho)

    return unicos


# =====================================================================
# 5. CHATBOT — INTEGRAÇÃO COM DEEPSEEK E MEMÓRIA FIFO
# =====================================================================

def perguntar_ao_assistente_stream(pergunta_usuario: str):
    """Orquestra a busca RAG e envia a chamada para o LLM DeepSeek, retornando um gerador de texto."""

    # 1. Recupera trechos relevantes conforme configurado em configuracoes.yaml
    top_n = int(config.get("busca", {}).get("top_n", 10))
    documentos = buscar_trechos_relevantes(pergunta_usuario, top_n=top_n)

    # 2. Monta o contexto numerado
    if documentos:
        blocos = [
            f"[Trecho {i}]\n{trecho.formatar_para_llm()}"
            for i, trecho in enumerate(documentos, start=1)
        ]
        contexto_str = "\n".join(blocos)
    else:
        contexto_str = "[Nenhuma informação encontrada na base de manuais]"

    # 3. Monta o system prompt + contexto
    prompt_sistema = (
        f"{config['comportamento']['prompt_do_sistema']}\n\n"
        f"=== CONTEXTO DOS MANUAIS ({len(documentos)} trechos recuperados) ===\n"
        f"{contexto_str}\n"
        f"======================================================================\n"
    )

    # 4. Monta a lista de mensagens incluindo a memória FIFO da sessão atual
    mensagens = [
        {"role": "system", "content": prompt_sistema},
        *historico_conversa,
        {"role": "user", "content": pergunta_usuario},
    ]

    # 5. Chamada à API via Streaming
    cliente = _obter_cliente()
    resposta = cliente.chat.completions.create(
        model=config["modelo"]["nome"],
        temperature=config["modelo"]["temperatura"],
        max_tokens=config["modelo"]["max_tokens"],
        messages=mensagens,
        stream=True,
    )

    acumulador_resposta = []
    for chunk in resposta:
        texto = chunk.choices[0].delta.content or ""
        if texto:
            acumulador_resposta.append(texto)
            yield texto

    # 6. Atualiza a memória FIFO ao finalizar o stream
    resposta_completa = "".join(acumulador_resposta).strip()
    if resposta_completa:
        _registrar_turno_memoria(pergunta_usuario, resposta_completa)


def perguntar_ao_assistente(pergunta_usuario: str) -> str:
    """Wrapper síncrono para o terminal e rota /api/chat."""
    gerador = perguntar_ao_assistente_stream(pergunta_usuario)
    return "".join(list(gerador))


# =====================================================================
# 6. INTERFACE NO TERMINAL (uso direto via python assistente.py)
# =====================================================================
if __name__ == "__main__":
    print("-" * 55)
    print("  PreparAI — SISTEMA DE PESQUISA DOCUMENTAL MILITAR")
    print("-" * 55)

    carregar_xmls()
    preparar_motor_de_busca()

    print("\nSistema pronto. Digite 'sair' para encerrar.\n")
    while True:
        try:
            pergunta = input("Você: ")
        except (EOFError, KeyboardInterrupt):
            print("\nEncerrando...")
            break

        if pergunta.strip().lower() in {"sair", "exit", "quit"}:
            print("Encerrando o sistema.")
            break

        if not pergunta.strip():
            continue

        print("\nConsultando os manuais...\n")
        try:
            resposta_ia = perguntar_ao_assistente(pergunta)
            print(f"PreparAI: {resposta_ia}\n")
        except Exception as e:
            print(f"ERRO: {e}\n")
