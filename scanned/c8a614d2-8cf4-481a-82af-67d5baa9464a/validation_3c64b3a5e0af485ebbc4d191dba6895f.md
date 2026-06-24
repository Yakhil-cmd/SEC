### Title
Unsafe `u64 as usize` Truncation in ICRC-1 Ledger Query Endpoints Returns Silently Empty Results on wasm32 - (File: `rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

The ICRC-1 ledger's `get_transactions` and `get_blocks` query endpoints cast the caller-supplied `length` parameter directly from `u64` to `usize` without first clamping it to `usize::MAX`. On the wasm32 target that all IC canisters run on, `usize` is 32 bits. A caller supplying `length = (u32::MAX as u64) + 1 = 4_294_967_296` causes the cast to silently wrap to `0`, and the ledger returns an empty result set even though transactions exist. The ICP ledger's `query_blocks` already guards against this with an explicit `.min(usize::MAX as u64)` clamp; the ICRC-1 ledger does not.

---

### Finding Description

`get_transactions` and `get_blocks` in the ICRC-1 ledger canister call `req.as_start_and_length()`, which returns `(u64, u64)`, and then immediately cast the second element with `length as usize`:

```rust
// rs/ledger_suite/icrc1/ledger/src/main.rs  lines 793-797
fn get_transactions(req: GetTransactionsRequest) -> GetTransactionsResponse {
    let (start, length) = req
        .as_start_and_length()
        .unwrap_or_else(|msg| ic_cdk::api::trap(&msg));
    Access::with_ledger(|ledger| ledger.get_transactions(start, length as usize))
}
``` [1](#0-0) 

```rust
// rs/ledger_suite/icrc1/ledger/src/main.rs  lines 802-806
fn get_blocks(req: GetBlocksRequest) -> GetBlocksResponse {
    let (start, length) = req
        .as_start_and_length()
        .unwrap_or_else(|msg| ic_cdk::api::trap(&msg));
    Access::with_ledger(|ledger| ledger.get_blocks(start, length as usize))
}
``` [2](#0-1) 

`as_start_and_length()` caps the Candid `nat` at `u64::MAX` but performs no further reduction:

```rust
// packages/icrc-ledger-types/src/icrc3/blocks.rs  lines 64-83
pub fn as_start_and_length(&self) -> Result<(u64, u64), String> {
    let length = self.length.0.to_u64().ok_or_else(|| { ... })?;
    Ok((start, length))
}
``` [3](#0-2) 

On wasm32, `usize` is 32 bits. The Rust `as` cast from `u64` to `usize` silently truncates the upper 32 bits. For `length = 4_294_967_296` (`u32::MAX + 1`), the result is `0usize`. The downstream `block_locations` call then computes an empty range and returns zero local blocks and zero archived blocks.

The ICP ledger's `query_blocks` already applies the correct guard:

```rust
// rs/ledger_suite/icp/ledger/src/main.rs  line 1065
let locations = block_locations(&*ledger, start, length.min(usize::MAX as u64) as usize);
``` [4](#0-3) 

The ICP ledger test suite even documents the exact truncation scenario:

```rust
// rs/ledger_suite/icp/ledger/tests/tests.rs  lines 1885-1886
// If this is cast (in a wasm32 ledger) using `as usize`, it will overflow to 0u32.
length: (u32::MAX as u64) + 1
``` [5](#0-4) 

The ICRC-1 ledger has no equivalent test or guard.

---

### Impact Explanation

Any unprivileged caller (query or ingress) can send `GetTransactionsRequest { start: 0, length: 4_294_967_296 }` to the ICRC-1 ledger's `get_transactions` or `get_blocks` endpoints. The ledger silently returns an empty `transactions` / `blocks` vector and an empty `archived_transactions` / `archived_blocks` list, even when thousands of blocks exist. Applications that use these endpoints to verify that a transaction was included (e.g., a bridge, a DEX, or a wallet) may incorrectly conclude that no transactions exist, leading to incorrect application-level decisions. Ledger state and balances are not modified; the impact is confined to incorrect query responses.

**Vulnerability class:** boundary/API validation bypass (incorrect data returned to an unprivileged query caller).

---

### Likelihood Explanation

The `length` field in `GetTransactionsRequest` is typed as Candid `nat` (arbitrary precision), so any caller can supply a value exceeding `u32::MAX` without any client-side restriction. The truncation is silent — no error, no trap, just an empty response — making it easy to trigger accidentally or deliberately. Likelihood is **Medium**: the value must be crafted specifically above `u32::MAX`, but the interface is fully open to any caller.

---

### Recommendation

Apply the same guard already used in the ICP ledger before the `as usize` cast:

```rust
// get_transactions
Access::with_ledger(|ledger| ledger.get_transactions(start, length.min(usize::MAX as u64) as usize))

// get_blocks
Access::with_ledger(|ledger| ledger.get_blocks(start, length.min(usize::MAX as u64) as usize))
```

Alternatively, centralise the guard inside `as_start_and_length()` so all callers benefit automatically. Add a regression test analogous to `test_query_blocks_large_length` for the ICRC-1 ledger.

---

### Proof of Concept

1. Deploy the ICRC-1 ledger canister with any initial balance so at least one block exists.
2. Call `get_transactions` with `{ start = 0; length = 4_294_967_296 }` (i.e., `u32::MAX + 1`).
3. Observe that the response contains `transactions = []` and `archived_transactions = []`, despite blocks being present.
4. Call again with `{ start = 0; length = 10 }` and observe that transactions are returned correctly.

The discrepancy is caused solely by the `u64 as usize` truncation: `4_294_967_296u64 as usize` on wasm32 equals `0usize`, so `block_locations` is called with `length = 0` and returns an empty range.

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L792-798)
```rust
#[query]
fn get_transactions(req: GetTransactionsRequest) -> GetTransactionsResponse {
    let (start, length) = req
        .as_start_and_length()
        .unwrap_or_else(|msg| ic_cdk::api::trap(&msg));
    Access::with_ledger(|ledger| ledger.get_transactions(start, length as usize))
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L800-807)
```rust
#[cfg(not(feature = "get-blocks-disabled"))]
#[query]
fn get_blocks(req: GetBlocksRequest) -> GetBlocksResponse {
    let (start, length) = req
        .as_start_and_length()
        .unwrap_or_else(|msg| ic_cdk::api::trap(&msg));
    Access::with_ledger(|ledger| ledger.get_blocks(start, length as usize))
}
```

**File:** packages/icrc-ledger-types/src/icrc3/blocks.rs (L64-83)
```rust
impl GetBlocksRequest {
    pub fn as_start_and_length(&self) -> Result<(u64, u64), String> {
        use num_traits::cast::ToPrimitive;

        let start = self.start.0.to_u64().ok_or_else(|| {
            format!(
                "transaction index {} is too large, max allowed: {}",
                self.start,
                u64::MAX
            )
        })?;
        let length = self.length.0.to_u64().ok_or_else(|| {
            format!(
                "requested length {} is too large, max allowed: {}",
                self.length,
                u64::MAX
            )
        })?;
        Ok((start, length))
    }
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1063-1065)
```rust
fn query_blocks(GetBlocksArgs { start, length }: GetBlocksArgs) -> QueryBlocksResponse {
    let ledger = LEDGER.read().unwrap();
    let locations = block_locations(&*ledger, start, length.min(usize::MAX as u64) as usize);
```

**File:** rs/ledger_suite/icp/ledger/tests/tests.rs (L1884-1904)
```rust
                start: 0,
                // If this is cast (in a wasm32 ledger) using `as usize`, it will overflow to 0u32.
                length: (u32::MAX as u64) + 1
            })
            .unwrap()
        )
        .expect("failed to query blocks")
        .bytes(),
        QueryBlocksResponse
    )
    .expect("should successfully decode QueryBlocksResponse");
    // Verify that we have more blocks in the ledger than can be returned in a single query.
    assert_eq!(res.chain_length, (MAX_BLOCKS_PER_REQUEST + 1) as u64);
    // Verify that the number of blocks in the response is limited to MAX_BLOCKS_PER_REQUEST.
    assert_eq!(res.blocks.len(), MAX_BLOCKS_PER_REQUEST);
    // Also verify that the maximum number of blocks per request is larger than 0, in case the
    // length `(u32::MAX as u64) + 1` in the request was incorrectly cast to a wasm32 `usize`
    // (`u32`)).
    if MAX_BLOCKS_PER_REQUEST == 0 {
        panic!("MAX_BLOCKS_PER_REQUEST should be larger than 0");
    }
```
