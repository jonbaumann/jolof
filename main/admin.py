from django.contrib import admin
from .models import Recipe, Role, Tag, MealPlanGroup, UserProfile

admin.site.register(Recipe)
admin.site.register(Tag)
admin.site.register(MealPlanGroup)


# Roles are managed in the app; these are here as a backstop for emergencies.
@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ('name', 'system_key')
    readonly_fields = ('system_key',)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'household')
    list_editable = ('role', 'household')
