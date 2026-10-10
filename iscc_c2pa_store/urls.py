"""URL routes: the API (django-ninja), the operator dashboard, and the pages for people."""

from django.contrib import admin
from django.urls import path

from iscc_c2pa_store import views
from iscc_c2pa_store.api import api

handler404 = "iscc_c2pa_store.views.not_found"

urlpatterns = [
    path("", views.index, name="index"),
    path("docs", views.docs, name="docs"),
    path("openapi/<str:name>", views.openapi, name="openapi"),
    path("healthz", views.healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("", api.urls),
]
