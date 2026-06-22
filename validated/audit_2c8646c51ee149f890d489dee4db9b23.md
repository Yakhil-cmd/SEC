### Title
Inconsistent Ingress Induction Fee Calculation for `UpdateSettings` Delayed Charging Path — (`rs/cycles_account_manager/src/cycles_account_manager.rs`, `rs/execution_environment/src/execution_environment.rs`)

---

### Summary

The IC replica uses two different byte-count criteria to calculate the ingress induction fee for `UpdateSettings` management canister messages. The normal path charges based on the full signed ingress binary length (`ingress.binary().len()`), while the delayed charging path charges based only on `method_payload.len() + method_name.len()`. This inconsistency causes canisters to be undercharged for `UpdateSettings` messages with small payloads, analogous to the reported inconsistency between `swap` instruction fee adjustment and `get_swap_fees` stable-swap criterion.

---

### Finding Description

**Normal ingress induction path** (`ingress_induction_cost`):

The fee is computed from the full signed ingress binary, which includes the CBOR envelope, sender principal, sender public key, sender signature, ingress expiry, nonce, method name, and method payload:

```rust
// rs/cycles_account_manager/src/cycles_account_manager.rs:565
let raw_bytes = NumBytes::from(ingress.binary().len() as u64);
// ...
let cost = self.ingress_induction_cost_from_bytes(raw_bytes, subnet_cycles_config);
```

For `UpdateSettings` with a small payload (≤ `MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE` = 338 bytes), the paying canister is set to `None` (free at induction time):

```rust
// rs/cycles_account_manager/src/cycles_account_manager.rs:574-575
if self.is_delayed_ingress_induction_cost(ingress.arg()) {
    None  // Free at induction
```

**Delayed charging path** (`execute_subnet_message` for `UpdateSettings`):

After the settings are applied, the fee is charged using a narrower byte count — only `method_payload.len() + method_name.len()`, omitting the signature, sender principal, public key, CBOR framing, and other envelope fields:

```rust
// rs/execution_environment/src/execution_environment.rs:1033-1040
let bytes_to_charge =
    ingress.method_payload.len() + ingress.method_name.len();
let induction_cost = self
    .cycles_account_manager
    .ingress_induction_cost_from_bytes(
        NumBytes::from(bytes_to_charge as u64),
        subnet_cycles_config,
    );
```

The `is_delayed_ingress_induction_cost` check at induction time uses `ingress.arg()` (the raw payload bytes), while the delayed charge uses `ingress.method_payload` — these are the same field, but the fee basis differs from the normal path by excluding all envelope overhead.

---

### Impact Explanation

A typical Ed25519-signed `UpdateSettings` ingress message with a 100-byte payload has a full binary length of approximately 300–400 bytes (100 payload + 15 method name + 29 sender principal + 44 pubkey + 64 signature + ~50 CBOR framing). The delayed path charges only for ~115 bytes (100 + 15), roughly a **3–4× fee reduction** compared to any other management canister message of the same total wire size.

On a 13-node application subnet with `ingress_byte_reception_fee = 2_000 cycles/byte`:
- Normal fee for a 350-byte signed ingress: `1_200_000 + 350 × 2_000 = 1_900_000 cycles`
- Delayed fee for the same message: `1_200_000 + 115 × 2_000 = 1_430_000 cycles`
- **Undercharge: ~470,000 cycles per message**

Additionally, the delayed charge is silently ignored on failure (`let _ignore_error = ...`), meaning if the canister's balance is depleted after applying the new settings, the fee is entirely waived — a complete bypass of the induction cost for `UpdateSettings`.

---

### Likelihood Explanation

Any unprivileged user who controls a canister can trigger this path by sending an `UpdateSettings` ingress message with a payload ≤ 338 bytes (the `MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE` threshold). This is the normal, common case for `UpdateSettings` (e.g., changing the freezing threshold). The path is reachable on every application subnet without any special privileges.

---

### Recommendation

In the delayed charging path, compute `bytes_to_charge` from the full signed ingress binary length (consistent with the normal path), not just `method_payload.len() + method_name.len()`. The `Ingress` struct available at execution time should carry or allow reconstruction of the original wire size. Alternatively, store the original `raw_bytes` value computed at induction time alongside the ingress message so it can be reused at the delayed charging site.

Additionally, the `_ignore_error` suppression of the delayed charge should be reconsidered: if the canister cannot pay after settings are applied, the error should at minimum be logged as a critical metric rather than silently dropped.

---

### Proof of Concept

**Step 1 — Attacker-controlled entry path:**
An unprivileged user sends an `UpdateSettings` ingress message to a canister they control, with a payload ≤ 338 bytes (e.g., setting `freezing_threshold`).

**Step 2 — Induction (free):**
`ingress_induction_cost` detects `is_delayed_ingress_induction_cost(arg) == true` and returns `IngressInductionCost::Free`. The message is enqueued at zero cost.

**Step 3 — Execution (undercharged):**
`execute_subnet_message` applies the settings, then charges `ingress_induction_cost_from_bytes(method_payload.len() + method_name.len())` instead of `ingress_induction_cost_from_bytes(ingress.binary().len())`. The canister pays ~3–4× fewer cycles than it would for any other management message of the same wire size.

**Step 4 — Silent failure:**
If the canister's balance is insufficient after the settings change, `_ignore_error` discards the error and the message executes for free.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L43-45)
```rust
/// Maximum payload size of a management call to update_settings
/// overriding the canister's freezing threshold.
const MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE: usize = 338;
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L559-596)
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
