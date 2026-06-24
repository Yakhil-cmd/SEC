### Title
Free Ingress Induction for `UpdateSettings` with Small Payload Enables Zero-Cost Subnet DOS - (`rs/cycles_account_manager/src/cycles_account_manager.rs` / `rs/execution_environment/src/execution_environment.rs`)

---

### Summary

`UpdateSettings` ingress messages with a payload ≤ 338 bytes (`MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE`) are classified as `IngressInductionCost::Free` at every pre-execution check point. The deferred post-execution fee collection explicitly ignores the `CanisterOutOfCyclesError`. An attacker who controls a zero-balance canister can therefore spam `UpdateSettings` messages at zero economic cost, consuming subnet execution resources indefinitely.

---

### Finding Description

The IC deliberately defers the ingress induction fee for small `UpdateSettings` payloads to allow users to unfreeze canisters that have accidentally set an excessively high freezing threshold. The mechanism works as follows:

**Step 1 – Fee classified as `Free` at induction time.**

In `ingress_induction_cost()`, when the method is `UpdateSettings` and `arg.len() <= MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE` (338 bytes), the function returns `paying_canister = None`, which maps to `IngressInductionCost::Free`: [1](#0-0) 

The constant is defined as: [2](#0-1) 

**Step 2 – All pre-execution cycle checks are bypassed.**

Every gate that would normally reject a zero-balance canister's message silently passes `Free` messages through:

- **Ingress filter** (`should_accept_ingress_message`): the `IngressInductionCost::Fee` branch that calls `can_withdraw_cycles_with_threshold` is never entered: [3](#0-2) 

- **Ingress selector** (`validate_ingress`): the `Free` arm does nothing: [4](#0-3) 

- **Valid-set rule** (`enqueue`): the `Free` arm pushes the message directly without any balance check: [5](#0-4) 

**Step 3 – Post-execution fee collection silently fails.**

After `update_settings` executes, the deferred fee is attempted but the error is explicitly discarded: [6](#0-5) 

The comment `// we ignore the error anyway` confirms this is intentional for the unfreeze use case, but it means a canister with zero cycles pays nothing.

---

### Impact Explanation

An attacker who controls a canister (even one with zero cycles) can continuously submit `UpdateSettings` ingress messages with a small payload (e.g., only setting `freezing_threshold`). Each message:

1. Passes the ingress filter with no cycles check.
2. Passes the ingress selector with no cycles check.
3. Is inducted into the subnet's execution queue for free.
4. Consumes a full execution round slot on the subnet.
5. Has its fee silently dropped at execution time.

By flooding the subnet with such messages, the attacker can saturate the ingress queue and execution pipeline, delaying or starving legitimate canister calls. This is a direct analog to the Aleo split-transaction DOS: a special-case fee exemption with no fallback collateral allows free abuse of compute resources.

---

### Likelihood Explanation

The attack requires only:
- Creating a canister on the target subnet (one-time cost, minimal cycles).
- Sending repeated `UpdateSettings` ingress messages with a payload ≤ 338 bytes.

No privileged access, no threshold corruption, and no external dependency is needed. Any unprivileged user can execute this attack. The attacker's canister can be drained to zero cycles before the attack begins, making the ongoing cost zero.

---

### Recommendation

1. **Require a minimum balance check even for deferred-fee messages.** Before inducting a `Free`-classified `UpdateSettings` message, verify that the target canister holds at least the induction fee in its balance (or a small fixed deposit). This preserves the unfreeze use case while preventing zero-cost flooding.

2. **Rate-limit `UpdateSettings` ingress messages per canister per block**, independent of the fee mechanism, to bound the worst-case throughput of this path.

3. **Do not silently ignore the fee collection error.** At minimum, log a metric when `consume_cycles` fails for the deferred `UpdateSettings` fee, so operators can detect abuse.

---

### Proof of Concept

```
1. Create canister C on an application subnet with minimal cycles.
2. Drain C's cycles balance to zero (e.g., via compute allocation charges).
3. In a loop, submit ingress messages:
     destination: IC_00 (management canister)
     method:      update_settings
     payload:     UpdateSettingsArgs { canister_id: C, settings: { freezing_threshold: k } }
                  (encoded size ≤ 338 bytes — confirmed by the test at
                   rs/cycles_account_manager/src/cycles_account_manager/tests.rs:22-37)
4. Observe:
   - ingress_filter accepts each message (IngressInductionCost::Free, no balance check).
   - ingress_selector accepts each message (Free arm, no balance check).
   - valid_set_rule enqueues each message directly.
   - execution_environment executes each message and silently drops the fee error.
   - C's balance remains zero throughout.
   - Legitimate canister calls on the subnet experience increased latency.
``` [7](#0-6) [8](#0-7)

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L43-45)
```rust
/// Maximum payload size of a management call to update_settings
/// overriding the canister's freezing threshold.
const MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE: usize = 338;
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L570-595)
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
                } else {
                    effective_canister_id
                }
            }
            // A message to a canister is always paid for by the receiving canister.
            false => Some(ingress.canister_id()),
        };

        match paying_canister {
            Some(paying_canister) => {
                let cost = self.ingress_induction_cost_from_bytes(raw_bytes, subnet_cycles_config);
                IngressInductionCost::Fee {
                    payer: paying_canister,
                    cost: cost.real(),
                }
            }
            None => IngressInductionCost::Free,
```

**File:** rs/execution_environment/src/execution_environment.rs (L1043-1053)
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
                            }
```

**File:** rs/execution_environment/src/execution_environment.rs (L3344-3373)
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
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L592-594)
```rust
            IngressInductionCost::Free => {
                // Do nothing.
            }
```

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L287-291)
```rust
            IngressInductionCost::Free => {
                // Only subnet methods can be free. These are enqueued directly.
                assert!(ingress.is_addressed_to_subnet());
                state.push_ingress(ingress)
            }
```

**File:** rs/cycles_account_manager/src/cycles_account_manager/tests.rs (L22-37)
```rust
fn max_delayed_ingress_cost_payload_size_test() {
    let default_freezing_limit = 30 * 24 * 3600; // 30 days
    let payload = UpdateSettingsArgs {
        canister_id: CanisterId::from_u64(0).into(),
        settings: CanisterSettingsArgsBuilder::new()
            .with_freezing_threshold(default_freezing_limit)
            .build(),
        sender_canister_version: None, // ingress messages are not supposed to set this field
    };

    let payload_size = 2 * Encode!(&payload).unwrap().len();

    assert!(
        payload_size <= MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE,
        "Payload size: {payload_size}, is greater than MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE: {MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE}."
    );
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
