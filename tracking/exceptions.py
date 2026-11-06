from rest_framework.exceptions import ValidationError
from rest_framework.views import exception_handler as drf_exception_handler


def _flatten(data, prefix=""):
    """{'email': ['Enter a valid email.'], 'items': [{'amount': ['...']}]} -> ['email: Enter a valid email.', ...]"""
    out = []
    if isinstance(data, dict):
        for k, v in data.items():
            key = "" if k == "non_field_errors" else (f"{prefix} › {k}" if prefix else str(k))
            out += _flatten(v, key)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            out += _flatten(v, f"{prefix} › {i + 1}" if isinstance(v, (dict, list)) and prefix else prefix)
    else:
        out.append(f"{prefix}: {data}" if prefix else str(data))
    return out


def exception_handler(exc, context):
    """Return every error as {"detail": "<human readable string>"} – the shape the React client expects."""
    response = drf_exception_handler(exc, context)
    if response is None:
        return None
    if isinstance(exc, ValidationError):
        response.data = {"detail": "\n".join(_flatten(response.data)) or "Invalid input.", "errors": response.data}
    elif isinstance(response.data, dict) and "detail" in response.data:
        response.data = {"detail": str(response.data["detail"])}
    return response
