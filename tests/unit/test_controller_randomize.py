# tests/unit/test_controller_randomize.py
import asyncio
import pytest
from concurrent.futures import Future
from unittest.mock import AsyncMock, Mock, patch, PropertyMock

from proton.vpn.app.gtk.controller import Controller


def make_controller(connector=None):
    return Controller(
        executor=Mock(),
        exception_handler=Mock(),
        api=Mock(),
        vpn_reconnector=Mock(),
        app_config=Mock(),
        vpn_connector=connector or Mock(),
    )


def make_server(server_id, name="TestServer", city="TestCity", load=10):
    s = Mock()
    s.id = server_id
    s.name = name
    s.city = city
    s.load = load
    return s


def test_randomize_with_retry_connects_to_first_candidate_and_marks_visited():
    connector = Mock()
    connector.connect = AsyncMock()
    connector.get_vpn_server = Mock(return_value=Mock())

    controller = make_controller(connector=connector)
    server = make_server("s1", "Server#1")

    asyncio.run(controller._randomize_with_retry([server], "prev_id", "openvpn-tcp"))

    connector.connect.assert_called_once()
    assert "s1" in controller._randomize_visited


def test_randomize_with_retry_skips_candidate_when_get_vpn_server_raises():
    connector = Mock()
    connector.get_vpn_server = Mock(side_effect=[Exception("bad server"), Mock()])
    connector.connect = AsyncMock()

    controller = make_controller(connector=connector)
    server1 = make_server("s1")
    server2 = make_server("s2")

    asyncio.run(
        controller._randomize_with_retry([server1, server2], "prev", "openvpn-tcp")
    )

    # s1 skipped (get_vpn_server raised), s2 connected
    assert "s1" in controller._randomize_visited
    connector.connect.assert_called_once()


def test_connect_to_random_server_returns_existing_future_when_in_flight():
    controller = make_controller()

    in_flight = Mock(spec=Future)
    in_flight.done.return_value = False
    controller._randomize_future = in_flight

    result = controller.connect_to_random_server()

    assert result is in_flight
    controller.executor.submit.assert_not_called()
    assert controller._randomize_future is in_flight  # state was not mutated


# Task 3: retry on failure
def test_randomize_with_retry_tries_next_candidate_on_connection_failure():
    connector = Mock()
    connector.connect = AsyncMock(
        side_effect=[Exception("connect failed"), None]
    )
    connector.get_vpn_server = Mock(return_value=Mock())

    controller = make_controller(connector=connector)
    server1 = make_server("s1", "Server#1")
    server2 = make_server("s2", "Server#2")

    asyncio.run(
        controller._randomize_with_retry([server1, server2], "prev_id", "openvpn-tcp")
    )

    assert connector.connect.call_count == 2
    assert "s1" in controller._randomize_visited
    assert "s2" in controller._randomize_visited


# Task 4: fallback to previous server
def test_randomize_with_retry_falls_back_to_previous_server_when_all_candidates_fail():
    connector = Mock()
    connector.connect = AsyncMock(side_effect=[Exception("fail"), None])
    connector.get_vpn_server = Mock(return_value=Mock())

    controller = make_controller(connector=connector)
    prev_server = make_server("prev", "PrevServer")
    controller._api.server_list.get_by_id.return_value = prev_server

    server1 = make_server("s1")
    asyncio.run(
        controller._randomize_with_retry([server1], "prev", "openvpn-tcp")
    )

    assert connector.connect.call_count == 2
    controller._api.server_list.get_by_id.assert_called_with("prev")


# Task 5: complete failure paths
def test_randomize_with_retry_disconnects_and_raises_when_all_attempts_fail():
    connector = Mock()
    connector.connect = AsyncMock(side_effect=Exception("fail"))
    connector.disconnect = AsyncMock()
    connector.get_vpn_server = Mock(return_value=Mock())

    controller = make_controller(connector=connector)
    controller._api.server_list.get_by_id.return_value = make_server("prev")
    controller._randomize_city = "SomeCity"
    controller._randomize_visited = {"s0"}

    with pytest.raises(RuntimeError):
        asyncio.run(
            controller._randomize_with_retry(
                [make_server("s1")], "prev", "openvpn-tcp"
            )
        )

    connector.disconnect.assert_called_once()
    assert controller._randomize_visited == set()
    assert controller._randomize_city is None


def test_randomize_with_retry_disconnects_and_raises_when_previous_server_not_found():
    from proton.vpn.session.exceptions import ServerNotFoundError

    connector = Mock()
    connector.connect = AsyncMock(side_effect=Exception("fail"))
    connector.disconnect = AsyncMock()
    connector.get_vpn_server = Mock(return_value=Mock())

    controller = make_controller(connector=connector)
    controller._api.server_list.get_by_id.side_effect = ServerNotFoundError(
        "server removed"
    )

    with pytest.raises(RuntimeError):
        asyncio.run(
            controller._randomize_with_retry(
                [make_server("s1")], "prev", "openvpn-tcp"
            )
        )

    connector.disconnect.assert_called_once()


@patch("proton.vpn.app.gtk.controller.Controller.get_settings")
@patch("proton.vpn.app.gtk.controller.ServerList.get_available_servers")
@patch("proton.vpn.app.gtk.controller.ServerList.get_servers_in_city")
@patch("proton.vpn.app.gtk.controller.random.sample")
def test_connect_to_random_server_preselects_up_to_3_candidates(
    mock_sample, mock_get_in_city, mock_get_available, mock_get_settings
):
    candidates_pool = [make_server(f"s{i}", load=10) for i in range(5)]
    selected = candidates_pool[:3]

    mock_get_in_city.return_value = candidates_pool
    mock_get_available.return_value = candidates_pool
    mock_sample.return_value = selected
    mock_get_settings.return_value.protocol = "openvpn-tcp"

    controller = make_controller()
    controller._api.server_list.get_by_id.return_value = make_server(
        "current", city="TestCity"
    )
    controller._api.server_list.logicals = candidates_pool
    controller._api.server_list.user_tier = 2

    with patch.object(Controller, "current_server_id", new_callable=PropertyMock, return_value="current"):
        controller.connect_to_random_server()

    mock_sample.assert_called_once()
    call_args = mock_sample.call_args
    assert call_args[0][1] == 3  # num = min(3, len(pool))


@patch("proton.vpn.app.gtk.controller.Controller.get_settings")
@patch("proton.vpn.app.gtk.controller.ServerList.get_available_servers")
@patch("proton.vpn.app.gtk.controller.ServerList.get_servers_in_city")
def test_connect_to_random_server_submits_randomize_with_retry_coroutine(
    mock_get_in_city, mock_get_available, mock_get_settings
):
    candidates_pool = [make_server(f"s{i}", load=10) for i in range(2)]
    mock_get_in_city.return_value = candidates_pool
    mock_get_available.return_value = candidates_pool
    mock_get_settings.return_value.protocol = "openvpn-tcp"

    controller = make_controller()
    controller._api.server_list.get_by_id.return_value = make_server(
        "current", city="TestCity"
    )
    controller._api.server_list.logicals = candidates_pool
    controller._api.server_list.user_tier = 2

    with patch.object(Controller, "current_server_id", new_callable=PropertyMock, return_value="current"):
        controller.connect_to_random_server()

    controller.executor.submit.assert_called_once()
    submitted_fn = controller.executor.submit.call_args[0][0]
    assert submitted_fn == controller._randomize_with_retry
