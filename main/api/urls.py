from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from . import views

urlpatterns = [
    path('auth/login/', views.Login.as_view(), name='api-login'),
    path('auth/logout/', views.Logout.as_view(), name='api-logout'),
    path('auth/session/', views.SessionInfo.as_view(), name='api-session'),
    path('recipes/', views.RecipeList.as_view(), name='api-recipes'),
    path('recipes/<int:pk>/', views.RecipeDetail.as_view(), name='api-recipe'),
    path('tags/', views.TagList.as_view(), name='api-tags'),
    path('meal-plan/', views.MealPlanList.as_view(), name='api-meal-plan'),
    path('workspace/', views.WorkspaceList.as_view(), name='api-workspace'),
    path('freezer/', views.FreezerList.as_view(), name='api-freezer'),
    path('schema/', SpectacularAPIView.as_view(), name='api-schema'),
    path('docs/', SpectacularSwaggerView.as_view(url_name='api-schema'), name='api-docs'),
]
