"""Whose meal planner a user sees: their household's, or their own.

Shared by the planner pages and the API, so both scope it the same way.
"""
from .models import FreezerItem, MealPlanGroup, WorkspaceItem


def get_user_group(user):
    """The user's household, or None when they have a planner of their own."""
    return MealPlanGroup.objects.filter(profiles__user=user).first()


def owner_filter(user):
    """Filter kwargs selecting the user's planner rows (meal plans, workspace, freezer)."""
    group = get_user_group(user)
    return {'group': group} if group else {'user': user, 'group': None}


def workspace_items(user):
    return WorkspaceItem.objects.filter(**owner_filter(user)).select_related('recipe')


def freezer_items(user):
    # Freezer items are scoped identically to workspace items (group-or-personal).
    return FreezerItem.objects.filter(**owner_filter(user)).select_related('recipe')
