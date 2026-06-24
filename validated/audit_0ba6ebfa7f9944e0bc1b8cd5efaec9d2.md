Audit Report

## Title
TOCTOU Race in `launch_compiler` Allows `LauncherChildWatch` to Panic on Unknown PID — (`rs/canister_sandbox/src/launcher.rs`)

## Summary
A time-of-check/time-of-use race exists between `spawn_socketed_process` returning in `launch_compiler` and the resulting PID being inserted into `pid_to_process_info`. If the compiler child exits with a non-zero status during this window, the `LauncherChildWatch` watcher thread reaps the PID, finds no entry in the map, and evaluates `unwrap_or(true)` as `true`, triggering `panic!("Launcher detected sandbox exit")`. This crashes the launcher process, which the replica detects via `panic_due_to_exit` and re-panics, taking the node offline.

## Finding Description
In `launch_compiler` (lines 230–251 of `launcher.rs`), the child is spawned, the socket is dropped, and only then is the mutex acquired to insert the PID:

```
spawn_socketed_process(...)   // line 230 — child is live, PID exists in OS
drop(UnixStream::from_raw_fd(socket))  // line 233 — extra delay
self.pid_to_process_info.lock()        // line 235 — PID inserted here
```

The `LauncherChildWatch` thread only blocks on the condvar while the map is empty (lines 111–117). Once any sandbox process is registered, the watcher is already past the condvar and blocking inside `wait()`. If the compiler process exits with a non-zero status or signal during the window between lines 230 and 235, `wait()` returns that PID before `launch_compiler` has inserted it. The watcher then:

1. Acquires the lock (succeeds — `launch_compiler` has not locked it yet).
2. Calls `info_map.remove(&pid)` → returns `None` (line 138).
3. Evaluates `should_panic` via `unwrap_or(true)` (line 146) → `true`.
4. Calls `panic!("Launcher detected sandbox exit")` (line 156).

The intent for compiler processes is `panic_on_failure: false` (line 249), but the `unwrap_or(true)` default inverts this for any PID that arrives before insertion, making the race directly exploitable.

The replica's watcher thread calls `panic_due_to_exit` (lines 2175–2183 of `sandboxed_execution_controller.rs`) when the launcher exits non-zero, crashing the replica process.

## Impact Explanation
A single replica node crashes and goes offline until restarted. The subnet continues with remaining replicas but the affected node is unavailable. This matches the allowed Medium impact: *"One-time crash of a single replica on an application subnet, limited subnet availability impact."*

## Likelihood Explanation
Three conditions are required: (1) at least one sandbox process is already running — the normal steady state during canister execution; (2) a `launch_compiler` call is in flight — triggered by any canister install or upgrade; (3) the compiler exits with a non-zero status before the PID is inserted. An unprivileged user can submit a canister install with a crafted Wasm binary designed to crash the compiler immediately on startup. The race window is narrow (a few instructions), but the attack is repeatable: the attacker can submit many install requests until the race is won.

## Recommendation
Change the `unwrap_or` default from `true` to `false` for unknown PIDs in `launcher.rs` line 146:

```rust
let should_panic = process_info
    .as_ref()
    .map(|x| x.panic_on_failure)
    .unwrap_or(false);  // unknown PID → do not panic
```

Additionally, insert the PID into the map before spawning the process, or perform the spawn and insert atomically under the lock, to close the race entirely.

## Proof of Concept
1. Deploy a canister to ensure at least one sandbox process is running (map non-empty, watcher past condvar).
2. Craft a Wasm module that causes the compiler binary to exit with a non-zero code immediately on startup (e.g., a Wasm binary that triggers an immediate compiler panic or an invalid embedder config).
3. Repeatedly trigger `launch_compiler` via canister install/upgrade calls. On the iteration where the compiler exits during the race window (between `spawn_socketed_process` returning at line 230 and `info_map.lock()` at line 235), the watcher reaps the PID, finds `None`, sets `should_panic = true` via `unwrap_or(true)` at line 146, and calls `panic!("Launcher detected sandbox exit")` at line 156, crashing the replica.
4. Confirm via a local integration test: spawn a `LauncherServer`, register one dummy sandbox PID, then call `launch_compiler` with a binary that exits immediately with status 1, and assert the launcher thread panics.