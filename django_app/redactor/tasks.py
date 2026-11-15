"""
Background processing logic for legal document redaction.
Now uses the multi-agent Orchestrator pipeline:
  ClassifierAgent → LLMDetectorAgent → VerifierAgent → DataRedaction → QAAgent
"""

import logging
import os
import shutil
import sys
import tempfile
import threading

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
EXISTING_SRC_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'legal_docs_redaction', 'src')
)
AGENTS_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'agents')
)
PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..')
)

for p in [EXISTING_SRC_PATH, AGENTS_PATH, PROJECT_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

# ---------------------------------------------------------------------------
# Lazy singleton — Orchestrator (holds spaCy model + all agents)
# ---------------------------------------------------------------------------
_orchestrator_lock = threading.Lock()
_orchestrator = None


def _get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        with _orchestrator_lock:
            if _orchestrator is None:
                from agents.orchestrator import Orchestrator
                logger.info("Initialising multi-agent Orchestrator...")
                _orchestrator = Orchestrator()
                logger.info("Orchestrator ready.")
    return _orchestrator


# ---------------------------------------------------------------------------
# Main processing function
# ---------------------------------------------------------------------------

def process_redaction_job(job_id: int) -> None:
    """
    Process all files in a RedactionJob using the multi-agent pipeline.
    Called from a background daemon thread.
    """
    from django.conf import settings
    from redactor.models import RedactionJob, RedactionFile

    try:
        job = RedactionJob.objects.get(pk=job_id)
    except RedactionJob.DoesNotExist:
        logger.error("RedactionJob #%s does not exist.", job_id)
        return

    job.status = RedactionJob.STATUS_PROCESSING
    job.save(update_fields=['status'])

    orchestrator = _get_orchestrator()
    all_failed = True

    for rfile in job.files.all():
        rfile.status = RedactionFile.STATUS_PROCESSING
        rfile.save(update_fields=['status'])

        tmp_dir = None
        try:
            tmp_dir = tempfile.mkdtemp(prefix='redaction_')
            original_path = rfile.original_file.path
            ext = os.path.splitext(original_path)[1].lower()

            # ------------------------------------------------------------------
            # Step 1: Ensure we have a DOCX
            # ------------------------------------------------------------------
            if ext == '.pdf':
                docx_path = _convert_pdf_to_docx(original_path, tmp_dir)
            elif ext in ('.doc', '.docx'):
                if ext == '.doc':
                    docx_path = _convert_doc_to_docx(original_path, tmp_dir)
                else:
                    dest = os.path.join(tmp_dir, os.path.basename(original_path))
                    shutil.copy2(original_path, dest)
                    docx_path = dest
            else:
                rfile.status = RedactionFile.STATUS_SKIPPED
                rfile.error_message = f"Unsupported file type: {ext}"
                rfile.save(update_fields=['status', 'error_message'])
                logger.warning("Skipping unsupported file type: %s", original_path)
                continue

            # ------------------------------------------------------------------
            # Step 2: Run the full multi-agent pipeline via Orchestrator
            # ------------------------------------------------------------------
            result = orchestrator.run_file(
                docx_path=docx_path,
                output_dir=tmp_dir,
                original_filename=rfile.original_filename,
            )
            redacted_docx_path = result["redacted_docx_path"]
            redaction_map_path = result["redaction_map_path"]
            qa_report          = result["qa_report"]
            classification     = result["classification"]

            logger.info(
                "Job #%s file '%s' — type: %s | QA: %d/100 | %s",
                job_id, rfile.original_filename,
                classification["doc_type"],
                qa_report["confidence_score"],
                qa_report["recommendation"],
            )

            # ------------------------------------------------------------------
            # Step 3: Persist outputs
            # ------------------------------------------------------------------
            base_name = os.path.splitext(rfile.original_filename)[0]

            # Save redacted DOCX
            redacted_dest_rel = f"redacted/{job_id}/{base_name}_redacted.docx"
            redacted_dest_abs = os.path.join(settings.MEDIA_ROOT, redacted_dest_rel)
            os.makedirs(os.path.dirname(redacted_dest_abs), exist_ok=True)
            shutil.copy2(redacted_docx_path, redacted_dest_abs)
            rfile.redacted_file = redacted_dest_rel

            # Save CSV redaction map
            map_dest_rel = f"redaction_maps/{job_id}/{base_name}_redaction_map.csv"
            map_dest_abs = os.path.join(settings.MEDIA_ROOT, map_dest_rel)
            os.makedirs(os.path.dirname(map_dest_abs), exist_ok=True)
            shutil.copy2(redaction_map_path, map_dest_abs)
            rfile.redaction_map_file = map_dest_rel

            rfile.status = RedactionFile.STATUS_COMPLETED
            rfile.save(update_fields=['redacted_file', 'redaction_map_file', 'status'])
            all_failed = False
            logger.info("Successfully processed file: %s", rfile.original_filename)

        except Exception as exc:
            logger.exception("Error processing file #%s (%s)", rfile.pk, rfile.original_filename)
            rfile.status = RedactionFile.STATUS_FAILED
            rfile.error_message = str(exc)
            rfile.save(update_fields=['status', 'error_message'])

        finally:
            if tmp_dir and os.path.isdir(tmp_dir):
                try:
                    shutil.rmtree(tmp_dir)
                except Exception:
                    pass  # best-effort cleanup

    # --------------------------------------------------------------------------
    # Update job-level status
    # --------------------------------------------------------------------------
    if all_failed and job.files.exists():
        job.status = RedactionJob.STATUS_FAILED
        job.error_message = "All files failed to process. Check individual file errors."
    else:
        job.status = RedactionJob.STATUS_COMPLETED
    job.save(update_fields=['status', 'error_message'])
    logger.info("Job #%s finished with status: %s", job_id, job.status)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _convert_pdf_to_docx(pdf_path: str, output_dir: str) -> str:
    """
    Convert a PDF file to DOCX using ConvertToDocx private methods.
    Returns the path to the produced .docx file.
    """
    from ConvertToDocx import ConvertToDocx  # noqa: PLC0415

    # We instantiate ConvertToDocx with the PDF's parent dir as input_folder
    # so that _get_and_create_relative_output_dir resolves correctly.
    pdf_dir = os.path.dirname(pdf_path)
    converter = ConvertToDocx(input_folder=pdf_dir, output_docx_folder_path=output_dir)
    converter._convert_pdf_to_docx_and_write(pdf_path)

    # Locate the produced docx
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    docx_path = os.path.join(output_dir, base + '.docx')
    if not os.path.isfile(docx_path):
        raise FileNotFoundError(
            f"PDF conversion did not produce expected file: {docx_path}"
        )
    return docx_path


def _convert_doc_to_docx(doc_path: str, output_dir: str) -> str:
    """
    Convert a legacy .doc file to .docx using LibreOffice (soffice).
    Returns the path to the produced .docx file.
    """
    import subprocess

    result = subprocess.run(
        ['soffice', '--headless', '--convert-to', 'docx', doc_path, '--outdir', output_dir],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"soffice failed to convert {doc_path}: {result.stderr}"
        )
    base = os.path.splitext(os.path.basename(doc_path))[0]
    docx_path = os.path.join(output_dir, base + '.docx')
    if not os.path.isfile(docx_path):
        raise FileNotFoundError(
            f"DOC conversion did not produce expected file: {docx_path}"
        )
    return docx_path


def _redact_docx(docx_path: str, output_dir: str, original_filename: str):
    """
    Run DataRedaction on a DOCX file.

    Returns (redacted_docx_path, redaction_map_csv_path).
    """
    from spacy_redaction import DataRedaction  # noqa: PLC0415

    # DataRedaction uses _get_and_create_relative_output_dir which needs
    # input_dir_path to compute relative paths. We pass docx's parent dir.
    docx_dir = os.path.dirname(docx_path)

    # Monkey-patch a pre-loaded nlp model so we don't reload it every call.
    nlp = _get_nlp()

    redactor = DataRedaction(
        input_dir_path=docx_dir,
        redacted_output_path=output_dir,
    )
    # Replace the freshly loaded model with our shared singleton
    redactor.nlp = nlp

    redactor.redact_one_file(docx_path)

    # Locate outputs: DataRedaction appends _redacted.docx and _redaction_map.csv
    base = os.path.splitext(os.path.basename(docx_path))[0]
    redacted_docx = os.path.join(output_dir, base + '_redacted.docx')
    redaction_map = os.path.join(output_dir, base + '_redaction_map.csv')

    if not os.path.isfile(redacted_docx):
        raise FileNotFoundError(
            f"Redaction did not produce expected file: {redacted_docx}"
        )
    if not os.path.isfile(redaction_map):
        raise FileNotFoundError(
            f"Redaction did not produce expected CSV: {redaction_map}"
        )

    return redacted_docx, redaction_map
