"""How API responses look.

Recipes come in three sizes, picked with ?view= on the list endpoint, so a
client (an AI agent, say) can scan the whole library cheaply and only pull
the long fields -- ingredients, instructions, notes -- for the recipes it
wants.
"""
from typing import Optional

from django.urls import reverse
from rest_framework import serializers

from ..models import FreezerItem, MealPlan, MealPlanGroup, Recipe, Tag, WorkspaceItem


class RecipeNameSerializer(serializers.ModelSerializer):
    class Meta:
        model = Recipe
        fields = ['id', 'name']


class RecipeSummarySerializer(serializers.ModelSerializer):
    # Dietary flags are tick boxes: unset means "no", never "unknown".
    gluten_free = serializers.SerializerMethodField()
    dairy_free = serializers.SerializerMethodField()
    vegetarian = serializers.SerializerMethodField()
    vegan = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField(help_text='Tags as "label:value" strings.')
    is_favorite = serializers.SerializerMethodField(help_text="In the caller's favorites.")

    class Meta:
        model = Recipe
        fields = [
            'id', 'name', 'slug', 'description',
            'rating', 'complexity', 'effort', 'health',
            'gluten_free', 'dairy_free', 'vegetarian', 'vegan',
            'tags', 'is_favorite', 'is_public',
        ]

    def get_gluten_free(self, obj) -> bool:
        return bool(obj.gluten_free)

    def get_dairy_free(self, obj) -> bool:
        return bool(obj.dairy_free)

    def get_vegetarian(self, obj) -> bool:
        return bool(obj.vegetarian)

    def get_vegan(self, obj) -> bool:
        return bool(obj.vegan)

    def get_tags(self, obj) -> list[str]:
        return [f'{tag.label}:{tag.value}' for tag in obj.tags.all()]

    def get_is_favorite(self, obj) -> bool:
        return obj.id in self.context.get('favorite_ids', ())


class RecipeFullSerializer(RecipeSummarySerializer):
    source_url = serializers.CharField(source='url', allow_null=True,
                                       help_text='Where the recipe came from.')
    image_url = serializers.SerializerMethodField()
    web_url = serializers.SerializerMethodField(help_text='The recipe page in Jolof.')

    class Meta(RecipeSummarySerializer.Meta):
        fields = RecipeSummarySerializer.Meta.fields + [
            'ingredients', 'instructions', 'notes',
            'source_url', 'image_url', 'web_url', 'created_at', 'updated_at',
        ]

    def get_image_url(self, obj) -> Optional[str]:
        return obj.image.url if obj.image else None

    def get_web_url(self, obj) -> str:
        path = reverse('recipe-detail', args=[obj.slug])
        request = self.context.get('request')
        return request.build_absolute_uri(path) if request else path


RECIPE_SERIALIZERS = {
    'names': RecipeNameSerializer,
    'summary': RecipeSummarySerializer,
    'full': RecipeFullSerializer,
}


class TagSerializer(serializers.ModelSerializer):
    recipe_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Tag
        fields = ['id', 'label', 'value', 'recipe_count']


class RecipeRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Recipe
        fields = ['id', 'name', 'slug']


class HouseholdSerializer(serializers.ModelSerializer):
    class Meta:
        model = MealPlanGroup
        fields = ['id', 'name']


class MealPlanEntrySerializer(serializers.ModelSerializer):
    name = serializers.CharField(source='display_name')
    recipe = RecipeRefSerializer(allow_null=True)

    class Meta:
        model = MealPlan
        fields = ['id', 'date', 'slot', 'name', 'recipe', 'custom_text']


class WorkspaceItemSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source='display_name')
    recipe = RecipeRefSerializer(allow_null=True)

    class Meta:
        model = WorkspaceItem
        fields = ['id', 'name', 'recipe', 'custom_text', 'created_at']


class FreezerItemSerializer(WorkspaceItemSerializer):
    class Meta(WorkspaceItemSerializer.Meta):
        model = FreezerItem


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(style={'input_type': 'password'}, trim_whitespace=False)
    client_name = serializers.CharField(
        required=False, allow_blank=True, max_length=100,
        help_text='Shown on the session lists, e.g. "jolof-mcp".')


class SessionSerializer(serializers.Serializer):
    """The caller's own session, as login and auth/session/ describe it."""
    user = serializers.SerializerMethodField()
    permissions = serializers.ListField(child=serializers.CharField())
    token_prefix = serializers.CharField(source='session.token_prefix')
    client_name = serializers.CharField(source='session.client_name')
    created_at = serializers.DateTimeField(source='session.created_at')
    last_used_at = serializers.DateTimeField(source='session.last_used_at')
    idle_timeout_minutes = serializers.IntegerField(source='settings.idle_timeout_minutes',
                                                    help_text='0 means no idle timeout.')
    expires_at = serializers.DateTimeField(
        allow_null=True, help_text='When the session ends if it goes unused; each request pushes it back. '
                                   'Null when there is no idle timeout.')

    def get_user(self, obj) -> dict:
        user = obj['session'].user
        return {'id': user.id, 'username': user.username}


class LoginResponseSerializer(SessionSerializer):
    token = serializers.CharField(help_text='Send as "Authorization: Bearer <token>". Shown only once.')
