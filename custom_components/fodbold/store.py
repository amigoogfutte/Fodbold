"""Fælles lager for Kampdag (uden Home Assistant-afhængigheder, så det kan testes for sig).

Alt gemmes i /config/fodbold/data.json som {id: post}. Hver post har "updated" (tid fra telefonen)
og "srv" (tid hvor serveren modtog den). Ved synkronisering vinder den senest ændrede udgave af en post.
Telefonerne henter kun poster med srv > det de sidst har set, så der ikke sendes logoer hver gang.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

_LOGGER = logging.getLogger(__name__)
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
KINDS = {"club", "settings", "tour", "match", "event", "team"}


class Store:
    def __init__(self, root: str, max_record_bytes: int, same_goal_ms: int, filename: str = "data.json") -> None:
        self.root = Path(root)
        self.filename = filename
        self.max_record = max_record_bytes
        self.same_ms = same_goal_ms
        self._laas = threading.Lock()
        self._data: dict[str, dict] | None = None
        self._last_srv = 0

    @property
    def _fil(self) -> Path:
        return self.root / self.filename

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, dict]:
        if self._data is None:
            d: dict = {}
            if self._fil.exists():
                try:
                    d = json.loads(self._fil.read_text(encoding="utf-8"))
                    if not isinstance(d, dict):
                        d = {}
                except (OSError, ValueError):
                    _LOGGER.warning("Kunne ikke læse %s", self._fil)
                    d = {}
            self._data = d
            self._last_srv = max([r.get("srv", 0) for r in d.values() if isinstance(r, dict)] or [0])
        return self._data

    def _save(self) -> None:
        tmp = self._fil.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._fil)

    def _srv(self) -> int:
        self._last_srv = max(int(time.time() * 1000), self._last_srv + 1)
        return self._last_srv

    def _dublet(self, data: dict[str, dict], r: dict) -> str | None:
        """Samme mål/kort registreret på en ANDEN telefon inden for 10 sek = samme hændelse.

        Returnerer id på den hændelse, den nye er en dublet af. Hver hændelse kan kun "dække"
        én registrering pr. anden telefon, så to hurtige mål på to telefoner stadig bliver to.
        """
        dev, at = r.get("dev"), r.get("at")
        if not dev or not isinstance(at, (int, float)):
            return None
        kandidater = []
        for o in data.values():
            if (o.get("kind") != "event" or o.get("deleted") or o.get("match") != r.get("match")
                    or o.get("t") != r.get("t") or o.get("side") != r.get("side")
                    or not o.get("dev") or o.get("dev") == dev
                    or not isinstance(o.get("at"), (int, float)) or abs(o["at"] - at) > self.same_ms):
                continue
            daekket = any(x.get("dupOf") == o.get("id") and x.get("dev") == dev for x in data.values())
            if not daekket:
                kandidater.append(o)
        if not kandidater:
            return None
        kandidater.sort(key=lambda o: (abs(o["at"] - at), o.get("at")))
        return kandidater[0]["id"]

    def sync(self, indkomne: list, since: int = 0) -> tuple[list[dict], int, list[dict]]:
        """Flet poster ind. Returnerer (ændringer siden `since`, nyeste srv, nye mål)."""
        with self._laas:
            data = self._load()
            nye_maal: list[dict] = []
            aendret = False
            for r in indkomne:
                if not isinstance(r, dict):
                    continue
                rid, upd = r.get("id"), r.get("updated")
                if (not isinstance(rid, str) or not _ID_RE.match(rid) or not isinstance(upd, (int, float))
                        or isinstance(upd, bool) or r.get("kind") not in KINDS):
                    continue
                if len(json.dumps(r, ensure_ascii=False)) > self.max_record:
                    continue
                cur = data.get(rid)
                if cur is not None and upd <= cur.get("updated", 0):
                    continue
                r = dict(r)
                if r["kind"] == "event" and cur is None and not r.get("deleted"):
                    dup = self._dublet(data, r)
                    if dup:
                        r["deleted"] = True
                        r["dupOf"] = dup
                    elif r.get("t") == "goal":
                        nye_maal.append(r)
                r["srv"] = self._srv()
                data[rid] = r
                aendret = True
            if aendret:
                self._save()
            ud = [r for r in data.values() if r.get("srv", 0) > since]
            return ud, self._last_srv, nye_maal

    def get(self, rid: str) -> dict | None:
        with self._laas:
            return self._load().get(rid)
