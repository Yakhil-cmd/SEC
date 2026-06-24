### Title
`UpdateSettings` Ingress Messages with Small Payloads Bypass Upfront Cycles Fee, Enabling Cheap Block Stuffing - (`rs/cycles_account_manager/src/cycles_account_manager.rs`)

---

### Summary

`UpdateSettings` management canister ingress messages whose payload is ≤ 338 bytes (`MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE`) are classified as `IngressInductionCost::Free` at the induction layer. Every cycles check in the ingress filter, ingress selector, and valid-set-rule is skipped for free messages. The delayed fee that is supposed to be charged after execution explicitly ignores its own error. An attacker who controls any canister can therefore flood blocks with up to 1,000 `UpdateSettings` messages per block at effectively zero upfront cost, crowding out legitimate transactions.

---

### Finding Description

In `ingress_induction_cost()`, when the ingress message targets `IC_00` with method `UpdateSettings` and its argument is ≤ `MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE` (338 bytes), `paying_canister` is set to `None`, causing the function to return `IngressInductionCost::Free`: [1](#0-0) 

The constant that gates this behaviour: [2](#0-1) 

The predicate: [3](#0-2) 

Every downstream check branches on the `IngressInductionCost` variant and does nothing for `Free`:

**Ingress filter** (`should_accept_ingress_message`): [4](#0-3) 

**Ingress selector** (`validate_ingress`): [5](#0-4) 

**Valid-set-rule** (`enqueue`): [6](#0-5) 

The delayed fee that is charged after `UpdateSettings` executes explicitly discards the error: [7](#0-6) 

So if the target canister is already out of cycles, the fee is silently dropped and the message was processed for free end-to-end.

The per-block ingress message cap is: [8](#0-7) 

---

### Impact Explanation

An attacker who controls at least one canister (a one-time cost) can continuously submit 1,000 `UpdateSettings` ingress messages per block targeting their own canister. Because all 1,000 messages are classified `Free` at induction time, they pass every cycles gate without consuming any cycles upfront. This fills the entire ingress payload of every block, crowding out all legitimate user transactions. Protocols that depend on timely execution of ingress messages (e.g., liquidations, time-sensitive governance actions) are denied service for as long as the attacker sustains the flood. If the attacker's canister is drained of cycles, the post-execution fee is silently ignored, making the attack self-sustaining at near-zero cost.

---

### Likelihood Explanation

The entry path requires only that the attacker has created one canister on an application subnet (a routine, low-cost operation). No privileged role, no threshold corruption, and no external dependency is needed. The attack is fully automated and can be sustained indefinitely. The `UpdateSettings` method is a standard management canister call available to any principal who controls a canister.

---

### Recommendation

1. Remove the `IngressInductionCost::Free` special case for `UpdateSettings`. Charge the induction cost upfront at the ingress selector and valid-set-rule, exactly as every other management canister method is charged. The "unfreeze" use-case can be preserved by allowing the fee to be charged against the canister's balance even when it is below the freeze threshold (i.e., relax the threshold check only for this specific operation, rather than waiving the fee entirely).
2. If the delayed-charge design must be kept, the post-execution `consume_cycles` error must not be silently ignored; it should at minimum be logged as a critical metric so that fee evasion is observable.
3. Ensure the ingress selector's cumulative-cycles accounting (`cycles_needed` map) covers `Free` messages that will incur a delayed charge, so that a block cannot be stuffed with messages whose aggregate delayed cost exceeds what the canister can pay.

---

### Proof of Concept

1. Attacker creates canister `C` on an application subnet with a minimal cycles balance.
2. Attacker constructs a minimal `UpdateSettingsArgs` payload (e.g., setting `freezing_threshold = 1`) encoded to ≤ 338 bytes.
3. Attacker submits 1,000 distinct `UpdateSettings` ingress messages (varying the nonce) targeting `C` via the boundary node.
4. `ingress_induction_cost()` returns `IngressInductionCost::Free` for each message; all 1,000 pass the ingress filter and selector with zero cycles deducted.
5. The ingress selector builds a block payload containing all 1,000 messages, exhausting `MAX_INGRESS_MESSAGES_PER_BLOCK`.
6. Legitimate user transactions submitted in the same round are excluded from the block.
7. After execution, the post-execution fee attempt on `C` either succeeds (small cost) or is silently ignored if `C` is out of cycles.
8. The attacker repeats every round.

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L43-45)
```rust
/// Maximum payload size of a management call to update_settings
/// overriding the canister's freezing threshold.
const MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE: usize = 338;
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L567-595)
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

**File:** rs/ingress_manager/src/ingress_selector.rs (L556-594)
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

**File:** rs/limits/src/lib.rs (L78-78)
```rust
pub const MAX_INGRESS_MESSAGES_PER_BLOCK: u64 = 1000;
```
