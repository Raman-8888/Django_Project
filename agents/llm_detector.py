"""
llm_detector.py — LLM-based PII Detector Agent

Runs TWO passes on the document text:
  Pass 1: Regex patterns  (phone, email, Aadhaar, PAN, URLs — instant)
  Pass 2: Gemini LLM      (contextual PII that regex misses)

Returns a list of dicts:
  [{"text": "...", "label": "PHONE", "start": 42, "end": 53}, ...]
"""

import re
import logging
from .base_agent import BaseAgent
from .policies import UNIVERSAL_PATTERNS

logger = logging.getLogger(__name__)

# Max chars per LLM chunk (stay well within token limits)
LLM_CHUNK_SIZE = 3000


class LLMDetectorAgent(BaseAgent):

    def __init__(self):
        super().__init__("LLMDetectorAgent")

    def detect(self, text: str, policy: dict) -> list[dict]:
        """
        Find PII in *text* using regex + LLM.
        Returns list of entity dicts with keys: text, label, start, end.
        """
        entities = []

        # --- Pass 1: Universal regex patterns ---
        regex_entities = self._regex_pass(text)
        self.log.info("Regex pass: %d entities found", len(regex_entities))
        entities.extend(regex_entities)

        # --- Pass 2: LLM contextual detection ---
        llm_hint = policy.get("llm_hint", "")
        required = policy.get("required_fields", [])
        llm_entities = self._llm_pass(text, llm_hint, required)
        self.log.info("LLM pass: %d entities found", len(llm_entities))
        entities.extend(llm_entities)

        # Deduplicate by exact text match
        seen = set()
        unique = []
        for e in entities:
            key = (e["text"].lower().strip(), e["label"])
            if key not in seen:
                seen.add(key)
                unique.append(e)

        self.log.info("Total unique entities after dedup: %d", len(unique))
        return unique

    # ------------------------------------------------------------------
    # Pass 1 — Regex
    # ------------------------------------------------------------------

    def _regex_pass(self, text: str) -> list[dict]:
        entities = []
        for label, pattern in UNIVERSAL_PATTERNS.items():
            for match in re.finditer(pattern, text):
                entities.append({
                    "text":  match.group(),
                    "label": label,
                    "start": match.start(),
                    "end":   match.end(),
                    "source": "regex",
                })
        return entities

    # ------------------------------------------------------------------
    # Pass 2 — LLM
    # ------------------------------------------------------------------

    def _llm_pass(self, text: str, llm_hint: str, required_fields: list) -> list[dict]:
        """Split text into chunks and run LLM on each."""
        chunks = self._chunk_text(text, LLM_CHUNK_SIZE)
        all_entities = []
        offset = 0
        for chunk in chunks:
            chunk_entities = self._llm_detect_chunk(chunk, llm_hint, required_fields, offset)
            all_entities.extend(chunk_entities)
            offset += len(chunk)
        return all_entities

    def _llm_detect_chunk(self, chunk: str, hint: str, fields: list, offset: int) -> list[dict]:
        fields_str = "\n".join(f"  - {f}" for f in fields)
        prompt = f"""You are a PII detection expert. {hint}

Find ALL personally identifiable information in the text below.
Look specifically for:
{fields_str}

For each piece of PII found, output one line in this exact format:
LABEL|exact text as it appears

Valid labels: PERSON, ORG, EMAIL, PHONE, ADDRESS, DATE, AADHAAR, PAN,
              PASSPORT, ACCOUNT_NUMBER, URL, LINKEDIN, GITHUB, ID_NUMBER,
              MONEY, PERCENT, MEDICAL_ID, CASE_NUMBER, OTHER_PII

Rules:
- Only output lines in the LABEL|text format, nothing else.
- If nothing is found, output: NONE
- Do NOT invent text. Copy it exactly from the document.
- Do NOT include generic words like "the company" or "the plaintiff".

Text:
\"\"\"
{chunk}
\"\"\"
"""
        response = self._call_llm(prompt)
        return self._parse_llm_response(response, chunk, offset)

    def _parse_llm_response(self, response: str, chunk: str, offset: int) -> list[dict]:
        entities = []
        if not response or response.strip().upper() == "NONE":
            return entities

        for line in response.strip().splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            parts = line.split("|", 1)
            if len(parts) != 2:
                continue
            label, entity_text = parts[0].strip().upper(), parts[1].strip()
            if not entity_text or entity_text.upper() == "NONE":
                continue

            # Find the entity in the chunk to get char offsets
            idx = chunk.find(entity_text)
            if idx == -1:
                # Try case-insensitive
                lower_chunk = chunk.lower()
                lower_text  = entity_text.lower()
                idx = lower_chunk.find(lower_text)

            if idx != -1:
                entities.append({
                    "text":  entity_text,
                    "label": label,
                    "start": offset + idx,
                    "end":   offset + idx + len(entity_text),
                    "source": "llm",
                })
            else:
                # Still record it even without exact offset (for the redaction map)
                entities.append({
                    "text":  entity_text,
                    "label": label,
                    "start": -1,
                    "end":   -1,
                    "source": "llm",
                })

        return entities

    def _chunk_text(self, text: str, size: int) -> list[str]:
        """Split text at paragraph boundaries to stay within chunk size."""
        chunks, current = [], ""
        for para in text.split("\n"):
            if len(current) + len(para) > size:
                if current:
                    chunks.append(current)
                current = para + "\n"
            else:
                current += para + "\n"
        if current:
            chunks.append(current)
        return chunks or [text]
