"""Map versioned JSON routes to service.views transport adapters.

Authentication, methods and dataset permissions are enforced by view decorators and shared
services rather than by route naming. /api/v1/openapi.json serves the single packaged contract
behind sign-in; no Swagger UI route is defined here.
"""

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
    path("api/v1/saved-views", views.saved_view_collection, name="saved-views"),
    path("api/v1/saved-views/<uuid:view_id>", views.saved_view_detail, name="saved-view"),
    path("api/v1/saved-views/<uuid:view_id>/run", views.saved_view_run, name="saved-view-run"),
    path("api/v1/refresh", views.refresh, name="refresh"),
    path("api/v1/refresh/<uuid:job_id>", views.refresh_status, name="refresh-status"),
    path("api/v1/health/live", views.live, name="live"),
    path("api/v1/health/ready", views.ready, name="ready"),
    path("api/v1/openapi.json", views.openapi, name="openapi"),
]
