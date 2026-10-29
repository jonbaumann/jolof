import datetime
import os
import shutil
import tempfile
from datetime import date as date_class, timedelta
from unittest import mock
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from main import audit, permissions, recipe_query, safe_fetch, timezones, views
from main.api import authentication as api_auth
from main.forms import URLScrapeFormBatch
from main.models import (
    ApiSession,
    ApiSettings,
    AuditLog,
    AuditSettings,
    Favorite,
    FreezerItem,
    MealPlan,
    MealPlanGroup,
    Recipe,
    RecipeSlugHistory,
    Role,
    Tag,
    UserProfile,
    WorkspaceItem,
)


DIETARY_FIELDS = ('gluten_free', 'vegetarian', 'vegan', 'dairy_free')


def dietary_post_data(recipe):
    """The allergy-info tick boxes as the browser would submit them.

    An unticked box posts nothing at all, which is why only the True ones
    appear here.
    """
    return {name: 'on' for name in DIETARY_FIELDS if getattr(recipe, name)}


class RecipeSlugTests(TestCase):
    """Slug generation, de-duplication, and rename handling."""

    def test_slug_generated_from_name(self):
        recipe = Recipe.objects.create(name='Air Fryer Cuban Sweet Plantains')
        self.assertEqual(recipe.slug, 'air-fryer-cuban-sweet-plantains')

    def test_special_characters_are_stripped(self):
        recipe = Recipe.objects.create(name="Mom's Mac & Cheese (Best Ever!)")
        self.assertEqual(recipe.slug, 'moms-mac-cheese-best-ever')

    def test_duplicate_names_get_numeric_suffixes(self):
        first = Recipe.objects.create(name='Caramelized Bananas')
        second = Recipe.objects.create(name='Caramelized Bananas')
        third = Recipe.objects.create(name='Caramelized Bananas')
        self.assertEqual(first.slug, 'caramelized-bananas')
        self.assertEqual(second.slug, 'caramelized-bananas-2')
        self.assertEqual(third.slug, 'caramelized-bananas-3')

    def test_name_with_no_sluggable_characters_falls_back(self):
        first = Recipe.objects.create(name='餃子')
        second = Recipe.objects.create(name='🔥🔥🔥')
        self.assertEqual(first.slug, 'recipe')
        self.assertEqual(second.slug, 'recipe-2')

    def test_long_name_is_truncated_within_field_limit(self):
        recipe = Recipe.objects.create(name='Sourdough ' * 10)
        self.assertLessEqual(len(recipe.slug), 140)
        self.assertFalse(recipe.slug.endswith('-'))

    def test_saving_without_renaming_leaves_slug_alone(self):
        recipe = Recipe.objects.create(name='Caramelized Bananas')
        Recipe.objects.create(name='Caramelized Bananas')  # occupies the clean slug's -2

        recipe = Recipe.objects.get(pk=recipe.pk)
        recipe.rating = 5
        recipe.save()

        self.assertEqual(recipe.slug, 'caramelized-bananas')
        self.assertFalse(RecipeSlugHistory.objects.exists())

    def test_rename_moves_slug_and_retires_the_old_one(self):
        recipe = Recipe.objects.create(name='Cuban Plantains')

        recipe = Recipe.objects.get(pk=recipe.pk)
        recipe.name = 'Sweet Plantains'
        recipe.save()

        self.assertEqual(recipe.slug, 'sweet-plantains')
        self.assertEqual(
            list(recipe.slug_history.values_list('slug', flat=True)),
            ['cuban-plantains'],
        )

    def test_renaming_back_reclaims_the_original_slug(self):
        recipe = Recipe.objects.create(name='Cuban Plantains')

        recipe = Recipe.objects.get(pk=recipe.pk)
        recipe.name = 'Sweet Plantains'
        recipe.save()

        recipe = Recipe.objects.get(pk=recipe.pk)
        recipe.name = 'Cuban Plantains'
        recipe.save()

        self.assertEqual(recipe.slug, 'cuban-plantains')
        self.assertEqual(
            list(recipe.slug_history.values_list('slug', flat=True)),
            ['sweet-plantains'],
        )

    def test_retired_slug_is_not_handed_to_another_recipe(self):
        original = Recipe.objects.create(name='Cuban Plantains')
        original = Recipe.objects.get(pk=original.pk)
        original.name = 'Sweet Plantains'
        original.save()

        newcomer = Recipe.objects.create(name='Cuban Plantains')

        self.assertEqual(newcomer.slug, 'cuban-plantains-2')

    def test_history_is_removed_with_the_recipe(self):
        recipe = Recipe.objects.create(name='Cuban Plantains')
        recipe = Recipe.objects.get(pk=recipe.pk)
        recipe.name = 'Sweet Plantains'
        recipe.save()

        recipe.delete()

        self.assertFalse(RecipeSlugHistory.objects.exists())


class RecipeDetailUrlTests(TestCase):
    """Canonical slug route plus the redirects that keep old links alive."""

    def setUp(self):
        User.objects.create_user(username='cook', password='secret-for-tests')
        self.client.login(username='cook', password='secret-for-tests')
        self.recipe = Recipe.objects.create(name='Air Fryer Cuban Sweet Plantains')

    def test_canonical_url_serves_the_recipe(self):
        response = self.client.get('/recipe/air-fryer-cuban-sweet-plantains/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Air Fryer Cuban Sweet Plantains')

    def test_get_absolute_url_matches_the_canonical_route(self):
        self.assertEqual(
            self.recipe.get_absolute_url(),
            '/recipe/air-fryer-cuban-sweet-plantains/',
        )

    def test_unknown_slug_returns_404(self):
        response = self.client.get('/recipe/no-such-recipe/')
        self.assertEqual(response.status_code, 404)

    def test_legacy_id_url_redirects_permanently(self):
        response = self.client.get(f'/recipe-detail/{self.recipe.pk}/')
        self.assertRedirects(
            response,
            '/recipe/air-fryer-cuban-sweet-plantains/',
            status_code=301,
        )

    def test_legacy_id_url_keeps_the_query_string(self):
        response = self.client.get(f'/recipe-detail/{self.recipe.pk}/?page=2')
        self.assertRedirects(
            response,
            '/recipe/air-fryer-cuban-sweet-plantains/?page=2',
            status_code=301,
        )

    def test_legacy_id_url_for_missing_recipe_returns_404(self):
        response = self.client.get('/recipe-detail/99999999/')
        self.assertEqual(response.status_code, 404)

    def test_legacy_slug_url_redirects_permanently(self):
        response = self.client.get('/recipe-detail/air-fryer-cuban-sweet-plantains/')
        self.assertRedirects(
            response,
            '/recipe/air-fryer-cuban-sweet-plantains/',
            status_code=301,
        )

    def test_retired_slug_redirects_to_the_current_url(self):
        recipe = Recipe.objects.get(pk=self.recipe.pk)
        recipe.name = 'Air Fryer Plantains'
        recipe.save()

        response = self.client.get('/recipe/air-fryer-cuban-sweet-plantains/')
        self.assertRedirects(
            response,
            '/recipe/air-fryer-plantains/',
            status_code=301,
        )

    def test_detail_view_requires_login(self):
        self.client.logout()
        response = self.client.get('/recipe/air-fryer-cuban-sweet-plantains/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])
        self.assertIn('next=/recipe/air-fryer-cuban-sweet-plantains/', response['Location'])


class TagManageTests(TestCase):
    """The Manage Tags tool: listing, live search, add, rename, merge, delete."""

    def setUp(self):
        User.objects.create_user(username='cook', password='secret-for-tests')
        self.client.login(username='cook', password='secret-for-tests')

        self.asian = Tag.objects.create(label='cuisine', value='asian')
        self.italian = Tag.objects.create(label='cuisine', value='italian')
        self.quick = Tag.objects.create(label='speed', value='quick')

        self.pho = Recipe.objects.create(name='Pho')
        self.pho.tags.add(self.asian)
        self.ramen = Recipe.objects.create(name='Ramen')
        self.ramen.tags.add(self.asian, self.quick)

    def test_page_requires_login(self):
        self.client.logout()
        response = self.client.get('/tags/manage/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_page_lists_every_tag_with_its_recipe_count(self):
        response = self.client.get('/tags/manage/')
        self.assertEqual(response.status_code, 200)
        counts = {str(tag): tag.recipe_count for tag in response.context['tags']}
        self.assertEqual(counts, {'cuisine:asian': 2, 'cuisine:italian': 0, 'speed:quick': 1})

    def test_count_links_to_a_prefilled_recipe_search(self):
        self.assertEqual(
            self.asian.get_recipe_search_url(),
            '/recipes/?search_term=cuisine%3Aasian',
        )
        response = self.client.get('/tags/manage/')
        self.assertContains(response, 'search_term=cuisine%3Aasian')

    def test_prefilled_search_finds_the_tagged_recipes(self):
        response = self.client.get(self.asian.get_recipe_search_url())
        names = {recipe.name for recipe in response.context['recipe_list']}
        self.assertEqual(names, {'Pho', 'Ramen'})

    def test_search_matches_either_half_of_a_tag(self):
        response = self.client.get('/tags/manage/list/', {'tag_search': 'quick'})
        self.assertEqual([str(t) for t in response.context['tags']], ['speed:quick'])

    def test_search_terms_are_anded_together(self):
        response = self.client.get('/tags/manage/list/', {'tag_search': 'cuisine ital'})
        self.assertEqual([str(t) for t in response.context['tags']], ['cuisine:italian'])

    def test_label_colon_search_lists_that_labels_values(self):
        response = self.client.get('/tags/manage/list/', {'tag_search': 'cuisine:'})
        self.assertEqual(
            [str(t) for t in response.context['tags']],
            ['cuisine:asian', 'cuisine:italian'],
        )

    def test_add_creates_a_tag(self):
        response = self.client.post('/tags/manage/add/', {'label': 'diet', 'value': 'vegan'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Tag.objects.filter(label='diet', value='vegan').exists())
        self.assertEqual(response['HX-Trigger'], 'tagAdded')

    def test_add_rejects_a_missing_half(self):
        response = self.client.post('/tags/manage/add/', {'label': 'diet', 'value': '  '})
        self.assertEqual(Tag.objects.count(), 3)
        self.assertIn('needs both a label and a value', response.context['error'])

    def test_add_rejects_a_case_insensitive_duplicate(self):
        response = self.client.post('/tags/manage/add/', {'label': 'Cuisine', 'value': 'ASIAN'})
        self.assertEqual(Tag.objects.count(), 3)
        self.assertIn('already exists', response.context['error'])

    def test_add_keeps_the_active_search_filter(self):
        response = self.client.post(
            '/tags/manage/add/',
            {'label': 'speed', 'value': 'slow', 'tag_search': 'speed'},
        )
        self.assertEqual(
            [str(t) for t in response.context['tags']],
            ['speed:quick', 'speed:slow'],
        )

    def test_edit_renames_the_tag_for_every_recipe(self):
        self.client.post(
            f'/tags/manage/{self.asian.pk}/edit/',
            {'label': 'cuisine', 'value': 'east asian'},
        )
        self.asian.refresh_from_db()
        self.assertEqual(str(self.asian), 'cuisine:east asian')
        self.assertEqual(
            [str(t) for t in self.pho.tags.all()],
            ['cuisine:east asian'],
        )

    def test_edit_onto_an_existing_tag_merges_them(self):
        response = self.client.post(
            f'/tags/manage/{self.asian.pk}/edit/',
            {'label': 'cuisine', 'value': 'italian'},
        )
        self.assertFalse(Tag.objects.filter(pk=self.asian.pk).exists())
        self.assertEqual([str(t) for t in self.pho.tags.all()], ['cuisine:italian'])
        self.assertEqual(
            {r.name for r in self.italian.recipes.all()},
            {'Pho', 'Ramen'},
        )
        self.assertIn('Merged', response.context['message'])

    def test_edit_rejects_a_missing_half_and_stays_in_edit_mode(self):
        response = self.client.post(
            f'/tags/manage/{self.asian.pk}/edit/',
            {'label': '', 'value': 'asian'},
        )
        self.asian.refresh_from_db()
        self.assertEqual(str(self.asian), 'cuisine:asian')
        self.assertEqual(response.context['editing_pk'], self.asian.pk)

    def test_delete_removes_the_tag_from_its_recipes(self):
        response = self.client.post(f'/tags/manage/{self.asian.pk}/delete/')
        self.assertFalse(Tag.objects.filter(pk=self.asian.pk).exists())
        self.assertEqual(self.pho.tags.count(), 0)
        self.assertEqual([str(t) for t in self.ramen.tags.all()], ['speed:quick'])
        self.assertIn('2 recipes', response.context['message'])

    def test_edit_and_delete_reject_get(self):
        self.assertEqual(self.client.get(f'/tags/manage/{self.asian.pk}/edit/').status_code, 405)
        self.assertEqual(self.client.get(f'/tags/manage/{self.asian.pk}/delete/').status_code, 405)
        self.assertEqual(self.client.get('/tags/manage/add/').status_code, 405)


# Smallest valid 1x1 PNG -- enough for ImageField's Pillow check to pass.
ONE_PIXEL_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06'
    b'\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05'
    b'\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
)

# Media storage points at S3 in every environment, so these tests must override
# it -- otherwise running the suite uploads junk to the real dev bucket.
_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix='jolof-test-media-')

_LOCAL_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


# Pointing MEDIA_ROOT at the temp dir is what keeps these files out of the repo.
# (It's set there rather than in STORAGES["default"]["OPTIONS"] because Django 4.2
# dropped OPTIONS when STORAGES was overridden; MEDIA_ROOT works on any version.)
@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class RecipeImageClearTests(TestCase):
    """Clearing and replacing a recipe photo, and the storage cleanup both need."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        User.objects.create_user(username='cook', password='secret-for-tests')
        self.client.login(username='cook', password='secret-for-tests')
        self.recipe = Recipe.objects.create(
            name='Pho', ingredients='noodles', instructions='boil',
        )
        self.recipe.image.save('pho.png', ContentFile(ONE_PIXEL_PNG), save=True)
        self.storage = Recipe._meta.get_field('image').storage
        self.original_name = self.recipe.image.name

    def post_data(self, **extra):
        """A complete, valid recipe-update payload; the required fields only."""
        data = {
            'name': 'Pho',
            'ingredients': 'noodles',
            'instructions': 'boil',
            'description': '',
            'notes': '',
            'url': '',
            'tags_input': '',
        }
        data.update(extra)
        return data

    def update(self, **extra):
        return self.client.post(f'/recipe-update/{self.recipe.pk}/', self.post_data(**extra))

    def test_update_page_offers_a_clear_checkbox(self):
        response = self.client.get(f'/recipe-update/{self.recipe.pk}/')
        self.assertContains(response, 'image-clear')

    def test_clearing_empties_the_image_field(self):
        self.update(**{'image-clear': 'on'})
        self.recipe.refresh_from_db()
        self.assertFalse(self.recipe.image)

    def test_clearing_deletes_the_underlying_file(self):
        self.assertTrue(self.storage.exists(self.original_name))
        self.update(**{'image-clear': 'on'})
        self.assertFalse(self.storage.exists(self.original_name))

    def test_clearing_redirects_like_any_other_successful_save(self):
        response = self.update(**{'image-clear': 'on'})
        self.assertRedirects(
            response,
            '/recipe/pho/',
            fetch_redirect_response=False,
        )

    def test_clearing_does_not_discard_other_edits_in_the_same_save(self):
        # The bug this guards: model validation rejected the blank image, so the
        # whole form failed and every other edit on the page was silently lost.
        self.update(**{'image-clear': 'on', 'notes': 'freeze the broth'})
        self.recipe.refresh_from_db()
        self.assertFalse(self.recipe.image)
        self.assertEqual(self.recipe.notes, 'freeze the broth')

    def test_detail_page_falls_back_to_the_default_image(self):
        self.update(**{'image-clear': 'on'})
        response = self.client.get('/recipe/pho/')
        self.assertContains(response, 'fork_knife.svg')

    def test_saving_without_clearing_keeps_the_image(self):
        self.update(notes='untouched')
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.image.name, self.original_name)
        self.assertTrue(self.storage.exists(self.original_name))

    def replace_image(self, filename='new.png'):
        upload = SimpleUploadedFile(filename, ONE_PIXEL_PNG, content_type='image/png')
        return self.client.post(
            f'/recipe-update/{self.recipe.pk}/',
            self.post_data(image=upload),
        )

    def test_uploading_a_replacement_still_works(self):
        self.replace_image()
        self.recipe.refresh_from_db()
        self.assertTrue(self.recipe.image)
        self.assertIn('new', self.recipe.image.name)

    def test_replacing_deletes_the_file_it_replaced(self):
        self.assertTrue(self.storage.exists(self.original_name))
        self.replace_image()
        self.assertFalse(self.storage.exists(self.original_name))

    def test_replacing_keeps_the_new_file(self):
        self.replace_image()
        self.recipe.refresh_from_db()
        self.assertNotEqual(self.recipe.image.name, self.original_name)
        self.assertTrue(self.storage.exists(self.recipe.image.name))

    def test_adding_a_first_image_deletes_nothing(self):
        bare = Recipe.objects.create(name='Congee', ingredients='rice', instructions='simmer')
        upload = SimpleUploadedFile('congee.png', ONE_PIXEL_PNG, content_type='image/png')
        self.client.post(
            f'/recipe-update/{bare.pk}/',
            self.post_data(name='Congee', ingredients='rice', instructions='simmer', image=upload),
        )
        bare.refresh_from_db()
        self.assertTrue(self.storage.exists(bare.image.name))
        # The unrelated recipe's photo must be untouched.
        self.assertTrue(self.storage.exists(self.original_name))

    def test_a_recipe_with_no_image_saves_normally(self):
        bare = Recipe.objects.create(name='Congee', ingredients='rice', instructions='simmer')
        response = self.client.post(
            f'/recipe-update/{bare.pk}/',
            self.post_data(name='Congee', ingredients='rice', instructions='simmer', notes='ok'),
        )
        bare.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(bare.notes, 'ok')
        self.assertFalse(bare.image)


class RecipeUpdateErrorDisplayTests(TestCase):
    """A rejected edit has to say so -- it used to re-render silently."""

    def setUp(self):
        User.objects.create_user(username='cook', password='secret-for-tests')
        self.client.login(username='cook', password='secret-for-tests')
        self.recipe = Recipe.objects.create(
            name='Pho', ingredients='noodles', instructions='boil',
        )

    def post(self, follow=False, **extra):
        data = {
            'name': 'Pho',
            'ingredients': 'noodles',
            'instructions': 'boil',
            'description': '',
            'notes': '',
            'url': '',
            'tags_input': '',
        }
        data.update(extra)
        return self.client.post(f'/recipe-update/{self.recipe.pk}/', data, follow=follow)

    def test_a_rejected_edit_redisplays_the_page(self):
        response = self.post(name='')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['form'].is_valid())

    def test_a_rejected_edit_explains_itself(self):
        response = self.post(name='')
        self.assertContains(response, "This recipe wasn't saved")
        self.assertContains(response, 'This field is required.')

    def test_the_offending_field_is_named(self):
        response = self.post(ingredients='')
        self.assertContains(response, 'Please enter the ingredients.')
        self.assertContains(response, '<strong>Ingredients:</strong>', html=False)

    def test_a_rejected_edit_changes_nothing(self):
        self.post(name='', notes='should not stick')
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.name, 'Pho')
        self.assertIsNone(self.recipe.notes)

    def test_a_successful_edit_shows_no_error_banner(self):
        response = self.post(notes='fine', follow=True)
        self.assertNotContains(response, "This recipe wasn't saved")

    def test_the_form_keeps_what_was_typed(self):
        response = self.post(name='', notes='keep me')
        self.assertContains(response, 'keep me')



class AuditLogTests(TestCase):
    """Signal-driven recording: what lands in the log, and what deliberately doesn't."""

    def setUp(self):
        self.user = User.objects.create_user(username='cook', password='pw12345!')
        # Creating that user logged itself; start each test from a clean slate.
        AuditLog.objects.all().delete()

    def _entries(self, **filters):
        return list(AuditLog.objects.filter(**filters).order_by('id'))

    def test_recipe_create_update_delete_are_recorded(self):
        recipe = Recipe.objects.create(name='Jollof Rice')
        recipe = Recipe.objects.get(pk=recipe.pk)
        recipe.rating = 5
        recipe.save()
        recipe.delete()

        actions = [entry.action for entry in self._entries(object_type='Recipe')]
        self.assertEqual(actions, ['created', 'updated', 'deleted'])

    def test_update_records_which_fields_changed_but_not_their_values(self):
        recipe = Recipe.objects.create(name='Jollof Rice', rating=2, notes='Too salty')
        recipe = Recipe.objects.get(pk=recipe.pk)
        recipe.rating = 5
        recipe.vegan = True
        recipe.save()

        entry = AuditLog.objects.get(object_type='Recipe', action='updated')
        self.assertEqual(entry.changed_fields, ['Rating', 'Vegan'])
        # The values themselves are nobody's business here.
        self.assertEqual(entry.changes, {'fields': ['Rating', 'Vegan']})

    def test_save_that_changes_only_prose_names_the_field(self):
        recipe = Recipe.objects.create(name='Jollof Rice', instructions='Rinse the rice.')
        recipe = Recipe.objects.get(pk=recipe.pk)
        recipe.instructions = 'Simmer gently. ' * 200
        recipe.save()

        entry = AuditLog.objects.get(object_type='Recipe', action='updated')
        self.assertEqual(entry.changed_fields, ['Instructions'])
        self.assertNotIn('Simmer', str(entry.changes))

    def test_password_change_names_the_field_and_nothing_else(self):
        user = User.objects.get(pk=self.user.pk)
        user.set_password('a-different-one')
        user.save()

        entry = AuditLog.objects.get(object_type='User', action='updated')
        self.assertEqual(entry.changed_fields, ['Password'])
        self.assertNotIn('a-different-one', str(entry.changes))
        self.assertNotIn('pbkdf2', str(entry.changes))

    def test_line_endings_alone_are_not_a_change(self):
        """A textarea posts CRLF; that must not read as an edit."""
        recipe = Recipe.objects.create(name='Jollof Rice', instructions='Rinse.\nSimmer.')
        AuditLog.objects.all().delete()

        recipe = Recipe.objects.get(pk=recipe.pk)
        recipe.instructions = 'Rinse.\r\nSimmer.'
        recipe.save()

        self.assertFalse(AuditLog.objects.exists())

    def test_long_text_differing_only_past_the_truncation_point_is_caught(self):
        body = 'x' * 400
        recipe = Recipe.objects.create(name='Jollof Rice', instructions=body + 'a')
        AuditLog.objects.all().delete()

        recipe = Recipe.objects.get(pk=recipe.pk)
        recipe.instructions = body + 'b'
        recipe.save()

        self.assertTrue(AuditLog.objects.filter(object_type='Recipe', action='updated').exists())

    def test_legacy_entries_still_show_their_field_names(self):
        """Rows written while the log stored values must still render."""
        entry = AuditLog.objects.create(
            action='updated', object_type='Recipe', object_label='Jollof Rice',
            changes={'rating': {'label': 'Rating', 'from': 2, 'to': 5},
                     'notes': {'label': 'Notes', 'summary': 'Rewritten'}},
        )
        self.assertEqual(entry.changed_fields, ['Notes', 'Rating'])

    def test_rename_keeps_a_working_link_to_the_recipe(self):
        recipe = Recipe.objects.create(name='Jollof Rice')
        entry = AuditLog.objects.get(object_type='Recipe', action='created')

        recipe = Recipe.objects.get(pk=recipe.pk)
        recipe.name = 'Party Jollof'
        recipe.save()

        # The stored URL is the old slug, which now redirects rather than 404s.
        self.client.force_login(self.user)
        response = self.client.get(entry.object_url)
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], recipe.get_absolute_url())

    def test_tag_changes_are_recorded_against_the_recipe(self):
        recipe = Recipe.objects.create(name='Jollof Rice')
        tag = Tag.objects.create(label='cuisine', value='west african')
        AuditLog.objects.all().delete()

        recipe.tags.add(tag)
        self.assertEqual(AuditLog.objects.get(object_type='Recipe').changed_fields, ['Tags'])

        AuditLog.objects.all().delete()
        recipe.tags.remove(tag)
        self.assertEqual(AuditLog.objects.get(object_type='Recipe').changed_fields, ['Tags'])

    def test_tag_clear_is_recorded(self):
        recipe = Recipe.objects.create(name='Jollof Rice')
        recipe.tags.add(Tag.objects.create(label='cuisine', value='west african'))
        AuditLog.objects.all().delete()

        recipe.tags.clear()

        self.assertEqual(AuditLog.objects.get(object_type='Recipe').changed_fields, ['Tags'])

    def test_tags_and_meal_plans_are_covered(self):
        tag = Tag.objects.create(label='cuisine', value='thai')
        recipe = Recipe.objects.create(name='Pad Thai')
        MealPlan.objects.create(date=date_class(2026, 9, 4), slot=1, recipe=recipe, user=self.user)
        FreezerItem.objects.create(recipe=recipe, user=self.user)
        WorkspaceItem.objects.create(recipe=recipe, user=self.user)
        Favorite.objects.create(recipe=recipe, user=self.user)
        tag.delete()

        recorded = set(AuditLog.objects.values_list('object_type', flat=True))
        self.assertEqual(
            recorded,
            {'Tag', 'Recipe', 'Meal plan', 'Freezer item', 'Workspace item', 'Favorite'},
        )

    def test_new_user_is_recorded_and_attributed_to_themselves(self):
        User.objects.create_user(username='newcomer', password='pw12345!')

        entry = AuditLog.objects.get(object_type='User', action='created')
        self.assertEqual(entry.object_label, 'newcomer')
        self.assertEqual(entry.actor_label, 'newcomer')

    def test_read_only_tools_record_nothing(self):
        """Looking at data isn't a change. Find Duplicates must stay silent."""
        Recipe.objects.create(name='Jollof Rice')
        Recipe.objects.create(name='Jollof  Rice!')
        self.client.force_login(self.user)
        AuditLog.objects.all().delete()

        response = self.client.get(reverse('find-duplicates'))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AuditLog.objects.exists())

    def test_audit_log_itself_is_not_audited(self):
        Recipe.objects.create(name='Jollof Rice')
        before = AuditLog.objects.count()
        AuditLog.objects.filter(pk__lt=0).delete()
        self.assertEqual(AuditLog.objects.count(), before)

    def test_actor_is_the_logged_in_user(self):
        self.client.force_login(self.user)
        self.client.post(reverse('tag-add'), {'label': 'cuisine', 'value': 'thai'})

        entry = AuditLog.objects.get(object_type='Tag', action='created')
        self.assertEqual(entry.actor, self.user)
        self.assertEqual(entry.actor_label, 'cook')

    def test_actor_survives_the_user_being_deleted(self):
        with audit.actor_context(self.user):
            Recipe.objects.create(name='Jollof Rice')
        self.user.delete()

        entry = AuditLog.objects.get(object_type='Recipe', action='created')
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.actor_label, 'cook')


class AuditCoalescingTests(TestCase):
    """One edit, one entry -- however many saves the ORM does behind it."""

    def setUp(self):
        self.user = User.objects.create_user(username='cook', password='pw12345!')
        self.client.force_login(self.user)

    def _recipe(self, **overrides):
        """A recipe in the state the edit form round-trips to unchanged.

        The dietary flags are set to False rather than left null: an unticked
        box posts nothing and cleans to False, so a null one would show up as
        an edit on the first save whatever else changed.
        """
        fields = {
            'name': 'Jollof Rice', 'ingredients': 'rice', 'instructions': 'cook it',
            'description': '', 'notes': '', 'url': '',
            'gluten_free': False, 'dairy_free': False,
            'vegetarian': False, 'vegan': False,
        }
        fields.update(overrides)
        return Recipe.objects.create(**fields)

    def _post_edit(self, recipe, **overrides):
        """Submit the recipe edit form the way the browser does."""
        data = {
            'name': recipe.name,
            'description': recipe.description or '',
            'ingredients': recipe.ingredients,
            'instructions': recipe.instructions,
            'notes': recipe.notes or '',
            'url': recipe.url or '',
            'tags_input': ', '.join(str(tag) for tag in recipe.tags.all()),
        }
        # The browser always posts the three-state selects, so the tests must too.
        data.update(dietary_post_data(recipe))
        data.update(overrides)
        return self.client.post(reverse('recipe-update', args=[recipe.pk]), data)

    def test_editing_one_field_writes_one_entry(self):
        recipe = self._recipe()
        recipe.tags.add(Tag.objects.create(label='cuisine', value='west african'))
        AuditLog.objects.all().delete()

        self._post_edit(recipe, notes='Add more pepper')

        # RecipeForm.save() writes the row, clears every tag, then re-adds them
        # one at a time. That is one edit, not five.
        self.assertEqual(AuditLog.objects.count(), 1)

    def test_untouched_tags_are_not_reported(self):
        recipe = self._recipe()
        for value in ('west african', 'rice', 'party'):
            recipe.tags.add(Tag.objects.create(label='cuisine', value=value))
        AuditLog.objects.all().delete()

        self._post_edit(recipe, notes='Add more pepper')

        entry = AuditLog.objects.get(object_type='Recipe')
        self.assertIn('Notes', entry.changed_fields)
        self.assertNotIn('Tags', entry.changed_fields)

    def test_actually_changing_tags_is_reported(self):
        recipe = self._recipe()
        recipe.tags.add(Tag.objects.create(label='cuisine', value='west african'))
        AuditLog.objects.all().delete()

        self._post_edit(recipe, tags_input='cuisine:west african, meal:dinner')

        entry = AuditLog.objects.get(object_type='Recipe')
        self.assertEqual(entry.changed_fields, ['Tags'])
        # The new tag is its own creation, recorded separately.
        self.assertTrue(AuditLog.objects.filter(object_type='Tag', action='created').exists())

    def test_removing_every_tag_is_reported(self):
        recipe = self._recipe()
        recipe.tags.add(Tag.objects.create(label='cuisine', value='west african'))
        AuditLog.objects.all().delete()

        self._post_edit(recipe, tags_input='')

        self.assertEqual(AuditLog.objects.get(object_type='Recipe').changed_fields, ['Tags'])

    def test_a_save_that_changes_nothing_writes_nothing(self):
        recipe = self._recipe()
        recipe.tags.add(Tag.objects.create(label='cuisine', value='west african'))
        recipe = Recipe.objects.get(pk=recipe.pk)
        AuditLog.objects.all().delete()

        self._post_edit(recipe)

        self.assertFalse(AuditLog.objects.exists())

    def test_separate_objects_still_get_separate_entries(self):
        recipe = Recipe.objects.create(name='Jollof Rice')
        other = Recipe.objects.create(name='Pad Thai')
        AuditLog.objects.all().delete()

        with audit.actor_context(self.user):
            recipe.rating = 5
            recipe.save()
            other.rating = 4
            other.save()

        self.assertEqual(AuditLog.objects.count(), 2)

    def test_create_then_edit_in_one_request_reads_as_a_creation(self):
        AuditLog.objects.all().delete()

        with audit.actor_context(self.user):
            recipe = Recipe.objects.create(name='Jollof Rice')
            recipe.rating = 5
            recipe.save()

        entry = AuditLog.objects.get(object_type='Recipe')
        self.assertEqual(entry.action, 'created')

    def test_a_delete_supersedes_the_edits_before_it(self):
        recipe = Recipe.objects.create(name='Jollof Rice')
        AuditLog.objects.all().delete()

        with audit.actor_context(self.user):
            recipe.rating = 5
            recipe.save()
            recipe.delete()

        entry = AuditLog.objects.get(object_type='Recipe')
        self.assertEqual(entry.action, 'deleted')

    def test_entries_are_still_written_when_the_view_raises(self):
        recipe = Recipe.objects.create(name='Jollof Rice')
        AuditLog.objects.all().delete()

        with self.assertRaises(ValueError):
            with audit.actor_context(self.user):
                recipe.rating = 5
                recipe.save()
                raise ValueError('boom')

        self.assertEqual(AuditLog.objects.count(), 1)


class AuditRetentionTests(TestCase):
    """Retention is configured in the app, and actually deletes."""

    def setUp(self):
        self.user = User.objects.create_user(username='cook', password='pw12345!')
        self.client.force_login(self.user)
        AuditLog.objects.all().delete()

    def _entry_aged(self, days):
        entry = AuditLog.objects.create(action='created', object_type='Recipe', object_label='old')
        # timestamp is auto_now_add, so age it with an update rather than a save.
        AuditLog.objects.filter(pk=entry.pk).update(
            timestamp=timezone.now() - timedelta(days=days)
        )
        return entry

    def test_default_retention_is_one_year(self):
        self.assertEqual(AuditSettings.load().retention_days, 365)

    def test_prune_removes_only_entries_past_retention(self):
        old = self._entry_aged(400)
        recent = self._entry_aged(10)

        self.assertEqual(AuditLog.prune(), 1)
        self.assertFalse(AuditLog.objects.filter(pk=old.pk).exists())
        self.assertTrue(AuditLog.objects.filter(pk=recent.pk).exists())

    def test_zero_days_keeps_everything(self):
        self._entry_aged(5000)
        audit_settings = AuditSettings.load()
        audit_settings.retention_days = 0
        audit_settings.save()

        self.assertEqual(AuditLog.prune(), 0)
        self.assertEqual(AuditLog.objects.filter(object_label='old').count(), 1)

    def test_saving_retention_applies_it_immediately(self):
        self._entry_aged(100)

        response = self.client.post(reverse('audit-retention'), {'retention_days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(AuditSettings.load().retention_days, 30)
        self.assertFalse(AuditLog.objects.filter(object_label='old').exists())

    def test_retention_change_is_itself_recorded(self):
        self.client.post(reverse('audit-retention'), {'retention_days': 30})

        entry = AuditLog.objects.get(object_type='Audit settings')
        self.assertEqual(entry.changed_fields, ['Retention days'])
        self.assertEqual(entry.actor, self.user)

    def test_retention_is_capped(self):
        self.client.post(reverse('audit-retention'), {'retention_days': 99999})
        self.assertEqual(AuditSettings.load().retention_days, 365)


class AuditLogViewTests(TestCase):
    """The page: who can see it, and whether the filters filter."""

    def setUp(self):
        self.user = User.objects.create_user(username='cook', password='pw12345!')
        self.other = User.objects.create_user(username='baker', password='pw12345!')
        AuditLog.objects.all().delete()
        with audit.actor_context(self.user):
            self.recipe = Recipe.objects.create(name='Jollof Rice')
        with audit.actor_context(self.other):
            Tag.objects.create(label='cuisine', value='thai')

    def test_login_is_required(self):
        response = self.client.get(reverse('audit-log'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_any_logged_in_user_sees_everyone_s_changes(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse('audit-log'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Jollof Rice')
        self.assertContains(response, 'cuisine:thai')

    def test_filter_by_type(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('audit-log-list'), {'object_type': 'Tag'})

        self.assertNotContains(response, 'Jollof Rice')
        self.assertContains(response, 'cuisine:thai')

    def test_filter_by_actor(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('audit-log-list'), {'actor': str(self.user.pk)})

        self.assertContains(response, 'Jollof Rice')
        self.assertNotContains(response, 'cuisine:thai')

    def test_search_matches_the_object_name(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('audit-log-list'), {'audit_search': 'jollof'})

        self.assertContains(response, 'Jollof Rice')
        self.assertNotContains(response, 'cuisine:thai')


class UserTimezoneTests(TestCase):
    """Per-user timezone: choosing one, and it reaching every timestamp."""

    def setUp(self):
        self.user = User.objects.create_user(username='cook', password='pw12345!')
        self.client.force_login(self.user)

    def _profile(self, user=None):
        profile, _ = UserProfile.objects.get_or_create(user=user or self.user)
        return profile

    def _set_timezone(self, name, user=None):
        """Set the preference, creating the profile row if it isn't there yet.

        Profiles are made lazily on first render, so a bare queryset .update()
        here would quietly do nothing.
        """
        profile = self._profile(user)
        profile.timezone = name
        profile.save(update_fields=['timezone'])

    def _edit(self, **overrides):
        data = {'theme': 'light', 'timezone': ''}
        data.update(overrides)
        return self.client.post(reverse('preferences'), data)

    def test_the_list_is_the_iana_database(self):
        self.assertIn('America/New_York', timezones.NAMES)
        self.assertIn('Africa/Dakar', timezones.NAMES)
        self.assertIn('Australia/Sydney', timezones.NAMES)
        self.assertIn('UTC', timezones.NAMES)
        # The real database is in the hundreds; a stub would not be.
        self.assertGreater(len(timezones.NAMES), 400)

    def test_choices_are_grouped_by_region(self):
        choices = timezones.grouped_choices()
        self.assertEqual(choices[0][0], '')  # site default first
        groups = dict(choices[1:])
        self.assertIn('America', groups)
        self.assertIn(('America/New_York', 'New York'), groups['America'])
        # Bare names like UTC have no region of their own.
        self.assertIn(('UTC', 'UTC'), groups['Other'])

    def test_saving_a_timezone_from_the_preferences_page(self):
        response = self._edit(timezone='America/New_York')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._profile().timezone, 'America/New_York')

    def test_blank_means_follow_the_site_default(self):
        self._set_timezone('America/New_York')

        self._edit(timezone='')

        self.assertEqual(self._profile().timezone, '')

    def test_a_bogus_timezone_is_rejected(self):
        response = self._edit(timezone='Mars/Olympus_Mons')

        self.assertEqual(response.status_code, 200)  # redisplayed, not saved
        self.assertEqual(self._profile().timezone, '')

    def test_the_preferences_page_offers_the_full_list(self):
        response = self.client.get(reverse('preferences'))

        self.assertContains(response, 'America/New_York')
        self.assertContains(response, 'Australia/Sydney')
        self.assertContains(response, 'Site default (UTC)')

    def test_timestamps_render_in_the_chosen_timezone(self):
        """The audit log's timestamps must follow the preference."""
        # 2026-01-15 12:00 UTC is 07:00 in New York, still the 15th.
        moment = datetime.datetime(2026, 1, 15, 12, 0, tzinfo=datetime.timezone.utc)
        entry = AuditLog.objects.create(
            action='updated', object_type='Recipe', object_label='Jollof Rice',
        )
        AuditLog.objects.filter(pk=entry.pk).update(timestamp=moment)

        response = self.client.get(reverse('audit-log'))
        self.assertContains(response, 'Jan 15, 2026 12:00')  # UTC, no preference

        self._set_timezone('America/New_York')
        response = self.client.get(reverse('audit-log'))
        self.assertContains(response, 'Jan 15, 2026 07:00')

    def test_a_timezone_that_shifts_the_date_shifts_it(self):
        # 23:00 UTC is already the next morning in Sydney.
        moment = datetime.datetime(2026, 1, 15, 23, 0, tzinfo=datetime.timezone.utc)
        entry = AuditLog.objects.create(
            action='updated', object_type='Recipe', object_label='Jollof Rice',
        )
        AuditLog.objects.filter(pk=entry.pk).update(timestamp=moment)
        self._set_timezone('Australia/Sydney')

        response = self.client.get(reverse('audit-log'))
        self.assertContains(response, 'Jan 16, 2026 10:00')

    def test_a_stale_timezone_name_falls_back_instead_of_erroring(self):
        self._set_timezone('Mars/Olympus_Mons')

        response = self.client.get(reverse('audit-log'))

        self.assertEqual(response.status_code, 200)

    def test_the_preference_does_not_leak_between_users(self):
        """Threads are reused, so an activated timezone must not outlive its request."""
        self._set_timezone('Australia/Sydney')
        self.client.get(reverse('audit-log'))

        other = User.objects.create_user(username='baker', password='pw12345!')
        self.client.force_login(other)
        moment = datetime.datetime(2026, 1, 15, 23, 0, tzinfo=datetime.timezone.utc)
        entry = AuditLog.objects.create(
            action='updated', object_type='Recipe', object_label='Pad Thai',
        )
        AuditLog.objects.filter(pk=entry.pk).update(timestamp=moment)

        response = self.client.get(reverse('audit-log'))
        self.assertContains(response, 'Jan 15, 2026 23:00')

    def test_the_preferences_page_shows_the_chosen_timezone(self):
        self._set_timezone('America/New_York')
        response = self.client.get(reverse('preferences'))
        self.assertContains(response, '<option value="America/New_York" selected>', html=False)

    def test_editing_the_profile_leaves_the_timezone_alone(self):
        self._set_timezone('America/New_York')
        self.client.post(reverse('profile-edit'), {
            'username': 'cook', 'first_name': '', 'last_name': '', 'email': 'cook@example.com'})
        self.assertEqual(self._profile().timezone, 'America/New_York')

    def test_csv_export_follows_the_preference(self):
        """csv_fields uses timezone.localtime(), so it should need no changes."""
        recipe = Recipe.objects.create(name='Jollof Rice')
        moment = datetime.datetime(2026, 1, 15, 23, 0, tzinfo=datetime.timezone.utc)
        Recipe.objects.filter(pk=recipe.pk).update(created_at=moment)
        self._set_timezone('Australia/Sydney')

        response = self.client.post(reverse('recipe-export'), {'fields': ['name', 'created_at']})

        self.assertIn('2026-01-16 10:00:00', response.content.decode())


class DietaryFlagTests(TestCase):
    """Allergy info is answered by tick box, and an unanswered flag reads as "no"."""

    def setUp(self):
        self.user = User.objects.create_user(username='cook', password='pw12345!')
        self.client.force_login(self.user)

    def _recipe(self, **overrides):
        fields = {'name': 'Jollof Rice', 'ingredients': 'rice', 'instructions': 'cook it',
                  'description': '', 'notes': '', 'url': ''}
        fields.update(overrides)
        return Recipe.objects.create(**fields)

    def _post_edit(self, recipe, **overrides):
        data = {
            'name': recipe.name, 'description': recipe.description or '',
            'ingredients': recipe.ingredients, 'instructions': recipe.instructions,
            'notes': recipe.notes or '', 'url': recipe.url or '', 'tags_input': '',
        }
        data.update(dietary_post_data(recipe))
        data.update(overrides)
        return self.client.post(reverse('recipe-update', args=[recipe.pk]), data)

    def test_saving_settles_an_unanswered_flag_as_no(self):
        """Imported recipes arrive with these null; a save makes them a definite no."""
        recipe = self._recipe()
        self.assertIsNone(recipe.gluten_free)

        self._post_edit(recipe, notes='Add more pepper')

        recipe.refresh_from_db()
        for name in DIETARY_FIELDS:
            self.assertIs(getattr(recipe, name), False, name)

    def test_ticking_a_box_is_saved(self):
        recipe = self._recipe()

        self._post_edit(recipe, gluten_free='on')

        recipe.refresh_from_db()
        self.assertIs(recipe.gluten_free, True)
        self.assertIs(recipe.vegan, False)

    def test_unticking_a_box_is_saved(self):
        recipe = self._recipe(gluten_free=True)

        self._post_edit(recipe, gluten_free='')

        recipe.refresh_from_db()
        self.assertIs(recipe.gluten_free, False)

    def test_the_edit_form_uses_tick_boxes(self):
        recipe = self._recipe(gluten_free=True)

        html = self.client.get(reverse('recipe-update', args=[recipe.pk])).content.decode()

        self.assertIn('type="checkbox"', html)
        self.assertNotIn('<option value="unknown"', html)
        # Each label points at its own field, not at some other form's.
        self.assertIn('for="id_gluten_free"', html)

    def test_an_unanswered_flag_shows_as_no(self):
        recipe = self._recipe()

        html = self.client.get(recipe.get_absolute_url()).content.decode()

        self.assertIn('&#10060;', html)
        self.assertNotIn('&ndash;', html)

    def test_an_answered_flag_shows_as_yes(self):
        recipe = self._recipe(gluten_free=True)

        html = self.client.get(recipe.get_absolute_url()).content.decode()

        self.assertIn('&#x2705;', html)

    def test_csv_export_still_distinguishes_never_answered(self):
        """The column stays null-able, and CSV keeps saying so."""
        self._recipe()

        response = self.client.post(reverse('recipe-export'), {'fields': ['name', 'gluten_free']})

        self.assertIn('Jollof Rice,\r\n', response.content.decode())


class SharedRecipeReviewTests(TestCase):
    """Reviewing public share links: the filter, the column, and the tool page."""

    def setUp(self):
        self.user = User.objects.create_user(username='cook', password='pw12345!')
        self.client.force_login(self.user)

        self.shared = Recipe.objects.create(
            name='Jollof Rice', is_public=True, share_token='token-jollof')
        self.also_shared = Recipe.objects.create(
            name='Attieke', is_public=True, share_token='token-attieke', share_notes=True)
        # Was public once, isn't now -- keeps its token, must not be listed.
        self.unshared = Recipe.objects.create(
            name='Yassa', is_public=False, share_token='token-yassa')
        self.never_shared = Recipe.objects.create(name='Mafe')

    # -- the public_only filter on /recipes/ --------------------------------

    def test_filter_shows_only_recipes_with_a_live_link(self):
        response = self.client.get('/recipes/', {'public_only': '1'})

        listed = {r.name for r in response.context['recipe_list'].object_list}
        self.assertEqual(listed, {'Jollof Rice', 'Attieke'})

    def test_filter_sticks_in_a_cookie_like_the_other_filters(self):
        self.client.get('/recipes/', {'public_only': '1'})
        self.assertEqual(self.client.cookies['public_only'].value, '1')

        # No parameter at all -- the cookie should still be filtering.
        response = self.client.get('/recipes/')
        self.assertTrue(response.context['public_only'])

    def test_filter_can_be_turned_back_off(self):
        self.client.get('/recipes/', {'public_only': '1'})
        response = self.client.get('/recipes/', {'public_only': '0'})

        self.assertFalse(response.context['public_only'])
        self.assertEqual(response.context['recipe_list'].paginator.count, 4)

    def test_total_recipe_count_ignores_the_filter(self):
        """"Total Recipes" means the library, not the current filter."""
        response = self.client.get('/recipes/', {'public_only': '1'})
        self.assertEqual(response.context['recipe_count'], 4)

    def test_public_first_sort_puts_shared_recipes_on_top(self):
        response = self.client.get('/recipes/', {'sort_by': 'public-desc'})

        names = [r.name for r in response.context['recipe_list'].object_list]
        self.assertEqual(names[:2], ['Attieke', 'Jollof Rice'])

        response = self.client.get('/recipes/', {'sort_by': 'public-asc'})
        names = [r.name for r in response.context['recipe_list'].object_list]
        self.assertEqual(names[-2:], ['Attieke', 'Jollof Rice'])

    def test_list_view_links_the_globe_to_the_public_page(self):
        response = self.client.get('/recipes/', {'view': 'list', 'public_only': '1'})
        self.assertContains(response, self.shared.get_public_url())

    # -- the Shared Recipes tool page ---------------------------------------

    def test_page_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('shared-recipes'))

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_page_lists_every_shared_recipe_and_nothing_else(self):
        response = self.client.get(reverse('shared-recipes'))

        listed = [row['recipe'].name for row in response.context['shared_rows']]
        self.assertEqual(listed, ['Attieke', 'Jollof Rice'])
        self.assertEqual(response.context['shared_count'], 2)
        self.assertEqual(response.context['recipe_total'], 4)

    def test_a_recipe_that_kept_its_token_is_not_listed(self):
        """Unsharing keeps the token; that must not read as still shared."""
        response = self.client.get(reverse('shared-recipes'))
        self.assertNotContains(response, 'Yassa')

    def test_page_shows_the_full_shareable_url(self):
        response = self.client.get(reverse('shared-recipes'))
        self.assertContains(response, f'http://testserver{self.shared.get_public_url()}')

    def test_sorting_returns_the_table_only(self):
        """The sort headers swap into the table div, so they must not send a whole page."""
        response = self.client.get(reverse('shared-recipes-list'), {'shared_sort': 'name-desc'})

        templates = [t.name for t in response.templates if t.name]
        self.assertIn('partials/shared_recipes_body.html', templates)
        self.assertNotIn('main/shared_recipes.html', templates)
        self.assertEqual([row['recipe'].name for row in response.context['shared_rows']],
                         ['Jollof Rice', 'Attieke'])

    def test_an_unknown_sort_falls_back_to_the_default(self):
        response = self.client.get(reverse('shared-recipes-list'), {'shared_sort': 'nonsense'})
        self.assertEqual(response.context['shared_sort'], 'name-asc')

    def test_unshare_turns_the_link_off_but_keeps_the_token(self):
        response = self.client.post(
            reverse('shared-recipe-unshare', args=[self.shared.pk]))

        self.shared.refresh_from_db()
        self.assertFalse(self.shared.is_public)
        self.assertEqual(self.shared.share_token, 'token-jollof')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['recipe'].name for row in response.context['shared_rows']],
                         ['Attieke'])

    def test_unshare_kills_the_public_page(self):
        public_url = self.shared.get_public_url()
        self.assertEqual(self.client.get(public_url).status_code, 200)

        self.client.post(reverse('shared-recipe-unshare', args=[self.shared.pk]))

        self.assertEqual(self.client.get(public_url).status_code, 404)

    # -- the public page itself ----------------------------------------------

    def test_public_page_links_the_source_url(self):
        self.shared.url = 'https://example.com/jollof'
        self.shared.save()

        response = self.client.get(self.shared.get_public_url())

        self.assertContains(response, 'href="https://example.com/jollof"')

    def test_public_page_skips_non_http_source_urls(self):
        self.shared.url = 'javascript:alert(1)'
        self.shared.save()

        response = self.client.get(self.shared.get_public_url())

        self.assertNotContains(response, 'javascript:alert(1)')

    def test_unshare_keeps_the_current_sort(self):
        response = self.client.post(
            reverse('shared-recipe-unshare', args=[self.unshared.pk]),
            {'shared_sort': 'name-desc'})

        self.assertEqual(response.context['shared_sort'], 'name-desc')
        self.assertEqual([row['recipe'].name for row in response.context['shared_rows']],
                         ['Jollof Rice', 'Attieke'])

    def test_unshare_rejects_a_get(self):
        response = self.client.get(reverse('shared-recipe-unshare', args=[self.shared.pk]))

        self.assertEqual(response.status_code, 405)
        self.shared.refresh_from_db()
        self.assertTrue(self.shared.is_public)

    def test_unshare_requires_login(self):
        self.client.logout()
        response = self.client.post(
            reverse('shared-recipe-unshare', args=[self.shared.pk]))

        self.assertEqual(response.status_code, 302)
        self.shared.refresh_from_db()
        self.assertTrue(self.shared.is_public)

    def test_unshare_is_recorded_in_the_audit_log(self):
        AuditLog.objects.all().delete()
        with audit.actor_context(self.user):
            self.client.post(reverse('shared-recipe-unshare', args=[self.shared.pk]))

        entry = AuditLog.objects.latest('id')
        self.assertEqual(entry.action, 'updated')
        self.assertIn('Is public', entry.changed_fields)

    def test_empty_state_when_nothing_is_shared(self):
        Recipe.objects.update(is_public=False)
        response = self.client.get(reverse('shared-recipes'))

        self.assertEqual(response.context['shared_count'], 0)
        self.assertContains(response, 'No recipes are shared publicly')


class RecipeFilterBarTests(TestCase):
    """The consolidated filter bar: one copy, always reachable."""

    def setUp(self):
        self.user = User.objects.create_user(username='cook', password='pw12345!')
        self.client.force_login(self.user)
        Recipe.objects.create(name='Jollof Rice')
        Recipe.objects.create(name='Attieke', is_public=True, share_token='token-attieke')

    def test_every_view_gets_the_filter_bar(self):
        for view in ('grid', 'list', 'double_list'):
            with self.subTest(view=view):
                response = self.client.get('/recipes/', {'view': view})
                self.assertContains(response, 'id="filter-panel"')
                self.assertContains(response, 'id="public-only-toggle"')

    def test_each_control_appears_exactly_once(self):
        """Duplicate ids would break the getElementById the hx-vals rely on."""
        for view in ('grid', 'list', 'double_list'):
            with self.subTest(view=view):
                html = self.client.get('/recipes/', {'view': view}).content.decode()
                for element_id in ('filter-panel', 'cooked-only-toggle',
                                   'favorites-only-toggle', 'public-only-toggle'):
                    self.assertEqual(html.count(f'id="{element_id}"'), 1, element_id)

    def test_filters_survive_a_search_that_matches_nothing(self):
        """The list view used to hide its filters when nothing matched.

        That stranded the user: the filter was on, the checkbox to switch it
        back off had vanished with the results.
        """
        response = self.client.get('/recipes/', {'view': 'list', 'search_term': 'nothing-matches-this'})

        self.assertContains(response, 'No recipes found')
        self.assertContains(response, 'id="public-only-toggle"')

    def test_the_htmx_results_swap_no_longer_carries_filters(self):
        """They live outside #recipe-results now, so the swap must not resend them."""
        response = self.client.get('/recipes/', {'view': 'list'}, HTTP_HX_REQUEST='true')

        self.assertNotContains(response, 'id="public-only-toggle"')
        self.assertNotContains(response, 'id="filter-panel"')

    def test_sort_select_is_replaced_by_column_headers_in_list_view(self):
        self.assertContains(self.client.get('/recipes/', {'view': 'grid'}), 'id="sort_by"')
        self.assertNotContains(self.client.get('/recipes/', {'view': 'list'}), 'id="sort_by"')

    def test_checkbox_reflects_the_active_filter(self):
        response = self.client.get('/recipes/', {'public_only': '1'})
        html = response.content.decode()
        box = html[html.index('id="public-only-toggle"'):]
        self.assertIn('checked', box[:200])

    def test_labels_are_short_and_match_the_chips(self):
        """Collapsed chips and expanded labels must read as the same thing."""
        html = self.client.get('/recipes/', {'view': 'grid'}).content.decode()

        for label in ('>Cooked<', '>Favorites<', '>Public<'):
            self.assertIn(label.replace('>', '').replace('<', ''), html)
        self.assertNotIn('Only show', html)

    def test_the_toggle_carries_a_chevron_cue(self):
        html = self.client.get('/recipes/', {'view': 'grid'}).content.decode()
        self.assertIn('filter-chevron', html)
        self.assertIn('aria-controls="filter-panel"', html)

    def test_list_view_sort_shortcut_is_labelled_as_a_sort(self):
        html = self.client.get('/recipes/', {'view': 'list'}).content.decode()
        self.assertIn('Newest first', html)
        self.assertIn('id="sort-recently-added"', html)

    def test_all_three_filters_can_be_set_at_once(self):
        """The bar posts every filter on each change, not just the clicked one."""
        response = self.client.get('/recipes/', {
            'cooked_only': '0', 'favorites_only': '0', 'public_only': '1'})

        self.assertFalse(response.context['cooked_only'])
        self.assertFalse(response.context['favorites_only'])
        self.assertTrue(response.context['public_only'])
        self.assertEqual([r.name for r in response.context['recipe_list'].object_list],
                         ['Attieke'])


class LoggedOutPageChromeTests(TestCase):
    """The login page: version footer present, navbar absent."""

    def test_version_footer_shows_the_version(self):
        self.assertContains(self.client.get(reverse('login')), f'Jolof Version {settings.VERSION}')

    def test_navbar_is_hidden(self):
        self.assertNotContains(self.client.get(reverse('login')), '<nav')

    def test_public_sign_up_is_gone(self):
        """Accounts are added under user management now."""
        self.assertEqual(self.client.get('/sign-up/').status_code, 404)


class AddFromUrlTests(TestCase):
    """One URL box: a single URL opens the pre-filled form to check before
    saving, several go through the background batch import."""

    SCRAPED = {
        'name': 'Poutine',
        'description': 'Fries, curds, gravy.',
        'ingredients': 'fries\ncurds\ngravy',
        'instructions': 'Assemble.',
    }

    def setUp(self):
        User.objects.create_user(username='cook', password='secret-for-tests')
        self.client.login(username='cook', password='secret-for-tests')

    def scraped(self, url):
        return dict(self.SCRAPED, url=url), None

    def test_form_skips_blank_lines_and_repeats(self):
        form = URLScrapeFormBatch({'urls': (
            '  https://a.example/one  \n\n'
            'https://b.example/two\n'
            'https://a.example/one\n')})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['urls'],
                         ['https://a.example/one', 'https://b.example/two'])

    def test_form_lists_every_bad_line(self):
        form = URLScrapeFormBatch({'urls': 'https://a.example/one\nnot a url\nalso bad'})
        self.assertFalse(form.is_valid())
        self.assertIn('not a url', form.errors['urls'])
        self.assertIn('also bad', form.errors['urls'])

    def test_form_rejects_only_whitespace(self):
        self.assertFalse(URLScrapeFormBatch({'urls': '  \n \n'}).is_valid())

    def test_page_has_one_url_box(self):
        html = self.client.get(reverse('recipe-add-url')).content.decode()
        self.assertEqual(html.count('<textarea'), 1)
        self.assertNotIn('type="url"', html)

    def test_single_url_redirects_to_the_edit_page(self):
        url = 'https://a.example/poutine?x=1&y=2'
        response = self.client.post(reverse('process-url-batch'), {'urls': url})
        self.assertEqual(response['HX-Redirect'],
                         reverse('recipe-add-url') + '?url=' + quote(url, safe=''))

    def test_same_url_twice_counts_as_one(self):
        url = 'https://a.example/poutine'
        response = self.client.post(reverse('process-url-batch'), {'urls': f'{url}\n{url}'})
        self.assertIn('HX-Redirect', response)

    def test_several_urls_start_the_batch_import(self):
        urls = ['https://a.example/one', 'https://b.example/two']
        with mock.patch.object(views.threading, 'Thread') as thread:
            response = self.client.post(reverse('process-url-batch'), {'urls': '\n'.join(urls)})
        self.assertNotIn('HX-Redirect', response)
        self.assertContains(response, 'Processing...')
        self.assertEqual(thread.call_args.kwargs['args'][2], urls)
        thread.return_value.start.assert_called_once()

    def test_bad_lines_are_reported_and_nothing_is_scraped(self):
        with mock.patch.object(views, 'scrape_recipe_url') as scrape, \
                mock.patch.object(views.threading, 'Thread') as thread:
            response = self.client.post(reverse('process-url-batch'),
                                        {'urls': 'https://a.example/one\nnope'})
        self.assertContains(response, 'alert-danger')
        self.assertContains(response, 'nope')
        scrape.assert_not_called()
        thread.assert_not_called()

    def test_edit_page_is_prefilled_from_the_scrape(self):
        url = 'https://a.example/poutine'
        with mock.patch.object(views, 'scrape_recipe_url', side_effect=self.scraped):
            response = self.client.get(reverse('recipe-add-url'), {'url': url})
        self.assertTemplateUsed(response, 'main/recipe_add.html')
        self.assertTrue(response.context['scraped'])
        self.assertEqual(response.context['form'].initial['name'], 'Poutine')
        self.assertEqual(response.context['form'].initial['url'], url)
        self.assertFalse(Recipe.objects.exists())

    def test_failed_scrape_shows_the_error_and_keeps_the_url(self):
        url = 'https://a.example/gone'
        with mock.patch.object(views, 'scrape_recipe_url',
                               side_effect=views.requests.RequestException('404 Not Found')):
            response = self.client.get(reverse('recipe-add-url'), {'url': url})
        self.assertTemplateUsed(response, 'main/recipe_add_url.html')
        self.assertContains(response, 'Error fetching URL: 404 Not Found')
        self.assertContains(response, url)

    def test_batch_saves_each_recipe_from_its_own_url(self):
        """Each URL is scraped (and its images resolved) against itself; the
        old code resolved every image against the last URL in the list."""
        urls = ['https://a.example/one', 'https://b.example/two', 'https://c.example/bad']

        def scrape(url):
            if 'bad' in url:
                raise views.requests.RequestException('boom')
            return self.scraped(url)

        with mock.patch.object(views, 'scrape_recipe_url', side_effect=scrape) as mocked:
            views._recipe_add_url_batch('task_test', urls)

        self.assertEqual([c.args[0] for c in mocked.call_args_list], urls)
        self.assertEqual(sorted(Recipe.objects.values_list('url', flat=True)), urls[:2])
        result = views.cache.get('result_task_test')
        self.assertEqual(result['failure'], ['https://c.example/bad'])
        self.assertEqual(len(result['success']), 2)


class PermissionTests(TestCase):
    """Roles and permissions: what each role grants, and that every view asks."""

    # Views that deliberately don't check a permission, and why.
    UNGUARDED = {
        'home', 'login',                 # reachable logged out by design
        'recipe-public',                 # anonymous public share links
        'profile', 'profile-edit',       # your own account, whatever your role
        'preferences', 'theme-adopt',
        'password-change', 'password-change-done',
        'whats-new-dismiss',             # your own popup state
        'profile-api-session-revoke',    # your own API sessions
        'get-progress',                  # polls a task the user already started
    }

    def setUp(self):
        # The first user on an install becomes an Administrator; made here so
        # the users the tests care about get the role they're given.
        User.objects.create_user(username='founder', password='pw12345!')

    def make_user(self, username, role):
        user = User.objects.create_user(username=username, password='pw12345!')
        user.profile.role = role
        user.profile.save()
        return user

    def custom_role(self, *codes, name='Custom'):
        return Role.objects.create(name=name, permissions=list(codes))

    @staticmethod
    def main_url_patterns():
        from main import urls as main_urls
        return [p for p in main_urls.urlpatterns if p.name]

    @staticmethod
    def sample_url(pattern):
        """A URL for the pattern, with placeholder arguments filled in."""
        kwargs = {
            name: 1 if converter.__class__.__name__ == 'IntConverter' else 'x'
            for name, converter in pattern.pattern.converters.items()
        }
        return reverse(pattern.name, kwargs=kwargs)

    # --- Coverage ---

    def test_every_view_checks_a_permission_unless_listed(self):
        unguarded = {
            p.name for p in self.main_url_patterns()
            if not hasattr(p.callback, 'required_permission')
        }
        self.assertEqual(unguarded - self.UNGUARDED, set(),
                         'New view without @require(...): add one or list it in UNGUARDED.')

    def test_every_guarded_view_refuses_a_user_without_the_permission(self):
        nobody = self.make_user('nobody', self.custom_role(name='Nothing'))
        self.client.force_login(nobody)
        for pattern in self.main_url_patterns():
            if not hasattr(pattern.callback, 'required_permission'):
                continue
            url = self.sample_url(pattern)
            for method in (self.client.get, self.client.post):
                with self.subTest(url=url, method=method.__name__):
                    self.assertEqual(method(url).status_code, 403)

    def test_every_guarded_view_redirects_anonymous_users_to_login(self):
        for pattern in self.main_url_patterns():
            if not hasattr(pattern.callback, 'required_permission'):
                continue
            url = self.sample_url(pattern)
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response['Location'].startswith(settings.LOGIN_URL))

    def test_image_scrape_start_needs_login(self):
        """It used to run for anyone who POSTed to it, logged in or not."""
        response = self.client.post(reverse('process-image-scrape'))
        self.assertEqual(response.status_code, 302)

    # --- What the roles grant ---

    def test_system_roles_grant_what_was_agreed(self):
        expected = {
            permissions.ADMINISTRATOR: set(permissions.ALL_CODES),
            permissions.READ_WRITE: {
                'recipe-read', 'recipe-read-write', 'mealplan-read', 'mealplan-read-write',
                'audit-log', 'api-recipe-read', 'api-recipe-read-write',
                'api-mealplan-read', 'api-mealplan-read-write',
            },
            permissions.READ_ONLY: {
                'recipe-read', 'mealplan-read', 'audit-log',
                'api-recipe-read', 'api-mealplan-read',
            },
        }
        for key, codes in expected.items():
            with self.subTest(role=key):
                self.assertEqual(set(permissions.system_role(key).permission_codes()), codes)

    def test_only_administrator_has_user_mgmt_and_platform_admin(self):
        for key in (permissions.READ_WRITE, permissions.READ_ONLY):
            codes = permissions.system_role(key).permission_codes()
            self.assertNotIn(permissions.USER_MGMT, codes)
            self.assertNotIn(permissions.PLATFORM_ADMIN, codes)

    def test_read_write_includes_read(self):
        role = self.custom_role(permissions.RECIPE_READ_WRITE)
        self.assertIn(permissions.RECIPE_READ, role.permission_codes())

    def test_unknown_codes_are_ignored(self):
        role = self.custom_role('recipe-read', 'no-such-thing')
        self.assertEqual(role.permission_codes(), {'recipe-read'})

    def test_system_role_ignores_its_stored_list(self):
        role = permissions.system_role(permissions.READ_ONLY)
        role.permissions = [permissions.USER_MGMT]
        role.save()
        self.assertNotIn(permissions.USER_MGMT, role.permission_codes())

    def test_superuser_flag_grants_nothing_in_the_app(self):
        user = self.make_user('root', permissions.system_role(permissions.READ_ONLY))
        user.is_superuser = True
        user.is_staff = True
        user.save()
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse('recipe-add')).status_code, 403)

    def test_inactive_user_has_no_permissions(self):
        user = self.make_user('gone', permissions.system_role(permissions.ADMINISTRATOR))
        user.is_active = False
        self.assertEqual(permissions.user_permissions(user), frozenset())

    def test_role_change_applies_on_the_next_request(self):
        user = self.make_user('cook', permissions.system_role(permissions.READ_WRITE))
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse('recipe-add')).status_code, 200)
        user.profile.role = permissions.system_role(permissions.READ_ONLY)
        user.profile.save()
        self.assertEqual(self.client.get(reverse('recipe-add')).status_code, 403)

    # --- Default roles for new users ---

    def test_first_user_becomes_administrator(self):
        founder = User.objects.get(username='founder')
        self.assertEqual(founder.profile.role.system_key, permissions.ADMINISTRATOR)

    def test_later_users_start_read_only(self):
        user = User.objects.create_user(username='newcomer', password='pw12345!')
        self.assertEqual(user.profile.role.system_key, permissions.READ_ONLY)

    def test_createsuperuser_gets_administrator(self):
        user = User.objects.create_superuser(username='boss', password='pw12345!')
        self.assertEqual(user.profile.role.system_key, permissions.ADMINISTRATOR)

    def test_new_user_gets_administrator_when_no_one_can_manage_users(self):
        founder = User.objects.get(username='founder')
        founder.is_active = False
        founder.save()
        user = User.objects.create_user(username='rescuer', password='pw12345!')
        self.assertEqual(user.profile.role.system_key, permissions.ADMINISTRATOR)

    # --- What each role sees ---

    def test_read_only_user_sees_no_edit_controls(self):
        recipe = Recipe.objects.create(name='Jollof', ingredients='rice', instructions='cook')
        user = self.make_user('reader', permissions.system_role(permissions.READ_ONLY))
        self.client.force_login(user)

        detail = self.client.get(recipe.get_absolute_url())
        self.assertEqual(detail.status_code, 200)
        for marker in (f'/recipe-update/{recipe.id}/', f'/recipe-delete/{recipe.id}/',
                       reverse('update-tags', args=[recipe.id]),
                       reverse('recipe-make-public', args=[recipe.id]),
                       reverse('workspace-add', args=[recipe.id])):
            self.assertNotContains(detail, marker)

        listing = self.client.get(reverse('recipes'))
        self.assertNotContains(listing, reverse('recipe-add'))

    def test_read_write_user_sees_edit_controls(self):
        recipe = Recipe.objects.create(name='Jollof', ingredients='rice', instructions='cook')
        user = self.make_user('writer', permissions.system_role(permissions.READ_WRITE))
        self.client.force_login(user)
        detail = self.client.get(recipe.get_absolute_url())
        self.assertContains(detail, f'/recipe-update/{recipe.id}/')
        self.assertContains(detail, reverse('workspace-add', args=[recipe.id]))

    def test_read_only_planner_has_no_write_controls(self):
        recipe = Recipe.objects.create(name='Jollof', ingredients='rice', instructions='cook')
        user = self.make_user('reader', permissions.system_role(permissions.READ_ONLY))
        MealPlan.objects.create(user=user, date=date_class.today(), slot=1, recipe=recipe)
        WorkspaceItem.objects.create(user=user, recipe=recipe)
        self.client.force_login(user)

        response = self.client.get(reverse('meal-planner'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Jollof')
        self.assertNotContains(response, 'class="planner-slot-filled" draggable="true"')
        self.assertNotContains(response, 'class="workspace-item" draggable="true"')
        self.assertContains(response, 'class="planner-slot-filled" draggable="false"')
        self.assertNotContains(response, '/meal-planner/remove/')
        self.assertNotContains(response, 'workspace-add-tile"')
        self.assertNotContains(response, '<button class="planner-slot-empty"')

    def test_admin_menu_follows_audit_log_permission(self):
        reader = self.make_user('reader', permissions.system_role(permissions.READ_ONLY))
        self.client.force_login(reader)
        self.assertContains(self.client.get(reverse('profile')), 'navbarAdminDropdownMenuLink')

        blind = self.make_user('blind', self.custom_role(permissions.RECIPE_READ))
        self.client.force_login(blind)
        response = self.client.get(reverse('profile'))
        self.assertNotContains(response, 'navbarAdminDropdownMenuLink')
        self.assertNotContains(response, reverse('audit-log'))

    def test_retention_controls_need_platform_admin(self):
        reader = self.make_user('reader', permissions.system_role(permissions.READ_WRITE))
        self.client.force_login(reader)
        response = self.client.get(reverse('audit-log'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'audit-retention-panel')
        self.assertEqual(
            self.client.post(reverse('audit-retention'), {'retention_days': 1}).status_code, 403)
        self.assertEqual(AuditSettings.load().retention_days, 365)

    def test_profile_shows_role(self):
        user = self.make_user('reader', permissions.system_role(permissions.READ_ONLY))
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse('profile')), 'Read-only')

    def test_role_change_is_audited(self):
        user = self.make_user('cook', permissions.system_role(permissions.READ_ONLY))
        user.profile.role = permissions.system_role(permissions.READ_WRITE)
        user.profile.save()
        entry = AuditLog.objects.filter(object_type='User profile').latest('id')
        self.assertEqual(entry.action, AuditLog.ACTION_UPDATED)
        self.assertEqual(entry.changes, {'fields': ['Role']})


class UserManagementTests(TestCase):
    """Users, roles and platform settings pages, and lockout protection."""

    def setUp(self):
        # First user on the install, so an Administrator.
        self.admin = User.objects.create_user(username='admin', password='pw12345!', email='a@example.com')
        self.client.force_login(self.admin)

    def role(self, key):
        return permissions.system_role(key)

    def make_user(self, username, key=permissions.READ_WRITE, **extra):
        user = User.objects.create_user(username=username, password='pw12345!',
                                        email=f'{username}@example.com', **extra)
        user.profile.role = self.role(key)
        user.profile.save()
        return user

    def edit_data(self, user, **overrides):
        data = {
            'username': user.username, 'first_name': user.first_name, 'last_name': user.last_name,
            'email': user.email, 'role': user.profile.role.pk, 'is_active': 'on',
        }
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    # --- Users page and Add User ---

    def test_users_page_lists_everyone_with_their_role(self):
        self.make_user('cook')
        response = self.client.get(reverse('users'))
        self.assertContains(response, 'cook')
        self.assertContains(response, 'Read-write')
        self.assertContains(response, 'Administrator')

    def test_add_user_sets_role_and_forced_password_change(self):
        response = self.client.post(reverse('user-add'), {
            'username': 'newbie', 'first_name': 'New', 'last_name': 'Bie',
            'email': 'newbie@example.com', 'role': self.role(permissions.READ_WRITE).pk,
            'password1': 'Tr1cky-passw0rd', 'password2': 'Tr1cky-passw0rd',
            'must_change_password': 'on',
        })
        self.assertRedirects(response, reverse('users'))
        newbie = User.objects.get(username='newbie')
        self.assertEqual(newbie.profile.role.system_key, permissions.READ_WRITE)
        self.assertTrue(newbie.profile.must_change_password)
        self.assertTrue(newbie.check_password('Tr1cky-passw0rd'))

    def test_add_user_without_forced_change(self):
        self.client.post(reverse('user-add'), {
            'username': 'newbie', 'first_name': 'New', 'email': 'newbie@example.com',
            'role': self.role(permissions.READ_ONLY).pk,
            'password1': 'Tr1cky-passw0rd', 'password2': 'Tr1cky-passw0rd',
        })
        self.assertFalse(User.objects.get(username='newbie').profile.must_change_password)

    # --- Forced password change ---

    def test_forced_user_is_held_on_the_change_password_page(self):
        cook = self.make_user('cook')
        cook.profile.must_change_password = True
        cook.profile.save()
        self.client.force_login(cook)

        self.assertRedirects(self.client.get(reverse('recipes')), reverse('password-change'))
        htmx = self.client.get(reverse('recipes'), HTTP_HX_REQUEST='true')
        self.assertEqual(htmx['HX-Redirect'], reverse('password-change'))
        page = self.client.get(reverse('password-change'))
        self.assertContains(page, 'Choose your own')

        response = self.client.post(reverse('password-change'), {
            'old_password': 'pw12345!',
            'new_password1': 'Brand-new-pw-9', 'new_password2': 'Brand-new-pw-9',
        })
        self.assertRedirects(response, reverse('password-change-done'))
        cook.profile.refresh_from_db()
        self.assertFalse(cook.profile.must_change_password)
        self.assertEqual(self.client.get(reverse('recipes')).status_code, 200)

    # --- Edit user ---

    def test_edit_user_changes_details_and_role(self):
        cook = self.make_user('cook')
        response = self.client.post(reverse('user-edit', args=[cook.pk]), self.edit_data(
            cook, first_name='Chef', role=self.role(permissions.READ_ONLY).pk))
        self.assertRedirects(response, reverse('users'))
        cook.refresh_from_db()
        self.assertEqual(cook.first_name, 'Chef')
        self.assertEqual(cook.profile.role.system_key, permissions.READ_ONLY)

    def test_deactivating_a_user_logs_them_out(self):
        cook = self.make_user('cook')
        other = self.client_class()
        other.force_login(cook)
        self.client.post(reverse('user-edit', args=[cook.pk]), self.edit_data(cook, is_active=None))
        cook.refresh_from_db()
        self.assertFalse(cook.is_active)
        self.assertEqual(other.get(reverse('recipes')).status_code, 302)

    def test_cannot_deactivate_yourself(self):
        self.make_user('second', permissions.ADMINISTRATOR)
        response = self.client.post(reverse('user-edit', args=[self.admin.pk]),
                                    self.edit_data(self.admin, is_active=None))
        self.assertContains(response, 'deactivate your own account.')
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_last_user_manager_cannot_demote_themselves(self):
        response = self.client.post(reverse('user-edit', args=[self.admin.pk]), self.edit_data(
            self.admin, role=self.role(permissions.READ_WRITE).pk))
        self.assertContains(response, 'nobody able to manage users')
        self.assertEqual(self.admin.profile.role.system_key, permissions.ADMINISTRATOR)

    def test_can_demote_yourself_when_someone_else_manages_users(self):
        self.make_user('second', permissions.ADMINISTRATOR)
        response = self.client.post(reverse('user-edit', args=[self.admin.pk]), self.edit_data(
            self.admin, role=self.role(permissions.READ_WRITE).pk))
        self.assertRedirects(response, reverse('profile'))
        self.admin.profile.refresh_from_db()
        self.assertEqual(self.admin.profile.role.system_key, permissions.READ_WRITE)

    def test_inactive_admins_do_not_count(self):
        self.make_user('dormant', permissions.ADMINISTRATOR, is_active=False)
        response = self.client.post(reverse('user-edit', args=[self.admin.pk]), self.edit_data(
            self.admin, role=self.role(permissions.READ_WRITE).pk))
        self.assertContains(response, 'nobody able to manage users')

    # --- Set password ---

    def test_set_password_for_another_user(self):
        cook = self.make_user('cook')
        response = self.client.post(reverse('user-set-password', args=[cook.pk]), {
            'new_password1': 'Temp-pass-123', 'new_password2': 'Temp-pass-123',
            'must_change_password': 'on',
        })
        self.assertRedirects(response, reverse('user-edit', args=[cook.pk]))
        cook.refresh_from_db()
        self.assertTrue(cook.check_password('Temp-pass-123'))
        self.assertTrue(cook.profile.must_change_password)

    def test_set_password_mismatch_shows_error(self):
        cook = self.make_user('cook')
        response = self.client.post(reverse('user-set-password', args=[cook.pk]), {
            'new_password1': 'Temp-pass-123', 'new_password2': 'Other-pass-123',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'didn')
        cook.refresh_from_db()
        self.assertTrue(cook.check_password('pw12345!'))

    def test_setting_your_own_password_keeps_you_logged_in(self):
        self.client.post(reverse('user-set-password', args=[self.admin.pk]), {
            'new_password1': 'Temp-pass-123', 'new_password2': 'Temp-pass-123',
        })
        self.assertEqual(self.client.get(reverse('users')).status_code, 200)

    # --- Delete user ---

    def test_cannot_delete_yourself(self):
        response = self.client.post(reverse('user-delete', args=[self.admin.pk]))
        self.assertContains(response, 'delete your own account.')
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_delete_keeps_shared_planner_entries_and_drops_personal_ones(self):
        cook = self.make_user('cook')
        recipe = Recipe.objects.create(name='Jollof', ingredients='rice', instructions='cook')
        household = MealPlanGroup.objects.create(name='Home')
        UserProfile.objects.filter(user__in=[cook, self.admin]).update(household=household)
        shared = MealPlan.objects.create(user=cook, group=household, date=date_class.today(), slot=1, recipe=recipe)
        MealPlan.objects.create(user=cook, date=date_class.today(), slot=2, recipe=recipe)
        Favorite.objects.create(user=cook, recipe=recipe)

        response = self.client.post(reverse('user-delete', args=[cook.pk]))
        self.assertRedirects(response, reverse('users'))
        self.assertFalse(User.objects.filter(username='cook').exists())
        shared.refresh_from_db()
        self.assertIsNone(shared.user)
        self.assertEqual(MealPlan.objects.count(), 1)
        self.assertFalse(Favorite.objects.exists())

    def test_delete_is_audited_under_the_actor(self):
        cook = self.make_user('cook')
        self.client.post(reverse('user-delete', args=[cook.pk]))
        entry = AuditLog.objects.filter(object_type='User', action=AuditLog.ACTION_DELETED).latest('id')
        self.assertEqual(entry.actor, self.admin)

    # --- Roles ---

    def test_roles_page_lists_system_roles_with_permission_tooltips(self):
        response = self.client.get(reverse('roles'))
        for name in ('Administrator', 'Read-write', 'Read-only'):
            self.assertContains(response, name)
        self.assertContains(response, permissions.PERMISSIONS_BY_CODE['recipe-read'].description)

    def test_add_custom_role_fills_in_implied_permissions(self):
        response = self.client.post(reverse('role-add'), {
            'name': 'Planner', 'description': 'Plans meals',
            'permissions': ['mealplan-read-write', 'recipe-read'],
        })
        self.assertRedirects(response, reverse('roles'))
        role = Role.objects.get(name='Planner')
        self.assertEqual(role.permissions, ['mealplan-read', 'mealplan-read-write', 'recipe-read'])

    def test_role_names_are_unique_ignoring_case(self):
        response = self.client.post(reverse('role-add'), {'name': 'administrator'})
        self.assertContains(response, 'There is already a role called')

    def test_system_roles_cannot_be_edited_or_deleted(self):
        system = self.role(permissions.READ_ONLY)
        self.assertEqual(self.client.get(reverse('role-edit', args=[system.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('role-delete', args=[system.pk])).status_code, 404)

    def test_role_in_use_cannot_be_deleted(self):
        role = Role.objects.create(name='Helper', permissions=['recipe-read'])
        cook = self.make_user('cook')
        cook.profile.role = role
        cook.profile.save()
        self.client.post(reverse('role-delete', args=[role.pk]))
        self.assertTrue(Role.objects.filter(pk=role.pk).exists())
        self.client.post(reverse('role-delete', args=[Role.objects.create(name='Spare').pk]))
        self.assertFalse(Role.objects.filter(name='Spare').exists())

    def test_removing_user_mgmt_from_the_only_managers_role_is_blocked(self):
        boss = Role.objects.create(name='Boss', permissions=['user-mgmt'])
        self.admin.profile.role = boss
        self.admin.profile.save()
        response = self.client.post(reverse('role-edit', args=[boss.pk]),
                                    {'name': 'Boss', 'permissions': ['recipe-read']})
        self.assertContains(response, 'nobody able to manage users')
        boss.refresh_from_db()
        self.assertEqual(boss.permissions, ['user-mgmt'])

    # --- Platform settings and navigation ---

    def test_platform_settings_has_retention_and_delete_all(self):
        response = self.client.get(reverse('platform-settings'))
        self.assertContains(response, reverse('audit-retention'))
        self.assertContains(response, reverse('delete-all-recipes'))

    def test_admin_menu_links_follow_permissions(self):
        response = self.client.get(reverse('profile'))
        for name in ('users', 'roles', 'platform-settings', 'audit-log'):
            self.assertContains(response, reverse(name))

        self.client.force_login(self.make_user('cook'))
        response = self.client.get(reverse('profile'))
        self.assertContains(response, reverse('audit-log'))
        for name in ('users', 'roles', 'platform-settings'):
            self.assertNotContains(response, f'href="{reverse(name)}"')

    def test_grant_admin_command(self):
        from django.core.management import call_command
        cook = self.make_user('cook', permissions.READ_ONLY, is_active=False)
        call_command('grant_admin', 'cook', stdout=open(os.devnull, 'w'))
        cook.refresh_from_db()
        self.assertTrue(cook.is_active)
        self.assertEqual(cook.profile.role.system_key, permissions.ADMINISTRATOR)


class HouseholdTests(TestCase):
    """Households: who shares a meal planner, managed under user management."""

    def setUp(self):
        self.admin = User.objects.create_user(username='admin', password='pw12345!', email='a@example.com')
        self.cook = User.objects.create_user(username='cook', password='pw12345!', email='c@example.com')
        self.client.force_login(self.admin)

    def household_of(self, user):
        return UserProfile.objects.get(user=user).household

    def test_pages_need_user_mgmt(self):
        self.client.force_login(self.cook)  # Read-only
        home = MealPlanGroup.objects.create(name='Home')
        for url in (reverse('households'), reverse('household-add'), reverse('household-edit', args=[home.pk])):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(reverse('household-delete', args=[home.pk])).status_code, 403)

    def test_add_household_with_members(self):
        response = self.client.post(reverse('household-add'), {
            'name': 'Home', 'members': [self.admin.pk, self.cook.pk],
        })
        self.assertRedirects(response, reverse('households'))
        home = MealPlanGroup.objects.get(name='Home')
        self.assertEqual(self.household_of(self.admin), home)
        self.assertEqual(self.household_of(self.cook), home)

    def test_names_are_unique_ignoring_case(self):
        MealPlanGroup.objects.create(name='Home')
        response = self.client.post(reverse('household-add'), {'name': 'home'})
        self.assertContains(response, 'There is already a household called')
        self.assertEqual(MealPlanGroup.objects.count(), 1)

    def test_ticking_a_member_moves_them_and_unticking_releases_them(self):
        home = MealPlanGroup.objects.create(name='Home')
        cabin = MealPlanGroup.objects.create(name='Cabin')
        UserProfile.objects.filter(user=self.cook).update(household=home)
        UserProfile.objects.filter(user=self.admin).update(household=cabin)

        response = self.client.get(reverse('household-edit', args=[cabin.pk]))
        self.assertContains(response, 'cook (in Home)')

        self.client.post(reverse('household-edit', args=[cabin.pk]), {'name': 'Cabin', 'members': [self.cook.pk]})
        self.assertEqual(self.household_of(self.cook), cabin)
        self.assertIsNone(self.household_of(self.admin))

    def test_user_edit_sets_household(self):
        home = MealPlanGroup.objects.create(name='Home')
        self.client.post(reverse('user-edit', args=[self.cook.pk]), {
            'username': 'cook', 'email': 'c@example.com',
            'role': self.cook.profile.role.pk, 'household': home.pk, 'is_active': 'on',
        })
        self.assertEqual(self.household_of(self.cook), home)
        self.assertContains(self.client.get(reverse('users')), 'Home')

    def test_add_user_with_household(self):
        home = MealPlanGroup.objects.create(name='Home')
        self.client.post(reverse('user-add'), {
            'username': 'newbie', 'first_name': 'New', 'email': 'n@example.com',
            'role': permissions.system_role(permissions.READ_WRITE).pk, 'household': home.pk,
            'password1': 'Tr1cky-passw0rd', 'password2': 'Tr1cky-passw0rd',
        })
        self.assertEqual(self.household_of(User.objects.get(username='newbie')), home)

    def test_cannot_delete_a_household_with_members(self):
        home = MealPlanGroup.objects.create(name='Home')
        UserProfile.objects.filter(user=self.cook).update(household=home)
        response = self.client.post(reverse('household-delete', args=[home.pk]), follow=True)
        self.assertContains(response, 'Move them out before deleting it.')
        self.assertTrue(MealPlanGroup.objects.filter(pk=home.pk).exists())

    def test_deleting_an_empty_household_takes_its_planner(self):
        home = MealPlanGroup.objects.create(name='Home')
        MealPlan.objects.create(group=home, date=date_class.today(), slot=1, custom_text='Soup')
        response = self.client.post(reverse('household-delete', args=[home.pk]))
        self.assertRedirects(response, reverse('households'))
        self.assertFalse(MealPlanGroup.objects.exists())
        self.assertFalse(MealPlan.objects.exists())

    def test_members_share_a_planner_and_leaving_restores_your_own(self):
        home = MealPlanGroup.objects.create(name='Home')
        MealPlan.objects.create(user=self.admin, date=date_class.today(), slot=1, custom_text='Mine')
        MealPlan.objects.create(group=home, date=date_class.today(), slot=1, custom_text='Ours')

        UserProfile.objects.filter(user=self.admin).update(household=home)
        response = self.client.get(reverse('meal-planner'))
        self.assertContains(response, 'Ours')
        self.assertNotContains(response, 'Mine')
        self.assertContains(response, 'bi-house-door')

        UserProfile.objects.filter(user=self.admin).update(household=None)
        response = self.client.get(reverse('meal-planner'))
        self.assertContains(response, 'Mine')
        self.assertNotContains(response, 'Ours')

    def test_membership_changes_are_audited(self):
        home = MealPlanGroup.objects.create(name='Home')
        self.client.post(reverse('household-edit', args=[home.pk]), {'name': 'Home', 'members': [self.cook.pk]})
        entry = AuditLog.objects.filter(object_type='User profile').latest('id')
        self.assertEqual(entry.actor, self.admin)
        self.assertTrue(AuditLog.objects.filter(object_type='Household').exists())


class ThemePreferenceTests(TestCase):
    """Light/dark/auto theme, saved per user instead of per browser."""

    def setUp(self):
        self.user = User.objects.create_user(username='cook', password='pw12345!')
        self.client.force_login(self.user)

    def test_saving_a_theme(self):
        response = self.client.post(reverse('preferences'), {'theme': 'dark', 'timezone': ''})
        self.assertRedirects(response, reverse('preferences'))
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.theme, 'dark')

    def test_an_unknown_theme_is_rejected(self):
        self.client.post(reverse('preferences'), {'theme': 'purple', 'timezone': ''})
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.theme, '')

    def test_saved_theme_is_handed_to_the_page(self):
        UserProfile.objects.filter(user=self.user).update(theme='auto')
        response = self.client.get(reverse('profile'))
        self.assertContains(response, "const preference = 'auto';")
        self.assertNotContains(response, reverse('theme-adopt'))

    def test_unset_theme_adopts_the_browsers_once(self):
        response = self.client.get(reverse('profile'))
        self.assertContains(response, "const preference = '';")
        self.assertContains(response, reverse('theme-adopt'))

        self.client.post(reverse('theme-adopt'), {'theme': 'dark'})
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.theme, 'dark')

    def test_adopt_never_overwrites_a_real_choice(self):
        UserProfile.objects.filter(user=self.user).update(theme='light')
        self.client.post(reverse('theme-adopt'), {'theme': 'dark'})
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.theme, 'light')

    def test_adopt_ignores_junk(self):
        self.client.post(reverse('theme-adopt'), {'theme': '<script>'})
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.theme, '')

    def test_logged_out_pages_use_the_browsers_theme(self):
        self.client.logout()
        self.assertContains(self.client.get(reverse('login')), 'const preference = null;')

    def test_navbar_toggle_is_gone_and_menu_links_preferences(self):
        response = self.client.get(reverse('profile'))
        self.assertNotContains(response, 'themeToggle')
        self.assertContains(response, f'href="{reverse("preferences")}"')

    def test_public_page_stays_dark_whatever_the_preference(self):
        UserProfile.objects.filter(user=self.user).update(theme='light')
        recipe = Recipe.objects.create(name='Jollof', is_public=True, share_token='tok123')
        response = self.client.get(recipe.get_public_url())
        self.assertContains(response, "const forced = 'dark';")


# --- REST API ----------------------------------------------------------------

class ApiTestCase(TestCase):
    """Helpers for calling the API the way a client would: with a session token."""

    def setUp(self):
        # Login throttle counts live in the cache; start every test fresh.
        cache.clear()
        # The first user on an install becomes an Administrator; made here so
        # the users the tests care about get the role they're given.
        User.objects.create_user(username='founder', password='pw12345!')
        self.user = self.make_user('agent', permissions.system_role(permissions.READ_ONLY))

    def make_user(self, username, role):
        user = User.objects.create_user(username=username, password='pw12345!')
        user.profile.role = role
        user.profile.save()
        return user

    def login(self, username='agent', password='pw12345!', **extra):
        return self.client.post('/api/v1/auth/login/', {'username': username, 'password': password, **extra},
                                content_type='application/json')

    def token_for(self, username='agent'):
        response = self.login(username)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['token']

    def get(self, path, token=None, **params):
        token = token or getattr(self, 'token', None) or self.token_for()
        return self.client.get(path, params, HTTP_AUTHORIZATION=f'Bearer {token}')


class ApiAuthTests(ApiTestCase):

    def test_login_returns_a_token_that_works(self):
        response = self.login(client_name='tests')
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body['token'].startswith('jlf_'))
        self.assertEqual(body['user']['username'], 'agent')
        self.assertEqual(body['permissions'], ['api-mealplan-read', 'api-recipe-read'])
        self.assertEqual(body['client_name'], 'tests')
        self.assertEqual(self.get('/api/v1/auth/session/', token=body['token']).status_code, 200)

    def test_only_a_hash_of_the_token_is_stored(self):
        token = self.token_for()
        session = ApiSession.objects.get()
        self.assertNotEqual(session.token_hash, token)
        self.assertEqual(session.token_hash, api_auth.hash_token(token))
        self.assertTrue(token.startswith(session.token_prefix))

    def test_wrong_password_is_refused(self):
        response = self.login(password='nope')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'invalid_credentials')
        self.assertFalse(ApiSession.objects.exists())

    def test_inactive_user_cannot_log_in(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.login().status_code, 400)

    def test_role_without_api_permissions_cannot_log_in(self):
        self.make_user('webonly', Role.objects.create(name='Web only', permissions=['recipe-read']))
        response = self.login('webonly')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'no_api_access')

    def test_must_change_password_blocks_login(self):
        UserProfile.objects.filter(user=self.user).update(must_change_password=True)
        response = self.login()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'password_change_required')

    def test_login_is_throttled_per_username(self):
        for _ in range(10):
            self.login(password='nope')
        self.assertEqual(self.login().status_code, 429)
        # Another account isn't held up by it.
        self.make_user('other', permissions.system_role(permissions.READ_ONLY))
        self.assertEqual(self.login('other').status_code, 201)

    def test_no_key_bad_key_and_wrong_scheme_are_401(self):
        self.assertEqual(self.client.get('/api/v1/recipes/').status_code, 401)
        response = self.get('/api/v1/recipes/', token='jlf_madeup')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'invalid_token')
        self.assertEqual(response['WWW-Authenticate'], 'Bearer')

    def test_browser_session_cookie_is_not_enough(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/api/v1/recipes/').status_code, 401)

    def test_logout_revokes_the_token(self):
        token = self.token_for()
        response = self.client.post('/api/v1/auth/logout/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.get('/api/v1/recipes/', token=token).status_code, 401)

    def test_idle_timeout_ends_the_session(self):
        token = self.token_for()
        later = timezone.now() + timedelta(minutes=61)
        with mock.patch('django.utils.timezone.now', return_value=later):
            response = self.get('/api/v1/recipes/', token=token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'session_expired')
        self.assertFalse(ApiSession.objects.exists())

    def test_use_resets_the_idle_clock(self):
        token = self.token_for()
        start = timezone.now()
        for minutes in (50, 100, 150):
            with mock.patch('django.utils.timezone.now', return_value=start + timedelta(minutes=minutes)):
                self.assertEqual(self.get('/api/v1/recipes/', token=token).status_code, 200, minutes)

    def test_last_used_is_only_written_once_a_minute(self):
        token = self.token_for()
        first = ApiSession.objects.get().last_used_at
        with mock.patch('django.utils.timezone.now', return_value=first + timedelta(seconds=30)):
            self.get('/api/v1/recipes/', token=token)
        self.assertEqual(ApiSession.objects.get().last_used_at, first)
        with mock.patch('django.utils.timezone.now', return_value=first + timedelta(seconds=90)):
            self.get('/api/v1/recipes/', token=token)
        self.assertEqual(ApiSession.objects.get().last_used_at, first + timedelta(seconds=90))

    def test_a_session_in_use_never_runs_out(self):
        token = self.token_for()
        start = timezone.now()
        for hours in range(1, 73):
            with mock.patch('django.utils.timezone.now', return_value=start + timedelta(minutes=hours * 59)):
                self.assertEqual(self.get('/api/v1/recipes/', token=token).status_code, 200, hours)

    def test_expires_at_follows_the_last_use(self):
        token = self.token_for()
        session = ApiSession.objects.get()
        later = session.last_used_at + timedelta(minutes=5)
        with mock.patch('django.utils.timezone.now', return_value=later):
            body = self.get('/api/v1/auth/session/', token=token).json()
        self.assertEqual(body['expires_at'], (later + timedelta(minutes=60)).isoformat().replace('+00:00', 'Z'))
        self.assertNotIn('max_lifetime_hours', body)

    def test_zero_turns_the_idle_timeout_off(self):
        ApiSettings.objects.update_or_create(pk=1, defaults={'idle_timeout_minutes': 0})
        response = self.login()
        self.assertIsNone(response.json()['expires_at'])
        token = response.json()['token']
        with mock.patch('django.utils.timezone.now', return_value=timezone.now() + timedelta(days=400)):
            self.assertEqual(self.get('/api/v1/recipes/', token=token).status_code, 200)

    def test_changing_settings_applies_to_existing_tokens(self):
        token = self.token_for()
        ApiSettings.objects.update_or_create(pk=1, defaults={'idle_timeout_minutes': 5})
        with mock.patch('django.utils.timezone.now', return_value=timezone.now() + timedelta(minutes=6)):
            self.assertEqual(self.get('/api/v1/recipes/', token=token).status_code, 401)

    def test_password_change_revokes_tokens(self):
        token = self.token_for()
        self.user.set_password('new-pw-6789!')
        self.user.save()
        response = self.get('/api/v1/recipes/', token=token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'session_expired')

    def test_deactivating_the_user_revokes_tokens(self):
        token = self.token_for()
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.get('/api/v1/recipes/', token=token).status_code, 401)

    def test_role_change_applies_on_the_next_request(self):
        token = self.token_for()
        self.user.profile.role = Role.objects.create(name='Planner API', permissions=['api-mealplan-read'])
        self.user.profile.save()
        response = self.get('/api/v1/recipes/', token=token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'permission_denied')
        self.assertEqual(self.get('/api/v1/meal-plan/', token=token).status_code, 200)

    def test_forced_password_change_does_not_redirect_api_calls(self):
        """The browser cookie's user owes a password change; the API token's call still answers."""
        token = self.token_for()
        self.client.force_login(self.user)
        UserProfile.objects.filter(user=self.user).update(must_change_password=True)
        self.assertEqual(self.get('/api/v1/recipes/', token=token).status_code, 200)

    def test_every_api_view_names_a_permission(self):
        from main.api import urls as api_urls
        unguarded = {'api-login', 'api-schema', 'api-docs'}
        for pattern in api_urls.urlpatterns:
            view_class = getattr(pattern.callback, 'view_class', None)
            if pattern.name in unguarded:
                continue
            with self.subTest(name=pattern.name):
                self.assertTrue(getattr(view_class, 'required_permission', None),
                                'New API view without required_permission.')

    def test_docs_need_a_logged_in_user_with_api_access(self):
        self.assertEqual(self.client.get('/api/v1/schema/').status_code, 403)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/api/v1/schema/').status_code, 200)
        self.assertEqual(self.client.get('/api/v1/docs/').status_code, 200)


class ApiRecipeTests(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.token = self.token_for()
        thai = Tag.objects.create(label='cuisine', value='thai')
        cooked = Tag.objects.create(label='status', value='cooked')
        self.curry = Recipe.objects.create(
            name='Green curry', description='Spicy', ingredients='coconut milk', instructions='Simmer',
            notes='Use fresh basil', rating=5, complexity=3, effort=2, health=4, vegetarian=True,
            share_token='secret-token', is_public=True)
        self.curry.tags.add(thai, cooked)
        self.salad = Recipe.objects.create(name='apple salad', rating=3, complexity=1, vegan=True)
        self.salad.tags.add(cooked)
        self.stew = Recipe.objects.create(name='Beef stew', complexity=5)
        Favorite.objects.create(user=self.user, recipe=self.stew)

    def names(self, **params):
        response = self.get('/api/v1/recipes/', view='names', **params)
        self.assertEqual(response.status_code, 200, response.content)
        return [r['name'] for r in response.json()['results']]

    def test_views_return_different_field_sets(self):
        names = self.get('/api/v1/recipes/', view='names').json()['results'][0]
        self.assertEqual(set(names), {'id', 'name'})
        summary = self.get('/api/v1/recipes/', ids=self.curry.id).json()['results'][0]
        self.assertNotIn('ingredients', summary)
        self.assertEqual(summary['tags'], ['cuisine:thai', 'status:cooked'])
        self.assertIs(summary['vegan'], False)  # null means no
        full = self.get('/api/v1/recipes/', view='full', ids=self.curry.id).json()['results'][0]
        self.assertEqual(full['ingredients'], 'coconut milk')
        self.assertEqual(full['notes'], 'Use fresh basil')
        self.assertTrue(full['web_url'].endswith(f'/recipe/{self.curry.slug}/'))
        self.assertNotIn('share_token', full)
        self.assertNotIn('secret-token', str(full))

    def test_unknown_view_is_400(self):
        response = self.get('/api/v1/recipes/', view='huge')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'invalid')
        self.assertIn('view', response.json()['error']['fields'])

    def test_detail(self):
        response = self.get(f'/api/v1/recipes/{self.stew.id}/')
        self.assertEqual(response.json()['name'], 'Beef stew')
        self.assertTrue(response.json()['is_favorite'])
        missing = self.get('/api/v1/recipes/99999/')
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()['error']['code'], 'not_found')

    def test_search_matches_the_recipes_page(self):
        self.assertEqual(self.names(q='coconut'), ['Green curry'])
        self.assertEqual(self.names(q='cuisine:thai'), ['Green curry'])

    def test_filters(self):
        self.assertEqual(self.names(cooked='true'), ['apple salad', 'Green curry'])
        self.assertEqual(self.names(favorites='1'), ['Beef stew'])
        self.assertEqual(self.names(public='true'), ['Green curry'])
        self.assertEqual(self.names(vegan='true'), ['apple salad'])
        self.assertEqual(self.names(min_rating=4), ['Green curry'])
        self.assertEqual(self.names(max_complexity=3), ['apple salad', 'Green curry'])
        self.assertEqual(self.names(tag=['status:cooked', 'cuisine:THAI']), ['Green curry'])
        self.assertEqual(self.names(ids=f'{self.stew.id},{self.salad.id}'), ['apple salad', 'Beef stew'])

    def test_bad_filter_values_are_400(self):
        for params in ({'min_rating': 9}, {'tag': 'nocolon'}, {'ids': 'a,b'}, {'sort': 'random'}):
            with self.subTest(params=params):
                self.assertEqual(self.get('/api/v1/recipes/', **params).status_code, 400)

    def test_sorts(self):
        self.assertEqual(self.names(), ['apple salad', 'Beef stew', 'Green curry'])
        self.assertEqual(self.names(sort='highest-rating'), ['Green curry', 'apple salad', 'Beef stew'])
        self.assertEqual(self.names(sort='complexity-desc'), ['Beef stew', 'Green curry', 'apple salad'])
        self.assertEqual(self.names(sort='most-tags'), ['Green curry', 'apple salad', 'Beef stew'])
        self.assertEqual(self.names(sort='favorites-desc'), ['Beef stew', 'apple salad', 'Green curry'])
        self.assertEqual(self.names(sort='public-desc'), ['Green curry', 'apple salad', 'Beef stew'])

    def test_every_sort_key_is_accepted(self):
        for key in recipe_query.SORT_KEYS:
            with self.subTest(sort=key):
                self.assertEqual(self.get('/api/v1/recipes/', sort=key).status_code, 200)

    def test_pagination_envelope_and_caps(self):
        body = self.get('/api/v1/recipes/', page_size=2).json()
        self.assertEqual((body['count'], body['page'], body['page_size'], body['total_pages']), (3, 1, 2, 2))
        self.assertIsNotNone(body['next'])
        self.assertIsNone(body['previous'])
        self.assertEqual(self.get('/api/v1/recipes/', page_size=1000).json()['page_size'], 100)
        self.assertEqual(self.get('/api/v1/recipes/', view='names', page_size=1000).json()['page_size'], 500)
        self.assertEqual(self.get('/api/v1/recipes/', page=9).status_code, 404)

    def test_tags_with_counts(self):
        results = self.get('/api/v1/tags/').json()['results']
        self.assertEqual([(t['label'], t['value'], t['recipe_count']) for t in results],
                         [('cuisine', 'thai', 1), ('status', 'cooked', 2)])
        self.assertEqual(len(self.get('/api/v1/tags/', q='thai').json()['results']), 1)


class ApiMealPlanTests(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.token = self.token_for()
        self.recipe = Recipe.objects.create(name='Jollof')
        self.today = date_class.today()

    def test_personal_planner(self):
        MealPlan.objects.create(user=self.user, date=self.today, slot=1, recipe=self.recipe)
        MealPlan.objects.create(user=self.user, date=self.today, slot=2, custom_text='Leftovers')
        MealPlan.objects.create(user=self.user, date=self.today + timedelta(days=20), slot=1, custom_text='Later')
        other = User.objects.get(username='founder')
        MealPlan.objects.create(user=other, date=self.today, slot=1, custom_text='Not mine')
        body = self.get('/api/v1/meal-plan/').json()
        self.assertIsNone(body['household'])
        self.assertEqual(body['end'], (self.today + timedelta(days=13)).isoformat())
        self.assertEqual([e['name'] for e in body['entries']], ['Jollof', 'Leftovers'])
        self.assertEqual(body['entries'][0]['recipe']['id'], self.recipe.id)
        self.assertIsNone(body['entries'][1]['recipe'])

    def test_household_planner(self):
        household = MealPlanGroup.objects.create(name='Home')
        UserProfile.objects.filter(user=self.user).update(household=household)
        MealPlan.objects.create(group=household, date=self.today, slot=1, custom_text='Shared')
        MealPlan.objects.create(user=self.user, date=self.today, slot=1, custom_text='Old personal')
        WorkspaceItem.objects.create(group=household, recipe=self.recipe)
        FreezerItem.objects.create(group=household, custom_text='Corn')
        body = self.get('/api/v1/meal-plan/').json()
        self.assertEqual(body['household'], {'id': household.id, 'name': 'Home'})
        self.assertEqual([e['name'] for e in body['entries']], ['Shared'])
        self.assertEqual([i['name'] for i in self.get('/api/v1/workspace/').json()['items']], ['Jollof'])
        self.assertEqual([i['name'] for i in self.get('/api/v1/freezer/').json()['items']], ['Corn'])

    def test_date_range(self):
        later = self.today + timedelta(days=30)
        MealPlan.objects.create(user=self.user, date=later, slot=1, custom_text='Later')
        body = self.get('/api/v1/meal-plan/', start=later.isoformat(), end=later.isoformat()).json()
        self.assertEqual([e['name'] for e in body['entries']], ['Later'])
        too_long = self.get('/api/v1/meal-plan/', end=(self.today + timedelta(days=92)).isoformat())
        self.assertEqual(too_long.status_code, 400)
        self.assertEqual(self.get('/api/v1/meal-plan/', start='22/10/2026').status_code, 400)
        backwards = self.get('/api/v1/meal-plan/', start=later.isoformat(), end=self.today.isoformat())
        self.assertEqual(backwards.status_code, 400)


class ApiSessionUiTests(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.admin = self.make_user('boss', permissions.system_role(permissions.ADMINISTRATOR))
        self.mine = ApiSession.objects.get(token_hash=api_auth.hash_token(self.token_for('agent')))
        self.theirs = ApiSession.objects.get(token_hash=api_auth.hash_token(self.token_for('boss')))

    def test_profile_lists_and_revokes_only_your_own(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('profile'))
        self.assertContains(response, self.mine.token_prefix)
        self.assertNotContains(response, self.theirs.token_prefix)
        self.assertEqual(self.client.post(reverse('profile-api-session-revoke', args=[self.theirs.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('profile-api-session-revoke', args=[self.mine.pk])).status_code, 200)
        self.assertEqual(list(ApiSession.objects.all()), [self.theirs])

    def test_platform_settings_lists_and_revokes_everyones(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('platform-settings'))
        self.assertContains(response, self.mine.token_prefix)
        self.assertContains(response, self.theirs.token_prefix)
        self.client.post(reverse('api-session-revoke', args=[self.mine.pk]))
        self.assertFalse(ApiSession.objects.filter(pk=self.mine.pk).exists())

    def test_platform_settings_need_platform_admin(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(reverse('api-session-revoke', args=[self.theirs.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse('api-settings'), {'idle_timeout_minutes': 1}).status_code, 403)

    def test_settings_save_is_validated_and_audited(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('api-settings'), {'idle_timeout_minutes': 99999})
        self.assertContains(response, 'caps at 10080')
        self.client.post(reverse('api-settings'), {'idle_timeout_minutes': 30})
        self.assertEqual(ApiSettings.load().idle_timeout_minutes, 30)
        self.assertTrue(AuditLog.objects.filter(object_type='API settings').exists())

    def test_expired_sessions_are_pruned_from_the_lists(self):
        ApiSession.objects.filter(pk=self.mine.pk).update(last_used_at=timezone.now() - timedelta(hours=2))
        self.client.force_login(self.admin)
        response = self.client.get(reverse('platform-settings'))
        self.assertNotContains(response, f'{self.mine.token_prefix}…')
        self.assertFalse(ApiSession.objects.filter(pk=self.mine.pk).exists())


class ApiReferenceTests(ApiTestCase):

    def test_needs_an_api_permission(self):
        self.client.force_login(self.make_user('webonly', Role.objects.create(name='Web only', permissions=['recipe-read'])))
        self.assertEqual(self.client.get(reverse('api-reference')).status_code, 403)
        self.assertNotContains(self.client.get(reverse('recipes')), reverse('api-reference'))

    def test_linked_from_the_user_menu_and_profile(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('recipes')), reverse('api-reference'))
        self.assertContains(self.client.get(reverse('profile')), reverse('api-reference'))

    def test_examples_use_this_servers_address(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('api-reference'))
        self.assertContains(response, 'http://testserver/api/v1/recipes/')

    def test_covers_every_endpoint_and_sort_key(self):
        """The page is hand-written; this catches it falling behind the code."""
        from main.api import urls as api_urls
        self.client.force_login(self.user)
        page = self.client.get(reverse('api-reference')).content.decode()
        for pattern in api_urls.urlpatterns:
            if pattern.name in ('api-schema', 'api-docs'):
                continue
            path = '/api/v1/' + str(pattern.pattern).split('<')[0]
            with self.subTest(endpoint=path):
                self.assertIn(path, page)
        for key in recipe_query.SORT_KEYS:
            with self.subTest(sort=key):
                self.assertIn(f'<code>{key}</code>', page)


class RecipeSortTests(TestCase):
    """The recipes page sorts through recipe_query, in the database."""

    def setUp(self):
        self.user = User.objects.create_user(username='cook', password='pw12345!')
        self.client.force_login(self.user)
        Recipe.objects.create(name='banana bread', rating=2, effort=None)
        Recipe.objects.create(name='Apple pie', rating=None, effort=3)
        Recipe.objects.create(name='Carrot cake', rating=5, effort=1)

    def page_names(self, sort_by):
        response = self.client.get('/recipes/', {'sort_by': sort_by, 'view': 'list', 'results_per_page': 50})
        return [r.name for r in response.context['recipe_list']]

    def test_name_sort_ignores_case(self):
        self.assertEqual(self.page_names('recipe-name'), ['Apple pie', 'banana bread', 'Carrot cake'])

    def test_unrated_sorts_last_both_ways(self):
        self.assertEqual(self.page_names('highest-rating'), ['Carrot cake', 'banana bread', 'Apple pie'])
        self.assertEqual(self.page_names('effort-asc'), ['Carrot cake', 'Apple pie', 'banana bread'])
        self.assertEqual(self.page_names('effort-desc'), ['Apple pie', 'Carrot cake', 'banana bread'])

    def test_unknown_sort_falls_back_to_name(self):
        self.assertEqual(self.page_names('bogus'), ['Apple pie', 'banana bread', 'Carrot cake'])

    def test_membership_sorts_work_with_nothing_to_sort_on(self):
        """An empty workspace or no favorites used to mean ORDER BY a constant, which Postgres refuses."""
        for sort_by in ('in-planner-desc', 'in-planner-asc', 'favorites-desc', 'favorites-asc'):
            with self.subTest(sort_by=sort_by):
                self.assertEqual(self.page_names(sort_by), ['Apple pie', 'banana bread', 'Carrot cake'])

    def test_in_planner_sort(self):
        WorkspaceItem.objects.create(user=self.user, recipe=Recipe.objects.get(name='Carrot cake'))
        self.assertEqual(self.page_names('in-planner-desc'), ['Carrot cake', 'Apple pie', 'banana bread'])
        self.assertEqual(self.page_names('in-planner-asc'), ['Apple pie', 'banana bread', 'Carrot cake'])


class SafeFetchTests(TestCase):
    """User-supplied URLs mustn't reach the server's own network."""

    def resolve_to(self, address):
        return mock.patch.object(safe_fetch.socket, 'getaddrinfo',
                                 return_value=[(None, None, None, '', (address, 0))])

    def test_private_and_local_addresses_are_refused(self):
        for address in ('127.0.0.1', '10.1.2.3', '192.168.0.5', '169.254.169.254', '::1', 'fd00::1'):
            with self.subTest(address=address), self.resolve_to(address), \
                    mock.patch.object(safe_fetch.requests, 'get') as get:
                with self.assertRaises(safe_fetch.UnsafeURL):
                    safe_fetch.get('http://internal.example/')
                get.assert_not_called()

    def test_only_http_and_https(self):
        for url in ('file:///etc/passwd', 'ftp://a.example/x', 'gopher://a.example/'):
            with self.subTest(url=url), self.assertRaises(safe_fetch.UnsafeURL):
                safe_fetch.get(url)

    def test_public_address_is_fetched(self):
        page = mock.Mock(is_redirect=False)
        with self.resolve_to('93.184.215.14'), \
                mock.patch.object(safe_fetch.requests, 'get', return_value=page) as get:
            self.assertIs(safe_fetch.get('https://a.example/pie', timeout=5), page)
        get.assert_called_once_with('https://a.example/pie', timeout=5, allow_redirects=False)

    def test_a_redirect_to_a_private_address_is_refused(self):
        hop = mock.Mock(is_redirect=True, headers={'Location': 'http://169.254.169.254/latest/'})
        addresses = iter(['93.184.215.14', '169.254.169.254'])

        def getaddrinfo(*args, **kwargs):
            return [(None, None, None, '', (next(addresses), 0))]

        with mock.patch.object(safe_fetch.socket, 'getaddrinfo', side_effect=getaddrinfo), \
                mock.patch.object(safe_fetch.requests, 'get', return_value=hop) as get:
            with self.assertRaises(safe_fetch.UnsafeURL):
                safe_fetch.get('https://a.example/pie')
        self.assertEqual(get.call_count, 1)

    def test_url_import_shows_the_refusal(self):
        user = User.objects.create_user(username='cook', password='pw12345!')
        UserProfile.objects.filter(user=user).update(role=permissions.system_role(permissions.READ_WRITE))
        self.client.force_login(user)
        with self.resolve_to('127.0.0.1'):
            response = self.client.get(reverse('recipe-add-url'), {'url': 'http://localhost:8000/admin/'})
        self.assertContains(response, 'private or local address')
