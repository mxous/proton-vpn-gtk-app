"""
System suspend/resume monitoring.


Copyright (c) 2023 Proton AG

This file is part of Proton VPN.

Proton VPN is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

Proton VPN is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with ProtonVPN.  If not, see <https://www.gnu.org/licenses/>.
"""
from typing import Callable, Optional

import dbus
from dbus import SystemBus
from dbus.mainloop.glib import DBusGMainLoop

DBusGMainLoop(set_as_default=True)

BUS_NAME = "org.freedesktop.login1"
OBJECT_PATH = "/org/freedesktop/login1"
MANAGER_INTERFACE = "org.freedesktop.login1.Manager"
PREPARE_FOR_SLEEP_SIGNAL = "PrepareForSleep"


class SleepMonitor:
    """
    After being enabled, it calls the callback set on the resumed_callback
    attribute whenever the system resumes from suspend.

    It relies on logind's PrepareForSleep signal, which is emitted with True
    right before the system suspends and with False right after it resumes.

    Attributes:
        resumed_callback: callable that will be called when the system
        resumes from suspend.
    """

    def __init__(self, bus: Optional[SystemBus] = None):
        self._bus = bus
        self._signal_receiver: Optional[object] = None
        self.resumed_callback: Optional[Callable] = None

    def enable(self):
        """Enables suspend/resume monitoring."""
        if not callable(self.resumed_callback):
            raise RuntimeError("Callback was not set")

        try:
            if self._bus is None:
                self._bus = SystemBus()
            self._signal_receiver = self._bus.add_signal_receiver(
                handler_function=self._on_prepare_for_sleep,
                signal_name=PREPARE_FOR_SLEEP_SIGNAL,
                dbus_interface=MANAGER_INTERFACE,
                bus_name=BUS_NAME,
                path=OBJECT_PATH,
            )
        except dbus.exceptions.DBusException:
            # logind is inaccessible (e.g. AppArmor in strict snap confinement).
            # Randomize-on-resume won't work, but everything else is fine.
            pass

    def disable(self):
        """Disables suspend/resume monitoring."""
        if self._signal_receiver:
            self._signal_receiver.remove()
            self._signal_receiver = None

    def _on_prepare_for_sleep(self, start: bool):
        if not start:
            self.resumed_callback()

    def set_signal_receiver(self, new_object: Optional[object]):
        """Sets signal receiver.
        This is mainly used for testing purposes.
        """
        self._signal_receiver = new_object
