from django.db import models


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
        return f'Job #{self.pk} [{self.status}]'

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
