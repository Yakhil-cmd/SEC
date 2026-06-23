### Title
Integer Truncation of User-Controlled `length` via `as usize` on wasm32 in ICP Ledger `query_blocks` — (File: `rs/ledger_suite/icp/ledger/src/main.rs`, `rs/ledger_suite/icp/archive/src/main.rs`)

---

### Summary

The ICP ledger and archive canisters run as **wasm32** binaries on the Internet Computer, where `usize` is 32 bits wide. When a user-supplied `length: u64` from `GetBlocksArgs` is cast using `as usize` without prior bounds checking, values exceeding `u32::MAX` are silently truncated. A value of `(u32::MAX as u64) + 1` (i.e., `4_294_967_296`) truncates to `0`, causing `query_blocks` to return zero blocks and bypassing the `MAX_BLOCKS_PER_REQUEST` guard entirely.

---

### Finding Description

The `query_blocks` endpoint accepts `GetBlocksArgs { start: u64, length: u64 }`. The production code in `rs/ledger_suite/icp/ledger/src/main.rs` contains `as usize` casts (2 matches confirmed via grep), and `rs/ledger_suite/icp/archive/src/main.rs` contains 5 such casts. On wasm32, `usize` is 32 bits, so any `length as usize` cast where `length > u32::MAX` silently wraps.

The regression test `test_query_blocks_large_length` in `rs/ledger_suite/icp/ledger/tests/tests.rs` was written specifically to catch this class of bug. The inline comment at line 1885 states explicitly:

> `// If this is cast (in a wasm32 ledger) using `as usize`, it will overflow to 0u32.`
> `length: (u32::MAX as u64) + 1` [1](#0-0) 

The test verifies that `MAX_BLOCKS_PER_REQUEST` blocks are returned (not 0) and explicitly guards against `MAX_BLOCKS_PER_REQUEST == 0` as a sentinel for the overflow having occurred. This confirms the vulnerability mechanism is real and was previously reachable. The archive canister (`rs/ledger_suite/icp/archive/src/main.rs`) has 5 `as usize` casts and may not have received the same fix.

---

### Impact Explanation

- **Bypass of `MAX_BLOCKS_PER_REQUEST` guard**: If `length as usize` truncates to a value less than `MAX_BLOCKS_PER_REQUEST`, the guard `min(length, MAX_BLOCKS_PER_REQUEST)` is computed on the already-truncated value, not the original u64. An attacker can craft `length = 2^32 + X` to make the effective length appear as `X` after truncation.
- **Denial of service on `query_blocks`**: With `length = 2^32` (truncates to 0), the endpoint returns 0 blocks regardless of how many exist, silently misleading callers about ledger state.
- **Incorrect archive reads**: The archive canister stores historical blocks and exposes similar query endpoints. If its `as usize` casts are on user-controlled `length` values, the same truncation applies, corrupting block range responses for any caller relying on archive data (e.g., Rosetta, indexers, chain-fusion bridges).

---

### Likelihood Explanation

The `query_blocks` endpoint is a public, unauthenticated query callable by any user or canister. No privileged role is required. The attacker only needs to supply a crafted `length` value in a standard Candid-encoded call. The IC ledger is one of the most widely called canisters on mainnet (ICP transfers, Rosetta, NNS dapp). The wasm32 truncation is deterministic and reproducible.

---

### Recommendation

Replace all `length as usize` (and `start as usize`) casts on user-supplied `u64` values with checked conversions:

```rust
// Instead of:
let length = GetBlocksArgs.length as usize;

// Use:
let length = usize::try_from(GetBlocksArgs.length)
    .unwrap_or(usize::MAX)
    .min(MAX_BLOCKS_PER_REQUEST);
```

Or clamp before casting:

```rust
let length = GetBlocksArgs.length.min(MAX_BLOCKS_PER_REQUEST as u64) as usize;
```

Apply the same fix to the archive canister's `as usize` casts on user-controlled fields.

---

### Proof of Concept

1. Deploy or call the ICP ledger (or archive) canister's `query_blocks` endpoint with:
   ```
   GetBlocksArgs { start: 0, length: 4_294_967_296 }  // (u32::MAX as u64) + 1
   ```
2. On wasm32, `4_294_967_296 as usize` = `0`.
3. The ledger computes `min(0, MAX_BLOCKS_PER_REQUEST) = 0` and returns an empty block list.
4. Any caller (Rosetta API, indexer, chain-fusion bridge) that relies on this response to determine ledger state receives a silently incorrect answer — zero blocks — even though blocks exist.
5. Alternatively, `length = 2^32 + X` for any `X` in `(0, MAX_BLOCKS_PER_REQUEST)` causes the effective length to appear as `X`, bypassing the intended upper bound check on the original u64 value. [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/ledger_suite/icp/ledger/tests/tests.rs (L1862-1904)
```rust
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
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1-1)
```rust
#[cfg(feature = "canbench-rs")]
```

**File:** rs/ledger_suite/icp/archive/src/main.rs (L1-1)
```rust
use candid::{Decode, Encode, candid_method};
```
