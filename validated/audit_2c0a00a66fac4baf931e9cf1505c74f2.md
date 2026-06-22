### Title
Unbounded `ingress_expiries` Vec Allocation via Attacker-Controlled `ingress_end` Crashes Rosetta Node — (`rs/rosetta-api/icrc1/src/construction_api/services.rs` and `rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

An unprivileged HTTP client can POST a `ConstructionPayloadsRequest` with `ingress_end = u64::MAX` (or any astronomically large value). Both the ICRC1 and ICP Rosetta implementations contain a `while ingress_start < ingress_end` loop that generates one `ingress_expiry` entry per `~240s` interval. With `ingress_end = u64::MAX`, the loop executes ~70 million iterations, each cloning `canister_method_args` and allocating `EnvelopeContent::Call` structs, exhausting the Rosetta process's heap and crashing it.

---

### Finding Description

**ICRC1 path** — `rs/rosetta-api/icrc1/src/construction_api/services.rs`, `construction_payloads()`:

The function reads attacker-controlled `ingress_start` and `ingress_end` from request metadata and applies two guards before the loop: [1](#0-0) 

Guard 1 (`ingress_start >= ingress_end`) is bypassed trivially (e.g., `ingress_start = now`, `ingress_end = u64::MAX`).

Guard 2 (`ingress_end < now + ingress_interval`) is **permanently bypassable**: `u64::MAX` is never less than any `u64` value, so this condition is always `false` when `ingress_end = u64::MAX`. Even if `now + ingress_interval` wraps (overflow in release mode), the comparison still evaluates to `false`.

The unguarded loop then runs: [2](#0-1) 

With `ingress_interval ≈ 240,000,000,000 ns` and `ingress_start ≈ now ≈ 1.7×10¹⁸ ns`, the iteration count is approximately `(u64::MAX − now) / ingress_interval ≈ 70 million`. Each iteration clones `canister_method_args` and pushes an `EnvelopeContent::Call` to two `Vec`s inside `handle_construction_payloads`: [3](#0-2) 

In Rust release mode, if `ingress_start` eventually wraps past `u64::MAX`, the loop becomes **infinite**.

**ICP path** — `rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`:

The ICP handler has **no guards whatsoever** on the window size: [4](#0-3) 

With `ingress_start = 0` and `ingress_end = u64::MAX`, the loop runs for `u64::MAX / interval ≈ 153 billion` iterations before any overflow, making the ICP path even more severe.

---

### Impact Explanation

The Rosetta node is a single OS process. Allocating tens of millions of heap objects (each containing a cloned `Vec<u8>` for `canister_method_args`) exhausts available RAM, triggering an OOM kill or process crash. A single malformed HTTP POST request is sufficient. No authentication is required to call `/construction/payloads`. The IC protocol itself is unaffected; only the Rosetta replica crashes.

---

### Likelihood Explanation

The endpoint is publicly reachable on any deployed Rosetta node. The exploit requires no credentials, no prior state, and no volumetric traffic — a single small HTTP request suffices. The `ingress_end` field is a plain `u64` in the JSON metadata with no server-side cap. The faulty guard (`ingress_end < now + ingress_interval`) looks correct at a glance but is logically ineffective for the maximum-value case.

---

### Recommendation

1. **Cap the window size before the loop.** After computing `ingress_start` and `ingress_end`, add:
   ```rust
   const MAX_INGRESS_WINDOW: u64 = 24 * 3600 * 1_000_000_000; // e.g. 24 hours in ns
   if ingress_end.saturating_sub(ingress_start) > MAX_INGRESS_WINDOW {
       return Err(...);
   }
   ```
2. **Cap the Vec length.** After the loop, assert `ingress_expiries.len() <= SOME_REASONABLE_MAX` (e.g., 100) and return an error if exceeded.
3. Apply the same fix to both the ICRC1 (`services.rs`) and ICP (`construction_payloads.rs`) handlers.
4. Use **saturating arithmetic** on `ingress_start` inside the loop to prevent wrap-around infinite loops in release builds.

---

### Proof of Concept

```rust
// Unit test demonstrating OOM / infinite loop
#[test]
fn test_construction_payloads_unbounded_ingress_window() {
    use std::time::SystemTime;
    let now = SystemTime::now();
    let result = construction_payloads(
        vec![/* valid transfer operation */],
        Some(ConstructionPayloadsRequestMetadata {
            ingress_start: None,          // defaults to now
            ingress_end: Some(u64::MAX),  // attacker-controlled
            created_at_time: None,
            memo: None,
        }),
        &some_ledger_principal,
        vec![valid_public_key()],
        now,
    );
    // Without the fix this either OOMs or loops forever.
    // With the fix it must return Err.
    assert!(result.is_err());
    // And the payload count must be bounded:
    if let Ok(resp) = result {
        assert!(resp.payloads.len() <= 100);
    }
}
```

Sending the equivalent HTTP request:
```json
POST /construction/payloads
{
  "network_identifier": { ... },
  "operations": [ /* valid transfer */ ],
  "public_keys": [ { ... } ],
  "metadata": {
    "ingress_end": 18446744073709551615
  }
}
```
causes the Rosetta process to allocate ~70 million `EnvelopeContent::Call` structs and crash.

### Citations

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L148-158)
```rust
    if ingress_start >= ingress_end {
        return Err(Error::processing_construction_failed(&format!(
            "Ingress start should start before ingress end: Start: {ingress_start}, End: {ingress_end}"
        )));
    }

    if ingress_end < now + ingress_interval {
        return Err(Error::processing_construction_failed(&format!(
            "Ingress end should be at least one interval from the current time: Current time: {now}, End: {ingress_end}"
        )));
    }
```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L162-167)
```rust
    let mut ingress_expiries = vec![];
    while ingress_start < ingress_end {
        ingress_expiries.push(ingress_start + ingress_interval);
        ingress_start +=
            ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
    }
```

**File:** rs/rosetta-api/icrc1/src/construction_api/utils.rs (L441-461)
```rust
    let mut signing_payloads = Vec::new();
    let mut envelope_contents = Vec::new();
    for (nonce, ingress_expiry) in ingress_expiries.iter().enumerate() {
        // Rosetta will send an envelope containing the update information to a replica
        let envelope_content = EnvelopeContent::Call {
            canister_id,
            method_name: canister_method_name.to_string(),
            arg: canister_method_args.clone(),
            nonce: Some(nonce.to_ne_bytes().to_vec()),
            sender: caller,
            ingress_expiry: *ingress_expiry,
        };

        // For every operation we create a call envelope
        // For every envelope we create a signing payload
        let payload =
            build_payloads_from_call_envelope_content(&envelope_content, &sender_public_key)?;

        signing_payloads.push(payload);
        envelope_contents.push(envelope_content);
    }
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L99-107)
```rust
        let mut ingress_expiries = vec![];
        let mut now = ingress_start;
        while now < ingress_end {
            let ingress_expiry = (now
                + ic_limits::MAX_INGRESS_TTL.saturating_sub(ic_limits::PERMITTED_DRIFT))
            .as_nanos_since_unix_epoch();
            ingress_expiries.push(ingress_expiry);
            now += interval;
        }
```
