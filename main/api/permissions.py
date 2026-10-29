"""API permission checks, driven by the same roles as the rest of the app."""
from rest_framework.permissions import BasePermission

from .. import permissions as perms


class RolePermission(BasePermission):
    """The user's role must grant the view's `required_permission`.

    A view without one is refused, so forgetting to set it fails closed.
    """

    message = "Your role doesn't include API access to this."

    def has_permission(self, request, view):
        code = getattr(view, 'required_permission', None)
        if code is None:
            return False
        return perms.has_required(request.user, code)


# A view's required_permission when any one API permission will do.
ANY_API = perms.ANY_API


class HasAnyApiPermission(BasePermission):
    """For the docs pages, which a logged-in browser user opens."""

    def has_permission(self, request, view):
        return perms.has_any_api_perm(request.user)
