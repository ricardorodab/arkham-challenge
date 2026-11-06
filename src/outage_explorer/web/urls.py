"""Mount API and frontend apps in the same Django project and origin.

service.urls maps JSON capability views; frontend.urls maps HTML/public assets. Both rely on
shared policy for protected access. Safe JSON handlers cover project 400/404/500 errors;
routing alone does not authorize datasets.
"""

from django.urls import include, path

urlpatterns = [
    path("", include("outage_explorer.service.urls")),
    path("", include("outage_explorer.frontend.urls")),
]

handler400 = "outage_explorer.service.views.bad_request"
handler404 = "outage_explorer.service.views.not_found"
handler500 = "outage_explorer.service.views.server_error"
