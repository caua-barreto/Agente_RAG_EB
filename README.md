#  PreparAI — Assistente IA Documental com RAG

> **Status do Projeto:** Projeto Base (MVP) — Submetido a avaliação inicial com excelente aceitação. 

> O sistema demonstrou alta viabilidade para atuar como assistente de consulta a Manuais doutrinários reais do Exército Brasileiro

> PreparAI é o nome da empresa fictícia, na qual estamos trabalhando no contexto da disciplina de: **Introdução a Projetos de Engenharia 2** no Instituto Militar de Engenharia

O **PreparAI** é uma solução de Inteligência Artificial focada em pesquisa documental rápida e precisa sobre a doutrina militar. Construído com foco em **eficiência computacional, segurança e economia de tokens**, o projeto implementa um motor RAG (*Retrieval-Augmented Generation*) altamente otimizado, dispensando frameworks pesados em favor de arquiteturas mais limpas e interpretáveis.

---

## Decisões de Arquitetura e Engenharia

Este projeto foi desenhado como um portfólio de boas práticas em IA Generativa, em que cada feature foi escolhida considerando trade-offs técnicos:

### 1. RAG Probabilístico 100% Local (BM25 vs Vector DBs)

Em vez de utilizar Bancos de Dados Vetoriais (que exigem chamadas de API pagas para gerar *Embeddings*), o sistema utiliza o algoritmo **BM25**. 

* **Por quê?** O BM25 é um modelo probabilístico baseado em TF-IDF. Ele busca palavras-chave de forma exata e cruza com a densidade do termo no documento. Para manuais militares — repletos de siglas exatas (ex: TFM, GLO, CIGS), o BM25 supera modelos semânticos que poderiam alucinar  com sinônimos, garantindo precisão sem custos financeiros ou de processamento.

### 2. Ingestão de Dados: Extração para XML (PyMuPDF + Regex)

A leitura direta de PDFs pelo LLM ou a conversão para JSON foram descartadas pelo alto consumo de tokens. O script `pipeline.py` usa `PyMuPDF` e Expressões Regulares `Regex` para transformar as strings dos PDFs Doutrinários exportá-los em formato **XML**.

Buscou-se no projeto transformar o PDF em um dicionário de strings, baseado no seu índice, de modo que os dados possam ser facilmente referenciados.

* **Por quê?** Formatos como JSON injetam muitos caracteres de formatação (chaves, aspas) que consomem "Tokens" da IA. O XML permite isolar hierarquias (`<manual>`, `<capitulo>`, `<texto>`) de forma extremamente legível para o LLM, barateando o custo da requisição e facilitando o particionamento (chunking) do texto.

### 3. LLM: API Padrão OpenAI + DeepSeek

Utilizamos a biblioteca oficial da `openai` no Python, sem dúvida uma das mais famosas, bem trabalhadas e com muitos conteúdos e materiais oficiais, mas apontando para o endpoint do modelo **DeepSeek-Chat**.

* **Por quê?** A interface da OpenAI é o padrão universal do mercado, facilitando integrações. A escolha do DeepSeek se deu pelo baixíssimo custo de tokens (ideal para grandes volumes de RAG) e pela natureza da sua arquitetura aberta (Open Source). Manter a compatibilidade com a API da OpenAI significa que, no futuro, caso seja exigido pelos gestores, será possível rodar o modelo nativamente em um servidor interno do EB (garantindo **Segurança de Dados e Sigilo Absoluto** militar).

### 4. Controle de Memória e a Rejeição ao LangChain

Optamos por **não utilizar o LangChain** ou LlamaIndex para a gestão do agente.

* **Por quê?** Frameworks como o LangChain adicionam camadas de **abstração complexas** e "prompts ocultos" nas requisições, gerando consumo excessivo de tokens e latência. Nossa gestão de memória é feita de forma nativa através de **manipulação de listas** em Python. Mantemos um array FIFO estrito (limitado via `configuracoes.yaml`), passando à IA apenas o contexto estritamente necessário. O resultado é um código limpo, ultra-rápido e previsível.

### 5. Guardrails e System Prompt Rígido
O sistema é governado por um arquivo central (`configuracoes.yaml`) que dita o Prompt de Sistema.

* **Por quê?** Modelos de IA tendem a alucinar ou tentar ser amigáveis demais. O prompt foi desenhado de forma estrita para limitar o escopo de atuação do agente: ele está **proibido** de responder questões fora da doutrina militar ou sem embasamento nos textos recuperados.

### 6. Stack Minimalista e Streaming (FastAPI)

O Backend é orquestrado por **FastAPI**, servindo rotas de forma assíncrona. O Frontend foi feito em HTML/JS/CSS puros.

* **Por quê?** Evitamos bibliotecas como React para manter o peso do lado do cliente próximo a zero. Implementamos **Data Streaming** real usando geradores Python (`yield`), fazendo a resposta aparecer progressivamente na tela (efeito máquina de escrever) assim que o primeiro token é gerado, eliminando a percepção de tempo de espera do usuário.

---

##  Roadmap e Próximos Passos

O projeto está em sua **fase base V1.5** e serve como prova de conceito e avaliação inicial pelos professores. As próximas fases de desenvolvimento incluem:

* [ ] **Migração de Plataforma:** Integração como aplicação Web escalável e/ou Aplicativo Mobile.
* [ ] **Bancos de Dados Relacionais:** 
  * Implementação de **MySQL** (caso siga para arquitetura Web Cloud) para gestão multi-tenant de usuários e logs de conversas.
  * Uso de **SQLite** nativo (caso seja portado para App/Local) mantendo a premissa de processamento offline.
* [ ] **Segurança e ACL:** Sistema de login militar e controle de acesso baseado em patentes/cargos para liberação de documentos específicos (Níveis de Sigilo).
* [ ] **Refinamentos Visuais:** Expansão da identidade visual da interface (UX/UI).

---

## Como rodar o projeto localmente

1. Clone o repositório e instale as dependências:
   ```bash
   pip install -r requirements.txt
   ```
2. Configure sua chave no arquivo `.env`:
   ```env
   DEEPSEEK_API_KEY=sua_chave_aqui
   ```
3. Converta seus manuais em PDF para XML usando a pipeline:
   ```bash
   python scripts/pipeline.py
   ```
4. Inicie o servidor:
   ```bash
   python main.py
   ```
5. Acesse `http://localhost:8000` em seu navegador.

**Desenvolvido por:** [Cauã Barreto](https://www.linkedin.com/in/caua-fbarreto-) e [João Pedroti](https://www.linkedin.com/in/jo%C3%A3o-pedroti-0a3a62360/)

