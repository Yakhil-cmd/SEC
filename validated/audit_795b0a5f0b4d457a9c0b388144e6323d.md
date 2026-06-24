Audit Report

## Title
ckERC20 Withdrawal Head-of-Line Blocking via Insufficient `max_transaction_fee` During Gas Spike - (`rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

## Summary

When a ckERC20 withdrawal transaction is sent with a user-supplied `max_transaction_fee` that is later insufficient to cover a gas-price spike, `create_resubmit_transactions` returns `InsufficientTransactionFee` and immediately exits, leaving the stuck nonce in `sent_tx` indefinitely. Because Ethereum enforces sequential nonce ordering and `sent_transactions_to_finalize` only considers nonces below the on-chain finalized count, every subsequent withdrawal (nonces N+1, N+2, …) is permanently blocked from finalization until gas prices fall or a canister upgrade is performed. No privileged access is required to trigger this condition.

## Finding Description

**Root cause — ckERC20 transactions are created at the user's fee cap from the start:**

In `create_transaction` for `WithdrawalRequest::CkErc20`, the transaction is created with `max_fee_per_gas = request.max_transaction_fee / gas_limit` — the user's hard cap — from the very first submission. [1](#0-0) 

If gas prices later spike above this cap, the original transaction will not be mined (Ethereum requires `block.base_fee_per_gas ≤ transaction.max_fee_per_gas`), and the minter cannot resubmit at a higher fee.

**`create_resubmit_transactions` early-returns on the first fee-cap breach:**

The function iterates over pending sent transactions in nonce order. When `SignedTransactionRequest::resubmit` returns `InsufficientTransactionFee` for nonce N, the error is pushed and the function immediately `return`s, leaving nonces N+1, N+2, … unprocessed. The code comment explicitly acknowledges this: *"if a transaction with nonce n could not be resubmitted … then the next transactions with nonces n+1, n+2, … are blocked anyway."* [2](#0-1) 

**`SignedTransactionRequest::resubmit` enforces the user-supplied cap:**

For `GuaranteeEthAmount` (ckERC20), `allowed_max_transaction_fee()` returns the user-supplied `ckerc20.max_transaction_fee`. If the new price exceeds this, `Err(InsufficientTransactionFee)` is returned. [3](#0-2) [4](#0-3) 

**The caller silently logs and discards the error:**

`resubmit_transactions_batch` iterates the returned `Vec`, logs the error, and does nothing else. The stuck transaction remains in `sent_tx` with its original low fee cap indefinitely. [5](#0-4) 

**Finalization is gated on the Ethereum finalized transaction count:**

`sent_transactions_to_finalize` only considers nonces strictly below `finalized_transaction_count`. Because Ethereum will not mine nonce N+1 before nonce N is mined, the finalized count never advances past N. [6](#0-5) 

**Contrast with the pre-send path:**

`create_transactions_batch` handles `InsufficientTransactionFee` during transaction *creation* (before a nonce is assigned) by calling `reschedule_withdrawal_request`, moving the request to the back of the queue. No equivalent recovery exists for the post-send path. [7](#0-6) 

**ckETH withdrawals are not affected** because they use `ResubmissionStrategy::ReduceEthAmount { withdrawal_amount }`, where `allowed_max_transaction_fee` equals the full withdrawal amount — typically orders of magnitude larger than the gas fee. Only ckERC20 withdrawals use `GuaranteeEthAmount` with the user-supplied cap. [8](#0-7) 

## Impact Explanation

A single ckERC20 withdrawal with an insufficient `max_transaction_fee` during a gas spike causes a complete DoS of the ckETH/ckERC20 withdrawal finalization queue. All users whose withdrawals were submitted after the stuck one cannot receive their funds until gas prices drop below the stuck transaction's fee cap or a canister upgrade is performed. This is a concrete, application/platform-level DoS on a production Chain Fusion financial integration component, matching the **High ($2,000–$10,000)** impact tier: *"Application/platform-level DoS … or subnet availability impact not based on raw volumetric DDoS"* and *"Significant Chain Fusion, ck-token … security impact with concrete user or protocol harm."*

## Likelihood Explanation

Any unprivileged user calling `withdraw_erc20` can trigger this by supplying a `max_transaction_fee` that is valid at submission time (passes the `create_transaction` check) but insufficient during a subsequent gas-price spike. Ethereum gas prices are historically volatile; spikes of 5–10× within hours are common. An adversary can deliberately set a minimal `max_transaction_fee` (just enough to pass the initial check at current gas prices) and wait for natural congestion, or time the submission to coincide with a known high-activity event. No privileged access, no threshold corruption, and no external oracle manipulation is required.

## Recommendation

1. **Add a cancel-and-reimburse path for stuck nonces.** When `create_resubmit_transactions` returns `InsufficientTransactionFee` for nonce N, the minter should burn the nonce by sending a zero-value self-transfer at the current gas price (funded from the minter's ETH reserve), remove the entry from `sent_tx`, and reimburse the user's ckERC20 tokens. This unblocks nonces N+1, N+2, ….
2. **Alternatively, expose an operator-callable `cancel_withdrawal` endpoint** that performs the same nonce-burn and reimbursement under governance control.
3. **Enforce a minimum `max_transaction_fee` multiplier** at withdrawal submission time (e.g., a multiple of the current gas estimate) to reduce the probability of a fee cap being breached during normal volatility.

## Proof of Concept

The existing test `should_not_resubmit_ckerc20_transactions_unless_max_priority_fee_increases` already demonstrates the `InsufficientTransactionFee` path in isolation: [9](#0-8) 

A full integration PoC:

1. Deploy the minter on a local PocketIC/replica fork.
2. Call `withdraw_erc20` with `max_transaction_fee` set to exactly `current_gas_estimate × gas_limit` (barely passes the `create_transaction` check).
3. Advance the timer so the minter creates, signs, and sends the transaction (nonce N). Submit a second withdrawal (nonce N+1).
4. Simulate a gas-price spike (e.g., double `base_fee_per_gas`) in the mock EVM RPC responses.
5. Advance the timer to trigger `resubmit_transactions_batch`. Observe that:
   - `create_resubmit_transactions` returns `Err(InsufficientTransactionFee)` for nonce N and exits immediately.
   - The log emits `"Failed to resubmit transaction"` for nonce N.
   - `sent_transactions_to_finalize` returns an empty map because `finalized_transaction_count` is still N.
6. Advance the timer repeatedly; observe that nonce N+1 is never finalized regardless of how many timer ticks pass.

### Citations

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L530-536)
```rust
            resubmission: match &withdrawal_request {
                WithdrawalRequest::CkEth(cketh) => ResubmissionStrategy::ReduceEthAmount {
                    withdrawal_amount: cketh.withdrawal_amount,
                },
                WithdrawalRequest::CkErc20(ckerc20) => ResubmissionStrategy::GuaranteeEthAmount {
                    allowed_max_transaction_fee: ckerc20.max_transaction_fee,
                },
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L589-630)
```rust
    /// We stop on the first error since if a transaction with nonce n could not be resubmitted
    /// (e.g., the transaction amount does not cover the new fees),
    /// then the next transactions with nonces n+1, n+2, ... are blocked anyway
    /// and trying to resubmit them would only artificially increase their transaction fees.
    pub fn create_resubmit_transactions(
        &self,
        latest_transaction_count: TransactionCount,
        current_gas_fee: GasFeeEstimate,
    ) -> Vec<Result<(LedgerBurnIndex, Eip1559TransactionRequest), ResubmitTransactionError>> {
        // If transaction count at block height H is c > 0, then transactions with nonces
        // 0, 1, ..., c - 1 were mined. If transaction count is 0, then no transactions were mined.
        // The nonce of the first pending transaction is then exactly c.
        let first_pending_tx_nonce: TransactionNonce = latest_transaction_count.change_units();
        let mut transactions_to_resubmit = Vec::new();
        for (nonce, burn_index, signed_tx) in self
            .sent_tx
            .iter()
            .filter(|(nonce, _burn_index, _signed_tx)| *nonce >= &first_pending_tx_nonce)
        {
            let last_signed_tx = signed_tx.last().expect("BUG: empty sent transactions list");
            match last_signed_tx.resubmit(current_gas_fee.clone()) {
                Ok(Some(new_tx)) => {
                    transactions_to_resubmit.push(Ok((*burn_index, new_tx)));
                }
                Ok(None) => {
                    // the transaction fee is still up-to-date but because the transaction did not get included,
                    // we re-send it as is to be sure that it remains known to the mempool and hopefully be included at some point.
                    // Since we always re-send the last non-included transactions in sent_tx, there is nothing to do.
                }
                Err(crate::tx::ResubmitTransactionError::InsufficientTransactionFee {
                    allowed_max_transaction_fee,
                    actual_max_transaction_fee,
                }) => {
                    transactions_to_resubmit.push(Err(
                        ResubmitTransactionError::InsufficientTransactionFee {
                            ledger_burn_index: *burn_index,
                            transaction_nonce: *nonce,
                            allowed_max_transaction_fee,
                            max_transaction_fee: actual_max_transaction_fee,
                        },
                    ));
                    return transactions_to_resubmit;
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L654-664)
```rust
    pub fn sent_transactions_to_finalize(
        &self,
        finalized_transaction_count: &TransactionCount,
    ) -> BTreeMap<Hash, LedgerBurnIndex> {
        let first_non_finalized_tx_nonce: TransactionNonce =
            finalized_transaction_count.change_units();
        let mut transactions = BTreeMap::new();
        for (_nonce, index, sent_txs) in self
            .sent_tx
            .iter()
            .filter(|(nonce, _burn_index, _signed_txs)| *nonce < &first_non_finalized_tx_nonce)
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1155-1173)
```rust
            let request_max_fee_per_gas = request
                .max_transaction_fee
                .into_wei_per_gas(gas_limit)
                .expect("BUG: gas_limit should be non-zero");
            let actual_min_max_fee_per_gas = gas_fee_estimate.min_max_fee_per_gas();
            if actual_min_max_fee_per_gas > request_max_fee_per_gas {
                return Err(CreateTransactionError::InsufficientTransactionFee {
                    cketh_ledger_burn_index: request.cketh_ledger_burn_index,
                    allowed_max_transaction_fee: request.max_transaction_fee,
                    actual_max_transaction_fee: actual_min_max_fee_per_gas
                        .transaction_cost(gas_limit)
                        .unwrap_or(Wei::MAX),
                });
            }
            Ok(Eip1559TransactionRequest {
                chain_id: ethereum_network.chain_id(),
                nonce,
                max_priority_fee_per_gas: gas_fee_estimate.max_priority_fee_per_gas,
                max_fee_per_gas: request_max_fee_per_gas,
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L136-144)
```rust
impl ResubmissionStrategy {
    pub fn allowed_max_transaction_fee(&self) -> Wei {
        match self {
            ResubmissionStrategy::ReduceEthAmount { withdrawal_amount } => *withdrawal_amount,
            ResubmissionStrategy::GuaranteeEthAmount {
                allowed_max_transaction_fee,
            } => *allowed_max_transaction_fee,
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L169-173)
```rust
        if new_tx_price.max_transaction_fee() > self.resubmission.allowed_max_transaction_fee() {
            return Err(ResubmitTransactionError::InsufficientTransactionFee {
                allowed_max_transaction_fee: self.resubmission.allowed_max_transaction_fee(),
                actual_max_transaction_fee: new_tx_price.max_transaction_fee(),
            });
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L242-245)
```rust
            Err(e) => {
                log!(INFO, "Failed to resubmit transaction: {e:?}");
            }
        }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L281-290)
```rust
            Err(CreateTransactionError::InsufficientTransactionFee {
                cketh_ledger_burn_index: ledger_burn_index,
                allowed_max_transaction_fee: withdrawal_amount,
                actual_max_transaction_fee: max_transaction_fee,
            }) => {
                log!(
                    INFO,
                    "[create_transactions_batch]: Withdrawal request with burn index {ledger_burn_index} has insufficient amount {withdrawal_amount:?} to cover transaction fees: {max_transaction_fee:?}. Request moved back to end of queue."
                );
                mutate_state(|s| s.eth_transactions.reschedule_withdrawal_request(request));
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L1014-1030)
```rust
            let too_high_price = GasFeeEstimate {
                base_fee_per_gas: DEFAULT_CKERC20_MAX_FEE_PER_GAS,
                max_priority_fee_per_gas: WeiPerGas::ONE,
            };
            let resubmitted_txs = transactions.create_resubmit_transactions(
                TransactionCount::from(30_u8),
                too_high_price.clone(),
            );
            assert_eq!(
                resubmitted_txs,
                vec![Err(ResubmitTransactionError::InsufficientTransactionFee {
                    ledger_burn_index: 93_u64.into(),
                    transaction_nonce: 30_u8.into(),
                    allowed_max_transaction_fee: DEFAULT_MAX_TRANSACTION_FEE.into(),
                    max_transaction_fee: 30_000_000_000_165_000_u128.into(),
                })]
            );
```
