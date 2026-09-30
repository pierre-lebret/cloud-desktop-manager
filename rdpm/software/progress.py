"""Suivi d'un travail détaché sur un bureau (installation Linux, logiciels) : état de l'unité ou de la tâche,
jalons « RDPM-STEP » / « RDPM-APP » écrits par les scripts, fin du journal."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import catalog

_STEP_RE = re.compile(r"^RDPM-STEP (\d+)/(\d+) (.+)$")
_START = re.compile(r"^RDPM-APP start (\d+)/(\d+) (\S+)")
_RESULT = re.compile(r"^RDPM-APP (ok|fail) (\S+)")
_UPDATE = re.compile(r"^RDPM-APP update (\S+)")


@dataclass
class AppsProgress:
    current: tuple[int, int, str] | None = None     # (i, total, clé) du logiciel en cours
    ok: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    done: bool = False

    @property
    def label(self) -> str:
        if not self.current:
            return "préparation"
        i, total, key = self.current
        app = catalog.APPS_BY_KEY.get(key)
        return f"{i}/{total} : {app.name if app else key}"

    def percent(self) -> int | None:
        if not self.current:
            return None
        i, total, _ = self.current
        return int((i - 1) / max(total, 1) * 100)


def parse_app_marks(marks: list[str]) -> AppsProgress:
    p = AppsProgress()
    for mark in marks:
        mark = mark.strip()
        if mark.startswith("RDPM-APPS-DONE"):
            p.done = True
            continue
        m = _START.match(mark)
        if m:
            p.current = (int(m.group(1)), int(m.group(2)), m.group(3))
            continue
        m = _UPDATE.match(mark)
        if m:
            p.current = (0, 0, m.group(1))
            continue
        m = _RESULT.match(mark)
        if m:
            target = p.ok if m.group(1) == "ok" else p.failed
            key = m.group(2)
            if key not in target:
                target.append(key)
    p.failed = [k for k in p.failed if k not in p.ok]
    return p


def parse_inventory(out: str) -> set[str] | None:
    """Clés de la ligne « RDPM-APPS-STATUS … » ; None si la ligne est absente."""
    for line in out.splitlines():
        if line.startswith("RDPM-APPS-STATUS"):
            return {k for k in line.split()[1:] if k in catalog.APPS_BY_KEY}
    return None


@dataclass
class InstallStatus:
    unit: str                 # ActiveState de l'unité rdpm-install (« unknown » si illisible)
    marks: list[str]
    tail: str

    @property
    def done(self) -> bool:
        return "RDPM-DONE" in self.marks

    @property
    def failure(self) -> str | None:
        failed = [m for m in self.marks if m.startswith("RDPM-FAILED")]
        return failed[-1][len("RDPM-FAILED"):].strip() if failed else None

    @property
    def step(self) -> tuple[int, int, str] | None:
        for mark in reversed(self.marks):
            m = _STEP_RE.match(mark)
            if m:
                return int(m.group(1)), int(m.group(2)), m.group(3)
        return None


def parse_status(out: str) -> InstallStatus:
    unit = "unknown"
    head, _, rest = out.partition("RDPM_MARKS_BEGIN")
    for line in head.splitlines():
        if line.startswith("RDPM_UNIT "):
            unit = (line.split() + ["unknown"])[1]
    marks_text, _, tail = rest.partition("RDPM_TAIL_BEGIN")
    marks = [m.strip() for m in marks_text.splitlines() if m.strip().startswith("RDPM-")]
    return InstallStatus(unit, marks, tail.strip())
