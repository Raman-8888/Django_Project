"""
Views for the Legal Document Redactor Django app.
"""
import logging
import mimetypes
import os
import threading

from django.http import FileResponse, Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from .models import RedactionFile, RedactionJob
from .tasks import process_redaction_job

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {'.pdf', '.doc', '.docx'}


class IndexView(View):
    """Home page: file upload form."""

    template_name = 'redactor/index.html'

    def get(self, request):
        return render(request, self.template_name)

    def post(self, request):
        uploaded_files = request.FILES.getlist('documents')

        if not uploaded_files:
            return render(
                request,
                self.template_name,
                {'error': 'Please select at least one file to upload.'},
            )

        # Validate file extensions
        invalid_files = []
        for f in uploaded_files:
            ext = os.path.splitext(f.name)[1].lower()
            if ext not in ALLOWED_EXTENSIONS:
                invalid_files.append(f.name)

        if invalid_files:
            return render(
                request,
                self.template_name,
                {
                    'error': (
                        f"Unsupported file type(s): {', '.join(invalid_files)}. "
                        "Only PDF, DOC, and DOCX files are accepted."
                    )
                },
            )

        # Create a new job
        job = RedactionJob.objects.create(status=RedactionJob.STATUS_PENDING)

        # Create RedactionFile records for each upload
        for uploaded_file in uploaded_files:
            rfile = RedactionFile(
                job=job,
                original_filename=uploaded_file.name,
                status=RedactionFile.STATUS_PENDING,
            )
            rfile.original_file.save(uploaded_file.name, uploaded_file, save=True)

        # Kick off background processing in a daemon thread
        thread = threading.Thread(
            target=_run_job_in_thread,
            args=(job.pk,),
            daemon=True,
            name=f'redaction-job-{job.pk}',
        )
        thread.start()
        logger.info("Started background thread for job #%s", job.pk)

        return redirect('job_detail', pk=job.pk)


class JobDetailView(View):
    """Show the status and download links for a single job."""

    template_name = 'redactor/job_detail.html'

    def get(self, request, pk):
        job = get_object_or_404(RedactionJob, pk=pk)
        files = job.files.all()
        return render(request, self.template_name, {'job': job, 'files': files})


class JobListView(View):
    """List all redaction jobs."""

    template_name = 'redactor/job_list.html'

    def get(self, request):
        jobs = RedactionJob.objects.prefetch_related('files').all()
        return render(request, self.template_name, {'jobs': jobs})


def download_file(request, file_id, file_type):
    """
    Serve a processed file for download.

    file_type: 'redacted'  → serves the redacted DOCX
               'map'       → serves the CSV redaction map
    """
    rfile = get_object_or_404(RedactionFile, pk=file_id)

    if file_type == 'redacted':
        field = rfile.redacted_file
        if not field or not field.name:
            raise Http404("Redacted file not available yet.")
        file_path = field.path
        base = os.path.splitext(rfile.original_filename)[0]
        download_name = f"{base}_redacted.docx"
        content_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

    elif file_type == 'map':
        field = rfile.redaction_map_file
        if not field or not field.name:
            raise Http404("Redaction map not available yet.")
        file_path = field.path
        base = os.path.splitext(rfile.original_filename)[0]
        download_name = f"{base}_redaction_map.csv"
        content_type = 'text/csv'

    else:
        return HttpResponseBadRequest("Invalid file_type. Use 'redacted' or 'map'.")

    if not os.path.isfile(file_path):
        raise Http404("File not found on disk.")

    response = FileResponse(
        open(file_path, 'rb'),
        content_type=content_type,
        as_attachment=True,
        filename=download_name,
    )
    return response


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run_job_in_thread(job_id: int) -> None:
    """Wrapper that calls process_redaction_job and catches top-level errors."""
    try:
        process_redaction_job(job_id)
    except Exception as exc:
        logger.exception("Unhandled error in background thread for job #%s", job_id)
        # Mark job as FAILED so the UI doesn't spin forever
        try:
            from .models import RedactionJob
            job = RedactionJob.objects.get(pk=job_id)
            job.status = RedactionJob.STATUS_FAILED
            job.error_message = str(exc)
            job.save(update_fields=['status', 'error_message'])
        except Exception:
            pass
