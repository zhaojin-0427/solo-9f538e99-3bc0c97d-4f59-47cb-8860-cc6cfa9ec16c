"""Application service: imports, release publication and budget accounting."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Callable, Iterable

from .db import Database
from .errors import (
    BudgetExhaustedError,
    DuplicatePatientError,
    ImportNotFoundError,
    PublicationNotFoundError,
    ReconciliationClosedError,
    ReconciliationDateOutsideWeekError,
    ReconciliationIncompleteError,
    ReconciliationTotalMismatchError,
    StorageError,
    WeekAlreadyImportedError,
)
from .groups import AGE_BANDS, COST_PER_PUBLICATION, EPSILON, STREETS
from .noise import noisy_count
from .schemas import CountCell, PatientRecordIn

# Optional test hook invoked after all publication rows are written but before
# COMMIT, letting tests prove that a failing write leaves nothing behind.
BeforeCommitHook = Callable[[int], None]

# Optional test hook invoked inside a reconciliation write transaction, after
# the daily total has been upserted but before COMMIT.
ReconciliationHook = Callable[[int, date], None]

_WEEK_PATTERN = re.compile(r"^(\d{4})-W(\d{2})$")


def iso_week_dates(week: str) -> list[date]:
    """Return the seven calendar days (Monday..Sunday) of an ISO week label."""

    match = _WEEK_PATTERN.fullmatch(week)
    # Import requests already validate the label; keep a defensive guard.
    if match is None:
        raise ValueError("week must use the ISO form YYYY-Www")
    year, week_number = int(match.group(1)), int(match.group(2))
    return [
        date.fromisocalendar(year, week_number, weekday) for weekday in range(1, 8)
    ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReleaseService:
    """All state transitions used by the HTTP layer.

    Every public method is safe to call concurrently from multiple threads or
    processes sharing the same SQLite file.
    """

    def __init__(
        self,
        db: Database,
        total_budget: int,
        cost_per_publication: int = COST_PER_PUBLICATION,
        epsilon: float = EPSILON,
        before_commit: BeforeCommitHook | None = None,
        reconciliation_before_commit: ReconciliationHook | None = None,
    ) -> None:
        if total_budget < 0:
            raise ValueError("total_budget must be non-negative")
        if cost_per_publication <= 0:
            raise ValueError("cost_per_publication must be positive")
        if epsilon <= 0:
            raise ValueError("epsilon must be positive")
        self.db = db
        self.total_budget = total_budget
        self.cost_per_publication = cost_per_publication
        self.epsilon = epsilon
        self._before_commit = before_commit
        self._reconciliation_before_commit = reconciliation_before_commit

    # ------------------------------------------------------------------ #
    # Imports
    # ------------------------------------------------------------------ #
    def import_week(self, week: str, records: Iterable[PatientRecordIn]) -> dict:
        """Validate a week's records atomically and store an immutable version.

        If any de-identified patient number occurs more than once in the batch,
        the entire batch is rejected and nothing is written.  Raw identifiers
        are never persisted and are never echoed in error messages.
        """

        counts: dict[tuple[str, str], int] = defaultdict(int)
        seen: set[str] = set()
        record_count = 0

        for record in records:
            if record.patient_id in seen:
                raise DuplicatePatientError(
                    "the same de-identified patient number appears more than "
                    "once in this week; the entire batch was rejected"
                )
            seen.add(record.patient_id)
            counts[(record.street, record.age_band)] += 1
            record_count += 1

        if record_count == 0:
            # Unreachable through the HTTP schema; keep the service honest.
            raise ValueError("a week import must contain at least one record")

        try:
            with self.db.transaction() as conn:
                existing = conn.execute(
                    "SELECT version FROM imports WHERE week = ?", (week,)
                ).fetchone()
                if existing is not None:
                    raise WeekAlreadyImportedError(
                        f"week {week} has already been imported as version "
                        f"{existing['version']}; imported versions are immutable"
                    )

                cursor = conn.execute(
                    "INSERT INTO imports (week, record_count, created_at) "
                    "VALUES (?, ?, ?)",
                    (week, record_count, _now()),
                )
                version = int(cursor.lastrowid)

                conn.executemany(
                    "INSERT INTO import_cells "
                    "(import_version, street, age_band, true_count) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (version, street, age_band, counts.get((street, age_band), 0))
                        for street in STREETS
                        for age_band in AGE_BANDS
                    ],
                )
        except sqlite3.Error as exc:
            raise StorageError(
                "the import could not be stored; no data was written"
            ) from exc

        return {"version": version, "week": week, "record_count": record_count}

    def get_import(self, version: int) -> dict:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT version, week, record_count FROM imports WHERE version = ?",
                (version,),
            ).fetchone()
        if row is None:
            raise ImportNotFoundError(f"import version {version} does not exist")
        return {
            "version": int(row["version"]),
            "week": row["week"],
            "record_count": int(row["record_count"]),
        }

    # ------------------------------------------------------------------ #
    # Budget
    # ------------------------------------------------------------------ #
    @staticmethod
    def _budget_from(conn: sqlite3.Connection) -> tuple[int, int, int]:
        row = conn.execute(
            "SELECT total_budget, spent FROM budget_state WHERE id = 1"
        ).fetchone()
        if row is None:
            raise StorageError("budget state is missing")
        total, spent = int(row["total_budget"]), int(row["spent"])
        return total, spent, total - spent

    def budget(self) -> dict:
        with self.db.read() as conn:
            total, spent, remaining = self._budget_from(conn)
        return {
            "total_budget": total,
            "spent": spent,
            "remaining_budget": remaining,
        }

    # ------------------------------------------------------------------ #
    # Publications
    # ------------------------------------------------------------------ #
    def publish(self, import_version: int) -> dict:
        """Publish (or re-fetch) the noisy release for an imported week.

        * Every fixed street/age-band cell is released, including zero counts.
        * Each cell gets Laplace(0, 1/epsilon) noise; epsilon=1, sensitivity=1.
        * The first publication of a version spends one budget unit.  A repeat
          request returns the stored release and spends nothing.
        * The budget charge and the release rows are written in one
          ``BEGIN IMMEDIATE`` transaction, so an exhausted budget or a failed
          write leaves neither a publication nor a spent budget unit behind.
        """

        # Cheap read-only replay path: no write lock, no budget movement.
        replay = self._existing_publication(import_version)
        if replay is not None:
            return replay

        try:
            with self.db.transaction() as conn:
                # Re-check under the write lock; another request may have just
                # created this release.
                existing = conn.execute(
                    "SELECT version FROM publications WHERE import_version = ?",
                    (import_version,),
                ).fetchone()
                if existing is not None:
                    total, spent, _ = self._budget_from(conn)
                    version = int(existing["version"])
                    return {
                        "version": version,
                        "counts": self._cells_from(conn, version),
                        "remaining_budget": total - spent,
                    }

                imported = conn.execute(
                    "SELECT version, week, record_count FROM imports "
                    "WHERE version = ?",
                    (import_version,),
                ).fetchone()
                if imported is None:
                    raise ImportNotFoundError(
                        f"import version {import_version} does not exist"
                    )

                # First-publication gate: the seven daily outpatient totals
                # must all be registered and add up to the imported headcount.
                self._assert_reconciliation_ready(
                    conn,
                    import_version,
                    str(imported["week"]),
                    int(imported["record_count"]),
                )

                _, _, remaining = self._budget_from(conn)
                if remaining < self.cost_per_publication:
                    raise BudgetExhaustedError(
                        "the global privacy budget is exhausted; no publication "
                        "was created and no budget was spent"
                    )

                cursor = conn.execute(
                    "INSERT INTO publications "
                    "(import_version, epsilon, cost, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (import_version, self.epsilon, self.cost_per_publication, _now()),
                )
                publication_version = int(cursor.lastrowid)

                true_by_cell = {
                    (row["street"], row["age_band"]): int(row["true_count"])
                    for row in conn.execute(
                        "SELECT street, age_band, true_count FROM import_cells "
                        "WHERE import_version = ?",
                        (import_version,),
                    ).fetchall()
                }

                conn.executemany(
                    "INSERT INTO publication_cells "
                    "(publication_version, street, age_band, noisy_count) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (
                            publication_version,
                            street,
                            age_band,
                            noisy_count(
                                true_by_cell.get((street, age_band), 0), self.epsilon
                            ),
                        )
                        for street in STREETS
                        for age_band in AGE_BANDS
                    ],
                )

                # Conditional update: a second safety net against overspend.
                charged = conn.execute(
                    "UPDATE budget_state SET spent = spent + ? "
                    "WHERE id = 1 AND spent + ? <= total_budget",
                    (self.cost_per_publication, self.cost_per_publication),
                )
                if charged.rowcount != 1:
                    raise BudgetExhaustedError(
                        "the global privacy budget is exhausted; no publication "
                        "was created and no budget was spent"
                    )

                if self._before_commit is not None:
                    self._before_commit(import_version)

                return {
                    "version": publication_version,
                    "counts": self._cells_from(conn, publication_version),
                    "remaining_budget": remaining - self.cost_per_publication,
                }
        except sqlite3.Error as exc:
            raise StorageError(
                "the publication could not be stored; no release was created "
                "and no budget was spent"
            ) from exc

    def get_publication(self, version: int) -> dict:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT version FROM publications WHERE version = ?", (version,)
            ).fetchone()
            if row is None:
                raise PublicationNotFoundError(
                    f"publication version {version} does not exist"
                )
            total, spent, _ = self._budget_from(conn)
            return {
                "version": version,
                "counts": self._cells_from(conn, version),
                "remaining_budget": total - spent,
            }

    def list_publications(self) -> dict:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT version, import_version FROM publications ORDER BY version"
            ).fetchall()
            total, spent, _ = self._budget_from(conn)
            return {
                "publications": [
                    {
                        "version": int(row["version"]),
                        "import_version": int(row["import_version"]),
                        "counts": self._cells_from(conn, int(row["version"])),
                    }
                    for row in rows
                ],
                "remaining_budget": total - spent,
            }

    # ------------------------------------------------------------------ #
    # Reconciliation
    # ------------------------------------------------------------------ #
    def register_daily_total(
        self, import_version: int, day: date, total: int
    ) -> dict:
        """Register (or correct) one day's outpatient total for a version.

        * ``day`` must be one of the seven ISO-week days of the import.
        * A correction for an already-registered day overwrites the old value.
        * Once the version has been published, corrections are rejected:
          publication and correction are serialized by ``BEGIN IMMEDIATE``,
          so whichever transaction commits first decides the outcome.
        * Any write failure rolls the transaction back, leaving the previous
          reconciliation state untouched.
        """

        try:
            with self.db.transaction() as conn:
                imported = conn.execute(
                    "SELECT week FROM imports WHERE version = ?",
                    (import_version,),
                ).fetchone()
                if imported is None:
                    raise ImportNotFoundError(
                        f"import version {import_version} does not exist"
                    )

                week_days = iso_week_dates(str(imported["week"]))
                if day not in week_days:
                    raise ReconciliationDateOutsideWeekError(
                        f"day {day.isoformat()} does not belong to ISO week "
                        f"{imported['week']} of import version {import_version}"
                    )

                published = conn.execute(
                    "SELECT 1 FROM publications WHERE import_version = ?",
                    (import_version,),
                ).fetchone()
                if published is not None:
                    raise ReconciliationClosedError(
                        f"import version {import_version} has already been "
                        "published; its reconciliation can no longer be changed"
                    )

                conn.execute(
                    "INSERT INTO daily_totals "
                    "(import_version, day, total, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (import_version, day) DO UPDATE SET "
                    "total = excluded.total, updated_at = excluded.updated_at",
                    (import_version, day.isoformat(), total, _now()),
                )

                if self._reconciliation_before_commit is not None:
                    self._reconciliation_before_commit(import_version, day)

                state = self._reconciliation_state(conn, import_version)
        except sqlite3.Error as exc:
            raise StorageError(
                "the reconciliation write failed; the previous reconciliation "
                "state was preserved"
            ) from exc
        return state

    def get_reconciliation(self, import_version: int) -> dict:
        with self.db.read() as conn:
            imported = conn.execute(
                "SELECT week, record_count FROM imports WHERE version = ?",
                (import_version,),
            ).fetchone()
            if imported is None:
                raise ImportNotFoundError(
                    f"import version {import_version} does not exist"
                )
            return self._reconciliation_state(conn, import_version)

    @staticmethod
    def _assert_reconciliation_ready(
        conn: sqlite3.Connection,
        import_version: int,
        week: str,
        record_count: int,
    ) -> None:
        """Gate a first publication on a complete, consistent reconciliation."""

        rows = conn.execute(
            "SELECT day, total FROM daily_totals WHERE import_version = ?",
            (import_version,),
        ).fetchall()
        registered = {row["day"]: int(row["total"]) for row in rows}
        week_days = iso_week_dates(week)
        missing = [day for day in week_days if day.isoformat() not in registered]
        if missing:
            formatted = ", ".join(day.isoformat() for day in missing)
            raise ReconciliationIncompleteError(
                "all seven daily outpatient totals must be registered before "
                f"the first publication; missing day(s): {formatted}"
            )
        reported_total = sum(registered.values())
        if reported_total != record_count:
            raise ReconciliationTotalMismatchError(
                f"the seven-day total ({reported_total}) does not match the "
                f"imported record count ({record_count}); correct the daily "
                "totals before publication"
            )

    def _reconciliation_state(
        self, conn: sqlite3.Connection, import_version: int
    ) -> dict:
        imported = conn.execute(
            "SELECT week, record_count FROM imports WHERE version = ?",
            (import_version,),
        ).fetchone()
        if imported is None:  # defensive; callers check first
            raise ImportNotFoundError(
                f"import version {import_version} does not exist"
            )
        week = str(imported["week"])
        record_count = int(imported["record_count"])
        rows = conn.execute(
            "SELECT day, total FROM daily_totals WHERE import_version = ?",
            (import_version,),
        ).fetchall()
        registered = {row["day"]: int(row["total"]) for row in rows}

        week_days = iso_week_dates(week)
        days = [
            {"day": day, "total": registered.get(day.isoformat())}
            for day in week_days
        ]
        missing = [day for day in week_days if day.isoformat() not in registered]
        reported_total = sum(registered.values())
        complete = not missing
        total_matches = complete and reported_total == record_count
        published = (
            conn.execute(
                "SELECT 1 FROM publications WHERE import_version = ?",
                (import_version,),
            ).fetchone()
            is not None
        )
        return {
            "import_version": import_version,
            "week": week,
            "days": days,
            "missing_days": missing,
            "reported_total": reported_total,
            "import_record_count": record_count,
            "total_matches_import": total_matches,
            "ready_to_publish": total_matches,
            "published": published,
        }

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _existing_publication(self, import_version: int) -> dict | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT version FROM publications WHERE import_version = ?",
                (import_version,),
            ).fetchone()
            if row is None:
                return None
            total, spent, _ = self._budget_from(conn)
            version = int(row["version"])
            return {
                "version": version,
                "counts": self._cells_from(conn, version),
                "remaining_budget": total - spent,
            }

    @staticmethod
    def _cells_from(
        conn: sqlite3.Connection, publication_version: int
    ) -> list[CountCell]:
        rows = conn.execute(
            "SELECT street, age_band, noisy_count FROM publication_cells "
            "WHERE publication_version = ?",
            (publication_version,),
        ).fetchall()
        by_cell = {
            (row["street"], row["age_band"]): int(row["noisy_count"]) for row in rows
        }
        return [
            CountCell(
                street=street,
                age_band=age_band,
                count=by_cell.get((street, age_band), 0),
            )
            for street in STREETS
            for age_band in AGE_BANDS
        ]
