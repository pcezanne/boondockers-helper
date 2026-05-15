# Architectural Roadmap: Boondockers' Helper v1.0

This document outlines the architectural refactor strategy required to transition **Boondockers' Helper** from a laptop-based utility to an always-on, multi-source power management system running on Raspberry Pi.

---

## 1. Vision: From "Analyser" to "System Hub"

The current "Pragmatic Monolith" architecture (v0.1) excels at post-hoc analysis of Victron BMV-712 data. To support **Always-On monitoring (RPi)** and **Heterogeneous Power Sources (Solar, Magnum Inverters, DC-DC)**, the system must evolve toward a decoupled, provider-based architecture.

---

## 2. Pillar I: Domain Isolation (The "Engine")

**The Goal:** Separate battery physics logic from the logging and reporting interfaces.

- **Current State:** Session detection and health diagnostics are coupled with data loading in `report.py` and `app.py`.
- **Target State:** A standalone `victron.engine` module.
    - **Calculations:** Move Peukert linear extrapolation, CC/CV knee detection, and thermal derating into pure functions.
    - **Benefit:** Allows for a future **Alerts Service**. A background daemon can use the same "Engine" to send a notification when "Hours to Target SOC" hits 0.5h, without needing to launch the full web dashboard.

---

## 3. Pillar II: Hardware Abstraction Layer (HAL)

**The Goal:** Support Solar and third-party Inverter/Chargers without rewriting analysis logic.

- **The Problem:** The system is currently "hard-wired" to the Victron BLE protocol.
- **The Refactor:** Implement a **Provider Pattern**.
    - **Interface:** Define a standard `IBatteryMonitor` or `IPowerSource` interface with methods like `get_current()`, `get_soc()`, and `is_charging()`.
    - **Implementations:** - `VictronBleProvider` (Existing)
        - `SolarControllerProvider` (New: for gross solar harvest vs. net shunt current)
        - `MagnumInverterProvider` (New: for integrated inverter/charger state)
- **Benefit:** The analysis engine remains "blind" to the hardware, making the app future-proof for any RV sensor suite.

---

## 4. Pillar III: Pre-Processed State (Performance on Pi)

**The Goal:** Ensure the Dashboard remains snappy on low-power Raspberry Pi hardware.

- **The Problem:** Re-calculating "Laptop-closed gaps" and session boundaries from 10,000+ raw readings every time the UI refreshes will bottleneck a Pi's CPU.
- **The Refactor:** **Stateful Logging.**
    - As `logger.py` receives data, it should write to a `sessions` table in real-time. 
    - The logger maintains the "Open Session" state. When a charging current is detected, it opens a session; when it drops, it closes it and persists the stats (`ah_consumed`, `avg_watts`).
- **Benefit:** The Dashboard and Reports become simple "Read" operations (`SELECT * FROM sessions`), eliminating the need for expensive on-the-fly computation.

---

## 5. Pillar IV: Data Access Layer (DAL)

**The Goal:** Centralize schema management and improve data resilience.

- **Current State:** Raw SQL strings are distributed across modules, making schema updates (e.g., adding `temperature` or `cell_imbalance`) high-risk.
- **The Refactor:** Introduce a **Repository Pattern** or Light ORM (e.g., Peewee).
    - Centralize all database interactions in a `victron.db` module.
    - Implement a migration service (e.g., Yoyo or Alembic) to handle schema versioning automatically.
- **Benefit:** Ensures data integrity during software updates and simplifies the transition to time-series databases (like InfluxDB) if required for higher-resolution logging.

---

## 6. Implementation Roadmap

| Phase | Milestone | Priority |
| :--- | :--- | :--- |
| **Phase 1** | **Domain Extraction:** Move core math/physics to `victron.engine`. | **High** |
| **Phase 2** | **Pi Hardening:** Transition to `systemd` on RPi and implement pre-processed session logging. | **High** |
| **Phase 3** | **HAL Introduction:** Build the Solar Provider to track gross harvest vs. net battery delta. | **Medium** |
| **Phase 4** | **Expansion:** Integrate Magnum/Inverter metrics via HAL for a "Total Power View." | **Low** |

---
*Roadmap generated for Boondockers' Helper v1.0 Development.*
