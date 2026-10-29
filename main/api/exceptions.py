"""Every API error has the same shape:

    {"error": {"code": "not_found", "message": "Not found."}}

with "fields" added for validation errors: {field: [messages]}.
"""
from django.core.exceptions import PermissionDenied
from django.http import Http404
from rest_framework import exceptions
from rest_framework.views import exception_handler as drf_exception_handler


def api_error(code, message, fields=None):
    error = {'code': code, 'message': message}
    if fields:
        error['fields'] = fields
    return {'error': error}


def exception_handler(exc, context):
    # Django's own exceptions, swapped for DRF's the way DRF does, so they
    # carry a code too.
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, PermissionDenied):
        exc = exceptions.PermissionDenied()
    response = drf_exception_handler(exc, context)
    if response is None:
        # Not an API error DRF knows: let Django's 500 handling take it.
        return None

    if isinstance(exc, exceptions.ValidationError):
        detail = exc.detail
        if isinstance(detail, dict):
            fields = {name: [str(m) for m in (msgs if isinstance(msgs, list) else [msgs])]
                      for name, msgs in detail.items()}
            first = next(iter(fields.values()), ['Invalid request.'])[0]
            response.data = api_error('invalid', first, fields)
        else:
            messages = [str(m) for m in (detail if isinstance(detail, list) else [detail])]
            response.data = api_error('invalid', ' '.join(messages))
        return response

    detail = getattr(exc, 'detail', None)
    code = getattr(detail, 'code', None) or getattr(exc, 'default_code', 'error')
    response.data = api_error(code, str(detail) if detail is not None else str(exc))
    return response
