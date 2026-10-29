# What's New in Jolof

This file powers the in-app "What's New" popup. Each time you ship a release,
bump `VERSION` in `jolof/settings/base.py` and add a matching section below.

Format rules (kept deliberately simple):
- Start each release with `## <version>` — optionally `## <version> — <short title>`.
- List each change as a `- ` bullet. Keep them user-facing and friendly.
- Keep bullets brief: name the feature or fix, then one sentence at most.
  e.g. `- Manage Tags - Added sorting and bulk tag removal.`
- Newest version goes at the top.

## 1.33.0
- Messages - Adding, saving and deleting recipes now confirms with the same dismissible banner as the rest of the app.
- Import from URL - Addresses on private or local networks are refused, for safety.

## 1.32.0 — REST API
- REST API - Read recipes, tags and the meal planner from other apps and AI agents.
- API Reference - A guide to every endpoint, filter and sort, with copyable examples, in the user menu.
- API sessions - See and revoke them on your Profile, or everyone's on Platform Settings, where you can also set how long an unused session lasts.

## 1.31.1
- Roles - API permission badges are now orange, so they stand out in both light and dark themes.

## 1.31.0 — Households
- Households - Group people who share a meal planner, workspace and freezer, from the new Households page under the gear menu or on each user's edit page.

## 1.30.0 — User management
- Users - Add, edit, deactivate and delete users, set their role, and reset their password, from the new gear menu in the navbar.
- Roles - Create custom roles with any mix of permissions; hover a permission to see what it allows.
- Platform Settings - Audit log retention and Delete All Recipes now live on one page under the gear menu.
- Sign-up - The public sign-up page is gone; new accounts are added from the Users page.
- Preferences - A new Preferences page in your user menu holds your theme (light, dark or match your device) and timezone; the theme now follows you across devices.

## 1.29.0 — Roles and permissions
- Roles - Every user now has a role (Administrator, Read-write or Read-only) that decides what they can see and change.
- Admin menu - The Audit Log has moved to a new Admin menu in the navbar.

## 1.28.1 — Tooltips
- Tooltips - Button hints now pop up straight away in a styled tooltip, instead of the browser's slow plain one.

## 1.28.0 — One box for URL imports
- Add From URL - One box now takes a single URL or many; a single recipe opens for review before saving, several are imported in one go.

## 1.27.0 — Source links on shared recipes
- Shared recipes - The public page now includes the recipe's source URL, when it has one.

## 1.26.0 — A real logo
- Logo - Jolof has an official logo, now in the navbar, on the login page, and as the browser tab icon.
- Version footer - Now stays pinned to the bottom of the window, and shows the version on every page.

## 1.25.0 — Find Duplicates, with progress
- Find Duplicates - Now runs in the background with a progress bar, and the match threshold is adjustable instead of fixed at 90%.
- Profile page - Fixed the "My Profile" heading so it lines up with the centered content below it.

## 1.24.0 — Shared recipes
- Shared Recipes - A new page under Tools listing every recipe with a live public link, with copy buttons and an Unshare action.
- Public filter - Filter any recipe view down to public recipes only, from the checkbox or the new Recipes menu entry.
- List view - A globe column shows which recipes are public and links straight to the public page; click it to sort.
- Filters - Sort and all three filters now live in one collapsible Filters panel, with a badge and chips showing what's active.

## 1.23.0 — Your timezone
- Timezone - Pick yours on the Profile page from the full IANA list, and every time across the app shows in it.
- Times were previously shown in UTC for everyone.

## 1.22.0 — Audit log
- Audit Log - A new page under Tools showing every change to recipes, tags, the meal planner, and user accounts, with who made it and when.
- Entry details - Each entry lists which fields an edit touched and links to the recipe; one edit makes one entry.
- Filters - Narrow the log by type, action, or person, or search it by name.
- Retention - History is kept for a year by default; change that on the same page, or set 0 to keep everything.

## 1.21.0 — Profiles and favorites
- Your profile - Update your name, email, and password from the new Profile page in the user menu.
- Favorites - Click the heart on any recipe to save it to your own favorites, then filter or sort by them in any view.

## 1.20.0 — Tag autocomplete
- Recipe tags - Typing a tag now shows a live dropdown of matching existing tags, so you can pick one instead of retyping it.

## 1.19.1 — Tidier tag management
- Manage Tags - Added sortable columns and bulk removal of unused tags.

## 1.19.0 — Manage your tags
- New "Manage Tags" tool under Misc. Tools: every tag in one place, with a live search that narrows the list as you type.
- Search either half of a tag, or use `cuisine:` to see every value filed under a label.
- Each tag shows how many recipes use it — click the number to open those recipes in a new tab, with the search already filled in.
- Add a tag straight from the page, rename one, or delete it; renames and deletions apply to every recipe using that tag.
- Renaming a tag onto one that already exists merges the two instead of refusing, moving its recipes across.
- Fixed: the "Clear" checkbox on the recipe edit page now actually removes the photo. It used to be ignored, and silently threw away any other edits made in the same save. Cleared recipes fall back to the default illustration.
- Photos you clear or replace are now deleted from storage instead of being left behind for good.
- When an edit can't be saved, the recipe page now says why and points at the field that needs fixing, instead of quietly reloading as if nothing happened.

## 1.18.0 — Smoother updates
- New releases now go out through a single deployment step with automatic checks beforehand, so a bad configuration gets caught before it reaches the site rather than after.
- Jolof keeps serving pages while a new version is being built — the switchover is now a few seconds at the end instead of the site being down for the whole update.
- Every update takes a database backup first and can be put straight back to the previous version, so a problem during an update no longer risks your recipes.

## 1.17.0 — CSV Import, rebuilt
- Importing now starts with just the file: pick your CSV and Jolof reads it, shows you what it found, and waits. Nothing is imported until you say so.
- Columns are matched by name in any order, and friendly names like "Source URL" work as well as the raw ones.
- Any column that doesn't match a recipe field is called out, with an example of what's in it, and you can map it to a field yourself — so a file with a "title" column instead of "name" now imports fine instead of being rejected.
- Untick any recognised column you'd rather not import.
- Choose what happens when a recipe is already in your collection: skip it, overwrite it with the row from the file, or bring it in as a second copy.
- Everything CSV Export writes now comes back in — ratings, dietary flags, tags, and public sharing status included, not just the recipe text.
- A progress bar tracks the import row by row, like the URL batch import, so a big file doesn't just sit there looking frozen.
- The results tell you what was added, what was overwritten, what was skipped as a duplicate, which columns you mapped, and exactly which rows had a problem and why — long lists are trimmed to the first 20 with a count of the rest.
- Files saved out of Excel no longer trip over their own encoding marker.

## 1.16.0 — Export your recipes to CSV
- New CSV Export tool under Tools, next to CSV Import: downloads every recipe as a spreadsheet.
- Tick exactly the columns you want — recipe text, ratings, dietary flags, tags, and optional extras like the slug, image link, timestamps, and public sharing status.
- Tags come out as `label:value` pairs in one column, the same format the tags box on a recipe accepts.
- The file opens cleanly in Excel, with accents and multi-line ingredients and instructions intact.
- Tidied the Tools menu: the Recipe Roulette, Advanced Search, and Users entries were placeholders that went nowhere, so they're gone for now.

## 1.15.0 — Share a recipe publicly
- New "Make Public" checkbox at the bottom of a recipe page generates a shareable link anyone can open without logging in.
- The public page shows just the name, photo, description, ingredients, and instructions — ratings, tags, effort/complexity/healthiness, diet flags, and notes stay private by default, and there are no edit/delete/meal-planner buttons.
- Optional second checkbox to also include the notes section on the public page, for recipes where you want to share those too.
- Public pages always use the dark theme and won't show up in search engines.
- Turning "Make Public" off deactivates the link immediately; turning it back on brings back the same link rather than generating a new one.

## 1.14.0 — Live meal planner
- The meal planner now updates itself every few seconds — meals, workspace cards, and freezer items added from another tab, device, or a shared meal-plan-group member show up without refreshing the page.
- These background checks are silent unless something actually changed, so recipe thumbnails no longer flicker every few seconds.
- Updates pause automatically while you're dragging a card or reading a recipe's hover popup, so nothing gets interrupted mid-action.

## 1.13.0 — Recipe page tidy-up
- Tidier layout at the top of the recipes page: the recipe count now sits above the search box, with the Add buttons and view buttons on one line beneath it.
- Fixed the total recipe count sometimes showing the filtered number (like 6) and staying stuck there until you refreshed.
- The meal planner is no longer marked "beta".

## 1.12.0 — Readable recipe links
- Recipe pages now live at a readable address like `/recipe/air-fryer-cuban-sweet-plantains/` instead of a numbered one.
- Every old link and bookmark still works — they redirect to the new address automatically.
- Rename a recipe and its link updates to match, while the previous link keeps working too.

## 1.11.0 — What's in my freezer
- New "Freezer" tab in the meal planner to keep a running list of what's in your freezer — recipes or your own items like "corn" or "ground beef".
- Drag freezer items onto any calendar slot, and the planner offers to remove them from your freezer once scheduled.
- Recipe cards in the meal planner now show the recipe's tags in the hover popup.
- The app version moved to a footer at the bottom of the page.

## 1.10.0 — Custom workspace cards & handy tweaks
- Stage your own custom cards in the meal-planner workspace — type your own wording (like "Leftovers" or "Eat out") and drag it onto any calendar slot, just like recipe cards.
- Sort the recipe list view by whether a recipe is in the meal planner — click the calendar icon in the Actions column header, click again to flip direction.
- Clear a rating on the edit page: the main rating and the effort, complexity, and healthiness ratings each have a "Clear" button now.
- The Find Duplicates review page now shows each recipe's notes, so you don't accidentally delete the copy you've written notes on. Recipe links there also open in a new tab.

## 1.9.0 — Recipe list view & tools
- The list view now shows effort, complexity, and healthiness ratings as color-coded columns, plus a quick "add to meal planner" button on each row.
- Sort the list view right from the column headers — click a header to sort, click again to flip direction. A "Sort by date added" checkbox handles sorting by newest.
- New "Find Duplicates" tool in Misc. Tools scans for recipes with identical or very similar names so you can review and clean them up.
- New "Only show cooked recipes" filter on every recipe view.

## 1.8.5 — What's New popup
- Added a "What's New" popup that highlights new features and fixes after each update.
- The popup only appears once per version, and remembers your dismissal across all your devices.

## 1.8.4 — Meal planner polish
- Recipe cards in the meal planner now show a hover card with the rating and effort, complexity, and healthiness scores.
- Longer recipe names wrap to two lines instead of being cut off.
- Redesigned the workspace pane into a tidy, scrollable grid.
