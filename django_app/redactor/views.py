"""
Views for the Legal Document Redactor Django app.
Includes User Authentication, MongoDB Profile Sync, and Job Ownership.
"""
import logging
import mimetypes
import os
import threading

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import FileResponse, Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View

from .models import RedactionFile, RedactionJob, UserProfile
from .mongo import (
    get_user_mongo_profile,
    sync_user_profile_to_mongo,
    get_user_jobs_from_mongo,
    sync_job_to_mongo,
)
from .tasks import process_redaction_job

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {'.pdf', '.doc', '.docx'}


# ===========================================================================
# Landing / Upload View
# ===========================================================================

class IndexView(View):
    """Modern 3D Landing Page & File Upload."""

    template_name = 'redactor/index.html'

    def get(self, request):
        user_recent_jobs = []
        if request.user.is_authenticated:
            user_recent_jobs = RedactionJob.objects.filter(user=request.user).prefetch_related('files')[:5]

        # Overall platform statistics for hero counter
        total_jobs_count = RedactionJob.objects.count()
        completed_jobs_count = RedactionJob.objects.filter(status=RedactionJob.STATUS_COMPLETED).count()
        total_files_count = RedactionFile.objects.count()

        context = {
            'user_recent_jobs': user_recent_jobs,
            'stats': {
                'total_jobs': total_jobs_count,
                'completed_jobs': completed_jobs_count,
                'total_files': total_files_count,
            }
        }
        return render(request, self.template_name, context)

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

        # Create a new job, associate with logged-in user if available
        job_user = request.user if request.user.is_authenticated else None
        job = RedactionJob.objects.create(
            user=job_user,
            status=RedactionJob.STATUS_PENDING
        )

        # Create RedactionFile records for each upload
        for uploaded_file in uploaded_files:
            rfile = RedactionFile(
                job=job,
                original_filename=uploaded_file.name,
                status=RedactionFile.STATUS_PENDING,
            )
            rfile.original_file.save(uploaded_file.name, uploaded_file, save=True)

        # Sync initial pending job state to MongoDB Atlas
        try:
            job.sync_to_mongo()
        except Exception as exc:
            logger.warning("Could not sync job to mongo initially: %s", exc)

        # Kick off background processing in a daemon thread
        thread = threading.Thread(
            target=_run_job_in_thread,
            args=(job.pk,),
            daemon=True,
            name=f'redaction-job-{job.pk}',
        )
        thread.start()
        logger.info("Started background thread for job #%s (User: %s)", job.pk, job_user)

        return redirect('job_detail', pk=job.pk)


# ===========================================================================
# Job Detail & Management
# ===========================================================================

class JobDetailView(View):
    """Show the real-time status and download links for a single job."""

    template_name = 'redactor/job_detail.html'

    def get(self, request, pk):
        job = get_object_or_404(RedactionJob, pk=pk)
        files = job.files.all()
        return render(request, self.template_name, {'job': job, 'files': files})


class JobListView(View):
    """List redaction jobs with filter for 'My Jobs' vs 'All Jobs'."""

    template_name = 'redactor/job_list.html'

    def get(self, request):
        filter_mode = request.GET.get('filter', 'my' if request.user.is_authenticated else 'all')

        if filter_mode == 'my' and request.user.is_authenticated:
            jobs = RedactionJob.objects.filter(user=request.user).prefetch_related('files')
        else:
            jobs = RedactionJob.objects.prefetch_related('files').all()

        my_jobs_count = 0
        if request.user.is_authenticated:
            my_jobs_count = RedactionJob.objects.filter(user=request.user).count()

        return render(request, self.template_name, {
            'jobs': jobs,
            'filter_mode': filter_mode,
            'my_jobs_count': my_jobs_count,
            'all_jobs_count': RedactionJob.objects.count(),
        })


# ===========================================================================
# User Authentication & Profile
# ===========================================================================

class RegisterView(View):
    """Register a new user account and synchronize profile to MongoDB Atlas."""

    template_name = 'redactor/register.html'

    def get(self, request):
        if request.user.is_authenticated:
            return redirect('profile')
        return render(request, self.template_name)

    def post(self, request):
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        password_confirm = request.POST.get('password_confirm', '')
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        organization = request.POST.get('organization', '').strip()
        role = request.POST.get('role', '').strip()

        errors = []
        if not username:
            errors.append("Username is required.")
        elif User.objects.filter(username=username).exists():
            errors.append("Username is already taken.")

        if not email:
            errors.append("Email address is required.")
        elif User.objects.filter(email=email).exists():
            errors.append("An account with this email already exists.")

        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if password != password_confirm:
            errors.append("Passwords do not match.")

        if errors:
            return render(request, self.template_name, {
                'errors': errors,
                'form_data': request.POST,
            })

        # Create user
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
        )

        # Profile gets auto-created via signal; update details
        profile = getattr(user, 'profile', None)
        if not profile:
            profile = UserProfile.objects.create(user=user)

        profile.organization = organization
        profile.role = role
        profile.save()

        # Synchronize to MongoDB Atlas
        try:
            profile.sync_to_mongo()
        except Exception as exc:
            logger.warning("MongoDB user sync error: %s", exc)

        login(request, user)
        messages.success(request, f"Welcome, {user.first_name or user.username}! Your account is ready.")
        return redirect('profile')


class LoginView(View):
    """User Login View."""

    template_name = 'redactor/login.html'

    def get(self, request):
        if request.user.is_authenticated:
            return redirect('profile')
        return render(request, self.template_name)

    def post(self, request):
        username_or_email = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        user = None
        # Allow sign in with email as well
        if '@' in username_or_email:
            try:
                user_obj = User.objects.get(email=username_or_email)
                user = authenticate(request, username=user_obj.username, password=password)
            except User.DoesNotExist:
                user = None
        else:
            user = authenticate(request, username=username_or_email, password=password)

        if user is not None:
            login(request, user)
            # Sync last login to Mongo
            try:
                if hasattr(user, 'profile'):
                    user.profile.sync_to_mongo()
            except Exception:
                pass

            messages.success(request, f"Welcome back, {user.first_name or user.username}!")
            next_url = request.GET.get('next') or 'profile'
            return redirect(next_url)
        else:
            return render(request, self.template_name, {
                'error': 'Invalid username or password. Please try again.',
                'username': username_or_email,
            })


class LogoutView(View):
    """User Logout View."""

    def get(self, request):
        logout(request)
        messages.info(request, "You have been logged out.")
        return redirect('index')

    def post(self, request):
        logout(request)
        messages.info(request, "You have been logged out.")
        return redirect('index')


class ProfileView(View):
    """User Profile and personal job dashboard (MongoDB synchronized)."""

    template_name = 'redactor/profile.html'

    @method_decorator(login_required)
    def get(self, request):
        user = request.user
        profile, _ = UserProfile.objects.get_or_create(user=user)

        # Fetch personal jobs from Django ORM
        user_jobs = RedactionJob.objects.filter(user=user).prefetch_related('files').order_by('-created_at')

        # Stats
        total_jobs = user_jobs.count()
        completed_jobs = user_jobs.filter(status=RedactionJob.STATUS_COMPLETED).count()
        failed_jobs = user_jobs.filter(status=RedactionJob.STATUS_FAILED).count()
        processing_jobs = user_jobs.filter(status=RedactionJob.STATUS_PROCESSING).count()

        # Files count
        total_files = RedactionFile.objects.filter(job__user=user).count()

        # Fetch MongoDB profile mirror
        mongo_profile = {}
        try:
            mongo_profile = get_user_mongo_profile(user.id)
        except Exception:
            pass

        context = {
            'profile': profile,
            'user_jobs': user_jobs,
            'mongo_profile': mongo_profile,
            'stats': {
                'total_jobs': total_jobs,
                'completed_jobs': completed_jobs,
                'failed_jobs': failed_jobs,
                'processing_jobs': processing_jobs,
                'total_files': total_files,
                'success_rate': round((completed_jobs / total_jobs * 100) if total_jobs > 0 else 100),
            }
        }
        return render(request, self.template_name, context)

    @method_decorator(login_required)
    def post(self, request):
        user = request.user
        profile, _ = UserProfile.objects.get_or_create(user=user)

        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        organization = request.POST.get('organization', '').strip()
        role = request.POST.get('role', '').strip()
        bio = request.POST.get('bio', '').strip()
        preferred_policy = request.POST.get('preferred_policy', 'AUTO')

        # Update User
        user.first_name = first_name
        user.last_name = last_name
        user.save(update_fields=['first_name', 'last_name'])

        # Update Profile
        profile.organization = organization
        profile.role = role
        profile.bio = bio
        profile.preferred_policy = preferred_policy
        profile.save()

        # Sync to MongoDB Atlas
        try:
            profile.sync_to_mongo()
            messages.success(request, "Your profile was updated and synchronized with MongoDB Atlas!")
        except Exception as exc:
            messages.warning(request, f"Profile updated locally (MongoDB sync notice: {exc})")

        return redirect('profile')


# ===========================================================================
# File Download Endpoint
# ===========================================================================

def download_file(request, file_id, file_type):
    """
    Serve a processed file for download.
    file_type: 'redacted' → serves redacted DOCX
               'map'      → serves CSV redaction map
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


# ===========================================================================
# Thread Runner Helper
# ===========================================================================

def _run_job_in_thread(job_id: int) -> None:
    """Wrapper that calls process_redaction_job and catches top-level errors."""
    try:
        process_redaction_job(job_id)
        try:
            from .models import RedactionJob
            job = RedactionJob.objects.get(pk=job_id)
            job.sync_to_mongo()
        except Exception:
            pass
    except Exception as exc:
        logger.exception("Unhandled error in background thread for job #%s", job_id)
        try:
            from .models import RedactionJob
            job = RedactionJob.objects.get(pk=job_id)
            job.status = RedactionJob.STATUS_FAILED
            job.error_message = str(exc)
            job.save(update_fields=['status', 'error_message'])
            job.sync_to_mongo()
        except Exception:
            pass
