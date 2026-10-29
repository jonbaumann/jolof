from django.db import migrations, models
import django.db.models.deletion
import django.db.models.functions.text


def members_to_household(apps, schema_editor):
    """Move each user's group membership onto their profile.

    The old many-to-many allowed several groups per user, but the planner only
    ever used the first (lowest id) -- so that's the one kept.
    """
    MealPlanGroup = apps.get_model('main', 'MealPlanGroup')
    UserProfile = apps.get_model('main', 'UserProfile')
    for group in MealPlanGroup.objects.order_by('-pk'):
        for user in group.members.all():
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.household = group
            profile.save(update_fields=['household'])


def household_to_members(apps, schema_editor):
    UserProfile = apps.get_model('main', 'UserProfile')
    for profile in UserProfile.objects.filter(household__isnull=False).select_related('household', 'user'):
        profile.household.members.add(profile.user)


# The audit log names what changed by these labels; keep earlier rows matching
# the new ones so filtering by type still finds them.
RELABEL = [('Meal plan group', 'Household'), ('User role', 'User profile')]


def relabel_audit(apps, schema_editor):
    AuditLog = apps.get_model('main', 'AuditLog')
    for old, new in RELABEL:
        AuditLog.objects.filter(object_type=old).update(object_type=new)


def unrelabel_audit(apps, schema_editor):
    AuditLog = apps.get_model('main', 'AuditLog')
    for old, new in RELABEL:
        AuditLog.objects.filter(object_type=new).update(object_type=old)


class Migration(migrations.Migration):

    dependencies = [
        ('main', '0044_userprofile_theme'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='mealplangroup',
            options={'ordering': [django.db.models.functions.text.Lower('name')], 'verbose_name': 'household'},
        ),
        migrations.AddField(
            model_name='userprofile',
            name='household',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='profiles', to='main.mealplangroup'),
        ),
        migrations.RunPython(members_to_household, household_to_members),
        migrations.RemoveField(
            model_name='mealplangroup',
            name='members',
        ),
        migrations.RunPython(relabel_audit, unrelabel_audit),
    ]
