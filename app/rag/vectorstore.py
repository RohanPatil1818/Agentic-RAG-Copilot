
import hashlib
import time
from pinecone import Pinecone, ServerlessSpec
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_pinecone import PineconeVectorStore
from app.core.config import get_settings

settings = get_settings()

_embeddings = None
_vectorstore = None

EMBEDDING_DIMENSIONS = {
    "BAAI/bge-small-en-v1.5" : 384,   # Currently using this model
    "all-MiniLM-L6-v2" : 384,
    "text-embedding-3-small" : 1536,     # OpenAI model for embedding
    "text-embedding-3-large" : 3072,     # We can use it in future if we required
}

# Returning the Dimension of the embedding model. If the model is not supported, it raises a ValueError.
def get_embedding_dimension(model_name: str | None = None) -> int:
    name = (model_name or settings.embeddings_model or "").strip()
    if not name:
        raise RuntimeError("Embedding model is not configured")

    normalized = name.lower()

    for model, dimension in EMBEDDING_DIMENSIONS.items():
        if normalized == model.strip().lower():
            return dimension


    raise ValueError(
        f"Unsupported embedding model '{model_name or settings.embeddings_model}' for Pinecone."
        "Add the matching dimension to EMBEDDING_DIMENSIONS."
    )

# Here we create a Embedding
def get_embeddings():
    global _embeddings
    if _embeddings is None:
        _embeddings = HuggingFaceEmbeddings(
            model = settings.embeddings_model,
       #     api_key = settings.hf_api_key             #here huggingface not use api its run locally on our machine
        )
    return _embeddings


def ensure_index():
    if not settings.pinecone_api_key:
        raise RuntimeError("PINECONE_API_KEY is missing")

    desired_dimension = get_embedding_dimension()     # Function calling to get dimension of the embedding model
    
    pc = Pinecone(api_key=settings.pinecone_api_key)
    names = [x.name for x in pc.list_indexes()]

    if settings.pinecone_index_name in names:
        index_info = pc.describe_index(settings.pinecone_index_name)
        current_dimension = getattr(index_info, "dimension", None)
        if current_dimension is None and isinstance(index_info, dict):
            current_dimension = index_info.get("dimension")

        if current_dimension is not None and current_dimension != desired_dimension:
            raise RuntimeError(
                f"Pinecone index '{settings.pinecone_index_name}' has dimension {current_dimension} "
                f"but embeddings model '{settings.embeddings_model}' needs {desired_dimension}. "
                "Use a different PINECONE_INDEX_NAME or delete the index manually."
            )

    if settings.pinecone_index_name not in [x.name for x in pc.list_indexes()]:
        pc.create_index(
            name=settings.pinecone_index_name,
            dimension=desired_dimension,
            metric="cosine",
            spec=ServerlessSpec(cloud='aws', region='us-east-1'),
        )
        while not pc.describe_index(settings.pinecone_index_name).status['ready']:
            time.sleep(3)

    return pc.Index(settings.pinecone_index_name)


def get_vectorstore():
    global _vectorstore

    if _vectorstore is None:
        # Ensure the Pinecone index exists and has the correct dimension
        ensure_index()

        # Connect using the index name and API key
        _vectorstore = PineconeVectorStore(
            index_name=settings.pinecone_index_name,
            embedding=get_embeddings(),
            namespace=settings.pinecone_namespace,
            pinecone_api_key=settings.pinecone_api_key,
        )

    return _vectorstore


# Make the above function as a retriver
def get_retriever():
    return get_vectorstore().as_retriever(search_kwargs={"k": settings.top_k})


# Adding new Documents to vectorstore
def add_documents(chunks):
    # Deterministic ids: re-ingesting the same file overwrites its vectors instead of duplicating them.
    ids = [
        hashlib.sha1(
            f"{c.metadata.get('source', '')}|{c.metadata.get('start_index', i)}|{c.page_content}".encode("utf-8")
        ).hexdigest()
        for i, c in enumerate(chunks)
    ]
    store = get_vectorstore()
    return store.add_documents(chunks, ids=ids)



# For testing this file 
if __name__ == "__main__":
    print("Model:", repr(settings.embeddings_model))
    print("Dimension:", get_embedding_dimension())