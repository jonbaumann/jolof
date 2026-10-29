from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """Step 1 of 3: add the slug column (nullable, not yet unique) and the
    history table. Populated by 0032, tightened to unique/non-null by 0033."""

    dependencies = [
        ('main', '0030_freezeritem_freezeritem_unique_group_freezer_item_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='recipe',
            name='slug',
            field=models.SlugField(max_length=140, null=True),
        ),
        migrations.CreateModel(
            name='RecipeSlugHistory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('slug', models.SlugField(max_length=140, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('recipe', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='slug_history', to='main.recipe')),
            ],
            options={
                'db_table': 'recipe_slug_history',
                'ordering': ['-created_at'],
                'verbose_name_plural': 'recipe slug history',
            },
        ),
    ]
