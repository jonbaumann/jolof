"""Reading recipes in from a CSV.

Reads the columns defined in csv_fields, so a file produced by the export tool
round-trips back in. Everything here is deliberately tolerant of a file that
has been through a spreadsheet: a byte-order mark, columns in any order, extra
columns we don't know about, and headers written as the friendly label rather
than the raw key are all fine.

The import is a two-step flow. Step one reads the file and works out what its
columns are (`read_file` then `analyse`); step two takes the user's decisions
about the columns we couldn't place and does the work (`resolve` then
`import_rows`). The uploaded file waits between the two steps in a temp file --
see `stash`.
"""

import csv
import io
import os
import re
import secrets
import tempfile
import time

from django.db import transaction

from . import csv_fields
from .models import Recipe, RecipeSlugHistory


# What to do with a row whose recipe is already in the database.
SKIP = 'skip'
OVERWRITE = 'overwrite'
DUPLICATE_AS_NEW = 'duplicate'

DUPLICATE_CHOICES = [
    (SKIP, 'Skip it and leave the existing recipe untouched'),
    (OVERWRITE, 'Overwrite the existing recipe with the row from the file'),
    (DUPLICATE_AS_NEW, 'Import it anyway, as a second copy'),
]

# Blanking these on an overwrite would leave the recipe unusable, and a blank
# cell far more often means "this column wasn't filled in" than "delete what's
# there". Every other selected column is applied blank-and-all.
NEVER_BLANKED = {'name', 'ingredients', 'instructions'}

# Longest sample cell we show back to the user when asking about a column.
SAMPLE_LENGTH = 60

# Columns that are read but never written: they identify which recipe a row is,
# and find_existing() uses them whether or not anything is being imported into
# them. They aren't importable fields, so they're never offered as checkboxes.
MATCH_KEYS = ('id', 'slug')


# --------------------------------------------------------------------------
# Holding the upload between the two steps
# --------------------------------------------------------------------------

STASH_DIR = os.path.join(tempfile.gettempdir(), 'jolof-csv-imports')

# How long a half-finished import is allowed to keep its file around.
STASH_MAX_AGE_SECONDS = 60 * 60

TOKEN_PATTERN = re.compile(r'^[0-9a-f]{32}$')


def stash(uploaded_file):
    """Park an upload on disk for the second step. Returns its token.

    The token alone isn't authority to read the file back -- the view keeps it
    in the user's session, and `stashed` refuses anything that isn't a plain
    32-character token so a crafted one can't walk out of the directory.
    """
    os.makedirs(STASH_DIR, exist_ok=True)
    token = secrets.token_hex(16)
    with open(os.path.join(STASH_DIR, token), 'wb') as handle:
        for chunk in uploaded_file.chunks():
            handle.write(chunk)
    return token


def stashed(token):
    """The bytes of a stashed upload, or None if it's gone or the token is junk."""
    if not token or not TOKEN_PATTERN.match(token):
        return None
    path = os.path.join(STASH_DIR, token)
    try:
        with open(path, 'rb') as handle:
            return handle.read()
    except OSError:
        return None


def discard(token):
    if token and TOKEN_PATTERN.match(token):
        try:
            os.remove(os.path.join(STASH_DIR, token))
        except OSError:
            pass


def sweep():
    """Bin stashed files from imports nobody came back to finish."""
    cutoff = time.time() - STASH_MAX_AGE_SECONDS
    try:
        names = os.listdir(STASH_DIR)
    except OSError:
        return
    for name in names:
        path = os.path.join(STASH_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Step one: what's in this file?
# --------------------------------------------------------------------------

class FileAnalysis:
    """What we could work out about an uploaded file, before the user helps.

    columns   -- every header in the file, in order, as spelled there.
    detected  -- {header: field key} for the ones we recognised ourselves.
    unknown   -- headers we couldn't place, in file order.
    samples   -- {header: first non-empty cell}, to help the user recognise a column.
    row_count -- data rows, excluding the header.
    """

    def __init__(self, columns, detected, unknown, samples, row_count):
        self.columns = columns
        self.detected = detected
        self.unknown = unknown
        self.samples = samples
        self.row_count = row_count

    @property
    def detected_keys(self):
        return list(self.detected.values())

    def _detected_pairs(self, test):
        return [
            (header, csv_fields.FIELDS_BY_KEY[self.detected[header]])
            for header in self.columns
            if header in self.detected and test(csv_fields.FIELDS_BY_KEY[self.detected[header]])
        ]

    def importable_columns(self):
        """Detected columns the user can choose to import, in file order."""
        return self._detected_pairs(lambda field: bool(field.parse))

    def match_columns(self):
        """Detected id/slug columns -- used to find recipes, never written to."""
        return self._detected_pairs(lambda field: field.key in MATCH_KEYS)

    def unusable_columns(self):
        """Recognised, but nothing can be done with them (timestamps, image URL)."""
        return self._detected_pairs(
            lambda field: not field.parse and field.key not in MATCH_KEYS
        )

    def unplaced_fields(self):
        """Importable fields the file doesn't already have a column for.

        These are what an unrecognised column can be mapped onto -- there's no
        sense offering a field that's already coming from somewhere.
        """
        taken = set(self.detected.values())
        return [
            csv_fields.FIELDS_BY_KEY[key]
            for key in csv_fields.IMPORT_KEYS
            if key not in taken
        ]


def read_file(data):
    """(columns, rows) from raw CSV bytes, rows keyed by header as spelled.

    Raises ValueError if the file isn't readable as CSV at all.
    """
    try:
        # utf-8-sig strips the byte-order mark our own export writes, which
        # would otherwise glue itself to the first column name.
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError(
            "That file isn't UTF-8 text. If it came out of Excel, re-save it "
            'as "CSV UTF-8".'
        )

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("That file is empty -- there's no header row to read.")

    columns = []
    for header in reader.fieldnames:
        if header is None:
            continue
        header = header.strip()
        # A file with the same column twice: keep the first, ignore the rest.
        if header and header not in columns:
            columns.append(header)

    if not columns:
        raise ValueError("That file's header row is blank, so its columns have no names.")

    rows = [
        {header.strip(): (value or '') for header, value in row.items() if header}
        for row in reader
    ]
    return columns, rows


def analyse(columns, rows):
    """Work out which columns we recognise, and grab a sample of each."""
    detected = {}
    unknown = []
    for header in columns:
        key = csv_fields.HEADER_ALIASES.get(header.lower())
        # Two columns claiming the same field: the first wins, the second
        # becomes something for the user to place or ignore.
        if key and key not in detected.values():
            detected[header] = key
        else:
            unknown.append(header)

    samples = {}
    for header in columns:
        for row in rows:
            value = (row.get(header) or '').strip()
            if value:
                samples[header] = (
                    value[:SAMPLE_LENGTH] + '...' if len(value) > SAMPLE_LENGTH else value
                )
                break

    return FileAnalysis(columns, detected, unknown, samples, len(rows))


# --------------------------------------------------------------------------
# Step two: apply the user's decisions
# --------------------------------------------------------------------------

def resolve(analysis, mapping):
    """Combine what we detected with what the user mapped by hand.

    Returns (headers, errors): headers is {header: field key} for everything
    that should be read, errors is a list of user-facing problems that should
    send them back to the mapping page.
    """
    # Only columns we can actually do something with: fields we can import,
    # plus the id/slug we match on. A recognised-but-unusable column (a
    # timestamp, an image URL) is left out here so it's reported as ignored.
    headers = {
        header: key for header, key in analysis.detected.items()
        if csv_fields.FIELDS_BY_KEY[key].parse or key in MATCH_KEYS
    }
    errors = []

    claimed = {}
    for header, key in mapping.items():
        if not key:
            continue  # "Ignore this column"
        if header not in analysis.unknown:
            # Not reachable from our own form; a hand-crafted post.
            errors.append(f'"{header}" isn\'t a column we asked you to map.')
            continue
        if key not in csv_fields.FIELDS_BY_KEY or not csv_fields.FIELDS_BY_KEY[key].parse:
            errors.append(f'"{header}" was mapped to something that isn\'t an importable field.')
            continue
        if key in analysis.detected.values():
            label = csv_fields.FIELDS_BY_KEY[key].label
            errors.append(
                f'"{header}" was mapped to {label}, but the file already has a {label} column.'
            )
            continue
        if key in claimed:
            label = csv_fields.FIELDS_BY_KEY[key].label
            errors.append(
                f'"{header}" and "{claimed[key]}" were both mapped to {label} '
                '-- pick one of them.'
            )
            continue
        claimed[key] = header
        headers[header] = key

    if 'name' not in headers.values():
        errors.append(
            "Nothing in this file gives the recipe's name. Map one of the "
            'unrecognised columns to Name, or import a file that has a name column.'
        )

    return headers, errors


def find_existing(row):
    """The recipe this row is already in the database as, if any.

    Tried most to least precise: an id column points at exactly one recipe, a
    slug is unique and survives renames via the redirect history, and failing
    both, a name match is what a person would call the same recipe.
    """
    raw_id = row.get('id', '').strip()
    if raw_id:
        try:
            existing = Recipe.objects.filter(pk=int(float(raw_id))).first()
        except ValueError:
            existing = None
        if existing:
            return existing

    slug = row.get('slug', '').strip()
    if slug:
        existing = Recipe.objects.filter(slug=slug).first()
        if existing:
            return existing
        retired = RecipeSlugHistory.objects.filter(slug=slug).select_related('recipe').first()
        if retired:
            return retired.recipe

    # The cell exactly as written first, then tidied. A stored name can carry
    # odd whitespace -- a scraped recipe with a trailing non-breaking space,
    # say -- and the export writes it back out verbatim. Matching only on the
    # stripped value means such a recipe fails to recognise its own exported
    # row and gets imported again as a duplicate. (SQL TRIM() is no help: it
    # strips ASCII spaces, not U+00A0.)
    raw_name = row.get('name', '')
    for candidate in (raw_name, raw_name.strip()):
        if candidate:
            match = Recipe.objects.filter(name__iexact=candidate).first()
            if match:
                return match
        if raw_name == raw_name.strip():
            break

    return None


def _apply(recipe, fields, row):
    """Set the selected columns on a recipe. Returns work to do after saving."""
    deferred = []
    for field in fields:
        if field.key not in row:
            continue
        raw = row[field.key]
        if not raw.strip() and field.key in NEVER_BLANKED:
            continue
        value = field.parse(raw)
        if field.assign:
            deferred.append((field, value))
        else:
            setattr(recipe, field.key, value)
    return deferred


class ImportResult:
    """Row-by-row tally, rendered by partials/csv_import_result.html."""

    def __init__(self):
        self.created = 0
        self.updated = 0
        self.skipped = []
        self.errors = []
        self.ignored_columns = []

    @property
    def total(self):
        return self.created + self.updated


def import_rows(rows, headers, keys, on_duplicate, columns=(), progress=None):
    """Import every row, reading `headers` columns and applying `keys` fields.

    `progress`, if given, is called with (rows done, rows total) after every
    row -- including the ones that were skipped or failed, so it always
    reaches the total.
    """
    by_key = {key: header for header, key in headers.items()}

    fields = [
        field for field in csv_fields.ordered_fields(keys)
        if field.parse and field.key in by_key
    ]

    result = ImportResult()
    result.ignored_columns = [header for header in columns if header not in headers]

    total = len(rows)

    for index, raw_row in enumerate(rows):
        number = index + 2  # row 1 is the header, so the first recipe is row 2

        # try/finally rather than a call at the end of the loop: the body is
        # full of `continue`s, and progress has to count those rows too or the
        # bar stalls on a file with skips in it.
        try:
            # Re-key the row from header spellings to field keys, so everything
            # downstream can talk in fields rather than in whatever the file said.
            row = {key: raw_row.get(header, '') for key, header in by_key.items()}

            name = row.get('name', '').strip()
            if not name:
                result.errors.append(f'Row {number}: no name, skipped.')
                continue

            existing = find_existing(row)
            if existing and on_duplicate == SKIP:
                result.skipped.append(f'Row {number}: "{existing.name}" is already in your recipes.')
                continue

            recipe = existing if (existing and on_duplicate == OVERWRITE) else Recipe()
            is_new = recipe.pk is None

            try:
                with transaction.atomic():
                    # A new recipe needs a name whether or not the Name checkbox
                    # was ticked -- unticking it means "don't rename the recipes I
                    # already have", not "create nameless ones".
                    if is_new:
                        recipe.name = name
                    deferred = _apply(recipe, fields, row)
                    recipe.save()
                    for field, value in deferred:
                        field.assign(recipe, value)
            except Exception as error:
                # One unparseable cell shouldn't cost the user the whole file.
                result.errors.append(f'Row {number} ("{name}"): {error}')
                continue

            if is_new:
                result.created += 1
            else:
                result.updated += 1
        finally:
            if progress:
                progress(index + 1, total)

    return result


def import_recipes(data, keys, on_duplicate, mapping=None):
    """The whole thing in one call, for callers that have no user to ask.

    Raises ValueError if the file can't be read or can't be mapped without help.
    """
    if hasattr(data, 'read'):
        data = data.read()
    columns, rows = read_file(data)
    analysis = analyse(columns, rows)
    headers, errors = resolve(analysis, mapping or {})
    if errors:
        raise ValueError(' '.join(errors))
    return import_rows(rows, headers, keys, on_duplicate, columns)
