"""The IANA timezone list, for the per-user timezone preference.

zoneinfo reads whatever tz database the host provides, falling back to the
`tzdata` package (see requirements.txt) when the OS ships none -- which is the
usual case in a slim container image.
"""
import zoneinfo

from django.conf import settings

# The list is fixed for the life of the process, and building the grouped form
# is a few hundred string operations, so both are done once on import.
NAMES = sorted(zoneinfo.available_timezones())
_NAME_SET = set(NAMES)


def is_valid(name):
    """True if `name` is a timezone this machine knows about."""
    return bool(name) and name in _NAME_SET


def _label(name):
    """"America/Argentina/Buenos_Aires" -> "Argentina/Buenos Aires"."""
    _, _, rest = name.partition('/')
    return (rest or name).replace('_', ' ')


def grouped_choices():
    """IANA names as <select> choices, grouped by region.

    The blank first choice means "whatever the site is set to", so a user who
    never picks one follows settings.TIME_ZONE if it ever changes.
    """
    regions = {}
    for name in NAMES:
        region, sep, _ = name.partition('/')
        # UTC, GMT, and the handful of other bare names have no region.
        regions.setdefault(region if sep else 'Other', []).append((name, _label(name)))

    choices = [('', f'Site default ({settings.TIME_ZONE})')]
    for region in sorted(regions):
        choices.append((region, regions[region]))
    return choices
