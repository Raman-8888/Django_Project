"""
verifier.py — Verifier Agent

Takes the combined list of PII candidates from spaCy + LLM Detector
and removes false positives using LLM context understanding.

Returns:
  verified   : list of confirmed PII entities (with confidence scores)
  rejected   : list of removed false positives (for audit log)
"""

import json
import logging
import re
from .base_agent import BaseAgent

logger = logging.getLogger(__name__)

# Never send more than this many entities to the LLM at once
BATCH_SIZE = 20


class VerifierAgent(BaseAgent):

    def __init__(self):
        super().__init__("VerifierAgent")

    def verify(self, entities: list[dict], document_text: str) -> dict:
        """
        Verify a list of entity dicts.
        Each entity must have at least: text, label
        Returns {"verified": [...], "rejected": [...]}
        """
        if not entities:
            return {"verified": [], "rejected": []}

        verified, rejected = [], []

        # Process in batches
        for i in range(0, len(entities), BATCH_SIZE):
            batch = entities[i : i + BATCH_SIZE]
            context = self._get_context(document_text, batch)
            batch_result = self._verify_batch(batch, context)
            verified.extend(batch_result["verified"])
            rejected.extend(batch_result["rejected"])

        self.log.info(
            "Verification done: %d confirmed, %d rejected as false positives",
            len(verified), len(rejected),
        )
        return {"verified": verified, "rejected": rejected}

    # ------------------------------------------------------------------

    def _get_context(self, text: str, batch: list[dict]) -> str:
        """Extract up to 500 chars of surrounding context for each entity."""
        snippets = []
        for ent in batch:
            start = ent.get("start", -1)
            if start >= 0:
                s = max(0, start - 100)
                e = min(len(text), start + len(ent["text"]) + 100)
                snippets.append(text[s:e])
        return "\n---\n".join(snippets[:5])   # limit context size

    def _verify_batch(self, batch: list[dict], context: str) -> dict:
        entities_str = "\n".join(
            f'{i+1}. [{e["label"]}] "{e["text"]}"'
            for i, e in enumerate(batch)
        )

        prompt = f"""You are a PII verification expert. Your job is to remove FALSE POSITIVES
from a list of detected PII entities.

Context from the document:
\"\"\"
{context[:1500]}
\"\"\"

Detected entities:
{entities_str}

For each entity, decide: is it genuinely PII that should be redacted?

Common false positives to reject:
- Country/city names flagged as ORG (e.g. "India", "Mumbai")
- Month names flagged as DATE (e.g. "May", "March" when used as names)
- Generic company words ("the company", "the firm")
- Product names that are also common words ("Apple" = fruit, not company)
- Legal boilerplate terms ("Party A", "the Defendant" if fictional)
- Percentages in legal thresholds (context-dependent)

Reply with ONLY a JSON object in this exact format:
{{
  "results": [
    {{"index": 1, "keep": true,  "confidence": 0.95, "reason": "clear person name"}},
    {{"index": 2, "keep": false, "reason": "country name, not an organisation"}}
  ]
}}
"""
        response = self._call_llm(prompt)
        return self._parse_verification(response, batch)

    def _parse_verification(self, response: str, batch: list[dict]) -> dict:
        verified, rejected = [], []

        # Strip markdown fences
        response = re.sub(r"```(?:json)?", "", response).strip()

        try:
            data = json.loads(response)
            results = data.get("results", [])
            result_map = {r["index"]: r for r in results}

            for i, entity in enumerate(batch, start=1):
                result = result_map.get(i, {})
                keep   = result.get("keep", True)   # default: keep if unsure
                confidence = result.get("confidence", 0.7)
                reason     = result.get("reason", "")

                enriched = {**entity, "confidence": confidence, "reason": reason}

                if keep:
                    verified.append(enriched)
                else:
                    rejected.append(enriched)

        except (json.JSONDecodeError, TypeError):
            self.log.warning("Could not parse verifier response — keeping all entities")
            # On parse failure, keep everything (better safe than sorry)
            verified = [{**e, "confidence": 0.5, "reason": "verifier parse error"} for e in batch]

        return {"verified": verified, "rejected": rejected}
