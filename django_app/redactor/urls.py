from django.urls import path
from .views import IndexView, JobDetailView, JobListView, download_file

urlpatterns = [
    path('', IndexView.as_view(), name='index'),
    path('jobs/', JobListView.as_view(), name='job_list'),
    path('jobs/<int:pk>/', JobDetailView.as_view(), name='job_detail'),
    path('download/<int:file_id>/<str:file_type>/', download_file, name='download_file'),
]
