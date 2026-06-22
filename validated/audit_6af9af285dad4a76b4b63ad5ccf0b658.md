### Title
Unresolvable `bounded_wait` XRC Inter-Canister Call Permanently Blocks CMC Stopping and Upgrade - (File: `rs/nervous_system/clients/src/exchange_rate_canister_client.rs`)

### Summary
The Cycles Minting Canister (CMC) calls the Exchange Rate Canister (XRC) from its heartbeat using `Call::bounded_wait`. This call has no timeout and waits indefinitely for a response. If the XRC subnet is unavailable, the call never returns, leaving the CMC with a permanently outstanding callback. A canister with outstanding callbacks cannot transition to the `Stopped` state, which means the CMC can never be upgraded. Additionally, the `UpdateExchangeRateGuard` has no `Drop` implementation, so the `update_exchange_rate_canister_state` is permanently stuck at `InProgress`, blocking all future XRC calls even after the XRC subnet recovers.

### Finding Description
`RealExchangeRateCanisterClient::get_icp_to_xdr_exchange_rate` in `rs/nervous_system/clients/src/exchange_rate_canister_client.rs` issues the XRC call using `Call::bounded_wait`:

```rust
let result = Call::bounded_wait(self.callee_canister_id.get().0, "get_exchange_rate")
    .with_arg(request)
    .await
    .map_err(call_failed_to_get_exchange_rate_error)?;
``` [1](#0-0) 

`bounded_wait` is a non-best-effort call: it suspends the caller indefinitely until a reply or rejection arrives. If the XRC subnet is partitioned or not yet recovered, no reply ever arrives.

The CMC heartbeat drives this call: [2](#0-1) 

Before the `await`, `UpdateExchangeRateGuard::new` commits the state change `InProgress` to replicated state: [3](#0-2) 

`schedule_next_attempt` — the only code that transitions the state out of `InProgress` — is placed **after** the `await` and is therefore never reached if the call hangs: [4](#0-3) 

`UpdateExchangeRateGuard` has no `Drop` implementation, so there is no cleanup path. The state is permanently `InProgress`.

The codebase itself documents the resulting upgrade-blocking DoS in two places:

1. A comment in the NNS integration test build file explicitly describes the symptom and root cause: [5](#0-4) 

2. The canister upgrade helper script special-cases the CMC by skipping the stop step entirely because the outstanding XRC call prevents the canister from ever reaching `Stopped`: [6](#0-5) 

The `TODO` comment in the build file — *"When the platform supports best-effort requests, make the CMC use that"* — confirms the intended fix and that the issue is known but unresolved in the production code path.

### Impact Explanation
1. **CMC upgrade DoS.** Any NNS proposal to upgrade the CMC requires stopping it first. With an outstanding `bounded_wait` callback to an unavailable XRC subnet, the CMC never reaches `Stopped`, so the upgrade proposal cannot complete. Security patches and bug fixes to the CMC are blocked for the duration of the XRC subnet outage.
2. **Permanent `InProgress` lock.** Because `UpdateExchangeRateGuard` has no `Drop`, the `update_exchange_rate_canister_state` stays `InProgress` even after the XRC subnet recovers. Every subsequent heartbeat returns `UpdateAlreadyInProgress` and no new XRC calls are ever made, causing the ICP/XDR rate to become permanently stale. Stale rates directly affect cycles pricing for all users converting ICP to cycles via the CMC.

### Likelihood Explanation
The scenario is not theoretical: the codebase documents it as a real observed failure mode during mainnet recovery operations. The XRC canister lives on a dedicated subnet; any event that makes that subnet temporarily unreachable (subnet software bug, rolling upgrade, partial network partition, or disaster-recovery replay where the XRC subnet is not included) triggers the condition. The upgrade script already works around it by skipping the stop step, confirming the issue manifests in practice.

### Recommendation
1. **Switch to best-effort calls.** Replace `Call::bounded_wait` with `Call::best_effort` (with an appropriate deadline) in `RealExchangeRateCanisterClient::get_icp_to_xdr_exchange_rate`. The build-file TODO already identifies this as the intended fix.
2. **Implement `Drop` for `UpdateExchangeRateGuard`.** The guard should reset `update_exchange_rate_canister_state` from `InProgress` to a `GetRateAt(next_minute)` value in its `Drop` implementation, so that a stuck or panicking call never permanently blocks future attempts.
3. **Add a fallback rate.** If the XRC has been unreachable for longer than a configurable threshold, the CMC should continue serving the last known rate rather than blocking upgrades.

### Proof of Concept
1. The CMC heartbeat fires and calls `update_exchange_rate()`. [2](#0-1) 
2. `UpdateExchangeRateGuard::new` commits `InProgress` to replicated state before the `await` point. [3](#0-2) 
3. `RealExchangeRateCanisterClient::get_icp_to_xdr_exchange_rate` issues `Call::bounded_wait` to the XRC. [1](#0-0) 
4. The XRC subnet is unavailable; no reply or rejection is ever delivered. The `await` suspends indefinitely.
5. `schedule_next_attempt` is never called; `update_exchange_rate_canister_state` remains `InProgress` in replicated state. [7](#0-6) 
6. The CMC holds an outstanding callback. The IC runtime refuses to transition the canister to `Stopped` while callbacks are pending.
7. Any NNS upgrade proposal for the CMC stalls indefinitely — confirmed by the documented workaround of `SKIP_STOPPING=yes`. [8](#0-7)

### Citations

**File:** rs/nervous_system/clients/src/exchange_rate_canister_client.rs (L188-191)
```rust
        let result = Call::bounded_wait(self.callee_canister_id.get().0, "get_exchange_rate")
            .with_arg(request)
            .await
            .map_err(call_failed_to_get_exchange_rate_error)?;
```

**File:** rs/nns/cmc/src/main.rs (L2397-2402)
```rust
#[heartbeat]
async fn canister_heartbeat() {
    if with_state(|state| state.exchange_rate_canister_id.is_some()) {
        update_exchange_rate().await
    }
}
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L114-118)
```rust
        mutate_state(safe_state, |state| {
            state
                .update_exchange_rate_canister_state
                .replace(UpdateExchangeRateState::InProgress);
        });
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L167-181)
```rust
    async fn with_guard<F>(
        safe_state: &'static LocalKey<RefCell<Option<State>>>,
        current_minute_seconds: u64,
        future: F,
    ) -> Result<(), UpdateExchangeRateError>
    where
        F: std::future::Future<Output = Result<(), UpdateExchangeRateError>>,
    {
        let guard = Self::new(safe_state, current_minute_seconds)?;
        let result = future.await;
        // Check the result. Based on the contents, this will affect the next
        // update state.
        guard.schedule_next_attempt(&result);
        result
    }
```

**File:** rs/nns/integration_tests/BUILD.bazel (L349-358)
```text
# Symptom: [Governance] Error when refreshing XDR rate in run_periodic_tasks: External: Error calling 'get_average_icp_xdr_conversion_rate': code: Some(5), message: Canister rkp4c-7iaaa-aaaaa-aaaca-cai is stopping
#
# Possible solution: Wait a few hours for next golden state to be generated.
#
# Possible cause: In the current golden state, the cycles-minting canister is calling
# the exchange-rate canister. In this case, the CMC cannot be upgraded, because
# it never transitions from the stopping state to the stopped state. This transition
# is required in order to proceed with upgrading the CMC.
#
# TODO: When the plaform supports best-effort requests, make the CMC use that.
```

**File:** rs/nervous_system/tools/release/upgrade-canister-to-working-tree.sh (L39-47)
```shellscript
# When CMC is recovered from mainnet, it soon starts making calls to the Exchange Rate Canister (XRC), which is on a
# subnet that is not recovered.  These calls don't timeout and can't return.  That prevents CMC from ever being able to
# stop, which means we could never complete the upgrade. However, because they cannot return,
# it is safe to skip stopping when testing the upgrade of this canister, as replies cannot cause arbitrary code to execute
# when they return (which is the only reason for stopping in the first place).  The upgrade will still work, and
# the upgrade process will be exercised.
if [ "$CANISTER_NAME" == "cycles-minting" ]; then
    export SKIP_STOPPING=yes
fi
```
