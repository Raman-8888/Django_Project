"""
orchestrator.py — Orchestrator Agent

Coordinates the full multi-agent redaction pipeline:

  1. ClassifierAgent  → detect document type, load policy
  2. LLMDetectorAgent → find PII via regex + LLM
  3. VerifierAgent    → remove false positives
  4. DataRedaction    → in-place DOCX redaction (spaCy + LLM entities)
  5. QAAgent          → scan output, produce confidence report

Returns a full job result dict that tasks.py stores to the DB.
"""

import logging
import os
import sys

logger = logging.getLogger(__name__)

# Ensure the existing src/ is importable
_SRC_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', 'legal_docs_redaction', 'src')
)
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

# Ensure agents/ itself is importable
_AGENTS_PATH = os.path.dirname(__file__)
if _AGENTS_PATH not in sys.path:
    sys.path.insert(0, _AGENTS_PATH)


from agents.classifier   import ClassifierAgent
from agents.llm_detector import LLMDetectorAgent
from agents.verifier     import VerifierAgent
from agents.qa_agent     import QAAgent


class Orchestrator:
    """
    Single entry-point for the full multi-agent pipeline.
    Instantiate once and call run_file() per document.
    """

    def __init__(self):
        self.classifier  = ClassifierAgent()
        self.llm_detector = LLMDetectorAgent()
        self.verifier    = VerifierAgent()
        self.qa          = QAAgent()
        self._spacy_redactor = None   # lazy-loaded (spaCy model is expensive)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_file(self, docx_path: str, output_dir: str, original_filename: str) -> dict:
        """
        Run the full pipeline on a single DOCX file.

        Returns:
        {
          "redacted_docx_path": "...",
          "redaction_map_path": "...",
          "classification": {...},
          "llm_entity_count": 12,
          "verified_entity_count": 10,
          "qa_report": {...},
        }
        """
        logger.info("[Orchestrator] ▶ Starting pipeline for: %s", original_filename)

        # --- Step 1: Read document text ---
        doc_text = self._read_docx_text(docx_path)

        # --- Step 2: Classify document ---
        logger.info("[Orchestrator] Step 1/5 — Classification")
        classification = self.classifier.classify(doc_text)
        policy = classification["policy"]
        logger.info(
            "[Orchestrator] Document type: %s (confidence: %s)",
            classification["doc_type"], classification["confidence"]
        )

        # --- Step 3: LLM-based PII detection ---
        logger.info("[Orchestrator] Step 2/5 — LLM PII Detection")
        llm_entities = self.llm_detector.detect(doc_text, policy)
        logger.info("[Orchestrator] LLM + Regex found %d entities", len(llm_entities))

        # --- Step 4: Verify (remove false positives) ---
        logger.info("[Orchestrator] Step 3/5 — Verification")
        verification = self.verifier.verify(llm_entities, doc_text)
        verified_entities = verification["verified"]
        rejected_entities = verification["rejected"]
        logger.info(
            "[Orchestrator] Verified: %d kept, %d rejected",
            len(verified_entities), len(rejected_entities)
        )

        # --- Step 5: Redact document (spaCy in-place + inject LLM entities) ---
        logger.info("[Orchestrator] Step 4/5 — Redaction")
        redactor = self._get_spacy_redactor(docx_path, output_dir)

        # Inject the LLM-verified entities into the redactor so it
        # replaces them in addition to whatever spaCy finds.
        redactor.extra_entities = verified_entities

        redactor.redact_one_file(docx_path)

        # Locate output files
        base = os.path.splitext(os.path.basename(docx_path))[0]
        redacted_docx = os.path.join(output_dir, base + "_redacted.docx")
        redaction_map = os.path.join(output_dir, base + "_redaction_map.csv")

        if not os.path.isfile(redacted_docx):
            raise FileNotFoundError(f"Redacted file not produced: {redacted_docx}")

        # --- Step 6: QA scan ---
        logger.info("[Orchestrator] Step 5/5 — QA Scan")
        redacted_text = self._read_docx_text(redacted_docx)
        total_entities = len(llm_entities)
        qa_report = self.qa.run(redacted_text, total_entities)
        logger.info(
            "[Orchestrator] QA score: %d/100 | Recommendation: %s",
            qa_report["confidence_score"], qa_report["recommendation"]
        )

        logger.info("[Orchestrator] ✅ Pipeline complete for: %s", original_filename)

        return {
            "redacted_docx_path":    redacted_docx,
            "redaction_map_path":    redaction_map,
            "classification":        classification,
            "llm_entity_count":      len(llm_entities),
            "verified_entity_count": len(verified_entities),
            "rejected_entity_count": len(rejected_entities),
            "qa_report":             qa_report,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_spacy_redactor(self, docx_path: str, output_dir: str):
        """Lazy-load DataRedaction (spaCy model takes ~30s first time)."""
        if self._spacy_redactor is None:
            from spacy_redaction import DataRedaction
            docx_dir = os.path.dirname(docx_path)
            self._spacy_redactor = DataRedaction(
                input_dir_path=docx_dir,
                redacted_output_path=output_dir,
            )
        else:
            # Update paths for new file
            self._spacy_redactor.input_dir_path  = os.path.dirname(docx_path)
            self._spacy_redactor.redacted_output_path = output_dir

        return self._spacy_redactor

    def _read_docx_text(self, docx_path: str) -> str:
        """Extract plain text from a DOCX for LLM consumption."""
        try:
            from docx import Document
            doc = Document(docx_path)
            parts = [p.text for p in doc.paragraphs]
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        parts.extend(p.text for p in cell.paragraphs)
            return "\n".join(parts)
        except Exception as exc:
            logger.warning("Could not read DOCX text: %s", exc)
            return ""
