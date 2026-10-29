# Households

A household is a group of people who share one meal planner, workspace and
freezer. When one member adds, moves or removes something, the others see it.
Anyone not in a household has a planner of their own.

Roles and households are separate. A role decides what a user can do, and a
household decides whose planner they see.

## Managing households

Households are managed in the app by anyone whose role has the `user-mgmt`
permission: **Admin menu (gear icon) → Households** (`/households/`).

- **The Households page** lists each household with its members and planner
  entry count. Below that is an "Own planner:" line naming everyone who isn't in
  a household.
- **Add / Edit:** give the household a name (names are unique, ignoring case)
  and tick its members. A user already in another household is shown as
  "user (in Other)". Ticking them moves them here. Unticking someone sends them
  back to their own planner.
- **Users:** the Add User and Edit User pages also have a Household field. The
  Users list, the user's Profile page and the meal planner header (house icon)
  all show the household.
- **Delete** is disabled while a household still has members. Deleting an empty
  household also deletes its planner entries, workspace and freezer.

Every membership change is recorded in the Audit Log as a change to the user's
User profile.

## Behaviour to know about

- **One household per user.** It's a single foreign key on the user's profile.
- **Personal entries are kept, not merged.** A user's own planner, workspace and
  freezer are hidden while they're in a household, and come back when they leave.
  Nothing is copied into the household.
- **Deleting a user** removes their personal entries. Anything they added to a
  household stays there, with no "added by" user.

## Code map

The model is still called `MealPlanGroup` (table `meal_plan_groups`) and planner
rows still use a `group` field. "Household" is the name users see. The code
names were kept to avoid a table rename.

| Path | What it does |
|---|---|
| `MealPlanGroup` in `main/models.py` | The household: just a name. |
| `UserProfile.household` | Membership. FK with `PROTECT`, null means the user has their own planner. |
| `MealPlan.group`, `WorkspaceItem.group`, `FreezerItem.group` | Household rows have `group` set. Personal rows have `group = NULL` and are filtered by `user`. `CASCADE` on the household. |
| `main/planner.py` | `get_user_group()` and `owner_filter()`: the one place that decides whose rows a user sees. The planner pages and the API both use it. |
| `HouseholdForm` in `main/forms.py` | Name check and the member tick boxes. Saves each move separately so each one is audited. |
| `households`, `household_add/edit/delete` in `main/views.py` | The Households pages. |
| Migration `0045` | Moved the old many-to-many `members` onto `UserProfile.household`. A user who was in several groups kept the lowest-numbered one. |

Django admin still lists households (**Main → Households**) and lets you set a
user's household under **User profiles**. You shouldn't need it, though.
