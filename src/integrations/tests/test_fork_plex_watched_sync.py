from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Movie,
    MoviePlay,
    Season,
    Sources,
    Status,
)
from integrations.fork_plex_watched_sync import (
    _match_item,
    _sync_movies,
    _sync_shows,
    sync_user,
)
from integrations.models import PlexAccount


class PlexFullWatchedSyncTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="plex-sync-test")
        self.metadata_patcher = patch(
            "app.providers.services.get_media_metadata",
            return_value={"max_progress": None, "related": {}, "episodes": []},
        )
        self.metadata_patcher.start()
        self.addCleanup(self.metadata_patcher.stop)
        self.section = {
            "id": "1",
            "type": "movie",
            "machine_identifier": "plex-server",
            "uri": "http://plex.test:32400",
            "access_token": "plex-token",
        }

    @patch("integrations.fork_plex_watched_sync.plex_api.fetch_section_all_items")
    def test_marks_watched_movie_and_creates_attributed_play(self, fetch_items):
        item = Item.objects.create(
            media_type=MediaTypes.MOVIE.value,
            source=Sources.TMDB.value,
            media_id="123",
            title="Test Movie",
        )
        fetch_items.return_value = (
            [
                {
                    "ratingKey": "movie-1",
                    "Guid": [{"id": "tmdb://123"}],
                    "title": "Test Movie",
                    "viewCount": 1,
                    "lastViewedAt": 1_700_000_000,
                }
            ],
            1,
        )
        stats = {
            "movies_checked": 0,
            "movies_marked_watched": 0,
            "movies_unwatched": 0,
            "unmatched": 0,
        }

        _sync_movies(self.user, self.section, stats, sync_unwatched=True)

        movie = Movie.objects.get(user=self.user, item=item)
        play = MoviePlay.objects.get(movie=movie)
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(play.external_id, "plex-library:movie-1")
        self.assertEqual(stats["movies_marked_watched"], 1)

    def test_series_match_uses_tvdb_catalog_and_accepts_anime_type(self):
        item = Item.objects.create(
            media_type=MediaTypes.ANIME.value,
            source=Sources.TVDB.value,
            media_id="789",
            title="Test Anime",
        )

        matched = _match_item(
            {
                "Guid": [
                    {"id": "tmdb://111"},
                    {"id": "tvdb://789"},
                ],
                "title": "Test Anime",
            },
            (MediaTypes.TV.value, MediaTypes.ANIME.value),
            Sources.TVDB.value,
        )

        self.assertEqual(matched, item)

    def test_movie_match_uses_tmdb_catalog(self):
        item = Item.objects.create(
            media_type=MediaTypes.MOVIE.value,
            source=Sources.TMDB.value,
            media_id="123",
            title="Test Movie",
        )

        matched = _match_item(
            {
                "Guid": [
                    {"id": "tvdb://999"},
                    {"id": "tmdb://123"},
                ],
                "title": "Test Movie",
            },
            MediaTypes.MOVIE.value,
            Sources.TMDB.value,
        )

        self.assertEqual(matched, item)

    @patch("integrations.fork_plex_watched_sync._all_leaves")
    @patch("integrations.fork_plex_watched_sync.plex_api.fetch_children")
    @patch("integrations.fork_plex_watched_sync.plex_api.fetch_section_all_items")
    def test_fully_watched_show_uses_aggregate_without_fetching_leaves(
        self, fetch_items, fetch_children, all_leaves
    ):
        item = Item.objects.create(
            media_type=MediaTypes.TV.value,
            source=Sources.TVDB.value,
            media_id="789",
            title="Completed Show",
        )
        fetch_items.return_value = (
            [
                {
                    "ratingKey": "show-1",
                    "Guid": [{"id": "tvdb://789"}],
                    "title": "Completed Show",
                    "viewedLeafCount": 8,
                    "leafCount": 8,
                }
            ],
            1,
        )
        fetch_children.return_value = [
            {
                "ratingKey": "season-1",
                "index": 1,
                "viewedLeafCount": 8,
                "leafCount": 8,
            },
        ]
        stats = {
            "shows_checked": 0,
            "episodes_marked_watched": 0,
            "episodes_unwatched": 0,
            "unmatched": 0,
        }

        _sync_shows(
            self.user,
            {**self.section, "type": "show"},
            stats,
            sync_unwatched=True,
        )

        self.assertEqual(
            TV.objects.get(user=self.user, item=item).status,
            Status.COMPLETED.value,
        )
        self.assertEqual(
            Season.objects.get(user=self.user, related_tv__item=item).status,
            Status.COMPLETED.value,
        )
        fetch_children.assert_called_once()
        all_leaves.assert_not_called()
        self.assertEqual(stats["shows_completed_by_aggregate"], 1)

    @patch("integrations.fork_plex_watched_sync._all_leaves")
    @patch("integrations.fork_plex_watched_sync.plex_api.fetch_children")
    @patch("integrations.fork_plex_watched_sync.plex_api.fetch_section_all_items")
    def test_partial_duplicate_library_cannot_undo_completed_season(
        self, fetch_items, fetch_children, all_leaves
    ):
        show_item = Item.objects.create(
            media_type=MediaTypes.TV.value,
            source=Sources.TVDB.value,
            media_id="789",
            title="Duplicate Library Show",
        )
        fetch_items.side_effect = [
            (
                [
                    {
                        "ratingKey": "show-complete-copy",
                        "Guid": [{"id": "tvdb://789"}],
                        "title": "Duplicate Library Show",
                        "viewedLeafCount": 20,
                        "leafCount": 24,
                    }
                ],
                1,
            ),
            (
                [
                    {
                        "ratingKey": "show-partial-copy",
                        "Guid": [{"id": "tvdb://789"}],
                        "title": "Duplicate Library Show",
                        "viewedLeafCount": 19,
                        "leafCount": 24,
                    }
                ],
                1,
            ),
        ]
        fetch_children.side_effect = [
            [
                {
                    "ratingKey": "season-complete-copy",
                    "index": 6,
                    "viewedLeafCount": 21,
                    "leafCount": 21,
                }
            ],
            [
                {
                    "ratingKey": "season-partial-copy",
                    "index": 6,
                    "viewedLeafCount": 19,
                    "leafCount": 21,
                }
            ],
        ]
        complete_seasons = set()
        stats = {
            "shows_checked": 0,
            "episodes_marked_watched": 0,
            "episodes_unwatched": 0,
            "unmatched": 0,
        }

        _sync_shows(
            self.user,
            {**self.section, "type": "show", "title": "4k TV Shows"},
            stats,
            sync_unwatched=True,
            complete_seasons=complete_seasons,
        )
        _sync_shows(
            self.user,
            {**self.section, "type": "show", "title": "TV Shows"},
            stats,
            sync_unwatched=True,
            complete_seasons=complete_seasons,
        )

        season = Season.objects.get(user=self.user, related_tv__item=show_item)
        self.assertEqual(season.status, Status.COMPLETED.value)
        self.assertIn((show_item.pk, 6), complete_seasons)
        all_leaves.assert_not_called()

    @patch("integrations.fork_plex_watched_sync._all_leaves")
    @patch("integrations.fork_plex_watched_sync.plex_api.fetch_children")
    @patch("integrations.fork_plex_watched_sync.plex_api.fetch_section_all_items")
    def test_fully_watched_season_uses_aggregate_without_fetching_leaves(
        self, fetch_items, fetch_children, all_leaves
    ):
        show_item = Item.objects.create(
            media_type=MediaTypes.TV.value,
            source=Sources.TVDB.value,
            media_id="789",
            title="Partially Completed Show",
        )
        tv = TV.objects.create(
            user=self.user,
            item=show_item,
            status=Status.IN_PROGRESS.value,
        )
        alternate_season_item = Item.objects.create(
            media_type=MediaTypes.SEASON.value,
            source=Sources.TMDB.value,
            media_id="111",
            season_number=1,
            title="Partially Completed Show Season 1",
        )
        alternate_season = Season.objects.create(
            user=self.user,
            related_tv=tv,
            item=alternate_season_item,
            status=Status.IN_PROGRESS.value,
        )
        fetch_items.return_value = (
            [
                {
                    "ratingKey": "show-1",
                    "Guid": [{"id": "tvdb://789"}],
                    "title": "Partially Completed Show",
                    "viewedLeafCount": 8,
                    "leafCount": 16,
                }
            ],
            1,
        )
        fetch_children.return_value = [
            {
                "ratingKey": "season-1",
                "index": 1,
                "viewedLeafCount": 8,
                "leafCount": 8,
            },
            {
                "ratingKey": "season-2",
                "index": 2,
                "viewedLeafCount": 0,
                "leafCount": 8,
            },
        ]

        _sync_shows(
            self.user,
            {**self.section, "type": "show"},
            {
                "shows_checked": 0,
                "episodes_marked_watched": 0,
                "episodes_unwatched": 0,
                "unmatched": 0,
            },
            sync_unwatched=True,
        )

        season = Season.objects.get(
            related_tv=tv,
            item__season_number=1,
            item__source=Sources.TVDB.value,
        )
        self.assertEqual(season.status, Status.COMPLETED.value)
        alternate_season.refresh_from_db()
        self.assertEqual(alternate_season.status, Status.COMPLETED.value)
        all_leaves.assert_not_called()

    @patch("integrations.fork_plex_watched_sync.plex_api.fetch_section_all_items")
    def test_retracts_movie_status_but_keeps_play_history_when_plex_unwatches(
        self, fetch_items
    ):
        item = Item.objects.create(
            media_type=MediaTypes.MOVIE.value,
            source=Sources.TMDB.value,
            media_id="123",
            title="Test Movie",
        )
        movie = Movie.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            end_date=datetime(2023, 1, 1, tzinfo=UTC),
        )
        MoviePlay.objects.create(
            movie=movie,
            end_date=datetime(2023, 1, 1, tzinfo=UTC),
            external_id="older-play",
        )
        fetch_items.return_value = (
            [
                {
                    "ratingKey": "movie-1",
                    "Guid": [{"id": "tmdb://123"}],
                    "title": "Test Movie",
                    "viewCount": 0,
                }
            ],
            1,
        )
        stats = {
            "movies_checked": 0,
            "movies_marked_watched": 0,
            "movies_unwatched": 0,
            "unmatched": 0,
        }

        _sync_movies(self.user, self.section, stats, sync_unwatched=True)

        movie.refresh_from_db()
        self.assertEqual(movie.status, Status.IN_PROGRESS.value)
        self.assertIsNone(movie.end_date)
        self.assertTrue(
            MoviePlay.objects.filter(movie=movie, external_id="older-play").exists()
        )
        self.assertEqual(stats["movies_unwatched"], 1)

    @patch("integrations.fork_plex_watched_sync._all_leaves")
    @patch("integrations.fork_plex_watched_sync.plex_api.fetch_children")
    @patch("integrations.fork_plex_watched_sync.plex_api.fetch_section_all_items")
    def test_retracts_one_latest_episode_play_when_plex_unwatches(
        self, fetch_items, fetch_children, all_leaves
    ):
        show_item = Item.objects.create(
            media_type=MediaTypes.TV.value,
            source=Sources.TVDB.value,
            media_id="456",
            title="Test Show",
        )
        tv = TV.objects.create(user=self.user, item=show_item)
        season_item = Item.objects.create(
            media_type=MediaTypes.SEASON.value,
            source=Sources.TVDB.value,
            media_id="456",
            season_number=1,
            title="Test Show Season 1",
        )
        season = Season.objects.create(
            user=self.user,
            related_tv=tv,
            item=season_item,
            status=Status.IN_PROGRESS.value,
        )
        episode_item = Item.objects.create(
            media_type=MediaTypes.EPISODE.value,
            source=Sources.TVDB.value,
            media_id="456",
            season_number=1,
            episode_number=1,
            title="Pilot",
        )
        Episode.objects.create(
            related_season=season,
            item=episode_item,
            end_date=datetime(2023, 1, 1, tzinfo=UTC),
        )
        fetch_items.return_value = (
            [
                {
                    "ratingKey": "show-1",
                    "Guid": [{"id": "tvdb://456"}],
                    "title": "Test Show",
                }
            ],
            1,
        )
        all_leaves.return_value = [
            {"ratingKey": "episode-1", "parentIndex": 1, "index": 1, "viewCount": 0}
        ]
        fetch_children.return_value = [
            {
                "ratingKey": "season-1",
                "index": 1,
                "viewedLeafCount": 0,
                "leafCount": 1,
            }
        ]
        section = {**self.section, "type": "show"}
        stats = {
            "shows_checked": 0,
            "episodes_marked_watched": 0,
            "episodes_unwatched": 0,
            "unmatched": 0,
        }

        _sync_shows(self.user, section, stats, sync_unwatched=True)

        self.assertFalse(
            Episode.objects.filter(related_season=season, item=episode_item).exists()
        )
        self.assertEqual(stats["episodes_unwatched"], 1)

    @patch("integrations.tasks.sync_plex_full_watched_library.delay")
    def test_button_queues_bidirectional_sync_for_connected_user(self, queue_task):
        PlexAccount.objects.create(
            user=self.user,
            plex_token="plex-token",
            plex_username="plex-user",
        )
        self.client.force_login(self.user)

        response = self.client.post(reverse("fork_plex_full_watched_sync"))

        self.assertEqual(response.status_code, 302)
        queue_task.assert_called_once_with(user_id=self.user.id, sync_unwatched=True)
        self.assertIn(
            "queued", str(next(iter(get_messages(response.wsgi_request)))).lower()
        )

    @patch("integrations.fork_plex_watched_sync.cache_utils.clear_library_view_caches")
    @patch("integrations.fork_plex_watched_sync.plex_api.fetch_section_all_items")
    @patch("integrations.fork_plex_watched_sync.plex_api.list_sections")
    @patch(
        "integrations.fork_plex_watched_sync._plex_tokens_for_user",
        return_value=["plex-token"],
    )
    def test_ignores_mintie_photos_section(
        self, _tokens, list_sections, fetch_items, _clear_caches
    ):
        PlexAccount.objects.create(
            user=self.user,
            plex_token="plex-token",
            plex_username="plex-user",
        )
        list_sections.return_value = [
            {
                "id": "photos",
                "title": "Mintie Photos",
                "type": "movie",
                "machine_identifier": "plex-server",
            }
        ]

        stats = sync_user(self.user.id)

        self.assertEqual(stats["ignored_sections"], 1)
        fetch_items.assert_not_called()
