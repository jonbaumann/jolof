"""API endpoints. Read-only for now, apart from logging in and out.

Each view names the permission it needs in `required_permission`, which
permissions.RolePermission checks -- the API counterpart of @require.
"""
from datetime import date, timedelta

from django.contrib.auth import authenticate
from django.db.models import Count, Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import exceptions, generics, serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import permissions as perms
from .. import planner, recipe_query
from ..models import ApiSession, ApiSettings, Favorite, MealPlan, Recipe, Tag, UserProfile
from . import serializers as s
from .authentication import create_session
from .exceptions import api_error
from .permissions import ANY_API
from .throttling import LoginThrottle


def _session_payload(session, user):
    api_settings = ApiSettings.load()
    return {
        'session': session,
        'settings': api_settings,
        'permissions': sorted(perms.api_permissions(user)),
        'expires_at': session.expires_at(api_settings),
    }


# --- Auth --------------------------------------------------------------------

class Login(APIView):
    """Swap a username and password for a session token."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    @extend_schema(request=s.LoginSerializer, responses={201: s.LoginResponseSerializer},
                   auth=[], tags=['auth'])
    def post(self, request):
        form = s.LoginSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        # authenticate() refuses inactive accounts as well as wrong passwords.
        user = authenticate(request, username=form.validated_data['username'],
                            password=form.validated_data['password'])
        if user is None:
            return Response(api_error('invalid_credentials', 'Wrong username or password.'),
                            status=status.HTTP_400_BAD_REQUEST)
        if not perms.has_any_api_perm(user):
            return Response(api_error('no_api_access', "Your role doesn't include API access."),
                            status=status.HTTP_403_FORBIDDEN)
        if UserProfile.objects.filter(user=user, must_change_password=True).exists():
            return Response(api_error('password_change_required',
                                      'Log in to Jolof in a browser and change your password first.'),
                            status=status.HTTP_403_FORBIDDEN)

        ApiSession.prune()
        session, token = create_session(user, request, form.validated_data.get('client_name', ''))
        payload = _session_payload(session, user)
        payload['token'] = token
        return Response(s.LoginResponseSerializer(payload).data, status=status.HTTP_201_CREATED)


class Logout(APIView):
    """End the session whose token made this request."""

    required_permission = ANY_API

    @extend_schema(request=None, responses={204: None}, tags=['auth'])
    def post(self, request):
        request.auth.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SessionInfo(APIView):
    """Who the token belongs to, what it can do, and when it runs out."""

    required_permission = ANY_API

    @extend_schema(responses=s.SessionSerializer, tags=['auth'])
    def get(self, request):
        return Response(s.SessionSerializer(_session_payload(request.auth, request.user)).data)


# --- Recipes -----------------------------------------------------------------

def _flag(request, name):
    return request.query_params.get(name, '').lower() in ('1', 'true', 'yes', 'on')


def _int_param(request, name, low=1, high=5):
    raw = request.query_params.get(name)
    if raw in (None, ''):
        return None
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is None or not low <= value <= high:
        raise exceptions.ValidationError({name: [f'Must be a whole number from {low} to {high}.']})
    return value


RECIPE_VIEWS = tuple(s.RECIPE_SERIALIZERS)
# A names-only page is tiny, so it can be much longer.
NAMES_MAX_PAGE_SIZE = 500

BOOL_FILTERS = ('cooked', 'favorites', 'public', 'vegetarian', 'vegan', 'gluten_free', 'dairy_free')
RATING_FILTERS = {
    'min_rating': 'rating__gte',
    'min_health': 'health__gte',
    'max_complexity': 'complexity__lte',
    'max_effort': 'effort__lte',
}


@extend_schema(
    tags=['recipes'],
    parameters=[
        OpenApiParameter('view', str, enum=RECIPE_VIEWS, default='summary',
                         description='names: id and name only (up to 500 a page). summary: no '
                                     'ingredients, instructions or notes. full: everything.'),
        OpenApiParameter('q', str, description='Search like the recipes page: every word must '
                         'match the name, ingredients, description or a tag. label:value matches a tag.'),
        OpenApiParameter('tag', str, many=True, description='Exact tag as label:value; repeat to '
                         'require several.'),
        *[OpenApiParameter(name, bool, description=desc) for name, desc in (
            ('cooked', 'Only recipes tagged "cooked".'),
            ('favorites', "Only the caller's favorites."),
            ('public', 'Only recipes with a public share link.'),
            ('vegetarian', 'Only vegetarian recipes.'),
            ('vegan', 'Only vegan recipes.'),
            ('gluten_free', 'Only gluten-free recipes.'),
            ('dairy_free', 'Only dairy-free recipes.'),
        )],
        *[OpenApiParameter(name, int, description='1 to 5.') for name in RATING_FILTERS],
        OpenApiParameter('ids', str, description='Comma-separated recipe ids, e.g. 3,17,42.'),
        OpenApiParameter('sort', str, enum=recipe_query.SORT_KEYS, default=recipe_query.DEFAULT_SORT),
    ],
)
class RecipeList(generics.ListAPIView):
    """Search, filter, sort and page through recipes."""

    required_permission = perms.API_RECIPE_READ

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.recipe_view = request.query_params.get('view', 'summary')
        if self.recipe_view not in RECIPE_VIEWS:
            raise exceptions.ValidationError({'view': [f'Must be one of: {", ".join(RECIPE_VIEWS)}.']})
        if self.recipe_view == 'names':
            request.api_max_page_size = NAMES_MAX_PAGE_SIZE

    def get_serializer_class(self):
        return s.RECIPE_SERIALIZERS[getattr(self, 'recipe_view', 'summary')]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['favorite_ids'] = self.favorite_ids
        return context

    @property
    def favorite_ids(self):
        if not hasattr(self, '_favorite_ids'):
            self._favorite_ids = set(
                Favorite.objects.filter(user=self.request.user).values_list('recipe_id', flat=True))
        return self._favorite_ids

    def get_queryset(self):
        request = self.request
        params = request.query_params

        sort_by = params.get('sort', recipe_query.DEFAULT_SORT)
        if not recipe_query.is_sort_key(sort_by):
            raise exceptions.ValidationError(
                {'sort': [f'Must be one of: {", ".join(recipe_query.SORT_KEYS)}.']})

        qs = recipe_query.filter_recipes(
            query=params.get('q', ''),
            cooked_only=_flag(request, 'cooked'),
            favorites_only=_flag(request, 'favorites'),
            public_only=_flag(request, 'public'),
            favorite_ids=self.favorite_ids,
        )
        for flag in ('vegetarian', 'vegan', 'gluten_free', 'dairy_free'):
            if _flag(request, flag):
                qs = qs.filter(**{flag: True})
        for name, lookup in RATING_FILTERS.items():
            value = _int_param(request, name)
            if value is not None:
                qs = qs.filter(**{lookup: value})
        for tag in params.getlist('tag'):
            label, sep, value = tag.partition(':')
            if not sep or not label or not value:
                raise exceptions.ValidationError({'tag': ['Each tag must be label:value, e.g. cuisine:thai.']})
            qs = qs.filter(tags__label__iexact=label, tags__value__iexact=value)
        if params.get('ids'):
            try:
                ids = [int(part) for part in params['ids'].split(',') if part.strip()]
            except ValueError:
                raise exceptions.ValidationError({'ids': ['Must be comma-separated recipe ids.']})
            qs = qs.filter(id__in=ids)

        workspace_ids = ()
        if sort_by.startswith('in-planner'):
            workspace_ids = set(planner.workspace_items(request.user).values_list('recipe_id', flat=True))
        qs = recipe_query.sort_recipes(qs, sort_by, favorite_ids=self.favorite_ids,
                                       workspace_ids=workspace_ids)
        if self.recipe_view != 'names':
            qs = qs.prefetch_related('tags')
        return qs


@extend_schema(tags=['recipes'], responses=s.RecipeFullSerializer)
class RecipeDetail(generics.RetrieveAPIView):
    """One recipe, in full."""

    required_permission = perms.API_RECIPE_READ
    serializer_class = s.RecipeFullSerializer
    queryset = Recipe.objects.prefetch_related('tags')

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['favorite_ids'] = set(
            Favorite.objects.filter(user=self.request.user, recipe_id=self.kwargs['pk'])
            .values_list('recipe_id', flat=True))
        return context


@extend_schema(tags=['recipes'], parameters=[
    OpenApiParameter('q', str, description='Matches the label or value.'),
])
class TagList(generics.ListAPIView):
    """Every tag, with how many recipes carry it -- the values worth filtering on."""

    required_permission = perms.API_RECIPE_READ
    serializer_class = s.TagSerializer

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        request.api_default_page_size = 100
        request.api_max_page_size = 500

    def get_queryset(self):
        qs = Tag.objects.annotate(recipe_count=Count('recipes', distinct=True))
        query = self.request.query_params.get('q', '').strip()
        if query:
            qs = qs.filter(Q(label__icontains=query) | Q(value__icontains=query))
        return qs.order_by('label', 'value', 'id')


# --- Meal planner ------------------------------------------------------------

MEAL_PLAN_DEFAULT_DAYS = 14
MEAL_PLAN_MAX_DAYS = 92


def _household(user):
    group = planner.get_user_group(user)
    return s.HouseholdSerializer(group).data if group else None


def _date_param(request, name, default):
    raw = request.query_params.get(name)
    if not raw:
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise exceptions.ValidationError({name: ['Must be a date like 2026-10-22.']})


household_field = inline_serializer('Household', {'id': serializers.IntegerField(),
                                                  'name': serializers.CharField()}, allow_null=True)


@extend_schema(
    tags=['meal planner'],
    parameters=[
        OpenApiParameter('start', OpenApiTypes.DATE, description='Default: today.'),
        OpenApiParameter('end', OpenApiTypes.DATE,
                         description=f'Inclusive. Default: {MEAL_PLAN_DEFAULT_DAYS} days from start. '
                                     f'At most {MEAL_PLAN_MAX_DAYS} days after start.'),
    ],
    responses=inline_serializer('MealPlan', {
        'household': household_field,
        'start': serializers.DateField(),
        'end': serializers.DateField(),
        'entries': s.MealPlanEntrySerializer(many=True),
    }),
)
class MealPlanList(APIView):
    """What's planned for each day, for the caller's household (or their own planner)."""

    required_permission = perms.API_MEALPLAN_READ

    def get(self, request):
        start = _date_param(request, 'start', date.today())
        end = _date_param(request, 'end', start + timedelta(days=MEAL_PLAN_DEFAULT_DAYS - 1))
        if end < start:
            raise exceptions.ValidationError({'end': ['Must not be before start.']})
        if (end - start).days >= MEAL_PLAN_MAX_DAYS:
            raise exceptions.ValidationError({'end': [f'At most {MEAL_PLAN_MAX_DAYS} days after start.']})

        entries = (MealPlan.objects.filter(**planner.owner_filter(request.user),
                                           date__range=[start, end])
                   .select_related('recipe').order_by('date', 'slot'))
        return Response({
            'household': _household(request.user),
            'start': start.isoformat(),
            'end': end.isoformat(),
            'entries': s.MealPlanEntrySerializer(entries, many=True).data,
        })


class _PlannerItems(APIView):
    required_permission = perms.API_MEALPLAN_READ
    items = None
    serializer_class = None

    def get(self, request):
        return Response({
            'household': _household(request.user),
            'items': self.serializer_class(type(self).items(request.user), many=True).data,
        })


@extend_schema(tags=['meal planner'], responses=inline_serializer('Workspace', {
    'household': household_field,
    'items': s.WorkspaceItemSerializer(many=True),
}))
class WorkspaceList(_PlannerItems):
    """Recipes and notes staged in the planner's workspace, newest first."""

    items = planner.workspace_items
    serializer_class = s.WorkspaceItemSerializer


@extend_schema(tags=['meal planner'], responses=inline_serializer('Freezer', {
    'household': household_field,
    'items': s.FreezerItemSerializer(many=True),
}))
class FreezerList(_PlannerItems):
    """What's in the freezer, newest first."""

    items = planner.freezer_items
    serializer_class = s.FreezerItemSerializer
