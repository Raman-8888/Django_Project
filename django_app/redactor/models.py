from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver


class UserProfile(models.Model):
    """Extended user profile synchronized with MongoDB."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    organization = models.CharField(max_length=150, blank=True, default='')
    role = models.CharField(max_length=100, blank=True, default='')
    bio = models.TextField(blank=True, default='')
    preferred_policy = models.CharField(
        max_length=50,
        choices=[
            ('AUTO', 'Auto-Detect (Classifier Agent)'),
            ('CV', 'CV / Resume Policy'),
            ('CONTRACT', 'Legal Contract Policy'),
            ('MEDICAL', 'Medical Record Policy'),
            ('COURT_FILING', 'Court Filing Policy'),
            ('FINANCIAL', 'Financial Document Policy'),
        ],
        default='AUTO'
    )
    avatar_color = models.CharField(max_length=20, default='#3b82f6')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.user.username} Profile ({self.organization or "Individual"})'

    @property
    def display_name(self):
        full = f"{self.user.first_name} {self.user.last_name}".strip()
        return full if full else self.user.username

    @property
    def initials(self):
        if self.user.first_name and self.user.last_name:
            return f"{self.user.first_name[0]}{self.user.last_name[0]}".upper()
        return self.user.username[:2].upper()

    def sync_to_mongo(self):
        from .mongo import sync_user_profile_to_mongo
        return sync_user_profile_to_mongo(
            user=self.user,
            organization=self.organization,
            role=self.role,
            bio=self.bio,
            preferred_policy=self.preferred_policy
        )


@receiver(post_save, sender=User)
def create_or_save_user_profile(sender, instance, created, **kwargs):
    if created:
        profile = UserProfile.objects.create(user=instance)
        try:
            profile.sync_to_mongo()
        except Exception:
            pass


class RedactionJob(models.Model):
    """Represents a batch redaction job for one or more uploaded files."""

    STATUS_PENDING = 'PENDING'
    STATUS_PROCESSING = 'PROCESSING'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_FAILED = 'FAILED'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_FAILED, 'Failed'),
    ]

    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='redaction_jobs'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    error_message = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Redaction Job'
        verbose_name_plural = 'Redaction Jobs'

    def __str__(self):
        user_str = f" by {self.user.username}" if self.user else " (Guest)"
        return f'Job #{self.pk}{user_str} [{self.status}]'

    @property
    def file_count(self):
        return self.files.count()

    @property
    def completed_count(self):
        return self.files.filter(status=RedactionFile.STATUS_COMPLETED).count()

    @property
    def failed_count(self):
        return self.files.filter(status=RedactionFile.STATUS_FAILED).count()

    @property
    def status_badge_class(self):
        return {
            self.STATUS_PENDING: 'secondary',
            self.STATUS_PROCESSING: 'warning',
            self.STATUS_COMPLETED: 'success',
            self.STATUS_FAILED: 'danger',
        }.get(self.status, 'secondary')

    @property
    def is_active(self):
        """True while job still needs page refreshes."""
        return self.status in (self.STATUS_PENDING, self.STATUS_PROCESSING)

    def sync_to_mongo(self, metadata=None):
        from .mongo import sync_job_to_mongo
        return sync_job_to_mongo(self, metadata)


class RedactionFile(models.Model):
    """Represents a single file within a RedactionJob."""

    STATUS_PENDING = 'PENDING'
    STATUS_PROCESSING = 'PROCESSING'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_FAILED = 'FAILED'
    STATUS_SKIPPED = 'SKIPPED'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_SKIPPED, 'Skipped'),
    ]

    job = models.ForeignKey(
        RedactionJob,
        on_delete=models.CASCADE,
        related_name='files',
    )
    original_file = models.FileField(upload_to='uploads/')
    original_filename = models.CharField(max_length=255)
    redacted_file = models.FileField(upload_to='redacted/', blank=True)
    redaction_map_file = models.FileField(upload_to='redaction_maps/', blank=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    error_message = models.TextField(blank=True, default='')

    class Meta:
        verbose_name = 'Redaction File'
        verbose_name_plural = 'Redaction Files'

    def __str__(self):
        return f'{self.original_filename} [{self.status}]'

    @property
    def status_badge_class(self):
        return {
            self.STATUS_PENDING: 'secondary',
            self.STATUS_PROCESSING: 'warning text-dark',
            self.STATUS_COMPLETED: 'success',
            self.STATUS_FAILED: 'danger',
            self.STATUS_SKIPPED: 'info',
        }.get(self.status, 'secondary')
