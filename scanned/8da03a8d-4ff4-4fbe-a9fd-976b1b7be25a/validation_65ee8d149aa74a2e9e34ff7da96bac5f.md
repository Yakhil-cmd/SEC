### Title
Zero-Cost Ingress Induction for `UpdateSettings` and `CreateCanister` Enables Ingress Queue Flooding - (File: `rs/cycles_account_manager/src/cycles_account_manager.rs`)

---

### Summary

Two management canister (`ic:00`) ingress paths — `UpdateSettings` with a small payload and `CreateCanister` — are inducted into the replicated state with zero cycles cost on application subnets. Any unprivileged user can exploit this to flood the subnet's ingress queue at no cost, starving legitimate messages.

---

### Finding Description

**Path 1 — `UpdateSettings` with small payload (≤ 338 bytes):**

`ingress_induction_cost` in `rs/cycles_account_manager/src/cycles_account_manager.rs` contains a deliberate design exception: when the method is `UpdateSettings` and the payload is ≤ `MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE` (338 bytes), `paying_canister` is set to `None`, causing the function to return `IngressInductionCost::Free`. [1](#0-0) [2](#0-1) 

The design intent is to allow a canister owner to lower an accidentally-too-high freezing threshold even when the canister is frozen. However, the authorization check (whether the sender is a controller) happens only during execution, not at induction time. Any unprivileged user can therefore send `UpdateSettings` to any canister and have it inducted for free.

The deferred charge applied during execution explicitly ignores the error: [3](#0-2) 

This means even if the canister has zero cycles, the message is processed for free end-to-end.

**Path 2 — `CreateCanister` (and `ProvisionalCreateCanisterWithCycles`, `ProvisionalTopUpCanister`):**

`extract_effective_canister_id` returns `Ok(None)` for `CreateCanister`: [4](#0-3) 

Because `effective_canister_id` is `None` and the method is not `UpdateSettings`, `ingress_induction_cost` falls through to `effective_canister_id` (which is `None`), yielding `IngressInductionCost::Free`: [5](#0-4) 

Any user can send `CreateCanister` ingress messages (which will fail execution for lack of attached cycles, but induction is free).

**Induction path with no cycle gate:**

In `valid_set_rule.rs`, `IngressInductionCost::Free` messages are pushed directly into the ingress queue with no cycle balance check: [6](#0-5) 

The only backstop is the `ingress_history_max_messages` cap, which is a global per-subnet limit — not per-sender — so an attacker can monopolize it: [7](#0-6) 

The same zero-cost pass-through exists in the ingress selector's `validate_ingress_payload`: [8](#0-7) 

---

### Impact Explanation

An unprivileged user can continuously submit `UpdateSettings` (or `CreateCanister`) ingress messages at zero cycles cost. These messages fill the subnet's ingress history up to `ingress_history_max_messages`. Once the cap is reached, all subsequent legitimate ingress messages are rejected with `IngressHistoryFull`, causing a complete denial of service of the application subnet's ingress path. The attacker pays only the standard network transport cost (no cycles are burned), while the subnet's execution rounds are consumed processing the flood of failing messages.

---

### Likelihood Explanation

The attack requires only a valid IC identity (any anonymous or self-authenticating principal) and knowledge of any existing canister ID on the target subnet. No privileged access, governance majority, or threshold corruption is needed. The `UpdateSettings` payload is trivially constructable (a Candid-encoded struct with a canister ID and any settings field). The attack is fully reachable from the public `/api/v3/canister/.../call` endpoint.

---

### Recommendation

1. **Remove the unconditional free-induction exception for `UpdateSettings`.** Instead of making induction free, charge the induction cost upfront and refund it if the canister is found to be frozen and the sender is a controller. Alternatively, gate the free-induction path on a pre-check that the sender is a controller of the target canister.
2. **Assign a non-zero induction cost to `CreateCanister` ingress messages.** Since no canister pays for them today (effective canister ID is `None`), introduce a sender-funded deposit or require the sender to have a registered identity with a cycle balance.
3. **Add a per-sender rate limit** in the ingress pool / ingress selector to prevent any single principal from monopolizing the ingress history.

---

### Proof of Concept

```
# Attacker constructs a minimal UpdateSettings payload (≤ 338 bytes):
# Candid: record { canister_id = <any_canister_id>; settings = record {} }
# This is well under MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE = 338 bytes.

# Send in a tight loop to the target application subnet:
for i in 1..N:
    POST /api/v3/canister/aaaaa-aa/call
    {
      request_type: "call",
      canister_id: "aaaaa-aa",   # ic:00
      method_name: "update_settings",
      arg: <candid-encoded UpdateSettingsArgs with target canister_id>,
      sender: <attacker_principal>,
      ingress_expiry: <now + 5min>,
      ...
    }

# Each message is inducted with IngressInductionCost::Free.
# After ingress_history_max_messages messages, all legitimate
# ingress to the subnet is rejected with IngressHistoryFull.
```

Root cause confirmed at:
- `rs/cycles_account_manager/src/cycles_account_manager.rs` lines 570–578 (`ingress_induction_cost` free path for `UpdateSettings`)
- `rs/cycles_account_manager/src/cycles_account_manager.rs` line 43–45 (`MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE = 338`)
- `rs/execution_environment/src/execution_environment.rs` lines 1043–1052 (deferred charge silently ignored)
- `rs/messaging/src/scheduling/valid_set_rule.rs` lines 286–291 (free messages enqueued without any cycle gate)
- `rs/types/types/src/messages/ingress_messages.rs` lines 585–587 (`CreateCanister` returns `Ok(None)` → free induction)

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

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L579-595)
```rust
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

**File:** rs/types/types/src/messages/ingress_messages.rs (L585-587)
```rust
        Ok(Method::CreateCanister)
        | Ok(Method::ProvisionalCreateCanisterWithCycles)
        | Ok(Method::ProvisionalTopUpCanister) => Ok(None),
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

**File:** rs/ingress_manager/src/ingress_selector.rs (L592-594)
```rust
            IngressInductionCost::Free => {
                // Do nothing.
            }
```
