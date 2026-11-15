"""
base_agent.py — Shared base class for all agents.

Uses the new google-genai SDK (google.genai).
"""

import logging
import os
import time

logger = logging.getLogger(__name__)

_gemini_client = None


def _init_gemini():
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client

    try:
        from google import genai
        api_key = os.environ.get("GEMINI_API_KEY") or _read_env_key()
        if not api_key:
            raise EnvironmentError("GEMINI_API_KEY not set")
        _gemini_client = genai.Client(api_key=api_key)
        logger.info("[Gemini] Client initialised (google-genai SDK)")
        return _gemini_client
    except Exception as exc:
        logger.error("[Gemini] Failed to initialise: %s", exc)
        raise


def _read_env_key():
    env_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), '..', 'django_app', '.env')
    )
    if not os.path.isfile(env_path):
        return None
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


class BaseAgent:
    MODEL = "gemini-3.8-flash"

    def __init__(self, name: str):
        self.name = name
        self.log  = logging.getLogger(f"agents.{name}")

    def _call_llm(self, prompt: str, retries: int = 2) -> str:
        client = _init_gemini()
        for attempt in range(retries + 1):
            try:
                self.log.debug("LLM call (attempt %d)", attempt + 1)
                response = client.models.generate_content(
                    model=self.MODEL,
                    contents=prompt,
                )
                return response.text.strip()
            except Exception as exc:
                self.log.warning("LLM attempt %d failed: %s", attempt + 1, exc)
                if attempt < retries:
                    time.sleep(2 ** attempt)
        self.log.error("All LLM attempts failed for %s", self.name)
        return ""
