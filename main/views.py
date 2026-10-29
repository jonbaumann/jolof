from django.shortcuts import render, redirect, get_object_or_404
from django.conf import settings
from .forms import AdminSetPasswordForm, ApiSettingsForm, AuditRetentionForm, HouseholdForm, PreferencesForm, RoleForm, UserAddForm, UserEditForm, UserProfileEditForm, RecipeForm, CSVUploadForm, CSVImportForm, CSVExportForm, URLScrapeFormBatch
from . import audit, csv_export, csv_fields, csv_import, planner, recipe_query, safe_fetch
from .recipe_query import build_recipe_search_q
from . import permissions as perms
from .permissions import require
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.views import PasswordChangeView
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.http import HttpResponse, JsonResponse, Http404
from django.urls import reverse
from django.db import connection
from django.db.models import Q, Max, Count
from django.db.models.functions import Lower
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.core.files.base import ContentFile

from main.models import ApiSession, ApiSettings, AuditLog, AuditSettings, Recipe, RecipeSlugHistory, Role, Tag, MealPlan, MealPlanGroup, WorkspaceItem, FreezerItem, UserProfile, Favorite
from django.core.cache import cache
from bs4 import BeautifulSoup
from io import BytesIO
from PIL import Image

import os
import base64
import hashlib
import json
import logging
import secrets
import time
import requests
import threading
import re
from datetime import date as date_class, timedelta
from urllib.parse import urlencode
from collections import defaultdict
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


@login_required
def profile(request):
    profile_obj, _ = UserProfile.objects.get_or_create(user=request.user)
    return render(request, 'main/profile.html', {
        'profile_role': profile_obj.role,
        'profile_household': profile_obj.household,
        'has_api_access': perms.has_any_api_perm(request.user),
        **_api_sessions_context(request, own=True),
        'app_version': settings.VERSION,
    })


def _api_sessions_context(request, own):
    """The live API sessions for the Profile page (yours) or Platform Settings (everyone's)."""
    ApiSession.prune()
    api_settings = ApiSettings.load()
    sessions = ApiSession.objects.select_related('user')
    if own:
        sessions = sessions.filter(user=request.user)
    sessions = list(sessions)
    for session in sessions:
        session.ends = session.expires_at(api_settings)
    return {
        'api_sessions': sessions,
        'api_sessions_own': own,
        'api_session_revoke_url': 'profile-api-session-revoke' if own else 'api-session-revoke',
    }


@require(perms.ANY_API)
def api_reference(request):
    """How to use the REST API, with examples, for anyone whose role includes it."""
    my_permissions = perms.api_permissions(request.user)
    return render(request, 'main/api_reference.html', {
        'api_base': request.build_absolute_uri('/api/v1/'),
        'idle_timeout_minutes': ApiSettings.load().idle_timeout_minutes,
        'can_api_recipes': perms.API_RECIPE_READ in my_permissions,
        'can_api_mealplan': perms.API_MEALPLAN_READ in my_permissions,
    })


@login_required
def profile_api_session_revoke(request, pk):
    """End one of your own API sessions."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    get_object_or_404(ApiSession, pk=pk, user=request.user).delete()
    return render(request, 'partials/api_sessions.html', _api_sessions_context(request, own=True))

@login_required
def profile_edit(request):
    if request.method == 'POST':
        form = UserProfileEditForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            return redirect('profile')
    else:
        form = UserProfileEditForm(instance=request.user)

    return render(request, 'main/profile_edit.html', {"form": form, 'app_version': settings.VERSION})

@login_required
def preferences(request):
    profile_obj, _ = UserProfile.objects.get_or_create(user=request.user)
    form = PreferencesForm(request.POST or None, instance=profile_obj)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Preferences saved.')
        # Redirect rather than render, so the new timezone is active for the
        # page that shows it and the new theme is applied from the start.
        return redirect('preferences')
    return render(request, 'main/preferences.html', {'form': form})


@login_required
def theme_adopt(request):
    """Save the theme this browser was already using, the first time it's seen.

    Before themes were a preference, the navbar toggle kept them in
    localStorage. base.html posts that here once, while the profile has none,
    so upgrading doesn't flip anyone's theme. Never overwrites a real choice.
    """
    if request.method != 'POST':
        return HttpResponse(status=405)
    theme = request.POST.get('theme', '')
    if theme in dict(UserProfile.THEME_CHOICES):
        UserProfile.objects.filter(user=request.user, theme='').update(theme=theme)
    return HttpResponse(status=204)


@require(perms.RECIPE_READ)
def recipes(request):
    num_search_results = 0

    # Get results per page. Check the request first, then check for cookie
    if request.GET.get('results_per_page'):
        results_per_page = int(request.GET.get('results_per_page', '8'))
    else:
        results_per_page = int(request.COOKIES.get('results_per_page', 8))

    # Get the view. Check the request first, then check for cookie
    if request.GET.get('view'):
        view = request.GET.get('view')
    else:
        view = request.COOKIES.get('view', 'grid')

    # Get the sort method. Check the request first, then check for cookie
    if request.GET.get('sort_by'):
        sort_by = request.GET.get('sort_by')
    else:
        sort_by = request.COOKIES.get('sort_by', 'recipe-name')

    # Get the cooked-only filter. Check the request first, then check for cookie
    if 'cooked_only' in request.GET:
        cooked_only = request.GET.get('cooked_only') in ('1', 'true', 'on')
    else:
        cooked_only = request.COOKIES.get('cooked_only') == '1'

    # Get the favorites-only filter. Check the request first, then check for cookie
    if 'favorites_only' in request.GET:
        favorites_only = request.GET.get('favorites_only') in ('1', 'true', 'on')
    else:
        favorites_only = request.COOKIES.get('favorites_only') == '1'

    # Get the public-only filter. Check the request first, then check for cookie
    if 'public_only' in request.GET:
        public_only = request.GET.get('public_only') in ('1', 'true', 'on')
    else:
        public_only = request.COOKIES.get('public_only') == '1'

    query = request.GET.get('search_term', '')

    # The current user's favorites — needed for the heart's state in every view,
    # for the favorites-only filter, and for the "favorites first" sort below.
    favorite_recipe_ids = _get_favorite_recipe_ids(request.user)

    recipes = recipe_query.filter_recipes(
        query=query,
        cooked_only=cooked_only,
        favorites_only=favorites_only,
        public_only=public_only,
        favorite_ids=favorite_recipe_ids,
    )

    # "Total Recipes" always means the whole library. It sits outside the
    # htmx-swapped results block, so deriving it from the filtered queryset
    # left it showing a stale filtered number until a full page load.
    recipe_count = Recipe.objects.count()

    if query:
        num_search_results = recipes.count()

    # Recipes staged in the user's meal-planner workspace — needed both for the
    # Actions-column toggle state and for the "in planner" sort below.
    workspace_recipe_ids = set()
    if request.user.is_authenticated:
        workspace_recipe_ids = set(
            _get_workspace_items(request.user).values_list('recipe_id', flat=True)
        )

    # Sorting is shared with the API (main/recipe_query.py).
    recipes = recipe_query.sort_recipes(
        recipes, sort_by,
        favorite_ids=favorite_recipe_ids,
        workspace_ids=workspace_recipe_ids,
    ).prefetch_related('tags')

    paginator = Paginator(recipes, results_per_page)
    requested_page = request.GET.get('page', '')
    try:
        page_recipe_list = paginator.page(requested_page)
    except PageNotAnInteger:
        page_recipe_list = paginator.page(1)
    except EmptyPage:
        page_recipe_list = paginator.page(paginator.num_pages)
        
    # Logic to store the recipe ratings as a dictionary of lists
    # Each list represents a recipe rating via 5 binary values (stars)
    rating_dict = {}
    for each in page_recipe_list:
        list = []
        for iterator in range(0,5,1):
            if (each.rating is not None) and (iterator < each.rating):
                list.append(1)
            else:
                list.append(0)
        rating_dict[each.id] = list

    # Map the active sort key to the list-view column it belongs to, so the
    # template can highlight that column's header and cells.
    sort_column_map = {
        'highest-rating': 'rating',
        'effort-asc': 'effort', 'effort-desc': 'effort',
        'complexity-asc': 'complexity', 'complexity-desc': 'complexity',
        'healthiness-asc': 'health', 'healthiness-desc': 'health', 'healthiness': 'health',
        'recipe-name': 'name',
        'most-tags': 'tags',
    }

    context = {
        'recipe_list': page_recipe_list,
        'query': query,
        'results_per_page': results_per_page,
        'per_page_options': [8, 12, 24, 50, 100],
        'recipe_count': recipe_count,
        'view': view,
        'sort_by': sort_by,
        'sort_column': sort_column_map.get(sort_by),
        'cooked_only': cooked_only,
        'favorites_only': favorites_only,
        'public_only': public_only,
        'favorite_recipe_ids': favorite_recipe_ids,
        'rating_dict': rating_dict,
        'app_version': settings.VERSION,
    }
    if len(page_recipe_list) == 0:
        context.update({'no_results': 1})

    context.update({'num_search_results': num_search_results})

    # Expose workspace membership (computed above) so the list view can render
    # the add/remove toggle in the correct state.
    if request.user.is_authenticated:
        context['workspace_recipe_ids'] = workspace_recipe_ids

    # If view=double_list, get midpoint in order to display across two columns
    if view == "double_list":
        recipe_list_midpoint = (len(page_recipe_list) + 1) // 2
        context.update({
            'recipe_list_left': page_recipe_list[:recipe_list_midpoint],
            'recipe_list_right': page_recipe_list[recipe_list_midpoint:],
        })

    if request.htmx:
        # User entered text; Only render partial
        response = render(request, 'partials/recipe_search_list.html', context)
        # Set cookies to save user search settings
        response.set_cookie('results_per_page', results_per_page, max_age=60*60*24*365, samesite='Lax')
        response.set_cookie('sort_by', sort_by, max_age=60*60*24*365, samesite='Lax')
        response.set_cookie('cooked_only', '1' if cooked_only else '0', max_age=60*60*24*365, samesite='Lax')
        response.set_cookie('favorites_only', '1' if favorites_only else '0', max_age=60*60*24*365, samesite='Lax')
        response.set_cookie('public_only', '1' if public_only else '0', max_age=60*60*24*365, samesite='Lax')
        return response
    else:
        # Render landing page (search for all recipes)
        response = render(request, 'main/recipes.html', context)
        # Set cookies to save user search settings
        response.set_cookie('view', view, max_age=60*60*24*365, samesite='Lax')
        response.set_cookie('sort_by', sort_by, max_age=60*60*24*365, samesite='Lax')
        response.set_cookie('cooked_only', '1' if cooked_only else '0', max_age=60*60*24*365, samesite='Lax')
        response.set_cookie('favorites_only', '1' if favorites_only else '0', max_age=60*60*24*365, samesite='Lax')
        response.set_cookie('public_only', '1' if public_only else '0', max_age=60*60*24*365, samesite='Lax')
        return response
@require(perms.RECIPE_READ_WRITE)
def recipe_add(request):
    if request.method == 'GET':
        context = {'form': RecipeForm(),
                   'app_version': settings.VERSION
                   }
        return render(request, "main/recipe_add.html", context)
    
    elif request.method == 'POST':
        form = RecipeForm(request.POST, request.FILES)
             
        if form.is_valid():
            recipe = form.save(commit=False)
            # If no image was uploaded but we have a scraped image in session, use it
            if not request.FILES.get('image') and request.POST.get('use_scraped_image'):
                if 'scraped_image_data' in request.session:
                    # Retrieve image from session
                    image_data = base64.b64decode(request.session['scraped_image_data'])
                    image_name = request.session.get('scraped_image_name', 'recipe_image.jpg')
                    
                    # Create a file object and save it
                    recipe.image.save(
                        image_name,
                        ContentFile(image_data),
                        save=False
                    )
                    
                    # Clean up session
                    del request.session['scraped_image_data']
                    if 'scraped_image_name' in request.session:
                        del request.session['scraped_image_name']
                    if 'scraped_image_type' in request.session:
                        del request.session['scraped_image_type']
            
            recipe.save()

            if form.is_valid():
                # Need to do a form.save() here to ensure tags are saved since M2M relationships are handled differently
                new_recipe = form.save()
                messages.success(request, 'Recipe added.')
                return redirect(new_recipe.get_absolute_url())

        context = {'form': form,
                   'app_version': settings.VERSION,
            }

        return render(request, 'main/recipe_add.html', context)
    
SCRAPE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}


def scrape_recipe_url(url):
    """Fetches a recipe page and returns (recipe_data, image_file). The image
    is None when the page has no suitable one. Network errors are left to
    the caller as requests.RequestException."""
    response = safe_fetch.get(url, headers=SCRAPE_HEADERS, timeout=10)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')

    # Try to find JSON-LD structured data (common for recipes)
    recipe_data = extract_recipe_json_ld(soup)

    # If JSON-LD not found, try fallback scraping (html)
    if not recipe_data:
        recipe_data = extract_recipe_fallback(soup)

    # Add the source URL
    recipe_data['url'] = url

    image_file = extract_and_download_image(soup, url, SCRAPE_HEADERS)
    return recipe_data, image_file


@require(perms.RECIPE_READ_WRITE)
def recipe_add_url(request):
    """The add-from-URL page. With ?url= (where a single-URL submission is
    sent) it scrapes that page and shows the recipe form pre-filled, so the
    recipe can be checked before it's saved."""
    context = {'app_version': settings.VERSION}
    url = request.GET.get('url', '').strip()
    if url:
        try:
            recipe_data, image_file = scrape_recipe_url(url)

            # Clean up instructions
            if 'instructions' in recipe_data:
                recipe_data['instructions'] = clean_instructions(recipe_data['instructions'])

            # Create a form with pre-filled data
            recipe_form = RecipeForm(initial=recipe_data)

            # Store the image in session for the actual form submission
            image_preview_url = None
            if image_file:
                # Convert to base64 for preview
                image_file.seek(0)
                image_data = base64.b64encode(image_file.read()).decode('utf-8')
                image_preview_url = f"data:image/jpeg;base64,{image_data}"

                # Store in session so it persists when form is submitted
                request.session['scraped_image_data'] = image_data
                request.session['scraped_image_name'] = image_file.name
                request.session['scraped_image_type'] = image_file.content_type

            context.update({'form': recipe_form,
                            'scraped': True,
                            'source_url': url,
                            'scraped_image': image_file is not None,
                            'image_preview_url': image_preview_url,
                            })
            return render(request, 'main/recipe_add.html', context)
        except requests.RequestException as e:
            context['scrape_errors'] = [f'Error fetching URL: {str(e)}']
        except Exception as e:
            context['scrape_errors'] = [f'Error parsing recipe: {str(e)}']

    context['batch_form'] = URLScrapeFormBatch(initial={'urls': url})
    return render(request, 'main/recipe_add_url.html', context)

@require(perms.RECIPE_READ_WRITE)
def process_url_batch(request):
    if request.method == 'POST':
        form = URLScrapeFormBatch(request.POST)
        if not form.is_valid():
            return render(request, 'partials/url_import_errors.html', {
                'errors': form.errors['urls'],
            })
        urls = form.cleaned_data['urls']

        # A single URL gets the edit-before-saving page rather than being
        # saved straight away. HX-Redirect makes htmx do a full page load.
        if len(urls) == 1:
            response = HttpResponse()
            response['HX-Redirect'] = reverse('recipe-add-url') + '?' + urlencode({'url': urls[0]})
            return response

        # Create a session if it doesn't exist
        if not request.session.session_key:
            request.session.create()

        # Create task ID
        task_id = f"task_{request.session.session_key}_{int(time.time())}"

        # Initialize progress
        cache.set(f"progress_{task_id}", 0, timeout=3600)

        # Start recipe import to run in the background
        threading.Thread(target=recipe_add_url_batch, args=(request, task_id, urls)).start()

        # Return progress bar immediately
        return render(request, 'partials/progress_bar.html', {
            'task_id': task_id,
            'progress': 0
        })

def recipe_add_url_batch(request, task_id, url_list):
    # Threads start with an empty context, so the audit log wouldn't know who
    # kicked this off unless the request's user is put back in place here.
    with audit.actor_context(getattr(request, 'user', None)):
        return _recipe_add_url_batch(task_id, url_list)


def _recipe_add_url_batch(task_id, url_list):
    recipe_success = []
    recipe_failure = []

    # Process each URL in the list
    for index, each_url in enumerate(url_list):

        try:
            recipe_data, image_file = scrape_recipe_url(each_url)

            if 'name' in recipe_data and 'ingredients' in recipe_data and 'instructions' in recipe_data:
                try:
                    if image_file:
                        # Convert to base64
                        image_file.seek(0)
                        image_data = base64.b64encode(image_file.read()).decode('utf-8')
                        image_content_obj = ContentFile(base64.b64decode(image_data), name=image_file.name)

                        # Save recipe w/ image
                        new_recipe = Recipe.objects.create(
                        name=recipe_data['name'],
                        description=recipe_data['description'],
                        ingredients=recipe_data['ingredients'],
                        instructions=clean_instructions(recipe_data['instructions']),
                        url=each_url,
                        image=image_content_obj,
                        notes='',
                        )

                    else:
                        # Save recipe w/o image
                        new_recipe = Recipe.objects.create(
                        name=recipe_data['name'],
                        description=recipe_data['description'],
                        ingredients=recipe_data['ingredients'],
                        instructions=clean_instructions(recipe_data['instructions']),
                        url=each_url,
                        notes='',
                        )

                    # Store identifiers for each new recipe in the results.
                    # These are plain dicts (they go through the cache), so
                    # the slug has to be copied across for the result links.
                    recipe_data['id'] = new_recipe.id
                    recipe_data['slug'] = new_recipe.slug
                    recipe_success.append(recipe_data)
                except:
                    recipe_failure.append(each_url)

            else:
                recipe_failure.append(each_url)

        except Exception:
            # Fetch and parse errors alike just count as a failed URL.
            recipe_failure.append(each_url)

        # Update progress
        progress = int(((index+1)/ url_list.__len__()  * 100))
        cache.set(f"progress_{task_id}", progress, timeout=3600)
    
    # Store results in the cache
    cache.set(f"result_{task_id}", {
        'success': recipe_success,
        'failure': recipe_failure,
        'job_type': "url_batch"
    }, timeout=3600)
    cache.set(f"progress_{task_id}", 100, timeout=3600)

@login_required
def get_progress(request, task_id):
    progress = cache.get(f"progress_{task_id}", 0)
    if progress >= 100:
        result = cache.get(f"result_{task_id}", {})

        # The CSV import reports a tally rather than lists of recipes, so it
        # gets its own results panel instead of progress_complete.html.
        if result.get('job_type') == 'csv_import':
            return render(request, 'partials/csv_import_result.html', result)

        if result.get('job_type') == 'find_duplicates':
            return render(request, 'partials/duplicate_pairs.html', {
                'pairs': result.get('pairs', []),
                'threshold_percent': result.get('threshold_percent', 90),
            })

        success_count = len(result.get('success', []))
        fail_count = len(result.get('failure', []))
        # Pass the results to the template
        return render(request, 'partials/progress_complete.html', {
            'recipe_success': result.get('success', []),
            'recipe_failure': result.get('failure', []),
            'job_type': result.get('job_type'),
            'success_count': success_count,
            'fail_count': fail_count,
        })
    
    # Return updated progress bar
    return render(request, 'partials/progress_bar.html', {
        'task_id': task_id, 
        'progress': progress
    })

def extract_recipe_json_ld(soup):
    scripts = soup.find_all('script', type='application/ld+json')
    for script in scripts:
        try:
            data = json.loads(script.string)
            
            # Handle both single objects and arrays
            if isinstance(data, list):
                data = next((item for item in data if item.get('@type') == 'Recipe'), None)
            elif isinstance(data, dict) and data.get('@type') != 'Recipe':
                # Check if Recipe is nested in @graph
                if '@graph' in data:
                    data = next((item for item in data['@graph'] if item.get('@type') == 'Recipe'), None)
                else:
                    continue
            
            if data and data.get('@type') == 'Recipe':
                recipe_data = {
                    'name': data.get('name', ''),
                    'description': data.get('description', ''),
                }
                
                # Extract ingredients
                ingredients = data.get('recipeIngredient', [])
                if ingredients:
                    recipe_data['ingredients'] = '\n'.join(ingredients)
                
                # Extract instructions
                instructions = data.get('recipeInstructions', [])
                if instructions:
                    if isinstance(instructions, list):
                        inst_text = []
                        for i, step in enumerate(instructions, 1):
                            if isinstance(step, dict):
                                inst_text.append(f"{i}. {step.get('text', '')}")
                            else:
                                inst_text.append(f"{i}. {step}")
                        recipe_data['instructions'] = '\n'.join(inst_text)
                    else:
                        recipe_data['instructions'] = instructions
                
                return recipe_data
        # except (json.JSONDecodeError, KeyError, ValueError):
        except:
            continue
            
    return None

def extract_recipe_fallback(soup):
    recipe_data = {}
    # Try to find title
    title = soup.find('h1')
    if title:
        recipe_data['name'] = title.get_text(strip=True)
    
    # Try to find description/summary
    desc = soup.find('meta', {'name': 'description'})
    if desc:
        recipe_data['description'] = desc.get('content', '')
    
    # Try to find ingredients (common class names)
    ingredients_container = soup.find(['ul', 'div'], class_=lambda x: x and any(
        term in x.lower() for term in ['ingredient', 'ingredients',]
    ))
    if ingredients_container:
        ingredients = []
        for item in ingredients_container.find_all(['li', 'p']):
            text = item.get_text(strip=True)
            if text:
                ingredients.append(text)
        if ingredients:
            recipe_data['ingredients'] = '\n'.join(ingredients)
    
    # Try to find instructions
    instructions_container = soup.find(['ol', 'div'], class_=lambda x: x and any(
        term in x.lower() for term in ['instruction', 'directions', 'steps', 'method']
    ))
    if instructions_container:
        instructions = []
        for i, item in enumerate(instructions_container.find_all(['li', 'p']), 1):
            text = item.get_text(strip=True)
            if text:
                instructions.append(f"{i}. {text}")
        if instructions:
            recipe_data['instructions'] = '\n'.join(instructions)
    
    return recipe_data

def extract_and_download_image(soup, base_url, headers):
    """
    Extract and download the first large image from a recipe page.
    Returns an InMemoryUploadedFile suitable for Django forms, or None if no suitable image found.
    """
    image_url = find_recipe_image(soup, base_url)

    if not image_url:
        return None
    
    try:
        # Download the image
        img_response = safe_fetch.get(image_url, headers=headers, timeout=10)
        img_response.raise_for_status()
        
        # Open with PIL to validate and get format
        img = Image.open(BytesIO(img_response.content))
        
        # Filter out small images (likely icons or logos)
        if img.width < 200 or img.height < 200:
            return None
        
        # Convert to RGB if necessary (for PNG with transparency, etc.)
        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = background
        
        # Save to BytesIO
        output = BytesIO()
        img_format = img.format if img.format in ['JPEG', 'PNG', 'WEBP'] else 'JPEG'
        img.save(output, format=img_format, quality=85)
        output.seek(0)
        
        # Get filename from URL or use default
        filename = os.path.basename(image_url.split('?')[0])
        if not filename or '.' not in filename:
            filename = f'recipe_image.{img_format.lower()}'
        
        # Create InMemoryUploadedFile
        return InMemoryUploadedFile(
            output,
            'ImageField',
            filename,
            f'image/{img_format.lower()}',
            output.getbuffer().nbytes,
            None
        )
    
    except Exception as e:
        logger.warning("Couldn't download image %s: %s", image_url, e)
        return None


def find_recipe_image(soup, base_url):
    """
    Find the main recipe image URL from the page.
    Tries multiple strategies to find the best image.
    """
    from urllib.parse import urljoin
    
    # Strategy 1: Check JSON-LD structured data
    scripts = soup.find_all('script', type='application/ld+json')
    for script in scripts:
        try:
            data = json.loads(script.string)
            
            if isinstance(data, list):
                data = next((item for item in data if item.get('@type') == 'Recipe'), None)
            elif isinstance(data, dict):
                if '@graph' in data:
                    data = next((item for item in data['@graph'] if item.get('@type') == 'Recipe'), None)
            
            if data and data.get('@type') == 'Recipe':
                image = data.get('image')
                if image:
                    if isinstance(image, str):
                        return urljoin(base_url, image)
                    elif isinstance(image, dict):
                        return urljoin(base_url, image.get('url', ''))
                    elif isinstance(image, list) and len(image) > 0:
                        img = image[0]
                        if isinstance(img, str):
                            return urljoin(base_url, img)
                        elif isinstance(img, dict):
                            return urljoin(base_url, img.get('url', ''))
        except:
            continue
    
    # Strategy 2: Look for og:image meta tag

    og_image = soup.find('meta', property='og:image')
    if og_image and og_image.get('content'):
        return urljoin(base_url, og_image['content'])
    
    # Strategy 3: Find the first large image in the content
    # Look for images with recipe-related classes first
    priority_images = soup.find_all('img', class_=lambda x: x and any(
        term in x.lower() for term in ['recipe', 'featured', 'hero', 'main']
    ))
    
    for img in priority_images:
        src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
        if src:
            return urljoin(base_url, src)
    
    # Strategy 4: Find any large image (check width/height attributes)
    all_images = soup.find_all('img')
    for img in all_images:
        width = img.get('width')
        height = img.get('height')
        
        # Try to find images with dimensions specified
        if width and height:
            try:
                if int(width) >= 300 and int(height) >= 300:
                    src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
                    if src:
                        return urljoin(base_url, src)
            except (ValueError, TypeError):
                pass
    
    # Strategy 5: Just get the first image with src
    for img in all_images:
        src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
        if src and not any(skip in src.lower() for skip in ['icon', 'logo', 'avatar', 'sprite']):
            return urljoin(base_url, src)
    
    return None

@require(perms.RECIPE_READ_WRITE)
def recipe_delete(request, pk):
    recipe_obj = Recipe.objects.get(pk=pk)
    context = {'name': recipe_obj.name,
               'id': recipe_obj.id,
               'slug': recipe_obj.slug,
               'app_version': settings.VERSION
               }
    return render(request, 'main/recipe_delete.html', context)

@require(perms.RECIPE_READ_WRITE)
def recipe_delete_confirm(request, pk):
    recipe_obj = Recipe.objects.get(pk=pk)
    recipe_obj.delete()
    messages.success(request, 'Recipe deleted.')
    return redirect('recipes')
    
def _with_query_string(request, url):
    """Carry any query string across a redirect."""
    query_string = request.META.get('QUERY_STRING')
    return f"{url}?{query_string}" if query_string else url


@require(perms.RECIPE_READ)
def recipe_detail_legacy_id(request, pk):
    """Permanently redirect a pre-slug /recipe-detail/<id>/ URL to /recipe/<slug>/."""
    recipe_obj = get_object_or_404(Recipe, pk=pk)
    return redirect(_with_query_string(request, recipe_obj.get_absolute_url()), permanent=True)


@require(perms.RECIPE_READ)
def recipe_detail_legacy_slug(request, slug):
    """Permanently redirect /recipe-detail/<slug>/ to the canonical /recipe/<slug>/.

    No lookup needed -- recipe_detail resolves the slug, including retired ones.
    """
    target = reverse('recipe-detail', args=[slug])
    return redirect(_with_query_string(request, target), permanent=True)


@require(perms.RECIPE_READ)
def recipe_detail(request, slug):
    recipe_obj = Recipe.objects.filter(slug=slug).first()

    if recipe_obj is None:
        # Might be a slug the recipe used before a rename; send old links onward.
        retired = RecipeSlugHistory.objects.filter(slug=slug).select_related('recipe').first()
        if retired is None:
            raise Http404("No recipe matches that URL.")
        return redirect(_with_query_string(request, retired.recipe.get_absolute_url()), permanent=True)

    recipe_id = recipe_obj.id
    recipe_name = recipe_obj.name
    recipe_description = recipe_obj.description
    recipe_ingredients = recipe_obj.ingredients
    recipe_instructions = recipe_obj.instructions
    recipe_notes = recipe_obj.notes
    recipe_rating = recipe_obj.rating
    recipe_url = recipe_obj.url
    recipe_image = recipe_obj.image
    recipe_complexity = recipe_obj.complexity
    recipe_effort = recipe_obj.effort
    recipe_health = recipe_obj.health
    tags = recipe_obj.tags.all()

    context = {
        "name": recipe_name,
        "description": recipe_description,
        "id": recipe_id,
        "image": recipe_image,
        "ingredients": recipe_ingredients,
        "instructions": recipe_instructions,
        "notes": recipe_notes,
        "rating": recipe_rating,
        "url": recipe_url,
        "complexity": recipe_complexity,
        "effort": recipe_effort,
        "health": recipe_health,
        "range": range(5),
        "gluten_free": recipe_obj.gluten_free,
        "dairy_free": recipe_obj.dairy_free,
        "vegetarian": recipe_obj.vegetarian,
        "vegan": recipe_obj.vegan,
        "tags": tags,
        "app_version": settings.VERSION,
        "is_public": recipe_obj.is_public,
        "share_notes": recipe_obj.share_notes,
        "public_url": _absolute_public_url(request, recipe_obj),
    }

    if request.user.is_authenticated:
        context["in_workspace"] = WorkspaceItem.objects.filter(
            recipe=recipe_obj, **_workspace_owner_filter(request.user)
        ).exists()
        context["is_favorite"] = Favorite.objects.filter(
            recipe=recipe_obj, user=request.user
        ).exists()

    return render(request, "main/recipe_detail.html", context)


def _absolute_public_url(request, recipe_obj):
    public_path = recipe_obj.get_public_url()
    return request.build_absolute_uri(public_path) if public_path else None


@require(perms.RECIPE_READ_WRITE)
def recipe_make_public_toggle(request, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)

    recipe_obj = get_object_or_404(Recipe, pk=pk)
    recipe_obj.is_public = not recipe_obj.is_public
    if recipe_obj.is_public and not recipe_obj.share_token:
        # Kept forever once generated (even if made private again) so a link
        # that's already been shared keeps working if re-enabled later.
        recipe_obj.share_token = secrets.token_urlsafe(16)
    recipe_obj.save(update_fields=['is_public', 'share_token'])

    return render(request, 'partials/recipe_public_share.html', {
        'id': recipe_obj.id,
        'is_public': recipe_obj.is_public,
        'share_notes': recipe_obj.share_notes,
        'public_url': _absolute_public_url(request, recipe_obj),
    })


@require(perms.RECIPE_READ_WRITE)
def recipe_toggle_share_notes(request, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)

    recipe_obj = get_object_or_404(Recipe, pk=pk)
    recipe_obj.share_notes = not recipe_obj.share_notes
    recipe_obj.save(update_fields=['share_notes'])

    return render(request, 'partials/recipe_public_share.html', {
        'id': recipe_obj.id,
        'is_public': recipe_obj.is_public,
        'share_notes': recipe_obj.share_notes,
        'public_url': _absolute_public_url(request, recipe_obj),
    })


def recipe_public(request, token, slug):
    recipe_obj = Recipe.objects.filter(share_token=token, is_public=True).first()
    if recipe_obj is None:
        raise Http404("No recipe matches that URL.")

    if recipe_obj.slug != slug:
        # The recipe was renamed since this link was generated -- the token
        # still resolves it, just bounce to the current slug for a tidy URL.
        return redirect(recipe_obj.get_public_url(), permanent=True)

    context = {
        'name': recipe_obj.name,
        'description': recipe_obj.description,
        'image': recipe_obj.image,
        'ingredients': recipe_obj.ingredients,
        'instructions': recipe_obj.instructions,
        'notes': recipe_obj.notes if recipe_obj.share_notes else None,
        # Only http(s) sources become a link -- this page is anonymous, so a
        # stray javascript: or data: value mustn't end up in an href.
        'url': recipe_obj.url if (recipe_obj.url or '').lower().startswith(('http://', 'https://')) else None,
        'app_version': settings.VERSION,
        'force_theme': 'dark',
    }
    return render(request, 'main/recipe_public.html', context)


@require(perms.RECIPE_READ_WRITE)
def recipe_update(request, pk):
    recipe_obj = Recipe.objects.get(pk=pk)
    context = {'recipe_obj': recipe_obj,
               'app_version': settings.VERSION
               }

    # Logic to capture the recipe rating as a list of 5 binary values (stars)
    rating_binary_list = []
    for iterator in range(0,5,1):
        if (recipe_obj.rating is not None) and (iterator < recipe_obj.rating):
            rating_binary_list.append(1)
        else:
            rating_binary_list.append(0)
    context.update({'rating_list': rating_binary_list})

    form = RecipeForm(request.POST or None, request.FILES or None, instance=recipe_obj)
    if form.is_valid():
        # Save the form regardless  
        form.save()
        # Read the slug back off the saved instance: renaming the recipe moves it.
        messages.success(request, 'Recipe saved.')
        return redirect(recipe_obj.get_absolute_url())
    context.update({'form': form,
                    'recipe_tags': recipe_obj.tags.all()
                    })
    return render(request, 'main/recipe_update.html', context)

SESSION_IMPORT_TOKEN = 'csv_import_token'
SESSION_IMPORT_FILENAME = 'csv_import_filename'


def _configure_context(request, analysis, selected, mapping, on_duplicate, errors):
    """Everything main/recipe_import_configure.html needs to redraw itself."""
    return {
        'filename': request.session.get(SESSION_IMPORT_FILENAME, 'your file'),
        'token': request.session.get(SESSION_IMPORT_TOKEN, ''),
        'analysis': analysis,
        # Detected columns the user can pick from, paired with the field each
        # one feeds. id/slug and the read-only extras are listed separately --
        # they aren't importable, so they mustn't appear as checkboxes.
        'detected': analysis.importable_columns(),
        'match_columns': analysis.match_columns(),
        'unusable_columns': analysis.unusable_columns(),
        # Unrecognised columns, numbered so their dropdowns post back as map_<i>.
        'unknown': [
            {
                'index': index,
                'header': header,
                'sample': analysis.samples.get(header, ''),
                'chosen': mapping.get(header, ''),
            }
            for index, header in enumerate(analysis.unknown)
        ],
        'targets': analysis.unplaced_fields(),
        'selected': set(selected),
        'duplicate_choices': csv_import.DUPLICATE_CHOICES,
        'on_duplicate': on_duplicate,
        'errors': errors,
        'app_version': settings.VERSION,
    }


def _posted_mapping(request, analysis):
    """{header: field key} from the map_<i> dropdowns, blanks meaning "ignore"."""
    mapping = {}
    for index, header in enumerate(analysis.unknown):
        chosen = request.POST.get(f'map_{index}', '').strip()
        if chosen:
            mapping[header] = chosen
    return mapping


@require(perms.RECIPE_READ_WRITE)
def recipe_import(request):
    """Step one: take the file and work out what's in it."""
    # Tidy up after anyone who uploaded a file and never came back.
    csv_import.sweep()

    error = None
    form = CSVUploadForm()

    if request.method == 'POST':
        form = CSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            upload = request.FILES['csv_file']
            token = csv_import.stash(upload)
            try:
                columns, rows = csv_import.read_file(csv_import.stashed(token))
            except ValueError as bad_file:
                csv_import.discard(token)
                error = str(bad_file)
            else:
                # Only one import can be in flight per session; drop the file
                # from any previous half-finished attempt.
                csv_import.discard(request.session.get(SESSION_IMPORT_TOKEN))
                request.session[SESSION_IMPORT_TOKEN] = token
                request.session[SESSION_IMPORT_FILENAME] = upload.name

                analysis = csv_import.analyse(columns, rows)
                return render(request, 'main/recipe_import_configure.html', _configure_context(
                    request, analysis,
                    selected=[field.key for _header, field in analysis.importable_columns()],
                    mapping={},
                    on_duplicate=csv_import.SKIP,
                    errors=[],
                ))
        else:
            error = form.errors['csv_file'][0]

    return render(request, 'main/recipe_import.html', {
        'form': form,
        'error': error,
        'app_version': settings.VERSION,
    })


@require(perms.RECIPE_READ_WRITE)
def recipe_import_configure(request):
    """Step two: apply the column choices and mappings, then import."""
    token = request.session.get(SESSION_IMPORT_TOKEN)
    # The token in the form has to be the one we handed this session, so a
    # stale tab can't quietly import a file the user has since replaced.
    if request.method != 'POST' or not token or request.POST.get('token') != token:
        return redirect('recipe-import')

    data = csv_import.stashed(token)
    if data is None:
        return render(request, 'main/recipe_import.html', {
            'form': CSVUploadForm(),
            'error': 'That upload has expired. Choose the file again.',
            'app_version': settings.VERSION,
        })

    columns, rows = csv_import.read_file(data)
    analysis = csv_import.analyse(columns, rows)

    form = CSVImportForm(request.POST)
    mapping = _posted_mapping(request, analysis)
    selected = request.POST.getlist('fields')
    on_duplicate = request.POST.get('on_duplicate', csv_import.SKIP)

    headers, errors = csv_import.resolve(analysis, mapping)

    if form.is_valid():
        selected = form.cleaned_data['fields']
        on_duplicate = form.cleaned_data['on_duplicate']
    else:
        # Report what actually failed rather than guessing -- an invalid choice
        # here means the posted form didn't come from the page we rendered.
        for field_errors in form.errors.values():
            errors.extend(field_errors)

    # Mapping a column by hand is itself the decision to import it, so those
    # fields don't also need ticking.
    keys = set(selected) | set(mapping.values())
    if not keys:
        errors.append('Pick at least one field to import.')

    if errors:
        # Back to the form, swapped into the same panel it was posted from.
        return render(request, 'partials/csv_import_form.html', _configure_context(
            request, analysis, selected, mapping, on_duplicate, errors,
        ))

    # Everything the import needs is now in memory, so the upload can go and
    # the thread never has to care how long the request lived.
    csv_import.discard(token)
    request.session.pop(SESSION_IMPORT_TOKEN, None)
    request.session.pop(SESSION_IMPORT_FILENAME, None)

    task_id = f"task_{request.session.session_key}_{int(time.time())}"
    cache.set(f"progress_{task_id}", 0, timeout=3600)

    threading.Thread(target=run_csv_import, kwargs={
        'task_id': task_id,
        'rows': rows,
        'headers': headers,
        'keys': keys,
        'on_duplicate': on_duplicate,
        'columns': columns,
        'mapped': [
            (header, csv_fields.FIELDS_BY_KEY[key].label)
            for header, key in mapping.items()
        ],
        'actor': request.user,
    }).start()

    return render(request, 'partials/progress_bar.html', {
        'task_id': task_id,
        'progress': 0,
    })


def run_csv_import(task_id, rows, headers, keys, on_duplicate, columns, mapped, actor=None):
    """Import in the background, reporting progress for the htmx poller."""
    with audit.actor_context(actor):
        _run_csv_import(task_id, rows, headers, keys, on_duplicate, columns, mapped)


def _run_csv_import(task_id, rows, headers, keys, on_duplicate, columns, mapped):
    last_percent = -1

    def report(done, total):
        nonlocal last_percent
        # Held below 100 until the result is cached: get_progress treats 100 as
        # "finished", and would otherwise render a results page with no results.
        percent = min(int(done / total * 100), 99) if total else 99
        if percent != last_percent:
            last_percent = percent
            cache.set(f"progress_{task_id}", percent, timeout=3600)

    payload = {'job_type': 'csv_import', 'mapped': mapped,
               'overwrote': on_duplicate == csv_import.OVERWRITE}
    try:
        result = csv_import.import_rows(
            rows, headers, keys, on_duplicate, columns, progress=report,
        )
        payload.update({
            'created': result.created,
            'updated': result.updated,
            'total': result.total,
            'skipped': result.skipped,
            'errors': result.errors,
            'ignored_columns': result.ignored_columns,
        })
    except Exception as error:
        # The row loop already survives a bad row, so this is something worse.
        # Say so on the results panel rather than leaving the bar spinning.
        payload['fatal'] = str(error)
    finally:
        # Result first, then 100 -- the poller reads them in that order.
        cache.set(f"result_{task_id}", payload, timeout=3600)
        cache.set(f"progress_{task_id}", 100, timeout=3600)
        # Threads get their own connection, which nothing else will close.
        connection.close()

@require(perms.RECIPE_READ)
def recipe_export(request):
    """Pick columns, then download every recipe as a CSV."""
    selected = csv_fields.DEFAULT_KEYS
    error = None

    if request.method == 'POST':
        form = CSVExportForm(request.POST)
        if form.is_valid():
            selected = form.cleaned_data['fields']
            recipes = Recipe.objects.prefetch_related('tags').order_by('name')
            filename = f"jolof-recipes-{date_class.today():%Y-%m-%d}.csv"
            return csv_export.build_csv_response(recipes, selected, filename)

        # Keep whatever was ticked so the failed attempt isn't reset.
        selected = request.POST.getlist('fields')
        error = form.errors['fields'][0]

    context = {
        'field_groups': csv_fields.FIELD_GROUPS,
        'selected': set(selected),
        'error': error,
        'recipe_count': Recipe.objects.count(),
        'app_version': settings.VERSION,
    }

    return render(request, 'main/recipe_export.html', context)

@require(perms.RECIPE_READ_WRITE)
def update_tags(request, recipe_id):
    if request.method == 'POST':
        context = {}
        recipe = get_object_or_404(Recipe, pk=recipe_id)
        raw = request.POST.get('tags', '')
        
        # Filter out empty strings from trailing comma
        new_tags = [t.strip() for t in raw.split(',') if t.strip()]
        tag_objects = []
        invalid_tags = []
        
        for each_tag in new_tags:
            if ":" in each_tag:
                label, value = each_tag.split(':', 1)
                label = label.strip()
                value = value.strip()
                if label and value:
                    tag, created = Tag.objects.get_or_create(
                        label = label,
                        value = value
                    )
                    tag_objects.append(tag)
                    context.update({'tag_update': 1})
                else:
                    invalid_tags.append(each_tag)
            else:
                invalid_tags.append(each_tag)

        recipe.tags.set(tag_objects)
        context.update({'invalid_tags': invalid_tags})

        return render(request, "partials/recipe_tags_status.html", context)


def _tag_search_q(query):
    """Build the filter for the Manage Tags live search.

    Every whitespace-separated term must match (AND across terms). A bare term
    matches either half of the tag; a term containing ':' is split into
    ``label:value``, where an empty half means "anything" -- so "cuisine:" lists
    every value under that label.
    """
    search_q = Q()
    for term in query.split():
        if ':' in term:
            label_part, value_part = (part.strip() for part in term.split(':', 1))
            term_q = Q()
            if label_part:
                term_q &= Q(label__icontains=label_part)
            if value_part:
                term_q &= Q(value__icontains=value_part)
            # A lone ':' filters on nothing at all; ignore it rather than
            # matching every tag twice over.
            if not term_q:
                continue
        else:
            term_q = Q(label__icontains=term) | Q(value__icontains=term)
        search_q &= term_q
    return search_q


# Sortable columns on the Manage Tags table: sort key -> ordering. Ties fall
# back to label/value so the order is stable between renders.
TAG_SORT_ORDERINGS = {
    'label-asc': [Lower('label'), Lower('value')],
    'label-desc': [Lower('label').desc(), Lower('value')],
    'value-asc': [Lower('value'), Lower('label')],
    'value-desc': [Lower('value').desc(), Lower('label')],
    'count-asc': ['recipe_count', Lower('label'), Lower('value')],
    'count-desc': ['-recipe_count', Lower('label'), Lower('value')],
}
TAG_SORT_DEFAULT = 'label-asc'


def _tag_manage_context(request, editing_pk=None, **extra):
    """Context for the Manage Tags body, honouring the live-search box.

    The search term and the column sort ride along on every action (via
    hx-include) so adding, renaming, or deleting a tag re-renders the same
    filtered, sorted list rather than dropping the user back to the full one.
    """
    query = (request.POST.get('tag_search') or request.GET.get('tag_search') or '').strip()
    sort_by = (request.POST.get('tag_sort') or request.GET.get('tag_sort') or '').strip()
    if sort_by not in TAG_SORT_ORDERINGS:
        sort_by = TAG_SORT_DEFAULT

    tags = Tag.objects.annotate(recipe_count=Count('recipes'))
    if query:
        tags = tags.filter(_tag_search_q(query))
    tags = tags.order_by(*TAG_SORT_ORDERINGS[sort_by])

    context = {
        'tags': tags,
        'tag_search': query,
        'tag_sort': sort_by,
        'tag_total': Tag.objects.count(),
        # Counted across the whole collection, not the filtered list, because
        # "Remove unused" deletes every orphan regardless of the search.
        'unused_count': _unused_tags().count(),
        'editing_pk': editing_pk,
        'app_version': settings.VERSION,
    }
    context.update(extra)
    return context


def _unused_tags():
    """Tags no recipe is using."""
    return Tag.objects.annotate(recipe_count=Count('recipes')).filter(recipe_count=0)


def _render_tag_manage_body(request, editing_pk=None, **extra):
    return render(request, "partials/tag_manage_body.html",
                  _tag_manage_context(request, editing_pk=editing_pk, **extra))


def _clean_tag_parts(request):
    """Pull label/value off the request. Returns (label, value, error)."""
    label = request.POST.get('label', '').strip()
    value = request.POST.get('value', '').strip()
    if not label or not value:
        return None, None, 'A tag needs both a label and a value, e.g. cuisine:italian.'
    if len(label) > 100 or len(value) > 100:
        return None, None, 'Labels and values are limited to 100 characters each.'
    return label, value, None


@require(perms.RECIPE_READ_WRITE)
def tag_manage(request):
    """Full page: search box, add form, and the tag table."""
    return render(request, "main/tag_manage.html", _tag_manage_context(request))


@require(perms.RECIPE_READ_WRITE)
def tag_manage_list(request):
    """htmx endpoint behind the live search, the Edit button, and Cancel."""
    editing = request.GET.get('editing', '')
    editing_pk = int(editing) if editing.isdigit() else None
    return _render_tag_manage_body(request, editing_pk=editing_pk)


@require(perms.RECIPE_READ_WRITE)
def tag_add(request):
    if request.method != 'POST':
        return HttpResponse(status=405)

    label, value, error = _clean_tag_parts(request)
    if error:
        return _render_tag_manage_body(request, error=error)

    # Case-insensitive, so "Cuisine:Asian" can't quietly become a second tag
    # alongside "cuisine:asian".
    existing = Tag.objects.filter(label__iexact=label, value__iexact=value).first()
    if existing:
        return _render_tag_manage_body(request, error=f'"{existing}" already exists.')

    tag = Tag.objects.create(label=label, value=value)
    response = _render_tag_manage_body(request, message=f'Added "{tag}".')
    # Tells the page to clear the add form; see tag_manage.html.
    response['HX-Trigger'] = 'tagAdded'
    return response


@require(perms.RECIPE_READ_WRITE)
def tag_edit(request, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)

    tag = get_object_or_404(Tag, pk=pk)
    label, value, error = _clean_tag_parts(request)
    if error:
        # Stay in edit mode so the typed values aren't thrown away.
        return _render_tag_manage_body(request, editing_pk=tag.pk, error=error)

    # Renaming onto a tag that already exists merges the two rather than failing
    # the unique constraint: every recipe on this tag picks up the survivor, and
    # this row goes away. Deleting the tag clears its own m2m rows.
    duplicate = Tag.objects.filter(label__iexact=label, value__iexact=value).exclude(pk=tag.pk).first()
    if duplicate:
        recipes = list(tag.recipes.all())
        for recipe in recipes:
            recipe.tags.add(duplicate)
        old = str(tag)
        tag.delete()
        message = f'Merged "{old}" into "{duplicate}"'
        message += f', moving {len(recipes)} recipe{"" if len(recipes) == 1 else "s"} across.' if recipes else '.'
        return _render_tag_manage_body(request, message=message)

    old = str(tag)
    tag.label = label
    tag.value = value
    tag.save()
    message = f'Renamed "{old}" to "{tag}".' if old != str(tag) else f'Saved "{tag}".'
    return _render_tag_manage_body(request, message=message)


@require(perms.RECIPE_READ_WRITE)
def tag_delete(request, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)

    tag = get_object_or_404(Tag, pk=pk)
    name = str(tag)
    count = tag.recipes.count()
    tag.delete()

    message = f'Deleted "{name}"'
    message += f', removing it from {count} recipe{"" if count == 1 else "s"}.' if count else '.'
    return _render_tag_manage_body(request, message=message)


@require(perms.RECIPE_READ_WRITE)
def tag_remove_unused(request):
    """Bulk-delete every tag no recipe is using.

    Always the whole collection, never just the filtered list -- the button's
    confirmation prompt says as much.
    """
    if request.method != 'POST':
        return HttpResponse(status=405)

    # An annotated queryset can't be deleted directly, so resolve to pks first.
    pks = list(_unused_tags().values_list('pk', flat=True))
    if not pks:
        return _render_tag_manage_body(request, message='There were no unused tags to remove.')

    Tag.objects.filter(pk__in=pks).delete()
    return _render_tag_manage_body(
        request,
        message=f'Removed {len(pks)} unused tag{"" if len(pks) == 1 else "s"}.',
    )


@require(perms.RECIPE_READ)
def tags_validate(request):
    context = {
        'app_version': settings.VERSION
        }
    if request.method == 'GET':

        raw = request.GET.get('tags_input', '')
        # Filter out empty strings from trailing comma
        new_tags = [t.strip() for t in raw.split(',') if t.strip()]
        invalid_tags = []

        for each_tag in new_tags:

            if ":" in each_tag:
                label, value = each_tag.split(':', 1)
                label = label.strip()
                value = value.strip()
                if label and value:
                    continue
                else:
                    invalid_tags.append(each_tag)
            else:
                invalid_tags.append(each_tag)
        
        if invalid_tags:
            context.update({'invalid_tags': invalid_tags})

    return render(request, "partials/recipe_tags_status.html", context)

@require(perms.RECIPE_READ)
def tags_suggest(request):
    """JSON list of existing tag strings matching the in-progress text.

    Backs the live autocomplete dropdown on the recipe tag inputs. Reuses the
    Manage Tags search logic so "cuisine:as" and a bare "cuis" behave the
    same way here as they do there.
    """
    query = request.GET.get('q', '').strip()
    if not query:
        return JsonResponse([], safe=False)

    tags = Tag.objects.filter(_tag_search_q(query)).order_by(Lower('label'), Lower('value'))[:8]
    return JsonResponse([str(tag) for tag in tags], safe=False)

def clean_instructions(instructions):
    # Add extra line break between numbered steps
    lines = instructions.split('\n')
    result = []
    
    for line in lines:
        result.append(line)
        # Check if line starts with a number followed by . or )
        stripped = line.lstrip()
        if stripped and stripped[0].isdigit():
            # Find where the number ends
            i = 0
            while i < len(stripped) and stripped[i].isdigit():
                i += 1
            # Check if the character after the number(s) is . or )
            if i < len(stripped) and stripped[i] in '.):':
                result.append('')  # Add extra blank line
    
    return '\n'.join(result)

@require(perms.RECIPE_READ)
def misc_tools(request):
    context = {
        'app_version': settings.VERSION
        }
    return render(request, "main/misc_tools.html", context)


SHARED_RECIPE_SORTS = {
    'name-asc': [Lower('name')],
    'name-desc': [Lower('name').desc()],
    # updated_at is auto_now, so this is "last edited", not "date shared" --
    # nothing records when the link was switched on beyond the audit log.
    'updated-asc': ['updated_at', Lower('name')],
    'updated-desc': ['-updated_at', Lower('name')],
}
SHARED_RECIPE_SORT_DEFAULT = 'name-asc'


def _shared_recipes_context(request, **extra):
    """Context for the Shared Recipes table: every recipe with a live public link."""
    sort_by = (request.POST.get('shared_sort') or request.GET.get('shared_sort') or '').strip()
    if sort_by not in SHARED_RECIPE_SORTS:
        sort_by = SHARED_RECIPE_SORT_DEFAULT

    shared = Recipe.objects.filter(is_public=True).order_by(*SHARED_RECIPE_SORTS[sort_by])

    # The absolute URL needs the request, so build it here rather than in the
    # template -- get_public_url() alone only gives the path.
    rows = [{'recipe': recipe_obj, 'public_url': _absolute_public_url(request, recipe_obj)}
            for recipe_obj in shared]

    context = {
        'shared_rows': rows,
        'shared_count': len(rows),
        'shared_sort': sort_by,
        'recipe_total': Recipe.objects.count(),
        'app_version': settings.VERSION,
    }
    context.update(extra)
    return context


@require(perms.RECIPE_READ)
def shared_recipes(request):
    """Full page: every recipe currently reachable by a public share link."""
    return render(request, "main/shared_recipes.html", _shared_recipes_context(request))


@require(perms.RECIPE_READ)
def shared_recipes_list(request):
    """htmx endpoint behind the column sorts -- renders the table only."""
    return render(request, "partials/shared_recipes_body.html", _shared_recipes_context(request))


@require(perms.RECIPE_READ_WRITE)
def shared_recipe_unshare(request, pk):
    """Turn one recipe's public link off, then re-render the table."""
    if request.method != 'POST':
        return HttpResponse(status=405)

    recipe_obj = get_object_or_404(Recipe, pk=pk)
    if recipe_obj.is_public:
        # The token is deliberately kept -- see recipe_make_public_toggle.
        recipe_obj.is_public = False
        recipe_obj.save(update_fields=['is_public'])

    return render(request, "partials/shared_recipes_body.html",
                  _shared_recipes_context(request,
                                          message=f'"{recipe_obj.name}" is no longer public.'))


def _normalize_recipe_name(name):
    """Lowercase, strip punctuation, and collapse whitespace for fuzzy comparison."""
    s = (name or '').lower()
    s = re.sub(r'[^\w\s]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _find_duplicate_pairs(threshold=0.9, progress=None):
    """Return recipe pairs whose names are identical or fuzzily similar.

    Each pair is a dict with the two recipes and the similarity as a percentage,
    sorted from most to least similar. If given, `progress(done, total)` is
    called after each recipe's comparisons are finished.
    """
    recipes = list(Recipe.objects.all())
    normalized = {r.id: _normalize_recipe_name(r.name) for r in recipes}

    pairs = []
    total = len(recipes)
    for i, recipe_a in enumerate(recipes):
        name_a = normalized[recipe_a.id]
        if name_a:
            for recipe_b in recipes[i + 1:]:
                name_b = normalized[recipe_b.id]
                if not name_b:
                    continue
                if name_a == name_b:
                    ratio = 1.0
                else:
                    matcher = SequenceMatcher(None, name_a, name_b)
                    # real_quick_ratio is a cheap upper bound; skip clear non-matches.
                    if matcher.real_quick_ratio() < threshold:
                        continue
                    ratio = matcher.ratio()
                    if ratio < threshold:
                        continue
                pairs.append({
                    'recipes': (recipe_a, recipe_b),
                    'ratio': round(ratio * 100),
                })
        if progress:
            progress(i + 1, total)

    pairs.sort(key=lambda p: p['ratio'], reverse=True)
    return pairs


def _parse_threshold_percent(raw, default=90):
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    return max(50, min(100, value))


@require(perms.RECIPE_READ_WRITE)
def find_duplicates(request):
    return render(request, "main/find_duplicates.html", {'app_version': settings.VERSION})


def _find_duplicates_task(task_id, threshold_percent):
    last_percent = -1

    def report(done, total):
        nonlocal last_percent
        # Held below 100 until the result is cached: get_progress treats 100 as
        # "finished", and would otherwise render a results page with no results.
        percent = min(int(done / total * 100), 99) if total else 99
        if percent != last_percent:
            last_percent = percent
            cache.set(f"progress_{task_id}", percent, timeout=3600)

    try:
        pairs = _find_duplicate_pairs(threshold_percent / 100, progress=report)
        cache.set(f"result_{task_id}", {
            'pairs': pairs,
            'threshold_percent': threshold_percent,
            'job_type': 'find_duplicates',
        }, timeout=3600)
    finally:
        cache.set(f"progress_{task_id}", 100, timeout=3600)
        # Threads get their own connection, which nothing else will close.
        connection.close()


@require(perms.RECIPE_READ_WRITE)
def find_duplicates_start(request):
    if request.method != 'POST':
        return HttpResponse(status=405)
    if not request.session.session_key:
        request.session.create()
    threshold_percent = _parse_threshold_percent(request.POST.get('threshold'))
    task_id = f"task_{request.session.session_key}_{int(time.time())}"
    cache.set(f"progress_{task_id}", 0, timeout=3600)
    threading.Thread(target=_find_duplicates_task, args=(task_id, threshold_percent)).start()
    return render(request, 'partials/progress_bar.html', {'task_id': task_id, 'progress': 0})


@require(perms.RECIPE_READ_WRITE)
def find_duplicates_delete(request, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)
    threshold_percent = _parse_threshold_percent(request.POST.get('threshold'))
    Recipe.objects.filter(pk=pk).delete()
    pairs = _find_duplicate_pairs(threshold_percent / 100)
    return render(request, "partials/duplicate_pairs.html", {
        'pairs': pairs,
        'threshold_percent': threshold_percent,
    })

@require(perms.PLATFORM_ADMIN)
def delete_all_recipes(request):
    recipe_count = len(Recipe.objects.all())
    context = {
        'app_version': settings.VERSION,
        'recipe_count': recipe_count,
    }

    if request.method == 'POST':
        Recipe.objects.all().delete()
        
        messages.success(request, f'Deleted {recipe_count} recipe{"" if recipe_count == 1 else "s"}.')
        return redirect('recipes')

    else:
        return render(request, "main/delete_all_recipes.html", context)

@require(perms.RECIPE_READ_WRITE)
def scrape_images(request, task_id=None):
    # Also runs as a background thread (see process_image_scrape), which starts
    # with an empty context; re-attach the user so the audit log can name them.
    with audit.actor_context(getattr(request, 'user', None)):
        return _scrape_images(request, task_id)


def _scrape_images(request, task_id=None):
    context = {
        'app_version': settings.VERSION
    }
    if task_id:
        recipe_success = []
        recipe_failure = []

        recipes = Recipe.objects.all()
        recipe_count = len(recipes)

        for index, each_recipe in enumerate(recipes):
            image_file = ''
            if each_recipe.url and not each_recipe.image:
                
                try:
                    # Fetch the webpage
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    }
                    response = safe_fetch.get(each_recipe.url, headers=headers, timeout=10)
                    response.raise_for_status()
                    soup = BeautifulSoup(response.content, 'html.parser')

                    image_file = extract_and_download_image(soup, each_recipe.url, headers)
            
                except Exception:
                    recipe_failure.append(each_recipe)

                if image_file:
                    # Convert to base64
                    image_file.seek(0)
                    image_data = base64.b64encode(image_file.read()).decode('utf-8')
                    image_content_obj = ContentFile(base64.b64decode(image_data), name=image_file.name)

                    # Save recipe w/ image
                    each_recipe.image = image_content_obj
                    each_recipe.save()
                    recipe_success.append(each_recipe)
                else:
                    if each_recipe not in recipe_failure:
                        recipe_failure.append(each_recipe)
            
            # Update progress
            progress = int(((index+1)/ recipe_count  * 100))
            cache.set(f"progress_{task_id}", progress, timeout=3600)

        # Store results in the cache
        cache.set(f"result_{task_id}", {
            'success': recipe_success,
            'failure': recipe_failure,
            'job_type': "scrape_image",
        }, timeout=3600)
        cache.set(f"progress_{task_id}", 100, timeout=3600)  
    else:
        return render(request, "main/scrape_images.html", context)
    
@require(perms.RECIPE_READ_WRITE)
def process_image_scrape(request):
    if request.method == 'POST':
        
        # Create a session if it doesn't exist
        if not request.session.session_key:
            request.session.create()

        # Create task ID
        task_id = f"task_{request.session.session_key}_{int(time.time())}"

        # Initialize progress
        cache.set(f"progress_{task_id}", 0, timeout=3600)
        
        # Start recipe import to run in the background
        threading.Thread(target=scrape_images, args=(request, task_id)).start()
        
        # Return progress bar immediately
        return render(request, 'partials/progress_bar.html', {
            'task_id': task_id,
            'progress': 0
        })


# Maximum number of meal slots a single day can hold.
MAX_MEAL_SLOTS = 6


# Planner scoping lives in main/planner.py, shared with the API.
_get_user_group = planner.get_user_group
_workspace_owner_filter = planner.owner_filter
_get_workspace_items = planner.workspace_items
_get_freezer_items = planner.freezer_items


def _next_slot_for_day(plan_date, mp_filter):
    """Return the next slot number for a day (max existing slot + 1)."""
    last = MealPlan.objects.filter(date=plan_date, **mp_filter).aggregate(m=Max('slot'))['m']
    return (last or 0) + 1


def _get_favorite_recipe_ids(user):
    """Recipe ids this user has favorited.

    Unlike the workspace, favorites are never shared with a meal-plan group —
    they belong to the individual user.
    """
    if not user.is_authenticated:
        return set()
    return set(Favorite.objects.filter(user=user).values_list('recipe_id', flat=True))


@require(perms.RECIPE_READ)
def favorite_toggle(request, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)

    recipe = get_object_or_404(Recipe, pk=pk)
    favorite, created = Favorite.objects.get_or_create(user=request.user, recipe=recipe)
    if not created:
        favorite.delete()

    return render(request, 'partials/favorite_button.html', {
        'recipe_id': recipe.id,
        'is_favorite': created,
        # Echoed back so the swapped-in button keeps the styling of whichever
        # view it was clicked from.
        'variant': request.POST.get('variant', 'default'),
        'icon_class': request.POST.get('icon_class', ''),
    })


def _planner_version(user, meal_plans):
    """Short fingerprint of everything the planner poll can change: the
    visible meal plans plus workspace/freezer items. The poll compares this
    against the value it last saw and skips the swap when nothing moved, so
    HTMX doesn't repaint every thumbnail every 3s for no reason."""
    owner_filter = _workspace_owner_filter(user)
    ws = WorkspaceItem.objects.filter(**owner_filter).values_list('id', 'recipe_id', 'custom_text').order_by('id')
    fz = FreezerItem.objects.filter(**owner_filter).values_list('id', 'recipe_id', 'custom_text').order_by('id')
    parts = [f"m{mp.id}:{mp.date}:{mp.slot}:{mp.recipe_id or 0}:{mp.custom_text}" for mp in meal_plans]
    parts += [f"w{i}:{r or 0}:{c}" for i, r, c in ws]
    parts += [f"f{i}:{r or 0}:{c}" for i, r, c in fz]
    parts.sort()
    return hashlib.md5('|'.join(parts).encode()).hexdigest()[:12]


@require(perms.MEALPLAN_READ)
def meal_planner(request):
    date_str = request.GET.get('date', '')
    try:
        current_date = date_class.fromisoformat(date_str)
    except (ValueError, AttributeError):
        current_date = date_class.today()

    context = _build_planner_context(request, current_date)
    context['app_version'] = settings.VERSION

    if request.htmx:
        if request.GET.get('poll'):
            # Nothing changed since the client's last poll: a 204 tells HTMX
            # not to touch the DOM at all, so thumbnails don't repaint for no
            # reason every 3s.
            if request.GET.get('v') == context['planner_version']:
                return HttpResponse(status=204)
            context['workspace_items'] = _get_workspace_items(request.user)
            context['freezer_items'] = _get_freezer_items(request.user)
            return render(request, 'partials/meal_planner_poll.html', context)
        return render(request, 'partials/meal_planner_calendar.html', context)
    context['workspace_items'] = _get_workspace_items(request.user)
    context['freezer_items'] = _get_freezer_items(request.user)
    return render(request, 'main/meal_planner.html', context)


def _build_planner_context(request, current_date):
    today = date_class.today()

    group = _get_user_group(request.user)
    mp_filter = {'group': group} if group else {'user': request.user, 'group': None}

    days_since_monday = current_date.weekday()
    week1_start = current_date - timedelta(days=days_since_monday)
    period_end = week1_start + timedelta(days=13)

    meal_plans = MealPlan.objects.filter(
        **mp_filter,
        date__range=[week1_start, period_end],
    ).select_related('recipe')

    # Group each day's meal plans by date (already ordered by slot via Meta).
    plans_by_date = defaultdict(list)
    for mp in meal_plans:
        plans_by_date[mp.date].append(mp)

    weeks = []
    for w in range(2):
        week_start = week1_start + timedelta(days=w * 7)
        week_end = week_start + timedelta(days=6)
        days = []
        for i in range(7):
            day_date = week_start + timedelta(days=i)
            day_plans = plans_by_date.get(day_date, [])
            days.append({
                'date': day_date,
                'day_name': day_date.strftime('%a'),
                'day_number': day_date.day,
                'month_name': day_date.strftime('%b'),
                'is_today': day_date == today,
                'meal_plans': day_plans,
                'can_add': len(day_plans) < MAX_MEAL_SLOTS,
            })
        ws, we = week_start, week_end
        weeks.append({
            'days': days,
            'label': f"{ws.strftime('%b')} {ws.day} – {we.strftime('%b')} {we.day}",
        })

    period_label = f"{week1_start.strftime('%b')} {week1_start.day} – {period_end.strftime('%b')} {period_end.day}, {period_end.year}"

    # The mobile view focuses on a single day (current_date) and navigates by day.
    focus_plans = plans_by_date.get(current_date, [])
    current_day = {
        'date': current_date,
        'is_today': current_date == today,
        'meal_plans': focus_plans,
        'can_add': len(focus_plans) < MAX_MEAL_SLOTS,
    }

    return {
        'weeks': weeks,
        'current_date': current_date,
        'period_label': period_label,
        'prev_date': (week1_start - timedelta(days=14)).isoformat(),
        'next_date': (week1_start + timedelta(days=14)).isoformat(),
        'today_date': today.isoformat(),
        'current_day': current_day,
        'current_day_label': f"{current_date.strftime('%A')}, {current_date.strftime('%b')} {current_date.day}",
        'mobile_prev_date': (current_date - timedelta(days=1)).isoformat(),
        'mobile_next_date': (current_date + timedelta(days=1)).isoformat(),
        'planner_version': _planner_version(request.user, meal_plans),
        'household': group,
    }


@require(perms.MEALPLAN_READ_WRITE)
def meal_planner_recipe_picker(request):
    date_str = request.GET.get('date', '')
    slot_num = request.GET.get('slot', '1')
    nav_date = request.GET.get('nav_date', date_class.today().isoformat())
    search_term = request.GET.get('search', '')
    custom_text = request.GET.get('custom_text', '')
    mode = request.GET.get('mode', 'slot')

    recipes = Recipe.objects.all().order_by('name')
    if search_term:
        recipes = recipes.filter(build_recipe_search_q(search_term)).distinct()
    recipes = recipes[:100]

    context = {
        'recipes': recipes,
        'date': date_str,
        'slot_num': slot_num,
        'nav_date': nav_date,
        'search_term': search_term,
        'custom_text': custom_text,
        'mode': mode,
    }
    if mode == 'workspace':
        context['workspace_recipe_ids'] = set(
            _get_workspace_items(request.user).values_list('recipe_id', flat=True)
        )
    elif mode == 'freezer':
        context['freezer_recipe_ids'] = set(
            _get_freezer_items(request.user).values_list('recipe_id', flat=True)
        )
    if request.htmx and 'search' in request.GET:
        return render(request, 'partials/meal_planner_recipe_list.html', context)
    return render(request, 'partials/meal_planner_recipe_picker.html', context)


@require(perms.MEALPLAN_READ_WRITE)
def meal_plan_add(request):
    if request.method != 'POST':
        return HttpResponse(status=405)

    date_str = request.POST.get('date', '')
    slot_raw = request.POST.get('slot', '1')
    recipe_id = request.POST.get('recipe_id')
    custom_text = request.POST.get('custom_text', '').strip()
    nav_date_str = request.POST.get('nav_date', date_str)

    try:
        plan_date = date_class.fromisoformat(date_str)
        nav_date = date_class.fromisoformat(nav_date_str)

        if recipe_id:
            recipe = get_object_or_404(Recipe, pk=recipe_id)
            entry = {'recipe': recipe, 'custom_text': ''}
        elif custom_text:
            entry = {'recipe': None, 'custom_text': custom_text}
        else:
            return HttpResponse(status=400)

        group = _get_user_group(request.user)
        mp_filter = {'group': group} if group else {'user': request.user, 'group': None}

        # A non-numeric slot (e.g. "new") means "append a new slot for this day".
        try:
            slot_num = int(slot_raw)
            is_new_slot = not MealPlan.objects.filter(date=plan_date, slot=slot_num, **mp_filter).exists()
        except (TypeError, ValueError):
            slot_num = _next_slot_for_day(plan_date, mp_filter)
            is_new_slot = True

        # Guard against exceeding the per-day cap when adding a brand-new slot.
        if is_new_slot:
            day_count = MealPlan.objects.filter(date=plan_date, **mp_filter).count()
            if day_count >= MAX_MEAL_SLOTS:
                return HttpResponse(status=400)

        lookup = {'date': plan_date, 'slot': slot_num, 'group': group} if group else {'date': plan_date, 'slot': slot_num, 'user': request.user, 'group': None}

        meal_plan, created = MealPlan.objects.get_or_create(
            **lookup,
            defaults={**entry, 'user': request.user}
        )
        if not created:
            meal_plan.recipe = entry['recipe']
            meal_plan.custom_text = entry['custom_text']
            meal_plan.save()

        context = _build_planner_context(request, nav_date)
        response = render(request, 'partials/meal_planner_calendar.html', context)
        response['HX-Trigger'] = 'closeModal'
        return response
    except Exception:
        return HttpResponse(status=400)


@require(perms.MEALPLAN_READ_WRITE)
def meal_plan_remove(request, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)

    group = _get_user_group(request.user)
    if group:
        meal_plan = get_object_or_404(MealPlan, pk=pk, group=group)
    else:
        meal_plan = get_object_or_404(MealPlan, pk=pk, user=request.user, group=None)
    nav_date_str = request.GET.get('nav_date', '')
    try:
        nav_date = date_class.fromisoformat(nav_date_str)
    except ValueError:
        nav_date = meal_plan.date

    meal_plan.delete()

    context = _build_planner_context(request, nav_date)
    return render(request, 'partials/meal_planner_calendar.html', context)


@require(perms.MEALPLAN_READ_WRITE)
def meal_plan_move(request):
    if request.method != 'POST':
        return HttpResponse(status=405)

    source_id = request.POST.get('source_id')
    target_date_str = request.POST.get('target_date', '')
    nav_date_str = request.POST.get('nav_date', '')

    group = _get_user_group(request.user)
    owner_filter = {'group': group} if group else {'user': request.user, 'group': None}

    try:
        target_date = date_class.fromisoformat(target_date_str)
        source = get_object_or_404(MealPlan, pk=source_id, **owner_filter)

        # A non-numeric target slot (e.g. "new") means "drop into a fresh slot".
        try:
            target_slot = int(request.POST.get('target_slot'))
        except (TypeError, ValueError):
            target_slot = _next_slot_for_day(target_date, owner_filter)

        if not (source.date == target_date and source.slot == target_slot):
            target = MealPlan.objects.filter(
                date=target_date, slot=target_slot, **owner_filter
            ).first()
            if target:
                # Swap the contents of the two slots (keep date/slot fixed to
                # avoid tripping the unique constraints mid-update).
                source.recipe, target.recipe = target.recipe, source.recipe
                source.custom_text, target.custom_text = target.custom_text, source.custom_text
                source.save()
                target.save()
            else:
                # Target empty — just relocate the source slot.
                source.date = target_date
                source.slot = target_slot
                source.save()

        try:
            nav_date = date_class.fromisoformat(nav_date_str)
        except ValueError:
            nav_date = target_date

        context = _build_planner_context(request, nav_date)
        return render(request, 'partials/meal_planner_calendar.html', context)
    except Exception:
        return HttpResponse(status=400)


@require(perms.MEALPLAN_READ_WRITE)
def workspace_add(request, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)

    recipe = get_object_or_404(Recipe, pk=pk)
    group = _get_user_group(request.user)
    lookup = {'recipe': recipe, 'group': group} if group else {'recipe': recipe, 'user': request.user, 'group': None}
    WorkspaceItem.objects.get_or_create(**lookup, defaults={'user': request.user})

    if request.POST.get('source') == 'picker':
        # Added from the picker on the meal-planner page: swap the row's button
        # to an "Added" state and refresh the workspace pane out-of-band.
        return render(request, 'partials/workspace_picker_added.html', {
            'workspace_items': _get_workspace_items(request.user),
            'freezer_items': _get_freezer_items(request.user),
        })
    if request.POST.get('source') == 'list':
        return render(request, 'partials/workspace_button_compact.html', {'recipe_id': recipe.id, 'in_workspace': True})
    return render(request, 'partials/workspace_button.html', {'recipe_id': recipe.id, 'in_workspace': True})


@require(perms.MEALPLAN_READ_WRITE)
def workspace_add_custom(request):
    if request.method != 'POST':
        return HttpResponse(status=405)

    custom_text = request.POST.get('custom_text', '').strip()
    if not custom_text:
        return HttpResponse(status=400)

    group = _get_user_group(request.user)
    WorkspaceItem.objects.create(custom_text=custom_text, user=request.user, group=group)

    response = render(request, 'partials/meal_planner_workspace.html', {
        'workspace_items': _get_workspace_items(request.user),
        'freezer_items': _get_freezer_items(request.user),
    })
    response['HX-Trigger'] = 'closeModal'
    return response


@require(perms.MEALPLAN_READ_WRITE)
def workspace_remove_item(request, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)

    WorkspaceItem.objects.filter(pk=pk, **_workspace_owner_filter(request.user)).delete()

    return render(request, 'partials/meal_planner_workspace.html', {
        'workspace_items': _get_workspace_items(request.user),
        'freezer_items': _get_freezer_items(request.user),
    })


@require(perms.MEALPLAN_READ_WRITE)
def workspace_remove(request, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)

    WorkspaceItem.objects.filter(recipe_id=pk, **_workspace_owner_filter(request.user)).delete()

    if request.POST.get('source') == 'workspace':
        return render(request, 'partials/meal_planner_workspace.html', {
            'workspace_items': _get_workspace_items(request.user),
            'freezer_items': _get_freezer_items(request.user),
        })
    if request.POST.get('source') == 'list':
        return render(request, 'partials/workspace_button_compact.html', {'recipe_id': int(pk), 'in_workspace': False})
    return render(request, 'partials/workspace_button.html', {'recipe_id': int(pk), 'in_workspace': False})


@require(perms.MEALPLAN_READ_WRITE)
def freezer_add(request, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)

    recipe = get_object_or_404(Recipe, pk=pk)
    group = _get_user_group(request.user)
    lookup = {'recipe': recipe, 'group': group} if group else {'recipe': recipe, 'user': request.user, 'group': None}
    FreezerItem.objects.get_or_create(**lookup, defaults={'user': request.user})

    # Reached only from the planner's freezer picker: swap the row's button to
    # "Added" and refresh the freezer strip out-of-band (keeps the tab active).
    return render(request, 'partials/freezer_picker_added.html', {
        'freezer_items': _get_freezer_items(request.user),
    })


@require(perms.MEALPLAN_READ_WRITE)
def freezer_add_custom(request):
    if request.method != 'POST':
        return HttpResponse(status=405)

    custom_text = request.POST.get('custom_text', '').strip()
    if not custom_text:
        return HttpResponse(status=400)

    group = _get_user_group(request.user)
    FreezerItem.objects.create(custom_text=custom_text, user=request.user, group=group)

    response = render(request, 'partials/meal_planner_freezer_strip.html', {
        'freezer_items': _get_freezer_items(request.user),
    })
    response['HX-Trigger'] = 'closeModal'
    return response


@require(perms.MEALPLAN_READ_WRITE)
def freezer_remove_item(request, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)

    FreezerItem.objects.filter(pk=pk, **_workspace_owner_filter(request.user)).delete()

    return render(request, 'partials/meal_planner_freezer_strip.html', {
        'freezer_items': _get_freezer_items(request.user),
    })


@login_required
def whats_new_dismiss(request):
    """Mark the current app version as seen so the "What's New" popup stops showing.

    Stored on the user's profile (server-side) so dismissal applies on every device.
    """
    if request.method != 'POST':
        return HttpResponse(status=405)

    UserProfile.objects.update_or_create(
        user=request.user,
        defaults={'last_seen_version': settings.VERSION},
    )
    return HttpResponse(status=204)


# --- Audit log ---------------------------------------------------------------

AUDIT_PER_PAGE_CHOICES = [25, 50, 100, 200]
AUDIT_DEFAULT_PER_PAGE = 50
# Pruning piggybacks on page loads rather than needing a cron entry, but only
# ever runs once an hour however many times the page is opened.
AUDIT_PRUNE_INTERVAL = 60 * 60
AUDIT_PRUNE_CACHE_KEY = 'audit_log_last_prune'


def _maybe_prune_audit_log():
    """Apply the retention policy, at most once per AUDIT_PRUNE_INTERVAL."""
    if cache.get(AUDIT_PRUNE_CACHE_KEY):
        return
    cache.set(AUDIT_PRUNE_CACHE_KEY, True, timeout=AUDIT_PRUNE_INTERVAL)
    try:
        AuditLog.prune()
    except Exception:
        # Never let housekeeping take the page down.
        pass


def _audit_filters(request):
    """The filter values currently in force, straight off the query string."""
    return {
        'search': request.GET.get('audit_search', '').strip(),
        'object_type': request.GET.get('object_type', '').strip(),
        'action': request.GET.get('action', '').strip(),
        'actor': request.GET.get('actor', '').strip(),
    }


def _audit_queryset(filters):
    entries = AuditLog.objects.select_related('actor')

    if filters['object_type']:
        entries = entries.filter(object_type=filters['object_type'])
    if filters['action']:
        entries = entries.filter(action=filters['action'])
    if filters['actor']:
        if filters['actor'] == 'system':
            entries = entries.filter(actor__isnull=True)
        elif filters['actor'].isdigit():
            entries = entries.filter(actor_id=int(filters['actor']))
    if filters['search']:
        term = filters['search']
        entries = entries.filter(
            Q(object_label__icontains=term)
            | Q(actor_label__icontains=term)
            | Q(object_type__icontains=term)
        )
    return entries


def _audit_context(request):
    """Everything partials/audit_log_body.html needs to redraw itself."""
    filters = _audit_filters(request)
    entries = _audit_queryset(filters)

    try:
        per_page = int(request.GET.get('per_page', AUDIT_DEFAULT_PER_PAGE))
    except (TypeError, ValueError):
        per_page = AUDIT_DEFAULT_PER_PAGE
    if per_page not in AUDIT_PER_PAGE_CHOICES:
        per_page = AUDIT_DEFAULT_PER_PAGE

    paginator = Paginator(entries, per_page)
    try:
        page = paginator.page(request.GET.get('page', 1))
    except PageNotAnInteger:
        page = paginator.page(1)
    except EmptyPage:
        page = paginator.page(paginator.num_pages)

    return {
        'entries': page,
        'total_count': paginator.count,
        'per_page': per_page,
        'per_page_choices': AUDIT_PER_PAGE_CHOICES,
        'filters': filters,
        'audit_search': filters['search'],
        # Only offer types that actually appear, so the filter can't come up empty.
        'object_types': sorted(
            AuditLog.objects.order_by().values_list('object_type', flat=True).distinct()
        ),
        'actions': AuditLog.ACTION_CHOICES,
        'actors': User.objects.filter(audit_entries__isnull=False).distinct().order_by('username'),
        'retention_days': AuditSettings.load().retention_days,
        'app_version': settings.VERSION,
    }


@require(perms.AUDIT_LOG)
def audit_log(request):
    _maybe_prune_audit_log()
    return render(request, 'main/audit_log.html', _audit_context(request))


@require(perms.AUDIT_LOG)
def audit_log_list(request):
    """The table on its own, for htmx to swap in as filters change."""
    return render(request, 'partials/audit_log_body.html', _audit_context(request))


@require(perms.PLATFORM_ADMIN)
def audit_retention(request):
    """Save the retention setting, then apply it straight away."""
    if request.method != 'POST':
        return HttpResponse(status=405)

    audit_settings = AuditSettings.load()
    form = AuditRetentionForm(request.POST, instance=audit_settings)
    context = {'retention_form': form}

    if form.is_valid():
        form.save()
        # Enforce the new policy now rather than waiting for the next prune
        # window, so shortening retention visibly does something.
        cache.delete(AUDIT_PRUNE_CACHE_KEY)
        removed = AuditLog.prune()
        context.update({
            'retention_form': AuditRetentionForm(instance=AuditSettings.load()),
            'retention_saved': True,
            'retention_removed': removed,
        })

    context['retention_days'] = AuditSettings.load().retention_days
    return render(request, 'partials/audit_retention_form.html', context)


# --- Your own password -------------------------------------------------------

class PasswordChange(PasswordChangeView):
    """Change your own password; also where a forced change lands."""

    template_name = 'main/password_change_form.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['forced'] = UserProfile.objects.filter(
            user=self.request.user, must_change_password=True).exists()
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        UserProfile.objects.filter(user=self.request.user).update(must_change_password=False)
        return response


# --- User management ---------------------------------------------------------

@require(perms.USER_MGMT)
def users(request):
    user_list = User.objects.select_related('profile__role', 'profile__household').order_by(Lower('username'))
    return render(request, 'main/users.html', {'user_list': user_list})


@require(perms.USER_MGMT)
def user_add(request):
    form = UserAddForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        new_user = form.save()
        messages.success(request, f'Added {new_user.username}.')
        return redirect('users')
    return render(request, 'main/user_add.html', {'form': form})


def _user_edit_page(request, target, form=None, password_form=None):
    return render(request, 'main/user_edit.html', {
        'target': target,
        'is_self': target.pk == request.user.pk,
        'form': form or UserEditForm(instance=target, acting_user=request.user),
        'password_form': password_form or AdminSetPasswordForm(target),
    })


@require(perms.USER_MGMT)
def user_edit(request, pk):
    target = get_object_or_404(User.objects.select_related('profile__role'), pk=pk)
    if request.method != 'POST':
        return _user_edit_page(request, target)

    form = UserEditForm(request.POST, instance=target, acting_user=request.user)
    if not form.is_valid():
        return _user_edit_page(request, target, form=form)
    form.save()
    messages.success(request, f'Saved {target.username}.')
    if not perms.has_perm(_fresh(request.user), perms.USER_MGMT):
        # Took user management away from themselves (someone else still has it).
        return redirect('profile')
    return redirect('users')


def _fresh(user):
    """The user re-read from the database, without cached permissions."""
    return User.objects.get(pk=user.pk)


@require(perms.USER_MGMT)
def user_set_password(request, pk):
    target = get_object_or_404(User.objects.select_related('profile__role'), pk=pk)
    if request.method != 'POST':
        return redirect('user-edit', pk=pk)

    password_form = AdminSetPasswordForm(target, request.POST)
    if not password_form.is_valid():
        return _user_edit_page(request, target, password_form=password_form)
    password_form.save()
    if target.pk == request.user.pk:
        # Changing a password ends every session that used the old one --
        # which is the point for everyone else, but shouldn't log you out.
        update_session_auth_hash(request, target)
    messages.success(request, f"Set a new password for {target.username}.")
    return redirect('user-edit', pk=pk)


def _user_delete_blocker(request, target):
    """Why this user can't be deleted, or None if they can."""
    if target.pk == request.user.pk:
        return "You can't delete your own account."
    if perms.leaves_no_user_manager(deleted_user=target.pk):
        return perms.LOCKOUT_MESSAGE
    return None


@require(perms.USER_MGMT)
def user_delete(request, pk):
    target = get_object_or_404(User.objects.select_related('profile__role'), pk=pk)
    blocker = _user_delete_blocker(request, target)

    if request.method == 'POST' and not blocker:
        username = target.username
        # Entries in a shared household planner stay, just without a name
        # against them (the foreign keys are SET_NULL). Personal ones nobody
        # else can see would only be orphaned, so they go with the user.
        for model in (MealPlan, WorkspaceItem, FreezerItem):
            model.objects.filter(user=target, group__isnull=True).delete()
        target.delete()
        messages.success(request, f'Deleted {username}.')
        return redirect('users')

    return render(request, 'main/user_delete.html', {
        'target': target,
        'blocker': blocker,
        'favorite_count': target.favorites.count(),
    })


# --- Roles -------------------------------------------------------------------

@require(perms.USER_MGMT)
def roles(request):
    # System roles first, in the order they're defined, then custom roles by name.
    system_order = list(perms.SYSTEM_ROLES)
    role_list = sorted(Role.objects.annotate(user_count=Count('profiles')), key=lambda r: (
        system_order.index(r.system_key) if r.system_key in system_order else len(system_order),
        r.name.lower()))
    for role in role_list:
        codes = role.permission_codes()
        role.granted = [p for p in perms.PERMISSIONS if p.code in codes]
    return render(request, 'main/roles.html', {'role_list': role_list})


def _role_form_page(request, form, role=None):
    return render(request, 'main/role_form.html', {
        'form': form,
        'role': role,
        'permission_list': perms.PERMISSIONS,
        'selected': set(form['permissions'].value() or ()),
    })


@require(perms.USER_MGMT)
def role_add(request):
    form = RoleForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        role = form.save()
        messages.success(request, f'Added the {role.name} role.')
        return redirect('roles')
    return _role_form_page(request, form)


@require(perms.USER_MGMT)
def role_edit(request, pk):
    role = get_object_or_404(Role, pk=pk, system_key__isnull=True)
    form = RoleForm(request.POST or None, instance=role)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'Saved the {role.name} role.')
        if not perms.has_perm(_fresh(request.user), perms.USER_MGMT):
            return redirect('profile')
        return redirect('roles')
    return _role_form_page(request, form, role=role)


@require(perms.USER_MGMT)
def role_delete(request, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)
    role = get_object_or_404(Role, pk=pk, system_key__isnull=True)
    in_use = role.profiles.count()
    if in_use:
        messages.error(request, f'{role.name} is assigned to {in_use} user{"s" if in_use != 1 else ""}. '
                                'Give them another role before deleting it.')
    else:
        role.delete()
        messages.success(request, f'Deleted the {role.name} role.')
    return redirect('roles')


# --- Households --------------------------------------------------------------

@require(perms.USER_MGMT)
def households(request):
    household_list = MealPlanGroup.objects.annotate(
        entry_count=Count('meal_plans', distinct=True),
    ).order_by(Lower('name')).prefetch_related('profiles__user')
    for household in household_list:
        household.member_list = sorted((p.user for p in household.profiles.all()),
                                       key=lambda u: u.username.lower())
    return render(request, 'main/households.html', {
        'household_list': household_list,
        'unassigned': User.objects.filter(profile__household__isnull=True).order_by(Lower('username')),
    })


def _household_form_page(request, form, household=None):
    return render(request, 'main/household_form.html', {'form': form, 'household': household})


@require(perms.USER_MGMT)
def household_add(request):
    form = HouseholdForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        household = form.save()
        messages.success(request, f'Added the {household.name} household.')
        return redirect('households')
    return _household_form_page(request, form)


@require(perms.USER_MGMT)
def household_edit(request, pk):
    household = get_object_or_404(MealPlanGroup, pk=pk)
    form = HouseholdForm(request.POST or None, instance=household)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'Saved the {household.name} household.')
        return redirect('households')
    return _household_form_page(request, form, household=household)


@require(perms.USER_MGMT)
def household_delete(request, pk):
    if request.method != 'POST':
        return HttpResponse(status=405)
    household = get_object_or_404(MealPlanGroup, pk=pk)
    in_use = household.profiles.count()
    if in_use:
        messages.error(request, f'{household.name} has {in_use} member{"s" if in_use != 1 else ""}. '
                                'Move them out before deleting it.')
    else:
        # Its planner, workspace and freezer go with it (CASCADE); nobody can
        # see them any more once it has no members.
        household.delete()
        messages.success(request, f'Deleted the {household.name} household.')
    return redirect('households')


# --- Platform settings -------------------------------------------------------

@require(perms.PLATFORM_ADMIN)
def platform_settings(request):
    return render(request, 'main/platform_settings.html', {
        'retention_form': AuditRetentionForm(instance=AuditSettings.load()),
        'api_settings_form': ApiSettingsForm(instance=ApiSettings.load()),
        'recipe_count': Recipe.objects.count(),
        **_api_sessions_context(request, own=False),
    })


@require(perms.PLATFORM_ADMIN)
def api_settings(request):
    """Save the API idle timeout. Applies to sessions already started too."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    form = ApiSettingsForm(request.POST, instance=ApiSettings.load())
    context = {'api_settings_form': form}
    if form.is_valid():
        form.save()
        context.update({
            'api_settings_form': ApiSettingsForm(instance=ApiSettings.load()),
            'api_settings_saved': True,
        })
    return render(request, 'partials/api_settings_form.html', context)


@require(perms.PLATFORM_ADMIN)
def api_session_revoke(request, pk):
    """End anyone's API session."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    get_object_or_404(ApiSession, pk=pk).delete()
    return render(request, 'partials/api_sessions.html', _api_sessions_context(request, own=False))
