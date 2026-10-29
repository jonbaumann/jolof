"""Request middleware that isn't tied to one feature.

(The audit log's actor/collector middleware lives in main/audit.py, next to the
signal handlers it feeds.)
"""
import zoneinfo

from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone

from . import timezones
from .models import UserProfile


class TimezoneMiddleware:
    """Render every timestamp in the timezone the user picked.

    Activating here is what makes it apply everywhere: Django converts aware
    datetimes to the active timezone in templates, and timezone.localtime()
    picks it up too, so the CSV export follows without knowing about any of it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        name = self._user_timezone(request)
        if name:
            timezone.activate(zoneinfo.ZoneInfo(name))
        else:
            # No preference: fall back to settings.TIME_ZONE.
            timezone.deactivate()
        try:
            return self.get_response(request)
        finally:
            # Threads are reused between requests, so an activated timezone
            # would otherwise leak into whoever is served next.
            timezone.deactivate()

    @staticmethod
    def _user_timezone(request):
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return ''
        name = (
            UserProfile.objects.filter(user=user)
            .values_list('timezone', flat=True)
            .first()
        )
        # A name the tz database no longer has (renamed or dropped between
        # releases) falls back rather than raising on every page.
        return name if timezones.is_valid(name) else ''


class ForcePasswordChangeMiddleware:
    """Hold a user on the change-password page until they've picked their own.

    Set when someone with user management sets a user's password and ticks
    "must change at next login". Everything else redirects there; the page
    itself and logging out stay reachable. (Static files and images come from
    S3, not through Django, so they never reach here.)
    """

    ALLOWED_URL_NAMES = {'password-change', 'password-change-done', 'logout'}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # The API authenticates with its own tokens, never the browser session,
        # so whatever the cookie's user owes doesn't apply there.
        if request.path_info.startswith('/api/'):
            return self.get_response(request)
        if self._must_change(request) and not self._allowed(request):
            url = reverse('password-change')
            if getattr(request, 'htmx', False):
                # A plain redirect would be followed by htmx and swapped into
                # whatever fragment asked; make it a full page load instead.
                response = HttpResponse()
                response['HX-Redirect'] = url
                return response
            return redirect(url)
        return self.get_response(request)

    @staticmethod
    def _must_change(request):
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return False
        return UserProfile.objects.filter(user=user, must_change_password=True).exists()

    def _allowed(self, request):
        try:
            return resolve(request.path_info).url_name in self.ALLOWED_URL_NAMES
        except Resolver404:
            return False
