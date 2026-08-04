#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Victron Battery Monitor (VBM) client.

Reads live battery data from a victron-bm-webui instance and hands it to the
power manager as an optional, higher-quality source of power state.

Everything here is best-effort by design. If the service is down, slow, or
serving stale readings, get_status() returns None and the caller falls back
to sentinel-based detection. Battery data is an enhancement, never a
dependency - a failure here must never keep servers running into a dead
battery, nor shut them down while mains is fine.
"""

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("PowerManager")

# Fallback AC detection, used only when the VBM instance is too old to report
# `ac_power` itself. Mirrors the defaults in victron-bm-webui/app/ac_state.py.
DEFAULT_AC_FALLBACK_VOLTAGE = 13.50
DEFAULT_AC_DISCHARGE_CURRENT = -1.0
DEFAULT_AC_CHARGE_CURRENT = 2.0


@dataclass
class BatteryStatus:
    """A single trustworthy snapshot of the battery."""

    voltage: Optional[float]
    current: Optional[float]
    power: Optional[float]
    soc: Optional[float]
    remaining_mins: Optional[int]
    temperature: Optional[float]
    ac_power: Optional[bool]
    ac_power_since: Optional[str]
    connected: bool
    last_update: Optional[str]
    data_age_seconds: float
    ac_power_inferred: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        """Short human-readable summary for logs and notifications."""
        parts = []
        if self.voltage is not None:
            parts.append(f"{self.voltage:.2f}V")
        if self.current is not None:
            parts.append(f"{self.current:+.2f}A")
        if self.soc is not None:
            parts.append(f"SoC {self.soc:.1f}%")
        if self.remaining_mins is not None:
            parts.append(f"~{self.remaining_mins} min left")
        mains = {True: "mains OK", False: "ON BATTERY", None: "mains unknown"}[self.ac_power]
        parts.append(mains)
        return ", ".join(parts)


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> Optional[bool]:
    """Parse a tri-state boolean from config or JSON ('' / None stay None)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return None


class BatteryMonitor:
    """HTTP client for the victron-bm-webui REST API."""

    def __init__(self, config: dict):
        self.enabled = str(config.get('BATTERY_ENABLED', 'false')).lower() == 'true'
        self.base_url = config.get('BATTERY_API_URL', 'http://localhost:8088').rstrip('/')
        self.timeout = _to_float(config.get('BATTERY_API_TIMEOUT')) or 5.0
        self.max_data_age = _to_float(config.get('BATTERY_MAX_DATA_AGE')) or 60.0

        self.ac_fallback_voltage = (
            _to_float(config.get('BATTERY_AC_FALLBACK_VOLTAGE'))
            or DEFAULT_AC_FALLBACK_VOLTAGE
        )
        self.ac_discharge_current = (
            _to_float(config.get('BATTERY_AC_DISCHARGE_CURRENT'))
            if config.get('BATTERY_AC_DISCHARGE_CURRENT') not in (None, '')
            else DEFAULT_AC_DISCHARGE_CURRENT
        )
        self.ac_charge_current = (
            _to_float(config.get('BATTERY_AC_CHARGE_CURRENT'))
            if config.get('BATTERY_AC_CHARGE_CURRENT') not in (None, '')
            else DEFAULT_AC_CHARGE_CURRENT
        )

        # Simulation overrides - lets thresholds be exercised without actually
        # draining the battery. See README, "Testing battery integration".
        self.simulation = str(config.get('BATTERY_SIMULATION', 'false')).lower() == 'true'
        self._sim_ac_power = _to_bool(config.get('BATTERY_SIM_AC_POWER'))
        self._sim_soc = _to_float(config.get('BATTERY_SIM_SOC') or None)
        self._sim_voltage = _to_float(config.get('BATTERY_SIM_VOLTAGE') or None)
        self._sim_current = _to_float(config.get('BATTERY_SIM_CURRENT') or None)
        self._sim_remaining = _to_int(config.get('BATTERY_SIM_REMAINING') or None)

        self.last_error: Optional[str] = None

    def get_status(self) -> Optional[BatteryStatus]:
        """Fetch the current battery state.

        Returns:
            BatteryStatus when the data is present, fresh and trustworthy;
            None when the service is unreachable or the reading is stale.
            The caller must treat None as "fall back to sentinels".
        """
        if not self.enabled:
            return None

        payload = self._fetch('/api/v1/status')
        if payload is None:
            return None

        if not payload.get('connected'):
            self.last_error = "VBM reports the BLE device as disconnected"
            log.warning("Battery data rejected: %s", self.last_error)
            return None

        age = self._data_age_seconds(payload.get('last_update'))
        if age is None:
            self.last_error = "VBM returned no last_update timestamp"
            log.warning("Battery data rejected: %s", self.last_error)
            return None

        if age > self.max_data_age:
            self.last_error = (
                f"VBM reading is {age:.0f}s old (max {self.max_data_age:.0f}s)"
            )
            log.warning("Battery data rejected: %s", self.last_error)
            return None

        status = self._build_status(payload, age)
        self._apply_simulation(status)
        self.last_error = None
        return status

    def is_available(self) -> bool:
        """Whether the VBM service itself is up and its BLE link is healthy."""
        if not self.enabled:
            return False
        payload = self._fetch('/api/v1/health')
        if payload is None:
            return False
        return (
            payload.get('status') == 'ok'
            and bool(payload.get('ble', {}).get('connected'))
        )

    def check_health(self) -> dict:
        """Full diagnostic, for the Web GUI 'Test Connection' button."""
        result = {
            'enabled': self.enabled,
            'url': self.base_url,
            'reachable': False,
            'ble_connected': False,
            'data_fresh': False,
            'simulation': self.simulation,
            'error': None,
            'status': None,
        }

        if not self.enabled:
            result['error'] = "Battery integration is disabled (BATTERY_ENABLED)"
            return result

        health = self._fetch('/api/v1/health')
        if health is None:
            result['error'] = self.last_error or "Cannot reach the VBM service"
            return result

        result['reachable'] = True
        result['ble_connected'] = bool(health.get('ble', {}).get('connected'))

        status = self.get_status()
        if status is None:
            result['error'] = self.last_error or "No usable battery reading"
            return result

        result['data_fresh'] = True
        result['status'] = status.to_dict()
        return result

    # --- internals ---

    def _fetch(self, path: str) -> Optional[dict]:
        """GET a JSON document, returning None on any failure."""
        url = f"{self.base_url}{path}"
        try:
            request = urllib.request.Request(
                url, headers={'Accept': 'application/json'}
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    self.last_error = f"HTTP {response.status} from {url}"
                    log.warning("Battery API: %s", self.last_error)
                    return None
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            self.last_error = f"HTTP {e.code} from {url}"
        except urllib.error.URLError as e:
            self.last_error = f"Cannot reach {url}: {e.reason}"
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self.last_error = f"Invalid JSON from {url}: {e}"
        except Exception as e:
            self.last_error = f"Unexpected error querying {url}: {e}"

        log.warning("Battery API: %s", self.last_error)
        return None

    @staticmethod
    def _data_age_seconds(last_update: Any) -> Optional[float]:
        """Age of a reading in seconds, or None if the timestamp is unusable."""
        if not last_update:
            return None
        try:
            text = str(last_update)
            if text.endswith('Z'):
                text = text[:-1] + '+00:00'
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - parsed).total_seconds()
        except (ValueError, TypeError):
            return None

    def _build_status(self, payload: dict, age: float) -> BatteryStatus:
        voltage = _to_float(payload.get('voltage'))
        current = _to_float(payload.get('current'))

        # VBM >= the ac_power release reports mains state itself. Older ones
        # do not, so derive it here rather than refusing to work with them.
        ac_power = _to_bool(payload.get('ac_power'))
        inferred = False
        if 'ac_power' not in payload:
            ac_power = self._infer_ac_power(voltage, current)
            inferred = True

        return BatteryStatus(
            voltage=voltage,
            current=current,
            power=_to_float(payload.get('power')),
            soc=_to_float(payload.get('soc')),
            remaining_mins=_to_int(payload.get('remaining_mins')),
            temperature=_to_float(payload.get('temperature')),
            ac_power=ac_power,
            ac_power_since=payload.get('ac_power_since'),
            connected=True,
            last_update=payload.get('last_update'),
            data_age_seconds=round(age, 1),
            ac_power_inferred=inferred,
        )

    def _infer_ac_power(
        self, voltage: Optional[float], current: Optional[float]
    ) -> Optional[bool]:
        """Single-reading mains guess for VBM instances without `ac_power`.

        Deliberately simpler than ACStateTracker upstream - no hysteresis and
        no debounce, because we only see one reading at a time here. Upgrading
        VBM is the better fix; this just keeps older ones usable.
        """
        if current is not None and current <= self.ac_discharge_current:
            return False
        # Charging current settles mains presence on its own: out of a deep
        # discharge the charger holds terminal voltage well below the fallback
        # threshold for a long time, so voltage alone reports "on battery" long
        # after mains is back. Set BATTERY_AC_CHARGE_CURRENT=0 to disable when
        # an independent DC source (solar/MPPT) can also charge the bank.
        if (self.ac_charge_current and self.ac_charge_current > 0
                and current is not None and current >= self.ac_charge_current):
            return True
        if voltage is None:
            return None
        return voltage >= self.ac_fallback_voltage

    def _apply_simulation(self, status: BatteryStatus) -> None:
        """Overlay configured simulation values onto a real reading."""
        if not self.simulation:
            return

        overrides = {
            'ac_power': self._sim_ac_power,
            'soc': self._sim_soc,
            'voltage': self._sim_voltage,
            'current': self._sim_current,
            'remaining_mins': self._sim_remaining,
        }
        applied = {}
        for field, value in overrides.items():
            if value is not None:
                setattr(status, field, value)
                applied[field] = value

        if applied:
            log.warning("BATTERY SIMULATION active - overriding %s", applied)
