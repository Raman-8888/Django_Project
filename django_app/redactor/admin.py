from django.contrib import admin
from .models import RedactionJob, RedactionFile


@admin.register(RedactionJob)
class RedactionJobAdmin(admin.ModelAdmin):
    list_display = ['id', 'status', 'created_at', 'file_count']
    list_filter = ['status']
    readonly_fields = ['created_at']

    def file_count(self, obj):
        return obj.files.count()
    file_count.short_description = '# Files'


@admin.register(RedactionFile)
class RedactionFileAdmin(admin.ModelAdmin):
    list_display = ['id', 'job', 'original_filename', 'status']
    list_filter = ['status']
    raw_id_fields = ['job']
