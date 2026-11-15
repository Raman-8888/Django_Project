from django.apps import AppConfig


class RedactorConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'redactor'
    verbose_name = 'Legal Document Redactor'

    def ready(self):
        """
        Called when Django starts. We intentionally do NOT pre-load the spaCy
        model here because:
        1. `ready()` is called during migrations, management commands, etc.
        2. The transformer model is very large (~500 MB) and slow to load.
        3. Loading it lazily inside tasks.py (once, at module level) is safer.

        The lazy singleton pattern in tasks.py handles thread-safe lazy loading.
        """
        pass
