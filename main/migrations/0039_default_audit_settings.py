from django.db import migrations


def create_settings(apps, schema_editor):
    """Seed the singleton settings row with the default one-year retention.

    Done here so the very first page load doesn't create it -- and log itself
    doing so -- as the app's first audit entry.
    """
    AuditSettings = apps.get_model('main', 'AuditSettings')
    AuditSettings.objects.get_or_create(pk=1, defaults={'retention_days': 365})


def delete_settings(apps, schema_editor):
    AuditSettings = apps.get_model('main', 'AuditSettings')
    AuditSettings.objects.filter(pk=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('main', '0038_auditsettings_auditlog'),
    ]

    operations = [
        migrations.RunPython(create_settings, delete_settings),
    ]
