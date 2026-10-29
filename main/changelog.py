"""Parse CHANGELOG.md into structured entries for the in-app "What's New" popup.

The changelog lives at the project root and is version-tracked. Each release is a
``## <version>`` (or ``## <version> — <title>``) section followed by ``- `` bullets.
"""
import functools
import os
import re

from django.conf import settings

CHANGELOG_PATH = settings.BASE_DIR / 'CHANGELOG.md'

# Matches "1.8.5" optionally followed by a "— Title" / "- Title" / "– Title".
_HEADER_RE = re.compile(r'^([0-9][0-9.]*)\s*(?:[—–-]\s*(.*))?$')


def _version_tuple(version):
    """Turn "1.8.5" into (1, 8, 5) so versions sort/compare numerically."""
    parts = []
    for chunk in str(version).split('.'):
        match = re.match(r'\d+', chunk)
        parts.append(int(match.group()) if match else 0)
    return tuple(parts)


@functools.lru_cache(maxsize=8)
def _parse(path, _mtime):
    """Parse the changelog. Cached on (path, mtime) so edits are picked up live."""
    entries = []
    current = None
    try:
        with open(path, encoding='utf-8') as handle:
            lines = handle.readlines()
    except OSError:
        return []

    for raw in lines:
        line = raw.rstrip()
        if line.startswith('## '):
            header = line[3:].strip()
            match = _HEADER_RE.match(header)
            if match:
                version, title = match.group(1), (match.group(2) or '').strip()
            else:
                version, title = header, ''
            current = {'version': version, 'title': title, 'items': []}
            entries.append(current)
        elif current is not None and line.lstrip().startswith('- '):
            current['items'].append(line.lstrip()[2:].strip())

    # Drop any malformed sections that ended up with no bullets.
    return [entry for entry in entries if entry['items']]


def get_changelog():
    try:
        mtime = os.path.getmtime(CHANGELOG_PATH)
    except OSError:
        return []
    return _parse(str(CHANGELOG_PATH), mtime)


def get_whats_new(last_seen_version, current_version):
    """Return the changelog entries a user with ``last_seen_version`` hasn't seen yet.

    - No record yet (new user / first visit after this feature ships): show only the
      single latest release, not the entire history.
    - Otherwise: show every release newer than what they last acknowledged, capped at
      the currently deployed version.
    """
    entries = get_changelog()
    if not entries:
        return []

    current_t = _version_tuple(current_version)
    visible = [e for e in entries if _version_tuple(e['version']) <= current_t]
    if not visible:
        return []

    if not last_seen_version:
        return visible[:1]

    last_t = _version_tuple(last_seen_version)
    return [e for e in visible if _version_tuple(e['version']) > last_t]
