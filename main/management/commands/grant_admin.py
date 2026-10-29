"""Break-glass: make a user an active Administrator.

The app won't let the last person with user management lose it, but this is
the way back if it ever happens anyway (say, through Django admin), or if the
only administrator forgets their password -- run this for another account, or
pair it with Django's own `changepassword`.
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from main import permissions


class Command(BaseCommand):
    help = "Give a user the Administrator role and make sure their account is active."

    def add_arguments(self, parser):
        parser.add_argument('username')

    def handle(self, *args, **options):
        try:
            user = User.objects.get(username=options['username'])
        except User.DoesNotExist:
            raise CommandError(f"No user called {options['username']!r}.")

        if not user.is_active:
            user.is_active = True
            user.save(update_fields=['is_active'])

        profile = user.profile
        profile.role = permissions.system_role(permissions.ADMINISTRATOR)
        profile.save(update_fields=['role'])

        self.stdout.write(self.style.SUCCESS(
            f"{user.username} is now an active Administrator. "
            f"To reset their password too: manage.py changepassword {user.username}"
        ))
