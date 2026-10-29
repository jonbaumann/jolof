from django.apps import AppConfig


class MainConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'main'

    def ready(self):
        # Wires the audit log's signal handlers. Imported here rather than at
        # module level so the models are loaded by the time audit.py needs them.
        from . import audit
        audit.connect_signals()
