from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from app.models import (
    Episode,
    Item,
    MediaTypes,
    Movie,
    MoviePlay,
    Season,
    Sources,
    Status,
    TV,
)
from integrations.fork_plex_watched_sync import _match_item, _sync_movies, _sync_shows
from integrations.models import PlexAccount


class PlexFullWatchedSyncTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="plex-sync-test")
        self.metadata_patcher = patch(
            "app.providers.services.get_media_metadata",
            return_value={"max_progress": None, "related": {}},
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
        stats = {"movies_checked": 0, "movies_marked_watched": 0, "movies_unwatched": 0, "unmatched": 0}

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
        stats = {"movies_checked": 0, "movies_marked_watched": 0, "movies_unwatched": 0, "unmatched": 0}

        _sync_movies(self.user, self.section, stats, sync_unwatched=True)

        movie.refresh_from_db()
        self.assertEqual(movie.status, Status.IN_PROGRESS.value)
        self.assertIsNone(movie.end_date)
        self.assertTrue(MoviePlay.objects.filter(movie=movie, external_id="older-play").exists())
        self.assertEqual(stats["movies_unwatched"], 1)

    @patch("integrations.fork_plex_watched_sync._all_leaves")
    @patch("integrations.fork_plex_watched_sync.plex_api.fetch_section_all_items")
    def test_retracts_one_latest_episode_play_when_plex_unwatches(
        self, fetch_items, all_leaves
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
        section = {**self.section, "type": "show"}
        stats = {
            "shows_checked": 0,
            "episodes_marked_watched": 0,
            "episodes_unwatched": 0,
            "unmatched": 0,
        }

        _sync_shows(self.user, section, stats, sync_unwatched=True)

        self.assertFalse(Episode.objects.filter(related_season=season, item=episode_item).exists())
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
        self.assertIn("queued", str(list(get_messages(response.wsgi_request))[0]).lower())
