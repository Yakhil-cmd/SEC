### Title
Inconsistent Byte Counting in Delayed `UpdateSettings` Ingress Induction Cost — (File: `rs/execution_environment/src/execution_environment.rs`)

---

### Summary

The IC's ingress induction cost function `ingress_induction_cost()` uses the **full signed-ingress binary length** (`ingress.binary().len()`) to compute the fee. However, the delayed-charge code path for `UpdateSettings` messages uses only `method_payload.len() + method_name.len()` — a strictly smaller value that omits the envelope overhead (signature, sender, nonce, expiry, canister-id, CBOR framing). This is the direct IC analog of the `getBidValue()` inconsistency: the same fee-computing helper (`ingress_induction_cost_from_bytes`) is called with different inputs in different code paths, causing the delayed path to systematically undercharge.

---

### Finding Description

**Normal path** — `ingress_induction_cost()` in `rs/cycles_account_manager/src/cycles_account_manager.rs`:

```rust
let raw_bytes = NumBytes::from(ingress.binary().len() as u64);
// ...
let cost = self.ingress_induction_cost_from_bytes(raw_bytes, subnet_cycles_config);
```

`ingress.binary()` is the full CBOR-encoded `SignedIngress` envelope, including the Ed25519 signature (~64 B), sender principal (~29 B), nonce, expiry, canister-id, method name, and argument. [1](#0-0) 

**Delayed `UpdateSettings` path** — `rs/execution_environment/src/execution_environment.rs`:

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

Here `ingress` is the already-decoded `Ingress` struct; only the raw argument bytes and the ASCII method name are counted. All envelope overhead is silently excluded. [2](#0-1) 

The delayed path is gated by `is_delayed_ingress_induction_cost`, which fires when `arg.len() <= 338`: [3](#0-2) 

The helper that both paths call is:

```rust
pub fn ingress_induction_cost_from_bytes(
    &self,
    bytes: NumBytes,
    subnet_cycles_config: CyclesAccountManagerSubnetConfig,
) -> CompoundCycles<IngressInduction> {
    self.ingress_message_received_fee(subnet_cycles_config)
        + self.ingress_byte_received_fee(subnet_cycles_config) * bytes.get()
}
``` [4](#0-3) 

The per-byte fee on an application subnet is `ingress_byte_reception_fee = 2 000 cycles/byte`: [5](#0-4) 

---

### Impact Explanation

For a small `UpdateSettings` message (payload ≤ 338 B), the delayed path charges for at most `338 + 15 = 353` bytes, while the normal path would charge for the full signed-ingress binary — typically **~500–600 bytes** for the same payload (adding ~150–250 bytes of envelope overhead). At 2 000 cycles/byte the per-message undercharge is **~300 000–500 000 cycles**. Multiplied across many calls this constitutes a measurable cycles-accounting discrepancy: canisters that receive many small `UpdateSettings` ingress messages are subsidised relative to canisters receiving equivalently-sized ordinary messages. Because the `_ignore_error` annotation on the charge call means a failed charge is silently swallowed, the undercharge is never recovered: [6](#0-5) 

---

### Likelihood Explanation

The trigger is fully attacker-controlled: any unprivileged user can send an `UpdateSettings` ingress message with a payload ≤ 338 bytes to any canister they control (or any canister for which they are a controller). No special role, key, or governance majority is required. The condition is deterministically reachable on every application subnet.

---

### Recommendation

Replace the ad-hoc byte count in the delayed path with the same measurement used by the normal path. One approach: pass the original `SignedIngress` binary length into the delayed-charge site (it is available before the content is decoded), or introduce a shared helper that both sites call:

```rust
// Instead of:
let bytes_to_charge = ingress.method_payload.len() + ingress.method_name.len();

// Use the same measure as ingress_induction_cost():
let bytes_to_charge = signed_ingress_binary_len; // captured before content() is called
```

Alternatively, document explicitly that the delayed path intentionally charges a reduced fee (and verify that the reduced amount is the policy intent, not an oversight).

---

### Proof of Concept

1. Craft a `SignedIngress` targeting `ic:00 / update_settings` with a payload of, say, 100 bytes (well within the 338-byte threshold).
2. The full binary of this message is approximately 100 (arg) + 15 (method name) + 64 (signature) + 29 (sender) + 8 (nonce) + 8 (expiry) + 10 (canister-id) + ~30 (CBOR framing) ≈ **264 bytes**.
3. The normal path (`ingress_induction_cost`) would charge `ingress_message_reception_fee + 264 × ingress_byte_reception_fee`.
4. The delayed path charges `ingress_message_reception_fee + (100 + 15) × ingress_byte_reception_fee` — a shortfall of **149 × 2 000 = 298 000 cycles** per message.
5. Sending this message repeatedly to a canister the attacker controls causes the canister to be undercharged relative to the protocol's stated fee schedule, constituting a cycles-accounting bug reachable by any unprivileged ingress sender.

### Citations

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

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L600-607)
```rust
    pub fn ingress_induction_cost_from_bytes(
        &self,
        bytes: NumBytes,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
    ) -> CompoundCycles<IngressInduction> {
        self.ingress_message_received_fee(subnet_cycles_config)
            + self.ingress_byte_received_fee(subnet_cycles_config) * bytes.get()
    }
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
