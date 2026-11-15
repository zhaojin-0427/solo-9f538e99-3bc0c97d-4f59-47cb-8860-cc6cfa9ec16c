"""End-to-end tests for the per-version daily reconciliation API."""

from __future__ import annotations

import sqlite3
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import iso_week_dates
from tests.test_api import WEEK, import_week, make_records, reconcile_week, week_day


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture()
def client(db_path):
    app = create_app(database_path=db_path, total_budget=3)
    with TestClient(app) as c:
        yield c


def reconciliation_url(version: int, day: str) -> str:
    return f"/v1/imports/{version}/reconciliation/days/{day}"


class TestReconciliationRegistration:
    def test_empty_state_lists_all_seven_days_as_missing(self, client):
        import_week(client)
        body = client.get("/v1/imports/1/reconciliation").json()

        days = iso_week_dates(WEEK)
        assert body["import_version"] == 1
        assert body["week"] == WEEK
        assert body["published"] is False
        assert body["import_record_count"] == 5
        assert body["reported_total"] == 0
        assert body["total_matches_import"] is False
        assert body["ready_to_publish"] is False
        assert [entry["day"] for entry in body["days"]] == [
            day.isoformat() for day in days
        ]
        assert all(entry["total"] is None for entry in body["days"])
        assert body["missing_days"] == [day.isoformat() for day in days]

    def test_registering_days_shrinks_missing_list(self, client):
        import_week(client)
        monday = week_day(index=0)

        response = client.put(reconciliation_url(1, monday), json={"total": 5})
        assert response.status_code == 200
        body = response.json()
        assert body["days"][0] == {"day": monday, "total": 5}
        assert len(body["missing_days"]) == 6
        assert monday not in body["missing_days"]
        assert body["reported_total"] == 5
        # One day alone can never make the week complete.
        assert body["ready_to_publish"] is False

    def test_same_day_correction_overwrites_old_value(self, client):
        import_week(client)
        monday = week_day(index=0)
        client.put(reconciliation_url(1, monday), json={"total": 4})
        corrected = client.put(reconciliation_url(1, monday), json={"total": 5})

        assert corrected.status_code == 200
        body = corrected.json()
        assert body["days"][0]["total"] == 5
        assert body["reported_total"] == 5
        assert len(body["missing_days"]) == 6

    def test_day_outside_import_week_is_rejected(self, client):
        import_week(client)
        outside = (iso_week_dates(WEEK)[-1] + timedelta(days=1)).isoformat()

        response = client.put(reconciliation_url(1, outside), json={"total": 1})
        assert response.status_code == 422
        assert response.json()["error"] == "reconciliation_day_outside_week"

        # State unchanged.
        body = client.get("/v1/imports/1/reconciliation").json()
        assert len(body["missing_days"]) == 7
        assert body["reported_total"] == 0

    def test_malformed_day_and_negative_total_are_validation_errors(self, client):
        import_week(client)
        bad_day = client.put(
            "/v1/imports/1/reconciliation/days/not-a-date", json={"total": 1}
        )
        assert bad_day.status_code == 422

        negative = client.put(
            reconciliation_url(1, week_day()), json={"total": -1}
        )
        assert negative.status_code == 422

        extra = client.put(
            reconciliation_url(1, week_day()),
            json={"total": 1, "source": "manual"},
        )
        assert extra.status_code == 422

    def test_unknown_import_returns_404(self, client):
        assert client.get("/v1/imports/99/reconciliation").status_code == 404
        response = client.put(
            reconciliation_url(99, week_day()), json={"total": 1}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "import_version_not_found"

    def test_iso_week_boundary_year_2020_w53(self, client):
        # 2020-W53 runs Monday 2020-12-28 .. Sunday 2021-01-03.
        import_week(client, week="2020-W53", records=make_records(1))
        body = client.get("/v1/imports/1/reconciliation").json()
        assert [entry["day"] for entry in body["days"]] == [
            "2020-12-28",
            "2020-12-29",
            "2020-12-30",
            "2020-12-31",
            "2021-01-01",
            "2021-01-02",
            "2021-01-03",
        ]
        # 2021-01-04 belongs to 2021-W01, not 2020-W53.
        response = client.put(
            reconciliation_url(1, "2021-01-04"), json={"total": 1}
        )
        assert response.status_code == 422


class TestReconciliationReadiness:
    def test_ready_when_seven_days_present_and_total_matches(self, client):
        import_week(client)
        body = reconcile_week(client, version=1, total=5)

        assert body["missing_days"] == []
        assert body["reported_total"] == 5
        assert body["import_record_count"] == 5
        assert body["total_matches_import"] is True
        assert body["ready_to_publish"] is True

    def test_total_mismatch_keeps_not_ready(self, client):
        import_week(client)
        body = reconcile_week(client, version=1, total=4)

        assert body["missing_days"] == []
        assert body["reported_total"] == 4
        assert body["total_matches_import"] is False
        assert body["ready_to_publish"] is False

    def test_partial_registration_keeps_not_ready_even_if_sum_matches(self, client):
        import_week(client)
        # Sum already equals the import size, but six days are still missing.
        client.put(reconciliation_url(1, week_day(index=0)), json={"total": 5})
        body = client.get("/v1/imports/1/reconciliation").json()
        assert body["reported_total"] == 5
        assert body["total_matches_import"] is False
        assert body["ready_to_publish"] is False


class TestPublicationGate:
    def test_publish_blocked_while_days_are_missing(self, client):
        import_week(client)
        client.put(reconciliation_url(1, week_day()), json={"total": 5})

        response = client.post("/v1/publications", json={"import_version": 1})
        assert response.status_code == 409
        assert response.json()["error"] == "reconciliation_incomplete"
        assert client.get("/v1/budget").json()["spent"] == 0
        assert client.get("/v1/publications/1").status_code == 404

    def test_publish_blocked_when_total_mismatches(self, client):
        import_week(client)
        reconcile_week(client, version=1, total=4)

        response = client.post("/v1/publications", json={"import_version": 1})
        assert response.status_code == 409
        assert response.json()["error"] == "reconciliation_total_mismatch"
        assert "4" in response.json()["detail"]
        assert "5" in response.json()["detail"]
        assert client.get("/v1/budget").json()["spent"] == 0
        assert client.get("/v1/publications/1").status_code == 404

    def test_publish_succeeds_once_reconciliation_is_ready(self, client):
        import_week(client)
        reconcile_week(client, version=1)

        response = client.post("/v1/publications", json={"import_version": 1})
        assert response.status_code == 200
        state = client.get("/v1/imports/1/reconciliation").json()
        assert state["published"] is True
        assert state["ready_to_publish"] is True

    def test_correction_after_publication_is_rejected(self, client):
        import_week(client)
        reconcile_week(client, version=1)
        client.post("/v1/publications", json={"import_version": 1})

        response = client.put(
            reconciliation_url(1, week_day()), json={"total": 2}
        )
        assert response.status_code == 409
        assert response.json()["error"] == "reconciliation_closed"

        # The stored total is untouched.
        body = client.get("/v1/imports/1/reconciliation").json()
        assert body["days"][0]["total"] == 5
        assert body["published"] is True

    def test_replay_after_publication_is_unchanged_and_free(self, client):
        import_week(client)
        reconcile_week(client, version=1)
        first = client.post(
            "/v1/publications", json={"import_version": 1}
        ).json()
        second = client.post(
            "/v1/publications", json={"import_version": 1}
        ).json()
        assert second == first
        assert client.get("/v1/budget").json()["spent"] == 1

    def test_gate_failure_does_not_consume_budget_or_close_reconciliation(self, client):
        import_week(client)
        reconcile_week(client, version=1, total=4)

        blocked = client.post("/v1/publications", json={"import_version": 1})
        assert blocked.status_code == 409

        # Correct the total, then publication goes through.
        client.put(reconciliation_url(1, week_day()), json={"total": 5})
        ok = client.post("/v1/publications", json={"import_version": 1})
        assert ok.status_code == 200
        assert ok.json()["remaining_budget"] == 2


class TestReconciliationRollback:
    def test_failed_write_preserves_previous_reconciliation_state(self, db_path):
        def explode(_version: int, _day) -> None:
            raise sqlite3.OperationalError("simulated disk failure")

        app = create_app(
            database_path=db_path,
            total_budget=3,
            reconciliation_before_commit=explode,
        )
        with TestClient(app) as c:
            import_week(c)
            first = c.put(
                reconciliation_url(1, week_day()), json={"total": 3}
            )
            assert first.status_code == 503
            assert first.json()["error"] == "storage_write_failed"

            state = c.get("/v1/imports/1/reconciliation").json()
            assert state["missing_days"] == [
                day.isoformat() for day in iso_week_dates(WEEK)
            ]
            assert state["reported_total"] == 0

        # A failing correction must not overwrite a previously stored total.
        healthy = create_app(database_path=db_path, total_budget=3)
        with TestClient(healthy) as c:
            ok = c.put(reconciliation_url(1, week_day()), json={"total": 3})
            assert ok.status_code == 200

        broken = create_app(
            database_path=db_path,
            total_budget=3,
            reconciliation_before_commit=explode,
        )
        with TestClient(broken) as c:
            failed = c.put(
                reconciliation_url(1, week_day()), json={"total": 99}
            )
            assert failed.status_code == 503
            state = c.get("/v1/imports/1/reconciliation").json()
            assert state["days"][0]["total"] == 3
            assert state["reported_total"] == 3

        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM daily_totals"
            ).fetchone()[0] == 1


class TestReconciliationConcurrency:
    def _ready_client(self, db_path: str):
        app = create_app(database_path=db_path, total_budget=3)
        client = TestClient(app)
        client.__enter__()
        import_week(client)
        reconcile_week(client, version=1, total=5)
        return client

    def test_correction_and_publication_serialize_by_commit_order(
        self, tmp_path
    ):
        # Every interleaving must end in one of two serial outcomes:
        #   * correction commits first -> publish re-checks against the new
        #     total and rejects it (mismatch); the correction is visible;
        #   * publication commits first -> the correction is rejected as
        #     closed; the release exists and no budget overspend happens.
        for iteration in range(8):
            iter_db = str(tmp_path / f"iter-{iteration}.db")
            client = self._ready_client(iter_db)
            barrier = threading.Barrier(2)
            outcomes: list[tuple[str, int]] = []
            lock = threading.Lock()

            def correct() -> None:
                barrier.wait()
                response = client.put(
                    reconciliation_url(1, week_day()), json={"total": 2}
                )
                with lock:
                    outcomes.append(("correction", response.status_code))

            def publish() -> None:
                barrier.wait()
                response = client.post(
                    "/v1/publications", json={"import_version": 1}
                )
                with lock:
                    outcomes.append(("publication", response.status_code))

            threads = [
                threading.Thread(target=correct),
                threading.Thread(target=publish),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            statuses = dict(outcomes)
            try:
                if statuses["correction"] == 200:
                    # Correction landed first: publication must see new total.
                    assert statuses["publication"] == 409
                    state = client.get(
                        "/v1/imports/1/reconciliation"
                    ).json()
                    assert state["days"][0]["total"] == 2
                    assert state["published"] is False
                    assert client.get("/v1/budget").json()["spent"] == 0
                else:
                    # Publication landed first: the correction is rejected.
                    assert statuses["correction"] == 409
                    assert statuses["publication"] == 200
                    state = client.get(
                        "/v1/imports/1/reconciliation"
                    ).json()
                    assert state["published"] is True
                    assert state["days"][0]["total"] == 5
                    assert client.get("/v1/budget").json()["spent"] == 1
            finally:
                client.__exit__(None, None, None)

    def test_correction_first_then_publish_checks_against_new_numbers(
        self, tmp_path
    ):
        iter_db = str(tmp_path / "correction-first.db")
        client = self._ready_client(iter_db)
        try:
            # Commit a correction first: the now-inconsistent reconciliation
            # blocks the publication until corrected again.
            assert client.put(
                reconciliation_url(1, week_day()), json={"total": 2}
            ).status_code == 200
            blocked = client.post(
                "/v1/publications", json={"import_version": 1}
            )
            assert blocked.status_code == 409
            assert blocked.json()["error"] == "reconciliation_total_mismatch"

            client.put(reconciliation_url(1, week_day()), json={"total": 5})
            assert client.post(
                "/v1/publications", json={"import_version": 1}
            ).status_code == 200
        finally:
            client.__exit__(None, None, None)

    def test_publish_first_then_correction_is_refused(self, tmp_path):
        iter_db = str(tmp_path / "publish-first.db")
        client = self._ready_client(iter_db)
        try:
            assert client.post(
                "/v1/publications", json={"import_version": 1}
            ).status_code == 200
            refused = client.put(
                reconciliation_url(1, week_day()), json={"total": 2}
            )
            assert refused.status_code == 409
            assert refused.json()["error"] == "reconciliation_closed"
        finally:
            client.__exit__(None, None, None)
