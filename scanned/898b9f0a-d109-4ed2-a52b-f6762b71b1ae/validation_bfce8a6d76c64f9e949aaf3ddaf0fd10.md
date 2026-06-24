### Title
Delayed Ingress Induction Cost for `UpdateSettings` Uses Partial Byte Count Instead of Full Binary Size, Causing Systematic Under-Charging — (`rs/execution_environment/src/execution_environment.rs`)

---

### Summary

When the ingress induction cost for `UpdateSettings` is deferred (to allow unfreezing of frozen canisters), the delayed charge path computes the fee using only `method_payload.len() + method_name.len()` instead of the full signed-ingress binary length (`ingress.binary().len()`) that the normal (non-delayed) path uses. Every `UpdateSettings` ingress with a small payload (≤ 338 bytes) is therefore systematically under-charged relative to any other ingress message of the same total wire size.

---

### Finding Description

The normal ingress induction cost is computed in `ingress_induction_cost()`:

```rust
// rs/cycles_account_manager/src/cycles_account_manager.rs  L565
let raw_bytes = NumBytes::from(ingress.binary().len() as u64);
```

`ingress.binary()` is the full CBOR-encoded, signed ingress wire message — it includes the sender principal, canister ID, method name, method payload, nonce, ingress expiry, and the cryptographic signature/public key. [1](#0-0) 

The delayed charge path, executed after `update_settings` is applied, uses a different byte count:

```rust
// rs/execution_environment/src/execution_environment.rs  L1033-1038
let bytes_to_charge =
    ingress.method_payload.len() + ingress.method_name.len();
let induction_cost = self
    .cycles_account_manager
    .ingress_induction_cost_from_bytes(
        NumBytes::from(bytes_to_charge as u64),
        subnet_cycles_config,
    );
``` [2](#0-1) 

`method_payload` and `method_name` are content-layer fields; they exclude the sender principal, nonce, ingress expiry, and the signature/public key that are part of the full binary. For a typical Ed25519-signed ingress, the omitted overhead is roughly 130–200 bytes (64-byte signature, 32-byte public key, 29-byte sender principal, 8-byte nonce, 8-byte expiry, plus CBOR framing).

The gate that triggers the delayed path is:

```rust
// rs/cycles_account_manager/src/cycles_account_manager.rs  L1379-1381
pub fn is_delayed_ingress_induction_cost(&self, arg: &[u8]) -> bool {
    arg.len() <= MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE  // 338 bytes
}
``` [3](#0-2) 

The fee formula is:

```rust
// rs/cycles_account_manager/src/cycles_account_manager.rs  L600-607
pub fn ingress_induction_cost_from_bytes(
    &self,
    bytes: NumBytes,
    subnet_cycles_config: CyclesAccountManagerSubnetConfig,
) -> CompoundCycles<IngressInduction> {
    self.ingress_message_received_fee(subnet_cycles_config)
        + self.ingress_byte_received_fee(subnet_cycles_config) * bytes.get()
}
``` [4](#0-3) 

With `ingress_byte_reception_fee = 2_000 cycles/byte` on an application subnet: [5](#0-4) 

The per-message under-charge is `2_000 × (binary_len − payload_len − method_name_len)` cycles, typically **260,000–400,000 cycles** per `UpdateSettings` call.

---

### Impact Explanation

Every `UpdateSettings` ingress message whose payload is ≤ 338 bytes is under-charged by the difference between the full binary size and the content-only size. Because `UpdateSettings` is the standard management-canister call used to change freezing thresholds, controllers, memory allocations, and compute allocations, this path is exercised by every canister developer and toolchain. The under-charge is not refunded elsewhere; it is a permanent revenue leak from the subnet's cycle economy. A high-volume integrator (e.g., a canister factory or orchestrator) that issues many `UpdateSettings` calls accumulates a proportionally larger discount.

---

### Likelihood Explanation

The trigger condition — an `UpdateSettings` ingress with payload ≤ 338 bytes — is the default for all standard `update_settings` calls (the typical Candid-encoded payload for changing a single setting is well under 100 bytes). No special privileges are required; any unprivileged user with a canister they control can reach this path on every application subnet. The bug fires on every such call, making it high-frequency and deterministic.

---

### Recommendation

Replace the partial byte count in the delayed charge path with the full signed-ingress binary length, consistent with the non-delayed path:

```rust
// rs/execution_environment/src/execution_environment.rs
if let CanisterCall::Ingress(ingress) = &msg {
    let subnet_cycles_config = state.get_own_subnet_cycles_config();
    if let Ok(canister) = canister_make_mut(canister_id, &mut state)
        && self.cycles_account_manager
               .is_delayed_ingress_induction_cost(&ingress.method_payload)
    {
        // FIX: use the full binary length, matching ingress_induction_cost()
        let bytes_to_charge = ingress.binary().len();
        let induction_cost = self.cycles_account_manager
            .ingress_induction_cost_from_bytes(
                NumBytes::from(bytes_to_charge as u64),
                subnet_cycles_config,
            );
        // ...
    }
}
```

This aligns the delayed charge with the formula used in `ingress_induction_cost()`.

---

### Proof of Concept

1. Construct a standard `UpdateSettings` ingress message targeting a canister you control, setting `freezing_threshold` to 30 days (payload ≈ 50 bytes, well under 338).
2. The boundary node charges the canister `ingress_message_reception_fee + ingress_byte_reception_fee × 0` (free, because `is_delayed_ingress_induction_cost` returns `true` and the normal path returns `IngressInductionCost::Free`).
3. After `update_settings` executes, the delayed charge fires with `bytes_to_charge = method_payload.len() + method_name.len()` ≈ 50 + 15 = 65 bytes.
4. The actual binary length of the same signed ingress is ≈ 65 + 140 (signature + sender + nonce + expiry + CBOR) = 205 bytes.
5. Under-charge per call = `2_000 × (205 − 65)` = **280,000 cycles**.
6. A canister factory issuing 10,000 `UpdateSettings` calls saves ≈ **2.8 billion cycles** compared to what the protocol intends to charge. [2](#0-1) [6](#0-5)

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L43-45)
```rust
/// Maximum payload size of a management call to update_settings
/// overriding the canister's freezing threshold.
const MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE: usize = 338;
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L559-597)
```rust
    pub fn ingress_induction_cost(
        &self,
        ingress: &SignedIngress,
        effective_canister_id: Option<CanisterId>,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
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
    }
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L599-607)
```rust
    /// Returns the cost of an ingress message based on the message size.
    pub fn ingress_induction_cost_from_bytes(
        &self,
        bytes: NumBytes,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
    ) -> CompoundCycles<IngressInduction> {
        self.ingress_message_received_fee(subnet_cycles_config)
            + self.ingress_byte_received_fee(subnet_cycles_config) * bytes.get()
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

**File:** rs/execution_environment/src/execution_environment.rs (L1026-1053)
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
```

**File:** rs/config/src/subnet_config.rs (L514-515)
```rust
            ingress_message_reception_fee: Cycles::new(1_200_000),
            ingress_byte_reception_fee: Cycles::new(2_000),
```
