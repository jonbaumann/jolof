from django import forms
from django.contrib.auth.forms import SetPasswordForm, UserCreationForm
from django.contrib.auth.models import User
from django.urls import reverse_lazy
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Submit

from . import csv_fields, csv_import, permissions
from . import timezones
from .models import ApiSettings, AuditSettings, MealPlanGroup, Recipe, Role, Tag, UserProfile


MUST_CHANGE_HELP = "They'll be asked to pick their own password as soon as they log in."


def _role_field():
    return forms.ModelChoiceField(
        queryset=Role.objects.all(),
        empty_label=None,
        help_text='What this user can see and change.',
    )


HOUSEHOLD_HELP = ("People in a household share one meal planner, workspace and freezer. "
                  "Someone in none has their own.")


def _household_field():
    return forms.ModelChoiceField(
        queryset=MealPlanGroup.objects.all(),
        required=False,
        empty_label='None (their own planner)',
        help_text=HOUSEHOLD_HELP,
    )


class UserAddForm(UserCreationForm):
    """Add User, under user management. Replaces the old public sign-up page."""

    email = forms.EmailField(required=True)
    first_name = forms.CharField(required=True)
    role = _role_field()
    household = _household_field()
    must_change_password = forms.BooleanField(
        required=False, initial=True,
        label='Require a password change at next login', help_text=MUST_CHANGE_HELP,
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "role", "household",
                  "password1", "password2", "must_change_password"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['role'].initial = permissions.system_role(permissions.READ_ONLY)

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            # The post_save signal has already given the user a default role;
            # replace it with the one picked here.
            profile = user.profile
            profile.role = self.cleaned_data['role']
            profile.household = self.cleaned_data['household']
            profile.must_change_password = self.cleaned_data['must_change_password']
            profile.save(update_fields=['role', 'household', 'must_change_password'])
        return user


class UserEditForm(forms.ModelForm):
    """Edit another user's account details, role and household. Timezone stays the user's own."""

    email = forms.EmailField(required=True)
    role = _role_field()
    household = _household_field()

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'role', 'household', 'is_active']
        labels = {'is_active': 'Active'}
        help_texts = {
            'username': None,
            'is_active': "Inactive users can't log in, but their history is kept.",
        }

    def __init__(self, *args, acting_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.acting_user = acting_user
        self.fields['role'].initial = self.instance.profile.role
        self.fields['household'].initial = self.instance.profile.household

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get('role')
        is_active = cleaned.get('is_active')
        if role is None:
            return cleaned
        if self.acting_user and self.instance.pk == self.acting_user.pk and not is_active:
            raise forms.ValidationError("You can't deactivate your own account.")
        if permissions.leaves_no_user_manager(
                user_changes={self.instance.pk: {'role': role, 'is_active': is_active}}):
            raise forms.ValidationError(permissions.LOCKOUT_MESSAGE)
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            profile = user.profile
            changed = []
            for field in ('role', 'household'):
                if getattr(profile, field) != self.cleaned_data[field]:
                    setattr(profile, field, self.cleaned_data[field])
                    changed.append(field)
            if changed:
                profile.save(update_fields=changed)
        return user


class AdminSetPasswordForm(SetPasswordForm):
    """Set another user's password, as someone with user management."""

    must_change_password = forms.BooleanField(
        required=False, initial=True,
        label='Require a password change at next login', help_text=MUST_CHANGE_HELP,
    )

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            profile = user.profile
            profile.must_change_password = self.cleaned_data['must_change_password']
            profile.save(update_fields=['must_change_password'])
        return user


class RoleForm(forms.ModelForm):
    """A custom role. System roles are never edited, so never reach here."""

    permissions = forms.MultipleChoiceField(
        choices=[(p.code, p.label) for p in permissions.PERMISSIONS],
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = Role
        fields = ['name', 'description', 'permissions']

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        clash = Role.objects.filter(name__iexact=name).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f'There is already a role called "{clash.first().name}".')
        return name

    def clean_permissions(self):
        # Stored with implied permissions filled in, so the role's page shows
        # everything it actually grants.
        return sorted(permissions.expand(self.cleaned_data['permissions']))

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and 'permissions' in cleaned and permissions.leaves_no_user_manager(
                role_changes={self.instance.pk: cleaned['permissions']}):
            raise forms.ValidationError(permissions.LOCKOUT_MESSAGE)
        return cleaned


class HouseholdForm(forms.ModelForm):
    """A household's name and who's in it."""

    members = forms.ModelMultipleChoiceField(
        queryset=User.objects.select_related('profile__household').order_by('username'),
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={'class': 'form-check-input'}),
    )

    class Meta:
        model = MealPlanGroup
        fields = ['name', 'members']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['members'].initial = User.objects.filter(profile__household=self.instance)
        self.fields['members'].label_from_instance = self._member_label

    def _member_label(self, user):
        household = user.profile.household
        if household and household.pk != self.instance.pk:
            return f'{user.username} (in {household.name})'
        return user.username

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        clash = MealPlanGroup.objects.filter(name__iexact=name).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f'There is already a household called "{clash.first().name}".')
        return name

    def save(self, commit=True):
        household = super().save(commit=commit)
        if commit:
            members = self.cleaned_data['members']
            # Ticked users join (leaving any other household); unticked ones
            # go back to a planner of their own. Saved one by one so each
            # move shows up in the audit log.
            for profile in UserProfile.objects.filter(household=household).exclude(user__in=members):
                profile.household = None
                profile.save(update_fields=['household'])
            for profile in UserProfile.objects.filter(user__in=members).exclude(household=household):
                profile.household = household
                profile.save(update_fields=['household'])
        return household


class UserProfileEditForm(forms.ModelForm):
    """Your own account details. Theme and timezone are on PreferencesForm."""

    email = forms.EmailField(required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_action = ''
        self.helper.form_method = 'POST'
        self.helper.add_input(Submit('submit', 'Save Changes'))

    class Meta:
        model = User
        fields = ('username', 'first_name', 'last_name', 'email')


class PreferencesForm(forms.ModelForm):
    """How Jolof looks and behaves for you: theme and timezone."""

    theme = forms.ChoiceField(
        choices=UserProfile.THEME_CHOICES,
        widget=forms.RadioSelect(attrs={'class': 'form-check-input'}),
        label='Theme',
    )
    timezone = forms.ChoiceField(
        required=False,
        label='Timezone',
        help_text='Times across the app are shown in this timezone.',
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    class Meta:
        model = UserProfile
        fields = ('theme', 'timezone')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Built per instance: the blank option names the current site default.
        self.fields['timezone'].choices = timezones.grouped_choices()
        if not self.instance.theme:
            self.initial['theme'] = UserProfile.THEME_LIGHT

    def clean_timezone(self):
        name = self.cleaned_data.get('timezone', '')
        # ChoiceField already rejects anything off the list; this keeps a name
        # that has since left the tz database from being stored.
        if name and not timezones.is_valid(name):
            raise forms.ValidationError('Unknown timezone.')
        return name


def _dietary_field(label):
    """One of the dietary flags, as a tick box.

    The column is null-able, but an unticked box cleans to False, so a flag
    nobody has answered is treated as a "no" -- and saving a recipe settles it
    as one. That is deliberate: see partials/dietary_flag.html.
    """
    return forms.BooleanField(
        required=False,
        label=label,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )


class RecipeForm(forms.ModelForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_action = reverse_lazy('recipe-add')
        self.helper.form_method = 'POST'
        self.helper.add_input(Submit('submit', 'Submit'))
        if self.instance.pk:
            tags = self.instance.tags.all()
            self.initial['tags_input'] = ', '.join([str(tag) for tag in tags])

        # Remembered before validation runs: _post_clean() swaps instance.image
        # for the cleared/uploaded one, so by save() time nothing on the instance
        # still points at the file currently sitting in S3.
        self._original_image_name = ''
        if self.instance.pk and self.instance.image:
            self._original_image_name = self.instance.image.name

    @property
    def dietary_fields(self):
        """The four allergy-info fields, for templates that lay them out as a set."""
        return [self[name] for name in ('gluten_free', 'vegetarian', 'vegan', 'dairy_free')]

    def save(self, commit=True):
        recipe = super().save(commit=commit)

        # Django only ever rewrites the column -- it never removes the file it
        # stops pointing at, so both clearing and replacing an image would leave
        # the old object orphaned in S3. Comparing against the name captured in
        # __init__ covers both: clearing leaves '' behind, replacing leaves the
        # new key. A re-upload under the same key overwrites in place and keeps
        # the same name, so it correctly deletes nothing.
        if commit and self._original_image_name and recipe.image.name != self._original_image_name:
            Recipe._meta.get_field('image').storage.delete(self._original_image_name)
            self._original_image_name = recipe.image.name or ''

        if commit:
            # Clear existing tags
            recipe.tags.clear()
            
            # Validate proper syntax for tags
            tags_input = self.cleaned_data.get('tags_input', '')
            if tags_input:
                for tag_str in tags_input.split(','):
                    tag_str = tag_str.strip()
                    if ':' in tag_str:
                        label, value = tag_str.split(':', 1)
                        label = label.strip()
                        value = value.strip()
                        if label and value:
                            tag, _ = Tag.objects.get_or_create(
                                label=label,
                                value=value
                            )
                            recipe.tags.add(tag)
        return recipe

    name = forms.CharField(
        required = True,
        widget=forms.TextInput(attrs= {
            'maxlength': '100',
            'class': 'textinput form-control',
            'placeholder': ' Name*',
            'style': 'border-color: #949494;',
        })     
    )
    description = forms.CharField(
        required = False,
        widget=forms.Textarea(attrs={
            'class': 'textinput form-control',
            'placeholder': ' Description',
            'rows': '3',
            'style': 'border-color: #949494;',
            }),
    )
    ingredients= forms.CharField(
        required = True,
        widget=forms.Textarea(attrs = {
            'placeholder': ' Ingredients*',
            'class': 'textinput form-control',
            'rows': '10',
            'style': 'border-color: #949494;',
            'required': True
        }),
        error_messages={'required': 'Please enter the ingredients.'}
    )
    instructions= forms.CharField(
        required = True,
        widget=forms.Textarea(attrs= {
            'placeholder': ' Instructions*',
            'class': 'textinput form-control',
            'rows': '20',
            'style': 'border-color: #949494;',
            'required': True
        }),
        error_messages={'required': 'Please enter the instructions.'}
    )
    notes= forms.CharField(
        required = False,
        widget=forms.Textarea(attrs= {
            'placeholder': ' Notes',
            'class': 'textinput form-control',
            'rows': '5',
            'style': 'border-color: #949494;',
        }),
    )
    url = forms.CharField(
        required = False,
        widget = forms.TextInput(attrs= {
            'class': 'textinput form-control',
            'placeholder': ' https://url',
            'style': 'border-color: #949494;',
        })
    )
    rating = forms.TypedChoiceField(
        required = False,
        empty_value = None,
        label = "rating",
        widget = forms.RadioSelect, 
        choices = [
            ('1', '1'),
            ('2', '2'),
            ('3', '3'),
            ('4', '4'),
            ('5', '5'),
        ],
    )
    effort = forms.TypedChoiceField(
        required = False,
        empty_value = None,
        label = "effort",
        widget = forms.RadioSelect, 
        choices = [
            ('1', '1'),
            ('2', '2'),
            ('3', '3'),
            ('4', '4'),
            ('5', '5'),
        ],
    )
    health = forms.TypedChoiceField(
        required = False,
        empty_value = None,
        label = "health",
        widget = forms.RadioSelect, 
        choices = [
            ('1', '1'),
            ('2', '2'),
            ('3', '3'),
            ('4', '4'),
            ('5', '5'),
        ],
    )
    complexity = forms.TypedChoiceField(
        required = False,
        empty_value = None,
        label = "complexity",
        widget=forms.RadioSelect, 
        choices = [
            ('1', '1'),
            ('2', '2'),
            ('3', '3'),
            ('4', '4'),
            ('5', '5'),
        ],
    )
    gluten_free = _dietary_field('Gluten Free')
    vegetarian = _dietary_field('Vegetarian')
    vegan = _dietary_field('Vegan')
    dairy_free = _dietary_field('Dairy Free')
    image = forms.ImageField(
        required = False,
        label = 'Upload Image',
        widget=forms.ClearableFileInput(attrs={
            'class': 'form-control',
            'style': 'border-color: #949494;',
            })
    )

    tags_input = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'e.g., cuisine:asian, diet:vegetarian'
        }),
        help_text='Comma-separated label:value pairs'
    )

    tag_cooked = forms.BooleanField(
        required=False,
        label='tag:cooked',
        widget=forms.CheckboxInput(attrs={
            'id': 'id_tag_cooked',
            'class': 'form-check-input'
        })
    )

    class Meta:
        model = Recipe
        fields = ('name', 'description', 'ingredients', 'instructions', 'rating', 'notes', 'url', 'effort', 'health', 'complexity', 
                  'gluten_free', 'vegetarian', 'vegan', 'dairy_free', 'image')

class CSVUploadForm(forms.Form):
    """Step one of the import: just the file. Its columns are read from it."""

    csv_file = forms.FileField(
        widget=forms.FileInput(attrs={'class': 'form-control', 'accept': '.csv'})
    )

class CSVImportForm(forms.Form):
    """Step two: which of the file's columns to apply, and what to do about
    recipes already in the database.

    Only validates the checkbox selection and the duplicate choice. The
    checkboxes are rendered from the file's own columns, and the mapping
    dropdowns for unrecognised columns are validated by csv_import.resolve(),
    which is the piece that knows what a legal mapping is.
    """

    fields = forms.MultipleChoiceField(
        choices=[(key, csv_fields.FIELDS_BY_KEY[key].label) for key in csv_fields.IMPORT_KEYS],
        required=False,
    )

    on_duplicate = forms.ChoiceField(
        choices=csv_import.DUPLICATE_CHOICES,
        initial=csv_import.SKIP,
    )

class CSVExportForm(forms.Form):
    """Validates the checkbox selection only -- the checkboxes themselves are
    rendered from csv_fields.FIELD_GROUPS so they can be laid out in groups."""

    fields = forms.MultipleChoiceField(
        choices=[(key, csv_fields.FIELDS_BY_KEY[key].label) for key in csv_fields.ALL_KEYS],
        error_messages={'required': 'Pick at least one field to export.'},
    )

class URLScrapeFormBatch(forms.Form):
    """One or more recipe URLs, one per line. A single URL goes to the
    edit-before-saving page; several are imported in the background."""

    urls = forms.CharField(
        required = True,
        error_messages={'required': 'Paste at least one URL.'},
        widget=forms.Textarea(attrs={
            'class': 'textinput form-control',
            'placeholder': 'https://example.com/recipe-1 \nhttps://example.com/recipe-2 \nhttps://example.com/recipe-3',
            'rows': '10',
            'style': 'border-color: #949494;',
            }),
    )

    def clean_urls(self):
        """Returns the URLs as a list, skipping blank lines and repeats. Each
        line is checked the same way a single URLField would check it, so a
        bare "example.com/recipe" still gets https:// added."""
        url_field = forms.URLField(assume_scheme="https")
        urls, bad_lines = [], []
        for line in self.cleaned_data['urls'].splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                url = url_field.clean(line)
            except forms.ValidationError:
                bad_lines.append(line)
                continue
            if url not in urls:
                urls.append(url)
        if bad_lines:
            raise forms.ValidationError(
                ['These lines aren\'t valid URLs:'] + bad_lines)
        if not urls:
            raise forms.ValidationError('Paste at least one URL.')
        return urls


class AuditRetentionForm(forms.ModelForm):
    """How long the audit log keeps its history."""

    class Meta:
        model = AuditSettings
        fields = ['retention_days']
        labels = {'retention_days': 'Keep history for'}
        widgets = {
            'retention_days': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': 0,
                'max': 3650,
                'style': 'max-width: 8rem;',
            }),
        }

    def clean_retention_days(self):
        days = self.cleaned_data['retention_days']
        if days > 3650:
            raise forms.ValidationError('Retention caps at 3650 days (10 years).')
        return days


class ApiSettingsForm(forms.ModelForm):
    """How long an idle API session token lasts."""

    class Meta:
        model = ApiSettings
        fields = ['idle_timeout_minutes']
        labels = {
            'idle_timeout_minutes': 'Idle timeout (minutes)',
        }
        widgets = {
            'idle_timeout_minutes': forms.NumberInput(attrs={
                'class': 'form-control', 'min': 0, 'max': 10080, 'style': 'max-width: 8rem;',
            }),
        }

    def clean_idle_timeout_minutes(self):
        minutes = self.cleaned_data['idle_timeout_minutes']
        if minutes > 10080:
            raise forms.ValidationError('The idle timeout caps at 10080 minutes (a week).')
        return minutes
