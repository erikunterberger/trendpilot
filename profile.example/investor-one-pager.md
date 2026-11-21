---
type: investor-profile
last_updated: 2026-01-01
# Copy this folder to profile/ (git-ignored) and edit. TrendPilot reads only
# the fields below; the rest of the file is for you.
risk_tolerance: moderate            # conservative | moderate | aggressive
time_horizon:
  tactical_months: 24
max_single_position_pct: 10         # hard cap per position, % of the account
allocation_guardrails_pct:          # [min, max] % per category; the max is enforced
  core_high_conviction: [10, 25]
  growth_momentum: [30, 50]
  defensive_income: [10, 25]
  speculative: [0, 10]
  diversified_etf: [10, 30]
excluded_vehicles: [margin, options, leveraged_etfs]
---

# Investor One-Pager (example)

Describe your situation, goals and rules here in your own words. Only the
YAML above is read by the code, and only its generic limits are ever sent
to the language model.

`investor-strategy-system-prompt.md` in the repo root is an interview prompt
that produces a filled-in version of this file with any capable LLM.
