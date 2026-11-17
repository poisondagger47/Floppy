"""Tests for the isolated manual Plex connection module."""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from integrations import plex as plex_api
from integrations.fork_plex_manual import (
    CUSTOM_SERVER_URL_KEY,
    normalize_plex_server_url,
)
from integrations.models import PlexAccount
from integrations.safe_fetch import SelfHostedUrlError


def _mock_response(status_code=200, json_data=None, text="", content_type="application/json"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.headers = {"Content-Type": content_type}
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


class ForkPlexManualTests(TestCase):
    """Verify isolated manual Plex URL/token/PIN connection flows."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="plextester",
            password="test-password-123",
        )
        self.client.force_login(self.user)

    def test_normalize_plex_server_url_strips_web_ui_and_extracts_token(self):
        url, token = normalize_plex_server_url(
            "http://192.168.1.50:32400/web/index.html#!/settings?X-Plex-Token=my_tok"
        )
        self.assertEqual(url, "http://192.168.1.50:32400")
        self.assertEqual(token, "")

        url2, token2 = normalize_plex_server_url(
            "192.168.1.50:32400/library/metadata/1?X-Plex-Token=xml_token"
        )
        self.assertEqual(url2, "http://192.168.1.50:32400")
        self.assertEqual(token2, "xml_token")

    def test_normalize_plex_server_url_blocks_link_local_ssrf(self):
        with self.assertRaises(SelfHostedUrlError):
            normalize_plex_server_url("http://169.254.169.254/latest/meta-data")

    @patch("integrations.plex.poll_pin")
    @patch("integrations.plex.create_pin")
    def test_pin_start_and_poll_generate_copyable_link_without_forward_url(
        self, mock_create_pin, mock_poll_pin
    ):
        mock_create_pin.return_value = {"id": "999", "code": "ABCD"}

        start_resp = self.client.post(reverse("fork_plex_manual_pin_start"))
        self.assertEqual(start_resp.status_code, 200)
        payload = start_resp.json()
        self.assertTrue(payload["ok"])
        self.assertIn("https://app.plex.tv/auth#?", payload["authUrl"])
        self.assertIn("code=ABCD", payload["authUrl"])
        self.assertNotIn("forwardUrl=", payload["authUrl"])

        mock_poll_pin.side_effect = plex_api.PlexAuthError("not ready")
        poll_pending = self.client.get(reverse("fork_plex_manual_pin_poll"))
        self.assertEqual(poll_pending.json()["status"], "pending")

        mock_poll_pin.side_effect = None
        mock_poll_pin.return_value = "resolved_plex_token_123"
        poll_ok = self.client.get(reverse("fork_plex_manual_pin_poll"))
        self.assertEqual(
            poll_ok.json(),
            {"status": "authorized", "token": "resolved_plex_token_123"},
        )

    @patch("integrations.fork_plex_manual.send_to_self_hosted")
    @patch("integrations.plex.send_to_self_hosted")
    @patch("integrations.plex.requests.get")
    def test_manual_connect_with_direct_server_url_works_offline_from_plex_tv(
        self, mock_cloud_get, mock_plex_self_hosted, mock_manual_self_hosted
    ):
        # Simulate plex.tv being unreachable / token being server-local.
        mock_cloud_get.side_effect = plex_api.PlexClientError("plex.tv unreachable")

        mock_manual_self_hosted.return_value = _mock_response(
            json_data={
                "MediaContainer": {
                    "machineIdentifier": "mach-local-1",
                    "friendlyName": "HomeNAS",
                    "myPlexUsername": "swapnil_plex",
                }
            }
        )
        mock_plex_self_hosted.return_value = _mock_response(
            json_data={
                "MediaContainer": {
                    "Directory": [
                        {
                            "key": "1",
                            "title": "Movies",
                            "type": "movie",
                            "uuid": "u-1",
                        }
                    ]
                }
            }
        )

        resp = self.client.post(
            reverse("fork_plex_manual_connect"),
            {
                "server_url": "http://192.168.1.99:32400/web/index.html#!/",
                "plex_token": "direct_tok_abc",
            },
        )
        self.assertEqual(resp.status_code, 302)

        account = PlexAccount.objects.get(user=self.user)
        self.assertEqual(account.plex_token, "direct_tok_abc")
        self.assertEqual(account.plex_username, "swapnil_plex")
        self.assertEqual(account.server_name, "HomeNAS")
        self.assertEqual(account.machine_identifier, "mach-local-1")
        self.assertEqual(
            account.section_settings.get(CUSTOM_SERVER_URL_KEY),
            "http://192.168.1.99:32400",
        )
        self.assertEqual(len(account.sections), 1)
        self.assertEqual(account.sections[0]["uri"], "http://192.168.1.99:32400")

        self.user.refresh_from_db()
        self.assertIn("swapnil_plex", self.user.plex_usernames)

        # Verify overrides keep using the direct server URL on subsequent refreshes/checks.
        refreshed_sections = plex_api.list_sections("direct_tok_abc")
        self.assertEqual(len(refreshed_sections), 1)
        self.assertEqual(refreshed_sections[0]["uri"], "http://192.168.1.99:32400")

        acct_check = plex_api.fetch_account("direct_tok_abc")
        self.assertEqual(acct_check["username"], "swapnil_plex")

    def test_provider_connection_tab_renders_manual_card_alongside_builtin(self):
        resp = self.client.get(reverse("provider_detail", args=["plex"]) + "?tab=connection")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("Manual Plex connection (URL / Token)", html)
        self.assertIn(reverse("fork_plex_manual_connect"), html)
        self.assertIn(reverse("plex_connect"), html)
