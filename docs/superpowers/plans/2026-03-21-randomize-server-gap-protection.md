# Randomize Server: Gap Protection & Retry Logic — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the "Randomize" tray action retry up to 3 servers on failure, fall back to the previous server, guard against double-triggering, and rely on the user's existing kill switch setting to protect the gap.

**Architecture:** Refactor `connect_to_random_server()` to pre-select up to 3 candidates in the GTK thread (in-memory, before any disconnect), then submit a new `_randomize_with_retry()` async coroutine to the executor. The coroutine tries each candidate in sequence, falls back to the previous server on full failure, and disconnects + raises on total failure. An in-flight guard prevents concurrent randomize calls.

**Tech Stack:** Python 3.9+, `asyncio`, `unittest.mock.AsyncMock`, `pytest`

---

## File Map

| File | Change |
|---|---|
| `proton/vpn/app/gtk/controller.py` | Add `_randomize_future` field; refactor `connect_to_random_server()`; add `_randomize_with_retry()` |
| `tests/unit/test_controller_randomize.py` | New file — all randomize unit tests |

---

### Task 1: In-flight guard on `connect_to_random_server`

**Files:**
- Modify: `proton/vpn/app/gtk/controller.py`
- Create: `tests/unit/test_controller_randomize.py`

- [ ] **Step 1: Create the test file with helpers and the guard test**

```python
# tests/unit/test_controller_randomize.py
import asyncio
import pytest
from concurrent.futures import Future
from unittest.mock import AsyncMock, Mock

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


def test_connect_to_random_server_returns_existing_future_when_in_flight():
    controller = make_controller()

    in_flight = Mock(spec=Future)
    in_flight.done.return_value = False
    controller._randomize_future = in_flight

    result = controller.connect_to_random_server()

    assert result is in_flight
    controller.executor.submit.assert_not_called()
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
pytest tests/unit/test_controller_randomize.py::test_connect_to_random_server_returns_existing_future_when_in_flight -v
```

Expected: `FAILED` — `AttributeError: Controller has no attribute '_randomize_future'` (or similar)

- [ ] **Step 3: Add `_randomize_future` to `Controller.__init__` and the guard to `connect_to_random_server`**

In `proton/vpn/app/gtk/controller.py`, in `__init__` after line 102 (`self._randomize_city`):

```python
self._randomize_future: Optional[Future] = None
```

At the top of `connect_to_random_server()`, before `current_server_id = self.current_server_id`:

```python
# Guard: if a randomize is already in flight, return the existing future
if self._randomize_future is not None and not self._randomize_future.done():
    logger.debug("Randomize already in progress, ignoring duplicate call")
    return self._randomize_future
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
pytest tests/unit/test_controller_randomize.py::test_connect_to_random_server_returns_existing_future_when_in_flight -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add proton/vpn/app/gtk/controller.py tests/unit/test_controller_randomize.py
git commit -m "feat: add in-flight guard to connect_to_random_server"
```

---

### Task 2: `_randomize_with_retry` — success on first candidate

**Files:**
- Modify: `proton/vpn/app/gtk/controller.py`
- Modify: `tests/unit/test_controller_randomize.py`

- [ ] **Step 1: Write the failing test**

```python
def test_randomize_with_retry_connects_to_first_candidate_and_marks_visited():
    connector = Mock()
    connector.connect = AsyncMock()
    connector.get_vpn_server = Mock(return_value=Mock())

    controller = make_controller(connector=connector)
    server = make_server("s1", "Server#1")

    asyncio.run(controller._randomize_with_retry([server], "prev_id", "openvpn-tcp"))

    connector.connect.assert_called_once()
    assert "s1" in controller._randomize_visited
```

- [ ] **Step 2: Run to confirm it fails**

```bash
pytest tests/unit/test_controller_randomize.py::test_randomize_with_retry_connects_to_first_candidate_and_marks_visited -v
```

Expected: `FAILED` — `AttributeError: Controller has no attribute '_randomize_with_retry'`

- [ ] **Step 3: Add minimal `_randomize_with_retry` to `controller.py`**

Add this method to `Controller` (after `connect_to_random_server`):

```python
async def _randomize_with_retry(
    self,
    candidates: list,
    previous_server_id: str,
    protocol: str,
) -> None:
    """
    Tries each candidate server in order. Falls back to the previous server
    if all candidates fail. Disconnects and raises if the fallback also fails.
    """
    for server in candidates:
        vpn_server = self._connector.get_vpn_server(
            server, self._api.refresher.client_config
        )
        try:
            logger.info(
                f"Randomize attempt: {server.name} (load: {server.load}%)",
                category="app", event="randomize_server"
            )
            await self._connector.connect(vpn_server, protocol=protocol)
            self._randomize_visited.add(server.id)
            return
        except Exception:  # noqa: BLE001
            logger.warning(
                f"Randomize: failed to connect to {server.name}, trying next"
            )
            self._randomize_visited.add(server.id)

    # All candidates failed — try fallback to previous server
    logger.warning(
        "Randomize: all candidates failed, falling back to previous server"
    )
    try:
        prev_server = self._api.server_list.get_by_id(previous_server_id)
        vpn_server = self._connector.get_vpn_server(
            prev_server, self._api.refresher.client_config
        )
        await self._connector.connect(vpn_server, protocol=protocol)
        return
    except Exception:  # noqa: BLE001
        logger.error("Randomize: failed to reconnect to previous server")

    # Everything failed — clean up state and disconnect
    logger.error("Randomize: completely failed, disconnecting")
    self._randomize_visited.clear()
    self._randomize_city = None
    await self._connector.disconnect()
    raise RuntimeError("Failed to connect to any server during randomize")
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
pytest tests/unit/test_controller_randomize.py::test_randomize_with_retry_connects_to_first_candidate_and_marks_visited -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add proton/vpn/app/gtk/controller.py tests/unit/test_controller_randomize.py
git commit -m "feat: add _randomize_with_retry coroutine (success path)"
```

---

### Task 3: `_randomize_with_retry` — retry on failure

**Files:**
- Modify: `tests/unit/test_controller_randomize.py`

(No implementation change needed — the coroutine already handles this.)

- [ ] **Step 1: Write the failing tests**

```python
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


def test_randomize_with_retry_marks_failed_candidate_as_visited():
    connector = Mock()
    connector.connect = AsyncMock(
        side_effect=[Exception("fail"), None]
    )
    connector.get_vpn_server = Mock(return_value=Mock())

    controller = make_controller(connector=connector)
    server1 = make_server("s1")
    server2 = make_server("s2")

    asyncio.run(
        controller._randomize_with_retry([server1, server2], "prev", "openvpn-tcp")
    )

    # Both visited: failed candidate s1 and successful s2
    assert "s1" in controller._randomize_visited
    assert "s2" in controller._randomize_visited
```

- [ ] **Step 2: Run to confirm they pass (no code change needed)**

```bash
pytest tests/unit/test_controller_randomize.py::test_randomize_with_retry_tries_next_candidate_on_connection_failure tests/unit/test_controller_randomize.py::test_randomize_with_retry_marks_failed_candidate_as_visited -v
```

Expected: `PASSED` (implementation already handles this)

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_controller_randomize.py
git commit -m "test: add retry-on-failure tests for _randomize_with_retry"
```

---

### Task 4: `_randomize_with_retry` — fallback to previous server

**Files:**
- Modify: `tests/unit/test_controller_randomize.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_randomize_with_retry_falls_back_to_previous_server_when_all_candidates_fail():
    connector = Mock()
    # First call (candidate) fails, second call (previous) succeeds
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
```

- [ ] **Step 2: Run to confirm it passes**

```bash
pytest tests/unit/test_controller_randomize.py::test_randomize_with_retry_falls_back_to_previous_server_when_all_candidates_fail -v
```

Expected: `PASSED`

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_controller_randomize.py
git commit -m "test: add previous-server fallback tests for _randomize_with_retry"
```

---

### Task 5: `_randomize_with_retry` — complete failure paths

**Files:**
- Modify: `tests/unit/test_controller_randomize.py`

- [ ] **Step 1: Write the failing tests**

```python
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
    # State cleared
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
```

- [ ] **Step 2: Run to confirm they pass**

```bash
pytest tests/unit/test_controller_randomize.py::test_randomize_with_retry_disconnects_and_raises_when_all_attempts_fail tests/unit/test_controller_randomize.py::test_randomize_with_retry_disconnects_and_raises_when_previous_server_not_found -v
```

Expected: `PASSED`

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_controller_randomize.py
git commit -m "test: add complete-failure path tests for _randomize_with_retry"
```

---

### Task 6: Refactor `connect_to_random_server` — multi-candidate pre-selection

**Files:**
- Modify: `proton/vpn/app/gtk/controller.py`
- Modify: `tests/unit/test_controller_randomize.py`

- [ ] **Step 1: Write the failing tests**

```python
from unittest.mock import patch


def _make_controller_with_server_list(servers, current_server_id="current"):
    """Helper for tests that need the full server-list path."""
    connector = Mock()
    connector.get_vpn_server = Mock(return_value=Mock())

    controller = make_controller(connector=connector)

    current_server = make_server(current_server_id, city="TestCity", load=30)
    # get_by_id returns the current server on the first call
    controller._api.server_list.get_by_id.return_value = current_server
    controller._api.server_list.logicals = servers
    controller._api.server_list.user_tier = 2

    # Simulate being connected
    controller._connector.current_connection = Mock()

    return controller, current_server


@patch("proton.vpn.app.gtk.controller.Controller.get_settings")
@patch("proton.vpn.app.gtk.controller.ServerList.get_available_servers")
@patch("proton.vpn.app.gtk.controller.ServerList.get_servers_in_city")
@patch("proton.vpn.app.gtk.controller.random.sample")
def test_connect_to_random_server_preselects_up_to_3_candidates(
    mock_sample, mock_get_in_city, mock_get_available, mock_get_settings
):
    # 5 candidate servers (excluding the current one)
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

    with patch.object(controller, "current_server_id", "current"):
        controller.connect_to_random_server()

    # random.sample called with the full pool and cap of 3
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

    with patch.object(controller, "current_server_id", "current"):
        controller.connect_to_random_server()

    # executor.submit should be called with _randomize_with_retry (not _connector.connect)
    controller.executor.submit.assert_called_once()
    submitted_fn = controller.executor.submit.call_args[0][0]
    assert submitted_fn == controller._randomize_with_retry
```

- [ ] **Step 2: Run to confirm they fail**

```bash
pytest tests/unit/test_controller_randomize.py::test_connect_to_random_server_preselects_up_to_3_candidates tests/unit/test_controller_randomize.py::test_connect_to_random_server_submits_randomize_with_retry_coroutine -v
```

Expected: `FAILED` — `connect_to_random_server` still calls `_connect_to_vpn` directly

- [ ] **Step 3: Refactor `connect_to_random_server` to use multi-candidate pre-selection**

Replace the end of `connect_to_random_server()` — from the `# Prefer low-load servers` comment onward — with:

```python
        # Prefer low-load servers
        low_load = [s for s in unvisited if s.load < self.MAX_SERVER_LOAD]
        candidates_pool = low_load if low_load else unvisited

        # Pre-select up to 3 candidates in the GTK thread (in-memory, before any disconnect)
        num = min(3, len(candidates_pool))
        candidates = random.sample(candidates_pool, num)

        # Fetch protocol here (GTK thread) — same pattern as _connect_to_vpn.
        # Doing it inside the coroutine would deadlock the executor loop.
        protocol = self.get_settings().protocol

        self._randomize_future = self.executor.submit(
            self._randomize_with_retry, candidates, current_server_id, protocol
        )
        return self._randomize_future
```

Also remove the old `server = random.choice(candidates)` and `logger.info` and `return self._connect_to_vpn(server)` lines.

- [ ] **Step 4: Run the new tests**

```bash
pytest tests/unit/test_controller_randomize.py::test_connect_to_random_server_preselects_up_to_3_candidates tests/unit/test_controller_randomize.py::test_connect_to_random_server_submits_randomize_with_retry_coroutine -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add proton/vpn/app/gtk/controller.py tests/unit/test_controller_randomize.py
git commit -m "feat: refactor connect_to_random_server to use multi-candidate pre-selection"
```

---

### Task 7: Run the full test suite and fix any regressions

- [ ] **Step 1: Run all unit tests**

```bash
pytest tests/unit/ -v
```

Expected: All tests pass. If any existing tests fail due to the refactor (e.g., tests that mock `_connect_to_vpn` or `random.choice`), fix them now.

Common breakages to look for:
- Any test that expects `executor.submit` to be called with `_connector.connect` — update to expect `_randomize_with_retry`
- Any test that patches `random.choice` — update to patch `random.sample`

- [ ] **Step 2: Run the full test suite including coverage**

```bash
pytest
```

- [ ] **Step 3: Commit any fixes**

```bash
git add -p  # stage only the regression fixes
git commit -m "test: fix regressions from randomize refactor"
```

---

### Task 8: Kill switch gap testing — manual verification

This is a manual test, not automated. No code changes. Document the result.

- [ ] **Step 1: Build/run the app locally**

```bash
python -m proton.vpn.app.gtk
```

- [ ] **Step 2: Enable kill switch** (Settings → Kill switch → Standard or Permanent)

- [ ] **Step 3: Connect to a VPN server**

- [ ] **Step 4: Start a connectivity probe in a terminal**

```bash
# This pings 8.8.8.8 once per second and timestamps each reply
ping -i 1 -D 8.8.8.8
```

- [ ] **Step 5: In a second terminal, watch for non-VPN traffic** (optional but thorough)

```bash
# Replace eth0 with your real interface name (ip link to find it)
sudo tcpdump -i eth0 icmp -n
```

- [ ] **Step 6: Click Randomize from the tray**

- [ ] **Step 7: Observe results**

| Kill switch setting | Expected behaviour during gap |
|---|---|
| `OFF` | ping replies may continue (no blocking) |
| `ON` or `PERMANENT` | ping requests time out during the gap; `tcpdump` on `eth0` shows no ICMP traffic |

- [ ] **Step 8: Verify retry behaviour** — temporarily simulate a connect failure by killing the network interface during the randomize, and confirm the app retries and eventually surfaces an error dialog

---

## Spec Reference

`docs/superpowers/specs/2026-03-21-randomize-server-gap-protection-design.md`
