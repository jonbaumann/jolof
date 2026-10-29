from rest_framework.throttling import SimpleRateThrottle


class LoginThrottle(SimpleRateThrottle):
    """Caps login attempts per username.

    Keyed on the account rather than the address, because the address comes
    from a header a client can set to anything, and guessing one account's
    password is the attack worth slowing down. Counts live in the default
    cache, which is per process.
    """

    scope = 'api-login'

    def get_cache_key(self, request, view):
        username = request.data.get('username') if hasattr(request.data, 'get') else None
        if not isinstance(username, str) or not username:
            return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}
        return self.cache_format % {'scope': self.scope, 'ident': username.strip().lower()}
