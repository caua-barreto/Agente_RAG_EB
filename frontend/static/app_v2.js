// PreparAI — Lógica do Chat com Markdown, Histórico FIFO e localStorage
document.addEventListener("DOMContentLoaded", () => {

    // ── Referências DOM ─────────────────────────────────────────────────────
    const messagesContainer = document.getElementById("mensagens");
    const input             = document.getElementById("entrada");
    const sendButton        = document.getElementById("btn-enviar");
    const btnNovaConversa   = document.getElementById("btn-nova-conversa");
    const btnCatalogo       = document.getElementById("btn-catalogo");
    const btnCatalogoTopo   = document.getElementById("btn-catalogo-topo");
    const badgeTotalManuais = document.getElementById("badge-total-manuais");
    const textoCatalogoTopo = document.getElementById("texto-catalogo-topo");
    const camadaModal       = document.getElementById("camada-modal");
    const listaHistorico    = document.getElementById("lista-historico");
    const tituloConversa    = document.getElementById("titulo-conversa");

    if (!messagesContainer || !input || !sendButton) {
        console.error("PreparAI: elementos do chat não encontrados no DOM.");
        return;
    }

    // ── Configuração do marked.js ────────────────────────────────────────────
    // Garante que marked esteja disponível (carregado via CDN no HTML)
    if (typeof marked === "undefined") {
        console.warn("PreparAI: marked.js não carregado. Usando fallback simples.");
    }

    marked.setOptions({
        breaks: true,        // \n vira <br> dentro de parágrafos
        gfm:    true,        // GitHub Flavored Markdown (tabelas, strikethrough etc.)
    });

    // ── Converte Markdown em HTML seguro usando marked.js ───────────────────
    function renderMarkdown(text) {
        if (typeof marked === "undefined") {
            // Fallback básico se o CDN falhar
            return text
                .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
                .replace(/^#{1,3}\s+(.+)$/gm, "<h4>$1</h4>")
                .replace(/^[-•]\s+(.+)$/gm, "<li>$1</li>")
                .replace(/(<li>[\s\S]+?<\/li>)/g, "<ul>$1</ul>")
                .replace(/\n/g, "<br>");
        }
        return marked.parse(text);
    }

    // ══════════════════════════════════════════════════════════════════════════
    //  GERENCIAMENTO DE HISTÓRICO DE SESSÕES (localStorage, FIFO, máx 5)
    // ══════════════════════════════════════════════════════════════════════════

    const MAX_SESSOES = 5;
    const LS_KEY      = "PreparAI_sessoes";

    // Sessão em andamento: array de {role, text} para reconstrução visual
    let sessaoAtual = [];

    /**
     * Carrega o array de sessões salvas do localStorage.
     * Cada sessão: { id, titulo, timestamp, mensagens: [{role, text}] }
     */
    function carregarSessoes() {
        try {
            return JSON.parse(localStorage.getItem(LS_KEY)) || [];
        } catch {
            return [];
        }
    }

    /** Salva o array de sessões no localStorage. */
    function salvarSessoes(sessoes) {
        localStorage.setItem(LS_KEY, JSON.stringify(sessoes));
    }

    /**
     * Persiste a sessão atual e limita a MAX_SESSOES (FIFO).
     * Só salva se houver pelo menos uma mensagem do usuário.
     */
    function persistirSessaoAtual() {
        const mensagensUsuario = sessaoAtual.filter(m => m.role === "user");
        if (mensagensUsuario.length === 0) return; // conversa vazia, não salva

        const sessoes = carregarSessoes();
        const novaSessao = {
            id:        Date.now(),
            titulo:    mensagensUsuario[0].text.slice(0, 48) + (mensagensUsuario[0].text.length > 48 ? "…" : ""),
            timestamp: new Date().toLocaleString("pt-BR", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" }),
            mensagens: [...sessaoAtual],
        };

        sessoes.push(novaSessao);

        // FIFO: remove a mais antiga se ultrapassar o limite
        while (sessoes.length > MAX_SESSOES) {
            sessoes.shift();
        }

        salvarSessoes(sessoes);
        renderizarListaHistorico();
    }

    /** Reconstrói visualmente uma sessão salva na área de mensagens. */
    function restaurarSessao(sessao) {
        messagesContainer.innerHTML = "";
        sessaoAtual = [];

        for (const msg of sessao.mensagens) {
            if (msg.role === "user") {
                messagesContainer.appendChild(criarBolha("user", msg.text));
            } else {
                messagesContainer.appendChild(criarBolha("ai", renderMarkdown(msg.text)));
            }
            sessaoAtual.push(msg);
        }

        // Atualiza título e rola para o fundo
        if (tituloConversa) {
            tituloConversa.textContent = sessao.titulo;
            tituloConversa.className = "";
        }
        messagesContainer.scrollTop = messagesContainer.scrollHeight;

        // Sincroniza o histórico do servidor com a sessão restaurada
        // (simplificado: limpa o servidor — a sessão restaurada é só visual)
        fetch("/api/nova-conversa", { method: "POST" }).catch(() => {});
    }

    /** Renderiza a lista de sessões na sidebar. */
    function renderizarListaHistorico() {
        if (!listaHistorico) return;
        const sessoes = carregarSessoes();
        listaHistorico.innerHTML = "";

        if (sessoes.length === 0) {
            listaHistorico.innerHTML = '<li class="sb-history-empty">Nenhuma conversa salva</li>';
            return;
        }

        // Exibe da mais recente para a mais antiga
        [...sessoes].reverse().forEach(sessao => {
            const li = document.createElement("li");
            li.className = "sb-history-item";
            li.innerHTML = `
                <button class="sb-history-btn" title="${sessao.titulo}">
                    <span class="sb-history-title">${sessao.titulo}</span>
                    <span class="sb-history-time">${sessao.timestamp}</span>
                </button>`;
            li.querySelector(".sb-history-btn").addEventListener("click", () => {
                restaurarSessao(sessao);
            });
            listaHistorico.appendChild(li);
        });
    }

    // Renderiza o histórico ao carregar a página
    renderizarListaHistorico();

    // ══════════════════════════════════════════════════════════════════════════
    //  BOAS-VINDAS E NOVA CONVERSA
    // ══════════════════════════════════════════════════════════════════════════

    function exibirBoasVindas() {
        messagesContainer.innerHTML = `
            <div class="msg ai">
                <div class="msg-content ai-content">
                    <span class="ai-label">PreparAI</span>
                    Olá, Senhor/Senhora. Sou o <strong>PreparAI</strong>, seu assistente de
                    pesquisa documental treinado nos manuais oficiais do Exército Brasileiro.
                    Como posso auxiliá-lo?
                </div>
            </div>`;
    }

    exibirBoasVindas();

    function novaConversa() {
        // 1. Persiste a sessão atual antes de limpar
        persistirSessaoAtual();

        // 2. Zera o histórico no servidor
        fetch("/api/nova-conversa", { method: "POST" }).catch(() => {
            console.warn("PreparAI: falha ao limpar histórico no servidor.");
        });

        // 3. Reseta o estado local
        sessaoAtual = [];

        // 4. Limpa a UI
        exibirBoasVindas();
        input.value = "";
        input.style.height = "auto";
        input.focus();

        if (tituloConversa) {
            tituloConversa.textContent = "Nova conversa";
            tituloConversa.className = "muted";
        }
    }

    if (btnNovaConversa) {
        btnNovaConversa.addEventListener("click", novaConversa);
    }

    // ══════════════════════════════════════════════════════════════════════════
    //  RENDERIZAÇÃO DE MENSAGENS
    // ══════════════════════════════════════════════════════════════════════════

    function criarBolha(tipo, htmlContent) {
        const wrapper = document.createElement("div");
        wrapper.className = `msg ${tipo}`;

        if (tipo === "user") {
            wrapper.innerHTML = `<div class="msg-content user-content">${htmlContent}</div>`;
        } else {
            // .md-body aplica os estilos de markdown (listas, headings, etc.)
            wrapper.innerHTML = `
                <div class="msg-content ai-content">
                    <span class="ai-label">PreparAI</span>
                    <div class="md-body">${htmlContent}</div>
                </div>`;
        }
        return wrapper;
    }

    function criarLoadingBubble() {
        const wrapper = document.createElement("div");
        wrapper.className = "msg ai";
        wrapper.id = "loading-bubble";
        wrapper.innerHTML = `
            <div class="msg-content ai-content loading-bubble">
                <span class="ai-label">PreparAI</span>
                <span class="typing-indicator" aria-label="Processando">
                    <span></span><span></span><span></span>
                </span>
                <span class="loading-text">processando consulta...</span>
            </div>`;
        return wrapper;
    }

    // ══════════════════════════════════════════════════════════════════════════
    //  ENVIO DE MENSAGEM
    // ══════════════════════════════════════════════════════════════════════════

    async function sendMessage(texto) {
        const trimmed = texto.trim();
        if (!trimmed) return;

        // Adiciona à sessão em andamento e renderiza na tela
        sessaoAtual.push({ role: "user", text: trimmed });
        messagesContainer.appendChild(criarBolha("user", trimmed));
        input.value = "";
        input.style.height = "auto";
        messagesContainer.scrollTop = messagesContainer.scrollHeight;

        // Atualiza título da conversa com a primeira pergunta
        if (sessaoAtual.filter(m => m.role === "user").length === 1 && tituloConversa) {
            const tituloTexto = trimmed.slice(0, 40) + (trimmed.length > 40 ? "…" : "");
            tituloConversa.textContent = tituloTexto;
            tituloConversa.className = "";
        }

        // Bloqueia o envio e exibe loading
        sendButton.disabled = true;
        sendButton.setAttribute("aria-busy", "true");
        const loadingBubble = criarLoadingBubble();
        messagesContainer.appendChild(loadingBubble);
        messagesContainer.scrollTop = messagesContainer.scrollHeight;

        try {
            // Usa a nova rota de Streaming (Opção A: Ideal)
            const response = await fetch("/api/chat/stream", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify({ mensagem: trimmed }),
            });

            if (!response.ok) throw new Error(`HTTP ${response.status}`);

            // Remove o loading
            loadingBubble.remove();
            
            // Cria a bolha vazia para receber o texto progressivo
            const aiBubble = criarBolha("ai", "");
            messagesContainer.appendChild(aiBubble);
            const mdBody = aiBubble.querySelector(".md-body");

            // Processamento do Stream
            const reader = response.body.getReader();
            const decoder = new TextDecoder("utf-8");
            let aiTextAccumulator = "";

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                
                // Converte Uint8Array em string e acumula
                const chunk = decoder.decode(value, { stream: true });
                aiTextAccumulator += chunk;
                
                // Renderiza o markdown parcial
                mdBody.innerHTML = renderMarkdown(aiTextAccumulator);
                messagesContainer.scrollTop = messagesContainer.scrollHeight;
            }

            // Garante o flush final do decoder
            aiTextAccumulator += decoder.decode();
            mdBody.innerHTML = renderMarkdown(aiTextAccumulator);

            // Salva na sessão o texto completo retornado
            sessaoAtual.push({ role: "assistant", text: aiTextAccumulator });

        } catch (error) {
            loadingBubble.remove();
            messagesContainer.appendChild(criarBolha(
                "ai",
                '<span class="ai-error">⚠ Erro de conexão com o servidor. Verifique se o FastAPI está em execução.</span>'
            ));
            console.error("PreparAI error:", error);
        } finally {
            sendButton.disabled = false;
            sendButton.removeAttribute("aria-busy");
            messagesContainer.scrollTop = messagesContainer.scrollHeight;
        }
    }


    // ── Eventos ──────────────────────────────────────────────────────────────
    sendButton.addEventListener("click", () => sendMessage(input.value));

    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage(input.value);
        }
    });

    // Auto-resize do textarea (máx 160px)
    input.addEventListener("input", () => {
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 160) + "px";
    });

    // ══════════════════════════════════════════════════════════════════════════
    //  CATÁLOGO VISUAL DE DOCUMENTOS INDEXADOS (SOMENTE MANUAIS ATIVOS)
    // ══════════════════════════════════════════════════════════════════════════

    let dadosCatalogo = { total_manuais: 0, total_trechos: 0, total_siglas: 0, manuais: [] };

    async function carregarCatalogo() {
        try {
            const resp = await fetch("/api/catalogo");
            if (!resp.ok) return;
            dadosCatalogo = await resp.json();
            if (badgeTotalManuais) {
                badgeTotalManuais.textContent = dadosCatalogo.total_manuais;
            }
            if (textoCatalogoTopo) {
                textoCatalogoTopo.textContent =
                    `${dadosCatalogo.total_manuais} manuais ativos · ${dadosCatalogo.total_trechos.toLocaleString("pt-BR")} seções`;
            }
        } catch (err) {
            console.warn("PreparAI: falha ao carregar catálogo de manuais.", err);
        }
    }

    function escaparHtml(str) {
        const div = document.createElement("div");
        div.textContent = str || "";
        return div.innerHTML;
    }

    function abrirModalCatalogo() {
        if (!camadaModal) return;

        const totalManuais = dadosCatalogo.total_manuais || 0;
        const totalTrechos = (dadosCatalogo.total_trechos || 0).toLocaleString("pt-BR");
        const totalSiglas  = dadosCatalogo.total_siglas || 0;

        camadaModal.innerHTML = `
            <div class="modal-backdrop" id="backdrop-catalogo">
                <div class="modal catalog-modal" role="dialog" aria-modal="true" aria-labelledby="titulo-modal-catalogo">
                    <div class="modal-head catalog-modal-head">
                        <div>
                            <h2 id="titulo-modal-catalogo">Catálogo de Documentos Indexados</h2>
                            <span class="catalog-subtitle">Acervo oficial ativo do Exército Brasileiro (manuais revogados excluídos)</span>
                        </div>
                        <button class="icon-btn" id="btn-fechar-catalogo" aria-label="Fechar catálogo" title="Fechar (Esc)">✕</button>
                    </div>

                    <div class="catalog-stats-bar">
                        <div class="catalog-stat-pill">
                            <strong>${totalManuais}</strong>
                            <span>Manuais Vigentes</span>
                        </div>
                        <div class="catalog-stat-pill">
                            <strong>${totalTrechos}</strong>
                            <span>Seções Indexadas</span>
                        </div>
                        <div class="catalog-stat-pill">
                            <strong>${totalSiglas}</strong>
                            <span>Siglas Mapeadas</span>
                        </div>
                    </div>

                    <div class="catalog-search-bar">
                        <input
                            type="text"
                            id="input-busca-catalogo"
                            placeholder="Filtrar manual por nome, código (ex: EB70-MC-10.375) ou ano..."
                            autocomplete="off"
                        />
                        <span class="catalog-filter-count" id="contador-filtro-catalogo">${totalManuais} exibidos</span>
                    </div>

                    <div class="catalog-list" id="lista-manuais-catalogo"></div>

                    <div class="modal-foot catalog-modal-foot">
                        <span class="catalog-foot-hint">Clique em "Consultar" em qualquer manual para direcionar sua pergunta.</span>
                        <button class="btn primary" id="btn-fechar-catalogo-rodape">Fechar</button>
                    </div>
                </div>
            </div>
        `;

        const listaEl    = document.getElementById("lista-manuais-catalogo");
        const inputBusca = document.getElementById("input-busca-catalogo");
        const contadorEl = document.getElementById("contador-filtro-catalogo");

        function renderizarItens(filtro = "") {
            const termo = filtro.trim().toLowerCase();
            const manuaisFiltrados = (dadosCatalogo.manuais || []).filter(m => {
                if (!termo) return true;
                return (
                    (m.nome_manual || "").toLowerCase().includes(termo) ||
                    (m.codigo || "").toLowerCase().includes(termo) ||
                    (m.edicao_completa || "").toLowerCase().includes(termo)
                );
            });

            if (contadorEl) {
                contadorEl.textContent = `${manuaisFiltrados.length} de ${totalManuais}`;
            }

            if (manuaisFiltrados.length === 0) {
                listaEl.innerHTML = `
                    <div class="catalog-empty">
                        Nenhum manual ativo encontrado para "<strong>${escaparHtml(filtro)}</strong>".
                    </div>`;
                return;
            }

            listaEl.innerHTML = manuaisFiltrados.map(m => `
                <div class="doc-row catalog-doc-row">
                    <div class="doc-row-body">
                        <div class="doc-row-line1">
                            <span class="doc-row-code">${escaparHtml(m.codigo)}</span>
                            <span class="doc-row-title"><strong>${escaparHtml(m.nome_manual)}</strong></span>
                            <span class="status-badge indexado">ATIVO</span>
                        </div>
                        <div class="doc-row-line2">
                            <span class="doc-row-meta">Edição: ${escaparHtml(m.edicao_completa)}</span>
                            <span class="doc-row-meta">·</span>
                            <span class="doc-row-meta">${m.capitulos} cap. / ${m.secoes} seções</span>
                        </div>
                    </div>
                    <div class="doc-row-actions">
                        <button
                            class="btn-quiet btn-consultar-manual"
                            data-nome="${escaparHtml(m.nome_manual)}"
                            data-codigo="${escaparHtml(m.codigo)}"
                            title="Preencher consulta sobre este manual"
                        >Consultar →</button>
                    </div>
                </div>
            `).join("");

            listaEl.querySelectorAll(".btn-consultar-manual").forEach(btn => {
                btn.addEventListener("click", () => {
                    const nome = btn.getAttribute("data-nome");
                    const cod  = btn.getAttribute("data-codigo");
                    fecharModalCatalogo();
                    input.value = `Segundo o manual ${nome} (${cod}), `;
                    input.focus();
                });
            });
        }

        renderizarItens("");

        if (inputBusca) {
            inputBusca.addEventListener("input", (e) => renderizarItens(e.target.value));
            setTimeout(() => inputBusca.focus(), 40);
        }

        function fecharModalCatalogo() {
            camadaModal.innerHTML = "";
            document.removeEventListener("keydown", onEscModal);
        }

        function onEscModal(e) {
            if (e.key === "Escape") fecharModalCatalogo();
        }

        document.addEventListener("keydown", onEscModal);
        document.getElementById("btn-fechar-catalogo")?.addEventListener("click", fecharModalCatalogo);
        document.getElementById("btn-fechar-catalogo-rodape")?.addEventListener("click", fecharModalCatalogo);
        document.getElementById("backdrop-catalogo")?.addEventListener("click", (e) => {
            if (e.target.id === "backdrop-catalogo") fecharModalCatalogo();
        });
    }

    if (btnCatalogo) {
        btnCatalogo.addEventListener("click", abrirModalCatalogo);
    }
    if (btnCatalogoTopo) {
        btnCatalogoTopo.addEventListener("click", abrirModalCatalogo);
    }

    carregarCatalogo();
});
