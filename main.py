from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import assistente

# Raiz do projeto — garante que os caminhos funcionem independente do CWD
_DIR = Path(__file__).resolve().parent
_STATIC = _DIR / "frontend" / "static"
_INDEX  = _STATIC / "index.html"


# ── Startup com lifespan (substitui @app.on_event deprecated) ─────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Carrega XMLs e inicializa o BM25 uma única vez ao subir o servidor."""
    print("PreparAI — inicializando...")
    assistente.carregar_xmls()
    assistente.preparar_motor_de_busca()
    yield
    # (limpeza na parada, se necessário no futuro)


app = FastAPI(
    title="PreparAI",
    description="API de Pesquisa Documental Militar — PreparAI",
    lifespan=lifespan,
)


# ── Modelo de dados da requisição ─────────────────────────────────────────
class ChatRequest(BaseModel):
    mensagem: str


from fastapi.responses import FileResponse, StreamingResponse

# ── Rota da API de chat ───────────────────────────────────────────────────
@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    """Recebe a mensagem do front-end, consulta o RAG e retorna a resposta."""
    if not req.mensagem.strip():
        raise HTTPException(status_code=400, detail="Mensagem não pode ser vazia.")
    try:
        resposta_ia = assistente.perguntar_ao_assistente(req.mensagem)
        return {"resposta": resposta_ia}
    except Exception as e:
        print(f"ERRO em /api/chat: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao processar a consulta.")

@app.post("/api/chat/stream")
async def chat_stream_endpoint(req: ChatRequest):
    """Rota para streaming contínuo da resposta do LLM."""
    if not req.mensagem.strip():
        raise HTTPException(status_code=400, detail="Mensagem não pode ser vazia.")
    
    return StreamingResponse(
        assistente.perguntar_ao_assistente_stream(req.mensagem),
        media_type="text/plain"
    )



# ── Arquivos estáticos (CSS, JS, imagens) ─────────────────────────────────
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


# ── Rota raiz — entrega o HTML do chat ────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse(str(_INDEX))


# ── Ponto de entrada direto (python main.py) ──────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
