from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token

urlpatterns = [
    path("", include("inspection.web_urls")),
    path("admin/", admin.site.urls),
    path("api/auth/token/", obtain_auth_token, name="api-token"),
    path("api/", include("inspection.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
