from django.urls import path

from . import views

urlpatterns = [
    path("api/v1/auth/csrf", views.csrf, name="csrf"),
    path("api/v1/auth/login", views.login, name="login"),
    path("api/v1/auth/logout", views.logout, name="logout"),
    path("api/v1/auth/session", views.session, name="session"),
    path("api/v1/catalog", views.catalog, name="catalog"),
    path("api/v1/data/<slug:dataset_id>", views.data, name="data"),
    path("api/v1/query", views.query, name="query"),
    path("api/v1/refresh", views.refresh, name="refresh"),
    path("api/v1/refresh/<uuid:job_id>", views.refresh_status, name="refresh-status"),
    path("api/v1/health/live", views.live, name="live"),
    path("api/v1/health/ready", views.ready, name="ready"),
    path("api/v1/openapi.json", views.openapi, name="openapi"),
]
