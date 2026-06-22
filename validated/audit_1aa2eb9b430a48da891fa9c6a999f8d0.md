### Title
ckERC20 Withdrawal: User Burns ckETH Gas Fee Without Receiving ERC20 Tokens When Ethereum Transaction Succeeds But Overcharged Fee Is Not Reimbursed - (File: rs/ethereum/cketh/minter/src/main.rs, rs/ethereum/cketh/minter/src/tx.rs)

### Summary

The ckETH minter's `withdraw_erc20` flow burns the user's ckETH upfront as a gas fee estimate (`max_transaction_fee`), then burns the user's ckERC20 tokens. The gas fee estimate uses a cached value up to 60 seconds old (`MAX_AGE_NS = 60_000_000_000`). By design and explicit documentation, **overcharged transaction fees are never reimbursed** for ckERC20 withdrawals. This is an accepted, documented asymmetry. However, the analogous vulnerability from the external report — a user burning tokens and receiving less value than expected due to a stale or manipulated price — maps directly to the ckERC20 withdrawal flow: the `erc20_tx_fee` burned from the user's ckETH balance is computed from a potentially stale gas estimate, and the difference between `max_transaction_fee` and `actual_tx_fee` is permanently lost to the user with no recourse.

### Finding Description

In `withdraw_erc20` in `rs/ethereum/cketh/minter/src/main.rs`, the minter calls `estimate_erc20_transaction_fee()` which internally calls `lazy_refresh_gas_fee_estimate()`. This function returns a **cached** gas fee estimate if it is less than 60 seconds old, without fetching a fresh one:

```rust
// rs/ethereum/cketh/minter/src/tx.rs
const MAX_AGE_NS: u64 = 60_000_000_000_u64; //60 seconds
...
match read_state(|s| s.last_transaction_price_estimate.clone()) {
    Some((last_estimate_timestamp_ns, estimate))
        if now_ns < last_estimate_timestamp_ns.saturating_add(MAX_AGE_NS) =>
    {
        Some(estimate)  // returns stale cached estimate
    }
    _ => do_refresh().await,
}
```

The fee estimate formula is `max_fee_per_gas = 2 * base_fee_per_gas + max_priority_fee_per_gas`, a deliberate 2x overestimate to cover resubmissions. The user's ckETH is burned for this full `max_transaction_fee` amount:

```rust
// rs/ethereum/cketh/minter/src/main.rs:448-458
match cketh_ledger
    .burn_from(
        cketh_account,
        erc20_tx_fee,   // full max_transaction_fee burned upfront
        BurnMemo::Erc20GasFee { ... },
    )
    .await
```

The `Erc20WithdrawalRequest` records `max_transaction_fee: erc20_tx_fee` as the ceiling. When the Ethereum transaction finalizes successfully, the minter computes `unspent_tx_fee = max_transaction_fee - actual_tx_fee` and tracks it as a protocol metric — but **does not reimburse it to the user**. The documentation explicitly states: *"Overcharged transaction fees are not reimbursed."*

This means every successful ckERC20 withdrawal results in the user burning more ckETH than the actual Ethereum gas cost, with the surplus permanently retained by the minter's ETH balance. The `update_balance_upon_withdrawal` function in `rs/ethereum/cketh/minter/src/state.rs` confirms this: for `CkErc20` withdrawals, `charged_tx_fee = req.max_transaction_fee` (the full upfront burn), and `unspent_tx_fee` is added to `total_unspent_tx_fees` — a minter-side accounting entry, not a user reimbursement.

The stale cache window (up to 60 seconds) compounds this: if Ethereum gas prices drop significantly within that window, users calling `withdraw_erc20` will burn ckETH at the old (higher) estimate, losing more than necessary with no refund path.

### Impact Explanation

Every user who successfully completes a ckERC20 withdrawal permanently loses the difference between `max_transaction_fee` (burned upfront) and `actual_tx_fee` (what Ethereum actually charged). Given the formula `max_fee_per_gas = 2 * base_fee_per_gas + max_priority_fee_per_gas`, the overcharge is structurally guaranteed on every successful withdrawal. The `total_unspent_tx_fees` metric in the minter dashboard confirms this is a known, ongoing value extraction from users. This is a **ledger conservation bug / chain-fusion burn/value-loss bug**: ckETH tokens are burned from users in excess of the actual cost, and the surplus is never returned.

### Likelihood Explanation

This affects **every** successful ckERC20 withdrawal — it is not a rare edge case. Any unprivileged user calling `withdraw_erc20` on the ckETH minter canister triggers this path. The 60-second cache window means the overcharge can be amplified during periods of falling gas prices. The entry path is a standard ingress call to a public update endpoint.

### Recommendation

Reimburse the `unspent_tx_fee` (i.e., `max_transaction_fee - actual_tx_fee`) to the user's ckETH account upon successful finalization of a ckERC20 Ethereum transaction, analogous to how ckETH withdrawals reimburse unused fees when the transaction fails. The `record_finalized_transaction` logic in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs` should be extended to schedule a ckETH mint-back for the unspent portion on success, not just on failure.

### Proof of Concept

1. User approves minter for 1 ETH ckETH and calls `withdraw_erc20` for 1000 ckUSDC.
2. `estimate_erc20_transaction_fee()` returns a cached estimate from 55 seconds ago when gas was high: `max_transaction_fee = 65_000 * 86_815_552_328 = ~5.6M gwei`.
3. Minter burns `5.6M gwei` of ckETH from user via `burn_from`.
4. Minter burns 1000 ckUSDC from user.
5. Ethereum transaction is submitted and mined with `actual_tx_fee = 65_000 * 42_828_524_488 = ~2.8M gwei`.
6. `unspent_tx_fee = 5.6M - 2.8M = ~2.8M gwei` is added to `total_unspent_tx_fees` in minter state.
7. User receives 1000 USDC on Ethereum but permanently loses ~2.8M gwei of ckETH with no reimbursement path.

The explicit documentation at `rs/ethereum/cketh/docs/ckerc20.adoc:275` confirms this is by design: *"Overcharged transaction fees are not reimbursed."* This design choice constitutes a systematic value extraction from every ckERC20 withdrawal user.

---

**Key code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/src/tx.rs (L610-681)
```rust
pub async fn lazy_refresh_gas_fee_estimate() -> Option<GasFeeEstimate> {
    const MAX_AGE_NS: u64 = 60_000_000_000_u64; //60 seconds

    async fn do_refresh() -> Option<GasFeeEstimate> {
        let _guard = match TimerGuard::new(TaskType::RefreshGasFeeEstimate) {
            Ok(guard) => guard,
            Err(e) => {
                log!(
                    DEBUG,
                    "[refresh_gas_fee_estimate]: Failed retrieving guard: {e:?}",
                );
                return None;
            }
        };

        let fee_history = match eth_fee_history().await {
            Ok(fee_history) => fee_history,
            Err(e) => {
                log!(
                    INFO,
                    "[refresh_gas_fee_estimate]: Failed retrieving fee history: {e:?}",
                );
                return None;
            }
        };

        let gas_fee_estimate = match estimate_transaction_fee(&fee_history) {
            Ok(estimate) => {
                mutate_state(|s| {
                    s.last_transaction_price_estimate =
                        Some((ic_cdk::api::time(), estimate.clone()));
                });
                estimate
            }
            Err(e) => {
                log!(
                    INFO,
                    "[refresh_gas_fee_estimate]: Failed estimating gas fee: {e:?}",
                );
                return None;
            }
        };
        log!(
            INFO,
            "[refresh_gas_fee_estimate]: Estimated transaction fee: {:?}",
            gas_fee_estimate,
        );
        Some(gas_fee_estimate)
    }

    async fn eth_fee_history() -> Result<FeeHistory, MultiCallError<FeeHistory>> {
        read_state(rpc_client)
            .fee_history((5_u8, BlockTag::Latest))
            .with_reward_percentiles(vec![20])
            .with_cycles(MIN_ATTACHED_CYCLES)
            .try_send()
            .await
            .reduce_with_strategy(StrictMajorityByKey::new(|fee_history: &FeeHistory| {
                Nat::from(fee_history.oldest_block.clone())
            }))
    }

    let now_ns = ic_cdk::api::time();
    match read_state(|s| s.last_transaction_price_estimate.clone()) {
        Some((last_estimate_timestamp_ns, estimate))
            if now_ns < last_estimate_timestamp_ns.saturating_add(MAX_AGE_NS) =>
        {
            Some(estimate)
        }
        _ => do_refresh().await,
    }
}
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-458)
```rust
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
    let cketh_account = Account {
        owner: caller,
        subaccount: from_cketh_subaccount,
    };
    let ckerc20_account = Account {
        owner: caller,
        subaccount: from_ckerc20_subaccount,
    };
    let now = ic_cdk::api::time();
    log!(
        INFO,
        "[withdraw_erc20]: burning {:?} ckETH from account {}",
        erc20_tx_fee,
        cketh_account
    );
    match cketh_ledger
        .burn_from(
            cketh_account,
            erc20_tx_fee,
            BurnMemo::Erc20GasFee {
                ckerc20_token_symbol: ckerc20_token.ckerc20_token_symbol.clone(),
                ckerc20_withdrawal_amount,
                to_address: destination,
            },
        )
        .await
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L545-553)
```rust
async fn estimate_erc20_transaction_fee() -> Option<Wei> {
    lazy_refresh_gas_fee_estimate()
        .await
        .map(|gas_fee_estimate| {
            gas_fee_estimate
                .to_price(CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT)
                .max_transaction_fee()
        })
}
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L355-375)
```rust
        let charged_tx_fee = match withdrawal_request {
            WithdrawalRequest::CkEth(req) => req
                .withdrawal_amount
                .checked_sub(tx.transaction().amount)
                .expect("BUG: withdrawal amount MUST always be at least the transaction amount"),
            WithdrawalRequest::CkErc20(req) => req.max_transaction_fee,
        };
        let unspent_tx_fee = charged_tx_fee.checked_sub(tx_fee).expect(
            "BUG: charged transaction fee MUST always be at least the effective transaction fee",
        );
        let debited_amount = match receipt.status {
            TransactionStatus::Success => tx
                .transaction()
                .amount
                .checked_add(tx_fee)
                .expect("BUG: debited amount always fits into U256"),
            TransactionStatus::Failure => tx_fee,
        };
        self.eth_balance.eth_balance_sub(debited_amount);
        self.eth_balance.total_effective_tx_fees_add(tx_fee);
        self.eth_balance.total_unspent_tx_fees_add(unspent_tx_fee);
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-275)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
. The minter attempts to burn the specified token amount from the user account on the ckERC20 ledger. If the burn succeeds, the minter schedules a withdrawal task. If the burn fails (e.g., insufficient funds), the minter schedules the reimbursement of the burnt ckETH amount from the previous step minus some (small) penalty fee.
. The ckETH minter constructs a 0-ETH amount transaction containing the ERC-20 withdrawal (in `data` field) to the Ethereum network.
. The user can query the withdrawal status using the identifier from the erc20_withdraw response.
. Once the transaction gets enough confirmations, the minter considers the transaction finalized.
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```
