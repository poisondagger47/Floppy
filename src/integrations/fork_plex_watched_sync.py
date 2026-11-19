"""Fork-only full Plex watched-state reconciliation.

This intentionally lives outside the upstream Plex importer. It scans the
connected user's current library view and reconciles watched state both ways.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import requests
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from app import cache_utils
from app import helpers as app_helpers
from app.models import (
    TV,
    USER_HELD_STATUSES,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from app.services.unwatch import retract_episode_watch, retract_watch
from integrations import plex as plex_api
from integrations.models import PlexAccount
from integrations.safe_fetch import send_to_self_hosted

logger = logging.getLogger(__name__)


def _match_item(plex_item, media_types, source):
    """Match Plex IDs against the selected catalogue, then title/year."""
    if isinstance(media_types, str):
        media_types = (media_types,)
    for guid in plex_item.get("Guid") or []:
        value = (guid.get("id") or "").strip()
        if "://" not in value:
            continue
        provider, external_id = value.split("://", 1)
        external_id = external_id.split("?", 1)[0]
        if not external_id:
            continue
        expected_provider = {
            Sources.TMDB.value: "tmdb",
            Sources.TVDB.value: "tvdb",
        }.get(source)
        if provider == expected_provider:
            item = Item.objects.filter(
                media_type__in=media_types,
                source=source,
                media_id=external_id,
            ).first()
        elif provider == "imdb":
            item = Item.objects.filter(
                media_type__in=media_types,
                source=source,
                provider_external_ids__imdb_id=external_id,
            ).first()
        else:
            item = None
        if item:
            return item

    title = plex_item.get("title")
    if not title:
        return None
    candidates = Item.objects.filter(
        media_type__in=media_types, source=source, title__iexact=title
    )
    if plex_item.get("year"):
        candidates = candidates.filter(release_datetime__year=plex_item["year"])
    return candidates.first() if candidates.count() == 1 else None


def _all_leaves(section, rating_key):
    """Fetch the episode list for a Plex show using its selected server URI."""
    token = section.get("access_token")
    response = send_to_self_hosted(
        requests.get,
        f"{section['uri']}/library/metadata/{rating_key}/allLeaves",
        headers=plex_api._headers(token),
        params={"X-Plex-Token": token},
        timeout=30,
    )
    plex_api._raise_for_auth(response)
    if not response.ok:
        raise plex_api.PlexClientError(  # noqa: TRY003
            f"Plex returned HTTP {response.status_code} while listing episodes."  # noqa: EM102
        )
    if "json" in response.headers.get("Content-Type", ""):
        container = response.json().get("MediaContainer") or {}
        return container.get("Metadata") or []
    return plex_api._parse_history_xml(response.text)[0]


def _leaf_counts(entry):
    """Return Plex's (watched, available) child counts when both are known."""
    try:
        viewed = int(entry.get("viewedLeafCount"))
        total = int(entry.get("leafCount"))
    except (TypeError, ValueError):
        return None
    if total <= 0 or viewed < 0:
        return None
    return min(viewed, total), total


def _season_item(show_item, season_number):
    """Get or create the catalogue row for a season."""
    item, _ = Item.objects.get_or_create(
        media_type=MediaTypes.SEASON.value,
        media_id=show_item.media_id,
        source=show_item.source,
        season_number=season_number,
        defaults={
            "title": f"{show_item.title} Season {season_number}",
            "library_media_type": (
                MediaTypes.ANIME.value
                if show_item.library_media_type == MediaTypes.ANIME.value
                else MediaTypes.SEASON.value
            ),
        },
    )
    return item


def _complete_season_from_plex(user, show_item, tv, season_number, stats):
    """Use Plex's aggregate count for every Floppy tracker of that season."""
    season_item = _season_item(show_item, season_number)
    Season.objects.get_or_create(
        user=user,
        related_tv=tv,
        item=season_item,
        defaults={"status": Status.COMPLETED.value},
    )
    seasons_completed = False
    for season in Season.objects.filter(
        user=user,
        related_tv=tv,
        item__season_number=season_number,
    ):
        if season.status not in USER_HELD_STATUSES | {Status.COMPLETED.value}:
            season.status = Status.COMPLETED.value
            season.save(update_fields=["status"])
        if season.status == Status.COMPLETED.value:
            seasons_completed = True
    if seasons_completed:
        stats["seasons_completed_by_aggregate"] = (
            stats.get("seasons_completed_by_aggregate", 0) + 1
        )
    return Season.objects.filter(
        user=user,
        related_tv=tv,
        item=season_item,
    ).first()


def _retract_unwatched_season(user, season, stats):
    """Retract one latest Floppy play for each Plex-unwatched episode."""
    item_ids = (
        Episode.objects.filter(related_season=season)
        .values_list("item_id", flat=True)
        .distinct()
    )
    for item in Item.objects.filter(id__in=item_ids):
        result = retract_episode_watch(user, item)
        stats["episodes_unwatched"] += result.deleted_plays


def _sync_season_leaves(
    user, show_item, tv, section, season_number, leaves, stats, *, sync_unwatched
):
    """Reconcile a season's individual Plex leaves when its aggregate is mixed."""
    season_item = _season_item(show_item, season_number)
    season = Season.objects.filter(user=user, related_tv=tv, item=season_item).first()
    if season is None:
        if not any(int(leaf.get("viewCount") or 0) > 0 for leaf in leaves):
            return
        season = Season.objects.create(
            user=user,
            related_tv=tv,
            item=season_item,
            status=Status.IN_PROGRESS.value,
        )

    watched = 0
    for leaf in leaves:
        try:
            episode_number = int(leaf.get("index") or leaf["episodeNumber"])
        except (KeyError, TypeError, ValueError):
            continue
        is_plex_watched = int(leaf.get("viewCount") or 0) > 0
        if not is_plex_watched and not sync_unwatched:
            continue
        try:
            ep_item = season.get_episode_item(episode_number)
        except Exception:
            logger.info(
                "Plex full sync could not resolve episode for "
                "user_id=%s show_item_id=%s S%02dE%02d",
                user.id,
                show_item.id,
                season_number,
                episode_number,
            )
            continue
        has_floppy_watch = Episode.objects.filter(
            related_season=season, item=ep_item
        ).exists()
        if not is_plex_watched:
            if has_floppy_watch:
                result = retract_episode_watch(user, ep_item)
                stats["episodes_unwatched"] += result.deleted_plays
            continue
        watched += 1
        if has_floppy_watch:
            continue
        viewed = leaf.get("lastViewedAt")
        watched_at = (
            datetime.fromtimestamp(int(viewed), tz=UTC) if viewed else timezone.now()
        )
        season.watch(episode_number, watched_at, entry_source="plex")
        stats["episodes_marked_watched"] += 1

    if watched:
        season._sync_status_after_episode_change(
            max_progress=len(leaves), plays_added=True
        )


def _plex_tokens_for_user(user, account):
    """Resolve Plex account tokens for the connected owner and Home users."""
    names = [
        name.strip() for name in (user.plex_usernames or "").split(",") if name.strip()
    ]
    owner_name = (account.plex_username or "").strip()
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": "Floppy",
        "X-Plex-Client-Identifier": "Floppy-Full-Watched-Sync",
        "X-Plex-Token": account.plex_token,
    }
    if not names:
        return [account.plex_token]

    tokens = []
    home_users = None
    try:
        for plex_username in names:
            if owner_name and plex_username.casefold() == owner_name.casefold():
                tokens.append(account.plex_token)
                continue
            if home_users is None:
                headers["X-Plex-Token"] = account.plex_token
                home = requests.get(
                    "https://plex.tv/api/v2/home/users", headers=headers, timeout=10
                )
                if not home.ok:
                    raise plex_api.PlexClientError(  # noqa: TRY003
                        "Could not fetch Plex Home users for the managed account."  # noqa: EM101
                    )
                home_users = home.json().get("users", [])
            target = next(
                (
                    entry
                    for entry in home_users
                    if (entry.get("title") or "").casefold() == plex_username.casefold()
                ),
                None,
            )
            if not target or not target.get("uuid"):
                raise plex_api.PlexClientError(  # noqa: TRY003
                    f"Configured Plex Home user '{plex_username}' was not found."  # noqa: EM102
                )
            headers["X-Plex-Token"] = account.plex_token
            switched = requests.post(
                f"https://plex.tv/api/v2/home/users/{target['uuid']}/switch",
                headers=headers,
                json={"pin": ""},
                timeout=10,
            )
            if switched.status_code not in (200, 201):
                raise plex_api.PlexClientError(  # noqa: TRY003
                    f"Could not switch to Plex Home user '{plex_username}'."  # noqa: EM102
                )
            auth_token = switched.json().get("authToken")
            if not auth_token:
                raise plex_api.PlexClientError(  # noqa: TRY003
                    f"Plex returned no authentication token for '{plex_username}'."  # noqa: EM102
                )
            tokens.append(auth_token)
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.warning(
            "Could not resolve Plex Home token for user_id=%s: %s", user.id, exc
        )
        raise plex_api.PlexClientError(  # noqa: TRY003
            "Could not resolve the Plex Home user's server access."  # noqa: EM101
        ) from exc
    return list(dict.fromkeys(tokens))


def _sync_movies(user, section, stats, *, sync_unwatched):
    token = section.get("access_token")
    start = 0
    while True:
        entries, total = plex_api.fetch_section_all_items(
            token, section["uri"], str(section["id"]), start=start
        )
        for entry in entries:
            item = _match_item(entry, MediaTypes.MOVIE.value, Sources.TMDB.value)
            if not item:
                stats["unmatched"] += 1
                library_name = section.get("title", "unknown")
                unmatched_by_library = stats.setdefault("unmatched_by_library", {})
                unmatched_by_library[library_name] = (
                    unmatched_by_library.get(library_name, 0) + 1
                )
                continue
            stats["movies_checked"] += 1
            movie = Movie.objects.filter(user=user, item=item).first()
            is_plex_watched = int(entry.get("viewCount") or 0) > 0
            if not is_plex_watched:
                if sync_unwatched and movie and movie.status == Status.COMPLETED.value:
                    retract_watch(user, item)
                    stats["movies_unwatched"] += 1
                continue
            if movie and movie.status == Status.COMPLETED.value:
                continue
            viewed = entry.get("lastViewedAt")
            watched_at = (
                datetime.fromtimestamp(int(viewed), tz=UTC)
                if viewed
                else timezone.now()
            )
            if movie is None:
                movie = Movie.objects.create(
                    user=user,
                    item=item,
                    status=Status.IN_PROGRESS.value,
                )
            movie.watch(
                watched_at,
                external_id=f"plex-library:{entry.get('ratingKey')}",
                entry_source="plex",
            )
            stats["movies_marked_watched"] += 1
        start += len(entries)
        if not entries or start >= total:
            break


def _sync_shows(
    user,
    section,
    stats,
    *,
    sync_unwatched,
    complete_seasons: set[tuple[int, int]] | None = None,
):
    if complete_seasons is None:
        complete_seasons = set()
    token = section.get("access_token")
    start = 0
    while True:
        entries, total = plex_api.fetch_section_all_items(
            token, section["uri"], str(section["id"]), start=start
        )
        for entry in entries:
            show_item = _match_item(
                entry,
                (MediaTypes.TV.value, MediaTypes.ANIME.value),
                Sources.TVDB.value,
            )
            if not show_item:
                stats["unmatched"] += 1
                library_name = section.get("title", "unknown")
                unmatched_by_library = stats.setdefault("unmatched_by_library", {})
                unmatched_by_library[library_name] = (
                    unmatched_by_library.get(library_name, 0) + 1
                )
                continue
            stats["shows_checked"] += 1
            tv = TV.objects.filter(user=user, item=show_item).first()
            counts = _leaf_counts(entry)

            # A complete Plex show can be reflected through Floppy's existing
            # completion fan-out, which bulk creates season and episode rows.
            # There is no need to request and individually write every Plex
            # episode when its aggregate says every available leaf is watched.
            if (
                counts
                and counts[0] == counts[1]
                and (tv is None or tv.status not in USER_HELD_STATUSES)
            ):
                if tv is None:
                    tv = TV.objects.create(
                        user=user,
                        item=show_item,
                        status=Status.COMPLETED.value,
                    )
                elif tv.status != Status.COMPLETED.value:
                    tv.status = Status.COMPLETED.value
                    tv.save(update_fields=["status"])
                stats["shows_completed_by_aggregate"] = (
                    stats.get("shows_completed_by_aggregate", 0) + 1
                )

            # An entirely unwatched show with no Floppy tracker has no state to
            # retract. Avoid fetching its episode list altogether.
            if counts and counts[0] == 0 and (tv is None or not sync_unwatched):
                continue

            if tv is None:
                tv = TV.objects.create(
                    user=user, item=show_item, status=Status.IN_PROGRESS.value
                )

            plex_seasons = plex_api.fetch_children(
                token, section["uri"], str(entry["ratingKey"])
            )
            if not plex_seasons:
                # Older Plex servers may omit children or aggregate counts.
                leaves = _all_leaves(section, entry["ratingKey"])
                grouped = {}
                for leaf in leaves:
                    try:
                        number = int(leaf["parentIndex"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    grouped.setdefault(number, []).append(leaf)
                for season_number, season_leaves in grouped.items():
                    if all(
                        int(leaf.get("viewCount") or 0) > 0 for leaf in season_leaves
                    ):
                        complete_seasons.add((show_item.pk, season_number))
                    _sync_season_leaves(
                        user,
                        show_item,
                        tv,
                        section,
                        season_number,
                        season_leaves,
                        stats,
                        sync_unwatched=sync_unwatched,
                    )
                continue

            for plex_season in plex_seasons:
                try:
                    season_number = int(
                        plex_season.get("index")
                        or plex_season.get("seasonNumber")
                        or plex_season.get("parentIndex")
                    )
                except (TypeError, ValueError):
                    continue
                complete_key = (show_item.pk, season_number)
                if complete_key in complete_seasons:
                    continue
                season_counts = _leaf_counts(plex_season)
                if (
                    season_counts
                    and season_counts[0] == season_counts[1]
                    and tv.status not in USER_HELD_STATUSES
                ):
                    complete_seasons.add(complete_key)
                    _complete_season_from_plex(
                        user, show_item, tv, season_number, stats
                    )
                    continue

                season_item = _season_item(show_item, season_number)
                season = Season.objects.filter(
                    user=user, related_tv=tv, item=season_item
                ).first()
                if (
                    season_counts
                    and season_counts[0] == 0
                    and (season is None or not sync_unwatched)
                ):
                    continue
                if season_counts and season_counts[0] == 0 and season is not None:
                    _retract_unwatched_season(user, season, stats)
                    continue

                leaves = _all_leaves(section, plex_season["ratingKey"])
                if leaves and all(
                    int(leaf.get("viewCount") or 0) > 0 for leaf in leaves
                ):
                    complete_seasons.add(complete_key)
                _sync_season_leaves(
                    user,
                    show_item,
                    tv,
                    section,
                    season_number,
                    leaves,
                    stats,
                    sync_unwatched=sync_unwatched,
                )

        start += len(entries)
        if not entries or start >= total:
            break


def sync_user(user_id, *, sync_unwatched=True):
    """Synchronize watched state from all accessible Plex show/movie sections."""
    user = get_user_model().objects.get(pk=user_id)
    account = PlexAccount.objects.filter(user=user).first()
    if not account or not account.plex_token:
        raise plex_api.PlexClientError(  # noqa: TRY003
            "Connect Plex before running a full sync."  # noqa: EM101
        )
    tokens = _plex_tokens_for_user(user, account)
    if not tokens:
        raise plex_api.PlexClientError(  # noqa: TRY003
            "No Plex account tokens are available to sync."  # noqa: EM101
        )
    stats = {
        "movies_checked": 0,
        "movies_marked_watched": 0,
        "movies_unwatched": 0,
        "shows_checked": 0,
        "episodes_marked_watched": 0,
        "episodes_unwatched": 0,
        "unmatched": 0,
        "unmatched_by_library": {},
        "shows_completed_by_aggregate": 0,
        "seasons_completed_by_aggregate": 0,
        "ignored_sections": 0,
    }
    try:
        any_sections = False
        for token in tokens:
            sections = plex_api.list_sections(token)
            any_sections = any_sections or bool(sections)
            seen_items = set()
            complete_seasons = set()
            for section in sections:
                kind = section.get("type")
                section_name = str(
                    section.get("title") or section.get("name") or ""
                ).strip()
                if "mintie photos" in section_name.casefold():
                    stats["ignored_sections"] += 1
                    continue
                identity = (section.get("machine_identifier"), str(section.get("id")))
                if identity in seen_items or kind not in {"movie", "show"}:
                    continue
                seen_items.add(identity)
                section["access_token"] = section.get("access_token") or token
                if kind == "movie":
                    _sync_movies(user, section, stats, sync_unwatched=sync_unwatched)
                else:
                    _sync_shows(
                        user,
                        section,
                        stats,
                        sync_unwatched=sync_unwatched,
                        complete_seasons=complete_seasons,
                    )
        if not any_sections:
            raise plex_api.PlexClientError(  # noqa: TRY003
                "No Plex libraries are available to sync."  # noqa: EM101
            )
    finally:
        cache_utils.clear_library_view_caches(
            user.id,
            media_types=(
                MediaTypes.EPISODE.value,
                MediaTypes.TV.value,
                MediaTypes.ANIME.value,
                MediaTypes.MOVIE.value,
                MediaTypes.SEASON.value,
            ),
        )
    logger.info("Plex full watched sync finished user_id=%s counts=%s", user.id, stats)
    return stats


@login_required
@require_POST
def queue_full_watched_sync(request):
    """Queue a bidirectional full-library Plex watched-state sync."""
    account = PlexAccount.objects.filter(user=request.user).first()
    destination = app_helpers.safe_return_url(
        request,
        request.POST.get("next"),
        reverse("provider_detail", args=["plex"]) + "?tab=import",
    )
    if request.user.is_demo:
        messages.error(request, "Demo accounts cannot run a Plex library sync.")
        return redirect(destination)
    if not account or not account.plex_token:
        messages.error(request, "Connect Plex before syncing watched status.")
        return redirect(destination)
    try:
        from integrations.tasks import sync_plex_full_watched_library

        sync_plex_full_watched_library.delay(
            user_id=request.user.id, sync_unwatched=True
        )
    except Exception:
        logger.exception(
            "Could not queue full Plex watched sync user_id=%s", request.user.id
        )
        messages.error(
            request,
            "The Plex sync could not be queued. Check the worker and try again.",
        )
    else:
        messages.success(request, "Full Plex watched-state sync queued.")
    return redirect(destination)


urlpatterns = [
    path(
        "import/plex/sync-watched-full",
        queue_full_watched_sync,
        name="fork_plex_full_watched_sync",
    ),
]
