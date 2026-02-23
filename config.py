"""LLM model configuration loaded from environment variables.

Creates a google.genai Client for direct LLM calls (no ADK dependency).

Environment variables:
    GOOGLE_API_KEY          - Google AI API key (required)
    GOOGLE_GENAI_USE_VERTEXAI - Set to "TRUE" to use Vertex AI backend
    LLM_MODEL               - Model name (default: "gemini-2.5-flash")
"""

import os

from dotenv import load_dotenv
from google.genai import Client

load_dotenv()


def get_model_name() -> str:
    """Return the configured LLM model name."""
    return os.getenv("LLM_MODEL", "gemini-2.5-flash")


def get_genai_client() -> Client:
    """Create and return a google.genai Client instance."""
    api_key = os.getenv("GOOGLE_API_KEY")
    use_vertexai = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() == "TRUE"

    return Client(
        api_key=api_key if not use_vertexai else None,
        vertexai=use_vertexai,
    )


# Module-level singleton for convenience
_client: Client | None = None


def get_client() -> Client:
    """Return a cached genai Client singleton."""
    global _client
    if _client is None:
        _client = get_genai_client()
    return _client
