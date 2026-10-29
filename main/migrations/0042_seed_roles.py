from django.db import migrations


# Copied rather than imported from main.permissions, so this migration keeps
# doing the same thing if the code's names or descriptions change later.
SYSTEM_ROLES = [
    ('administrator', 'Administrator', 'Every permission, including any added in future.'),
    ('read-write', 'Read-write', 'Use and change recipes and the meal planner, and view the audit log.'),
    ('read-only', 'Read-only', 'View recipes, the meal planner and the audit log.'),
]


def seed_roles(apps, schema_editor):
    """Create the system roles and give every existing user one.

    Superusers become Administrators and everyone else Read-write, which is
    what they could effectively do before roles existed (everything but the
    user management that's arriving with them).
    """
    Role = apps.get_model('main', 'Role')
    UserProfile = apps.get_model('main', 'UserProfile')
    User = apps.get_model('auth', 'User')

    roles = {}
    for key, name, description in SYSTEM_ROLES:
        roles[key], _ = Role.objects.get_or_create(
            system_key=key, defaults={'name': name, 'description': description},
        )

    for user in User.objects.all():
        profile, _ = UserProfile.objects.get_or_create(user=user)
        if profile.role_id is None:
            profile.role = roles['administrator' if user.is_superuser else 'read-write']
            profile.save(update_fields=['role'])


def unseed_roles(apps, schema_editor):
    UserProfile = apps.get_model('main', 'UserProfile')
    Role = apps.get_model('main', 'Role')
    UserProfile.objects.update(role=None)
    Role.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('main', '0041_role'),
    ]

    operations = [
        migrations.RunPython(seed_roles, unseed_roles),
    ]
