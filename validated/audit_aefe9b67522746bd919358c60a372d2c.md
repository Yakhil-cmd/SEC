### Title
Cycles Accounting Discrepancy in Delayed `UpdateSettings` Ingress Induction Cost — (`File: rs/execution_environment/src/execution_environment.rs`)

---

### Summary

When an ingress `UpdateSettings` message has a small payload (below `MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE`), the IC intentionally defers charging the induction fee until after the settings are applied. However, the deferred charge is computed from only `method_payload.len() + method_name.len()` bytes, while the normal (non-deferred) path charges based on `ingress.binary().len()` — the full serialized wire size of the signed ingress envelope. This creates a systematic undercharge for the deferred path, analogous to the EVM bug where two different code paths compute the same resource cost differently.

---

### Finding Description

The IC's `ingress_induction_cost` function computes the fee using the full binary (wire) size of the signed ingress message:

```rust
let raw_bytes = NumBytes::from(ingress.binary().len() as u64);
// ...
let cost = self.ingress_induction_cost_from_bytes(raw_bytes, subnet_cycles_config);
``` [1](#0-0) 

For `UpdateSettings` with a small payload, `ingress_induction_cost` returns `IngressInductionCost::Free` (no upfront charge), and the deferred charge is applied later in `execute_subnet_message`:

```rust
let bytes_to_charge =
    ingress.method_payload.len() + ingress.method_name.len();
let induction_cost = self
    .cycles_account_manager
    .ingress_induction_cost_from_bytes(
        NumBytes::from(bytes_to_charge as u64),
        subnet_cycles_config,
    );
``` [2](#0-1) 

The deferred path charges only `method_payload.len() + method_name.len()` bytes, whereas the normal path charges `ingress.binary().len()` — the full CBOR/COSE-encoded envelope, which includes the sender principal, ingress expiry, nonce, request ID, and signature. The wire size is always larger than the sum of just the two application-layer fields.

The `is_delayed_ingress_induction_cost` gate:

```rust
pub fn is_delayed_ingress_induction_cost(&self, arg: &[u8]) -> bool {
    arg.len() <= MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE
}
``` [3](#0-2) 

...only checks the argument size, not the full wire size, so the deferred path is always triggered for small `UpdateSettings` payloads.

The normal (non-deferred) path charges the full wire size:

```rust
let raw_bytes = NumBytes::from(ingress.binary().len() as u64);
``` [4](#0-3) 

The deferred path charges only the application-layer fields:

```rust
let bytes_to_charge = ingress.method_payload.len() + ingress.method_name.len();
``` [5](#0-4) 

The `ingress_byte_reception_fee` is 2,000 cycles/byte on application subnets: [6](#0-5) 

A typical signed ingress envelope overhead (sender, expiry, nonce, signature) is on the order of 100–300 bytes. At 2,000 cycles/byte, this is 200,000–600,000 cycles of undercharge per message. The `_ignore_error` on the `consume_cycles` call means even if the canister is out of cycles, the charge silently fails:

```rust
let _ignore_error = self.cycles_account_manager.consume_cycles(
    ...
    false, // we ignore the error anyway
);
``` [7](#0-6) 

---

### Impact Explanation

Any unprivileged user who is a controller of a canister can repeatedly send small `UpdateSettings` ingress messages (e.g., toggling the freezing threshold) and pay fewer cycles than the actual network cost of processing those messages. The canister pays less than the true cost of the bytes transmitted through the subnet, creating a cycles accounting discrepancy. At scale (many messages per block), this allows a canister to consume more network bandwidth than it pays for, subsidized by the subnet. The impact is a **cycles/resource accounting bug** — the canister underpays for ingress induction, which is the IC analog of the EVM "activity points exceed gas spent" issue.

---

### Likelihood Explanation

The trigger condition is straightforward: send an `UpdateSettings` ingress message with a payload below `MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE`. This is the normal, common case for `UpdateSettings` (e.g., changing the freezing threshold). Any canister controller can do this without any special privileges. The code path is exercised on every such call on every application subnet.

---

### Recommendation

In the deferred charging path in `execute_subnet_message`, compute `bytes_to_charge` from the full signed ingress wire size (as `ingress.binary().len()` is used in the normal path), not just `method_payload.len() + method_name.len()`. Alternatively, store the full wire byte count at induction time and use it for the deferred charge, ensuring both paths use the same accounting basis.

---

### Proof of Concept

1. Deploy a canister on an application subnet. Record its cycle balance `B0`.
2. Send an `UpdateSettings` ingress message with a small payload (e.g., `freezing_threshold = 1`). This triggers the deferred path (`is_delayed_ingress_induction_cost` returns `true`).
3. Record the cycle balance `B1` after execution.
4. Compute `expected_charge = ingress_message_reception_fee + ingress_byte_reception_fee * ingress.binary().len()` (the normal path formula).
5. Compute `actual_charge = B0 - B1`.
6. Observe `actual_charge < expected_charge` by approximately `ingress_byte_reception_fee * (envelope_overhead_bytes)`, where `envelope_overhead_bytes` is the difference between the full wire size and `method_payload.len() + method_name.len()`.

The existing test `unfreezing_of_frozen_canister` in `rs/execution_environment/src/canister_manager/tests.rs` already demonstrates the deferred charge uses only `method_payload.len() + method_name.len()` bytes, confirming the discrepancy:

```rust
let ingress_bytes =
    NumBytes::from((Method::UpdateSettings.to_string().len() + payload.len()) as u64);
``` [8](#0-7) 

This test validates the current (undercharging) behavior rather than the correct full-wire-size behavior, confirming the bug is present and untested against the correct baseline.

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L565-589)
```rust
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
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L1379-1381)
```rust
    pub fn is_delayed_ingress_induction_cost(&self, arg: &[u8]) -> bool {
        arg.len() <= MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE
    }
```

**File:** rs/execution_environment/src/execution_environment.rs (L1033-1040)
```rust
                                let bytes_to_charge =
                                    ingress.method_payload.len() + ingress.method_name.len();
                                let induction_cost = self
                                    .cycles_account_manager
                                    .ingress_induction_cost_from_bytes(
                                        NumBytes::from(bytes_to_charge as u64),
                                        subnet_cycles_config,
                                    );
```

**File:** rs/execution_environment/src/execution_environment.rs (L1045-1052)
```rust
                                let _ignore_error = self.cycles_account_manager.consume_cycles(
                                    &mut canister.system_state,
                                    memory_usage,
                                    message_memory_usage,
                                    induction_cost,
                                    subnet_cycles_config,
                                    false, // we ignore the error anyway => no need to reveal top up balance
                                );
```

**File:** rs/config/src/subnet_config.rs (L514-515)
```rust
            ingress_message_reception_fee: Cycles::new(1_200_000),
            ingress_byte_reception_fee: Cycles::new(2_000),
```

**File:** rs/execution_environment/src/canister_manager/tests.rs (L3664-3665)
```rust
    let ingress_bytes =
        NumBytes::from((Method::UpdateSettings.to_string().len() + payload.len()) as u64);
```
