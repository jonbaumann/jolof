from django.db import migrations
from django.utils.text import slugify

SLUG_MAX_LENGTH = 140
SLUG_FALLBACK = 'recipe'


def backfill_slugs(apps, schema_editor):
    """Give every existing recipe a slug.

    Ordered by id so the oldest recipe with a given name keeps the clean slug
    and later duplicates get -2, -3, and so on. Only touches rows where the slug
    is still null, so re-running is a no-op.

    The slug logic is deliberately duplicated from main.models rather than
    imported: a migration has to keep behaving the same way even if that helper
    changes later.
    """
    Recipe = apps.get_model('main', 'Recipe')

    taken = set(
        Recipe.objects.exclude(slug__isnull=True).values_list('slug', flat=True)
    )

    pending = list(Recipe.objects.filter(slug__isnull=True).order_by('id'))
    for recipe in pending:
        base = slugify(recipe.name or '')[:SLUG_MAX_LENGTH].strip('-_') or SLUG_FALLBACK
        candidate = base
        counter = 1
        while candidate in taken:
            counter += 1
            suffix = f'-{counter}'
            candidate = f'{base[:SLUG_MAX_LENGTH - len(suffix)]}{suffix}'
        taken.add(candidate)
        recipe.slug = candidate

    for start in range(0, len(pending), 500):
        Recipe.objects.bulk_update(pending[start:start + 500], ['slug'])


def clear_slugs(apps, schema_editor):
    Recipe = apps.get_model('main', 'Recipe')
    Recipe.objects.update(slug=None)


class Migration(migrations.Migration):
    """Step 2 of 3: populate slugs for existing recipes."""

    dependencies = [
        ('main', '0031_recipe_slug_recipeslughistory'),
    ]

    operations = [
        migrations.RunPython(backfill_slugs, clear_slugs),
    ]
