"""Env-driven configuration. Loaded once at import via python-dotenv.

All other modules read these constants rather than touching os.environ directly,
so the LLM endpoint / DB DSN / cache dir are set in exactly one place (.env).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (the parent of this package dir). The server
# entry is also bootstrapped to put the project root on sys.path before this
# import happens, so __file__-relative resolution is stable regardless of CWD.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# --- Postgres ---
DB_DSN: str = os.environ.get("DB_DSN", "postgresql://eval:eval@localhost:5435/evalutea_classi_ocr")

# --- LLM endpoint (OpenAI-compatible) ---
LLM_ENDPOINT: str = os.environ.get("LLM_ENDPOINT", "http://localhost:4000")
MODEL_API_KEY: str = os.environ.get("MODEL_API_KEY", "sk-1234")
MODEL_NAME: str = os.environ.get("MODEL_NAME", "Qwen/Qwen3.6-35B-A3B")

# Classification/OCR call tuning. Wind's defaults: temperature 0.0, thinking off
# (the modelharbor proxy has no standard "think" field). Kept as module constants
# so a future tuning pass changes them in one spot.
LLM_TEMPERATURE: float = float(os.environ.get("LLM_TEMPERATURE", "0.0"))
LLM_THINK: bool = os.environ.get("LLM_THINK", "0") in ("1", "true", "True")

# --- Per-filetype production extractor (eval.ai.extract) --------------------
# The real extractors live under service.* in the elicense repo; its root must
# be on sys.path so the relative imports inside the extractor modules resolve.
# NOT /home/admins/... (per the env-path memory) — the real repo is under
# /home/user/....
ELICENSE_REPO_PATH: Path = Path(
    os.environ.get(
        "ELICENSE_REPO_PATH",
        "/home/user/aiProject/dev/korn/elicense-approval-supportsystem-dev",
    )
)
# Central OCR service (detect-rotation + the ocr_text for the 6 fulltext slugs).
# Empty string -> extract.make_clients() passes ocr_client=None -> JSON-only
# slugs still succeed (rotation skipped, fails-safe to 0); fulltext slugs error
# on the OCR call (expected when the service is down).
CENTRAL_OCR_BASE_URL: str = os.environ.get("CENTRAL_OCR_BASE_URL", "http://localhost:8123")
# Per-request timeout for the production ChatCompletionsService (seconds).
LLM_TIMEOUT: int = int(os.environ.get("LLM_TIMEOUT", "120"))

# --- Page-PNG cache (rendered on demand, keyed by sha256) ---
_CACHE_DEFAULT = str(Path(__file__).resolve().parent.parent / ".cache" / "pages")
CACHE_DIR: Path = Path(os.environ.get("CACHE_DIR", _CACHE_DEFAULT))

# --- GET-mock JSON tree root (source of petition metadata) ---
MOCK_ROOT: Path = Path(
    os.environ.get(
        "MOCK_ROOT",
        "/home/admins/aiProject/dev/korn/elicense-query/แกะapiระบบElicense-ดึงจากDB",
    )
)

# --- Request-Body modal: master-data dir (GeoResolver reads 3 JSONs) + doc root ---
# GEO_MASTER_DIR holds province/district/subdistrict master-data JSONs used by
# build_datastring to resolve numeric geo codes to Thai names. DOC_ROOT is the
# local attachment tree (S3-bucket mirror) build_attachments resolves URLs to.
# Both verified to exist on this box; both env-overridable.
GEO_MASTER_DIR: Path = Path(
    os.environ.get(
        "GEO_MASTER_DIR",
        "/home/admins/aiProject/dev/korn/elicense-query/แกะapiระบบE-license/Elicense-network-collection",
    )
)
DOC_ROOT: Path = Path(
    os.environ.get(
        "DOC_ROOT",
        "/home/admins/aiProject/eLicenseDocuments/miid-attachment-prod",
    )
)

# --- filtered.csv location (project root) ---
CSV_PATH: Path = Path(__file__).resolve().parent.parent / "filtered.csv"

# HTTP server port (the worker + indexer don't bind a port).
HTTP_PORT: int = int(os.environ.get("HTTP_PORT", "8080"))
