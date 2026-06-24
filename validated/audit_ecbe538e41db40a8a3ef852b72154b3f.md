Audit Report

## Title
Unchecked Index Panic on Empty `EnvelopePair` Vec Crashes Rosetta Process - (File: `rs/rosetta-api/icp/src/request_handler/construction_parse.rs`)

## Summary
In `construction_parse`, the signed-transaction branch unconditionally indexes `updates[0]` on a `Vec<EnvelopePair>` that the type system permits to be empty. Because `ParsedTransaction::try_from` performs no structural validation beyond CBOR deserialization, an unauthenticated attacker can POST a single crafted request to `/construction/parse` with `signed=true` and an empty envelope list, triggering a Rust index-out-of-bounds panic that terminates the Rosetta process.

## Finding Description
The unchecked access is at `construction_parse.rs` line 42:

```rust
|(request_type, updates)| match updates[0].update.content.clone() {
``` [1](#0-0) 

`Request` is a type alias with no minimum-length constraint on the inner `Vec<EnvelopePair>`:

```rust
pub type Request = (RequestType, Vec<EnvelopePair>);
``` [2](#0-1) 

The deserialization path in `ParsedTransaction::try_from` calls only `serde_cbor::from_slice` — it applies no structural validation and will successfully decode a `SignedTransaction` whose `requests` field contains a tuple with an empty `Vec<EnvelopePair>`: [3](#0-2) 

Exploit flow:
1. Attacker constructs a CBOR-encoded `SignedTransaction { requests: vec![(RequestType::Send, vec![])] }`.
2. Attacker POSTs `{ signed: true, transaction: <hex> }` to `/construction/parse` — no authentication required.
3. `ParsedTransaction::try_from` succeeds (CBOR is structurally valid).
4. The `.map(|(request_type, updates)| ... updates[0] ...)` closure executes with `updates.len() == 0`.
5. Rust panics: `index out of bounds: the len is 0 but the index is 0`, terminating the process.

No existing guard checks for an empty `Vec<EnvelopePair>` before the index access.

## Impact Explanation
Rosetta runs as a single OS process. A Rust panic from an unchecked slice index is unrecoverable in the default configuration and terminates the process immediately. Any client that can reach the public Rosetta HTTP endpoint can crash it with a single malformed request, causing complete availability loss until the process is manually restarted. This matches the allowed impact: **High — Application/platform-level DoS crash of the Rosetta API with concrete user and protocol harm** (operators and integrators relying on Rosetta for ICP transaction construction lose service entirely).

## Likelihood Explanation
The Rosetta HTTP API is intentionally public-facing; no authentication or special privilege is required to POST to `/construction/parse`. The crafted payload is trivial to construct: a CBOR-encoded struct with one request entry containing an empty envelope list. The panic is deterministic and reproducible on every invocation, making this a reliable, repeatable denial-of-service.

## Recommendation
Add an explicit guard before the `updates[0]` access. Return `ApiError::invalid_request` if the inner vec is empty:

```rust
.map(|(request_type, updates)| {
    let first = updates.first().ok_or_else(|| {
        ApiError::invalid_request("SignedTransaction request has no envelope pairs")
    })?;
    match first.update.content.clone() {
        HttpCallContent::Call { update } => Ok((request_type.clone(), update)),
    }
})
.collect::<Result<Vec<_>, _>>()?
```

This converts the panic into a graceful `Err(ApiError)` that is returned to the caller as an HTTP 400 response without crashing the process.

## Proof of Concept
```rust
// Unit test — add to construction_parse.rs #[cfg(test)] mod tests
#[test]
fn test_empty_envelope_pair_vec_does_not_panic() {
    use crate::models::{SignedTransaction, Request};
    use crate::request_types::RequestType;

    let signed_tx = SignedTransaction {
        requests: vec![(RequestType::Send, vec![])], // empty Vec<EnvelopePair>
    };
    let cbor_bytes = serde_cbor::to_vec(&signed_tx).unwrap();
    let hex_tx = hex::encode(&cbor_bytes);

    let req = ConstructionParseRequest {
        network_identifier: handler.network_id(),
        signed: true,
        transaction: hex_tx,
    };

    // Before fix: panics with "index out of bounds: the len is 0 but the index is 0"
    // After fix: returns Err(ApiError::invalid_request(...))
    let result = handler.construction_parse(req);
    assert!(result.is_err());
}
```

### Citations

**File:** rs/rosetta-api/icp/src/request_handler/construction_parse.rs (L41-45)
```rust
                .map(
                    |(request_type, updates)| match updates[0].update.content.clone() {
                        HttpCallContent::Call { update } => (request_type.clone(), update),
                    },
                )
```

**File:** rs/rosetta-api/icp/src/models.rs (L56-58)
```rust
/// A vector of update/read-state calls for different ingress windows
/// of the same call.
pub type Request = (RequestType, Vec<EnvelopePair>);
```

**File:** rs/rosetta-api/icp/src/models.rs (L183-187)
```rust
        if value.signed {
            Ok(ParsedTransaction::Signed(
                serde_cbor::from_slice(&from_hex(&value.transaction)?).map_err(|e| {
                    ApiError::invalid_request(format!("Could not decode signed transaction: {e}"))
                })?,
```
