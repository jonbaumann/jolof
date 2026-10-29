"""Audit log: who changed what, and when.

Coverage comes from model signals rather than logging calls sprinkled through
the views, so anything that writes through the ORM -- a form save, a CSV import
running in a background thread, a bulk delete from a tool page -- lands in the
log without the calling code having to remember to say so.

The actor is whoever's request is in flight, carried in a ContextVar that
AuditActorMiddleware sets for the life of each request. Threads start with a
fresh context and so inherit nothing, which is why the background tasks in
views.py wrap themselves in actor_context().

One edit means one entry. The ORM may touch the same object several times to
satisfy a single form submit -- RecipeForm.save() writes the row, clears every
tag, then adds them back one at a time -- so signals are collected in a
per-request buffer keyed by object and written once, at the end. That also lets
tag churn cancel out: a tag removed and re-added in the same request never
changed, and isn't reported. Outside a request or task there's no buffer and
entries are written as they happen.

Entries record which fields changed, not what they changed to. The values are
the recipe; the log is the fact that someone edited it.

Recording must never be the reason a save fails, so every handler swallows its
own errors and logs them instead.
"""
import contextlib
import contextvars
import datetime
import logging

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save

from .models import (
    ApiSettings,
    AuditLog,
    AuditSettings,
    Favorite,
    FreezerItem,
    MealPlan,
    MealPlanGroup,
    Recipe,
    Role,
    Tag,
    UserProfile,
    WorkspaceItem,
)

logger = logging.getLogger(__name__)

# The label used for a recipe's tags, which change through m2m signals rather
# than through a field on the row.
TAGS_LABEL = 'Tags'


_current_actor = contextvars.ContextVar('audit_current_actor', default=None)
# The open buffer, when a request or task is collecting. None means "write now".
_pending = contextvars.ContextVar('audit_pending', default=None)


def get_actor():
    """The user whose action is currently being recorded, or None."""
    return _current_actor.get()


def set_actor(user):
    """Set the current actor. Returns a token for _current_actor.reset()."""
    if user is not None and not getattr(user, 'is_authenticated', False):
        user = None
    return _current_actor.set(user)


class _Buffer:
    """Everything one request changed, keyed by the object it happened to.

    Repeated signals for the same object fold into one pending entry, so the
    six saves behind a single recipe edit become one row.
    """

    def __init__(self):
        self.entries = {}

    def touch(self, action, object_type, instance):
        """Get (or start) the pending entry for this object."""
        key = (object_type, instance.pk)
        entry = self.entries.get(key)
        if entry is None:
            entry = {
                'action': action,
                'object_type': object_type,
                'object_id': instance.pk,
                'fields': set(),
                'tags_added': set(),
                'tags_removed': set(),
                'actor': get_actor(),
            }
            self.entries[key] = entry
        else:
            entry['action'] = _merge_action(entry['action'], action)
        # Re-read every time so the last known name and link win -- a rename
        # part-way through the request should be what the entry points at.
        entry['object_label'] = _object_label(instance)
        entry['object_url'] = _object_url(instance)
        return entry


def _merge_action(existing, incoming):
    """Which action a pair of signals for the same object adds up to.

    A delete supersedes anything pending; an object created and then edited in
    the same request was only ever created.
    """
    if incoming == AuditLog.ACTION_DELETED or existing == AuditLog.ACTION_DELETED:
        return AuditLog.ACTION_DELETED
    if existing == AuditLog.ACTION_CREATED:
        return AuditLog.ACTION_CREATED
    return incoming


@contextlib.contextmanager
def collecting():
    """Fold everything changed inside this block into one entry per object."""
    token = _pending.set(_Buffer())
    try:
        yield
    finally:
        try:
            flush()
        finally:
            _pending.reset(token)


def flush():
    """Write out whatever the open buffer has collected."""
    buffer = _pending.get()
    if buffer is None:
        return
    entries, buffer.entries = buffer.entries, {}
    for entry in entries.values():
        _write(entry)


@contextlib.contextmanager
def actor_context(user):
    """Attribute everything written inside this block to `user`, as one entry
    per object.

    Used by the background threads, which don't inherit the request's context.
    """
    token = set_actor(user)
    try:
        with collecting():
            yield
    finally:
        _current_actor.reset(token)


class AuditActorMiddleware:
    """Make the requesting user available to the audit signal handlers.

    Must sit after AuthenticationMiddleware, which is what puts request.user
    there in the first place.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = set_actor(getattr(request, 'user', None))
        try:
            # One request is one edit, however many saves it takes.
            with collecting():
                return self.get_response(request)
        finally:
            _current_actor.reset(token)


class Tracked:
    """How one model is written into the log.

    fields: the fields worth diffing, in the order they should be shown.
    url:    instance -> a link to the thing, or '' when there's nothing to
            link to. Recipe links survive a later rename because retired
            slugs redirect (see RecipeSlugHistory).
    """

    def __init__(self, label, fields, url=None):
        self.label = label
        self.fields = fields
        self.url = url


def _recipe_url(recipe):
    try:
        return recipe.get_absolute_url()
    except Exception:
        return ''


# Which models are audited, and what's worth recording about each. Anything not
# listed here is invisible to the log -- notably AuditLog itself, which would
# recurse. UserProfile is tracked for its role and household alone; the rest of
# it is personal state (a dismissed popup, a timezone) not worth a line.
REGISTRY = {
    Recipe: Tracked(
        'Recipe',
        (
            'name', 'description', 'complexity', 'effort', 'health', 'rating',
            'gluten_free', 'dairy_free', 'vegetarian', 'vegan', 'url',
            'instructions', 'ingredients', 'notes', 'image', 'is_public',
            'share_notes',
        ),
        url=_recipe_url,
    ),
    Tag: Tracked('Tag', ('label', 'value'), url=lambda t: t.get_recipe_search_url()),
    MealPlan: Tracked('Meal plan', ('date', 'slot', 'recipe', 'custom_text', 'user', 'group')),
    MealPlanGroup: Tracked('Household', ('name',)),
    WorkspaceItem: Tracked('Workspace item', ('recipe', 'custom_text', 'user', 'group')),
    FreezerItem: Tracked('Freezer item', ('recipe', 'custom_text', 'user', 'group')),
    Favorite: Tracked('Favorite', ('user', 'recipe'), url=lambda f: _recipe_url(f.recipe) if f.recipe_id else ''),
    User: Tracked(
        'User',
        ('username', 'first_name', 'last_name', 'email', 'password',
         'is_active', 'is_staff', 'is_superuser'),
    ),
    AuditSettings: Tracked('Audit settings', ('retention_days',)),
    ApiSettings: Tracked('API settings', ('idle_timeout_minutes',)),
    Role: Tracked('Role', ('name', 'description', 'permissions')),
    UserProfile: Tracked('User profile', ('role', 'household')),
}


def _serialize(value):
    """Reduce a field value to something JSON can hold and a person can read.

    Returns the value at full length; callers decide how much of it to keep.
    """
    if value is None or isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return value
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    text = str(value)
    # A textarea posts CRLF where the database holds LF, so a recipe saved
    # without touching its instructions would otherwise report every long field
    # as changed. Compare the text, not the line endings.
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _field_value(instance, name):
    """Current value of one field, tolerant of a related row having gone away."""
    try:
        return _serialize(getattr(instance, name, None))
    except ObjectDoesNotExist:
        return None


def _field_label(model, name):
    try:
        return model._meta.get_field(name).verbose_name.capitalize()
    except Exception:
        return name.replace('_', ' ').capitalize()


def _changed_fields(model, old, new, fields):
    """The display labels of the fields that actually differ.

    Values are compared, but never kept: the log says a recipe's instructions
    were edited, not what they now say.
    """
    changed = set()
    for name in fields:
        if _field_value(old, name) != _field_value(new, name):
            changed.add(_field_label(model, name))
    return changed


def _object_type(instance):
    tracked = REGISTRY.get(type(instance))
    return tracked.label if tracked else type(instance).__name__


def _object_label(instance):
    try:
        return _serialize(str(instance))[:255]
    except Exception:
        return ''


def _object_url(instance):
    tracked = REGISTRY.get(type(instance))
    if not tracked or not tracked.url:
        return ''
    try:
        return (tracked.url(instance) or '')[:500]
    except Exception:
        return ''


def record(action, instance, fields=None, tags_added=None, tags_removed=None):
    """Note a change. Buffered when a request is collecting, written otherwise.

    Never raises -- a failed recording must not fail the save that caused it.
    """
    try:
        object_type = _object_type(instance)
        buffer = _pending.get()

        if buffer is None:
            # No request or task around this save (a management command, the
            # shell, a test touching models directly): write it straight out.
            _write({
                'action': action,
                'object_type': object_type,
                'object_id': instance.pk,
                'object_label': _object_label(instance),
                'object_url': _object_url(instance),
                'fields': set(fields or ()),
                'tags_added': set(tags_added or ()),
                'tags_removed': set(tags_removed or ()),
                'actor': _actor_for(action, instance),
            })
            return

        entry = buffer.touch(action, object_type, instance)
        entry['fields'].update(fields or ())
        entry['tags_added'].update(tags_added or ())
        entry['tags_removed'].update(tags_removed or ())
        if entry['actor'] is None:
            entry['actor'] = _actor_for(action, instance)
    except Exception:
        logger.exception('Failed to record audit entry for %r', instance)


def _actor_for(action, instance):
    actor = get_actor()
    # A user created with nobody logged in signed themselves up.
    if actor is None and isinstance(instance, User) and action == AuditLog.ACTION_CREATED:
        return instance
    return actor


def _write(entry):
    """Turn one collected entry into a row, or drop it if nothing survived."""
    try:
        fields = set(entry['fields'])

        # Tags removed and added back in the same request never changed. This
        # is what stops RecipeForm.save()'s clear-then-re-add from showing up.
        if entry['tags_added'] ^ entry['tags_removed']:
            fields.add(TAGS_LABEL)

        if entry['action'] == AuditLog.ACTION_UPDATED and not fields:
            return

        actor = entry['actor']
        AuditLog.objects.create(
            actor=actor if (actor is not None and actor.pk) else None,
            actor_label=str(actor) if actor is not None else '',
            action=entry['action'],
            object_type=entry['object_type'],
            object_id=entry['object_id'],
            object_label=entry['object_label'],
            object_url=entry['object_url'],
            changes={'fields': sorted(fields)} if fields else {},
        )
    except Exception:
        logger.exception('Failed to write audit log entry %r', entry)


def _capture_previous_state(sender, instance, raw=False, **kwargs):
    """Stash the pre-save row so post_save can diff against it."""
    if raw or sender not in REGISTRY or instance.pk is None:
        return
    try:
        instance._audit_previous = sender.objects.filter(pk=instance.pk).first()
    except Exception:
        instance._audit_previous = None


def _log_save(sender, instance, created, raw=False, **kwargs):
    if raw or sender not in REGISTRY:
        return
    tracked = REGISTRY[sender]

    if created:
        record(AuditLog.ACTION_CREATED, instance)
        return

    previous = getattr(instance, '_audit_previous', None)
    if previous is None:
        # An update we couldn't read the "before" of -- still worth recording.
        record(AuditLog.ACTION_UPDATED, instance)
        return

    changed = _changed_fields(sender, previous, instance, tracked.fields)
    if not changed:
        # A save that touched nothing we track (auto_now timestamps, a slug
        # regenerated to the same value) isn't worth a line in the log.
        return
    record(AuditLog.ACTION_UPDATED, instance, fields=changed)


def _log_delete(sender, instance, **kwargs):
    if sender not in REGISTRY:
        return
    record(AuditLog.ACTION_DELETED, instance)


def _tag_names(tag_ids):
    return sorted(str(tag) for tag in Tag.objects.filter(pk__in=tag_ids))


def _log_recipe_tags(sender, instance, action, pk_set, reverse, **kwargs):
    """Note tag add/remove against the recipe they happened to.

    Which tags moved is collected rather than reported: a request that removes
    a tag and puts it straight back changed nothing, and _write() nets the two
    out. Only the fact that tags moved reaches the log.
    """
    if action == 'pre_clear':
        # post_clear arrives with the rows already gone, so snapshot them now.
        try:
            if reverse:
                instance._audit_cleared = list(instance.recipes.all())
            else:
                instance._audit_cleared = _tag_names(instance.tags.values_list('pk', flat=True))
        except Exception:
            instance._audit_cleared = []
        return

    if action not in ('post_add', 'post_remove', 'post_clear'):
        return

    try:
        if action == 'post_clear':
            cleared = getattr(instance, '_audit_cleared', [])
            if reverse:
                for recipe in cleared:
                    _record_tag_change(recipe, removed=[str(instance)])
            elif cleared:
                _record_tag_change(instance, removed=cleared)
            return

        key = 'added' if action == 'post_add' else 'removed'
        if reverse:
            # instance is a Tag; pk_set holds the recipes it moved on or off.
            for recipe in Recipe.objects.filter(pk__in=pk_set or []):
                _record_tag_change(recipe, **{key: [str(instance)]})
        else:
            names = _tag_names(pk_set or [])
            if names:
                _record_tag_change(instance, **{key: names})
    except Exception:
        logger.exception('Failed to record recipe tag change')


def _record_tag_change(recipe, added=None, removed=None):
    record(
        AuditLog.ACTION_UPDATED, recipe,
        tags_added=added or (), tags_removed=removed or (),
    )


def connect_signals():
    """Wire the handlers up. Called once from MainConfig.ready().

    Connected per registered model rather than globally, so saves of anything
    unaudited -- sessions, the audit log itself -- never reach this module.
    """
    for model in REGISTRY:
        dispatch_uid = f'audit_{model._meta.label_lower}'
        pre_save.connect(_capture_previous_state, sender=model, dispatch_uid=dispatch_uid)
        post_save.connect(_log_save, sender=model, dispatch_uid=dispatch_uid)
        post_delete.connect(_log_delete, sender=model, dispatch_uid=dispatch_uid)

    m2m_changed.connect(
        _log_recipe_tags,
        sender=Recipe.tags.through,
        dispatch_uid='audit_recipe_tags',
    )
