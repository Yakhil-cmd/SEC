All cited code references check out exactly. Let me verify the authorization check during execution to confirm the claim that non-controllers can also trigger this path.

All cited code references are confirmed accurate. Every step of the exploit chain is verified in the actual source.

Audit Report

## Title
Gasless Ingress Execution via `UpdateSettings` Delayed-Charge Bypass — (`rs/cycles_account_manager/src/cycles_account_manager.rs`, `rs/execution_environment/src/execution_environment.rs`)

## Summary
`UpdateSettings` ingress messages with payloads ≤ 338 bytes are classified as `IngressInductionCost::Free`, bypassing all cycle checks at induction, HTTP handler validation, and ingress selector stages. After execution, the deferred charge is attempted via `consume_cycles` but the returned `CanisterOutOfCyclesError` is explicitly discarded with `let _ignore_error`. Any unprivileged ingress sender can flood a subnet with these messages at zero economic cost, consuming ingress pool slots, block payload space, ingress history entries, and subnet execution instruction budget indefinitely.

## Finding Description
**Root cause — free induction classification:**
`ingress_induction_cost()` in `rs/cycles_account_manager/src/cycles_account_manager.rs` returns `IngressInductionCost::Free` (i.e., `paying_canister = None`) whenever the method is `UpdateSettings` and `is_delayed_ingress_induction_cost()` returns true (payload ≤ `MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE` = 338 bytes). [1](#0-0) [2](#0-1) 

**Cycle check skipped at induction (`valid_set_rule.rs`):**
The `Free` branch in `enqueue()` calls `state.push_ingress(ingress)` directly with no cycle withdrawal. [3](#0-2) 

**Cycle check skipped at ingress selector:**
The `Free` branch in `validate_ingress()` is a no-op. [4](#0-3) 

**Cycle check skipped at HTTP handler (`should_accept_ingress_message`):**
Only the `IngressInductionCost::Fee` branch triggers `can_withdraw_cycles_with_threshold`. The `Free` branch falls through with no check. [5](#0-4) 

**Deferred charge silently discarded:**
After `update_settings` executes, `consume_cycles` is called but its `Result` is bound to `_ignore_error` and dropped. A canister with zero cycles will never pay. [6](#0-5) 

**Authorization checked only at execution time:**
`validate_controller` is called inside `canister_manager::update_settings`, not at induction. A non-controller sender's message is still inducted and executed (failing quickly), consuming resources before the authorization error is returned. [7](#0-6) 

## Impact Explanation
An unprivileged attacker can sustain a flood of `UpdateSettings` messages at the maximum ingress rate with zero cycle cost. Each message occupies an ingress pool slot, a block payload slot (bounded by `max_ingress_messages_per_block`), an ingress history entry, and consumes subnet execution instruction budget. This crowds out legitimate canister messages and degrades execution throughput for all canisters on the targeted subnet. This matches the allowed impact: **High — Application/platform-level DoS, subnet availability impact not based on raw volumetric DDoS** ($2,000–$10,000). The bypass is a specific protocol-level accounting gap, not a generic network flood.

## Likelihood Explanation
The attack requires no special privileges, no governance majority, no threshold corruption, and no leaked keys. Any user with network access to a subnet boundary node can construct a valid Candid-encoded `UpdateSettingsArgs` with a single `freezing_threshold` field (well under 338 bytes, as confirmed by the existing test) and submit it in a loop. The attack is fully repeatable and sustainable indefinitely. [8](#0-7) 

## Recommendation
1. **Decouple the balance check from the charge deferral.** At induction and HTTP handler validation, verify the canister has sufficient cycles to cover the induction cost even when the actual deduction is deferred to post-execution. The check and the charge can remain temporally separated.
2. **Do not silently discard the deferred charge error.** At minimum, log a warning and increment a metric when `consume_cycles` fails for a deferred `UpdateSettings` charge, so operators can detect abuse.
3. **Rate-limit free-induction management messages per sender principal** to bound worst-case resource consumption per attacker identity.

## Proof of Concept
```
# Attacker controls canister C with 0 cycles (or targets any canister as a non-controller).
# Construct a small UpdateSettings payload (well under 338 bytes):
payload = Candid.encode(UpdateSettingsArgs {
    canister_id: C,
    settings: CanisterSettingsArgs { freezing_threshold: Some(1) },
    sender_canister_version: None,
})
# payload.len() << 338 → is_delayed_ingress_induction_cost() returns true
# → ingress_induction_cost() returns IngressInductionCost::Free

# Submit at maximum ingress rate (no cycle cost to attacker):
loop:
    submit_ingress(to=IC_00, method="update_settings", payload=payload)
```

Each submitted message:
- Passes `should_accept_ingress_message` with no cycle check (Free branch, line 3351).
- Is inducted by `valid_set_rule::enqueue` with no cycle withdrawal (Free branch, line 287).
- Is included in a block by the ingress selector with no cycle check (Free branch, line 592).
- Executes `update_settings`; the deferred `consume_cycles` call fails silently (`_ignore_error`, line 1045).

A deterministic integration test using PocketIC or `ExecutionTestBuilder` can confirm this by: (1) creating a canister with 0 cycles, (2) submitting a small `UpdateSettings` ingress message from a non-controller, (3) asserting the message is inducted and executed, and (4) asserting the canister's cycle balance remains 0 after execution.

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

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L286-291)
```rust
        match induction_cost {
            IngressInductionCost::Free => {
                // Only subnet methods can be free. These are enqueued directly.
                assert!(ingress.is_addressed_to_subnet());
                state.push_ingress(ingress)
            }
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L592-594)
```rust
            IngressInductionCost::Free => {
                // Do nothing.
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

**File:** rs/execution_environment/src/canister_manager.rs (L651-653)
```rust
        let sender = origin.origin();

        validate_controller(canister, &sender)?;
```
