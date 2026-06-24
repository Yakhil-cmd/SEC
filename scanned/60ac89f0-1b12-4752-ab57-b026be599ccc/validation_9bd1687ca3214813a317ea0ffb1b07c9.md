### Title
Silent `u64 → usize` Truncation in ICRC-1 Ledger Block/Transaction Queries on wasm32 - (File: rs/ledger_suite/icrc1/ledger/src/main.rs)

### Summary
The ICRC-1 ledger's `get_transactions` and `get_blocks` query handlers cast a caller-supplied `u64` length directly to `usize` without first capping it to `usize::MAX`. On the wasm32 target (the IC canister ABI), `usize` is 32 bits. A `length` value of `u32::MAX + 1` (i.e., `4294967296`) silently truncates to `0`, causing the ledger to return zero blocks/transactions to the caller.

### Finding Description
In `rs/ledger_suite/icrc1/ledger/src/main.rs`, both public query endpoints perform an unchecked narrowing cast:

```rust
// line 797
Access::with_ledger(|ledger| ledger.get_transactions(start, length as usize))

// line 806
Access::with_ledger(|ledger| ledger.get_blocks(start, length as usize))
```

`length` is a `u64` produced by `as_start_and_length()`, which only validates that the Candid `Nat` fits in `u64` — it does not cap to `usize::MAX`. [1](#0-0) 

On wasm32, `usize::MAX == u32::MAX == 4294967295`. Any `length` value in the range `[u32::MAX+1, u64::MAX]` wraps modulo `2^32` when cast with `as usize`. For example, `length = 4294967296` becomes `0 as usize`, so the ledger returns an empty result set.

The ICP ledger already has the correct guard in its analogous `query_blocks` handler:

```rust
let locations = block_locations(&*ledger, start, length.min(usize::MAX as u64) as usize);
``` [2](#0-1) 

The ICP ledger test suite even documents this exact truncation hazard:

```rust
// If this is cast (in a wasm32 ledger) using `as usize`, it will overflow to 0u32.
length: (u32::MAX as u64) + 1
``` [3](#0-2) 

The ICRC-1 ledger's `GetBlocksRequest` uses a Candid `nat` (arbitrary-precision) for `length`, so any value can be submitted by an unprivileged caller. [4](#0-3) 

### Impact Explanation
An unprivileged caller submitting `length = u32::MAX + 1` to `get_transactions` or `get_blocks` on an ICRC-1 ledger receives an empty response (0 blocks/transactions) even though the ledger holds matching data. Clients that rely on these queries for balance reconstruction, audit, or chain-fusion bridging logic may incorrectly conclude that no transactions exist in the requested range, leading to silent data loss at the application layer.

**Impact: Medium** — incorrect ledger query results; does not directly allow fund theft but can corrupt client-side state derived from block history.

### Likelihood Explanation
**Likelihood: Low** — requires a caller to deliberately (or accidentally) supply a `length` value exceeding `u32::MAX`. Standard client libraries use small lengths, but the Candid interface accepts arbitrary `nat` values, making it reachable by any unprivileged ingress or query caller.

### Recommendation
Apply the same guard already present in the ICP ledger to both ICRC-1 handlers:

```rust
// get_transactions
Access::with_ledger(|ledger| ledger.get_transactions(start, length.min(usize::MAX as u64) as usize))

// get_blocks
Access::with_ledger(|ledger| ledger.get_blocks(start, length.min(usize::MAX as u64) as usize))
``` [1](#0-0) 

### Proof of Concept
1. Deploy an ICRC-1 ledger canister (wasm32 build) with at least one transaction.
2. Call `get_transactions` with `start = 0` and `length = Nat(4294967296)` (i.e., `u32::MAX + 1`).
3. On wasm32, `4294967296u64 as usize` evaluates to `0`.
4. The ledger calls `ledger.get_transactions(0, 0)` and returns an empty `GetTransactionsResponse` with `transactions = []`, despite transactions existing.
5. Repeat with `get_blocks` for the same result.

The ICP ledger test `test_query_blocks_large_length` demonstrates the identical truncation path and confirms the fix required. [5](#0-4)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L793-807)
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
}
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1063-1065)
```rust
fn query_blocks(GetBlocksArgs { start, length }: GetBlocksArgs) -> QueryBlocksResponse {
    let ledger = LEDGER.read().unwrap();
    let locations = block_locations(&*ledger, start, length.min(usize::MAX as u64) as usize);
```

**File:** rs/ledger_suite/icp/ledger/tests/tests.rs (L1861-1905)
```rust
#[test]
fn test_query_blocks_large_length() {
    let env = StateMachine::new();
    let mut initial_balances = HashMap::new();
    for i in 0..MAX_BLOCKS_PER_REQUEST + 1 {
        let user = PrincipalId::new_user_test_id(i as u64);
        initial_balances.insert(Account::from(user.0).into(), Tokens::from_e8s(100_000));
    }
    let payload = LedgerCanisterInitPayload::builder()
        .minting_account(MINTER.into())
        .initial_values(initial_balances)
        .build()
        .unwrap();
    let canister_id = env
        .install_canister(ledger_wasm(), Encode!(&payload).unwrap(), None)
        .expect("Unable to install the Ledger canister with the new init");

    // query_blocks
    let res = Decode!(
        &env.execute_ingress(
            canister_id,
            "query_blocks",
            Encode!(&GetBlocksArgs {
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
}
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L301-306)
```text
type GetBlocksArgs = record {
  // The index of the first block to fetch.
  start : BlockIndex;
  // Max number of blocks to fetch.
  length : nat
};
```
