"""
qa_agent.py — QA Agent

After redaction is complete, the QA Agent re-scans the redacted document
text and checks for any surviving PII. It produces a QA report with:
  - overall confidence score (0-100)
  - list of any flagged surviving PII
  - recommendation (approved / needs_review)
"""

import json
import logging
import re
from .base_agent import BaseAgent

logger = logging.getLogger(__name__)


class QAAgent(BaseAgent):

    def __init__(self):
        super().__init__("QAAgent")

    def run(self, redacted_text: str, original_entity_count: int) -> dict:
        """
        Scan *redacted_text* for surviving PII.

        Returns:
        {
          "confidence_score"      : 94,          # 0-100
          "surviving_pii"         : [...],        # list of found items
          "surviving_pii_count"   : 2,
          "recommendation"        : "approved",   # or "needs_review"
          "summary"               : "..."
        }
        """
        self.log.info("QA scan started on redacted document...")

        chunks = self._chunk_text(redacted_text, 3000)
        surviving = []

        for chunk in chunks:
            found = self._scan_chunk(chunk)
            surviving.extend(found)

        # Deduplicate
        seen = set()
        unique_surviving = []
        for item in surviving:
            key = item["text"].lower().strip()
            if key not in seen:
                seen.add(key)
                unique_surviving.append(item)

        count = len(unique_surviving)

        # Score: start at 100, deduct per surviving PII item
        if original_entity_count > 0:
            penalty = min(count / original_entity_count, 1.0) * 40
        else:
            penalty = count * 5

        confidence = max(0, round(100 - penalty))
        recommendation = "approved" if confidence >= 85 else "needs_review"

        summary = (
            f"QA scan complete. {count} potential PII item(s) may have survived redaction. "
            f"Confidence score: {confidence}/100."
        )

        self.log.info(summary)

        return {
            "confidence_score":    confidence,
            "surviving_pii":       unique_surviving,
            "surviving_pii_count": count,
            "recommendation":      recommendation,
            "summary":             summary,
        }

    # ------------------------------------------------------------------

    def _scan_chunk(self, chunk: str) -> list[dict]:
        prompt = f"""You are a PII audit expert. The following text has already been redacted.
Your job is to find any personally identifiable information that was NOT redacted
and is still visible in plain text.

IGNORE these — they are valid redaction labels, not real PII:
  PERSON_0, PERSON_1, ORG_0, ORG_1, XXXXX, [REDACTED]

Look for surviving:
  - Real person names (not "PERSON_0")
  - Real company names (not "ORG_0")
  - Phone numbers, email addresses, URLs
  - Aadhaar, PAN, passport, account numbers
  - Real physical addresses
  - Specific financial amounts tied to an identity

Redacted text:
\"\"\"
{chunk}
\"\"\"

Reply with ONLY a JSON array. If nothing is found reply with [].
Format:
[
  {{"text": "john.smith@email.com", "type": "EMAIL", "risk": "high"}},
  {{"text": "+91 98765 43210",      "type": "PHONE", "risk": "high"}}
]
"""
        response = self._call_llm(prompt)
        return self._parse_response(response)

    def _parse_response(self, response: str) -> list[dict]:
        response = re.sub(r"```(?:json)?", "", response).strip()
        try:
            data = json.loads(response)
            if isinstance(data, list):
                return [
                    {"text": d.get("text",""), "type": d.get("type","?"), "risk": d.get("risk","medium")}
                    for d in data if d.get("text")
                ]
        except (json.JSONDecodeError, TypeError):
            self.log.warning("QA agent response parse failed: %s", response[:200])
        return []

    def _chunk_text(self, text: str, size: int) -> list[str]:
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
