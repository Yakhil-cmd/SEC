Now I have all the information needed. Let me verify the exact subtraction and confirm there is no guard between the input and the arithmetic.

### Title
u64 Integer Underflow in `handle_construction_parse` via Attacker-Controlled `ingress_expiry` — (`rs/rosetta-api/icrc1/src/construction_api/utils.rs`)

---

### Summary

`handle_construction_parse` performs a bare u64 subtraction using an `ingress_expiry` value extracted directly from attacker-supplied transaction bytes, with no lower-bound guard. Any client that submits a crafted transaction whose lowest `ingress_expiry` is less than `(MAX_INGRESS_TTL − PERMITTED_DRIFT).as_nanos() as u64` (= 240,000,000,000 ns) triggers the underflow.

---

### Finding Description

The subtraction is at: [1](#0-0) 

```rust
ingress_start: ingress_expiry_start.map(|start| {
    start
        - (ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT).as_nanos()
            as u64
}),
```

The subtrahend is fixed: [2](#0-1) 

`MAX_INGRESS_TTL = 300 s`, `PERMITTED_DRIFT = 60 s` → subtrahend = **240,000,000,000 ns**.

`ingress_expiry_start` is the raw minimum value returned by: [3](#0-2) 

```rust
pub fn get_lowest_ingress_expiry(&self) -> Option<u64> {
    self.envelopes.iter().map(|e| e.content.ingress_expiry()).min()
}
```

This is a direct read of the `ingress_expiry` field from the CBOR-encoded transaction bytes supplied by the caller. No validation of this value occurs anywhere between deserialization and the subtraction: [4](#0-3) 

`construction_parse` extracts `ingress_expiry_start` and passes it straight to `handle_construction_parse` without any range check.

---

### Impact Explanation

| Build mode | Behavior | Effect |
|---|---|---|
| **Debug** | Rust panics on u64 overflow | Rosetta process crashes → DoS |
| **Release** | Wraps to `u64::MAX − 240_000_000_000 + 1 + start` | `ingress_start` is set ~584 years in the future; downstream callers silently discard the transaction |

The release-mode wrap is the more dangerous outcome: the API returns HTTP 200 with structurally valid JSON, but the embedded `ingress_start` metadata is nonsensical, causing any conforming Rosetta client to conclude the transaction window has not yet opened and never submit it. This silently drops a user's signed transaction with no error signal.

---

### Likelihood Explanation

The `/construction/parse` endpoint is a public, unauthenticated Rosetta Construction API endpoint. Crafting a CBOR-encoded `UnsignedTransaction` or `SignedTransaction` with `ingress_expiry = 0` requires only knowledge of the CBOR schema (which is open-source). No key material, privileged role, or network-level attack is needed.

---

### Recommendation

Replace the bare subtraction with a checked or saturating variant and return an error on underflow:

```rust
ingress_start: ingress_expiry_start.map(|start| {
    let interval = (ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT)
        .as_nanos() as u64;
    start.checked_sub(interval)
        .ok_or_else(|| anyhow::anyhow!(
            "ingress_expiry {} is too small to compute ingress_start \
             (must be >= {} ns)", start, interval
        ))
}).transpose()?
```

This converts the arithmetic error into a proper `anyhow::Result` propagation, consistent with the rest of the function's error-handling style.

---

### Proof of Concept

```rust
// Craft a minimal UnsignedTransaction with ingress_expiry = 0
let envelope_content = EnvelopeContent::Call {
    canister_id: Principal::anonymous(),
    method_name: "icrc1_transfer".to_string(),
    arg: /* valid Candid-encoded TransferArg */ ...,
    nonce: Some(vec![0u8; 8]),
    sender: Principal::anonymous(),
    ingress_expiry: 0,   // <-- triggers underflow
};
let unsigned_tx = UnsignedTransaction { envelope_contents: vec![envelope_content] };
let tx_string = unsigned_tx.to_string();

// POST to /construction/parse with transaction_is_signed = false
// Debug build  → panic (process crash)
// Release build → ingress_start = u64::MAX - 240_000_000_000 + 1
//                 returned in HTTP 200 response body
let result = construction_parse(tx_string, false, currency);
// In release: result is Ok(...) with a wrapped ingress_start value
```

### Citations

**File:** rs/rosetta-api/icrc1/src/construction_api/utils.rs (L520-524)
```rust
                    ingress_start: ingress_expiry_start.map(|start| {
                        start
                            - (ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT).as_nanos()
                                as u64
                    }),
```

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```

**File:** rs/rosetta-api/icrc1/src/construction_api/types.rs (L63-68)
```rust
    pub fn get_lowest_ingress_expiry(&self) -> Option<u64> {
        self.envelopes
            .iter()
            .map(|envelope| envelope.content.ingress_expiry())
            .min()
    }
```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L201-230)
```rust
    let (ingress_expiry_start, ingress_expiry_end, envelope_contents) = if transaction_is_signed {
        let signed_transaction = SignedTransaction::from_str(&transaction_string)
            .map_err(|err| Error::parsing_unsuccessful(&err))?;
        (
            signed_transaction.get_lowest_ingress_expiry(),
            signed_transaction.get_highest_ingress_expiry(),
            signed_transaction
                .envelopes
                .into_iter()
                .map(|envelope| envelope.content.into_owned())
                .collect(),
        )
    } else {
        let unsigned_transaction = UnsignedTransaction::from_str(&transaction_string)
            .map_err(|err| Error::parsing_unsuccessful(&err))?;
        (
            unsigned_transaction.get_lowest_ingress_expiry(),
            unsigned_transaction.get_highest_ingress_expiry(),
            unsigned_transaction.envelope_contents,
        )
    };

    handle_construction_parse(
        envelope_contents,
        currency,
        ingress_expiry_start,
        ingress_expiry_end,
        transaction_is_signed,
    )
    .map_err(|err| Error::processing_construction_failed(&err))
```
