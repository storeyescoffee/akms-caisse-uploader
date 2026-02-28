#!/usr/bin/env python3
"""
Merged script: Caisse file watcher + API uploader + MQTT status publisher.
Combines script.sh and caisse_monitor.sh functionality.
"""

import argparse
import configparser
import json
import logging
import os
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# Optional: paho-mqtt for MQTT publishing
try:
    import paho.mqtt.client as mqtt
    HAS_MQTT = True
except ImportError:
    HAS_MQTT = False

# Status codes
STATUS_PENDING = 0
STATUS_SUCCESS = 1
STATUS_FAILED = 2
STATUS_FALLBACK = 3
STATUS_UNKNOWN = 5

CONFIG_PATH = Path(__file__).resolve().parent / "config.conf"


def load_config() -> configparser.ConfigParser:
    """Load config from config.conf."""
    cfg = configparser.ConfigParser()
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")
    cfg.read(CONFIG_PATH)
    return cfg


SCRIPT_DIR = Path(__file__).resolve().parent


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def _resolve(path: str) -> str:
    """Expand ~ and resolve relative paths against script directory."""
    expanded = _expand(path)
    if not os.path.isabs(expanded):
        return str(SCRIPT_DIR / expanded)
    return expanded


def get_config(cfg: configparser.ConfigParser) -> dict:
    """Parse config into a flat dict with expanded paths."""
    return {
        "base_dir": _expand(cfg.get("local", "base_dir", fallback="~/shared/POINTEX21/CAFEDEROME")),
        "log_dir": _resolve(cfg.get("local", "log_dir", fallback="./logs")),
        "status_file": _resolve(cfg.get("local", "status_file", fallback="caisse_status.txt")),
        "api_url": cfg.get("api", "url", fallback="http://app.storeyes.io:8000/process"),
        "api_timeout": cfg.getint("api", "timeout", fallback=120),
        "sleep_interval": cfg.getint("watcher", "sleep_interval", fallback=10),
        "stable_seconds": cfg.getint("watcher", "stable_seconds", fallback=2),
        "mqtt_host": cfg.get("mqtt", "host", fallback="18.100.207.236"),
        "mqtt_port": cfg.getint("mqtt", "port", fallback=1883),
        "mqtt_user": cfg.get("mqtt", "user", fallback="storeyes"),
        "mqtt_pass": cfg.get("mqtt", "password", fallback="12345"),
        "mqtt_qos": cfg.getint("mqtt", "qos", fallback=1),
        "mqtt_retain": cfg.getboolean("mqtt", "retain", fallback=False),
        "mqtt_timeout": cfg.getint("mqtt", "timeout", fallback=5),
        "mqtt_retries": cfg.getint("mqtt", "retries", fallback=3),
    }


def get_board_id() -> str:
    """Read board/serial ID from /proc/cpuinfo (Linux)."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.strip().startswith("Serial"):
                    parts = line.split()
                    return parts[2] if len(parts) >= 3 else "unknown"
    except (FileNotFoundError, OSError):
        pass
    return platform.node() or "unknown"


CONFIG: dict = {}


def setup_logging(cfg: dict):
    """Configure file logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(cfg["log_file"]),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(__name__)


def set_status(status: int, message: str = "", log: logging.Logger | None = None, cfg: dict | None = None) -> None:
    """Write status to file and optionally publish to MQTT."""
    c = cfg or CONFIG
    Path(c["status_file"]).write_text(str(status))
    if log:
        log.info("📊 Status: %s - %s", status, message)
    publish_status_to_mqtt(status, c)


def publish_status_to_mqtt(status: int, cfg: dict | None = None) -> bool:
    """Publish caisse status to MQTT broker (from caisse_monitor.sh)."""
    if not HAS_MQTT:
        return False

    c = cfg or CONFIG
    board_id = get_board_id()
    topic = f"storeyes/{board_id}/caisse"
    payload = {
        "board_id": board_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "caisse-status": str(status),
    }

    for attempt in range(1, c["mqtt_retries"] + 1):
        try:
            client = mqtt.Client()
            client.username_pw_set(c["mqtt_user"], c["mqtt_pass"])
            client.connect(c["mqtt_host"], c["mqtt_port"], c["mqtt_timeout"])
            msg = json.dumps(payload)
            client.publish(topic, msg, qos=c["mqtt_qos"], retain=c["mqtt_retain"])
            client.disconnect()
            return True
        except Exception:
            if attempt < c["mqtt_retries"]:
                time.sleep(2)
    return False


def is_stable(filepath: Path, cfg: dict | None = None) -> bool:
    """Check if file size is unchanged after stable_seconds."""
    c = cfg or CONFIG
    try:
        s1 = filepath.stat().st_size
        time.sleep(c["stable_seconds"])
        s2 = filepath.stat().st_size
        return s1 == s2
    except (OSError, FileNotFoundError):
        return False


def target_date_and_paths(day_offset: int, cfg: dict | None = None) -> tuple[str, str, Path]:
    """Compute target date, MMDDYY, and year dir."""
    from datetime import datetime, timedelta
    c = cfg or CONFIG
    target = datetime.now() + timedelta(days=day_offset)
    target_date = target.strftime("%Y-%m-%d")
    mmddyy = target.strftime("%m%d%y")
    year = target.strftime("%Y")
    year_dir = Path(c["base_dir"]) / f"AN{year}"
    return target_date, mmddyy, year_dir


def run_mount() -> None:
    """Run sudo mount -a (Linux)."""
    if sys.platform != "linux":
        return
    try:
        os.system("sudo mount -a")
    except Exception:
        pass


def run_test_mode(log: logging.Logger, cfg: dict, day_offset: int) -> int:
    """Test mode: check MQTT connectivity and dry-run the file detection process."""
    log.info("🧪 Running in TEST mode (dry run)")
    errors = 0

    # --- MQTT connectivity check ---
    log.info("--- MQTT connectivity check ---")
    if not HAS_MQTT:
        log.error("❌ paho-mqtt is not installed, MQTT unavailable")
        errors += 1
    else:
        board_id = get_board_id()
        topic = f"storeyes/{board_id}/caisse"
        log.info("   Board ID : %s", board_id)
        log.info("   Broker   : %s:%s", cfg["mqtt_host"], cfg["mqtt_port"])
        log.info("   Topic    : %s", topic)
        try:
            client = mqtt.Client()
            client.username_pw_set(cfg["mqtt_user"], cfg["mqtt_pass"])
            client.connect(cfg["mqtt_host"], cfg["mqtt_port"], cfg["mqtt_timeout"])
            client.disconnect()
            log.info("✅ MQTT connection successful")
        except Exception as e:
            log.error("❌ MQTT connection failed: %s", e)
            errors += 1

    # --- Dry-run file detection ---
    log.info("--- Dry-run file detection ---")
    target_date, mmddyy, year_dir = target_date_and_paths(day_offset, cfg)
    log.info("   Target date  : %s", target_date)
    log.info("   MMDDYY       : %s", mmddyy)
    log.info("   Year dir     : %s", year_dir)

    if not year_dir.is_dir():
        log.error("❌ Year directory not found: %s", year_dir)
        errors += 1
    else:
        log.info("✅ Year directory exists")
        db_pattern = f"VD{mmddyy}.DB"
        mb_pattern = f"VD{mmddyy}.MB"
        db_file = next(year_dir.glob(db_pattern), None)
        mb_file = next(year_dir.glob(mb_pattern), None)

        if db_file:
            log.info("✅ DB file found: %s (%d bytes)", db_file.name, db_file.stat().st_size)
        else:
            log.warning("⚠️  DB file not found: %s", db_pattern)

        if mb_file:
            log.info("✅ MB file found: %s (%d bytes)", mb_file.name, mb_file.stat().st_size)
        else:
            log.warning("⚠️  MB file not found: %s", mb_pattern)

        if db_file and mb_file:
            if is_stable(db_file, cfg) and is_stable(mb_file, cfg):
                log.info("✅ Both files are stable")
                # --- Upload with dry_run ---
                log.info("--- API upload (dry_run: true) ---")
                log.info("   URL     : %s", cfg["api_url"])
                log.info("   Timeout : %ss", cfg["api_timeout"])
                try:
                    with open(db_file, "rb") as dbf, open(mb_file, "rb") as mbf:
                        r = requests.post(
                            cfg["api_url"],
                            headers={"X-DEVICE-ID": get_board_id()},
                            files={
                                "file": (db_file.name, dbf, "application/octet-stream"),
                                "mb_file": (mb_file.name, mbf, "application/octet-stream"),
                            },
                            data={"dry_run": "true"},
                            timeout=cfg["api_timeout"],
                        )
                        r.raise_for_status()
                    log.info("✅ Upload successful (dry run)")
                except requests.RequestException as e:
                    log.error("❌ Upload failed: %s", e)
                    errors += 1
            else:
                log.warning("⚠️  Files exist but are still changing")

    # --- Summary ---
    if errors:
        log.info("🧪 Test completed with %d error(s)", errors)
        return 1
    log.info("🧪 Test completed successfully — all checks passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Caisse watcher + API upload + MQTT status")
    parser.add_argument(
        "--date-cursor",
        type=int,
        default=0,
        help="Day offset: 0=today, -1=yesterday, etc.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: check connectivity and upload with dry_run: true.",
    )
    args = parser.parse_args()
    day_offset = args.date_cursor

    global CONFIG
    CONFIG = get_config(load_config())
    log_dir = Path(CONFIG["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    # One log file per day; set once at startup so it stays the same after midnight
    CONFIG["log_file"] = str(log_dir / f"{datetime.now():%Y-%m-%d}.log")
    log = setup_logging(CONFIG)

    if args.test:
        return run_test_mode(log, CONFIG, day_offset)

    target_date, mmddyy, year_dir = target_date_and_paths(day_offset, CONFIG)

    log.info("▶️ Watcher started")
    log.info("📂 Watching: %s", year_dir)
    log.info("⏪ Date offset: %s day(s)", day_offset)
    log.info("🗓️  Target date (MMDDYY): %s", mmddyy)

    # Check previous status for fallback (day_offset != 0)
    if day_offset != 0:
        status_path = Path(CONFIG["status_file"])
        if status_path.exists():
            try:
                prev = int(status_path.read_text().strip())
                if prev == STATUS_SUCCESS:
                    log.info("✅ Previous status was SUCCESS, skipping processing")
                    return 0
                log.info("⚠️  Previous status was %s, attempting fallback", prev)
            except (ValueError, OSError):
                log.info("ℹ️  No previous status file found, proceeding with fallback")
        else:
            log.info("ℹ️  No previous status file found, proceeding with fallback")

    run_mount()

    try:
        if not year_dir.is_dir():
            log.error("❌ Year directory not found: %s", year_dir)
            set_status(STATUS_FAILED, "Year directory not found", log, CONFIG)
            return 1
    except OSError as e:
        log.error("❌ Cannot access year directory %s: %s", year_dir, e)
        set_status(STATUS_FAILED, f"Year directory unavailable: {e}", log, CONFIG)
        return 1

    # Clear log and set pending
    with open(CONFIG["log_file"], "w") as f:
        f.write("==============================\n")
    set_status(STATUS_PENDING, "Waiting for caisse files", log, CONFIG)

    db_pattern = f"VD{mmddyy}.DB"
    mb_pattern = f"VD{mmddyy}.MB"
    db_file = next(year_dir.glob(db_pattern), None)
    mb_file = next(year_dir.glob(mb_pattern), None)

    while True:
        run_mount()
        try:
            db_file = next(year_dir.glob(db_pattern), None)
            mb_file = next(year_dir.glob(mb_pattern), None)
        except OSError as e:
            log.error("❌ Cannot access year directory %s: %s", year_dir, e)
            time.sleep(CONFIG["sleep_interval"])
            continue

        if db_file and mb_file:
            log.info("📄 Found matching files")
            log.info("   DB: %s", db_file)
            log.info("   MB: %s", mb_file)

            try:
                stable_db = is_stable(db_file, CONFIG)
                stable_mb = is_stable(mb_file, CONFIG)
            except OSError as e:
                log.error("❌ Cannot access files (mount down?): %s", e)
                time.sleep(CONFIG["sleep_interval"])
                continue

            if stable_db and stable_mb:
                log.info("✅ Files are stable, sending API request")
                is_fallback = day_offset != 0

                try:
                    with open(db_file, "rb") as dbf, open(mb_file, "rb") as mbf:
                        r = requests.post(
                            CONFIG["api_url"],
                            headers={"X-DEVICE-ID": get_board_id()},
                            files={
                                "file": (db_file.name, dbf, "application/octet-stream"),
                                "mb_file": (mb_file.name, mbf, "application/octet-stream"),
                            },
                            timeout=CONFIG["api_timeout"],
                        )
                        r.raise_for_status()
                except OSError as e:
                    log.error("❌ Cannot read files (mount down?): %s", e)
                    time.sleep(CONFIG["sleep_interval"])
                    continue
                except requests.RequestException as e:
                    set_status(STATUS_FAILED, "API call failed", log, CONFIG)
                    log.error("❌ API call failed: %s", e)
                    return 1

                if is_fallback:
                    set_status(STATUS_FALLBACK, "Success after fallback (morning retry)", log, CONFIG)
                    log.info("🚀 API call succeeded (fallback mode)")
                else:
                    set_status(STATUS_SUCCESS, "Upload success", log, CONFIG)
                    log.info("🚀 API call succeeded")
                return 0
            else:
                log.info("⏳ Files exist but still changing")
        else:
            log.info("⌛ Waiting for DB + MB files for %s", mmddyy)

        time.sleep(CONFIG["sleep_interval"])


if __name__ == "__main__":
    sys.exit(main())
