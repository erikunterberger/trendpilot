import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src import daily_runner as dr

MON_8AM = datetime(2026, 11, 23, 8, 0)  # a Monday
TODAY = MON_8AM.date().isoformat()


class Decide(unittest.TestCase):
    def test_first_weekday_attempt_runs(self):
        self.assertTrue(dr.decide(MON_8AM, {}, report_done=False)[0])

    def test_yesterdays_state_does_not_block_today(self):
        state = {"date": "2026-11-20", "status": "ok", "attempts": 1}
        self.assertTrue(dr.decide(MON_8AM, state, report_done=False)[0])

    def test_weekend_skips(self):
        sat = datetime(2026, 11, 28, 9, 0)
        self.assertEqual(dr.decide(sat, {}, False), (False, "weekend"))

    def test_too_early_skips(self):
        self.assertFalse(dr.decide(datetime(2026, 11, 23, 2, 0), {}, False)[0])

    def test_existing_report_counts_as_done(self):
        should, reason = dr.decide(MON_8AM, {}, report_done=True)
        self.assertFalse(should)
        self.assertIn("report exists", reason)

    def test_ok_today_skips(self):
        state = {"date": TODAY, "status": "ok", "attempts": 1}
        self.assertFalse(dr.decide(MON_8AM, state, False)[0])

    def test_recent_running_skips_but_stale_running_retries(self):
        running = {"date": TODAY, "status": "running", "attempts": 1, "started": "2026-11-23T07:30:00"}
        self.assertFalse(dr.decide(MON_8AM, running, False)[0])
        later = datetime(2026, 11, 23, 9, 0)
        self.assertTrue(dr.decide(later, running, False)[0])

    def test_failures_retry_until_max_attempts(self):
        failed = {"date": TODAY, "status": "failed", "attempts": 1}
        self.assertTrue(dr.decide(MON_8AM, failed, False)[0])
        failed["attempts"] = dr.MAX_ATTEMPTS
        self.assertFalse(dr.decide(MON_8AM, failed, False)[0])

    def test_quota_error_is_not_retried(self):
        state = {"date": TODAY, "status": "quota", "attempts": 1}
        self.assertFalse(dr.decide(MON_8AM, state, False)[0])

    def test_force_overrides_everything(self):
        sat = datetime(2026, 11, 28, 3, 0)
        state = {"date": "2026-11-28", "status": "ok"}
        self.assertTrue(dr.decide(sat, state, report_done=True, force=True)[0])


class StateFile(unittest.TestCase):
    def test_round_trip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            self.assertEqual(dr.load_state(p), {})
            dr.save_state({"date": TODAY, "status": "ok"}, p)
            self.assertEqual(dr.load_state(p)["status"], "ok")
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(dr.load_state(p), {})

    def test_report_exists(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(dr.report_exists(TODAY, Path(d)))
            (Path(d) / f"{TODAY}-report.md").write_text("x", encoding="utf-8")
            self.assertTrue(dr.report_exists(TODAY, Path(d)))


if __name__ == "__main__":
    unittest.main()
