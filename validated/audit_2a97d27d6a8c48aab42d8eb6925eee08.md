Audit Report

## Title
Silent `u64 → usize` Truncation in ICRC-1 Ledger Block/Transaction Queries on wasm32 - (File: rs/ledger_suite/icrc1/ledger/src/main.rs)

## Summary
Both `get_transactions` and `get_blocks` query handlers in the ICRC-1 ledger cast a caller-supplied `u64` length directly to `usize` without first capping it to `usize::MAX`. On the wasm32 IC canister ABI, `usize` is 32 bits, so any `length` value in `[u32::MAX+1, u64::MAX]` wraps modulo 2^32 on cast. A caller supplying `length = u32::MAX + 1` (4294967296) causes the ledger to silently return zero blocks/transactions despite data existing, and the `get_blocks` response includes a valid data certificate alongside the empty block list, creating a misleading certified response.

## Finding Description
In `rs/ledger_suite/icrc1/ledger/src/main.rs` at lines 797 and 806, both public query endpoints perform an unchecked narrowing cast:

```rust
// line 797
Access::with_ledger(|ledger| ledger.get_transactions(start, length as usize))
// line 806
Access::with_ledger(|ledger| ledger.get_blocks(start, length as usize))
```

The `length` value is produced by `as_start_and_length()` in `packages/icrc-ledger-types/src/icrc3/blocks.rs` (lines 75–81), which only validates that the Candid `nat` fits in `u64` — it does not cap to `usize::MAX`. On wasm32, `4294967296u64 as usize` evaluates to `0u32`. The internal `query_blocks` method in `rs/ledger_suite/icrc1/ledger/src/lib.rs` (line 1015) then calls `block_locations(self, start, 0)`, producing an empty range. Both `blocks`/`transactions` and `archived_blocks` in the response are empty, while `chain_length` correctly reflects the actual ledger length — a detectable but easily-missed inconsistency. The `get_blocks` response also embeds `ic_cdk::api::data_certificate()` (line 1073), so the empty-block response carries a valid certificate for the chain tip, making the response appear legitimate to clients that do not cross-check `chain_length` against the returned block count.

The ICP ledger already applies the correct guard at `rs/ledger_suite/icp/ledger/src/main.rs` line 1065:
```rust
let locations = block_locations(&*ledger, start, length.min(usize::MAX as u64) as usize);
```
and the ICP ledger test suite at `rs/ledger_suite/icp/ledger/tests/tests.rs` lines 1885–1886 explicitly documents this exact truncation hazard with the comment: *"If this is cast (in a wasm32 ledger) using `as usize`, it will overflow to 0u32."*

## Impact Explanation
An unprivileged caller submitting `length = u32::MAX + 1` to `get_transactions` or `get_blocks` receives an empty response with a valid data certificate, despite the ledger holding matching data. Clients performing balance reconstruction, audit, or chain-fusion bridging that rely on these endpoints without cross-checking `chain_length` may silently conclude no transactions exist in the requested range. The `get_blocks` response embeds a real IC data certificate alongside the empty block list, making the misleading response appear certified. This constitutes a forged/misleading certified response accepted under constrained conditions, matching the **Medium** bounty impact tier.

## Likelihood Explanation
Any unprivileged caller can trigger this by submitting a single query or ingress call with `length = Nat(4294967296)`. No special privileges, node control, or social engineering are required. Standard client libraries use small lengths, so accidental triggering is unlikely, but deliberate exploitation requires only knowledge of the Candid interface. The `GetTransactionsRequest.length` and `GetBlocksArgs.length` fields are typed as `nat` (arbitrary precision), making the oversized value fully encodable by any Candid client.

## Recommendation
Apply the same guard already present in the ICP ledger to both ICRC-1 handlers in `rs/ledger_suite/icrc1/ledger/src/main.rs`:

```rust
// get_transactions (line 797)
Access::with_ledger(|ledger| ledger.get_transactions(start, length.min(usize::MAX as u64) as usize))

// get_blocks (line 806)
Access::with_ledger(|ledger| ledger.get_blocks(start, length.min(usize::MAX as u64) as usize))
```

Additionally, consider adding a test analogous to `test_query_blocks_large_length` in the ICRC-1 ledger test suite to prevent regression.

## Proof of Concept
1. Deploy an ICRC-1 ledger canister (wasm32 build) with at least one transaction.
2. Call `get_transactions` with `start = Nat(0)` and `length = Nat(4294967296)` (i.e., `u32::MAX + 1`).
3. On wasm32, `4294967296u64 as usize` evaluates to `0u32`.
4. The ledger calls `ledger.get_transactions(0, 0)`, `block_locations` returns an empty range, and the response has `transactions = []` and `archived_transactions = []` despite `log_length > 0`.
5. Repeat with `get_blocks`; the response additionally includes a valid `certificate` field alongside the empty `blocks = []`.
6. The analogous integration test `test_query_blocks_large_length` in `rs/ledger_suite/icp/ledger/tests/tests.rs` (lines 1861–1905) demonstrates the identical truncation path and confirms the fix required; an equivalent test for the ICRC-1 ledger would reproduce the bug.