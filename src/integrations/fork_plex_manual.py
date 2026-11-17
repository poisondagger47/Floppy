"""Isolated manual Plex connection (Server URL + X-Plex-Token + copyable auth link).

Kept in a standalone ``fork_*`` module with no database migrations so upstream
merges from ``dannyvfilms/Floppy`` stay conflict-free.
"""

from __future__ import annotations

import logging
import re
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunsplit

import requests
from defusedxml import ElementTree
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from requests import RequestException

from app import helpers as app_helpers
from app.db_retry import run_retryable_db_operation
from app.log_safety import exception_summary
from integrations import pending_auth
from integrations.safe_fetch import SelfHostedUrlError, send_to_self_hosted, validate_self_hosted_url

logger = logging.getLogger(__name__)

# Stored inside PlexAccount.section_settings (JSONField) to avoid fork migrations.
# Does not start with '_' so Django templates can read it directly, and contains
# no '::' so it never collides with '<machine_identifier>::<section_id>' keys.
CUSTOM_SERVER_URL_KEY = "fork_plex_server_url"
PENDING_PIN_PROVIDER = "fork_plex_manual"
_PLEX_UI_OR_API_PATH_RE = re.compile(
    r"/(?:web|library|identity|status)(?:/.*)?$", re.IGNORECASE
)


def normalize_plex_server_url(raw_url: str) -> tuple[str, str]:
    """Normalize a user-supplied Plex Server/Web URL into ``(base_url, query_token)``.

    Users frequently paste a Plex Web URL from their browser bar such as
    ``http://192.168.1.50:32400/web/index.html#!/settings`` or an XML inspect URL
    carrying ``?X-Plex-Token=...``. Strip ``/web/...``, query parameters, and
    fragments, and validate the resulting host against ``validate_self_hosted_url``.
    """
    text = (raw_url or "").strip()
    if not text:
        return "", ""

    if "://" not in text:
        text = f"http://{text}"

    try:
        parsed = urlparse(text)
    except ValueError as exc:
        msg = "Invalid Plex server URL."
        raise SelfHostedUrlError("unparsable_url", msg) from exc

    query_params = parse_qs(parsed.query or "")
    extracted_token = (
        (query_params.get("X-Plex-Token") or query_params.get("x-plex-token") or [""])[0]
    ).strip()

    clean_path = _PLEX_UI_OR_API_PATH_RE.sub("", parsed.path or "").rstrip("/")
    normalized = urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, clean_path, "", "")
    ).rstrip("/")

    validate_self_hosted_url(normalized)
    return normalized, extracted_token


def build_copyable_auth_url(code: str) -> str:
    """Build a standalone ``app.plex.tv/auth`` URL without ``forwardUrl``.

    Omitting ``forwardUrl`` lets the user open and approve the link in any
    browser, container tab, or device without needing Plex to redirect back to
    the Floppy instance.
    """
    query = (
        f"clientID={settings.PLEX_CLIENT_IDENTIFIER}"
        f"&code={code}"
        f"&context%5Bdevice%5D%5Bproduct%5D={settings.PLEX_PRODUCT}"
        f"&context%5Bdevice%5D%5Bplatform%5D={settings.PLEX_PLATFORM}"
        f"&context%5Bdevice%5D%5Bdevice%5D={settings.PLEX_DEVICE}"
    )
    return f"https://app.plex.tv/auth#?{query}"


def _parse_media_container(response: requests.Response) -> dict[str, Any]:
    """Extract root MediaContainer attributes from JSON or XML Plex responses."""
    from integrations import plex as plex_api

    content_type = response.headers.get("Content-Type", "")
    if "json" in content_type:
        try:
            payload = response.json()
        except ValueError as exc:
            msg = "Invalid JSON response from Plex server."
            raise plex_api.PlexClientError(msg) from exc
        container = payload.get("MediaContainer") if isinstance(payload, dict) else None
        return container if isinstance(container, dict) else (payload if isinstance(payload, dict) else {})

    try:
        root = ElementTree.fromstring(response.text or "")
    except ElementTree.ParseError as exc:
        msg = "Response was not a valid Plex Media Server XML/JSON payload."
        raise plex_api.PlexClientError(msg) from exc
    return dict(root.attrib)


def fetch_direct_server_identity(server_url: str, token: str) -> dict[str, str]:
    """Verify ``server_url`` + ``token`` directly against the Plex Media Server."""
    from integrations import plex as plex_api

    def _get(endpoint: str) -> dict[str, Any]:
        try:
            response = send_to_self_hosted(
                requests.get,
                f"{server_url}{endpoint}",
                headers=plex_api._headers(token),
                params={"X-Plex-Token": token},
                timeout=10,
                verify=settings.PLEX_SSL_VERIFY,
            )
        except RequestException as exc:
            raise plex_api.PlexClientError(str(exc)) from exc

        if response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            msg = "Plex server rejected the token (401/403)."
            raise plex_api.PlexAuthError(msg)
        if not response.ok:
            msg = f"Plex server returned HTTP {response.status_code}."
            raise plex_api.PlexClientError(msg)
        return _parse_media_container(response)

    container = _get("/")
    machine_id = str(
        container.get("machineIdentifier") or container.get("machine_identifier") or ""
    ).strip()
    friendly_name = str(
        container.get("friendlyName") or container.get("title") or ""
    ).strip()
    my_plex_username = str(container.get("myPlexUsername") or "").strip()

    if not machine_id:
        identity = _get("/identity")
        machine_id = str(
            identity.get("machineIdentifier") or identity.get("machine_identifier") or ""
        ).strip()

    if not machine_id:
        msg = "The URL responded, but did not identify itself as a Plex Media Server."
        raise plex_api.PlexClientError(msg)

    if not friendly_name:
        friendly_name = urlparse(server_url).hostname or "Plex Server"

    return {
        "name": friendly_name,
        "machine_identifier": machine_id,
        "username": my_plex_username,
    }


def fetch_direct_sections(
    server_url: str, token: str
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Fetch server identity and library sections directly from ``server_url``."""
    from integrations import plex as plex_api

    identity = fetch_direct_server_identity(server_url, token)
    sections = plex_api._fetch_sections_from_connection(
        {"uri": server_url},
        {
            "name": identity["name"],
            "machine_identifier": identity["machine_identifier"],
        },
        token,
    )
    return identity, sections


def _account_with_custom_url(token: str | None) -> tuple[Any, str]:
    """Return ``(PlexAccount, server_url)`` if ``token`` has a custom server URL."""
    if not token:
        return None, ""
    try:
        from integrations.models import PlexAccount

        account = (
            PlexAccount.objects.filter(plex_token=token)
            .only("id", "plex_username", "plex_account_id", "section_settings")
            .first()
        )
    except Exception:
        return None, ""

    if not account or not isinstance(account.section_settings, dict):
        return account, ""
    server_url = str(account.section_settings.get(CUSTOM_SERVER_URL_KEY) or "").strip()
    return account, server_url


def install_plex_overrides(plex_module: Any) -> None:
    """Wrap ``plex.py`` discovery helpers so custom Server URLs persist across syncs.

    Accounts without ``fork_plex_server_url`` call the original functions directly
    with zero change in behavior.
    """
    if getattr(plex_module, "_fork_plex_manual_installed", False):
        return

    orig_list_resources = plex_module.list_resources
    orig_list_sections = plex_module.list_sections
    orig_fetch_account = plex_module.fetch_account

    def list_resources_override(token: str) -> list[dict[str, Any]]:
        _account, server_url = _account_with_custom_url(token)
        if not server_url:
            return orig_list_resources(token)

        identity = fetch_direct_server_identity(server_url, token)
        scheme = urlparse(server_url).scheme.lower() or "http"
        direct_conn = {
            "uri": server_url,
            "protocol": scheme,
            "local": "0",
            "relay": "1",
        }
        direct_server: dict[str, Any] = {
            "name": identity["name"],
            "machine_identifier": identity["machine_identifier"],
            "access_token": token,
            "owned": True,
            "connections": [direct_conn],
        }

        servers = [direct_server]
        try:
            cloud_servers = orig_list_resources(token)
        except Exception as exc:
            logger.debug(
                "Using direct Plex server URL only (plex.tv resources unavailable: %s)",
                exception_summary(exc),
            )
            return servers

        for srv in cloud_servers:
            if srv.get("machine_identifier") == identity["machine_identifier"]:
                extra_conns = [
                    c
                    for c in (srv.get("connections") or [])
                    if c.get("uri") and c.get("uri") != server_url
                ]
                direct_server["connections"].extend(extra_conns)
            else:
                servers.append(srv)
        return servers

    def list_sections_override(token: str) -> list[dict[str, Any]]:
        _account, server_url = _account_with_custom_url(token)
        if not server_url:
            return orig_list_sections(token)

        # orig_list_sections calls module-level list_resources (our override above),
        # which places server_url first and stops after the first working URI per server.
        try:
            sections = orig_list_sections(token)
            if sections:
                return sections
        except Exception as exc:
            logger.debug(
                "Falling back to direct Plex sections fetch (%s)",
                exception_summary(exc),
            )

        _identity, direct_sections = fetch_direct_sections(server_url, token)
        return list(direct_sections)

    def fetch_account_override(token: str) -> dict[str, Any]:
        account, server_url = _account_with_custom_url(token)
        if not server_url:
            return orig_fetch_account(token)

        try:
            return orig_fetch_account(token)
        except Exception as exc:
            logger.debug(
                "Falling back to direct Plex server identity for account check: %s",
                exception_summary(exc),
            )
            identity = fetch_direct_server_identity(server_url, token)
            username = (
                identity.get("username")
                or (account.plex_username if account else "")
                or "Plex User"
            )
            account_id = (account.plex_account_id if account else "") or "1"
            return {"username": username, "id": account_id}

    list_resources_override.__wrapped__ = orig_list_resources
    list_sections_override.__wrapped__ = orig_list_sections
    fetch_account_override.__wrapped__ = orig_fetch_account
    plex_module.list_resources = list_resources_override
    plex_module.list_sections = list_sections_override
    plex_module.fetch_account = fetch_account_override
    plex_module._fork_plex_manual_installed = True


def _default_return_url(request) -> str:
    return app_helpers.safe_return_url(
        request,
        request.POST.get("next") or request.GET.get("next"),
        reverse("provider_detail", args=["plex"]) + "?tab=connection",
    )


@login_required
@require_POST
def manual_plex_pin_start(request):
    """Create a Plex PIN and return a copyable ``app.plex.tv/auth`` login URL."""
    from integrations import plex as plex_api

    if getattr(request.user, "is_demo", False):
        return JsonResponse(
            {"detail": "This section is view-only for demo accounts."},
            status=HTTPStatus.FORBIDDEN,
        )

    try:
        pin = plex_api.create_pin()
    except Exception as exc:
        logger.warning("Could not create manual Plex PIN: %s", exception_summary(exc))
        return JsonResponse(
            {"detail": f"Could not generate Plex login link: {exc}"},
            status=HTTPStatus.BAD_GATEWAY,
        )

    auth_url = build_copyable_auth_url(str(pin["code"]))
    pending_auth.put(
        PENDING_PIN_PROVIDER,
        request.user.pk,
        {"pin_id": str(pin["id"]), "code": str(pin["code"]), "auth_url": auth_url},
    )
    return JsonResponse({"ok": True, "authUrl": auth_url, "code": str(pin["code"])})


@login_required
@require_GET
def manual_plex_pin_poll(request):
    """Check whether the user approved the generated ``app.plex.tv`` login link."""
    from integrations import plex as plex_api

    if getattr(request.user, "is_demo", False):
        return JsonResponse(
            {"detail": "This section is view-only for demo accounts."},
            status=HTTPStatus.FORBIDDEN,
        )

    state = pending_auth.get(PENDING_PIN_PROVIDER, request.user.pk)
    if not state or not state.get("pin_id"):
        return JsonResponse({"status": "idle"})

    try:
        token = plex_api.poll_pin(str(state["pin_id"]))
    except plex_api.PlexAuthError:
        # Plex returns no authToken until the user finishes signing in.
        return JsonResponse({"status": "pending", "authUrl": state.get("auth_url", "")})
    except Exception as exc:
        logger.warning("Manual Plex PIN poll failed: %s", exception_summary(exc))
        return JsonResponse(
            {"status": "error", "detail": f"Could not check Plex login status: {exc}"},
            status=HTTPStatus.BAD_GATEWAY,
        )

    pending_auth.clear(PENDING_PIN_PROVIDER, request.user.pk)
    return JsonResponse({"status": "authorized", "token": token})


@login_required
@require_POST
def manual_plex_connect(request):
    """Save a Plex connection using an optional Server URL and an ``X-Plex-Token``."""
    from integrations import plex as plex_api
    from integrations.models import PlexAccount

    if getattr(request.user, "is_demo", False):
        return HttpResponseForbidden("This section is view-only for demo accounts.")

    target_url = _default_return_url(request)
    raw_server_url = (request.POST.get("server_url") or "").strip()
    raw_token = (request.POST.get("plex_token") or "").strip()

    try:
        server_url, url_token = normalize_plex_server_url(raw_server_url)
    except SelfHostedUrlError as exc:
        messages.error(request, f"Invalid Plex Server URL: {exc}")
        return redirect(target_url)

    existing_account = PlexAccount.objects.filter(user=request.user).first()
    plex_token = raw_token or url_token or (existing_account.plex_token if existing_account else "")

    if not plex_token:
        messages.error(
            request,
            "Enter an X-Plex-Token (or generate one with the Plex login link first).",
        )
        return redirect(target_url)

    account_username = ""
    account_id = ""
    sections: list[dict[str, Any]] = []
    server_name: str | None = None
    machine_identifier: str | None = None

    if server_url:
        try:
            identity, direct_sections = fetch_direct_sections(server_url, plex_token)
        except plex_api.PlexAuthError as exc:
            messages.error(request, f"Plex server rejected the token: {exc}")
            return redirect(target_url)
        except plex_api.PlexClientError as exc:
            messages.error(request, f"Could not connect to Plex server URL: {exc}")
            return redirect(target_url)
        except Exception as exc:
            messages.error(request, f"Unexpected error contacting Plex server: {exc}")
            return redirect(target_url)

        sections = list(direct_sections)
        server_name = identity.get("name") or None
        machine_identifier = identity.get("machine_identifier") or None
        account_username = identity.get("username") or ""

        # Best-effort lookup on plex.tv for account ID + username + any extra shared servers.
        try:
            cloud_account = plex_api.fetch_account.__wrapped__(plex_token) if hasattr(plex_api.fetch_account, "__wrapped__") else plex_api.fetch_account(plex_token)
            account_username = (cloud_account.get("username") or account_username).strip()
            account_id = str(cloud_account.get("id") or "").strip()
        except Exception as exc:
            logger.debug(
                "Proceeding with direct Plex server identity (plex.tv user lookup skipped: %s)",
                exception_summary(exc),
            )
            if not account_id:
                account_id = (
                    (existing_account.plex_account_id if existing_account else None) or "1"
                )
    else:
        try:
            cloud_account = plex_api.fetch_account(plex_token)
        except plex_api.PlexAuthError as exc:
            messages.error(request, f"Plex rejected the token: {exc}")
            return redirect(target_url)
        except plex_api.PlexClientError as exc:
            messages.error(request, f"Could not verify Plex token: {exc}")
            return redirect(target_url)
        except Exception as exc:
            messages.error(request, f"Unexpected Plex response: {exc}")
            return redirect(target_url)

        account_username = str(cloud_account.get("username") or "").strip()
        account_id = str(cloud_account.get("id") or "").strip()

        try:
            sections = plex_api.list_sections(plex_token)
        except Exception as exc:
            logger.warning(
                "Connected to Plex via token but could not load libraries: %s",
                exception_summary(exc),
            )
            messages.warning(
                request,
                "Connected to Plex, but could not load libraries yet. Try providing your direct Plex Server URL.",
            )

        if sections:
            server_name = sections[0].get("server_name")
            machine_identifier = sections[0].get("machine_identifier")

    # Keep webhook username allowlist in sync, matching upstream plex_callback.
    if account_username:
        existing_names = [
            u.strip()
            for u in (request.user.plex_usernames or "").split(",")
            if u.strip()
        ]
        if account_username.lower() not in [u.lower() for u in existing_names]:
            request.user.plex_usernames = ", ".join([*existing_names, account_username])
            run_retryable_db_operation(
                lambda: request.user.save(update_fields=["plex_usernames"]),
                operation_name="save Plex usernames",
            )

    merged_settings = dict(
        (existing_account.section_settings if existing_account else None) or {}
    )
    if server_url:
        merged_settings[CUSTOM_SERVER_URL_KEY] = server_url
    else:
        merged_settings.pop(CUSTOM_SERVER_URL_KEY, None)

    defaults = {
        "plex_token": plex_token,
        "plex_username": account_username or (existing_account.plex_username if existing_account else "") or "Plex User",
        "plex_account_id": account_id or (existing_account.plex_account_id if existing_account else "") or "",
        "sections": sections,
        "sections_refreshed_at": timezone.now(),
        "section_settings": merged_settings,
        "server_name": server_name,
        "machine_identifier": machine_identifier,
    }

    run_retryable_db_operation(
        lambda: PlexAccount.objects.update_or_create(
            user=request.user,
            defaults=defaults,
        ),
        operation_name="manual connect Plex",
    )
    pending_auth.clear(PENDING_PIN_PROVIDER, request.user.pk)

    lib_count = len(sections)
    target_desc = f"{server_name} ({server_url})" if server_url and server_name else (server_url or defaults["plex_username"])
    messages.success(
        request,
        f"Connected to Plex ({target_desc}) — {lib_count} librar{'y' if lib_count == 1 else 'ies'} loaded.",
    )
    return redirect(target_url)


urlpatterns = [
    path(
        "import/plex/manual/connect",
        manual_plex_connect,
        name="fork_plex_manual_connect",
    ),
    path(
        "import/plex/manual/pin/start",
        manual_plex_pin_start,
        name="fork_plex_manual_pin_start",
    ),
    path(
        "import/plex/manual/pin/poll",
        manual_plex_pin_poll,
        name="fork_plex_manual_pin_poll",
    ),
]
