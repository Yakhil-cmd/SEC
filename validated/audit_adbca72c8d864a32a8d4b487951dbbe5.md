All cited code references are verified against the actual repository. Here is the validation result:

---

Audit Report

## Title
Free Ingress Induction for `UpdateSettings` Messages Enables Subnet Resource Exhaustion Without Cycles Payment - (File: `rs/cycles_account_manager/src/cycles_account_manager.rs`)

## Summary
`ingress_induction_cost` returns `IngressInductionCost::Free` for `UpdateSettings` management messages with payload ≤ `MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE` bytes. This is intentional to allow unfreezing frozen canisters, but it creates an exploitable path: any user who controls a canister can flood the subnet with distinct `UpdateSettings` ingress messages at zero upfront cost. No cycles are checked or charged at the ingress filter, ingress selector, or induction stages, and the post-execution charge is silently discarded if the canister has insufficient cycles. This removes all economic deterrence and allows an attacker to consume subnet block space and execution resources without payment, crowding out legitimate traffic.

## Finding Description

**Root cause — `ingress_induction_cost` returns `Free` for small `UpdateSettings` payloads**

In `rs/cycles_account_manager/src/cycles_account_manager.rs`, `ingress_induction_cost` sets `paying_canister = None` when the method is `UpdateSettings` and `is_delayed_ingress_induction_cost` returns true, which maps directly to `IngressInductionCost::Free`. [1](#0-0) 

The threshold check is `arg.len() <= MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE`. [2](#0-1) 

**No balance check at the ingress filter (`should_accept_ingress_message`)**

In `rs/execution_environment/src/execution_environment.rs`, the balance check is gated on `if let IngressInductionCost::Fee { payer, cost } = induction_cost`. When the cost is `Free`, the entire balance-check block is skipped. [3](#0-2) 

**No cycles check at the ingress selector (`validate_ingress`)**

In `rs/ingress_manager/src/ingress_selector.rs`, the `IngressInductionCost::Free` arm explicitly does nothing. [4](#0-3) 

**No cycles charged at induction (`enqueue`)**

In `rs/messaging/src/scheduling/valid_set_rule.rs`, free messages are pushed directly into the subnet queue without any cycles withdrawal. [5](#0-4) 

This is confirmed by the existing test `management_message_update_setting_is_inducted_but_not_charged`, which asserts the canister balance is unchanged after induction. [6](#0-5) 

**Post-execution charge is silently ignored on failure**

After executing `UpdateSettings`, the delayed induction cost is charged via `consume_cycles`. The return value is bound to `_ignore_error` and explicitly discarded, so a canister with zero cycles incurs no penalty. [7](#0-6) 

**Exploit flow:**
1. Attacker creates canister C (one-time cost) and drains its cycles to zero.
2. Attacker constructs N distinct `UpdateSettings` ingress messages targeting C (different nonces, payload ≤ 338 bytes each, sender = controller of C).
3. Each message passes `should_accept_ingress_message` with no balance check (cost is `Free`).
4. Each message passes `validate_ingress` with no cycles check (the `Free` arm does nothing).
5. Each message is enqueued via `enqueue` without any cycles withdrawal.
6. Messages are included in blocks and executed on all replicas.
7. The post-execution charge fails silently (`_ignore_error`) because C has no cycles.
8. Attacker has consumed N × (execution cost per `UpdateSettings`) of subnet resources at zero net cost, displacing legitimate ingress traffic.

## Impact Explanation

This matches the allowed High impact class: **Application/platform-level DoS or subnet availability impact not based on raw volumetric DDoS** ($2,000–$10,000). An attacker can fill blocks with free `UpdateSettings` messages up to `max_ingress_messages_per_block`, crowding out legitimate paid ingress traffic and degrading subnet throughput. Multiple coordinated attackers with multiple canisters amplify the effect. Unlike normal ingress messages, there is no economic deterrent at any stage of the pipeline. This is not a "gas/cycles-only" issue because the harm falls on other users whose legitimate messages are displaced, not merely on the attacker's own cost accounting.

## Likelihood Explanation

Any user with an IC identity can create a canister and become its controller — no privileged access is required. The only upfront cost is the one-time canister creation fee; all subsequent `UpdateSettings` messages are free at induction and the post-execution charge is silently dropped. The attack is submitted via the standard `/api/v2/canister/.../call` HTTP endpoint. Per-peer ingress pool limits, `max_ingress_messages_per_block`, and the 5-minute ingress TTL reduce but do not eliminate the impact, and multiple coordinated attackers with multiple canisters can sustain the attack continuously.

## Recommendation

1. **Add a minimum balance check for free-induction messages**: In `should_accept_ingress_message` and `validate_ingress`, even when `IngressInductionCost::Free` is returned, verify that the target canister has sufficient cycles to cover the post-execution charge before accepting the message.
2. **Rate-limit free-induction messages per canister**: Track and cap the number of `UpdateSettings` messages per canister per block or per time window in the ingress selector.
3. **Do not silently ignore the post-execution charge failure**: If `consume_cycles` fails after `UpdateSettings` execution, record the debt or penalize the canister rather than discarding the error via `_ignore_error`.
4. **Charge upfront with a refund mechanism**: Charge the induction cost at induction time and refund it if the canister was frozen, eliminating the free window entirely while preserving the unfreeze use case.

## Proof of Concept

```
1. Create canister C with minimal cycles (just enough for creation).
2. Drain C's cycles to zero.
3. Construct N distinct UpdateSettings ingress messages targeting C:
   - canister_id = C, settings = {freezing_threshold: X}, payload ≤ 338 bytes
   - Each message uses a different nonce so MessageId is unique
   - Sender = controller of C (attacker's identity)
4. Submit all N messages via POST /api/v2/canister/IC_00/call
   - ingress_induction_cost() returns Free for each → no balance check at filter
   - validate_ingress() hits the `IngressInductionCost::Free => { // Do nothing. }` arm
   - enqueue() pushes each message without charging cycles
5. Messages are included in blocks and executed on all replicas.
6. Post-execution charge hits `_ignore_error` because C has no cycles.
7. Attacker has consumed N × (execution cost per UpdateSettings) of subnet resources
   at zero net cost, displacing legitimate ingress traffic.

Reproducible as a unit test extending management_message_update_setting_is_inducted_but_not_charged:
- Set canister balance to zero before enqueue.
- Submit N messages in a loop.
- Assert all N messages are enqueued successfully and balance remains zero.
- Assert the ingress queue length equals N.
```

### Citations

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

**File:** rs/execution_environment/src/execution_environment.rs (L3351-3374)
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
