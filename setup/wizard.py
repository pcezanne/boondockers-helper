#!/usr/bin/env python3
"""
Setup wizard for Boondockers Helper.

Usage (normally invoked via setup/setup.sh):
    python3 setup/wizard.py [--skip-launchd | --skip-config]
"""

import argparse
import configparser
import getpass
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT   = Path(__file__).parent.parent
CONFIG_PATH    = PROJECT_ROOT / "config.ini"
CONFIG_EXAMPLE = PROJECT_ROOT / "config.ini.example"
PLIST_TEMPLATE = PROJECT_ROOT / "setup" / "com.victron.logger.plist"
PLIST_DEST     = Path.home() / "Library" / "LaunchAgents" / "com.victron.logger.plist"
PLIST_LABEL    = "com.victron.logger"
LOG_PATH       = Path.home() / "Library" / "Logs" / "victron-logger.log"

# Same derivation as logger.py line 25 — finds victron-ble next to python3.
VICTRON_BLE = str(Path(sys.executable).parent / "victron-ble")

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"


def _c(color: str, text: str) -> str:
    return f"{color}{text}{RESET}"


def success(msg: str) -> None:
    print(f"{GREEN}✓{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}⚠{RESET} {msg}")


def error(msg: str) -> None:
    print(f"{RED}✗{RESET} {msg}", file=sys.stderr)


def banner(title: str) -> None:
    width = 50
    print()
    print(_c(BOLD, "─" * width))
    print(_c(BOLD, f"  {title}"))
    print(_c(BOLD, "─" * width))


def prompt(msg: str, default: str | None = None) -> str:
    if default is not None:
        display = f"{msg} [{default}]: "
    else:
        display = f"{msg}: "
    try:
        val = input(display).strip()
    except EOFError:
        print()
        sys.exit(0)
    return val if val else (default or "")


def confirm(msg: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        val = input(f"{msg} {hint} ").strip().lower()
    except EOFError:
        print()
        sys.exit(0)
    if not val:
        return default
    return val.startswith("y")


def _prompt_float(msg: str, default: str) -> str:
    while True:
        val = prompt(msg, default=default)
        try:
            float(val)
            return val
        except ValueError:
            error(f"Expected a number, got: {val!r}")


def _prompt_int(msg: str, default: str) -> str:
    while True:
        val = prompt(msg, default=default)
        try:
            int(val)
            return val
        except ValueError:
            error(f"Expected a whole number, got: {val!r}")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
)
_KEY_RE = re.compile(r'^[0-9a-f]{32}$')


def _uuid_valid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


def _key_valid(s: str) -> bool:
    return bool(_KEY_RE.match(s.lower()))


# ---------------------------------------------------------------------------
# Step 0: Check existing config
# ---------------------------------------------------------------------------

def check_existing_config() -> tuple[str, configparser.ConfigParser | None]:
    """
    Returns ('fresh', None) if no config exists.
    Otherwise shows the existing config summary and returns
    ('skip', cfg), ('reconfigure', cfg), or ('quit', None).
    """
    if not CONFIG_PATH.exists():
        return "fresh", None

    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)

    uuid = cfg.get("device", "uuid", fallback="(not set)")
    key  = cfg.get("device", "key",  fallback="(not set)")
    key_display = ("●" * 32) if len(key) == 32 else key

    print()
    print(_c(BOLD, "config.ini already exists:"))
    print(f"  UUID: {uuid}")
    print(f"  Key:  {key_display}")
    print()
    print("  [s] Skip config — keep existing and jump to connection test")
    print("  [r] Reconfigure — re-enter UUID, key, and settings")
    print("  [q] Quit")
    print()

    for _ in range(3):
        try:
            choice = input("Choice [s]: ").strip().lower() or "s"
        except EOFError:
            print()
            sys.exit(0)
        if choice == "s":
            return "skip", cfg
        if choice == "r":
            return "reconfigure", cfg
        if choice == "q":
            sys.exit(0)
        print("  Please enter s, r, or q.")

    return "skip", cfg


# ---------------------------------------------------------------------------
# Step 1: Welcome
# ---------------------------------------------------------------------------

def step_welcome() -> None:
    width = 50
    print()
    print(_c(BOLD, "=" * width))
    print(_c(BOLD, "  Boondockers Helper — Setup Wizard"))
    print(_c(BOLD, "=" * width))
    print()
    print("This wizard will:")
    print("  1. Run BLE discovery to find your device UUID")
    print("  2. Collect your encryption key from VictronConnect")
    print("  3. Write config.ini")
    print("  4. Verify the BLE connection")
    print("  5. Install the macOS background agent (launchd)")
    print()
    print(_c(BOLD, "Before you continue, make sure:"))
    print("  • Your BMV-712 is powered on and within Bluetooth range")
    print("  • VictronConnect is open on your phone with the device connected")
    print("    (you'll need to navigate to Product Info → Instant Readout Details)")
    print()
    try:
        input("Press Enter to start, or Ctrl-C to abort… ")
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


# ---------------------------------------------------------------------------
# Step 2: BLE Discovery
# ---------------------------------------------------------------------------

def step_ble_discovery() -> str:
    banner("Step 1 of 5 — BLE Discovery")
    print()
    print("We'll run:  victron-ble discover")
    print()
    print("Look for a line like:")
    print(_c(YELLOW + BOLD,
             "  INFO:victron_ble.scanner:A1B2C3D4-...: SmartBMV HQ2144XXXX"))
    print()
    print("The UUID is the segment before the colon — 8-4-4-4-12 hex digits.")
    print("Scanning stops automatically after 30 seconds; press Ctrl-C to stop early.")
    print()
    try:
        input("Press Enter to start scanning… ")
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    print()
    _run_ble_discover()
    print()

    for attempt in range(1, 4):
        raw = prompt("Enter the device UUID (or leave blank to re-scan)")
        if not raw:
            if attempt < 3:
                print()
                print("Re-scanning…")
                print()
                _run_ble_discover()
                print()
                continue
            else:
                error("No UUID entered after 3 attempts.")
                sys.exit(1)

        uuid = raw.strip().upper()
        if _uuid_valid(uuid):
            success(f"UUID accepted: {uuid}")
            return uuid

        error(
            f"That doesn't look like a valid UUID.\n"
            f"  Expected: XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX\n"
            f"  Got:      {uuid}"
        )

    error("Could not obtain a valid UUID.")
    sys.exit(1)


def _run_ble_discover() -> None:
    """Stream victron-ble discover output for up to 30 seconds, highlighting SmartBMV lines."""
    try:
        proc = subprocess.Popen(
            [VICTRON_BLE, "discover"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        error(f"victron-ble not found at: {VICTRON_BLE}")
        error("Run: pip install victron-ble")
        sys.exit(1)

    timer = threading.Timer(30.0, proc.terminate)
    timer.start()

    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            if "SmartBMV" in line or _uuid_re_in_line(line):
                print(_c(YELLOW + BOLD, line))
            else:
                print(_c(DIM, line))
    except KeyboardInterrupt:
        proc.terminate()
        print()
    finally:
        timer.cancel()
        proc.wait()


def _uuid_re_in_line(line: str) -> bool:
    return bool(re.search(
        r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}',
        line
    ))


# ---------------------------------------------------------------------------
# Step 3: Encryption Key
# ---------------------------------------------------------------------------

def step_encryption_key() -> str:
    banner("Step 2 of 5 — Encryption Key")
    print()
    print("Retrieve your key from the VictronConnect app:")
    print("  1. Open VictronConnect on your iPhone or Android")
    print("  2. Connect to your BMV-712")
    print("  3. Tap ••• (top-right)  →  Product Info")
    print("  4. Scroll to 'Instant Readout via Bluetooth'")
    print("     Enable it if it is not already on")
    print("  5. Tap [Show] next to 'Instant Readout Details'")
    print("  6. Copy the Encryption Key  (32 hex characters, no spaces)")
    print()
    print(_c(DIM, "  Example:  a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"))
    print()
    print(_c(DIM, "  (Key input is hidden — it will not appear as you type)"))
    print()

    for attempt in range(1, 4):
        try:
            raw = getpass.getpass("Encryption key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        key = raw.lower()

        if len(key) != 32:
            error(f"Expected 32 hex characters, got {len(key)}. Try again.")
        elif not re.match(r'^[0-9a-f]+$', key):
            bad = [c for c in key if c not in "0123456789abcdef"]
            error(f"Non-hex characters found: {bad!r}. Try again.")
        elif key == "0" * 32:
            error("That looks like the placeholder — copy the real key from VictronConnect.")
        else:
            success("Encryption key accepted.")
            return key

        if attempt == 3:
            error("Could not obtain a valid key.")
            sys.exit(1)

    error("Could not obtain a valid key.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Step 4: Settings
# ---------------------------------------------------------------------------

def step_collect_settings() -> dict:
    banner("Step 3 of 5 — Settings")
    print()
    print("Press Enter to accept the default shown in [brackets].")
    print()

    settings: dict = {}

    settings["poll_interval_minutes"] = _prompt_float("Poll interval in minutes", default="15")

    print()
    print("Battery capacity: total usable amp-hours in your bank (e.g. 200 for a 200Ah bank).")
    print("Used for remaining-Ah and average power draw calculations.")
    print("Leave blank to suppress those cards — you can add it to config.ini later.")
    cap = prompt("Battery capacity in Ah")
    settings["battery_capacity_ah"] = cap  # empty string = leave commented out

    print()
    settings["threshold_amps"] = _prompt_float("Charging threshold in amps", default="2.0")
    settings["target_soc_pct"] = _prompt_int("Target SOC % to stop generator", default="95")

    print()
    tf = ""
    while tf not in ("12h", "24h"):
        tf = prompt("Time format (12h/24h)", default="12h")
        if tf not in ("12h", "24h"):
            error("Please enter 12h or 24h.")
    settings["time_format"] = tf

    return settings


# ---------------------------------------------------------------------------
# Step 5: Write config.ini
# ---------------------------------------------------------------------------

def step_write_config(uuid: str, key: str, settings: dict) -> None:
    banner("Writing config.ini")

    template = CONFIG_EXAMPLE.read_text()

    replacements = {
        "uuid = 00000000-0000-0000-0000-000000000000": f"uuid = {uuid}",
        "key  = 00000000000000000000000000000000":      f"key  = {key}",
        "poll_interval_minutes = 15":
            f"poll_interval_minutes = {settings['poll_interval_minutes']}",
        "threshold_amps = 2.0":
            f"threshold_amps = {settings['threshold_amps']}",
        "target_soc_pct = 95":
            f"target_soc_pct = {settings['target_soc_pct']}",
        "time_format = 12h":
            f"time_format = {settings['time_format']}",
    }

    config_text = template
    for old, new in replacements.items():
        config_text = config_text.replace(old, new)

    # Battery capacity: comment out the line if left blank, else substitute.
    cap = settings.get("battery_capacity_ah", "").strip()
    if cap:
        config_text = config_text.replace(
            "battery_capacity_ah = 920",
            f"battery_capacity_ah = {cap}",
        )
    else:
        config_text = config_text.replace(
            "battery_capacity_ah = 920",
            "; battery_capacity_ah =",
        )
        warn("battery_capacity_ah left blank — remaining Ah and avg power cards will be suppressed.")

    CONFIG_PATH.write_text(config_text)
    success(f"config.ini written to: {CONFIG_PATH}")


# ---------------------------------------------------------------------------
# Step 6: Connection test
# ---------------------------------------------------------------------------

def step_test_connection() -> tuple[bool, bool]:
    banner("Step 4 of 5 — Connection Test")
    print()
    print("Testing BLE connection (timeout: 35 seconds)…")
    print(_c(DIM, f"Running: python3 victron/logger.py --once"))
    print()

    reconfigured = False
    for attempt in range(1, 4):
        result = _run_once()
        output = (result.stdout or "") + (result.stderr or "")
        print(output.rstrip())
        print()

        if "Logged:" in output:
            success("Connection verified — device is responding.")
            return True, reconfigured

        _print_connection_failure_tips()

        if attempt < 3:
            choice = _connection_retry_prompt()
            if choice == "r":
                # Reconfigure UUID + key + rewrite config, then retry.
                uuid     = step_ble_discovery()
                key      = step_encryption_key()
                settings = step_collect_settings()
                step_write_config(uuid, key, settings)
                reconfigured = True
                print()
                continue
            elif choice == "s":
                warn("Skipping connection test — verify manually before relying on the logger.")
                return False, reconfigured
            else:
                sys.exit(0)
        else:
            warn("Connection test failed after 3 attempts.")
            warn("Verify config.ini and retry: python3 victron/logger.py --once")
            return False, reconfigured

    return False, reconfigured


def _run_once() -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [sys.executable, "victron/logger.py", "--once"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=40,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=[], returncode=1,
            stdout="", stderr="Timed out after 35 seconds — no data received.\n",
        )


def _print_connection_failure_tips() -> None:
    print("Possible causes:")
    print("  • BMV-712 is out of Bluetooth range")
    print("  • UUID is wrong — macOS UUIDs are pairing-specific to this Mac")
    print("    (run victron-ble discover again and copy the UUID carefully)")
    print("  • Encryption key is wrong — get it from VictronConnect → Product Info")
    print("  • Bluetooth is disabled in System Settings")
    print()


def _connection_retry_prompt() -> str:
    print("[r] Reconfigure UUID/key and retry")
    print("[s] Skip test")
    print("[q] Quit")
    print()
    for _ in range(3):
        try:
            choice = input("Choice [r]: ").strip().lower() or "r"
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if choice in ("r", "s", "q"):
            return choice
        print("Please enter r, s, or q.")
    return "r"


# ---------------------------------------------------------------------------
# Step 7: launchd agent
# ---------------------------------------------------------------------------

def step_launchd() -> bool:
    banner("Step 5 of 5 — Background Agent (launchd)")
    print()
    print("The background agent runs logger.py automatically whenever your Mac is on.")
    print(f"Logs: {LOG_PATH}")
    print()

    if not confirm("Install the launchd agent?", default=True):
        print()
        print(_c(DIM, "Skipped. To install later: bash setup/setup.sh --skip-config"))
        return False

    # Check if already loaded.
    already_loaded = (
        subprocess.run(
            ["launchctl", "list", PLIST_LABEL],
            capture_output=True,
        ).returncode == 0
    )

    if already_loaded:
        print()
        print("Agent is already installed.")
        print("[r] Reload with updated config")
        print("[s] Skip")
        print()
        for _ in range(3):
            try:
                choice = input("Choice [r]: ").strip().lower() or "r"
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            if choice == "s":
                return True
            if choice == "r":
                break
            print("Please enter r or s.")

    # Customize the plist.
    plist_text = PLIST_TEMPLATE.read_text()
    plist_text = plist_text.replace(
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
        sys.executable,
    )
    plist_text = plist_text.replace("__PROJECT_PATH__", str(PROJECT_ROOT))
    plist_text = plist_text.replace("__LOG_PATH__", str(LOG_PATH))

    PLIST_DEST.parent.mkdir(parents=True, exist_ok=True)
    PLIST_DEST.write_text(plist_text)
    success(f"Plist written to: {PLIST_DEST}")

    # Unload first if already running.
    if already_loaded:
        subprocess.run(["launchctl", "unload", str(PLIST_DEST)], capture_output=True)

    # Load.
    load_result = subprocess.run(
        ["launchctl", "load", str(PLIST_DEST)],
        capture_output=True,
        text=True,
    )
    if load_result.returncode != 0:
        error("launchctl load failed:")
        print(load_result.stderr or load_result.stdout)
        print()
        print(f"Debug: launchctl list | grep victron")
        print(f"Logs:  tail -20 {LOG_PATH}")
        return False

    # Verify.
    time.sleep(2)
    verify = subprocess.run(
        ["launchctl", "list", PLIST_LABEL],
        capture_output=True,
        text=True,
    )
    pid_match = re.search(r'"PID"\s*=\s*(\d+)', verify.stdout)
    if pid_match and pid_match.group(1) != "0":
        pid = pid_match.group(1)
        success(f"Agent running (PID: {pid})")
    else:
        warn("Agent loaded but may not have started yet.")
        print(f"  Check: launchctl list {PLIST_LABEL}")
        print(f"  Logs:  tail -20 {LOG_PATH}")

    return True


# ---------------------------------------------------------------------------
# Step 8: Done
# ---------------------------------------------------------------------------

def step_complete(*, config_written: bool, verified: bool, launchd_ok: bool) -> None:
    width = 50
    print()
    print(_c(BOLD, "=" * width))
    print(_c(BOLD, "  Setup Complete!"))
    print(_c(BOLD, "=" * width))
    print()

    def _status(ok: bool, label: str) -> str:
        return f"  {GREEN}✓{RESET} {label}" if ok else f"  {YELLOW}—{RESET} {label}"

    print(_status(config_written, "config.ini written"))
    print(_status(verified,       "BLE connection verified"))
    print(_status(launchd_ok,     "Background agent running"))
    print()
    print(_c(BOLD, "Next steps:"))
    print(f"  Dashboard:   ./start_dashboard.sh")
    print(f"  Check logs:  tail -f {LOG_PATH}")
    print(f"  One-off test: python3 victron/logger.py --once")
    print(f"  Stop agent:  launchctl unload {PLIST_DEST}")
    print()
    print("Data appears in the dashboard after the first poll interval.")
    print("Give it 7+ days of logging before trusting the usage averages.")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Boondockers Helper setup wizard"
    )
    parser.add_argument(
        "--skip-launchd",
        action="store_true",
        help="Configure only — skip launchd agent installation",
    )
    parser.add_argument(
        "--skip-config",
        action="store_true",
        help="Skip config — only (re)install the launchd agent",
    )
    args = parser.parse_args()

    step_welcome()

    config_written = False
    uuid = key = None

    if args.skip_config:
        # Jump straight to launchd.
        launchd_ok = step_launchd()
        step_complete(config_written=False, verified=False, launchd_ok=launchd_ok)
        return

    # Check for existing config.
    mode, existing_cfg = check_existing_config()

    if mode == "quit":
        sys.exit(0)

    if mode == "skip":
        # Use existing UUID/key; skip to connection test.
        pass
    else:
        # Fresh install or reconfigure.
        uuid     = step_ble_discovery()
        key      = step_encryption_key()
        settings = step_collect_settings()
        step_write_config(uuid, key, settings)
        config_written = True

    verified, reconfigured = step_test_connection()
    config_written = config_written or reconfigured

    if not args.skip_launchd:
        launchd_ok = step_launchd()
    else:
        launchd_ok = False

    step_complete(
        config_written=config_written,
        verified=verified,
        launchd_ok=launchd_ok,
    )


if __name__ == "__main__":
    main()
