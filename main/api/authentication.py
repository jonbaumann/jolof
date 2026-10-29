"""Session tokens: how API clients prove who they are.

A client logs in once with a username and password (views.Login) and gets a
token back. It sends that token on every request after, as
``Authorization: Bearer <token>``, for as long as the session stays in use:
until it goes unused for the idle timeout (ApiSettings) or the client logs out.
"""
import hashlib
import secrets
from datetime import timedelta

from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header

from ..models import ApiSession, ApiSettings

TOKEN_PREFIX = 'jlf_'

# last_used_at is only written when it's at least this stale, so a busy client
# doesn't cost a database write per request. Idle timeouts are in minutes, so
# the difference never shows.
TOUCH_INTERVAL = timedelta(seconds=60)


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def client_ip(request):
    """Where a login came from, for showing on the session lists only.

    Taken from X-Forwarded-For when the proxy sets it, which a client can also
    forge, so it's never used to decide anything.
    """
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (forwarded.split(',')[0].strip() or request.META.get('REMOTE_ADDR')) or None


def create_session(user, request, client_name=''):
    """Start a session for `user`. Returns (session, token); the token is never stored."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    session = ApiSession.objects.create(
        user=user,
        token_hash=hash_token(token),
        token_prefix=token[:len(TOKEN_PREFIX) + 4],
        auth_hash=user.get_session_auth_hash(),
        client_name=(client_name or '')[:100],
        ip_address=client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:255],
    )
    return session, token


class SessionTokenAuthentication(BaseAuthentication):
    keyword = 'Bearer'

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != self.keyword.lower().encode():
            # No token at all: anonymous, and the permission check turns that away.
            return None
        if len(parts) != 2:
            raise exceptions.AuthenticationFailed(
                'The Authorization header should be "Bearer <token>".', code='invalid_token')
        try:
            token = parts[1].decode()
        except UnicodeError:
            raise exceptions.AuthenticationFailed('That token is not valid.', code='invalid_token')

        session = (ApiSession.objects.select_related('user')
                   .filter(token_hash=hash_token(token)).first())
        if session is None:
            raise exceptions.AuthenticationFailed(
                'That token is not valid, or has been revoked.', code='invalid_token')

        user = session.user
        now = timezone.now()
        if not user.is_active:
            session.delete()
            raise exceptions.AuthenticationFailed('This account is deactivated.', code='invalid_token')
        # The auth hash follows the password, so a changed password ends every
        # token the old one handed out -- wherever it was changed from.
        if not secrets.compare_digest(session.auth_hash, user.get_session_auth_hash()):
            session.delete()
            raise exceptions.AuthenticationFailed(
                'The password has changed since this token was issued. Log in again.',
                code='session_expired')
        if session.is_expired(ApiSettings.load(), now=now):
            session.delete()
            raise exceptions.AuthenticationFailed(
                'This session has expired. Log in again.', code='session_expired')

        if now - session.last_used_at >= TOUCH_INTERVAL:
            ApiSession.objects.filter(pk=session.pk).update(last_used_at=now)
            session.last_used_at = now
        return user, session

    def authenticate_header(self, request):
        # Makes DRF answer a missing or bad token with 401 rather than 403.
        return self.keyword


class SessionTokenScheme(OpenApiAuthenticationExtension):
    """Describes the token to the OpenAPI schema, which gives the docs page its Authorize button."""

    target_class = SessionTokenAuthentication
    name = 'sessionToken'

    def get_security_definition(self, auto_schema):
        return {
            'type': 'http',
            'scheme': 'bearer',
            'description': 'A session token from auth/login/.',
        }
