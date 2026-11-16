from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("", include("claims.urls")),
]

# Uploaded documents are served only through the permission-checked
# `claims:document` view, never directly from MEDIA_URL.
