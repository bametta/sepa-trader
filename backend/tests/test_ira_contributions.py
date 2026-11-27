"""Roth IRA contribution limits, phase-out, deadlines and ledger validation."""
import types
import unittest
from datetime import date
from unittest.mock import MagicMock

from tests._loader import load, sql_stub

SETTINGS = {}
fake_db = types.ModuleType("app.database")
fake_db.get_user_setting = lambda db, k, d="", u=None: SETTINGS.get(k, d)
ic = load("ira_contributions", {**sql_stub(), "app.database": fake_db})


class LimitTests(unittest.TestCase):
    def test_catch_up_depends_on_age_at_year_end(self):
        self.assertEqual(ic.limit_for(2026, 1976)["limit"], 7500 + 1100)   # turns 50 in 2026
        self.assertEqual(ic.limit_for(2026, 1977)["limit"], 7500)          # turns 49
        self.assertEqual(ic.limit_for(2025, 1960)["limit"], 7000 + 1000)

    def test_unknown_birth_year_gets_no_catch_up(self):
        self.assertEqual(ic.limit_for(2026, None)["limit"], 7500)

    def test_future_year_uses_latest_known_limit_and_is_flagged(self):
        lim = ic.limit_for(2031, 1990)
        self.assertEqual(lim["limit"], 7500)
        self.assertTrue(lim["estimated"])
        self.assertFalse(ic.limit_for(2026, 1990)["estimated"])


class PhaseOutTests(unittest.TestCase):
    def test_below_start_full_limit(self):
        self.assertEqual(ic.phaseout_limit(7500, 100000, "single", 2026)[0], 7500)

    def test_above_end_zero(self):
        allowed, note = ic.phaseout_limit(7500, 200000, "single", 2026)
        self.assertEqual(allowed, 0.0)
        self.assertIn("not allowed", note)

    def test_midpoint_is_half_rounded_up_to_ten(self):
        allowed, _ = ic.phaseout_limit(7500, 160500, "single", 2026)   # exactly halfway through 153k-168k
        self.assertEqual(allowed, 3750)

    def test_reduction_rounds_up_to_next_ten(self):
        allowed, _ = ic.phaseout_limit(7500, 155000, "single", 2026)
        self.assertEqual(allowed % 10, 0)
        self.assertGreater(allowed, 7500 * (168000 - 155000) / 15000 - 1)

    def test_two_hundred_dollar_floor_while_in_range(self):
        self.assertEqual(ic.phaseout_limit(7500, 167900, "single", 2026)[0], 200)

    def test_joint_thresholds_differ(self):
        self.assertEqual(ic.phaseout_limit(7500, 200000, "joint", 2026)[0], 7500)
        self.assertEqual(ic.phaseout_limit(7500, 250000, "joint", 2026)[0], 1500)  # 20% of the 242k-252k range left

    def test_missing_inputs_do_not_reduce(self):
        self.assertEqual(ic.phaseout_limit(7500, None, "single", 2026)[0], 7500)
        self.assertEqual(ic.phaseout_limit(7500, 160000, None, 2026)[0], 7500)


class DeadlineTests(unittest.TestCase):
    def test_prior_year_open_until_april_15(self):
        self.assertEqual(ic.open_tax_years(date(2026, 1, 10)), [2026, 2025])
        self.assertEqual(ic.open_tax_years(date(2026, 4, 15)), [2026, 2025])
        self.assertEqual(ic.open_tax_years(date(2026, 4, 16)), [2026])

    def test_deadline_is_april_15_of_following_year(self):
        self.assertEqual(ic.contribution_deadline(2026), date(2027, 4, 15))


class SummaryTests(unittest.TestCase):
    def test_remaining_and_pace(self):
        s = ic.summarize_year(2026, date(2026, 6, 1), 1990, None, None, 2000.0)
        self.assertEqual(s["limit"], 7500)
        self.assertEqual(s["remaining"], 5500)
        self.assertEqual(s["excess"], 0)
        self.assertNotIn("warning", s)
        self.assertGreater(s["monthly_to_fill_by_year_end"], 0)
        self.assertLess(s["monthly_to_fill_by_deadline"], s["monthly_to_fill_by_year_end"])

    def test_excess_is_flagged_with_penalty_estimate(self):
        s = ic.summarize_year(2026, date(2026, 6, 1), 1990, None, None, 8000.0)
        self.assertEqual(s["excess"], 500)
        self.assertEqual(s["excess_tax_estimate"], 30.0)
        self.assertIn("6%", s["warning"])
        self.assertEqual(s["remaining"], 0)

    def test_phase_out_reduces_the_allowed_amount_and_creates_excess(self):
        s = ic.summarize_year(2026, date(2026, 6, 1), 1990, 160500.0, "single", 5000.0)
        self.assertEqual(s["allowed"], 3750)
        self.assertEqual(s["excess"], 1250)

    def test_exactly_at_limit_is_not_excess(self):
        s = ic.summarize_year(2026, date(2026, 6, 1), 1990, None, None, 7500.0)
        self.assertEqual((s["excess"], s["remaining"]), (0, 0))


class PaceTests(unittest.TestCase):
    def year(self, today, contributed, **kw):
        return ic.summarize_year(today.year, today, 1990, None, None, contributed)

    def test_on_pace_behind_and_maxed(self):
        mid = date(2026, 7, 2)                                   # ~half the year gone: expect ~$3,750
        self.assertEqual(self.year(mid, 4000)["pace"], "on_pace")
        behind = self.year(mid, 1000)
        self.assertEqual(behind["pace"], "behind")
        self.assertAlmostEqual(behind["behind_by"], 7500 * ((mid - date(2026, 1, 1)).days + 1) / 365 - 1000, places=1)
        self.assertEqual(self.year(mid, 7500)["pace"], "maxed")

    def test_front_loading_counts_as_on_pace(self):
        self.assertEqual(self.year(date(2026, 2, 1), 7000)["pace"], "on_pace")

    def test_pace_only_reported_for_the_current_year(self):
        prior = ic.summarize_year(2025, date(2026, 2, 1), 1990, None, None, 0.0)
        self.assertNotIn("pace", prior)


class ReminderTests(unittest.TestCase):
    def summary(self, today, contributed_cur, contributed_prior=7000.0):
        years = [ic.summarize_year(y, today, 1990, None, None, c)
                 for y, c in ((today.year, contributed_cur), (today.year - 1, contributed_prior))
                 if y in ic.open_tax_years(today)]
        return {"years": years}

    def test_nothing_to_say_when_everything_is_maxed(self):
        today = date(2026, 6, 1)
        self.assertIsNone(ic.reminder_text(self.summary(today, 7500), today))

    def test_reports_room_pace_and_monthly_amount(self):
        today = date(2026, 6, 1)
        msg = ic.reminder_text(self.summary(today, 2000), today)
        self.assertIn("2026: $2,000 of $7,500", msg)
        self.assertIn("$5,500 left", msg)
        self.assertIn("/month fills it by Dec 31", msg)
        self.assertIn("behind pace", msg)

    def test_prior_year_included_until_april_15(self):
        early = date(2026, 2, 1)
        msg = ic.reminder_text(self.summary(early, 0, contributed_prior=3000.0), early)
        self.assertIn("2025: $3,000 of $7,000", msg)
        self.assertIn("deadline 2026-04-15", msg)
        late = date(2026, 5, 1)
        self.assertNotIn("2025", ic.reminder_text(self.summary(late, 0), late))

    def test_summary_exposes_the_max_every_year_flag(self):
        SETTINGS.clear()
        SETTINGS.update(ira_birth_year="1990", ira_contribute_max="true")
        orig = ic.list_contributions
        ic.list_contributions = lambda db, uid: []
        try:
            self.assertTrue(ic.summary(MagicMock(), 1, today=date(2026, 3, 1))["max_every_year"])
            SETTINGS["ira_contribute_max"] = "false"
            self.assertFalse(ic.summary(MagicMock(), 1, today=date(2026, 3, 1))["max_every_year"])
        finally:
            ic.list_contributions = orig


class ValidationTests(unittest.TestCase):
    today = date(2026, 3, 1)

    def bad(self, *a):
        with self.assertRaises(ValueError):
            ic.validate(*a, self.today)

    def test_amount_bounds(self):
        self.bad(0, date(2026, 2, 1), 2026)
        self.bad(-5, date(2026, 2, 1), 2026)
        self.bad(60000, date(2026, 2, 1), 2026)

    def test_future_dates_rejected(self):
        self.bad(100, date(2026, 3, 2), 2026)

    def test_tax_year_must_be_this_or_prior_year(self):
        self.bad(100, date(2026, 2, 1), 2024)
        self.bad(100, date(2026, 2, 1), 2027)

    def test_prior_year_only_until_april_15(self):
        ic.validate(100, date(2026, 4, 15), 2025, date(2026, 4, 20))
        with self.assertRaises(ValueError):
            ic.validate(100, date(2026, 4, 16), 2025, date(2026, 4, 20))

    def test_valid_contributions_pass(self):
        ic.validate(500, date(2026, 2, 1), 2026, self.today)
        ic.validate(500, date(2026, 2, 1), 2025, self.today)


class LedgerTests(unittest.TestCase):
    def test_invalid_contribution_never_touches_the_database(self):
        db = MagicMock()
        with self.assertRaises(ValueError):
            ic.add_contribution(db, 1, -1, date(2026, 2, 1), 2026, today=date(2026, 3, 1))
        db.execute.assert_not_called()

    def test_valid_contribution_inserts_and_commits(self):
        db = MagicMock()
        db.execute.return_value.scalar.return_value = 7
        self.assertEqual(ic.add_contribution(db, 1, 250.456, date(2026, 2, 1), 2026, "x", today=date(2026, 3, 1)), 7)
        self.assertEqual(db.execute.call_args[0][1]["a"], 250.46)
        db.commit.assert_called_once()

    def test_summary_groups_ledger_by_tax_year(self):
        SETTINGS.clear()
        SETTINGS.update(ira_birth_year="1990")
        ledger = [{"id": 1, "tax_year": 2026, "amount": 1000.0, "contributed_on": "2026-02-01", "note": ""},
                  {"id": 2, "tax_year": 2026, "amount": 500.0, "contributed_on": "2026-02-10", "note": ""},
                  {"id": 3, "tax_year": 2025, "amount": 7000.0, "contributed_on": "2025-12-01", "note": ""}]
        orig = ic.list_contributions
        ic.list_contributions = lambda db, uid: ledger
        try:
            out = ic.summary(MagicMock(), 1, today=date(2026, 3, 1))
        finally:
            ic.list_contributions = orig
        by = {y["year"]: y for y in out["years"]}
        self.assertEqual(by[2026]["contributed"], 1500)
        self.assertEqual(by[2025]["remaining"], 0)
        self.assertEqual(by[2025]["limit"], 7000)


if __name__ == "__main__":
    unittest.main()
