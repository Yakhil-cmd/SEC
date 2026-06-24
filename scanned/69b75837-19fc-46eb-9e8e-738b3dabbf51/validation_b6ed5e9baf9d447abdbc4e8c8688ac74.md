### Title
Unbounded `Nat` Input Causes Panic/Trap in `get_account_transactions` Query — (`File: rs/ledger_suite/icrc1/index-ng/src/main.rs`)

---

### Summary

The ICRC-1 index-ng canister's `get_account_transactions` query endpoint accepts `max_results: Nat` and `start: opt BlockIndex` (also a `Nat`) via the Candid interface. The implementation unconditionally calls `.expect()` on the `to_u64()` conversion of these values. Because Candid `Nat` is an arbitrary-precision unsigned integer, any caller supplying a value larger than `u64::MAX` causes a Rust panic inside the canister, which manifests as a canister trap. No validation or graceful error path exists before the panic site.

---

### Finding Description

The public Candid interface for the ICRC-1 index-ng canister declares:

```
type GetAccountTransactionsArgs = record {
  account : Account;
  start : opt BlockIndex;   // BlockIndex = Nat
  max_results : nat
};
```

Both `max_results` and `start` are `Nat` — an unbounded type. Inside `get_account_transactions`, the implementation narrows these to `u64` using `.expect()`:

```rust
let length = arg
    .max_results
    .0
    .to_u64()
    .expect("The length must be a u64!")   // panics if Nat > u64::MAX
    ...

let start = arg
    .start
    .map_or(u64::MAX, |n| n.0.to_u64().expect("start must be a u64!"));
```

`BigUint::to_u64()` returns `None` for any value exceeding `u64::MAX`. Calling `.expect()` on `None` in a Rust canister causes an unhandled panic, which the IC runtime converts to a canister trap. The caller receives a trap error instead of a structured `Err` variant.

The ICP index canister (`rs/ledger_suite/icp/index/src/main.rs`) handles the same situation more deliberately with `ic_cdk::trap(...)`, but the index-ng canister uses `.expect()`, which is an unintentional panic path rather than an explicit error response.

---

### Impact Explanation

Any unprivileged ingress sender or query caller can submit a `get_account_transactions` request with `max_results` or `start` set to a value exceeding `u64::MAX` (e.g., `2^64` or larger, which is a valid Candid `Nat`). The canister will trap on every such call. This:

- Causes all such queries to fail with a trap error rather than a structured API error.
- Constitutes a denial-of-service on the `get_account_transactions` query endpoint for any caller supplying oversized values.
- Violates the implicit contract that a `Nat`-typed field in the public interface is handled for all valid `Nat` values.

The impact is bounded to individual query failures (queries do not modify replicated state), so there is no ledger conservation or consensus safety impact. However, the canister's public API silently accepts values it cannot process, which is the direct analog of the `LeqGadget` accepting numeric inputs it cannot handle.

---

### Likelihood Explanation

The attack requires only a crafted Candid message with a `Nat` value exceeding `u64::MAX`. This is trivially constructable by any caller using the standard Candid encoding. No privileged access, key material, or social engineering is required. The endpoint is publicly reachable as a query call.

---

### Recommendation

Before calling `.expect()` or `.unwrap()` on `to_u64()`, validate the `Nat` value and return a structured `Err(GetTransactionsErr { message: "..." })` instead of panicking:

```rust
let length = match arg.max_results.0.to_u64() {
    Some(v) => v,
    None => return Err(GetTransactionsErr { message: "max_results exceeds u64::MAX".to_string() }),
};
```

Apply the same pattern to the `start` field. This mirrors the correct handling already present in the ICP index canister (`ic_cdk::trap` is at least explicit, but a structured error is preferable).

---

### Proof of Concept

Encode a `GetAccountTransactionsArgs` Candid message with `max_results` set to `2^64` (one more than `u64::MAX`):

```python
# Using the candid Python library or didc:
# max_results = 18446744073709551616  (= 2^64)
# Candid encoding: (record { account = ...; max_results = 18446744073709551616 : nat })
```

Send this as a query call to the `get_account_transactions` method of any deployed ICRC-1 index-ng canister. The response will be a canister trap with the message `"The length must be a u64!"` instead of a valid `GetTransactionsResult`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L1262-1273)
```rust
fn get_account_transactions(arg: GetAccountTransactionsArgs) -> GetAccountTransactionsResult {
    let length = arg
        .max_results
        .0
        .to_u64()
        .expect("The length must be a u64!")
        .min(with_state(|opts| opts.max_blocks_per_response))
        .min(usize::MAX as u64) as usize;
    // TODO: deal with the user setting start to u64::MAX
    let start = arg
        .start
        .map_or(u64::MAX, |n| n.0.to_u64().expect("start must be a u64!"));
```

**File:** rs/ledger_suite/icp/index/src/main.rs (L703-716)
```rust
#[query]
fn get_account_transactions(arg: GetAccountTransactionsArgs) -> GetAccountTransactionsResult {
    get_account_identifier_transactions(GetAccountIdentifierTransactionsArgs {
        account_identifier: AccountIdentifier::from(arg.account),
        max_results: arg
            .max_results
            .0
            .to_u64()
            .unwrap_or_else(|| ic_cdk::trap("Conversion from candid Nat to u64 failed")),
        start: arg.start.map(|s| {
            s.0.to_u64()
                .unwrap_or_else(|| ic_cdk::trap("Conversion from candid Nat to u64 failed"))
        }),
    })
```

**File:** rs/ledger_suite/icrc1/index-ng/index-ng.did (L145-154)
```text
type GetAccountTransactionsArgs = record {
  account : Account;
  // The txid of the last transaction seen by the client.
  // If None then the results will start from the most recent
  // txid. If set then the results will start from the next
  // most recent txid after start (start won't be included).
  start : opt BlockIndex;
  // Maximum number of transactions to fetch.
  max_results : nat
};
```

**File:** rs/ledger_suite/icp/index/index.did (L7-16)
```text
type GetAccountTransactionsArgs = record {
  account : Account;
  // The txid of the last transaction seen by the client.
  // If None then the results will start from the most recent
  // txid. If set then the results will start from the next
  // most recent txid after start (start won't be included).
  start : opt nat;
  // Maximum number of transactions to fetch.
  max_results : nat
};
```
