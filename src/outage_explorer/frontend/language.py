"""Change the noncredential UI preference through a CSRF-protected same-origin POST."""

from django.conf import settings
from django.views.decorators.http import require_POST
from django.views.i18n import set_language

from outage_explorer.service.errors import APIError, problem


@require_POST
def change_language(request):
    """Set only a supported language and retain Django's safe redirect validation."""
    if request.POST.get("language") not in dict(settings.LANGUAGES):
        return problem(request, APIError(400, "invalid_language", "Choose English or Spanish."))
    return set_language(request)
