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
# 2. ESTRUTURAS DE DADOS E MEMÓRIA FIFO
# =====================================================================
banco_de_trechos: list = []
historico_conversa: list[dict[str, str]] = []


class TrechoMilitar:
    """Armazena um recorte estruturado de manual com seus metadados."""

    def __init__(self, manual_titulo: str, capitulo: str, secao: str, texto: str):
        self.manual_titulo = manual_titulo
        self.capitulo = capitulo
        self.secao = secao
        self.texto = texto

    def formatar_para_llm(self) -> str:
        """Retorna o trecho formatado para injeção no contexto do LLM."""
        return (
            f"[Manual: {self.manual_titulo} | Cap: {self.capitulo} | Sec: {self.secao}]\n"
            f"{self.texto}\n"
        )


def limpar_historico() -> None:
    """Zera o histórico de mensagens da conversa atual (memória FIFO)."""
    historico_conversa.clear()


def _registrar_turno_memoria(pergunta: str, resposta: str) -> None:
    """Adiciona o turno (user + assistant) ao histórico FIFO respeitando max_mensagens."""
    max_msg = int(config.get("memoria", {}).get("max_mensagens", 6))
    if max_msg <= 0:
        return
    historico_conversa.append({"role": "user", "content": pergunta})
    historico_conversa.append({"role": "assistant", "content": resposta})
    while len(historico_conversa) > max_msg:
        historico_conversa.pop(0)


# =====================================================================
# 3. LEITURA DOS ARQUIVOS XML
# =====================================================================

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


def carregar_xmls() -> None:
    """Lê todos os XMLs da pasta configurada e popula banco_de_trechos."""
    banco_de_trechos.clear()
    pasta_xml = config["caminhos"]["pasta_xml"]
    pasta_xml_abs = os.path.join(_DIR, pasta_xml)
    arquivos_xml = sorted(glob.glob(os.path.join(pasta_xml_abs, "*.xml")))

    if not arquivos_xml:
        print(f"AVISO: Nenhum arquivo XML encontrado em '{pasta_xml_abs}'.")
        return

    print(f"Lendo base de dados... {len(arquivos_xml)} manual(is) encontrado(s).")

    for caminho in arquivos_xml:
        try:
            arvore = etree.parse(caminho)
        except Exception as e:
            print(f"  AVISO: XML malformado ignorado → {caminho} ({e})")
            continue

        raiz = arvore.getroot()

        no_titulo = raiz.find(".//metadados/titulo")
        titulo_manual = (
            no_titulo.text.strip()
            if no_titulo is not None and no_titulo.text
            else "Manual Desconhecido"
        )

        # Percorre a árvore preservando a hierarquia (compatível com lxml e ElementTree)
        for filho in raiz:
            if filho.tag == "capitulo":
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
                        banco_de_trechos.append(
                            TrechoMilitar(titulo_manual, cap_nome, secao_nome, texto)
                        )

            elif filho.tag == "secao":
                sec_num = filho.get("numero", "")
                sec_tit = filho.get("titulo", "Seção Indefinida")
                secao_nome = f"{sec_num} {sec_tit}".strip()
                no_txt = filho.find("texto")
                texto = _texto_valido(no_txt.text if no_txt is not None else None)
                if texto:
                    banco_de_trechos.append(
                        TrechoMilitar(titulo_manual, "Sem Capítulo", secao_nome, texto)
                    )

            elif filho.tag == "anexo":
                letra = filho.get("letra", "")
                cap_nome = f"Anexo {letra}".strip()
                secao_nome = filho.get("titulo", "Anexo")
                no_txt = filho.find("texto")
                texto = _texto_valido(no_txt.text if no_txt is not None else None)
                if texto:
                    banco_de_trechos.append(
                        TrechoMilitar(titulo_manual, cap_nome, secao_nome, texto)
                    )

            elif filho.tag == "secao_especial":
                secao_nome = filho.get("titulo", "Seção Especial")
                no_txt = filho.find("texto")
                texto = _texto_valido(no_txt.text if no_txt is not None else None)
                if texto:
                    banco_de_trechos.append(
                        TrechoMilitar(titulo_manual, "Seção Especial", secao_nome, texto)
                    )

    print(f"Extração concluída! {len(banco_de_trechos)} recortes carregados.")


# =====================================================================
# 4. MOTOR DE BUSCA BM25 — com normalização Unicode e expansão de siglas
# =====================================================================

SIGLAS_MILITARES: dict[str, list[str]] = {
    # Testes e Treinamentos (TFM e Instrução)
    "tfm":  ["treinamento", "fisico", "militar"],
    "tiai": ["treinamento", "intervalado", "alta", "intensidade"],
    "taf":  ["teste", "avaliacao", "fisica"],
    "tap":  ["teste", "aptidao", "fisica"],
    "cmb":  ["combate", "manutencao", "boa", "forma"],
    "ppm":  ["pista", "progressao", "militar", "pentatlo"],
    "ptc":  ["pista", "treinamento", "circuito"],
    "tib":  ["tiro", "instrucao", "basico"],
    "tia":  ["tiro", "instrucao", "avancado"],
    "tat":  ["tiro", "acao", "tatica"],
    "pccs": ["preparo", "condicionamento", "combate", "sobrevivencia"],
    "ou":   ["ordem", "unida"],

    # Organizações e Instituições (EB)
    "eb":     ["exercito", "brasileiro"],
    "om":     ["organizacao", "militar"],
    "qg":     ["quartel", "general"],
    "ime":    ["instituto", "militar", "engenharia"],
    "aman":   ["academia", "militar", "agulhas", "negras"],
    "esa":    ["escola", "sargentos", "armas"],
    "espcex": ["escola", "preparatoria", "cadetes", "exercito"],
    "cigs":   ["centro", "instrucao", "guerra", "selva"],
    "cpor":   ["centro", "preparacao", "oficiais", "reserva"],
    "npor":   ["nucleo", "preparacao", "oficiais", "reserva"],
    "esfcex": ["escola", "formacao", "complementar", "exercito"],

    # Cursos e Formações
    "cfsd": ["curso", "formacao", "soldados"],
    "cfc":  ["curso", "formacao", "cabos"],
    "cfs":  ["curso", "formacao", "sargentos"],
    "cfo":  ["curso", "formacao", "oficiais"],
    "eib":  ["estagio", "instrucao", "basica"],
    "epcv": ["estagio", "preparacao", "comandantes", "vtr"],

    # Frações e Escalões (unificado: Bda = Brigada / Boletim)
    "bda":  ["brigada", "boletim", "acesso", "restrito"],
    "btl":  ["batalhao"],
    "cia":  ["companhia"],
    "pel":  ["pelotao"],
    "gc":   ["grupo", "combate"],
    "sqd":  ["esquadrao"],
    "bia":  ["bateria"],

    # Documentos e Manuais
    "ip":   ["instrucao", "provisoria"],
    "ig":   ["instrucoes", "gerais"],
    "ir":   ["instrucoes", "reguladoras"],
    "mc":   ["manual", "campanha"],
    "mt":   ["manual", "tecnico"],
    "cise": ["caderno", "instrucao", "suporte", "ensino"],
    "pci":  ["pedido", "cooperacao", "instrucao"],
    "bi":   ["boletim", "interno"],
    "bg":   ["boletim", "geral"],

    # Funções, Seções e Especialidades
    "cmt":  ["comandante"],
    "scmt": ["subcomandante"],
    "adj":  ["adjunto"],
    "pqd":  ["paraquedista"],
    "pe":   ["policia", "exercito"],
    "s1":   ["secao", "pessoal"],
    "s2":   ["secao", "inteligencia"],
    "s3":   ["secao", "operacoes"],
    "s4":   ["secao", "logistica"],

    # Diversos (Operacionais e Administrativos)
    "glo":  ["garantia", "lei", "ordem"],
    "vtr":  ["viatura"],
    "armt": ["armamento"],
    "mto":  ["manutencao"],
    "sup":  ["suprimento"],
    "epi":  ["equipamento", "protecao", "individual"],
    "ev":   ["efetivo", "variavel"],
    "ep":   ["efetivo", "profissional"],

    # Exceções / Ambíguos
    "c":    [],
    "a":    [],
    "o":    [],
}

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
            f"{trecho.manual_titulo} {trecho.capitulo} {trecho.secao} {trecho.texto}"
        )
        tokens = _normalizar(texto_rico).split()
        textos_tokenizados.append(tokens)

    motor_bm25 = BM25Okapi(textos_tokenizados)
    print("Motor de busca BM25 calibrado e pronto.")


def buscar_trechos_relevantes(pergunta: str, top_n: int = 8) -> list[TrechoMilitar]:
    """
    Retorna os top_n trechos mais relevantes para a pergunta usando BM25.
    Aplica normalização Unicode, remoção de pontuação e expansão de siglas na query.
    """
    if motor_bm25 is None:
        return []

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

    # 1. Recupera trechos relevantes
    documentos = buscar_trechos_relevantes(pergunta_usuario, top_n=8)

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
