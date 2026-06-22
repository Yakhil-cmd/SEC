### Title
Silent Integer Truncation of `length` Parameter in ICRC-1 Ledger Block/Transaction Query Endpoints on wasm32 - (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

### Summary

The ICRC-1 ledger's `get_blocks` and `get_transactions` query endpoints cast the caller-supplied `length` parameter from `u64` directly to `usize` using an unchecked `as` cast. On the wasm32 target (which all IC canisters use), `usize` is 32 bits. Any `length` value greater than `u32::MAX` is silently truncated to its lower 32 bits — most critically, `length = (u32::MAX as u64) + 1 = 4_294_967_296` truncates to `0`, causing the endpoint to return zero blocks with no archived-block pointers, silently hiding the entire ledger history from the caller.

### Finding Description

In `rs/ledger_suite/icrc1/ledger/src/main.rs`, both `get_transactions` and `get_blocks` call `req.as_start_and_length()`, which returns `(u64, u64)`, and then immediately cast the returned `length: u64` to `usize` with an unchecked `as`:

```rust
// line 793-797
fn get_transactions(req: GetTransactionsRequest) -> GetTransactionsResponse {
    let (start, length) = req
        .as_start_and_length()
        .unwrap_or_else(|msg| ic_cdk::api::trap(&msg));
    Access::with_ledger(|ledger| ledger.get_transactions(start, length as usize))
}

// line 802-806
fn get_blocks(req: GetBlocksRequest) -> GetBlocksResponse {
    let (start, length) = req
        .as_start_and_length()
        .unwrap_or_else(|msg| ic_cdk::api::trap(&msg));
    Access::with_ledger(|ledger| ledger.get_blocks(start, length as usize))
}
``` [1](#0-0) 

`as_start_and_length()` caps `length` to `u64::MAX` but performs no wasm32-`usize` guard:

```rust
pub fn as_start_and_length(&self) -> Result<(u64, u64), String> {
    let length = self.length.0.to_u64().ok_or_else(|| { ... })?;
    Ok((start, length))
}
``` [2](#0-1) 

On wasm32, `u64 as usize` is equivalent to `u64 as u32` — the upper 32 bits are discarded. For `length = 4_294_967_296` (`u32::MAX + 1`), the result is `0`. The downstream `block_locations(self, start, 0)` then returns an empty local range and empty archived-block pointers, so the response contains zero blocks and zero archive callbacks — silently hiding all ledger history from the caller.

By contrast, the ICP ledger explicitly guards against this with `length.min(usize::MAX as u64) as usize` and has a dedicated regression test:

```rust
// ICP ledger (correct):
let locations = block_locations(&*ledger, start, length.min(usize::MAX as u64) as usize);
``` [3](#0-2) 

The test comment explicitly documents the wasm32 truncation risk:

```rust
// If this is cast (in a wasm32 ledger) using `as usize`, it will overflow to 0u32.
length: (u32::MAX as u64) + 1
``` [4](#0-3) 

The ICRC-1 ledger has no equivalent guard and no equivalent test.

The `icrc3_get_blocks` path in the same ledger library is correctly guarded:

```rust
let length = max_length.min(length).min(usize::MAX as u64) as usize;
``` [5](#0-4) 

This inconsistency confirms the `get_blocks` / `get_transactions` paths were not updated when the fix was applied elsewhere.

### Impact Explanation

Any unprivileged ingress or query caller can send a `GetBlocksRequest` or `GetTransactionsRequest` with `length = (u32::MAX as u64) + 1` (or any multiple of `2^32`). The ICRC-1 ledger canister silently returns an empty block list and empty archive pointers. Downstream indexers, wallets, and chain-fusion bridges that rely on these endpoints to reconstruct ledger history will observe a gap or complete absence of blocks, potentially causing:

- Missed ICRC-1 token transfers in off-chain accounting systems
- Incorrect balance reconstruction by indexers
- Denial of historical data availability for any client using the affected endpoints

No panic or trap is raised; the response is structurally valid but semantically wrong, making the bug hard to detect.

### Likelihood Explanation

The `length` field is a `Nat` (arbitrary-precision integer) in the Candid interface, so any caller — including anonymous query callers — can supply a value exceeding `u32::MAX` with no special privilege. The ICRC-1 ledger is deployed on mainnet for ckBTC, ckETH, SNS tokens, and other assets. The ICP ledger's own regression test (`test_query_blocks_large_length`) demonstrates the exact truncation scenario, confirming the platform team is aware of the wasm32 hazard. The ICRC-1 ledger simply lacks the same fix.

### Recommendation

Apply the same guard used in the ICP ledger to both affected functions:

```rust
fn get_transactions(req: GetTransactionsRequest) -> GetTransactionsResponse {
    let (start, length) = req
        .as_start_and_length()
        .unwrap_or_else(|msg| ic_cdk::api::trap(&msg));
    Access::with_ledger(|ledger| {
        ledger.get_transactions(start, length.min(usize::MAX as u64) as usize)
    })
}

fn get_blocks(req: GetBlocksRequest) -> GetBlocksResponse {
    let (start, length) = req
        .as_start_and_length()
        .unwrap_or_else(|msg| ic_cdk::api::trap(&msg));
    Access::with_ledger(|ledger| {
        ledger.get_blocks(start, length.min(usize::MAX as u64) as usize)
    })
}
```

Alternatively, move the `usize::MAX` cap into `as_start_and_length()` itself so all callers benefit automatically.

### Proof of Concept

Mirroring the ICP ledger's own regression test structure:

```rust
#[test]
fn test_icrc1_get_blocks_large_length_truncation() {
    let env = StateMachine::new();
    // install ICRC-1 ledger with some initial blocks ...
    let canister_id = install_icrc1_ledger(&env);

    // Seed a few transactions so the ledger has blocks
    for _ in 0..5 { do_transfer(&env, canister_id); }

    let req = GetBlocksRequest {
        start: Nat::from(0u64),
        // (u32::MAX as u64) + 1 truncates to 0 on wasm32
        length: Nat::from((u32::MAX as u64) + 1),
    };

    let res: GetBlocksResponse = query(env, canister_id, "get_blocks", req);

    // On a fixed ledger this should be > 0; on the buggy ledger it is 0
    assert!(
        !res.blocks.is_empty(),
        "BUG: get_blocks returned 0 blocks due to wasm32 usize truncation"
    );
}
```

On the unfixed wasm32 canister, `length as usize` evaluates to `0`, `block_locations` returns an empty range, and `res.blocks` is empty despite the ledger containing blocks.

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L793-806)
```rust
fn get_transactions(req: GetTransactionsRequest) -> GetTransactionsResponse {
    let (start, length) = req
        .as_start_and_length()
        .unwrap_or_else(|msg| ic_cdk::api::trap(&msg));
    Access::with_ledger(|ledger| ledger.get_transactions(start, length as usize))
}

#[cfg(not(feature = "get-blocks-disabled"))]
#[query]
fn get_blocks(req: GetBlocksRequest) -> GetBlocksResponse {
    let (start, length) = req
        .as_start_and_length()
        .unwrap_or_else(|msg| ic_cdk::api::trap(&msg));
    Access::with_ledger(|ledger| ledger.get_blocks(start, length as usize))
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

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1065-1065)
```rust
    let locations = block_locations(&*ledger, start, length.min(usize::MAX as u64) as usize);
```

**File:** rs/ledger_suite/icp/ledger/tests/tests.rs (L1884-1886)
```rust
                start: 0,
                // If this is cast (in a wasm32 ledger) using `as usize`, it will overflow to 0u32.
                length: (u32::MAX as u64) + 1
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L1120-1120)
```rust
            let length = max_length.min(length).min(usize::MAX as u64) as usize;
```
