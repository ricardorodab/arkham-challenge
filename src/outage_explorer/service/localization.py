"""Localize authored interface messages while preserving operational and source values.

Stored jobs, dataset identifiers, user content and analytical values remain canonical.
Translation is applied only to explicitly selected presentation fields at response time.
"""

from __future__ import annotations

import re
from copy import deepcopy

from django.middleware.locale import LocaleMiddleware
from django.utils.cache import patch_vary_headers
from django.utils.translation import gettext


class InterfaceLocaleMiddleware(LocaleMiddleware):
    """Negotiate the supported UI languages without varying locale-neutral assets."""

    def process_response(self, request, response):
        """Expose the selected locale and vary private responses by its input sources."""
        if getattr(response, "_outage_public_asset", False):
            return response
        response = super().process_response(request, response)
        patch_vary_headers(response, ("Cookie", "Accept-Language"))
        return response


# These four authored validation templates are the only dynamic API detail forms.
# Restrict the captured field to a protocol identifier; never infer translations
# from arbitrary user names, table cells, SQL, filesystem paths or credentials.
_VALIDATION_TEMPLATES = (
    "{name} must be an ISO calendar date.",
    "{name} must be an integer in its allowed range.",
    "{name} is invalid.",
    "{name} must contain at most 150 characters.",
)
_VALIDATION_PATTERNS = tuple(
    (
        re.compile(
            re.escape(message).replace(re.escape("{name}"), r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)")
        ),
        message,
    )
    for message in _VALIDATION_TEMPLATES
)


def public_message(message: str) -> str:
    """Translate a safe authored message, retaining dynamic protocol field names."""
    translated = gettext(message)
    if translated != message:
        return translated
    for pattern, template in _VALIDATION_PATTERNS:
        match = pattern.fullmatch(message)
        if match:
            return gettext(template).format(name=match["name"])
    return message


def catalog_presentation(catalog: dict) -> dict:
    """Translate metadata prose without mutating manifests, keys or numerical data."""
    result = deepcopy(catalog)
    for dataset in result.get("datasets", []):
        if isinstance(dataset.get("description"), str):
            dataset["description"] = gettext(dataset["description"])
    result["warnings"] = [public_message(message) for message in result.get("warnings", [])]
    for metric in result.get("metrics", {}).values():
        for name in (
            "description",
            "definition",
            "label",
            "note",
            "source_grain",
            "denominator_policy",
            "rounding",
            "outlier_policy",
        ):
            if isinstance(metric.get(name), str):
                metric[name] = gettext(metric[name])
    return result
