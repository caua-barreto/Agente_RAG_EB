# 🪖 PreparAI — Assistente Inteligente de Pesquisa Doutrinária Militar (RAG)

> **Status do Projeto:** `V2.0 — MVP Consolidado`  
> **Contexto Acadêmico:** Projeto desenvolvido na disciplina de **Introdução a Projetos de Engenharia 2 (IPE 2)** do **Instituto Militar de Engenharia (IME)**.

O **PreparAI** é uma plataforma de Inteligência Artificial voltada à pesquisa documental rápida, auditável e de alta precisão sobre o acervo doutrinário oficial do **Exército Brasileiro (EB)**. Concebido sob rigorosas premissas de **eficiência computacional, baixíssima ingestão de tokens, determinismo nas respostas e rastreabilidade de fontes**, o sistema implementa uma arquitetura **RAG (*Retrieval-Augmented Generation*)** enxuta e 100% transparente, dispensando frameworks pesados em favor de controle fino sobre cada etapa da recuperação e geração.

---

## 🎯 O Problema ("A Dor" do Cliente) e a Nossa Solução

### ❌ A Dor Real na Consulta Doutrinária
O acervo público da **Biblioteca Digital do Exército (BDEx)** reúne centenas de Manuais de Campanha (`EB70-MC`, `EB20-MC`, `C-`, `MC`), totalizando dezenas de milhares de páginas técnicas. No dia a dia operacional, acadêmico e de planejamento militar, o usuário enfrenta quatro grandes gargalos:

1. **Alto Tempo de Busca Manual:** Localizar uma regra específica, composição de fração ou parâmetro técnico exige abrir múltiplos PDFs extensos e navegar manualmente por dezenas de sumários.
2. **Risco de Consulta a Manuais Revogados:** A doutrina evolui continuamente; manuais antigos são revogados por novas edições, e consultar uma publicação desatualizada induz a erros graves de procedimento.
3. **Alucinação em IAs Genéricas:** LLMs comerciais tradicionais desconhecem siglas castrenses específicas (`TFM`, `TAF`, `TIAI`, `PPCOT`, `GLO`, `REOP`, `DQBRN`) e frequentemente inventam artigos ou misturam conceitos.
4. **Custo Proibitivo de Tokens na Leitura Bruta de PDFs:** Injetar PDFs inteiros no contexto de uma IA consome dezenas de milhares de tokens por pergunta, tornando o sistema lento, caro e propenso ao fenômeno de *"lost in the middle"*.

### ✅ Como o PreparAI Resolve
O **PreparAI** atua como um motor de inteligência documental em três estágios:

* **Coleta e Higienização Automatizada (`webscraping.py`):** Baixa os Manuais de Campanha diretamente da BDEx, barra duplicatas via hash MD5 e **isola automaticamente manuais revogados**, garantindo que apenas a doutrina ativa alimente a IA.
* **Estruturação Hierárquica em XML com Cache Incremental (`pipeline.py`):** Converte cada PDF nativo em uma árvore XML limpa dividida exatamente por `<capitulo>` e `<secao>`, extraindo metadados de capa (Nome do Manual, Código Oficial, Edição e Ano).
* **Recuperação Cirúrgica BM25 + Expansão de Siglas (`assistente.py`):** Quando o militar faz uma pergunta, o motor expande automaticamente as siglas militares (via `siglas.yaml`), localiza em milissegundos os **10 recortes mais relevantes** entre quase **5.000 seções indexadas** e envia apenas os fragmentos exatos ao LLM, exigindo citação padronizada:
  > `[Manual: Treinamento Físico Militar | Edição: 5ª Ed. (2021) | Cap: II | Sec: 2.8 Situações Especiais do TFM]`

---

## 🏗️ Premissas de Engenharia e Decisões de Arquitetura

Cada componente do **PreparAI** foi projetado avaliando *trade-offs* reais de engenharia de software e IA:

### 1. ⚡ Baixíssima Ingestão de Tokens (XML vs. PDF Bruto / JSON)
Enviar arquivos PDF ou objetos JSON verbosos diretamente ao modelo desperdiça tokens com metadados repetitivos, chaves e quebras de página sujas.
* **Decisão:** O pipeline pré-processa os PDFs uma única vez (com **cache incremental** que evita retrabalho em execuções futuras) e gera arquivos XML enxutos. Na hora da consulta, apenas os $k$ trechos vencedores já limpos são injetados no prompt, reduzindo o consumo de tokens em **mais de 90%** por requisição.

### 2. 🔍 RAG Probabilístico 100% Local (BM25 + Dicionário de Siglas Externo)
Bancos de dados vetoriais exigem chamadas de API externas para gerar *embeddings* e costumam diluir siglas exatas em aproximações semânticas vagas.
* **Decisão:** Utilizamos o algoritmo probabilístico **BM25Okapi** combinado com normalização Unicode (`NFKD`) e um motor de expansão de siglas configurável externamente ([`siglas.yaml`](siglas.yaml), com quase 100 siglas militares catalogadas). Isso garante precisão cirúrgica para termos técnicos e códigos de manuais a **custo zero de indexação** e latência de busca inferior a **15 ms**.

### 3. 🧠 Controle Nativo de Contexto (Por que rejeitamos o LangChain)
Frameworks como *LangChain* e *LlamaIndex* adicionam camadas pesadas de abstração, prompts ocultos e sobrecarga de memória.
* **Decisão:** Desenvolvemos a orquestração do RAG e a **Memória Conversacional FIFO** diretamente em Python puro. O histórico retém exatamente o número ideal de turnos recentes configurado em [`configuracoes.yaml`](configuracoes.yaml) (`max_mensagens: 4`), preservando a fluidez do diálogo sem contaminar novas perguntas com assuntos anteriores.

### 4. 🛡️ Rigor Doutrinário e Guardrails Anti-Alucinação
* **Decisão:** O modelo opera com **temperatura reduzida (`0.10`)** e um *System Prompt* blindado em [`configuracoes.yaml`](configuracoes.yaml). O agente é proibido de extrapolar números, distâncias, tempos ou regras fora do contexto recuperado e deve apresentar múltiplas perspectivas caso mais de um manual aborde o mesmo tema.

### 5. 🔒 Soberania Tecnológica (OpenAI SDK + DeepSeek)
* **Decisão:** Utilizamos o cliente padrão da indústria (`openai`) apontando para o modelo `deepseek-chat`. Além da excelente relação custo-benefício em tarefas de raciocínio sobre textos longos, essa arquitetura padronizada permite que, no futuro, o Exército Brasileiro substitua apenas a `base_url` para rodar um LLM *open-weight* (como Llama ou Qwen) **100% on-premise / offline** dentro de sua própria rede segura.

---

## 🛠️ Stack Tecnológica

| Camada | Tecnologias Utilizadas | Finalidade no Projeto |
| :--- | :--- | :--- |
| ** Inteligência & RAG** | `Python 3.12+`, `rank-bm25`, `OpenAI SDK` (`DeepSeek-Chat`) | Motor de busca lexical BM25, expansão de siglas, memória FIFO e geração via streaming. |
| **📄 Ingestão & ETL** | `PyMuPDF (fitz)` / `pypdfium2`, `lxml`, `Regex` | Extração vetorial de texto, mapeamento de sumários/páginas e serialização XML com cache incremental. |
| **🌐 Web Scraping** | `Requests`, `BeautifulSoup4`, `Hashlib (MD5)` | Coleta paginada na BDEx, verificação de integridade e quarentena automática de manuais revogados. |
| **⚙️ Backend & API** | `FastAPI`, `Uvicorn`, `Pydantic`, `PyYAML`, `python-dotenv` | Servidor assíncrono de alta performance, endpoints REST e transmissão contínua (*Server-Sent Streaming*). |
| **💻 Frontend (UX/UI)** | `HTML5`, `CSS3`, `JavaScript (ES6+)`, `marked.js` | Interface institucional militar responsiva, renderização Markdown em tempo real e persistência local (`localStorage`). |

---

## 📂 Estrutura do Repositório

```text
EB-PROJETO-/
├── Arquivos/                  # Acervo de PDFs vigentes baixados da BDEx
│   └── Revogados/             # Quarentena automática de manuais revogados (ignorados pelo RAG)
├── Gerados/                   # Base de conhecimento convertida em XML estruturado (121 manuais ativos)
├── scripts/
│   └── pipeline.py            # Pipeline ETL: PDF -> XML com cache incremental e extração de metadados
├── webscraping/
│   ├── webscraping.py         # Coletor automatizado da BDEx com filtro de revogados e MD5
│   └── Coleta/                # Catálogo JSON e relatórios de coleta da BDEx
├── frontend/
│   └── static/                # Interface Web (index.html, app_v2.css, app_v2.js e assets visuais)
├── assistente.py              # Núcleo do RAG: parser XML, motor BM25, memória FIFO e integração LLM
├── main.py                    # Servidor FastAPI (rotas /api/chat, /api/chat/stream, /api/catalogo)
├── configuracoes.yaml         # Hiperparâmetros da IA, top_n, memória FIFO e System Prompt
├── siglas.yaml                # Dicionário externo configurável de siglas e abreviaturas militares
└── requirements.txt           # Dependências enxutas do projeto
```

---

## 🚀 Como Executar o Projeto Localmente

### 1. Pré-requisitos e Instalação
Clone o repositório, crie seu ambiente virtual e instale as dependências:

```bash
git clone https://github.com/caua-barreto/Agente_RAG_EB.git
cd Agente_RAG_EB
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configuração da Chave de API
Crie um arquivo `.env` na raiz do projeto contendo sua chave da API DeepSeek:

```env
DEEPSEEK_API_KEY=sua_chave_aqui_sem_aspas
```

### 3. Coleta de Manuais da BDEx (Opcional)
Para baixar novos manuais diretamente do repositório oficial da BDEx ou auditar revogados na pasta `Arquivos/`:

```bash
python webscraping/webscraping.py                # Coleta completa + higienização
python webscraping/webscraping.py --manutencao   # Apenas audita e isola revogados locais
```

### 4. Processamento de PDF para XML (Pipeline com Cache Incremental)
Converta os PDFs vigentes da pasta `Arquivos/` para XMLs estruturados em `Gerados/`:

```bash
python scripts/pipeline.py            # Processa apenas PDFs novos (usa cache incremental)
python scripts/pipeline.py --forcar   # Reprocessa todos os PDFs do zero
```

### 5. Inicialização da Aplicação Web
Suba o servidor FastAPI e acesse a interface no navegador:

```bash
python main.py
```
👉 Acesse em seu navegador: **`http://localhost:8000`**

---

## ⚠️ Limitações Atuais do Projeto (MVP V.2.0)

Por decisão estratégica de engenharia voltada a entregar um MVP local extremamente rápido, leve e funcional, a versão atual possui limitações conhecidas e mapeadas:

1. **Ausência de OCR em Imagens e PDFs Escaneados:** O pipeline realiza extração 100% vetorial/nativa sobre a camada de texto do PDF. Manuais históricos muito antigos (décadas de 1960–1990) que foram digitalizados apenas como imagem estática sem camada de texto não são extraídos nesta versão.
2. **Leitura de Tabelas Complexas, Figuras e Organogramas:** Quadros visuais complexos, mapas táticos, calcos de operações e fluxogramas desenhados como imagem dentro dos manuais não passam por visão computacional (*Vision OCR*), podendo perder a relação espacial entre colunas durante a extração textual pura.
3. **Dependência de Sumário Padronizado:** O particionamento hierárquico em `<capitulo>` e `<secao>` baseia-se na detecção do sumário/índice normativo do manual (10 dos 143 PDFs brutos não possuíam índice padronizado e foram automaticamente descartados para preservar a qualidade dos recortes).
4. **Escopo Local (*Single-Session* no Backend):** A memória conversacional FIFO reside em memória RAM no processo local, ideal para uso individual no MVP, mas ainda sem isolamento de sessões simultâneas em nuvem.

---

## 🔭 Visão de Futuro e Escalabilidade em Larga Escala

A arquitetura modular do **PreparAI** foi preparada para evoluir de um MVP local para uma plataforma corporativa em larga escala:

* 🗄️ **Bancos de Dados Relacionais e Governança (PostgreSQL / MySQL):** Migração do catálogo de metadados, histórico de conversas, logs de auditoria e gestão de sessões multiusuário para bancos relacionais robustos, mantendo os recortes indexados com versionamento de edições.
* 🧬 **Busca Híbrida (*Hybrid Search*: BM25 + Embeddings + Reranker):** Combinação do nosso motor lexical BM25 (imbatível para siglas e códigos exatos) com um banco vetorial (`pgvector` ou `Qdrant`) e um modelo *Cross-Encoder Reranker*, unindo exatidão terminológica e compreensão semântica profunda.
* 📊 **Ingestão Multimodal de Tabelas e Figuras:** Integração de parsers de layout baseados em Visão Computacional (como *Docling* ou *VLMs*) exclusivamente na etapa offline do `pipeline.py`, convertendo tabelas complexas e organogramas militares em tabelas Markdown estruturadas dentro do XML sem impactar a velocidade de busca do usuário.
* 🔐 **Controle de Acesso Baseado em Função/Patente (RBAC) e Deploy On-Premise:** Autenticação integrada para gerenciamento de níveis de sigilo documental (Ostensivo vs. Restrito) e implantação em servidores locais do próprio Exército Brasileiro utilizando modelos locais via *vLLM* ou *Ollama*.

---

## 👥 Equipe do Projeto (PreparAI — IME)

Projeto idealizado e desenvolvido na disciplina de **Introdução a Projetos de Engenharia 2** no **Instituto Militar de Engenharia (IME)**:

| Integrante | Função / Cargo | Contato Profissional |
| :--- | :--- | :--- |
| **Cauã Barreto** | 🧠 **AI Engineering** | [🔗 LinkedIn](https://www.linkedin.com/in/caua-fbarreto-/) |
| **Ana Luíza T.** | 🎖️ **CEO** (*Chief Executive Officer*) | [🔗 LinkedIn](https://www.linkedin.com/in/ana-luiza-tavares-castro-9639a4386/) |
| **João P. Sabiá** | 🚀 **CTO** (*Chief Technology Officer*) | [🔗 LinkedIn](https://www.linkedin.com/in/jo%C3%A3o-pedroti-0a3a62360/) |
| **Sidney Lima** | 💻 **Software Developer** | [🔗 LinkedIn](https://www.linkedin.com/in/sidneylimajr/) |
| **Danielly Satyro** | 📢 **Marketing** | [🔗 LinkedIn](https://www.linkedin.com/in/danielly-satyro-a0849239b/) |
