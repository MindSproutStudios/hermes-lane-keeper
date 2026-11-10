"""Lane storage: one human-readable markdown file per lane.

Each lane is ``<lanes_dir>/<slug>.md``. It has a YAML front-matter header
(title, status, timestamps, next action) and an append-only ``## Log``
section. The files are meant to be read and edited by people in any editor
or an Obsidian vault. Status changes go through :func:`set_status`, which only
the user-facing command surfaces call.

This module has no Hermes imports, so it can be unit-tested on its own.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

ACTIVE = "active"
DORMANT = "dormant"
ARCHIVED = "archived"
STATUSES = (ACTIVE, DORMANT, ARCHIVED)

_FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_LOG_HEADER = "## Log"
_TS_FMT = "%Y-%m-%d %H:%M"


class LaneError(ValueError):
    """User-facing error (bad lane name, illegal transition, cap reached)."""


def now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def slugify(title: str) -> str:
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")
    return slug[:64].strip("-") or "lane"


def _parse_ts(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    if not value:
        return None
    text = str(value).strip()
    for fmt in (_TS_FMT, "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.astimezone()
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.astimezone()
    except ValueError:
        return None


@dataclass
class Lane:
    slug: str
    title: str
    status: str = DORMANT
    created: Optional[datetime] = None
    updated: Optional[datetime] = None
    next_action: str = ""
    why: str = ""
    body: str = ""  # everything after the front matter, including the log
    extra: dict = field(default_factory=dict)  # unknown front-matter keys, preserved
    file_status: str = ""  # what the markdown header claims; the registry decides

    @property
    def mismatch(self) -> bool:
        """The file claims a status the registry never granted (e.g. a hand/agent edit)."""
        return bool(self.file_status) and self.file_status != self.status

    # -- derived ---------------------------------------------------------
    def age_days(self, ref: Optional[datetime] = None) -> Optional[int]:
        stamp = self.updated or self.created
        if stamp is None:
            return None
        return max(0, ((ref or now()) - stamp).days)

    def log_entries(self) -> list[str]:
        if _LOG_HEADER not in self.body:
            return []
        tail = self.body.split(_LOG_HEADER, 1)[1]
        return [ln[2:].strip() for ln in tail.splitlines() if ln.startswith("- ")]

    def to_dict(self, ref: Optional[datetime] = None) -> dict:
        return {
            "slug": self.slug,
            "title": self.title,
            "status": self.status,
            "next_action": self.next_action,
            "why": self.why,
            "updated": self.updated.strftime(_TS_FMT) if self.updated else None,
            "days_since_update": self.age_days(ref),
        }

    # -- serialisation ---------------------------------------------------
    def render(self) -> str:
        meta = dict(self.extra)
        meta.update({
            "title": self.title,
            "status": self.status,
            "created": self.created.strftime(_TS_FMT) if self.created else None,
            "updated": self.updated.strftime(_TS_FMT) if self.updated else None,
            "next_action": self.next_action or "",
            "why": self.why or "",
        })
        head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000).strip()
        body = self.body if self.body.strip() else f"# {self.title}\n\n{_LOG_HEADER}\n"
        return f"---\n{head}\n---\n{body if body.endswith(chr(10)) else body + chr(10)}"


def parse_lane(path: Path) -> Lane:
    text = path.read_text(encoding="utf-8")
    m = _FRONT_RE.match(text)
    meta, body = ({}, text)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        body = m.group(2)
    if not isinstance(meta, dict):
        meta = {}
    known = {"title", "status", "created", "updated", "next_action", "why"}
    raw_status = str(meta.get("status") or DORMANT).strip().lower()
    status = raw_status if raw_status in STATUSES else DORMANT  # unknown never counts as active
    return Lane(
        slug=path.stem,
        title=str(meta.get("title") or path.stem.replace("-", " ").title()),
        status=status,
        file_status=raw_status,
        created=_parse_ts(meta.get("created")),
        updated=_parse_ts(meta.get("updated")) or _parse_ts(meta.get("created")),
        next_action=str(meta.get("next_action") or ""),
        why=str(meta.get("why") or ""),
        body=body,
        extra={k: v for k, v in meta.items() if k not in known},
    )


class Registry:
    """Authoritative lane status, kept in the plugin's own data folder.

    The markdown files are a readable mirror. If a file's header disagrees with
    the registry (someone, or an agent, edited ``status: active`` by hand),
    the registry wins and the mismatch is reported to the user.

    The first time it sees a lanes folder it adopts the files' statuses
    once ("bootstrap"), so existing notes or an imported vault keep their state.
    Also holds small bits of plugin state (stale notices already given,
    per-session snapshot fingerprints).
    """

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()
        self.data = {"version": 1, "initialized": False, "status": {}, "stale_notified": {}, "sessions": {}}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for key in self.data:
                    if isinstance(loaded.get(key), type(self.data[key])):
                        self.data[key] = loaded[key]
        except (OSError, ValueError):
            pass

    @property
    def initialized(self) -> bool:
        return bool(self.data.get("initialized"))

    def get(self, slug: str) -> Optional[str]:
        st = self.data["status"].get(slug)
        return st if st in STATUSES else None

    def set(self, slug: str, status: str, *, save: bool = True) -> None:
        self.data["status"][slug] = status
        if save:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".registry-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class LaneStore:
    def __init__(self, root: Path | str, registry: Optional[Registry] = None):
        self.root = Path(root).expanduser()
        self.registry = registry

    # -- io --------------------------------------------------------------
    def _ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _write(self, lane: Lane) -> None:
        self._ensure()
        lane.file_status = lane.status  # writing through the store repairs any hand-edited header
        target = self.root / f"{lane.slug}.md"
        fd, tmp = tempfile.mkstemp(prefix=".lane-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(lane.render())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def all(self) -> list[Lane]:
        lanes = []
        if self.root.is_dir():
            for path in sorted(self.root.glob("*.md")):
                if path.name.startswith(".") or path.name.lower() == "readme.md":
                    continue
                try:
                    lanes.append(parse_lane(path))
                except (OSError, UnicodeDecodeError):
                    continue
        reg = self.registry
        if reg is None:
            return lanes
        if not reg.initialized:  # one-time adoption of whatever exists now (possibly nothing)
            for ln in lanes:
                reg.set(ln.slug, ln.status, save=False)
            reg.data["initialized"] = True
            reg.save()
        for ln in lanes:
            granted = reg.get(ln.slug)
            # A file the registry has never seen is dormant, whatever its header says.
            ln.status = granted or DORMANT
        return lanes

    def mismatches(self) -> list[Lane]:
        return [ln for ln in self.all() if ln.mismatch]

    def by_status(self, status: str) -> list[Lane]:
        lanes = [ln for ln in self.all() if ln.status == status]
        far_past = datetime.min.replace(tzinfo=now().tzinfo)
        return sorted(lanes, key=lambda ln: ln.updated or ln.created or far_past, reverse=True)

    # -- lookup ----------------------------------------------------------
    def find(self, query: str) -> Lane:
        """Resolve a lane by slug, exact title, unique prefix, or close fuzzy match."""
        q = (query or "").strip().strip('"').strip("'")
        if not q:
            raise LaneError("Name a lane.")
        lanes = self.all()
        qs = slugify(q)
        for ln in lanes:
            if ln.slug == qs or ln.title.lower() == q.lower():
                return ln
        prefix = [ln for ln in lanes if ln.slug.startswith(qs) or ln.title.lower().startswith(q.lower())]
        if len(prefix) == 1:
            return prefix[0]
        contains = [ln for ln in lanes if qs in ln.slug]
        if len(contains) == 1:
            return contains[0]
        close = difflib.get_close_matches(qs, [ln.slug for ln in lanes], n=3, cutoff=0.6)
        if len(close) == 1:
            return next(ln for ln in lanes if ln.slug == close[0])
        candidates = prefix or contains or [ln for ln in lanes if ln.slug in close]
        if candidates:
            names = ", ".join(f"`{ln.slug}`" for ln in candidates[:5])
            raise LaneError(f"'{q}' matches more than one lane: {names}. Be more specific.")
        raise LaneError(f"No lane matches '{q}'. Run /lanes all to see every lane.")

    def similar(self, title: str, cutoff: float = 0.72) -> list[Lane]:
        """Existing lanes whose title closely matches *title* (duplicate guard)."""
        target = slugify(title)
        out = []
        for ln in self.all():
            ratio = difflib.SequenceMatcher(None, target, ln.slug).ratio()
            if ratio >= cutoff or (len(target) > 4 and (target in ln.slug or ln.slug in target)):
                out.append(ln)
        return out

    # -- mutations -------------------------------------------------------
    def _unique_slug(self, title: str) -> str:
        base = slugify(title)
        slug, n = base, 2
        while (self.root / f"{slug}.md").exists():
            slug, n = f"{base}-{n}", n + 1
        return slug

    def create(self, title: str, *, why: str = "", next_action: str = "",
               status: str = DORMANT, source: str = "") -> Lane:
        title = (title or "").strip()
        if not title:
            raise LaneError("A lane needs a title.")
        if status not in STATUSES:
            raise LaneError(f"Unknown status '{status}'.")
        stamp = now()
        lane = Lane(slug=self._unique_slug(title), title=title, status=status,
                    created=stamp, updated=stamp, next_action=next_action.strip(), why=why.strip())
        intro = f"# {title}\n\n"
        if why.strip():
            intro += f"{why.strip()}\n\n"
        verb = "captured" if status == DORMANT else f"created as {status}"
        lane.body = intro + f"{_LOG_HEADER}\n- {stamp.strftime(_TS_FMT)} · {verb}" + (f" ({source})" if source else "") + "\n"
        if self.registry is not None:
            self.all()  # make sure a fresh registry bootstraps before we add to it
            self.registry.set(lane.slug, status)
        self._write(lane)
        lane.file_status = status
        return lane

    def append_log(self, lane: Lane, kind: str, note: str, *, next_action: Optional[str] = None) -> Lane:
        stamp = now()
        note = " ".join((note or "").split())
        if not note and next_action is None:
            raise LaneError("Nothing to record.")
        body = lane.body if _LOG_HEADER in lane.body else (lane.body.rstrip() + f"\n\n{_LOG_HEADER}\n")
        line = f"- {stamp.strftime(_TS_FMT)} · {kind}"
        if note:
            line += f": {note}"
        if next_action is not None and next_action.strip():
            line += f" → next: {next_action.strip()}"
        lane.body = body.rstrip("\n") + "\n" + line + "\n"
        if next_action is not None:
            lane.next_action = next_action.strip()
        lane.updated = stamp
        self._write(lane)
        return lane

    def set_status(self, lane: Lane, status: str, *, reason: str = "", actor: str = "user") -> Lane:
        if status not in STATUSES:
            raise LaneError(f"Unknown status '{status}'.")
        if lane.status == status:
            raise LaneError(f"`{lane.slug}` is already {status}.")
        old = lane.status
        lane.status = status
        if self.registry is not None:
            self.registry.set(lane.slug, status)
        note = f"{old} → {status} by {actor}" + (f": {reason}" if reason else "")
        return self.append_log(lane, "status", note)
