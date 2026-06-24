### Title
Registry Canister DoS via Oversized `node_signing_pk` Triggering Unconditional Panic in `extract_chip_id_from_payload` — (`rs/registry/canister/src/mutations/node_management/do_add_node.rs`)

---

### Summary

A registered node operator can submit an `AddNodePayload` where `node_signing_pk` is a valid protobuf-encoded `PublicKey` whose **raw byte length exceeds 65535** (the DER `OctetStringRef` limit), combined with any non-`None` `node_registration_attestation`. This causes `extract_chip_id_from_payload` to call `.expect()` on a failing `OctetStringRef::new`, unconditionally panicking and trapping the registry canister.

---

### Finding Description

In `do_add_node_`, after `valid_keys_from_payload` succeeds, the code calls:

```rust
let chip_id = self.extract_chip_id_from_payload(&payload)?;
``` [1](#0-0) 

Inside `extract_chip_id_from_payload`, when `node_registration_attestation` is `Some(...)`:

```rust
let expected_custom_data = NodeRegistrationAttestationCustomData {
    node_signing_pk: OctetStringRef::new(&payload.node_signing_pk)
        .expect("node_signing_pk must be valid"),
};
``` [2](#0-1) 

`OctetStringRef::new` (from the `der` crate) returns `Err` for any slice whose length exceeds `u16::MAX` (65535 bytes). The `.expect()` call converts that `Err` into an unconditional panic, trapping the canister.

**Why `valid_keys_from_payload` can succeed with a >65535-byte `node_signing_pk`:**

`valid_keys_from_payload` decodes the raw bytes as a protobuf `PublicKey` and then validates the *decoded* struct: [3](#0-2) 

`ValidNodeSigningPublicKey::try_from` only checks:
1. Algorithm is Ed25519
2. `key_value` length is exactly 32 bytes
3. `key_value` is a valid Ed25519 point (torsion-free) [4](#0-3) 

It does **not** check the `proof_data` field or the overall size of the protobuf encoding. A `PublicKey` with a valid 32-byte Ed25519 `key_value` and a `proof_data` field padded to ≥65536 bytes will pass all crypto validation, but its raw protobuf encoding will exceed 65535 bytes. The `OctetStringRef::new` call then operates on `payload.node_signing_pk` — the **original raw bytes** as submitted — not the re-encoded decoded struct.

---

### Impact Explanation

- The registry canister traps on every replica processing the block containing the malicious message.
- Because the trap rolls back all state changes (including the rate-limit reservation made at line 61), the attacker can resubmit indefinitely without consuming rate-limit capacity.
- Repeated submissions cause repeated traps, effectively denying service to the registry canister for all legitimate operations (node additions, subnet changes, etc.). [5](#0-4) 

---

### Likelihood Explanation

- The attacker only needs to be a **registered node operator** — a role obtainable through governance without admin privileges.
- The crafted payload is trivial to construct: take any valid node registration payload, append a large `proof_data` blob to the `PublicKey` protobuf for `node_signing_pk`, and set `node_registration_attestation` to any non-`None` value (even a structurally invalid one, since the panic occurs before attestation verification).
- The 65536-byte `node_signing_pk` is well within the IC's 2 MB ingress message size limit.
- The attack is deterministic and locally testable.

---

### Recommendation

Replace the `.expect()` with a proper error return:

```rust
let node_signing_pk_ref = OctetStringRef::new(&payload.node_signing_pk)
    .map_err(|e| format!("{LOG_PREFIX}do_add_node: node_signing_pk too large for DER encoding: {e}"))?;
let expected_custom_data = NodeRegistrationAttestationCustomData {
    node_signing_pk: node_signing_pk_ref,
};
```

Additionally, add an explicit size check in `valid_keys_from_payload` or at the ingress boundary to reject `node_signing_pk` raw bytes exceeding a reasonable bound (e.g., 512 bytes for an Ed25519 key protobuf). [6](#0-5) 

---

### Proof of Concept

```rust
// Craft a PublicKey protobuf with valid Ed25519 key_value but large proof_data
let mut pk = valid_node_signing_public_key(); // 32-byte key_value, passes ValidNodeSigningPublicKey
pk.proof_data = Some(vec![0u8; 65536]); // makes raw encoding > 65535 bytes
let node_signing_pk = pk.encode_to_vec(); // len > 65535
assert!(node_signing_pk.len() > 65535);

// valid_keys_from_payload succeeds because it only validates the decoded key_value
// extract_chip_id_from_payload panics because OctetStringRef::new(&node_signing_pk) returns Err

let payload = AddNodePayload {
    node_signing_pk,
    node_registration_attestation: Some(SevAttestationPackage { /* any value */ }),
    // ... other valid fields
};
// registry.do_add_node_(payload, node_operator_id, now) → PANIC (canister trap)
```

### Citations

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L60-65)
```rust
        let reservation =
            self.try_reserve_capacity_for_node_operator_operation(now, caller_id, 1)?;

        // Validate keys and get the node id
        let (node_id, valid_pks) = valid_keys_from_payload(&payload)
            .map_err(|err| format!("{LOG_PREFIX}do_add_node: {err}"))?;
```

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L195-195)
```rust
        let chip_id = self.extract_chip_id_from_payload(&payload)?;
```

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L251-258)
```rust
        let Some(attestation_package) = &payload.node_registration_attestation else {
            return Ok(None);
        };

        let expected_custom_data = NodeRegistrationAttestationCustomData {
            node_signing_pk: OctetStringRef::new(&payload.node_signing_pk)
                .expect("node_signing_pk must be valid"),
        };
```

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L359-360)
```rust
    let node_signing_pk = PublicKey::decode(&payload.node_signing_pk[..])
        .map_err(|e| format!("node_signing_pk is not in the expected format: {e:?}"))?;
```

**File:** rs/crypto/node_key_validation/src/lib.rs (L219-246)
```rust
        if AlgorithmIdProto::try_from(pk_proto.algorithm).ok() != Some(AlgorithmIdProto::Ed25519) {
            return Err(invalid_node_signing_key_error(format!(
                "Unexpected algorithm id {}",
                pk_proto.algorithm
            )));
        }

        if pk_proto.key_value.len() != ic_ed25519::PublicKey::BYTES {
            return Err(invalid_node_signing_key_error(format!(
                "Unexpected length {}",
                pk_proto.key_value.len()
            )));
        }

        let pk = ic_ed25519::PublicKey::deserialize_raw(&pk_proto.key_value)
            .map_err(|e| invalid_node_signing_key_error(format!("{:?}", e)))?;

        if !pk.is_torsion_free() {
            return Err(invalid_node_signing_key_error(
                "has torsion component".to_string(),
            ));
        }

        let derived_node_id = derive_node_id(&pk);
        Ok(Self {
            public_key: pk_proto,
            derived_node_id,
        })
```
