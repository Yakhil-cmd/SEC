Audit Report

## Title
`Approve` Operations Bypass Legacy Fee-Collector Credit in ICRC-1 Rosetta `update_account_balances`, Causing Inaccurate Balance Reporting - (File: `rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs`)

## Summary
The `update_account_balances` function in the ICRC-1 Rosetta API processes `Approve` operations using only the ICRC-107 fee collector (`current_fee_collector_107`) when crediting fees, while `Transfer` and `Burn` operations use the `get_107_fee_collector_or_legacy` helper that also handles legacy fee collectors embedded in block metadata. When `icrc2_approve` is called on a ledger with a legacy fee collector configured, the `account_balances` table is debited for the `from` account but the legacy fee collector is never credited, causing the Rosetta `/account/balance` endpoint to return incorrect balances.

## Finding Description
In `update_account_balances`, the `Approve` arm (lines 418–445) debits the `from` account for the fee and then conditionally credits only the ICRC-107 fee collector:

```rust
if let Some(Some(collector)) = current_fee_collector_107 {
    credit(collector, fee, ...)?;
}
```

The `Transfer` arm (lines 477–489) and `Burn` arm (lines 345–357) both use `get_107_fee_collector_or_legacy`, which resolves the fee collector from either the ICRC-107 metadata or the block's `fee_collector` / `fee_collector_block_index` fields (the legacy mechanism). The `Approve` arm skips this helper entirely. When a block carries a legacy fee collector (i.e., `current_fee_collector_107` is `None` but the block's `fee_collector` field is set), the fee is debited from `from` with no corresponding credit anywhere in `account_balances`. The cache is then flushed to the database at lines 502–514, persisting the divergent state. `get_account_balance_at_block_idx` (lines 863–891) reads exclusively from `account_balances`, so every subsequent balance query for the legacy fee collector account returns an understated value. The test at line 474 of `tests.rs` explicitly labels the missing credit as "the bug" for the `Transfer` case, and `repair_fee_collector_balances` exists to retroactively fix it — but the live `Approve` code path is never repaired and continues to produce broken state.

## Impact Explanation
This is a High-severity issue. Any caller of the Rosetta `/account/balance` endpoint receives an understated balance for the legacy fee collector account after each `Approve` operation. The total of all balances in `account_balances` no longer sums to the true token supply tracked in `blocks`, violating ledger conservation within the Rosetta index. Exchanges, wallets, and financial services relying on the Rosetta API for balance queries receive incorrect data, which can lead to incorrect crediting or debiting of user accounts. This constitutes a concrete, demonstrable financial harm via the Rosetta API — a listed in-scope financial integration component — matching the allowed High impact: "Significant Rosetta security impact with concrete user or protocol harm."

## Likelihood Explanation
Any unprivileged user can call `icrc2_approve` on any ICRC-1 ledger that has a legacy fee collector configured. No special privileges are required. The Rosetta API processes all blocks automatically. Legacy fee collector configuration is a real, documented deployment option used in production ledgers. The bug is triggered deterministically on every `Approve` block processed while a legacy fee collector is active, and it is not repaired by `repair_fee_collector_balances` (which only handles `Transfer` blocks).

## Recommendation
In the `Approve` arm of `update_account_balances`, replace the ICRC-107-only check with the same `get_107_fee_collector_or_legacy` helper used by `Transfer` and `Burn`:

```rust
// Replace:
if let Some(Some(collector)) = current_fee_collector_107 {
    credit(collector, fee, rosetta_block.index, connection, &mut account_balances_cache)?;
}

// With:
if let Some(collector) = get_107_fee_collector_or_legacy(
    &rosetta_block, connection, current_fee_collector_107)? {
    credit(collector, fee, rosetta_block.index, connection, &mut account_balances_cache)?;
}
```

Extend `repair_fee_collector_balances` to also scan and repair historical `Approve` blocks. Audit `Burn` (already fixed) and any future operation types that debit a fee to ensure they all use the same helper.

## Proof of Concept
1. Configure an ICRC-1 ledger with a legacy fee collector (set `fee_collector` in block metadata, leave ICRC-107 metadata absent).
2. Submit an `icrc2_approve` transaction from any account with a non-zero fee.
3. Call `update_account_balances` to process the block.
4. Call `get_account_balance_at_block_idx` for the legacy fee collector account — it will show no credit for the approval fee.
5. Call `get_account_balance_at_block_idx` for the `from` account — it will show the fee was debited.
6. Sum all balances in `account_balances` — the total will be less than the true supply tracked in `blocks`.

This is directly reproducible as a unit test mirroring the existing `repair_fee_collector_balances` test in `tests.rs` (lines 469–487), substituting an `Approve` block for the `Transfer` block used there.