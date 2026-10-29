"""The one definition of what a recipe looks like as a CSV row.

Both directions read this file: the export writes a column per field, and the
import reads a column per field back in. Keeping them in the same registry is
what makes a file exported by Jolof importable by Jolof -- add a field here and
both sides pick it up.

Fields with no ``parse`` are export-only (a timestamp the database owns, a URL
we generate) and never appear as an import option.
"""

from collections import namedtuple

from django.utils import timezone

from .models import Tag


# key:      CSV column header, and the checkbox value posted back.
# label:    what the user sees next to the checkbox.
# extract:  recipe -> cell value. Returned values are stringified by csv.
# default:  whether the checkbox starts ticked, on both tools.
# parse:    cell value -> python value, or None if the field can't be imported.
#           Raises ValueError with a user-facing message on bad input.
# assign:   applies a parsed value to a recipe *after* it has been saved. Only
#           needed for relations, which can't be set before there's a pk.
CSVField = namedtuple(
    'CSVField', 'key label extract default parse assign', defaults=(None, None)
)


def _blank_if_none(value):
    return '' if value is None else value


def _bool(value):
    """Nullable booleans export as true/false, with null left blank.

    Blank rather than "false" matters here: most of these flags are null on
    older recipes, meaning "nobody has said", which is not the same as "no".
    """
    if value is None:
        return ''
    return 'true' if value else 'false'


def _timestamp(value):
    if not value:
        return ''
    return timezone.localtime(value).strftime('%Y-%m-%d %H:%M:%S')


def _tags(recipe):
    """Tags as "label:value" pairs, comma separated.

    Deliberately the same shape RecipeForm.tags_input accepts, so a column
    pasted back into the tags box on a recipe is valid as-is.
    """
    return ', '.join(str(tag) for tag in recipe.tags.all())


def _image_url(recipe):
    # A recipe row shouldn't fail the whole export because its image is
    # missing from storage.
    try:
        return recipe.image.url if recipe.image else ''
    except (ValueError, OSError):
        return ''


TRUE_WORDS = {'true', 't', 'yes', 'y', '1'}
FALSE_WORDS = {'false', 'f', 'no', 'n', '0'}


def _parse_text(raw):
    return raw.strip()


def _parse_rating(raw):
    """A 1-5 score, or None when the cell is blank.

    Spreadsheets like to turn 4 into 4.0, so a trailing .0 is tolerated -- but
    a genuine fraction is an error rather than something to round away.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        number = float(raw)
    except ValueError:
        raise ValueError(f'expected a whole number from 1 to 5, got "{raw}"')
    if number != int(number):
        raise ValueError(f'expected a whole number from 1 to 5, got "{raw}"')
    value = int(number)
    if not 1 <= value <= 5:
        raise ValueError(f'expected a whole number from 1 to 5, got "{raw}"')
    return value


def _parse_nullable_bool(raw):
    """true/false, with blank meaning "nobody has said" -- see _bool()."""
    word = raw.strip().lower()
    if not word:
        return None
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise ValueError(f'expected true or false, got "{raw}"')


def _parse_bool(raw):
    """Same words, but for flags the model won't let be null."""
    value = _parse_nullable_bool(raw)
    return False if value is None else value


def _parse_tags(raw):
    """"label:value, label:value" -> the pairs, validated but not yet created."""
    pairs = []
    for entry in raw.split(','):
        entry = entry.strip()
        if not entry:
            continue
        if ':' not in entry:
            raise ValueError(f'tag "{entry}" is missing a colon, expected label:value')
        label, value = (part.strip() for part in entry.split(':', 1))
        if not label or not value:
            raise ValueError(f'tag "{entry}" needs a label and a value, e.g. cuisine:italian')
        pairs.append((label, value))
    return pairs


def _assign_tags(recipe, pairs):
    tags = [
        Tag.objects.get_or_create(label=label, value=value)[0]
        for label, value in pairs
    ]
    # set() rather than add(): a tags column that lists two tags means the
    # recipe has those two tags, not those two on top of whatever it had.
    recipe.tags.set(tags)


# Grouped for display; flattened for everything else. Order here is the order
# of the columns in an exported file.
FIELD_GROUPS = [
    ('Recipe', [
        CSVField('name', 'Name', lambda r: r.name, True, _parse_text),
        CSVField('description', 'Description', lambda r: _blank_if_none(r.description), True, _parse_text),
        CSVField('ingredients', 'Ingredients', lambda r: r.ingredients, True, _parse_text),
        CSVField('instructions', 'Instructions', lambda r: r.instructions, True, _parse_text),
        CSVField('notes', 'Notes', lambda r: _blank_if_none(r.notes), True, _parse_text),
        CSVField('url', 'Source URL', lambda r: _blank_if_none(r.url), True, _parse_text),
        CSVField('tags', 'Tags', _tags, True, _parse_tags, _assign_tags),
    ]),
    ('Ratings', [
        CSVField('rating', 'Rating', lambda r: _blank_if_none(r.rating), True, _parse_rating),
        CSVField('effort', 'Effort', lambda r: _blank_if_none(r.effort), True, _parse_rating),
        CSVField('complexity', 'Complexity', lambda r: _blank_if_none(r.complexity), True, _parse_rating),
        CSVField('health', 'Healthiness', lambda r: _blank_if_none(r.health), True, _parse_rating),
    ]),
    ('Dietary', [
        CSVField('gluten_free', 'Gluten free', lambda r: _bool(r.gluten_free), True, _parse_nullable_bool),
        CSVField('dairy_free', 'Dairy free', lambda r: _bool(r.dairy_free), True, _parse_nullable_bool),
        CSVField('vegetarian', 'Vegetarian', lambda r: _bool(r.vegetarian), True, _parse_nullable_bool),
        CSVField('vegan', 'Vegan', lambda r: _bool(r.vegan), True, _parse_nullable_bool),
    ]),
    ('Metadata', [
        # id and slug are export-only, but the import still reads them when
        # they're in the file -- see csv_import.find_existing().
        CSVField('id', 'ID', lambda r: r.pk, False),
        CSVField('slug', 'Slug', lambda r: r.slug, False),
        CSVField('image_url', 'Image URL', _image_url, False),
        CSVField('created_at', 'Created', lambda r: _timestamp(r.created_at), False),
        CSVField('updated_at', 'Last updated', lambda r: _timestamp(r.updated_at), False),
    ]),
    ('Sharing', [
        CSVField('is_public', 'Is public', lambda r: _bool(r.is_public), False, _parse_bool),
        CSVField('share_notes', 'Shares notes', lambda r: _bool(r.share_notes), False, _parse_bool),
        CSVField('public_url', 'Public link', lambda r: _blank_if_none(r.get_public_url()), False),
    ]),
]

FIELDS_BY_KEY = {
    field.key: field
    for _group, fields in FIELD_GROUPS
    for field in fields
}

ALL_KEYS = list(FIELDS_BY_KEY)

DEFAULT_KEYS = [key for key, field in FIELDS_BY_KEY.items() if field.default]

IMPORT_KEYS = [key for key, field in FIELDS_BY_KEY.items() if field.parse]

# Header spellings accepted on import, so a file that was tidied up in a
# spreadsheet ("Source URL" instead of "url") still lines up.
HEADER_ALIASES = {}
for _key, _field in FIELDS_BY_KEY.items():
    HEADER_ALIASES[_key.lower()] = _key
    HEADER_ALIASES[_field.label.lower()] = _key


def ordered_fields(keys):
    """Selected fields in registry order, ignoring anything unrecognised."""
    selected = set(keys)
    return [FIELDS_BY_KEY[key] for key in ALL_KEYS if key in selected]
