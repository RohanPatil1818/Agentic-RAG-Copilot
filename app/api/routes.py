
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
from app.services.semantic_cache import ask_with_cache, bump_kb_version

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
settings = get_settings()
MAX_UPLOAD_BYTES = 10 * 1024 * 1024      # 10 MB

# Passing the question to agent
class ChatRequest(BaseModel):
    question: str = Field(min_length=2, max_length=3000)

@router.get("/health")
def health():
    return {"status": "ok", "service": settings.app_name}

@router.post("/chat")
def chat(payload: ChatRequest):
    try:
        result = ask_with_cache(payload.question, ask)
        cache = result.get("cache") or {}
        try:
            write_audit(payload.question, result["source_used"], result.get("trace", []),
                        cache_status=cache.get("status"), cache_layer=cache.get("layer"), latency_ms=result.get("latency_ms"),
                        )
        except Exception:
            logger.exception("Audit write failed")   # never lose an answer because logging failed

        return {
            "answer": result["answer"],
            "source_used": result["source_used"],
            "trace": result.get("trace",[]),
            "citations": result.get("citations", []),
            "rewritten_query": result.get("current_query") or payload.question,
            "cache": result.get("cache"),
            "latency_ms": result.get("latency_ms"),
        }

    except Exception as exc:
        logger.exception("chat failed")
        raise HTTPException(status_code=500, detail="Something went wrong. Please try again.") from exc


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
    data = await file.read(MAX_UPLOAD_BYTES + 1)        # read at most limit + 1 byte, never the whole file
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)")
    dest.write_bytes(data)
    try:
        chunks_count, ids_count = await run_in_threadpool(index_file, dest)
        bump_kb_version()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Indexing failed")
        raise HTTPException(status_code=500, detail="Indexing failed. Check the server log.") from exc
    return {"message": "Document indexed", "file": dest.name, "chunks": chunks_count, "ids_created": ids_count}
 

