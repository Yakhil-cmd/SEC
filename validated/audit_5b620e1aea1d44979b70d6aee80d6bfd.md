Audit Report

## Title
Free `UpdateSettings` Ingress Induction Bypasses Balance Check, Enabling Zero-Cost Subnet Resource Exhaustion - (File: `rs/cycles_account_manager/src/cycles_account_manager.rs`)

## Summary
`ingress_induction_cost()` returns `IngressInductionCost::Free` for any `UpdateSettings` ingress message with a payload ≤ 338 bytes. This `Free` classification causes every pre-execution balance check across the HTTP endpoint, ingress selector, and valid-set rule to be skipped entirely. At execution time the deferred charge is attempted but the error is explicitly discarded via `_ignore_error`. A canister controller with a zero-balance canister can therefore submit an unbounded stream of `UpdateSettings` messages that are inducted, gossiped, block-included, and executed at zero cost, ultimately filling the ingress history and blocking legitimate message induction.

## Finding Description
`ingress_induction_cost()` in `rs/cycles_account_manager/src/cycles_account_manager.rs` sets `paying_canister = None` (resolving to `IngressInductionCost::Free`) whenever the method is `UpdateSettings` and `arg.len() <= MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE` (338 bytes): [1](#0-0) [2](#0-1) 

This `Free` value propagates through every validation layer without triggering a balance check:

**1. HTTP endpoint (`should_accept_ingress_message`):** The `can_withdraw_cycles_with_threshold` call is inside `if let IngressInductionCost::Fee { payer, cost } = induction_cost { … }`. For `Free`, the entire block is skipped. [3](#0-2) 

**2. Ingress selector (`validate_ingress`):** The cycles-sufficiency check is also gated on `IngressInductionCost::Fee`; the `Free` arm explicitly does nothing. [4](#0-3) 

**3. Valid-set rule (`enqueue`):** `Free` messages are pushed directly into the input queue with no balance check. [5](#0-4) 

**4. Execution-time deferred charge:** After `update_settings` executes, the charge is attempted but the `CanisterOutOfCyclesError` result is explicitly discarded. [6](#0-5) 

The existing test `management_message_update_setting_is_inducted_but_not_charged` explicitly asserts that the canister balance is unchanged after induction, confirming the zero-cost path is intentional but unguarded against abuse. [7](#0-6) 

The ingress history cap is enforced only at the top of `enqueue()`, and once reached it blocks all further induction on non-system subnets: [8](#0-7) 

The per-peer ingress pool throttle is keyed on `NodeId`, not on sender principal or canister ID, so it does not prevent a single attacker from submitting through multiple boundary nodes: [9](#0-8) 

## Impact Explanation
An attacker controlling a zero-balance canister can flood any application subnet with `UpdateSettings` ingress messages at zero cycles cost. Each message is gossiped across all subnet nodes, included in a consensus block (up to `max_ingress_messages_per_block` per round), and executed by every replica. Sustained flooding fills the ingress history (`ingress_history_max_messages`), after which `enqueue()` returns `IngressHistoryFull` for all subsequent messages — including legitimate ones from other users. This constitutes a **platform-level DoS / subnet availability impact** matching the High ($2,000–$10,000) bounty tier: "Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."

## Likelihood Explanation
The attack requires only: (a) a canister on an application subnet (one-time creation fee), (b) the ability to drain its balance to zero, and (c) a loop submitting small `UpdateSettings` ingress messages via any boundary node. No privileged access, key material, or threshold corruption is needed. The sender must be a controller of the target canister, which is trivially satisfied when the attacker owns the canister. The per-peer pool throttle does not constrain a single attacker using multiple boundary nodes or direct replica connections. The attack is repeatable indefinitely at zero marginal cost.

## Recommendation
Add a minimum-balance guard that applies even when the induction cost is deferred. In `should_accept_ingress_message` (and/or `validate_ingress`), before accepting a `UpdateSettings` message classified as `Free`, verify that the target canister's balance is at least sufficient to cover the deferred induction cost using the same `can_withdraw_cycles_with_threshold` check already applied for non-deferred messages. This preserves the unfreeze use-case (a frozen canister with enough balance to cover the small fee can still be unfrozen) while preventing zero-balance canisters from submitting unlimited free messages.

Alternatively, change `_ignore_error` at execution time to a hard rejection that propagates back to the ingress status, so a canister that cannot pay the deferred fee receives an explicit error and the message is not silently consumed for free.

## Proof of Concept
1. Create canister `C` on an application subnet with just enough cycles to cover creation.
2. Drain `C`'s balance to zero (e.g., via `ic0.call_cycles_add` to a burn address or by waiting for storage fees).
3. In a loop, submit ingress messages to `IC_00` with method `update_settings` and a valid `UpdateSettingsArgs` payload (e.g., `freezing_threshold = 1`) targeting `C`. Ensure the encoded payload is ≤ 338 bytes.
4. Observe that `should_accept_ingress_message` returns `Ok(())` for every message despite `C` having zero balance — the `IngressInductionCost::Free` branch skips `can_withdraw_cycles_with_threshold`.
5. Observe that each message is inducted, gossiped, included in a block, and executed; `_ignore_error` at line 1045 silently discards the `CanisterOutOfCyclesError`.
6. Confirm zero cycles are deducted from `C` across all iterations.
7. Continue until `ingress_history.len() >= ingress_history_max_messages`; confirm that subsequent legitimate ingress messages from other users receive `IngressHistoryFull` errors.

A deterministic unit test can be written by extending the existing `management_message_update_setting_is_inducted_but_not_charged` test: set the canister balance to zero before calling `enqueue`, assert `Ok(())` is returned, and assert the balance remains zero — directly demonstrating the zero-cost induction path.

### Citations

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

**File:** rs/execution_environment/src/execution_environment.rs (L3351-3373)
```rust
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
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L592-594)
```rust
            IngressInductionCost::Free => {
                // Do nothing.
            }
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

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L287-290)
```rust
            IngressInductionCost::Free => {
                // Only subnet methods can be free. These are enqueued directly.
                assert!(ingress.is_addressed_to_subnet());
                state.push_ingress(ingress)
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

**File:** rs/artifact_pool/src/ingress_pool.rs (L226-232)
```rust
    fn exceeds_limit(&self, peer_id: &NodeId) -> bool {
        let counters = self.unvalidated.peer_counters.get_counters(peer_id)
            + self.validated.peer_counters.get_counters(peer_id);

        counters.bytes > self.ingress_pool_max_bytes
            || counters.messages > self.ingress_pool_max_count
    }
```
