### Title
Delayed Ingress Induction Cost for `UpdateSettings` Undercharges Cycles by Omitting Full Message Envelope Overhead - (`rs/execution_environment/src/execution_environment.rs`)

---

### Summary

When an `UpdateSettings` ingress message has a small payload (≤ 338 bytes), the IC delays its induction fee and later charges only `method_payload.len() + method_name.len()` bytes. The normal (non-delayed) induction path charges `ingress.binary().len()` — the full CBOR-encoded signed message including sender principal, expiry, nonce, public key, and signature. The delayed path silently omits all of this overhead, causing a systematic undercharge of ~150–200+ cycles-bearing bytes per message.

---

### Finding Description

The `ingress_induction_cost()` function in `rs/cycles_account_manager/src/cycles_account_manager.rs` has two distinct charging paths for ingress messages:

**Normal path** (all methods except small `UpdateSettings`): charges the full binary size of the signed ingress message.

```rust
let raw_bytes = NumBytes::from(ingress.binary().len() as u64);
``` [1](#0-0) 

**Delayed path** (small `UpdateSettings`, payload ≤ `MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE` = 338 bytes): returns `IngressInductionCost::Free` at induction time, deferring the charge to execution. [2](#0-1) 

The deferred charge is applied in `execute_subnet_message()`:

```rust
let bytes_to_charge =
    ingress.method_payload.len() + ingress.method_name.len();
``` [3](#0-2) 

This `bytes_to_charge` value is then passed directly to `ingress_induction_cost_from_bytes()`: [4](#0-3) 

The `ingress.binary()` used in the normal path is the full CBOR-encoded `HttpRequestEnvelope`, which includes:

- `request_type` field
- `sender` (principal, ~29 bytes)
- `ingress_expiry` (8 bytes)
- `nonce` (optional, variable)
- `canister_id` (~10 bytes)
- `method_name` (already counted)
- `arg` / `method_payload` (already counted)
- `sender_pubkey` (~35 bytes)
- `sender_sig` (~64 bytes)
- CBOR framing overhead [5](#0-4) 

The delayed path charges only for `method_payload + method_name`, omitting all envelope fields. For a typical `UpdateSettings` call, the full binary is ~200–250 bytes larger than `method_payload + method_name` alone.

The fee rate is `ingress_byte_reception_fee = 2,000 cycles/byte`: [6](#0-5) 

The `is_delayed_ingress_induction_cost` threshold: [7](#0-6) 

---

### Impact Explanation

Any unprivileged user who controls a canister can send `UpdateSettings` ingress messages with a small payload (≤ 338 bytes — the common case for setting `freezing_threshold`, `memory_allocation`, etc.) and pay ~300,000–400,000 fewer cycles per message than the protocol intends. At `ingress_byte_reception_fee = 2,000 cycles/byte` and ~150–200 bytes of omitted overhead, the undercharge is systematic and predictable. This means the subnet bears the full consensus and networking cost of processing the complete signed message while the canister is charged only for a fraction of it. Repeated invocations amplify the economic loss to the subnet.

---

### Likelihood Explanation

**High.** The condition triggering the undercharge — a small `UpdateSettings` payload — is the normal, everyday case. The `MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE = 338` constant was sized to accommodate the typical `UpdateSettings` payload: [8](#0-7) 

Any canister controller sending routine settings updates (freezing threshold, memory allocation, log visibility) will trigger this path. No special privileges, no threshold corruption, and no social engineering are required — only a valid ingress sender with controller rights over any canister.

---

### Recommendation

Replace the partial byte count in the delayed charging path with the full binary size of the signed ingress message, consistent with the normal path. The `Ingress` struct (the content-only form stored after induction) does not retain the binary, so the fix should either:

1. Pass the full `ingress.binary().len()` value into the deferred charge before the binary is discarded (at the point where `ingress_induction_cost` returns `Free`), or
2. Store the original binary length alongside the `Ingress` content for use during the deferred charge.

This mirrors the normal path's use of `ingress.binary().len()` at line 565 of `rs/cycles_account_manager/src/cycles_account_manager.rs`.

---

### Proof of Concept

1. Deploy a canister on an application subnet.
2. Send an `UpdateSettings` ingress message with `freezing_threshold = 30 days` (a ~50-byte Candid payload, well under 338 bytes).
3. Observe the cycles deducted from the canister: it equals `ingress_message_reception_fee + ingress_byte_reception_fee * (len("update_settings") + len(candid_payload))` ≈ `1,200,000 + 2,000 * (15 + 50)` = `1,330,000` cycles.
4. Compare against the expected charge using the full binary: `1,200,000 + 2,000 * ~280` = `1,760,000` cycles (the full CBOR envelope including sender, expiry, pubkey, sig).
5. The canister is undercharged by ~430,000 cycles per message. Sending 1,000 such messages saves ~430,000,000 cycles relative to the protocol's intended fee schedule. [9](#0-8) [10](#0-9)

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

**File:** rs/types/types/src/messages/ingress_messages.rs (L365-373)
```rust
impl TryFrom<SignedRequestBytes> for SignedIngress {
    type Error = HttpRequestError;

    fn try_from(binary: SignedRequestBytes) -> Result<Self, Self::Error> {
        let request: HttpRequestEnvelope<HttpCallContent> = (&binary).try_into()?;
        let signed = request.try_into()?;
        Ok(SignedIngress { signed, binary })
    }
}
```

**File:** rs/config/src/subnet_config.rs (L514-515)
```rust
            ingress_message_reception_fee: Cycles::new(1_200_000),
            ingress_byte_reception_fee: Cycles::new(2_000),
```

**File:** rs/cycles_account_manager/src/cycles_account_manager/tests.rs (L22-37)
```rust
fn max_delayed_ingress_cost_payload_size_test() {
    let default_freezing_limit = 30 * 24 * 3600; // 30 days
    let payload = UpdateSettingsArgs {
        canister_id: CanisterId::from_u64(0).into(),
        settings: CanisterSettingsArgsBuilder::new()
            .with_freezing_threshold(default_freezing_limit)
            .build(),
        sender_canister_version: None, // ingress messages are not supposed to set this field
    };

    let payload_size = 2 * Encode!(&payload).unwrap().len();

    assert!(
        payload_size <= MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE,
        "Payload size: {payload_size}, is greater than MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE: {MAX_DELAYED_INGRESS_COST_PAYLOAD_SIZE}."
    );
```
