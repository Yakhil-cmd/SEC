Audit Report

## Title
ckETH Withdrawal Permanently Stuck With No Reimbursement When Gas Fees Spike Beyond Withdrawal Amount During Resubmission — (File: `rs/ethereum/cketh/minter/src/tx.rs`)

## Summary

When a ckETH withdrawal transaction is sent but not mined, and Ethereum gas fees subsequently spike such that the new estimated fee exceeds the original `withdrawal_amount`, `SignedTransactionRequest::resubmit()` returns `ResubmitTransactionError::InsufficientTransactionFee`. The caller `resubmit_transactions_batch` only logs this error and takes no further action — no reimbursement of the already-burned ckETH is triggered. The user's ckETH is permanently lost, the conservation invariant (1 ckETH ↔ 1 ETH) is violated, and all subsequent pending withdrawals with higher nonces are blocked until a governance upgrade resolves the stuck transaction.

## Finding Description

**Phase 1 — Burn:** `withdraw_eth` in `rs/ethereum/cketh/minter/src/main.rs` burns ckETH from the user's ledger account and stores `withdrawal_amount` in `EthWithdrawalRequest`. This burn is irreversible.

**Phase 2 — Transaction creation:** `create_transaction` in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs` (L1122–1134) computes `tx_amount = withdrawal_amount - max_tx_fee_estimate`. If fees are already too high here, the request is rescheduled (safe — no burn has occurred yet at this decision point in the queue).

**Phase 3 — Resubmission:** `SignedTransactionRequest::resubmit()` in `rs/ethereum/cketh/minter/src/tx.rs` (L169–173) enforces a strict ceiling:

```rust
if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {
    return Err(ResubmitTransactionError::InsufficientTransactionFee { ... });
}
```

For ckETH withdrawals, `ResubmissionStrategy` is `ReduceEthAmount { withdrawal_amount }`, so `allowed_max_transaction_fee()` returns `withdrawal_amount` (L139). If gas fees spike such that `new_fee > withdrawal_amount`, this check fails.

`create_resubmit_transactions` in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs` (L618–631) propagates this error and immediately returns, stopping processing of all higher-nonce transactions:

```rust
Err(crate::tx::ResubmitTransactionError::InsufficientTransactionFee { ... }) => {
    transactions_to_resubmit.push(Err(...));
    return transactions_to_resubmit;
}
```

`resubmit_transactions_batch` in `rs/ethereum/cketh/minter/src/withdraw.rs` (L242–244) only logs the error:

```rust
Err(e) => {
    log!(INFO, "Failed to resubmit transaction: {e:?}");
}
```

No reimbursement event is emitted, no entry is added to `reimbursement_requests`, and `process_reimbursement` is never invoked for this case.

**Contrast with the finalized-failure path:** When a transaction is mined but fails on-chain, `record_finalized_transaction` adds a `ReimbursementRequest` to state, which `process_reimbursement` then processes to mint ckETH back to the user. This path is confirmed by the test `should_record_finalized_transaction_and_reimburse_unused_tx_fee_when_cketh_withdrawal_fails` in `rs/ethereum/cketh/minter/src/state/transactions/tests.rs` (L1689–1737). No equivalent path exists for the resubmission failure case.

## Impact Explanation

A user's ckETH is burned from the ICRC-1 ledger but the corresponding ETH is never delivered. The chain-fusion conservation invariant (1 ckETH ↔ 1 ETH) is violated for the affected withdrawal. Additionally, because `create_resubmit_transactions` stops at the first `InsufficientTransactionFee` error, all subsequent pending withdrawals with higher nonces are blocked until the stuck transaction is resolved via a governance canister upgrade. This matches the allowed impact: **High — Significant Chain Fusion / ck-token security impact with concrete user and protocol harm**, including permanent loss of user funds and protocol-level DoS on the withdrawal queue.

## Likelihood Explanation

The minimum ckETH withdrawal amount was recently reduced from 0.03 ETH to **0.005 ETH** (confirmed in `rs/ethereum/cketh/mainnet/minter_upgrade_2026_05_29.md`, L21). For a standard ETH transfer (21,000 gas), a gas price of ~238 gwei produces a fee of exactly 0.005 ETH. Ethereum gas prices have historically reached 300–500 gwei during high-activity periods (NFT mints, DeFi events). Any user withdrawing at or near the minimum amount is vulnerable to a gas spike of this magnitude. No privileged access is required — the attacker-controlled entry path is simply calling `withdraw_eth` with the minimum amount; the gas spike is an external network condition. The ckBTC minter experienced a real-world instance of stuck transactions requiring an emergency governance upgrade due to a related fee-estimation issue (`rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md`), confirming this class of issue is not purely theoretical.

## Recommendation

When `ResubmitTransactionError::InsufficientTransactionFee` is encountered in `resubmit_transactions_batch`, the minter should initiate a reimbursement of the burned ckETH (minus any fees consumed by the original transaction attempt), analogous to the reimbursement path used for finalized-but-failed Ethereum transactions. Concretely, the error branch in `resubmit_transactions_batch` should call `mutate_state` to record a `ReimbursementRequest` for the affected `ledger_burn_index`, which `process_reimbursement` will then process. Alternatively, the `create_resubmit_transactions` function could be extended to return enough context (the original `withdrawal_amount` and the `from` address) to construct the reimbursement request directly in the caller.

## Proof of Concept

1. Alice calls `withdraw_eth` with `amount = 5_000_000_000_000_000` wei (0.005 ETH, the current minimum). The minter burns 0.005 ckETH from Alice's ledger account.
2. The minter estimates gas at 10 gwei and creates a transaction with value `0.005 ETH - 0.00021 ETH = 0.00479 ETH`. The transaction is sent to Ethereum.
3. The transaction is not mined (fee estimate was too low or network congestion).
4. Ethereum gas fees spike to 300 gwei (historically observed).
5. On the next timer tick, `resubmit_transactions_batch` calls `create_resubmit_transactions`. For Alice's transaction: `new_tx_price.max_transaction_fee() = 21_000 * 300 gwei = 0.0063 ETH > 0.005 ETH = allowed_max_transaction_fee`.
6. `InsufficientTransactionFee` is returned and only logged. No reimbursement event is emitted.
7. All subsequent pending withdrawals with higher nonces are also blocked.
8. Alice's 0.005 ckETH is permanently burned; she receives no ETH and has no recourse without a governance upgrade.

A deterministic integration test can reproduce this by: (a) recording a withdrawal request and a sent transaction with a low fee, (b) calling `create_resubmit_transactions` with a `GasFeeEstimate` whose `max_transaction_fee` exceeds `withdrawal_amount`, and (c) asserting that no `ReimbursementRequest` is present in state and that `reimbursement_requests` remains empty — confirming the missing reimbursement path.