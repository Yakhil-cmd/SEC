Audit Report

## Title
ICRC-1 Index-NG Canister Permanently Halted by Mint Block Where Fee Exceeds Amount - (File: `rs/ledger_suite/icrc1/index-ng/src/main.rs`)

## Summary
The `Operation::Mint` arm of `process_balance_changes` calls `trap()` when `checked_sub` returns `None` (i.e., when `fee >= amount`). Because a trap rolls back all state changes, the block cursor never advances, and every subsequent sync attempt re-enters the same code path and traps again, permanently halting the index canister's block processing until it is manually upgraded.

## Finding Description
At `rs/ledger_suite/icrc1/index-ng/src/main.rs` lines 1044–1058, the Mint arm computes the net credit by subtracting the effective fee from the gross amount:

```rust
Operation::Mint { to, amount, fee } => {
    let mut amount_without_fee = amount;
    let effective_fee = block.effective_fee.or(fee);
    if let Some(fee) = effective_fee {
        amount_without_fee = amount.checked_sub(&fee).unwrap_or_else(|| {
            trap(format!(
                "token amount underflow while indexing block {block_index}"
            ))
        });
        ...
    }
    credit(block_index, to, amount_without_fee)
}
```

When `fee >= amount`, `checked_sub` returns `None` and `trap()` is invoked. On the IC, a trap aborts the current message and rolls back all state mutations, including any advancement of the block cursor. The next heartbeat/timer fires, fetches the same block at the same index, and traps again. No existing guard validates `fee < amount` before the subtraction. The only test covering this path (`should_take_mint_block_fee_into_account`, lines 1897–1957) uses `MINT_FEE = 10_000` and `MINT_AMOUNT = 10_000_000`, exercising only the non-underflow path.

The Rosetta API counterpart at `rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs` lines 363–366 propagates the error via `?` rather than trapping, which stalls the Rosetta balance-update loop but does not cause an infinite trap loop.

## Impact Explanation
Any deployed ICRC-1 Index-NG canister configured to sync from a ledger that emits a Mint block with `fee >= amount` is permanently halted: it cannot advance its block cursor, serve up-to-date `icrc1_balance_of` responses, or return accurate transaction history. This is a concrete application-level DoS matching the High bounty impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."* Severity is High ($2,000–$10,000) rather than Critical because the ledger itself continues to operate and the impact is scoped to the index canister.

## Likelihood Explanation
The standard ICRC-1 ledger rejects mint fees at the ledger layer (`TxApplyError::BurnOrMintFee`), so production NNS/SNS ledgers are not directly affected. However, the index canister is explicitly designed to consume *any* ICRC-3-compliant ledger. A canister developer deploying a custom ICRC-3 ledger (a realistic and in-scope scenario) can trivially emit a Mint block with `fee >= amount`, either maliciously or due to a fee-accounting bug. The `BlockBuilder` test infrastructure already supports constructing such blocks, confirming the block format is valid at the protocol level. No special privileges are required beyond deploying a canister.

## Recommendation
Before performing the subtraction, validate that `fee < amount`. If the invariant is violated, the index canister should log a critical error and either skip the block (recording it as unprocessable) or halt gracefully with a descriptive error rather than trapping in a tight loop:

```rust
if fee >= amount {
    ic_cdk::trap(format!(
        "Block {block_index}: mint fee {fee} >= mint amount {amount}; \
         cannot compute net credit"
    ));
    // OR: log + skip block, advancing the cursor past the unprocessable block
}
let amount_without_fee = amount.checked_sub(&fee).expect("validated above");
```

The preferred recovery is to skip the block and advance the cursor, logging the anomaly, so the canister remains operational for subsequent valid blocks.

## Proof of Concept
1. Deploy a custom canister implementing `icrc3_get_blocks` that returns a single Mint block with `amount = 100` and `fee = 200` (or `effective_fee = 200`).
2. Deploy an ICRC-1 Index-NG canister configured to sync from the custom canister.
3. Observe that on every heartbeat the index canister traps with `"token amount underflow while indexing block 0"` and its block cursor never advances past block 0.
4. All `icrc1_balance_of` queries return stale (zero) balances indefinitely.

A deterministic integration test can reproduce this by extending the existing `should_take_mint_block_fee_into_account` test with `MINT_FEE > MINT_AMOUNT` and asserting that the canister traps and the cursor does not advance.