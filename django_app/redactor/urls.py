from django.urls import path
from .views import (
    IndexView,
    JobDetailView,
    JobListView,
    RegisterView,
    LoginView,
    LogoutView,
    ProfileView,
    download_file,
)

urlpatterns = [
    path('', IndexView.as_view(), name='index'),
    path('jobs/', JobListView.as_view(), name='job_list'),
    path('jobs/<int:pk>/', JobDetailView.as_view(), name='job_detail'),
    path('download/<int:file_id>/<str:file_type>/', download_file, name='download_file'),
    
    # Auth & Profile
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('profile/', ProfileView.as_view(), name='profile'),
]
