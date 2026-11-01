"""Application URL entry point; all API authorization lives in the service."""

from django.urls import include, path

urlpatterns = [
    path("", include("outage_explorer.service.urls")),
    path("", include("outage_explorer.frontend.urls")),
]

handler400 = "outage_explorer.service.views.bad_request"
handler404 = "outage_explorer.service.views.not_found"
handler500 = "outage_explorer.service.views.server_error"
