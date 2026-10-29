"""Root URL configuration: the admin, the REST API, and the app itself."""
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/', include('main.api.urls')),
    path('', include('main.urls')),
    path('', include('django.contrib.auth.urls')),
]
