### Title
Free Ingress Induction for `UpdateSettings` Messages Enables Subnet Resource Exhaustion Without Cycles Payment - (File: `rs/cycles_account_manager/src/cycles_account_manager.rs`)

---

### Summary

The IC's `ingress_induction_cost` function returns `IngressInductionCost::Free` for `UpdateSettings` management messages whose payload is ≤ 338 bytes. This is intentional to allow users to unfreeze frozen canisters, but it creates an exploitable path: any unprivileged user who controls a canister can flood the subnet with many distinct `UpdateSettings` ingress messages at zero upfront cost. No cycles are checked or charged at the ingress filter, ingress selector, or induction stages. The post-execution charge is also silently discarded if the canister has insufficient cycles. This is a direct analog to the SEDA H-12 vulnerability class: a free-execution bypass that allows resource exhaustion without economic deterrence.

---

### Finding Description

**Root Cause — `ingress_induction_cost` returns `Free` for small `UpdateSettings` payloads**

In `rs/cycles_account_manager/src/cycles_account_manager.rs`, the function `ingress_induction_cost` computes the payer for an ingress message. For `UpdateSettings` addressed to the management canister (`IC_00`), if the payload is ≤ `MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE` (338 bytes), the payer is set to `None`, which maps to `IngressInductionCost::Free`: [1](#0-0) 

The threshold check: [2](#0-1) 

**No balance check at the ingress filter (`should_accept_ingress_message`)**

In `rs/execution_environment/src/execution_environment.rs`, `should_accept_ingress_message` only checks the canister balance when `induction_cost` is `Fee`. When it is `Free`, the balance check branch is skipped entirely: [3](#0-2) 

**No cycles check at the ingress selector (`validate_ingress`)**

In `rs/ingress_manager/src/ingress_selector.rs`, the `validate_ingress` function explicitly does nothing for free messages: [4](#0-3) 

**No cycles charged at induction (`enqueue`)**

In `rs/messaging/src/scheduling/valid_set_rule.rs`, the `enqueue` function pushes free messages directly into the subnet queue without any cycles withdrawal: [5](#0-4) 

This is confirmed by the existing test `management_message_update_setting_is_inducted_but_not_charged`: [6](#0-5) 

**Post-execution charge is silently ignored on failure**

In `rs/execution_environment/src/execution_environment.rs`, after executing `UpdateSettings`, the delayed induction cost is charged. If the canister has no cycles, the error is explicitly discarded: [7](#0-6) 

---

### Impact Explanation

An unprivileged user can:

1. Create a canister (one-time cost) and become its controller.
2. Submit many distinct `UpdateSettings` ingress messages targeting that canister (different nonces/expiry times, payload ≤ 338 bytes each). Each message has a unique `MessageId` so deduplication does not block them.
3. Each message passes the ingress filter with no balance check (cost is `Free`).
4. Each message is included in blocks (no cycles check in the ingress selector for free messages).
5. Each message is executed on all replicas, consuming subnet execution resources.
6. The post-execution charge fails silently if the canister has no cycles.

The attacker can fill blocks with free `UpdateSettings` messages up to `max_ingress_messages_per_block`, crowding out legitimate user traffic and degrading subnet throughput. Multiple coordinated attackers with multiple canisters amplify the effect. Unlike normal ingress messages, there is no economic deterrent at any stage of the pipeline.

---

### Likelihood Explanation

- **Attacker entry point**: Any user with an IC identity can create a canister and become its controller. No privileged access is required.
- **Attack cost**: Only the one-time canister creation fee. All subsequent `UpdateSettings` messages are free at induction and the post-execution charge is silently dropped if the canister has no cycles.
- **Practical feasibility**: The attacker submits messages via the standard `/api/v2/canister/.../call` HTTP endpoint. The ingress pool throttler (`exceeds_threshold`) limits total pool size but does not distinguish free-induction messages from paid ones, so the attacker can fill the pool up to the configured limit.
- **Mitigating factors**: Per-peer ingress pool limits, `max_ingress_messages_per_block`, and round-robin ingress selection reduce but do not eliminate the impact. The ingress TTL (5 minutes) bounds the window per message.

---

### Recommendation

1. **Require a minimum balance check even for free-induction messages**: In `should_accept_ingress_message` and `validate_ingress`, add a balance check for the target canister even when `IngressInductionCost::Free` is returned, to ensure the canister can cover the post-execution charge.
2. **Rate-limit free-induction messages per canister**: Track and cap the number of `UpdateSettings` messages per canister per block or per time window.
3. **Do not silently ignore the post-execution charge failure**: If `consume_cycles` fails after `UpdateSettings` execution, record the debt or reject the message rather than discarding the error.
4. **Charge upfront with a refund mechanism**: Charge the induction cost at induction time and refund it if the canister was frozen (i.e., apply the same pattern as the delayed charge but with a guaranteed refund path), eliminating the free window entirely.

---

### Proof of Concept

```
1. Create canister C with minimal cycles (just enough for creation).
2. Drain C's cycles to zero (or leave it at zero after creation).
3. Construct N distinct UpdateSettings ingress messages targeting C:
   - Each message: canister_id=C, settings={freezing_threshold: X}, payload ≤ 338 bytes
   - Each message uses a different nonce so MessageId is unique
   - Sender = controller of C (the attacker's identity)
4. Submit all N messages via POST /api/v2/canister/IC_00/call
   - ingress_induction_cost() returns Free for each → no balance check at filter
   - validate_ingress() does nothing for Free → messages enter validated pool
   - enqueue() pushes each message without charging cycles
5. Messages are included in blocks (up to max_ingress_messages_per_block per block)
   and executed on all replicas.
6. Post-execution charge fails silently (_ignore_error) because C has no cycles.
7. Attacker has consumed N × (execution cost per UpdateSettings) of subnet resources
   at zero net cost, displacing legitimate ingress traffic.
```

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L567-596)
```rust
        let paying_canister = match ingress.is_addressed_to_subnet() {
            // If a subnet message, get effective canister id who will pay for the message.
            true => {
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
        }
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L1373-1381)
```rust
    // The fee for `UpdateSettings` is charged after applying
    // the settings to allow users to unfreeze canisters
    // after accidentally setting the freezing threshold too high.
    // To satisfy this use case, it is sufficient to send
    // a payload of a small size and thus we only delay
    // the ingress induction cost for small payloads.
    pub fn is_delayed_ingress_induction_cost(&self, arg: &[u8]) -> bool {
        arg.len() <= MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE
    }
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

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L286-291)
```rust
        match induction_cost {
            IngressInductionCost::Free => {
                // Only subnet methods can be free. These are enqueued directly.
                assert!(ingress.is_addressed_to_subnet());
                state.push_ingress(ingress)
            }
```

**File:** rs/messaging/src/scheduling/valid_set_rule/test.rs (L769-813)
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
}
```
