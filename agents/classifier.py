"""
classifier.py — Classifier Agent

Reads the first ~2000 characters of a document and asks Gemini
to identify the document type, then returns the matching policy.
"""

import logging
from .base_agent import BaseAgent
from .policies import POLICIES

logger = logging.getLogger(__name__)


class ClassifierAgent(BaseAgent):

    def __init__(self):
        super().__init__("ClassifierAgent")

    def classify(self, document_text: str) -> dict:
        """
        Classify the document and return:
          {
            "doc_type"  : "CONTRACT",
            "confidence": "high",
            "policy"    : { ... }   # from policies.py
          }
        """
        snippet = document_text[:2000].strip()

        prompt = f"""You are a document classification expert.

Read the following document excerpt and classify it into ONE of these categories:
  CV, CONTRACT, MEDICAL, COURT_FILING, FINANCIAL, OTHER

Document excerpt:
\"\"\"
{snippet}
\"\"\"

Reply with ONLY a JSON object in this exact format (no markdown, no explanation):
{{"doc_type": "CONTRACT", "confidence": "high", "reason": "one sentence"}}

confidence must be one of: high, medium, low
"""

        self.log.info("Classifying document...")
        response = self._call_llm(prompt)

        doc_type, confidence, reason = self._parse_response(response)
        policy = POLICIES.get(doc_type, POLICIES["OTHER"])

        self.log.info(
            "Classification → %s (confidence: %s) | %s", doc_type, confidence, reason
        )
        return {
            "doc_type":   doc_type,
            "confidence": confidence,
            "reason":     reason,
            "policy":     policy,
        }

    def _parse_response(self, response: str) -> tuple:
        """Parse the LLM JSON response safely."""
        import json, re

        # Strip markdown code fences if present
        response = re.sub(r"```(?:json)?", "", response).strip()

        try:
            data = json.loads(response)
            doc_type   = data.get("doc_type", "OTHER").upper()
            confidence = data.get("confidence", "low")
            reason     = data.get("reason", "")
            if doc_type not in POLICIES:
                doc_type = "OTHER"
            return doc_type, confidence, reason
        except (json.JSONDecodeError, AttributeError):
            self.log.warning("Could not parse classifier response: %s", response[:200])
            return "OTHER", "low", "parse error"
