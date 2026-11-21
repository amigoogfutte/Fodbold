"""Kampdag: stævner, kampe, mål og kort for familiens fodbold – delt mellem alle brugere i Home Assistant."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse

from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components import frontend
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    APP_URL, DOMAIN, EVENT_GOAL, MAX_BODY_BYTES, MAX_LOGO_BYTES, MAX_RECORD_BYTES,
    PANEL_URL_PATH, SAME_GOAL_MS, STORAGE_DIR,
)
from .store import Store

_LOGGER = logging.getLogger(__name__)
WWW = Path(__file__).parent / "www"


class Deling:
    """Hemmeligt link, så andre kan følge med uden at logge ind (kun læse)."""

    def __init__(self, root: str) -> None:
        self.fil = Path(root) / "deling.json"

    def token(self) -> str | None:
        try:
            d = json.loads(self.fil.read_text(encoding="utf-8")) if self.fil.exists() else {}
            t = d.get("token") if isinstance(d, dict) else None
            return t if isinstance(t, str) and len(t) >= 20 else None
        except (OSError, ValueError):
            return None

    def ny(self) -> str:
        t = secrets.token_urlsafe(18)
        self._skriv({"token": t})
        return t

    def stop(self) -> None:
        self._skriv({})

    def _skriv(self, d: dict) -> None:
        tmp = self.fil.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, self.fil)


class SyncView(HomeAssistantView):
    """POST {"poster":[...], "since": n} → {"poster": ændringer siden n, "srv": nyeste}."""

    url = "/api/fodbold/sync"
    name = "api:fodbold:sync"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def post(self, request: web.Request) -> web.Response:
        body = await request.read()
        if len(body) > MAX_BODY_BYTES:
            return self.json_message("For stor", 413)
        try:
            obj = json.loads(body or b"{}")
        except ValueError:
            return self.json_message("Ugyldig JSON", 400)
        if not isinstance(obj, dict):
            return self.json_message("Forkert indhold", 400)
        poster = obj.get("poster") or []
        since = obj.get("since") or 0
        if not isinstance(poster, list) or not isinstance(since, (int, float)):
            return self.json_message("Forkert indhold", 400)
        store = self.store
        ud, srv, nye_maal = await self.hass.async_add_executor_job(store.sync, poster, int(since))
        for m in nye_maal:
            await self._fyr_maal(store, m)
        return self.json({"poster": ud, "srv": srv})

    async def _fyr_maal(self, store: Store, ev: dict) -> None:
        """Sender hændelsen fodbold_maal, så automationer fx kan give besked på telefonen."""
        g = lambda rid: store.get(rid) if rid else None  # noqa: E731
        kamp = await self.hass.async_add_executor_job(g, ev.get("match")) or {}
        staevne = await self.hass.async_add_executor_job(g, kamp.get("tour")) or {}
        indst = await self.hass.async_add_executor_job(g, "settings") or {}
        vores = await self.hass.async_add_executor_job(g, indst.get("own")) or {}
        modst = await self.hass.async_add_executor_job(g, kamp.get("opp")) or {}
        alle = await self.hass.async_add_executor_job(lambda: store.sync([], 0)[0])
        os_ = sum(1 for e in alle if e.get("kind") == "event" and e.get("match") == kamp.get("id")
                  and e.get("t") == "goal" and not e.get("deleted") and e.get("side") == "os")
        dem = sum(1 for e in alle if e.get("kind") == "event" and e.get("match") == kamp.get("id")
                  and e.get("t") == "goal" and not e.get("deleted") and e.get("side") == "dem")
        self.hass.bus.async_fire(EVENT_GOAL, {
            "hvem": "os" if ev.get("side") == "os" else "dem",
            "hold": vores.get("name", "") if ev.get("side") == "os" else modst.get("name", ""),
            "vores_klub": vores.get("name", ""),
            "modstander": modst.get("name", ""),
            "stilling": f"{os_}-{dem}",
            "minut": ev.get("min"),
            "staevne": staevne.get("name", ""),
            "registreret_af": ev.get("by", ""),
        })


class MigView(HomeAssistantView):
    """Hvem er logget ind."""

    url = "/api/fodbold/mig"
    name = "api:fodbold:mig"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        u = request["hass_user"]
        return self.json({"id": u.id, "navn": u.name, "ejer": bool(u.is_owner)})


class DelView(HomeAssistantView):
    """Kun ejeren af Home Assistant: GET = nuværende delelink, POST = lav nyt, DELETE = stop deling."""

    url = "/api/fodbold/del"
    name = "api:fodbold:del"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, deling: Deling) -> None:
        self.hass = hass
        self.deling = deling

    async def get(self, request: web.Request) -> web.Response:
        if not request["hass_user"].is_owner:
            return self.json_message("Kun ejeren", 403)
        return self.json({"token": await self.hass.async_add_executor_job(self.deling.token)})

    async def post(self, request: web.Request) -> web.Response:
        if not request["hass_user"].is_owner:
            return self.json_message("Kun ejeren", 403)
        return self.json({"token": await self.hass.async_add_executor_job(self.deling.ny)})

    async def delete(self, request: web.Request) -> web.Response:
        if not request["hass_user"].is_owner:
            return self.json_message("Kun ejeren", 403)
        await self.hass.async_add_executor_job(self.deling.stop)
        return self.json({"token": None})


class SeView(HomeAssistantView):
    """Uden login, med det hemmelige link: læs alle data (kun læse)."""

    url = "/api/fodbold/se/{token}"
    name = "api:fodbold:se"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, store: Store, deling: Deling) -> None:
        self.hass = hass
        self.store = store
        self.deling = deling

    async def get(self, request: web.Request, token: str) -> web.Response:
        rigtig = await self.hass.async_add_executor_job(self.deling.token)
        if not rigtig or not secrets.compare_digest(rigtig, token):
            return self.json_message("Linket virker ikke længere", 404)
        try:
            since = int(request.query.get("since", "0"))
        except ValueError:
            since = 0
        ud, srv, _ = await self.hass.async_add_executor_job(self.store.sync, [], since)
        skjul = ("dev",)
        return self.json({"poster": [{k: v for k, v in r.items() if k not in skjul} for r in ud], "srv": srv},
                         headers={"Cache-Control": "no-store"})


async def _offentlig(hass: HomeAssistant, url: str) -> bool:
    """Kun almindelige internetadresser – ikke enheder på hjemmenetværket."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
                                                             type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


_IMG_PATTERNS = [
    r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:image',
    r'<link[^>]+rel=["\']apple-touch-icon[^"\']*["\'][^>]*href=["\']([^"\']+)',
    r'<link[^>]+rel=["\'](?:shortcut )?icon["\'][^>]*href=["\']([^"\']+)',
    r'<img[^>]+(?:class|id|alt)=["\'][^"\']*logo[^"\']*["\'][^>]*src=["\']([^"\']+)',
    r'<img[^>]+src=["\']([^"\']+)["\'][^>]*(?:class|id|alt)=["\'][^"\']*logo',
]


class LogoView(HomeAssistantView):
    """POST {"url": ...} → {"data": "data:image/...;base64,..."}.

    Linket kan pege direkte på et billede eller på en side (fx klubbens hjemmeside);
    så findes logoet i sidens oplysninger (og:image, ikon eller et billede med "logo" i navnet).
    """

    url = "/api/fodbold/logo"
    name = "api:fodbold:logo"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def _hent(self, url: str) -> tuple[bytes, str, str]:
        s = async_get_clientsession(self.hass)
        for _ in range(4):
            if not await _offentlig(self.hass, url):
                raise ValueError("Linket kan ikke bruges")
            async with s.get(url, allow_redirects=False, timeout=ClientTimeout(total=15),
                             headers={"User-Agent": "Mozilla/5.0 (Kampdag Home Assistant)"}) as r:
                if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                if r.status != 200:
                    raise ValueError(f"Siden svarede {r.status}")
                data = b""
                async for chunk in r.content.iter_chunked(65536):
                    data += chunk
                    if len(data) > MAX_LOGO_BYTES:
                        raise ValueError("Billedet er for stort")
                return data, (r.headers.get("Content-Type") or "").split(";")[0].strip().lower(), url
        raise ValueError("For mange omdirigeringer")

    async def post(self, request: web.Request) -> web.Response:
        try:
            obj = await request.json()
            url = str(obj.get("url") or "").strip()
        except (ValueError, AttributeError):
            return self.json_message("Ugyldigt indhold", 400)
        if not url:
            return self.json_message("Mangler link", 400)
        if not re.match(r"^https?://", url):
            url = "https://" + url
        try:
            data, ctype, endelig = await self._hent(url)
            if not ctype.startswith("image/"):
                html = data[:500_000].decode("utf-8", "ignore")
                fundet = None
                for pat in _IMG_PATTERNS:
                    m = re.search(pat, html, re.I)
                    if m:
                        fundet = urljoin(endelig, m.group(1).replace("&amp;", "&"))
                        break
                if not fundet:
                    return self.json_message("Fandt intet logo på siden – prøv et link direkte til billedet", 404)
                data, ctype, _ = await self._hent(fundet)
                if not ctype.startswith("image/"):
                    return self.json_message("Linket er ikke et billede", 415)
        except ValueError as e:
            return self.json_message(str(e), 400)
        except (ClientError, asyncio.TimeoutError):
            return self.json_message("Kunne ikke hente linket", 502)
        if ctype == "image/svg+xml" or ctype == "image/svg":
            ctype = "image/svg+xml"
        return self.json({"data": f"data:{ctype};base64,{base64.b64encode(data).decode('ascii')}"})


class AppRedirectView(HomeAssistantView):
    """Kort adresse: /kampdag → appen."""

    url = "/kampdag"
    name = "fodbold:start"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        raise web.HTTPFound(f"{APP_URL}/index.html")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Sæt integrationen op."""
    store = Store(hass.config.path(STORAGE_DIR), MAX_RECORD_BYTES, SAME_GOAL_MS)
    await hass.async_add_executor_job(store.ensure)
    deling = Deling(hass.config.path(STORAGE_DIR))

    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("http_registered"):
        # HTTP-ruter kan ikke fjernes igen, så de registreres kun én gang pr. opstart.
        await hass.http.async_register_static_paths([StaticPathConfig(APP_URL, str(WWW), False)])
        for view in (SyncView(hass, store), MigView(), DelView(hass, deling), SeView(hass, store, deling),
                     LogoView(hass), AppRedirectView()):
            hass.http.register_view(view)
        data["http_registered"] = True

    frontend.async_register_built_in_panel(
        hass,
        component_name="iframe",
        sidebar_title="Fodbold",
        sidebar_icon="mdi:scoreboard-outline",
        frontend_url_path=PANEL_URL_PATH,
        config={"url": f"{APP_URL}/index.html"},
        require_admin=False,
        update=True,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Fjern sidepanelet."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
    return True
