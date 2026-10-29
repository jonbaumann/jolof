"""Template context processors injected into every render."""
from django.conf import settings

from .changelog import get_whats_new
from .models import UserProfile


def app_version(request):
    """Expose the app version on every page, for the version footer in base.html."""
    return {'app_version': settings.VERSION}


def whats_new(request):
    """Expose unseen changelog entries so base.html can show the "What's New" popup."""
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {}

    profile, _ = UserProfile.objects.get_or_create(user=user)
    entries = get_whats_new(profile.last_seen_version, settings.VERSION)
    if not entries:
        return {}

    return {
        'whats_new_entries': entries,
        'whats_new_version': settings.VERSION,
    }


def permissions(request):
    """Expose `can` so templates can ask e.g. {% if can.recipe_read_write %}.

    Lazy: nothing is looked up unless a template actually asks.
    """
    from .permissions import Can
    return {'can': Can(getattr(request, 'user', None))}


def theme(request):
    """The user's theme preference, for the script in base.html's <head>.

    None when logged out (the browser's last-used theme applies); blank when
    logged in but not chosen yet.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {'theme_preference': None}
    preference = UserProfile.objects.filter(user=user).values_list('theme', flat=True).first()
    return {'theme_preference': preference or ''}
