# Randomize Server: Gap Protection & Retry Logic

**Date:** 2026-03-21
**Branch:** editz

## Summary

When the user triggers "Randomize" from the tray icon, there is a brief window between disconnecting from the current server and connecting to the new one during which traffic could escape if no kill switch is active. This spec adds retry-on-failure logic, minimises the gap between disconnect and reconnect, and guards against double-triggering.

## Goals

- Minimise the disconnect→reconnect gap during a randomize operation
- Try up to 3 different servers in total on failure
- If all 3 fail, attempt to reconnect to the previous server
- If that also fails, disconnect and surface an error
- Guard against concurrent randomize calls (double-click / rapid re-trigger)
- Do not manipulate kill switch state — rely on the user's existing setting

## Non-Goals

- Temporarily enabling kill switch for users who have it OFF
- Upgrading ON to PERMANENT during transition
- Changing any other connect path (quick connect, pinned servers, etc.)

## Kill Switch Behaviour

The kill switch is implemented in `proton-vpn-network-manager`. At the OS/NetworkManager level:
- `PERMANENT` — blocks all non-VPN traffic unconditionally; gap is fully protected
- `ON` (Standard) — expected to block traffic when the VPN interface goes down, regardless of whether the disconnect was intentional; gap is likely protected but depends on the NM backend implementation
- `OFF` — no protection; no change in behaviour from today

No kill switch state changes are made during the randomize operation.

## Design

### Async model

`AsyncExecutor.submit()` detects coroutine functions via `inspect.iscoroutinefunction` and schedules them via `asyncio.run_coroutine_threadsafe`. `_connector.connect` is submitted via this executor in all existing connect paths and is expected to be a coroutine function — it can be `await`-ed directly from within `_randomize_with_retry` on the same loop.

**Implicit disconnect:** `_connector.connect` handles teardown of the current connection internally before establishing the new one (confirmed by the reconnector usage pattern). No explicit disconnect call is needed before each attempt.

**`get_settings()` deadlock:** `get_settings()` calls `executor.submit(load_settings).result()`. Since `load_settings` is a coroutine, calling `.result()` from inside the asyncio loop would deadlock. Protocol is therefore fetched in `connect_to_random_server()` (GTK thread) before submitting — the same pattern used in `_connect_to_vpn`.

### Controller (`controller.py`)

**New instance variable** in `__init__`:
```python
self._randomize_future: Optional[Future] = None
```

`connect_to_random_server()` is refactored:

```
connect_to_random_server():
    # Guard: if a randomize is already in flight, return the existing future
    if self._randomize_future is not None and not self._randomize_future.done():
        logger.debug("Randomize already in progress, ignoring duplicate call")
        return self._randomize_future

    [existing not-connected / no-city fallback — unchanged]
    [existing city/visited candidate selection — unchanged]

    # Pre-select up to 3 total candidates (in-memory, no I/O)
    num = min(3, len(candidates_pool))
    candidates = random.sample(candidates_pool, num)

    protocol = self.get_settings().protocol   # GTK thread — same pattern as _connect_to_vpn

    self._randomize_future = self.executor.submit(
        self._randomize_with_retry, candidates, current_server_id, protocol
    )
    return self._randomize_future
```

New `async def _randomize_with_retry(candidates, previous_server_id, protocol)`:

```
for server in candidates:
    vpn_server = self._connector.get_vpn_server(server, self._api.refresher.client_config)
    try:
        logger.info(f"Randomize attempt: {server.name} (load: {server.load}%)",
                    category="app", event="randomize_server")
        await self._connector.connect(vpn_server, protocol=protocol)
        self._randomize_visited.add(server.id)   # mark visited on success too
        return
    except Exception:
        logger.warning(f"Randomize: failed to connect to {server.name}, trying next")
        self._randomize_visited.add(server.id)

# All candidates failed — try fallback to previous server
logger.warning("Randomize: all candidates failed, falling back to previous server")
try:
    prev_server = self._api.server_list.get_by_id(previous_server_id)
    vpn_server = self._connector.get_vpn_server(prev_server, self._api.refresher.client_config)
    await self._connector.connect(vpn_server, protocol=protocol)
    return
except Exception:   # catches ServerNotFoundError and connect failures
    logger.error("Randomize: failed to reconnect to previous server")

# Everything failed — clean up and raise
logger.error("Randomize: completely failed, disconnecting")
self._randomize_visited.clear()
self._randomize_city = None
await self._connector.disconnect()
raise RuntimeError("Failed to connect to any server during randomize")
```

The final failure path calls `_connector.disconnect()` directly (not `self.disconnect()`) to avoid submitting to the executor from within a coroutine already running on it. State is cleared inline.

`ServerNotFoundError` (from `proton.vpn.session.exceptions`) is caught by the broad `except Exception` in the fallback block — no additional import needed for the coroutine itself; the `ExceptionHandler` already handles it at the UI layer.

### Threading / state safety

`_randomize_visited` and `_randomize_city` are mutated in the GTK thread (before `executor.submit`) and inside the coroutine (after submission). These do not overlap for a single randomize call. The `_randomize_future` guard prevents a second call from mutating these fields while the coroutine is still running. No lock required.

### Gap minimisation

All candidate selection and `protocol` fetch happen in the GTK thread before any disconnect begins — entirely in-memory, no network calls. `get_vpn_server()` is a pure data transformation. There is no Python-layer delay between the point where `_connector.connect` begins and the start of the NM disconnect+reconnect cycle. Subsequent retry attempts start immediately after the previous exception is caught. The remaining gap is bounded by NetworkManager's own disconnect+reconnect cycle.

### Error surfacing

No changes to `TrayIndicator._on_randomize_entry_clicked`. The existing callback:

```python
future.add_done_callback(lambda f: GLib.idle_add(f.result))
```

re-raises on the GTK thread, caught by `ExceptionHandler` which shows a generic error dialog.

## Kill Switch Gap Testing Procedure

To verify that the kill switch actually blocks traffic during the reconnect gap, use the following manual test procedure. It requires kill switch set to `ON` or `PERMANENT` in the app settings.

### Setup

```bash
# Terminal 1: continuous connectivity probe (1 ping per second to a public DNS server)
ping -i 1 8.8.8.8
```

```bash
# Terminal 2: monitor outbound packets at the OS level (requires root)
# Watch for any non-VPN (non-tun/proton interface) outbound traffic
sudo nftables -a list ruleset   # or:
sudo iptables -nvL OUTPUT       # check packet counts before/after
```

### Procedure

1. Connect to any server and confirm VPN is active (`tun0` or equivalent interface is up)
2. Start the ping probe in Terminal 1 — note it is receiving replies via the VPN
3. Click **Randomize** from the tray
4. Observe Terminal 1 during the reconnect gap:
   - **Kill switch working:** ping requests time out during the gap (no replies), resume after reconnect
   - **Kill switch not working:** ping replies continue uninterrupted through the gap (traffic leaking via real interface)
5. Optionally capture with `tcpdump -i <real-interface> icmp` to confirm no packets leave the real interface during the gap

### Automated integration test (future)

An automated variant can be added to the `behave` integration suite:
- Use a mock or loopback interface to simulate the VPN
- Use `nftables` counters or packet capture to assert zero non-VPN packets during the state transition
- Assert `Disconnecting → Disconnected → Connecting → Connected` state sequence fires as expected

## Edge Cases

| Scenario | Behaviour |
|---|---|
| Not currently connected | Falls back to `connect_to_fastest_server()` — unchanged |
| Current server has no city | Falls back to `connect_to_fastest_server()` — unchanged |
| Fewer than 3 servers in pool | `random.sample(pool, min(3, len(pool)))` — all available tried, then fallback |
| Previous server removed from list | `ServerNotFoundError` caught by `except Exception`, proceeds to disconnect + error |
| Randomize triggered while in-flight | `_randomize_future` guard returns existing future, no-op |
| `_connector.connect` is not a coroutine | Use `await asyncio.wrap_future(executor.submit(...))` — verify at implementation time |

## Files Changed

- `proton/vpn/app/gtk/controller.py` — add `_randomize_future` field, refactor `connect_to_random_server()`, add `_randomize_with_retry()`

## Testing

Unit tests for `_randomize_with_retry()`:
- Succeeds on first candidate
- Fails first candidate, succeeds on second
- All 3 candidates fail, previous server fallback succeeds
- All 3 candidates fail, previous server fallback fails → disconnect called + error raised
- All 3 candidates fail, `get_by_id` raises `ServerNotFoundError` → disconnect called + error raised
- Fewer than 3 servers available in pool
- `connect_to_random_server()` called twice rapidly → second call returns existing future, no duplicate work

Existing randomize unit tests continue to pass.
