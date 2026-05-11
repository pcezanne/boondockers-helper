# Human Tasks

Things that require a human — browser, camera, device access, or physical action.
Code and design work goes in FUTURE.md and CLAUDE.md.

---

## Screenshots for User Manual

The diagnostics section of the manual has no screenshots yet. Use the demo database
to capture them without needing real anomaly data.

**Setup:**
```bash
python3 tools/make_demo_db.py     # writes victron_demo.db
# Edit config.ini: db_path = victron_demo.db
./start_dashboard.sh
# Click Refresh so diagnostics panel appears
```

**Screenshots needed** — save each file to:

```
docs/images/
```

(from the `src/` directory)

| Filename | What to capture |
|---|---|
| `diagnostics_panel.png` | All four alert cards visible (thermal, knee SOC, rate declining, parasitic drain) |
| `diagnostics_charging_table.png` | Charging table rows with ⚠ icons and hover tooltip showing flag description |
| `diagnostics_discharge_table.png` | Discharge table row with ⚠ icon for parasitic drain |
| `diagnostics_charge_rate_decline.png` | Charge rate chart showing the declining Generator trend |

After screenshots, restore real data:
```bash
# Edit config.ini: db_path = victron_data.db
```

**Add image references to the manual** once screenshots exist. The diagnostics section
in `docs/USER_MANUAL.md` currently has no images — add `![...]` lines to each
subsection once screenshots are captured.

---

## Physical Checks Suggested by Diagnostics

Items flagged by the tool that require hands-on investigation:

- [ ] **Charger bay ventilation** — verify intake and exhaust paths. Thermal derating
      is active (see May 3, May 8 sessions in real data). Check if cool air can enter
      the bay, not just exit.
- [ ] **Magnum charger temperature** — during next stationary generator session, watch
      the ME-ARC50-L temperature display in real time and note the reading when amps
      step down.
- [ ] **Battery terminal connections** — re-torque and inspect for oxidation. Charge rate
      decline diagnostic will flag if CC rate drops >20% below all-time average.

---

## Config Tuning After More Data Accumulates

- [ ] **Knee SOC baseline** — after 10+ shore sessions, review whether
      `knee_soc_baseline_sessions = 5` is the right window, or if a longer baseline
      gives more stable alerts.
- [ ] **Thermal derating threshold** — the 15% default may be too sensitive or not
      sensitive enough once more Generator sessions are in the DB. Check with
      `./run_tests.sh` after any config change to ensure tests still pass.
