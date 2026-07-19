"""
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
from unittest.mock import Mock

import dbus
import pytest

from proton.vpn.app.gtk.services.reconnector.sleep_monitor import SleepMonitor


def test_enable_hooks_prepare_for_sleep_signal():
    bus_mock = Mock()
    sleep_monitor = SleepMonitor(bus=bus_mock)
    sleep_monitor.resumed_callback = Mock()

    sleep_monitor.enable()

    bus_mock.add_signal_receiver.assert_called_once()
    kwargs = bus_mock.add_signal_receiver.call_args.kwargs
    assert kwargs["signal_name"] == "PrepareForSleep"
    assert kwargs["dbus_interface"] == "org.freedesktop.login1.Manager"
    assert kwargs["bus_name"] == "org.freedesktop.login1"
    assert kwargs["path"] == "/org/freedesktop/login1"


def test_enable_raises_runtime_error_if_callback_is_not_set():
    sleep_monitor = SleepMonitor(bus=Mock())

    with pytest.raises(RuntimeError):
        sleep_monitor.enable()


def test_enable_ignores_dbus_unavailable_error():
    bus_mock = Mock()
    bus_mock.add_signal_receiver.side_effect = dbus.exceptions.DBusException
    sleep_monitor = SleepMonitor(bus=bus_mock)
    sleep_monitor.resumed_callback = Mock()

    sleep_monitor.enable()

    assert sleep_monitor._signal_receiver is None


def test_resumed_callback_is_called_only_on_resume():
    bus_mock = Mock()
    callback_mock = Mock()
    sleep_monitor = SleepMonitor(bus=bus_mock)
    sleep_monitor.resumed_callback = callback_mock
    sleep_monitor.enable()

    handler = bus_mock.add_signal_receiver.call_args.kwargs["handler_function"]

    handler(True)  # PrepareForSleep(True): about to suspend.
    callback_mock.assert_not_called()

    handler(False)  # PrepareForSleep(False): resumed.
    callback_mock.assert_called_once()


def test_disable_unhooks_prepare_for_sleep_signal():
    signal_receiver_mock = Mock()
    sleep_monitor = SleepMonitor(bus=Mock())
    sleep_monitor.set_signal_receiver(signal_receiver_mock)

    sleep_monitor.disable()

    signal_receiver_mock.remove.assert_called_once()


def test_disable_does_not_unhook_if_not_previously_enabled():
    signal_receiver_mock = Mock()
    sleep_monitor = SleepMonitor(bus=Mock())

    sleep_monitor.disable()

    signal_receiver_mock.remove.assert_not_called()
