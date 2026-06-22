### Title
Incomplete Ingress Induction Cost Accounting for Delayed `UpdateSettings` Path — (`rs/execution_environment/src/execution_environment.rs`)

---

### Summary

The delayed ingress induction cost path for `UpdateSettings` messages charges only `method_payload.len() + method_name.len()` bytes, while the normal ingress induction path charges for the full signed-ingress binary (`ingress.binary().len()`). This is a direct analog to the Atlas.sol bug where `claims` only tracked execution costs between two `gasleft()` markers and missed the base 21,000 gas and per-byte calldata overhead.

---

### Finding Description

The IC charges canisters for ingress messages via `ingress_induction_cost_from_bytes`, which computes:

```
ingress_message_reception_fee + ingress_byte_reception_fee × bytes
```

In the **normal path**, `bytes` is the full signed-ingress binary length:

```rust
// rs/cycles_account_manager/src/cycles_account_manager.rs:565
let raw_bytes = NumBytes::from(ingress.binary().len() as u64);
``` [1](#0-0) 

The full binary includes: CBOR encoding overhead, sender principal, canister ID, method name, method payload, nonce, expiry, and signature — typically 240–350 bytes for a small `UpdateSettings` message.

In the **delayed `UpdateSettings` path**, however, only a subset of those bytes is charged:

```rust
// rs/execution_environment/src/execution_environment.rs:1033-1038
let bytes_to_charge =
    ingress.method_payload.len() + ingress.method_name.len();
let induction_cost = self
    .cycles_account_manager
    .ingress_induction_cost_from_bytes(
        NumBytes::from(bytes_to_charge as u64),
        subnet_cycles_config,
    );
``` [2](#0-1) 

This path is triggered when `is_delayed_ingress_induction_cost` returns `true`, i.e., when the `UpdateSettings` payload is ≤ `MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE` (338 bytes): [3](#0-2) 

The bytes for sender principal (~30 bytes), canister ID (~10 bytes), nonce (~10 bytes), expiry (~10 bytes), signature (~64 bytes), and CBOR framing (~50 bytes) are **never charged** in this path — a shortfall of roughly 125–225 bytes per message.

At `ingress_byte_reception_fee = 2,000 cycles/byte`, this represents ~250,000–450,000 cycles of undercharging per `UpdateSettings` message with a small payload. [4](#0-3) 

---

### Impact Explanation

Any canister controller can repeatedly send `UpdateSettings` ingress messages with small payloads (≤ 338 bytes) to their canister. Each message is undercharged by ~250,000–450,000 cycles compared to what the normal ingress induction path would charge. Over many messages, this allows a canister to consume subnet ingress-processing resources (consensus bandwidth, induction queue processing) at a below-cost rate, subsidized by the subnet. The base `ingress_message_reception_fee` (1,200,000 cycles) is still charged, so the impact is bounded, but the per-byte overhead for the non-payload fields is systematically omitted.

---

### Likelihood Explanation

The trigger condition is straightforward and reachable by any unprivileged canister controller: send an `UpdateSettings` ingress message with a payload ≤ 338 bytes to any canister they control. No special permissions, governance majority, or threshold corruption is required. The path is exercised on every application subnet for every small `UpdateSettings` call.

---

### Recommendation

In the delayed `UpdateSettings` charging path, replace `bytes_to_charge` with the full signed-ingress binary length, consistent with the normal path:

```rust
// Instead of:
let bytes_to_charge = ingress.method_payload.len() + ingress.method_name.len();

// Use:
let bytes_to_charge = ingress.binary().len();
// or pass the pre-computed raw_bytes from the induction-cost calculation
```

Alternatively, store the full `raw_bytes` value computed at induction time and reuse it during the delayed charge, so both paths use the same byte-count basis.

---

### Proof of Concept

1. A canister controller sends an `UpdateSettings` ingress message to `IC_00` targeting their canister, with a payload of ~50 bytes (e.g., setting `freezing_threshold`).
2. At induction time (`valid_set_rule.rs`), `ingress_induction_cost` returns `IngressInductionCost::Free` because `is_delayed_ingress_induction_cost` returns `true` — **no cycles are charged at induction**.
3. At execution time (`execution_environment.rs:1033`), the delayed charge is applied using only `method_payload.len() + method_name.len()` ≈ 65 bytes.
4. The normal path would have charged for `ingress.binary().len()` ≈ 280 bytes.
5. The canister is undercharged by `(280 - 65) × 2,000 = 430,000 cycles` per message.
6. Repeating this in a loop (rate-limited only by ingress queue depth and expiry windows) allows sustained below-cost consumption of subnet ingress resources. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L564-596)
```rust
    ) -> IngressInductionCost {
        let raw_bytes = NumBytes::from(ingress.binary().len() as u64);
        let ingress = ingress.content();
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

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L1379-1381)
```rust
    pub fn is_delayed_ingress_induction_cost(&self, arg: &[u8]) -> bool {
        arg.len() <= MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE
    }
```

**File:** rs/execution_environment/src/execution_environment.rs (L1026-1054)
```rust
                        if let CanisterCall::Ingress(ingress) = &msg {
                            let subnet_cycles_config = state.get_own_subnet_cycles_config();
                            if let Ok(canister) = canister_make_mut(canister_id, &mut state)
                                && self
                                    .cycles_account_manager
                                    .is_delayed_ingress_induction_cost(&ingress.method_payload)
                            {
                                let bytes_to_charge =
                                    ingress.method_payload.len() + ingress.method_name.len();
                                let induction_cost = self
                                    .cycles_account_manager
                                    .ingress_induction_cost_from_bytes(
                                        NumBytes::from(bytes_to_charge as u64),
                                        subnet_cycles_config,
                                    );
                                let memory_usage = canister.memory_usage();
                                let message_memory_usage = canister.message_memory_usage();
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
                        }
```

**File:** rs/config/src/subnet_config.rs (L514-515)
```rust
            ingress_message_reception_fee: Cycles::new(1_200_000),
            ingress_byte_reception_fee: Cycles::new(2_000),
```

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L277-295)
```rust
        // Compute the cost of induction.
        let subnet_cycles_config = state.get_own_subnet_cycles_config();
        let induction_cost = self.cycles_account_manager.ingress_induction_cost(
            &signed_ingress,
            effective_canister_id,
            subnet_cycles_config,
        );

        let ingress = Ingress::from((signed_ingress.take_content(), effective_canister_id));
        match induction_cost {
            IngressInductionCost::Free => {
                // Only subnet methods can be free. These are enqueued directly.
                assert!(ingress.is_addressed_to_subnet());
                state.push_ingress(ingress)
            }

            IngressInductionCost::Fee { payer, cost } => {
                // Get the paying canister from the state.
                let canister = match state.canister_state_make_mut(&payer) {
```
