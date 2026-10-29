from django.db import models, transaction, IntegrityError
from django.db.models.signals import post_delete, post_save
from django.db.models.functions import Lower
from django.dispatch import receiver
from django.core.validators import MinValueValidator, MaxValueValidator
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.text import slugify
from datetime import timedelta

# Longest slug we'll store. Recipe names cap at 100 characters, but slugify can
# lengthen a name slightly (spaces become hyphens) and collisions append a suffix.
SLUG_MAX_LENGTH = 140

# Used when a name slugifies to nothing at all -- e.g. a name that is entirely
# non-Latin characters or emoji. The usual collision suffixing then yields
# "recipe-2", "recipe-3", and so on.
SLUG_FALLBACK = 'recipe'


def build_slug_base(name):
    """Slugify a recipe name into a URL-safe base, before de-duplication."""
    base = slugify(name or '')[:SLUG_MAX_LENGTH].strip('-_')
    return base or SLUG_FALLBACK


# Create your models here.
class Tag(models.Model):
    label = models.CharField(max_length=100)
    value = models.CharField(max_length=100)
    
    class Meta:
        unique_together = ('label', 'value')
        ordering = ['label', 'value']
    
    def __str__(self):
        return f"{self.label}:{self.value}"

    def get_recipe_search_url(self):
        """Recipe search pre-filled with this tag, as the search box would accept it."""
        return f"{reverse('recipes')}?{urlencode({'search_term': str(self)})}"

class Recipe(models.Model):

    class Meta:
        db_table = "recipes"

    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=SLUG_MAX_LENGTH, unique=True)
    description = models.TextField(null=True)
    complexity = models.IntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)])
    effort = models.IntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)])
    health = models.IntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)])
    rating = models.IntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)])
    gluten_free = models.BooleanField(null=True)
    dairy_free = models.BooleanField(null=True)
    vegetarian = models.BooleanField(null=True)
    vegan = models.BooleanField(null=True)
    url = models.CharField(max_length=1000, null=True)
    instructions = models.TextField(default="add later")
    ingredients = models.TextField(default="add later")
    notes = models.TextField(null=True)
    # blank=True matters as much as null=True here: clearing the image via the
    # form's Clear checkbox hands the model an empty string, and model validation
    # would otherwise reject it as "This field cannot be blank".
    image = models.ImageField(null=True, blank=True, upload_to='images/') # Files stored in MEDIA_ROOT/images/
    tags = models.ManyToManyField(Tag, blank=True, related_name='recipes')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_public = models.BooleanField(default=False)
    # Looked up on its own for the public share URL -- unguessable, unlike the
    # slug, so a recipe can't be found publicly unless this exact link was
    # shared. Kept even while is_public is False so re-enabling reuses the
    # same link rather than invalidating anything already shared out.
    share_token = models.CharField(max_length=32, blank=True, null=True, unique=True)
    # Off by default -- the public page hides notes unless explicitly opted in.
    share_notes = models.BooleanField(default=False)

    # Name as it was when this instance was loaded from the database, so save()
    # can tell a rename from any other edit. Stays None for unsaved instances and
    # for instances loaded with the name deferred (e.g. via .only()).
    _original_name = None

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        if 'name' in field_names:
            instance._original_name = instance.name
        return instance

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('recipe-detail', args=[self.slug])

    def get_public_url(self):
        if not self.share_token:
            return None
        return reverse('recipe-public', args=[self.share_token, self.slug])

    def _slug_is_available(self, candidate):
        """True if no *other* recipe holds this slug, live or retired.

        A slug this recipe itself retired earlier is fair game -- that's the
        rename-and-rename-back case, handled in save() by reclaiming the row.
        """
        live = Recipe.objects.filter(slug=candidate)
        retired = RecipeSlugHistory.objects.filter(slug=candidate)
        if self.pk:
            live = live.exclude(pk=self.pk)
            retired = retired.exclude(recipe_id=self.pk)
        return not live.exists() and not retired.exists()

    def _unique_slug(self, base):
        """Append -2, -3, ... to base until it collides with nothing."""
        candidate = base
        counter = 1
        while not self._slug_is_available(candidate):
            counter += 1
            suffix = f'-{counter}'
            candidate = f'{base[:SLUG_MAX_LENGTH - len(suffix)]}{suffix}'
        return candidate

    def save(self, *args, **kwargs):
        # Regenerate the slug on create, on rename, or if one is somehow missing.
        renamed = self._original_name is not None and self._original_name != self.name
        if not self.slug or self.pk is None or renamed:
            retired_slug = self.slug if (self.pk and self.slug) else None

            # A slug collision can also be lost to a concurrent save, so retry
            # with a freshly numbered candidate rather than 500ing.
            for attempt in range(5):
                self.slug = self._unique_slug(build_slug_base(self.name))
                try:
                    with transaction.atomic():
                        if self.pk:
                            # Reclaiming a slug this recipe retired earlier.
                            RecipeSlugHistory.objects.filter(
                                recipe_id=self.pk, slug=self.slug
                            ).delete()
                        super().save(*args, **kwargs)
                        if retired_slug and retired_slug != self.slug:
                            RecipeSlugHistory.objects.create(
                                recipe=self, slug=retired_slug
                            )
                    break
                except IntegrityError:
                    # The atomic block rolled back, so nothing was written; loop
                    # round and recompute the slug against the new state.
                    if attempt == 4:
                        raise
        else:
            super().save(*args, **kwargs)

        self._original_name = self.name


class RecipeSlugHistory(models.Model):
    """A slug a recipe used to have, kept so old links keep working after a rename.

    Requests for a retired slug are permanently redirected to the recipe's
    current URL. Rows are also what stops a retired slug from being handed out
    to a different recipe later.
    """

    class Meta:
        db_table = "recipe_slug_history"
        ordering = ['-created_at']
        verbose_name_plural = "recipe slug history"

    slug = models.SlugField(max_length=SLUG_MAX_LENGTH, unique=True)
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='slug_history')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.slug} -> {self.recipe.slug}"


@receiver(post_delete, sender=Recipe)
def delete_recipe_image(sender, instance, **kwargs):
    if instance.image:
        instance.image.delete(save=False)


class Favorite(models.Model):
    """A recipe one user has favorited.

    Deliberately per-user rather than a flag on Recipe: the recipe library is
    shared, so a favorite only ever belongs to the person who set it.
    """

    class Meta:
        db_table = "recipe_favorites"
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['user', 'recipe'], name='unique_user_favorite'),
        ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='favorites')
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='favorites')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} ♥ {self.recipe}"


class UserProfile(models.Model):
    """Per-user app state that doesn't belong on the auth User model."""

    class Meta:
        db_table = "user_profiles"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    # Most recent app VERSION for which this user dismissed the "What's New" popup.
    # Stored server-side so the popup stays dismissed across all of the user's devices.
    last_seen_version = models.CharField(max_length=20, blank=True, default='')
    # IANA name, e.g. "America/New_York". Blank means follow settings.TIME_ZONE,
    # so a user who never picks one isn't pinned to today's site default.
    # Validated against the machine's tz database rather than by choices=, which
    # would bake a 600-entry list into every migration.
    timezone = models.CharField(max_length=64, blank=True, default='')

    THEME_LIGHT = 'light'
    THEME_DARK = 'dark'
    THEME_AUTO = 'auto'
    THEME_CHOICES = [
        (THEME_LIGHT, 'Light'),
        (THEME_DARK, 'Dark'),
        (THEME_AUTO, 'Match my device'),
    ]
    # Blank means "not chosen yet": the browser then keeps whatever it last had
    # (the old navbar toggle kept it in localStorage) and saves that here, once.
    theme = models.CharField(max_length=10, blank=True, default='', choices=THEME_CHOICES)
    # What the user may do; see main/permissions.py. Every user must have one --
    # nullable only so a profile can exist for the instant before it's assigned.
    # PROTECT so a role still in use can't be deleted out from under its users.
    role = models.ForeignKey('Role', on_delete=models.PROTECT, null=True, blank=True, related_name='profiles')
    # Set when someone else sets this user's password; the user is sent to the
    # change-password page until they pick their own. See ForcePasswordChangeMiddleware.
    must_change_password = models.BooleanField(default=False)
    # Whose meal planner this user sees; null means their own. PROTECT so a
    # household still in use can't be deleted, taking its planner with it.
    household = models.ForeignKey('MealPlanGroup', on_delete=models.PROTECT, null=True, blank=True,
                                  related_name='profiles')

    def __str__(self):
        return f"Profile for {self.user}"


class Role(models.Model):
    """A named set of permissions. Each user has exactly one.

    System roles (system_key set) are seeded by a migration and take their
    permissions from main/permissions.py, so they can't be changed in the app.
    Custom roles keep their own list in `permissions`.
    """

    class Meta:
        db_table = "roles"
        ordering = ['name']

    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=255, blank=True, default='')
    # One of permissions.SYSTEM_ROLES' keys, or null for a custom role.
    system_key = models.CharField(max_length=50, unique=True, null=True, blank=True, editable=False)
    # Permission codes, for custom roles only. Ignored for system roles.
    permissions = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    @property
    def is_system(self):
        return self.system_key is not None

    def permission_codes(self):
        """Every permission this role grants, implied ones included."""
        from . import permissions
        if self.is_system:
            system = permissions.SYSTEM_ROLES.get(self.system_key)
            return system['permissions'] if system else frozenset()
        return permissions.expand(self.permissions or ())


@receiver(post_save, sender=User)
def assign_default_role(sender, instance, created, raw=False, **kwargs):
    """Give every new user a role the moment they exist, however they were made
    (sign-up, createsuperuser, Django admin, a test)."""
    if raw or not created:
        return
    from . import permissions
    profile, _ = UserProfile.objects.get_or_create(user=instance)
    if profile.role_id is None:
        profile.role = permissions.default_role_for(instance)
        profile.save(update_fields=['role'])


class MealPlanGroup(models.Model):
    """A household: people who share one meal planner, workspace and freezer.

    Called a household everywhere a user can see it; the old name stays in the
    code and database. Members are set on UserProfile.household, so each user
    is in one household at most. Someone in none has a planner of their own.
    """

    class Meta:
        db_table = "meal_plan_groups"
        verbose_name = 'household'
        ordering = [Lower('name')]

    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class MealPlan(models.Model):

    class Meta:
        db_table = "meal_plans"
        ordering = ['date', 'slot']
        constraints = [
            models.UniqueConstraint(
                fields=['date', 'slot', 'group'],
                condition=models.Q(group__isnull=False),
                name='unique_group_meal_plan',
            ),
            models.UniqueConstraint(
                fields=['date', 'slot', 'user'],
                condition=models.Q(group__isnull=True),
                name='unique_personal_meal_plan',
            ),
        ]

    date = models.DateField()
    slot = models.IntegerField()
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='meal_plans', null=True, blank=True)
    custom_text = models.CharField(max_length=200, blank=True, default='')
    # Who added it. SET_NULL so deleting a user doesn't take their entries out of
    # a shared household planner; their personal ones are removed explicitly.
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='meal_plans')
    group = models.ForeignKey(MealPlanGroup, on_delete=models.CASCADE, related_name='meal_plans', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def display_name(self):
        return self.recipe.name if self.recipe else self.custom_text

    def __str__(self):
        owner = self.group or self.user
        return f"{owner}: {self.display_name} on {self.date} (slot {self.slot})"


class WorkspaceItem(models.Model):
    """A recipe or custom-text card staged in the meal-planner workspace, ready to drag onto the calendar."""

    class Meta:
        db_table = "meal_plan_workspace_items"
        ordering = ['-created_at']
        constraints = [
            # Only recipe-backed items are de-duplicated; custom-text cards
            # (recipe is null) may be staged freely, even with identical text.
            models.UniqueConstraint(
                fields=['recipe', 'group'],
                condition=models.Q(group__isnull=False, recipe__isnull=False),
                name='unique_group_workspace_item',
            ),
            models.UniqueConstraint(
                fields=['recipe', 'user'],
                condition=models.Q(group__isnull=True, recipe__isnull=False),
                name='unique_personal_workspace_item',
            ),
        ]

    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='workspace_items', null=True, blank=True)
    custom_text = models.CharField(max_length=200, blank=True, default='')
    # Who added it. SET_NULL so deleting a user doesn't take their entries out of
    # a shared household planner; their personal ones are removed explicitly.
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='workspace_items')
    group = models.ForeignKey(MealPlanGroup, on_delete=models.CASCADE, related_name='workspace_items', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def display_name(self):
        return self.recipe.name if self.recipe else self.custom_text

    def __str__(self):
        owner = self.group or self.user
        return f"{owner}: {self.display_name} (workspace)"


class FreezerItem(models.Model):
    """An item the user currently has in their freezer: a recipe in the database
    or a free-text name like "corn". Referenced from the meal planner to stage or
    schedule. Scoped like WorkspaceItem so a household shares one freezer."""

    class Meta:
        db_table = "freezer_items"
        ordering = ['-created_at']
        constraints = [
            # De-duplicate recipe-backed items; free-text names may repeat.
            models.UniqueConstraint(
                fields=['recipe', 'group'],
                condition=models.Q(group__isnull=False, recipe__isnull=False),
                name='unique_group_freezer_item',
            ),
            models.UniqueConstraint(
                fields=['recipe', 'user'],
                condition=models.Q(group__isnull=True, recipe__isnull=False),
                name='unique_personal_freezer_item',
            ),
        ]

    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='freezer_items', null=True, blank=True)
    custom_text = models.CharField(max_length=200, blank=True, default='')
    # Who added it. SET_NULL so deleting a user doesn't take their entries out of
    # a shared household planner; their personal ones are removed explicitly.
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='freezer_items')
    group = models.ForeignKey(MealPlanGroup, on_delete=models.CASCADE, related_name='freezer_items', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def display_name(self):
        return self.recipe.name if self.recipe else self.custom_text

    def __str__(self):
        owner = self.group or self.user
        return f"{owner}: {self.display_name} (freezer)"


class AuditSettings(models.Model):
    """Singleton row holding the audit log's configuration.

    Kept in the database rather than in settings.py so retention can be changed
    from inside the app, without a deploy.
    """

    class Meta:
        db_table = "audit_settings"
        verbose_name_plural = "audit settings"

    # How long entries are kept. 0 means keep everything, forever.
    retention_days = models.PositiveIntegerField(
        default=365,
        validators=[MaxValueValidator(3650)],
        help_text="How many days of history to keep. 0 keeps everything forever.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        settings_obj, _ = cls.objects.get_or_create(pk=1)
        return settings_obj

    def save(self, *args, **kwargs):
        # There is only ever one row, whatever anyone tries to save.
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return "Audit settings"


class AuditLog(models.Model):
    """One recorded change to something in the app.

    Written by the signal handlers in main/audit.py, never by hand. Rows are
    deliberately self-contained -- the actor's name, the object's name, and the
    labels of the fields that changed are all frozen into the row, so an entry
    still reads correctly after the recipe is deleted or the user renamed.
    """

    ACTION_CREATED = 'created'
    ACTION_UPDATED = 'updated'
    ACTION_DELETED = 'deleted'
    ACTION_CHOICES = [
        (ACTION_CREATED, 'Created'),
        (ACTION_UPDATED, 'Updated'),
        (ACTION_DELETED, 'Deleted'),
    ]

    class Meta:
        db_table = "audit_log"
        ordering = ['-timestamp', '-id']
        indexes = [
            models.Index(fields=['-timestamp'], name='audit_timestamp_idx'),
            models.Index(fields=['object_type', '-timestamp'], name='audit_type_time_idx'),
            models.Index(fields=['actor', '-timestamp'], name='audit_actor_time_idx'),
        ]

    timestamp = models.DateTimeField(auto_now_add=True)
    # Kept even if the user is later deleted; actor_label preserves who it was.
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_entries')
    actor_label = models.CharField(max_length=150, blank=True, default='')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    # Human-facing model name ("Recipe", "Meal plan"), not the model's label.
    object_type = models.CharField(max_length=50)
    object_id = models.IntegerField(null=True, blank=True)
    object_label = models.CharField(max_length=255, blank=True, default='')
    # Snapshot of the object's URL. Recipe links survive renames because
    # retired slugs redirect (see RecipeSlugHistory).
    object_url = models.CharField(max_length=500, blank=True, default='')
    # {"fields": ["Name", "Tags"]} -- which fields an edit touched. Values are
    # deliberately not kept: the log records that a recipe was edited, not what
    # it now says. Rows written before that decision keep their older shape and
    # are read for their field names by changed_fields.
    changes = models.JSONField(default=dict, blank=True)

    def __str__(self):
        who = self.actor_label or 'system'
        return f"{who} {self.action} {self.object_type} {self.object_label}"

    @property
    def actor_display(self):
        return self.actor_label or 'System'

    @property
    def changed_fields(self):
        """Labels of the fields this entry changed, in a stable order.

        Entries written before the log stopped storing values keep their old
        shape in the database, so those are read for their field names too
        rather than being shown as blank.
        """
        changes = self.changes or {}
        fields = changes.get('fields')
        if isinstance(fields, list):
            return fields
        # Legacy shape: {field_name: {"label": ..., "from": ..., "to": ...}}.
        return sorted(
            (change.get('label') or name.replace('_', ' ').capitalize())
            for name, change in changes.items()
            if isinstance(change, dict)
        )

    @classmethod
    def retention_cutoff(cls):
        """The oldest timestamp worth keeping, or None when keeping forever."""
        days = AuditSettings.load().retention_days
        if not days:
            return None
        return timezone.now() - timedelta(days=days)

    @classmethod
    def prune(cls):
        """Delete entries past the configured retention. Returns how many went."""
        cutoff = cls.retention_cutoff()
        if cutoff is None:
            return 0
        deleted, _ = cls.objects.filter(timestamp__lt=cutoff).delete()
        return deleted


class ApiSettings(models.Model):
    """Singleton row holding how long an idle API session token lasts.

    Kept in the database, like AuditSettings, so it can be changed from
    Platform Settings without a deploy. The timeout is read on every request
    rather than stamped on a token when it's issued, so changing it applies to
    tokens already handed out. There's deliberately no max lifetime: a token
    lasts as long as its session stays in use.
    """

    class Meta:
        db_table = "api_settings"
        verbose_name_plural = "API settings"

    # 0 turns the timeout off.
    idle_timeout_minutes = models.PositiveIntegerField(
        default=60,
        validators=[MaxValueValidator(10080)],
        help_text="Minutes a token can go unused before it stops working. 0 means never.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        settings_obj, _ = cls.objects.get_or_create(pk=1)
        return settings_obj

    def save(self, *args, **kwargs):
        # There is only ever one row, whatever anyone tries to save.
        self.pk = 1
        super().save(*args, **kwargs)

    @property
    def idle_timeout(self):
        return timedelta(minutes=self.idle_timeout_minutes) if self.idle_timeout_minutes else None

    def __str__(self):
        return "API settings"


class ApiSession(models.Model):
    """A session token handed out by the API's login endpoint.

    Only a hash of the token is stored, so a copy of the database doesn't hand
    out working tokens. When a token stops working is worked out from
    ApiSettings on each use rather than stored, so see expires_at.
    """

    class Meta:
        db_table = "api_sessions"
        ordering = ['-last_used_at']

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='api_sessions')
    token_hash = models.CharField(max_length=64, unique=True)
    # The start of the token, so a person can tell their sessions apart.
    token_prefix = models.CharField(max_length=12)
    # The user's session auth hash at login. It changes with the password, so
    # any password change -- from anywhere -- ends the user's API sessions.
    auth_hash = models.CharField(max_length=128)
    client_name = models.CharField(max_length=100, blank=True, default='')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(default=timezone.now)

    def expires_at(self, api_settings=None):
        """When the session ends if it isn't used again, or None if it never does."""
        idle = (api_settings or ApiSettings.load()).idle_timeout
        return self.last_used_at + idle if idle else None

    def is_expired(self, api_settings=None, now=None):
        ends = self.expires_at(api_settings)
        return ends is not None and (now or timezone.now()) >= ends

    @classmethod
    def active(cls):
        """Sessions used within the idle timeout."""
        idle = ApiSettings.load().idle_timeout
        qs = cls.objects.all()
        if idle:
            qs = qs.filter(last_used_at__gt=timezone.now() - idle)
        return qs

    @classmethod
    def prune(cls):
        """Delete sessions past the idle timeout. Returns how many went."""
        deleted, _ = cls.objects.exclude(pk__in=cls.active().values('pk')).delete()
        return deleted

    def __str__(self):
        return f"{self.user}: {self.token_prefix}… ({self.client_name or 'API'})"
