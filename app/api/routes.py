import secrets
from pathlib import Path
import logging
from fastapi  import APIRouter, UploadFile, File, HTTPException, Header
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field 
from app.core.config import get_settings
from app.rag.workflow import ask
from app.rag.vectorstore import add_documents
from app.services.ingestion import load_file, chunk_documents, SUPPORTED
from app.services.audit import write_audit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
settings = get_settings()

# Passing the question to agent
class ChatRequest(BaseModel):
    question: str = Field(min_length=2, max_length=3000)

@router.get("/health")
def health():
    return {"status": "ok", "service": settings.app_name}

@router.post("/chat")
def chat(payload: ChatRequest):
    try:
        result = ask(payload.question)
        try:
            write_audit(payload.question, result["source_used"], result.get("trace", []))
        except Exception:
            logger.exception("Audit write failed")   # never lose an answer because logging failed

        return {
            "answer": result["answer"],
            "source_used": result["source_used"],
            "trace": result.get("trace",[]),
            "citations": result.get("citations", []),
            "rewritten_query": result.get("current_query", payload.question),
        }

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# uploading new documents 
# Here we use asyncronize function 

def index_file(path: Path) -> tuple[int, int]:
    docs = load_file(path)                             # Documents/file get loaded
    for d in docs:
        d.metadata["source"] = path.name               # short name shown in citations
    chunks = chunk_documents(docs)                     # Performing chunks
    if not chunks:
        raise ValueError("No readable text found in the document")
    ids = add_documents(chunks)                        # Adding chunks to vector database
    return len(chunks), len(ids)


def require_admin(key: str) -> None:
    if settings.admin_api_key in ("", "change-me"):
        raise HTTPException(status_code=503, detail="ADMIN_API_KEY is not configured on the server")
    if not secrets.compare_digest(key.encode(), settings.admin_api_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid admin key")


@router.post("/admin/verify")
def verify_admin(x_admin_key: str = Header(default="")):
    require_admin(x_admin_key)
    return {"ok": True}


@router.post("/ingest")
async def ingest(file: UploadFile = File(...), x_admin_key: str = Header(default="")):
    require_admin(x_admin_key)   # Checking the admin password to upload a new document
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED:
        raise HTTPException(status_code=400, detail=f"Supported: {', '.join(sorted(SUPPORTED))}")
    upload_dir = Path(settings.upload_dir)             # Upload file to the data folder
    upload_dir.mkdir(parents=True, exist_ok=True)      # If folder is not there then we create it
    dest = upload_dir / Path(file.filename).name
    dest.write_bytes(await file.read())
    try:
        chunks_count, ids_count = await run_in_threadpool(index_file, dest)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Indexing failed")
        raise HTTPException(status_code=500, detail=f"Indexing failed: {exc}") from exc
    return {"message": "Document indexed", "file": dest.name, "chunks": chunks_count, "ids_created": ids_count}
