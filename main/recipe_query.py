"""Filtering and sorting the recipe library.

Shared by the recipes page and the API so the two can't drift apart: a sort
key or filter means the same thing in both. Everything happens in the
database, so a page of results doesn't mean loading the whole library.
"""
from django.db.models import BooleanField, Case, Count, F, Q, Value, When
from django.db.models.functions import Lower

from .models import Recipe


def build_recipe_search_q(query):
    """Build the Q filter for a recipe search string.

    Each whitespace-separated term must match (AND across terms), where a term
    matches if it appears in the name, ingredients, description, or a tag's
    label/value (OR across fields). A term containing ':' is also treated as a
    ``label:value`` (or bare ``label:``) tag filter. Shared by the recipes page,
    the meal-planner recipe picker and the API so their search stays in sync.
    """
    exact_q = Q()
    for term in query.split():
        term_q = (
            Q(name__icontains=term) |
            Q(ingredients__icontains=term) |
            Q(description__icontains=term) |
            Q(tags__label__icontains=term) |
            Q(tags__value__icontains=term)
        )
        exact_q &= term_q

        # Check for tags
        if ":" in term:
            # Remove empty strings
            tag_terms = [x for x in term.split(':') if x]

            # User entered a label:value pair (e.g., cuisine:asian)
            if len(tag_terms) > 1:
                two_term_q = Q()
                for index, each_term in enumerate(tag_terms):
                    # Match first term to label and second term to value
                    if index == 0:
                        tag_q = Q(tags__label__icontains=each_term)
                    else:
                        tag_q = Q(tags__value__icontains=each_term)
                    two_term_q &= tag_q
                exact_q |= two_term_q
            else:
                # User entered just a label (e.g., cuisine:)
                for each_term in tag_terms:
                    exact_q |= Q(tags__label__icontains=each_term)

    return exact_q


def filter_recipes(qs=None, *, query='', cooked_only=False, favorites_only=False,
                   public_only=False, favorite_ids=()):
    """The recipe filters the recipes page offers, applied to `qs`."""
    if qs is None:
        qs = Recipe.objects.all()
    if query:
        qs = qs.filter(build_recipe_search_q(query)).distinct()
    if cooked_only:
        qs = qs.filter(tags__value__iexact='cooked').distinct()
    if favorites_only:
        qs = qs.filter(id__in=favorite_ids)
    if public_only:
        qs = qs.filter(is_public=True)
    return qs


def _member_first(ids, descending):
    """Orders recipes by whether their id is in `ids` (in it first when descending).

    With no ids every recipe ties, so it adds no ordering at all -- Django
    would otherwise boil the Case down to a constant, which Postgres refuses
    in an ORDER BY.
    """
    ids = [i for i in ids if i is not None]
    if not ids:
        return ()
    member = Case(When(id__in=ids, then=Value(True)), default=Value(False),
                  output_field=BooleanField())
    return (member.desc() if descending else member.asc(),)


# Every sort the recipes page offers, as the ordering it stands for. Unrated
# recipes always sort last, whichever way a rating is sorted. Each ends in
# name then id, so ties come out alphabetically and pages never overlap.
_NAME = (Lower('name'), 'id')
_SORTS = {
    'recipe-name': lambda ctx: (),
    'recently-added': lambda ctx: ('-created_at',),
    'highest-rating': lambda ctx: (F('rating').desc(nulls_last=True),),
    'most-tags': lambda ctx: ('-sort_tag_count',),
    'complexity-asc': lambda ctx: (F('complexity').asc(nulls_last=True),),
    'complexity-desc': lambda ctx: (F('complexity').desc(nulls_last=True),),
    'effort-asc': lambda ctx: (F('effort').asc(nulls_last=True),),
    'effort-desc': lambda ctx: (F('effort').desc(nulls_last=True),),
    'healthiness-asc': lambda ctx: (F('health').asc(nulls_last=True),),
    'healthiness-desc': lambda ctx: (F('health').desc(nulls_last=True),),
    'in-planner-desc': lambda ctx: _member_first(ctx['workspace_ids'], descending=True),
    'in-planner-asc': lambda ctx: _member_first(ctx['workspace_ids'], descending=False),
    'favorites-desc': lambda ctx: _member_first(ctx['favorite_ids'], descending=True),
    'favorites-asc': lambda ctx: _member_first(ctx['favorite_ids'], descending=False),
    'public-desc': lambda ctx: ('-is_public',),
    'public-asc': lambda ctx: ('is_public',),
}
# Older name for healthiness-desc, still found in saved cookies.
_ALIASES = {'healthiness': 'healthiness-desc'}

SORT_KEYS = tuple(_SORTS)
DEFAULT_SORT = 'recipe-name'


def is_sort_key(sort_by):
    return sort_by in _SORTS or sort_by in _ALIASES


def sort_recipes(qs, sort_by, *, favorite_ids=(), workspace_ids=()):
    """`qs` in the order `sort_by` names; an unknown key sorts by name."""
    sort_by = _ALIASES.get(sort_by, sort_by)
    if sort_by not in _SORTS:
        sort_by = DEFAULT_SORT
    # Re-select by id first: a search joins tags, and counting tags across
    # that join would count only the ones that matched.
    qs = Recipe.objects.filter(pk__in=qs.values('pk'))
    if sort_by == 'most-tags':
        qs = qs.annotate(sort_tag_count=Count('tags', distinct=True))
    ctx = {'favorite_ids': favorite_ids, 'workspace_ids': workspace_ids}
    return qs.order_by(*_SORTS[sort_by](ctx), *_NAME)
