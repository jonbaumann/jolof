from django.db import migrations, models


class Migration(migrations.Migration):
    """Step 3 of 3: now that every row is populated, make the slug required
    and unique."""

    dependencies = [
        ('main', '0032_backfill_recipe_slugs'),
    ]

    operations = [
        migrations.AlterField(
            model_name='recipe',
            name='slug',
            field=models.SlugField(max_length=140, unique=True),
        ),
    ]
