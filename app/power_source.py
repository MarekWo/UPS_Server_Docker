#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Per-host power state evaluation.

The server has two ways of telling whether mains is gone:

  sentinel  Ping devices that sit on grid power. Cheap and hardware-free, but
            it cannot distinguish a power cut from a network failure, and it
            says nothing about how much runtime is left.

  battery   Read a Victron battery monitor. Knows whether the battery is being
            charged or discharged, and by how much - so each host can be shut
            down at its own state of charge instead of on a shared timer.

Which one applies is configured per host, because hosts differ in how much
they can afford to be wrong. The battery source always degrades to sentinel
when battery data is missing or stale; it never fails closed.

The evaluator is deliberately pure - no I/O, no files, no clock. That keeps
the shutdown rules, which are the part that can cost you a filesystem, easy
to read and to test.
"""

import logging
from dataclasses import dataclass, asdict
from typing import Optional

log = logging.getLogger("PowerManager")

SOURCE_SENTINEL = 'sentinel'
SOURCE_BATTERY = 'battery'
SOURCE_BOTH = 'both'
VALID_SOURCES = (SOURCE_SENTINEL, SOURCE_BATTERY, SOURCE_BOTH)

MODE_OBSERVE = 'observe'
MODE_ENFORCE = 'enforce'

STATUS_ONLINE = 'OL'
STATUS_ON_BATTERY = 'OB'
STATUS_LOW_BATTERY = 'OB LB'

# Applied when neither the host nor the global config specifies a threshold.
#
# Sized for a 12V lead-acid bank so that state of charge stays the rule that
# actually fires and voltage is only the backstop for when SoC is unavailable
# or wrong. The voltage figures are deliberately low: a real measurement on a
# ~100Ah bank showed 13.78V at rest collapsing to 12.23V the instant a 56A load
# moved onto the battery, at 99% SoC. An 11.8V threshold sits only 0.43V under
# that, so under a heavy load it would trip somewhere around 45-55% SoC - ahead
# of the SoC rule, and simultaneously for every host, which would defeat the
# per-host thresholds this module exists to provide.
#
# These are estimates from a single load point. Mapping loaded voltage against
# SoC on your own bank is the only way to place them properly.
BUILTIN_DEFAULTS = {
    'CRITICAL_VOLTAGE': 11.0,
    'SHUTDOWN_VOLTAGE': 11.4,
    'SHUTDOWN_SOC': 25.0,
    'MIN_RUNTIME_MINUTES': 0.0,   # 0 = rule disabled
    'WOL_MIN_SOC': 0.0,           # 0 = rule disabled
}


@dataclass
class HostVerdict:
    """What a single host should be told, and why."""

    status: str            # what GET /upsc reports now - only OL or OB LB
    detail: str            # the true NUT state: OL, OB or OB LB
    source: str            # which source actually drove the decision
    reason: str            # human-readable justification
    mode: str              # observe or enforce
    would_be: str          # what enforce mode would have returned
    disagreement: bool     # battery and sentinels tell different stories

    def to_dict(self) -> dict:
        return asdict(self)


def _to_float(value) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PowerSourceEvaluator:
    """Turns raw sentinel and battery readings into a per-host verdict."""

    def __init__(self, config: dict):
        self.config = config

        mode = str(config.get('BATTERY_DECISION_MODE', MODE_OBSERVE)).strip().lower()
        if mode not in (MODE_OBSERVE, MODE_ENFORCE):
            log.warning(
                "Invalid BATTERY_DECISION_MODE '%s' - falling back to '%s'",
                mode, MODE_OBSERVE,
            )
            mode = MODE_OBSERVE
        self.mode = mode

        default_source = str(
            config.get('BATTERY_DEFAULT_POWER_SOURCE', SOURCE_SENTINEL)
        ).strip().lower()
        if default_source not in VALID_SOURCES:
            log.warning(
                "Invalid BATTERY_DEFAULT_POWER_SOURCE '%s' - falling back to '%s'",
                default_source, SOURCE_SENTINEL,
            )
            default_source = SOURCE_SENTINEL
        self.default_source = default_source

    # --- configuration resolution ---

    def host_source(self, host: dict) -> str:
        """Which power source this host is configured to use."""
        source = str(host.get('POWER_SOURCE', self.default_source)).strip().lower()
        if source not in VALID_SOURCES:
            log.warning(
                "Host '%s' has invalid POWER_SOURCE '%s' - using '%s'",
                host.get('NAME', host.get('IP', '?')), source, SOURCE_SENTINEL,
            )
            return SOURCE_SENTINEL
        return source

    def threshold(self, host: dict, key: str) -> float:
        """Resolve a threshold: per-host value, else global default, else built-in."""
        value = _to_float(host.get(key))
        if value is None:
            value = _to_float(self.config.get(f'BATTERY_DEFAULT_{key}'))
        if value is None:
            value = BUILTIN_DEFAULTS[key]
        return value

    # --- evaluation ---

    def evaluate_host(
        self,
        host: dict,
        battery,
        sentinel_offline: bool,
        simulation: bool,
    ) -> HostVerdict:
        """Decide what this host should be told.

        Args:
            host: A [WAKE_HOST_N] section as a dict.
            battery: BatteryStatus, or None when no trustworthy reading exists.
            sentinel_offline: True when every sentinel host is unreachable.
            simulation: True when power outage simulation is active.
        """
        source = self.host_source(host)

        # Simulation wins over everything except an explicit opt-out. Keeping
        # this first preserves the existing contract exactly: a simulated
        # outage must still exercise the real shutdown path.
        if simulation:
            if str(host.get('IGNORE_SIMULATION', 'false')).lower() == 'true':
                return self._verdict(
                    STATUS_ONLINE, STATUS_ONLINE, source,
                    "power outage simulation active, but this host ignores it",
                    sentinel_offline, authoritative=True,
                )
            return self._verdict(
                STATUS_LOW_BATTERY, STATUS_LOW_BATTERY, source,
                "power outage simulation active", sentinel_offline,
                authoritative=True,
            )

        if source == SOURCE_SENTINEL:
            return self._sentinel_verdict(sentinel_offline, SOURCE_SENTINEL)

        # Battery sources fall back rather than fail closed.
        if battery is None:
            return self._sentinel_verdict(
                sentinel_offline, SOURCE_SENTINEL,
                prefix="no battery data - fell back to sentinel hosts: ",
            )
        if battery.ac_power is None:
            return self._sentinel_verdict(
                sentinel_offline, SOURCE_SENTINEL,
                prefix="battery monitor cannot determine mains state - "
                       "fell back to sentinel hosts: ",
            )

        on_battery = battery.ac_power is False

        if source == SOURCE_BOTH and on_battery and not sentinel_offline:
            # The battery says mains is gone but the sentinels are still up.
            # Requiring both to agree is the whole point of this mode.
            return self._verdict(
                STATUS_ONLINE, STATUS_ONLINE, SOURCE_BOTH,
                "battery reports mains lost but a sentinel host is still "
                "reachable - waiting for both sources to agree",
                sentinel_offline,
            )

        if not on_battery:
            if source == SOURCE_BOTH and sentinel_offline:
                # Sentinels unreachable while the battery is charging: almost
                # certainly a network fault, which is exactly the false alarm
                # this integration exists to prevent.
                return self._verdict(
                    STATUS_ONLINE, STATUS_ONLINE, SOURCE_BOTH,
                    "all sentinel hosts unreachable but the battery is on mains - "
                    "treating this as a network fault, not a power cut",
                    sentinel_offline,
                )
            return self._verdict(
                STATUS_ONLINE, STATUS_ONLINE, SOURCE_BATTERY,
                f"mains present ({battery.describe()})", sentinel_offline,
            )

        return self._battery_threshold_verdict(host, battery, sentinel_offline)

    def _battery_threshold_verdict(
        self, host: dict, battery, sentinel_offline: bool
    ) -> HostVerdict:
        """Running on battery: decide whether this host has run out of margin."""
        critical_voltage = self.threshold(host, 'CRITICAL_VOLTAGE')
        shutdown_voltage = self.threshold(host, 'SHUTDOWN_VOLTAGE')
        shutdown_soc = self.threshold(host, 'SHUTDOWN_SOC')
        min_runtime = self.threshold(host, 'MIN_RUNTIME_MINUTES')

        # Ordered by how conclusive each signal is. Voltage under load is the
        # last honest warning before the inverter cuts out, so it goes first.
        if battery.voltage is not None and battery.voltage <= critical_voltage:
            return self._shutdown(
                f"battery voltage {battery.voltage:.2f}V at or below "
                f"critical {critical_voltage:.2f}V", sentinel_offline,
            )

        if battery.soc is not None and battery.soc <= shutdown_soc:
            return self._shutdown(
                f"state of charge {battery.soc:.1f}% at or below "
                f"threshold {shutdown_soc:.1f}%", sentinel_offline,
            )

        if battery.voltage is not None and battery.voltage <= shutdown_voltage:
            return self._shutdown(
                f"battery voltage {battery.voltage:.2f}V at or below "
                f"threshold {shutdown_voltage:.2f}V", sentinel_offline,
            )

        if (min_runtime > 0 and battery.remaining_mins is not None
                and battery.remaining_mins <= min_runtime):
            return self._shutdown(
                f"estimated runtime {battery.remaining_mins} min at or below "
                f"threshold {min_runtime:.0f} min", sentinel_offline,
            )

        # On battery, but with margin to spare - keep the host running.
        margin = []
        if battery.soc is not None:
            margin.append(f"SoC {battery.soc:.1f}% > {shutdown_soc:.1f}%")
        if battery.voltage is not None:
            margin.append(f"{battery.voltage:.2f}V > {shutdown_voltage:.2f}V")
        if battery.remaining_mins is not None:
            margin.append(f"~{battery.remaining_mins} min left")

        return self._verdict(
            STATUS_ONLINE, STATUS_ON_BATTERY, SOURCE_BATTERY,
            "on battery, thresholds not reached ({})".format(
                ", ".join(margin) if margin else "no threshold data"
            ),
            sentinel_offline,
        )

    def should_wol(self, host: dict, battery) -> tuple:
        """Whether it is safe to wake this host yet.

        Returns:
            (allowed: bool, reason: str)
        """
        min_soc = self.threshold(host, 'WOL_MIN_SOC')
        if min_soc <= 0:
            return True, "no minimum state of charge configured"

        if self.host_source(host) == SOURCE_SENTINEL:
            return True, "host does not use the battery monitor"

        if battery is None:
            return True, "no battery data - not holding the wake-up back"

        if battery.soc is None:
            return True, "battery monitor reports no state of charge"

        if battery.soc < min_soc:
            return False, (
                f"state of charge {battery.soc:.1f}% below required {min_soc:.1f}%"
            )

        # Voltage is deliberately not a gate here: right after mains returns the
        # charger pushes it to 14.2-14.4V even at 40% SoC, so it would wave
        # through exactly the case we are trying to avoid. Current sign is a
        # sound check though - it confirms the charger really is running.
        if battery.current is not None and battery.current < 0:
            return False, (
                f"state of charge {battery.soc:.1f}% is sufficient but the battery "
                f"is still discharging ({battery.current:+.2f}A)"
            )

        return True, f"state of charge {battery.soc:.1f}% at or above {min_soc:.1f}%"

    # --- verdict construction ---

    def _shutdown(self, reason: str, sentinel_offline: bool) -> HostVerdict:
        return self._verdict(
            STATUS_LOW_BATTERY, STATUS_LOW_BATTERY, SOURCE_BATTERY,
            reason, sentinel_offline,
        )

    def _sentinel_verdict(
        self, sentinel_offline: bool, source: str, prefix: str = ""
    ) -> HostVerdict:
        if sentinel_offline:
            return self._verdict(
                STATUS_LOW_BATTERY, STATUS_LOW_BATTERY, source,
                prefix + "all sentinel hosts are offline", sentinel_offline,
                authoritative=True,
            )
        return self._verdict(
            STATUS_ONLINE, STATUS_ONLINE, source,
            prefix + "at least one sentinel host is reachable", sentinel_offline,
            authoritative=True,
        )

    def _verdict(
        self,
        status: str,
        detail: str,
        source: str,
        reason: str,
        sentinel_offline: bool,
        authoritative: bool = False,
    ) -> HostVerdict:
        """Apply the decision mode and record any source disagreement.

        `authoritative` marks verdicts that observe mode must not second-guess:
        the simulation override and anything already derived from the sentinels.
        Observe mode exists to withhold control from the *battery* rules, not to
        break power outage simulation or the pre-existing sentinel behaviour.
        """
        sentinel_status = STATUS_LOW_BATTERY if sentinel_offline else STATUS_ONLINE
        disagreement = (not authoritative) and status != sentinel_status

        # In observe mode the battery rules run in full and are logged and
        # published, but do not steer anything: clients keep seeing exactly what
        # the sentinels imply. That is what makes it safe to enable on a live
        # system and compare the two sources for a few days before handing over.
        if authoritative or self.mode == MODE_ENFORCE:
            served = status
        else:
            served = sentinel_status

        return HostVerdict(
            status=served,
            detail=detail,
            source=source,
            reason=reason,
            mode=self.mode,
            would_be=status,
            disagreement=disagreement,
        )
