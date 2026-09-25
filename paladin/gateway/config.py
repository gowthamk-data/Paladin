import os

POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "change_me_strong_password")
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:change_me_strong_password@postgres:5432/paladin",
)

JWT_PRIVATE_KEY_PATH = os.environ.get("JWT_PRIVATE_KEY_PATH", "/app/keys/private.pem")
JWT_PUBLIC_KEY_PATH = os.environ.get("JWT_PUBLIC_KEY_PATH", "/app/keys/public.pem")
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "RS256")
JWT_EXPIRY_MINUTES = int(os.environ.get("JWT_EXPIRY_MINUTES", "30"))

CHROMADB_HOST = os.environ.get("CHROMADB_HOST", "chromadb")
CHROMADB_PORT = int(os.environ.get("CHROMADB_PORT", "8000"))
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
# Local-development default is the small instruct model; deployed/demo uses
# OLLAMA_MODEL=llama3 via .env. Selection is entirely environment-driven.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
# Hard ceiling for a single generation call. Without it a stalled Ollama request would hold a
# worker thread forever (and, in a single-worker deployment, stall the whole gateway).
OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "300"))

RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "10"))
RATE_LIMIT_BURST = int(os.environ.get("RATE_LIMIT_BURST", "20"))

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_DIMENSION", "384"))
# sentence-transformers cache location. Pointed at a named volume in docker-compose so the
# downloaded embedding model survives container recreation instead of being re-fetched.
SENTENCE_TRANSFORMERS_HOME = os.environ.get("SENTENCE_TRANSFORMERS_HOME", "")

# Request-size guards. The prompt is bounded separately from the raw body because /ask takes
# `message` as a query parameter, so a body limit alone would not bound the prompt.
MAX_PROMPT_CHARS = int(os.environ.get("MAX_PROMPT_CHARS", "8000"))
MAX_REQUEST_BYTES = int(os.environ.get("MAX_REQUEST_BYTES", str(MAX_PROMPT_CHARS * 4)))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
