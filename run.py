
''' from app.core.config import get_settings

settings = get_settings()

print(f"App Name: {settings.app_name}")
print(f"Groq API Key: {settings.groq_api_key}")

from app.services.ingestion import load_file, chunk_documents
from pathlib import Path

docs = load_file(Path("data/sample_kb/company_it_handbook.md"))
chunk = chunk_documents(docs)

print(docs)
print("-------------------------------")
print("-------------------------------")
print(chunk)
'''

from app.services.ingestion import load_file, chunk_documents               
from pathlib import Path
from app.rag.vectorstore import add_documents

docs = load_file(Path("data/sample_kb/company_it_handbook.md"))
chunks = chunk_documents(docs)

## Adding documents
add_documents(chunks)