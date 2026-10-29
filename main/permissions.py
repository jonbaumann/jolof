"""Permissions and roles: what each user is allowed to do.

Permissions live here in code rather than in the database, because each one
only means something where a view checks it. Adding one is a code change
anyway, so the list of them is too.

A user has exactly one Role (on their UserProfile), and a role is a set of
permission codes. Every check in the app goes through the role -- Django's
is_superuser and is_staff only matter to Django admin, never in here.

A "-read-write" permission includes its "-read" counterpart, so a role can't
end up able to edit recipes it isn't allowed to see.
"""
import functools
from dataclasses import dataclass

from django.contrib.auth.decorators import login_required
from django.shortcuts import render


RECIPE_READ = 'recipe-read'
RECIPE_READ_WRITE = 'recipe-read-write'
MEALPLAN_READ = 'mealplan-read'
MEALPLAN_READ_WRITE = 'mealplan-read-write'
USER_MGMT = 'user-mgmt'
AUDIT_LOG = 'audit-log'
PLATFORM_ADMIN = 'platform-admin'
API_RECIPE_READ = 'api-recipe-read'
API_RECIPE_READ_WRITE = 'api-recipe-read-write'
API_MEALPLAN_READ = 'api-mealplan-read'
API_MEALPLAN_READ_WRITE = 'api-mealplan-read-write'


@dataclass(frozen=True)
class Permission:
    code: str
    label: str
    description: str
    # Codes this one includes, e.g. read-write includes read.
    implies: tuple = ()
    # Defined ahead of the feature that will check it; nothing does yet.
    future: bool = False


PERMISSIONS = (
    Permission(RECIPE_READ, 'Recipes: read',
               'View every recipe, search and filter them, and export them to CSV.'),
    Permission(RECIPE_READ_WRITE, 'Recipes: read & write',
               'Add, edit and delete recipes, make them public, manage tags, '
               'and use the import and clean-up tools.',
               implies=(RECIPE_READ,)),
    Permission(MEALPLAN_READ, 'Meal planner: read',
               'View the meal planner, including the workspace and freezer.'),
    Permission(MEALPLAN_READ_WRITE, 'Meal planner: read & write',
               'Change the meal planner, workspace and freezer.',
               implies=(MEALPLAN_READ,)),
    Permission(USER_MGMT, 'User management',
               'Add, edit and remove users, and manage roles.'),
    Permission(AUDIT_LOG, 'Audit log',
               'View the audit log. Changing how long it is kept needs Platform admin.'),
    Permission(PLATFORM_ADMIN, 'Platform admin',
               'Change platform settings such as audit log retention, and delete every recipe at once.'),
    Permission(API_RECIPE_READ, 'API: recipes read',
               'Read recipes and tags through the API.'),
    Permission(API_RECIPE_READ_WRITE, 'API: recipes read & write',
               'Add, edit and delete recipes through the API.',
               implies=(API_RECIPE_READ,), future=True),
    Permission(API_MEALPLAN_READ, 'API: meal planner read',
               'Read the meal planner, workspace and freezer through the API.'),
    Permission(API_MEALPLAN_READ_WRITE, 'API: meal planner read & write',
               'Change the meal planner through the API.',
               implies=(API_MEALPLAN_READ,), future=True),
)

PERMISSIONS_BY_CODE = {p.code: p for p in PERMISSIONS}
ALL_CODES = frozenset(PERMISSIONS_BY_CODE)

# Holding any one of these lets a user log in to the API.
API_CODES = frozenset({
    API_RECIPE_READ, API_RECIPE_READ_WRITE, API_MEALPLAN_READ, API_MEALPLAN_READ_WRITE,
})

# Not a permission of its own: what a view asks for when any one API
# permission will do, e.g. the API's logout or the API Reference page.
ANY_API = 'any-api'


def expand(codes):
    """The given codes plus everything they imply, minus any unknown ones.

    Unknown codes are dropped rather than kept, so a permission removed from
    the code stops counting even if an old custom role still lists it.
    """
    result = set()
    pending = [c for c in codes if c in PERMISSIONS_BY_CODE]
    while pending:
        code = pending.pop()
        if code not in result:
            result.add(code)
            pending.extend(PERMISSIONS_BY_CODE[code].implies)
    return frozenset(result)


# --- System roles ------------------------------------------------------------
#
# Fixed roles whose permissions come from here, not from the database, so they
# can't be edited in the app. Administrator is the only one that picks up new
# permissions automatically; the others list theirs explicitly, so adding a
# permission means deciding whether they should get it.

ADMINISTRATOR = 'administrator'
READ_WRITE = 'read-write'
READ_ONLY = 'read-only'

SYSTEM_ROLES = {
    ADMINISTRATOR: {
        'name': 'Administrator',
        'description': 'Every permission, including any added in future.',
        'permissions': ALL_CODES,
    },
    READ_WRITE: {
        'name': 'Read-write',
        'description': 'Use and change recipes and the meal planner, and view the audit log.',
        'permissions': expand({
            RECIPE_READ_WRITE, MEALPLAN_READ_WRITE, AUDIT_LOG,
            API_RECIPE_READ_WRITE, API_MEALPLAN_READ_WRITE,
        }),
    },
    READ_ONLY: {
        'name': 'Read-only',
        'description': 'View recipes, the meal planner and the audit log.',
        'permissions': expand({
            RECIPE_READ, MEALPLAN_READ, AUDIT_LOG,
            API_RECIPE_READ, API_MEALPLAN_READ,
        }),
    },
}


def system_role(key):
    """The Role row for a system role, recreated if it has somehow gone."""
    from .models import Role
    spec = SYSTEM_ROLES[key]
    role, _ = Role.objects.get_or_create(
        system_key=key, defaults={'name': spec['name'], 'description': spec['description']},
    )
    return role


def roles_with(code):
    """Every role that grants `code`."""
    from .models import Role
    return [role for role in Role.objects.all() if code in role.permission_codes()]


def users_with(code):
    """Active users whose role grants `code`."""
    from django.contrib.auth.models import User
    return User.objects.filter(is_active=True, profile__role__in=roles_with(code))


# --- Lockout protection ------------------------------------------------------
#
# Only user-mgmt can hand out roles, so if nobody active held it there would be
# no way back short of the shell. Every change that could take it away from
# someone is checked against this first.

LOCKOUT_MESSAGE = (
    "This would leave nobody able to manage users. Give another active user a "
    "role with User management first."
)


def user_mgmt_holders(user_changes=None, role_changes=None, deleted_user=None):
    """The active users who would hold user-mgmt after the given changes.

    user_changes: {user pk: {'role': Role, 'is_active': bool}}, either key optional
    role_changes: {role pk: permission codes the role would have}
    deleted_user: pk of a user about to be deleted
    """
    from .models import UserProfile
    user_changes = user_changes or {}
    role_changes = role_changes or {}

    holders = []
    for profile in UserProfile.objects.select_related('user', 'role'):
        user = profile.user
        if user.pk == deleted_user:
            continue
        change = user_changes.get(user.pk, {})
        if not change.get('is_active', user.is_active):
            continue
        role = change.get('role', profile.role)
        if role is None:
            continue
        codes = expand(role_changes[role.pk]) if role.pk in role_changes else role.permission_codes()
        if USER_MGMT in codes:
            holders.append(user)
    return holders


def leaves_no_user_manager(**changes):
    """True when the changes (see user_mgmt_holders) would lock everyone out."""
    return not user_mgmt_holders(**changes)


def default_role_for(user):
    """The role a newly created user starts with.

    Administrator when nobody active can manage users yet -- so the first
    account on a fresh install can't lock itself out -- or when the account was
    made with createsuperuser. Read-only otherwise: the least a new account can
    usefully have, until someone with user management decides more.
    """
    if user.is_superuser or not users_with(USER_MGMT).exclude(pk=user.pk).exists():
        return system_role(ADMINISTRATOR)
    return system_role(READ_ONLY)


# --- Checking ----------------------------------------------------------------

_CACHE_ATTR = '_jolof_permission_codes'


def user_permissions(user):
    """Every permission code the user holds, cached on the user object.

    The cache only lasts as long as the object -- in practice one request --
    so a role change applies from the user's very next page load.
    """
    if user is None or not user.is_authenticated or not user.is_active:
        return frozenset()
    cached = getattr(user, _CACHE_ATTR, None)
    if cached is None:
        from .models import UserProfile
        profile = UserProfile.objects.filter(user=user).select_related('role').first()
        cached = profile.role.permission_codes() if profile and profile.role else frozenset()
        setattr(user, _CACHE_ATTR, cached)
    return cached


def has_perm(user, code):
    return code in user_permissions(user)


def api_permissions(user):
    """The API permission codes the user holds."""
    return user_permissions(user) & API_CODES


def has_any_api_perm(user):
    return bool(api_permissions(user))


def has_required(user, code):
    """has_perm, but also understanding ANY_API."""
    return has_any_api_perm(user) if code == ANY_API else has_perm(user, code)


def forbidden(request):
    return render(request, '403.html', status=403)


def require(code):
    """View decorator: logged in, and holding `code`. Otherwise a 403.

    Includes login_required, so it replaces it rather than stacking with it.
    """
    if code not in PERMISSIONS_BY_CODE and code != ANY_API:
        raise ValueError(f'Unknown permission {code!r}')

    def decorator(view):
        @functools.wraps(view)
        def wrapped(request, *args, **kwargs):
            if not has_required(request.user, code):
                return forbidden(request)
            return view(request, *args, **kwargs)
        wrapped.required_permission = code
        return login_required(wrapped)
    return decorator


class Can:
    """Template-friendly view of a user's permissions.

    Codes have hyphens, which templates can't use as attribute names, so
    `{% if can.recipe_read_write %}` looks up 'recipe-read-write'.
    """

    def __init__(self, user):
        self._user = user

    @property
    def any_api(self):
        return has_any_api_perm(self._user)

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return has_perm(self._user, name.replace('_', '-'))
