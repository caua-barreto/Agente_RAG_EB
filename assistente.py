import os
import glob
import unicodedata
import yaml
from lxml import etree
from rank_bm25 import BM25Okapi
from openai import OpenAI
import dotenv

# =====================================================================
# 1. CARREGAMENTO DE VARIÁVEIS E CONFIGURAÇÕES
# =====================================================================

dotenv.load_dotenv() # Carregando a API do deepseek 


API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not API_KEY or API_KEY == "coloque_sua_chave_aqui_sem_aspas":
    raise RuntimeError(
        "DEEPSEEK_API_KEY não configurada. "
        "Abra o arquivo .env e coloque sua chave antes de rodar."
    )

# A API do deepseek é compativel com a OPENAI
cliente = OpenAI(api_key=API_KEY, base_url="https://api.deepseek.com")

# Carrega as configurações do arquivo YAML (configs)
_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_DIR, "configuracoes.yaml")
with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

# =====================================================================
# 2. ESTRUTURAS DE DADOS
# =====================================================================
banco_de_trechos: list = []

# Trecho dos documentos que serão referendados
# impoe uma classe de manual, capitulo, secao e texto

class TrechoMilitar:
    """Armazena um recorte estruturado de manual com seus metadados."""

    def __init__(self, manual_titulo: str, capitulo: str, secao: str, texto: str):
        self.manual_titulo = manual_titulo
        self.capitulo = capitulo
        self.secao = secao
        self.texto = texto

    #formata o trecho pra injeção no RAG da LLM

    def formatar_para_llm(self) -> str:
        """Retorna o trecho formatado para injeção no contexto do LLM."""
        return (
            f"[Manual: {self.manual_titulo} | Cap: {self.capitulo} | Sec: {self.secao}]\n"
            f"{self.texto}\n"
        )


# =====================================================================
# 3. LEITURA DOS ARQUIVOS XML
# =====================================================================

 # lê todos os XML configurada para o banco de texto

def carregar_xmls() -> None:
    """Lê todos os XMLs da pasta configurada e popula banco_de_trechos."""
    pasta_xml = config["caminhos"]["pasta_xml"]
    # Caminho absoluto relativo ao diretório do script — evita erro ao rodar
    # de diretórios diferentes (ex: uvicorn chamado de outro CWD)
    pasta_xml_abs = os.path.join(_DIR, pasta_xml)
    arquivos_xml = glob.glob(os.path.join(pasta_xml_abs, "*.xml"))

    if not arquivos_xml:
        print(f"AVISO: Nenhum arquivo XML encontrado em '{pasta_xml_abs}'.")
        return

    print(f"Lendo base de dados... {len(arquivos_xml)} manual(is) encontrado(s).")

    for caminho in arquivos_xml:
        try:
            arvore = etree.parse(caminho)
        except etree.XMLSyntaxError as e:
            print(f"  AVISO: XML malformado ignorado → {caminho} ({e})")
            continue

        raiz = arvore.getroot()

        no_titulo = raiz.find(".//metadados/titulo")
        titulo_manual = (
            no_titulo.text.strip() if no_titulo is not None and no_titulo.text else "Manual Desconhecido"
        )

        for elemento_texto in raiz.findall(".//texto"):
            texto = elemento_texto.text
            # Pula nós vazios ou marcadores de falha do Pipeline
            if not texto:
                continue
            texto = texto.strip()
            if not texto or texto.startswith("(Página não localizada)") or texto.startswith("(Conteúdo não extraído)"):
                continue

            pai = elemento_texto.getparent()
            secao_nome = pai.get("titulo", "Seção Indefinida") if pai is not None else "Seção Indefinida"

            avo = pai.getparent() if pai is not None else None
            if avo is not None and avo.tag == "capitulo":
                cap_nome = avo.get("titulo", "Capítulo Indefinido")
            else:
                cap_nome = "Sem Capítulo (Anexo/Prefácio)"

            banco_de_trechos.append(TrechoMilitar(titulo_manual, cap_nome, secao_nome, texto))

    print(f"Extração concluída! {len(banco_de_trechos)} recortes carregados.")


# =====================================================================
# 4. MOTOR DE BUSCA BM25 — com normalização Unicode e expansão de siglas
# =====================================================================

# Dicionário de expansão de siglas militares.
# Ao detectar uma sigla na query, seus termos equivalentes são adicionados,
# garantindo que blocos com a forma por extenso também sejam recuperados.

SIGLAS_MILITARES: dict[str, list[str]] = {
    # Testes e Treinamentos (TFM e Instrução)
    "tfm":  ["treinamento", "fisico", "militar"],
    "tiai": ["treinamento", "intervalado", "alta", "intensidade"], # Corrigido conforme manual 5ª Ed
    "taf":  ["teste", "avaliacao", "fisica"],
    "tap":  ["teste", "aptidao", "fisica"],
    "cmb":  ["combate", "manutencao", "boa", "forma"],
    "ppm":  ["pista", "progressao", "militar"],
    "ptc":  ["pista", "treinamento", "circuito"],
    "tib":  ["tiro", "instrucao", "basico"],
    "tia":  ["tiro", "instrucao", "avancado"],
    "tat":  ["tiro", "acao", "tatica"],
    "pccs": ["preparo", "condicionamento", "combate", "sobrevivencia"],
    
    # Organizações e Instituições (EB)
    "eb":   ["exercito", "brasileiro"],
    "om":   ["organizacao", "militar"],
    "qg":   ["quartel", "general"],
    "ime":  ["instituto", "militar", "engenharia"],
    "aman": ["academia", "militar", "agulhas", "negras"],
    "esa":  ["escola", "sargentos", "armas"],
    "espcex": ["escola", "preparatoria", "cadetes", "exercito"],
    "cigs": ["centro", "instrucao", "guerra", "selva"],
    "cpor": ["centro", "preparacao", "oficiais", "reserva"],
    "npor": ["nucleo", "preparacao", "oficiais", "reserva"],
    "esfcex": ["escola", "formacao", "complementar", "exercito"],
    
    # Cursos e Formações
    "cfsd": ["curso", "formacao", "soldados"],
    "cfc":  ["curso", "formacao", "cabos"],
    "cfs":  ["curso", "formacao", "sargentos"],
    "cfo":  ["curso", "formacao", "oficiais"],
    "eib":  ["estagio", "instrucao", "basica"],
    "epcv": ["estagio", "preparacao", "comandantes", "vtr"],
    
    # Frações e Escalões
    "bda":  ["brigada"],
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
    "bda":  ["boletim", "acesso", "restrito"], # Depende do contexto, mas Bda também é Brigada
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
    "c":    [],  # "C" isolado é ambíguo, não expandir
    "a":    [],
    "o":    []
}

motor_bm25 = None  # instância global do BM25

# NOrmalizar --> remover acentos e colocar minusculas, ajda a economizar tokens

def _normalizar(texto: str) -> str:
    """
    Remove acentos via decomposição NFKD e converte para minúsculas.
    Garante que 'Físico' == 'fisico' na comparação de tokens.
    """
    nfkd = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))

# Transforma siglas nas equilantes

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
        # Indexa título + cap + seção + texto para que buscas pelo nome do
        # manual (ex: "TFM") batam também nos blocos introdutórios.
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
    Aplica normalização Unicode e expansão de siglas na query.
    """
    if motor_bm25 is None:
        return []

    tokens_base = _normalizar(pergunta).split()
    tokens_expandidos = _expandir_siglas(tokens_base)

    resultados = motor_bm25.get_top_n(tokens_expandidos, banco_de_trechos, n=top_n)

    # Deduplicação por identidade do objeto
    vistos: set[int] = set()
    unicos: list[TrechoMilitar] = []
    for trecho in resultados:
        if id(trecho) not in vistos:
            vistos.add(id(trecho))
            unicos.append(trecho)

    return unicos


# =====================================================================
# 5. CHATBOT — INTEGRAÇÃO COM DEEPSEEK
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

    # 4. Chamada à API via Streaming
    resposta = cliente.chat.completions.create(
        model=config["modelo"]["nome"],
        temperature=config["modelo"]["temperatura"],
        max_tokens=config["modelo"]["max_tokens"],
        messages=[
            {"role": "system", "content": prompt_sistema},
            {"role": "user",   "content": pergunta_usuario},
        ],
        stream=True, # Habilita streaming (Opção A)
    )

    for chunk in resposta:
        texto = chunk.choices[0].delta.content or ""
        if texto:
            yield texto


def perguntar_ao_assistente(pergunta_usuario: str) -> str:
    """Wrapper síncrono para o terminal."""
    gerador = perguntar_ao_assistente_stream(pergunta_usuario)
    return "".join(list(gerador))



# =====================================================================
# 6. INTERFACE NO TERMINAL (uso direto via python assistente.py) para testes rápidos
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
