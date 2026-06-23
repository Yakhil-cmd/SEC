### Title
Free `UpdateSettings` Ingress Induction Bypasses Balance Check, Enabling Zero-Cost Subnet Resource Exhaustion - (File: `rs/cycles_account_manager/src/cycles_account_manager.rs`)

---

### Summary

The IC deliberately defers the ingress induction fee for `UpdateSettings` messages with small payloads (≤ 338 bytes) to allow users to unfreeze frozen canisters. However, this deferral causes `ingress_induction_cost()` to return `IngressInductionCost::Free` for such messages, which causes every pre-execution balance check to be skipped entirely. At execution time, the deferred charge is attempted but the error is explicitly silenced (`_ignore_error`). An attacker controlling a zero-balance canister can therefore flood the subnet with `UpdateSettings` ingress messages at zero cost, wasting block space, P2P gossip bandwidth, execution time, and ingress history capacity.

---

### Finding Description

`ingress_induction_cost()` in `rs/cycles_account_manager/src/cycles_account_manager.rs` classifies a `UpdateSettings` ingress message as `IngressInductionCost::Free` whenever the payload is ≤ `MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE` (338 bytes):

```rust
if let Ok(Method::UpdateSettings) = Method::from_str(ingress.method_name()) {
    if self.is_delayed_ingress_induction_cost(ingress.arg()) {
        None  // → IngressInductionCost::Free
    } else {
        effective_canister_id
    }
}
``` [1](#0-0) [2](#0-1) 

This `Free` classification propagates through every validation layer:

**1. HTTP endpoint (`should_accept_ingress_message`):** The balance check is gated on `IngressInductionCost::Fee`. For `Free`, the entire block is skipped — no balance check is performed before the message is accepted into the ingress pool. [3](#0-2) 

**2. Ingress selector (`validate_ingress`):** The cycles sufficiency check is also gated on `IngressInductionCost::Fee`. For `Free`, the `IngressInductionCost::Free => { // Do nothing. }` branch is taken. [4](#0-3) 

**3. Valid set rule (`enqueue`):** The message is pushed directly into the input queue with no balance check. [5](#0-4) 

**4. Execution time (deferred charge):** After `update_settings` executes, the deferred charge is attempted via `consume_cycles`, but the result is explicitly discarded:

```rust
// This call may fail with `CanisterOutOfCyclesError`,
// which is not actionable at this point.
let _ignore_error = self.cycles_account_manager.consume_cycles(...);
``` [6](#0-5) 

The combination means: a canister with zero cycles can send an unbounded stream of `UpdateSettings` ingress messages that pass all pre-execution checks and are executed for free.

---

### Impact Explanation

An attacker controlling a zero-balance canister (or any canister whose balance is below the induction cost) can:

1. Flood the subnet with `UpdateSettings` ingress messages (small payload, ≤ 338 bytes) targeting their own canister.
2. Each message passes `should_accept_ingress_message`, gets gossiped across all subnet nodes via P2P, is included in a consensus block, and is executed by every replica.
3. At execution time the charge silently fails; the attacker pays zero cycles.
4. Sustained flooding wastes: P2P gossip bandwidth, block payload space (up to `max_ingress_messages_per_block`), execution time per round, and ingress history slots (up to `ingress_history_max_messages`).
5. Filling the ingress history can block legitimate messages from being inducted, since `enqueue()` returns `IngressHistoryFull` once the global cap is reached. [7](#0-6) 

---

### Likelihood Explanation

The attack requires only a canister (obtainable for a one-time creation fee) and the ability to submit ingress messages via any boundary node. No privileged access, no key material, and no threshold corruption is needed. The `UpdateSettings` method is a standard management canister call available to any canister controller. The per-peer ingress pool throttle (`exceeds_limit`) is keyed on the originating node ID, not on the sender principal or canister, so it does not prevent a single attacker from submitting through multiple boundary nodes or directly to multiple replicas. [8](#0-7) 

---

### Recommendation

Add a minimum-balance guard in `should_accept_ingress_message` (and/or `validate_ingress`) that is applied even when the induction cost is deferred. Specifically, before accepting a `UpdateSettings` message with a delayed fee, verify that the target canister's balance is at least sufficient to cover the deferred induction cost (i.e., perform the same `can_withdraw_cycles_with_threshold` check that is already done for non-deferred messages). This mirrors the GMX fix: validate the fee at submission time, not only at execution time.

Alternatively, change `_ignore_error` to a hard rejection at execution time if the canister cannot pay the deferred fee, and propagate that rejection back to the ingress status so the message is not silently consumed for free.

---

### Proof of Concept

1. Create canister `C` on an application subnet with just enough cycles to cover creation.
2. Drain `C`'s balance to zero (e.g., via `ic0.call_cycles_add` to a burn address, or simply wait for storage fees to deplete it).
3. In a loop, submit ingress messages to `IC_00` with method `update_settings` and a small valid `UpdateSettingsArgs` payload (e.g., setting `freezing_threshold = 1`) targeting `C`. Each message is ≤ 338 bytes.
4. Observe that `should_accept_ingress_message` returns `Ok(())` for every message despite `C` having zero balance — confirmed by the `IngressInductionCost::Free` branch skipping the `can_withdraw_cycles_with_threshold` call.
5. Observe that each message is inducted, gossiped, included in a block, and executed; the `_ignore_error` at line 1045 silently discards the `CanisterOutOfCyclesError`.
6. Confirm zero cycles are deducted from `C` across all iterations. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L43-45)
```rust
/// Maximum payload size of a management call to update_settings
/// overriding the canister's freezing threshold.
const MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE: usize = 338;
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L570-578)
```rust
                if let Ok(Method::UpdateSettings) = Method::from_str(ingress.method_name()) {
                    // The fee for `UpdateSettings` with small payload is charged after
                    // applying the settings to allow users to unfreeze canisters
                    // after accidentally setting the freezing threshold too high.
                    if self.is_delayed_ingress_induction_cost(ingress.arg()) {
                        None
                    } else {
                        effective_canister_id
                    }
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L1379-1381)
```rust
    pub fn is_delayed_ingress_induction_cost(&self, arg: &[u8]) -> bool {
        arg.len() <= MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE
    }
```

**File:** rs/execution_environment/src/execution_environment.rs (L1043-1052)
```rust
                                // This call may fail with `CanisterOutOfCyclesError`,
                                // which is not actionable at this point.
                                let _ignore_error = self.cycles_account_manager.consume_cycles(
                                    &mut canister.system_state,
                                    memory_usage,
                                    message_memory_usage,
                                    induction_cost,
                                    subnet_cycles_config,
                                    false, // we ignore the error anyway => no need to reveal top up balance
                                );
```

**File:** rs/execution_environment/src/execution_environment.rs (L3344-3374)
```rust
            let subnet_cycles_config = state.get_own_subnet_cycles_config();
            let induction_cost = self.cycles_account_manager.ingress_induction_cost(
                ingress,
                effective_canister_id,
                subnet_cycles_config,
            );

            if let IngressInductionCost::Fee { payer, cost } = induction_cost {
                let paying_canister = canister(payer)?;
                let reveal_top_up = paying_canister
                    .controllers()
                    .contains(&ingress.sender().get());
                if let Err(err) = self
                    .cycles_account_manager
                    .can_withdraw_cycles_with_threshold(
                        &paying_canister.system_state,
                        cost,
                        paying_canister.memory_usage(),
                        paying_canister.message_memory_usage(),
                        paying_canister.system_state.reserved_balance(),
                        subnet_cycles_config,
                        reveal_top_up,
                    )
                {
                    return Err(UserError::new(
                        ErrorCode::CanisterOutOfCycles,
                        err.to_string(),
                    ));
                }
            }
        }
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L556-595)
```rust
        match self.cycles_account_manager.ingress_induction_cost(
            signed_ingress,
            effective_canister_id,
            subnet_cycles_config,
        ) {
            IngressInductionCost::Fee {
                payer,
                cost: ingress_cost,
            } => match state.canister_state(&payer) {
                Some(canister) => {
                    let cumulative_ingress_cost =
                        cycles_needed.entry(payer).or_insert_with(Cycles::zero);
                    if let Err(err) = self
                        .cycles_account_manager
                        .can_withdraw_cycles_with_threshold(
                            &canister.system_state,
                            *cumulative_ingress_cost + ingress_cost,
                            canister.memory_usage(),
                            canister.message_memory_usage(),
                            canister.system_state.reserved_balance(),
                            subnet_cycles_config,
                            false, // error here is not returned back to the user => no need to reveal top up balance
                        )
                    {
                        return Err(ValidationError::InvalidArtifact(
                            InvalidIngressPayloadReason::InsufficientCycles(err),
                        ));
                    }
                    *cumulative_ingress_cost += ingress_cost;
                }
                None => {
                    return Err(ValidationError::InvalidArtifact(
                        InvalidIngressPayloadReason::CanisterNotFound(payer),
                    ));
                }
            },
            IngressInductionCost::Free => {
                // Do nothing.
            }
        };
```

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L250-255)
```rust
        if state.metadata.own_subnet_type != SubnetType::System
            && state.metadata.ingress_history.len() >= self.ingress_history_max_messages
        {
            return Err(IngressInductionError::IngressHistoryFull {
                capacity: self.ingress_history_max_messages,
            });
```

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L286-291)
```rust
        match induction_cost {
            IngressInductionCost::Free => {
                // Only subnet methods can be free. These are enqueued directly.
                assert!(ingress.is_addressed_to_subnet());
                state.push_ingress(ingress)
            }
```

**File:** rs/artifact_pool/src/ingress_pool.rs (L226-232)
```rust
    fn exceeds_limit(&self, peer_id: &NodeId) -> bool {
        let counters = self.unvalidated.peer_counters.get_counters(peer_id)
            + self.validated.peer_counters.get_counters(peer_id);

        counters.bytes > self.ingress_pool_max_bytes
            || counters.messages > self.ingress_pool_max_count
    }
```

**File:** rs/messaging/src/scheduling/valid_set_rule/test.rs (L769-812)
```rust
#[test]
fn management_message_update_setting_is_inducted_but_not_charged() {
    let ingress_history_writer = MockIngressHistory::new();
    let metrics_registry = MetricsRegistry::new();
    let subnet_id = subnet_test_id(1);
    let valid_set_rule = ValidSetRuleImpl::new(
        Arc::new(ingress_history_writer),
        Arc::new(
            CyclesAccountManagerBuilder::new()
                .with_subnet_id(subnet_id)
                .build(),
        ),
        &metrics_registry,
        no_op_logger(),
    );

    let mut state = ReplicatedStateBuilder::new().build();
    let canister_id = canister_test_id(0);
    let canister = get_running_canister(canister_id);
    let balance_before = canister.system_state.balance();
    state.put_canister_state(canister);

    let payload = UpdateSettingsArgs {
        canister_id: canister_id.get(),
        settings: CanisterSettingsArgsBuilder::new()
            .with_freezing_threshold(1 << 20)
            .build(),
        sender_canister_version: None,
    }
    .encode();
    let ingress = SignedIngressBuilder::new()
        .canister_id(IC_00)
        .method_name("update_settings")
        .method_payload(payload)
        .build();
    assert!(valid_set_rule.enqueue(&mut state, ingress).is_ok());

    let balance_after = state
        .canister_state(&canister_id)
        .unwrap()
        .system_state
        .balance();

    assert_eq!(balance_after, balance_before);
```
