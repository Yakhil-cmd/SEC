### Title
ckETH/ckERC20 Withdrawal Requests with Insufficient Fee Permanently Stuck in Pending Queue Without Reimbursement - (`rs/ethereum/cketh/minter/src/withdraw.rs`)

### Summary

The ckETH minter canister permanently traps both ckETH and ckERC20 withdrawal requests in the `pending_withdrawal_requests` queue when the Ethereum gas fee rises above the user-specified fee budget. The minter loops these requests back to the end of the queue indefinitely via `reschedule_withdrawal_request`, with no timeout, no cancellation path, and no reimbursement. For ckERC20 withdrawals, the user's ckETH gas-fee tokens are already burned and locked at the time of the request; they are never returned while the request remains pending. This is a direct analog of the Holograph H-02 vulnerability class: a user-controlled fee parameter locks a resource (the minter's withdrawal queue and the user's burned ckETH) with no escape path.

### Finding Description

**ckETH withdrawal path (`WithdrawalRequest::CkEth`):**

In `withdraw_eth`, the user's entire `withdrawal_amount` is burned upfront on the ckETH ledger before any gas estimate is checked. [1](#0-0) 

Later, in `create_transactions_batch`, `create_transaction` computes the current gas fee and checks whether `withdrawal_amount >= max_transaction_fee`. If the gas fee has risen above the withdrawal amount, it returns `CreateTransactionError::InsufficientTransactionFee`. The handler does not cancel or reimburse — it calls `reschedule_withdrawal_request`, pushing the request to the back of the queue: [2](#0-1) 

This loop repeats on every timer tick indefinitely. There is no maximum retry count, no expiry, and no reimbursement triggered for the ckETH case when the request is merely rescheduled (as opposed to the `FailedErc20WithdrawalRequest` path which only fires during `withdraw_erc20` call-time failures).

**ckERC20 withdrawal path (`WithdrawalRequest::CkErc20`):**

For ckERC20, the user's ckETH gas-fee tokens (`erc20_tx_fee`) are burned at call time in `withdraw_erc20` before the request is enqueued: [3](#0-2) 

The `max_transaction_fee` field of `Erc20WithdrawalRequest` is fixed at the value burned at call time: [4](#0-3) 

In `create_transaction` for the ckERC20 case, if the current `actual_min_max_fee_per_gas > request_max_fee_per_gas`, the function returns `InsufficientTransactionFee`: [5](#0-4) 

The caller in `create_transactions_batch` again only reschedules — no reimbursement of the already-burned ckETH is triggered: [2](#0-1) 

The documentation explicitly states "Overcharged transaction fees are not reimbursed": [6](#0-5) 

The `ResubmitTransactionError::InsufficientTransactionFee` path (for already-sent transactions) similarly only logs and does not cancel or reimburse: [7](#0-6) 

The `reschedule_withdrawal_request` function simply moves the request to the back of the FIFO queue with no state change: [8](#0-7) 

### Impact Explanation

**For ckETH withdrawals:** A user who submits a `withdraw_eth` call with an amount just above the minimum (e.g., `5_000_000_000_000_000` wei after the recent reduction) during a low-fee period will have their ckETH burned. If Ethereum fees spike, the request loops in the queue forever. The user's ckETH is permanently burned with no ETH sent and no reimbursement. The minter's withdrawal queue grows unboundedly with unprocessable requests, degrading throughput for all users.

**For ckERC20 withdrawals:** The user's ckETH gas-fee tokens are burned at call time. If fees rise above `max_transaction_fee`, the ckERC20 withdrawal request is rescheduled indefinitely. The user loses their burned ckETH (no reimbursement path exists for the pending-queue reschedule case) and their ckERC20 tokens remain locked in the minter's pending queue. The minter queue accumulates permanently stuck requests.

**Queue pollution / DoS:** An attacker can deliberately submit many ckETH or ckERC20 withdrawal requests with amounts just above the minimum during low-fee periods, then wait for fees to spike. Each stuck request consumes queue space and is re-evaluated on every timer tick, wasting minter cycles and delaying legitimate withdrawals.

### Likelihood Explanation

Ethereum gas fees are highly volatile. Historical spikes of 10–100× within hours are well-documented. The minimum ckETH withdrawal amount was recently reduced to `5_000_000_000_000_000` wei (~$10), which provides only a ~10× safety margin. A moderate gas spike can push the required fee above a near-minimum withdrawal amount. Any unprivileged user can trigger this by calling `withdraw_eth` or `withdraw_erc20` with a near-minimum amount. No special access is required. [9](#0-8) 

### Recommendation

1. **Add a maximum retry count or expiry timestamp** to `WithdrawalRequest`. After N failed attempts or after a configurable deadline, trigger a reimbursement of the burned tokens (ckETH for ckETH withdrawals; ckETH gas fee + ckERC20 tokens for ckERC20 withdrawals) instead of rescheduling.

2. **For ckERC20 requests specifically**, when `InsufficientTransactionFee` is returned during `create_transactions_batch`, immediately schedule a `FailedErc20WithdrawalRequest` reimbursement (mirroring the existing call-time failure path in `withdraw_erc20`) rather than rescheduling.

3. **Enforce a minimum withdrawal amount** that accounts for realistic worst-case gas spikes (e.g., 100× the current estimate) rather than just the current estimate with a 10× margin.

### Proof of Concept

1. Observe current Ethereum `base_fee_per_gas` is low (e.g., 1 gwei). The minter's `eip_1559_transaction_price` query returns a low `max_fee_per_gas`.
2. Call `withdraw_eth` with `amount = minimum_withdrawal_amount` (e.g., `5_000_000_000_000_000` wei). The ckETH is burned immediately. [10](#0-9) 
3. Ethereum fees spike 20× (historically common during NFT mints, network congestion events).
4. On the next timer tick, `create_transactions_batch` calls `create_transaction`. The check `withdrawal_amount.checked_sub(max_transaction_fee)` returns `None` because `max_transaction_fee > withdrawal_amount`. [11](#0-10) 
5. The error arm calls `reschedule_withdrawal_request`. No reimbursement is issued. The request goes to the back of the queue. [2](#0-1) 
6. Steps 4–5 repeat on every timer tick indefinitely. The user's ckETH is permanently lost with no ETH delivered and no refund.
7. For ckERC20: repeat with `withdraw_erc20`. The ckETH gas fee is burned at step 2. The ckERC20 tokens are also burned. Both remain locked in the minter with no reimbursement path while the request cycles in the pending queue. [12](#0-11)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L291-296)
```rust
    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L301-312)
```rust
    match client
        .burn_from(
            Account {
                owner: caller,
                subaccount: from_subaccount,
            },
            amount,
            BurnMemo::Convert {
                to_address: destination,
            },
        )
        .await
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L448-481)
```rust
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
    {
        Ok(cketh_ledger_burn_index) => {
            log!(
                INFO,
                "[withdraw_erc20]: burning {} {} from account {}",
                ckerc20_withdrawal_amount,
                ckerc20_token.ckerc20_token_symbol,
                ckerc20_account
            );
            match LedgerClient::ckerc20_ledger(&ckerc20_token)
                .burn_from(
                    ckerc20_account,
                    ckerc20_withdrawal_amount,
                    BurnMemo::Erc20Convert {
                        ckerc20_withdrawal_id: cketh_ledger_burn_index.get(),
                        to_address: destination,
                    },
                )
                .await
            {
                Ok(ckerc20_ledger_burn_index) => {
                    let withdrawal_request = Erc20WithdrawalRequest {
                        max_transaction_fee: erc20_tx_fee,
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L242-246)
```rust
            Err(e) => {
                log!(INFO, "Failed to resubmit transaction: {e:?}");
            }
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L281-291)
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
            }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L146-149)
```rust
pub struct Erc20WithdrawalRequest {
    /// Amount of burn ckETH that can be used to pay for the Ethereum transaction fees.
    #[n(0)]
    pub max_transaction_fee: Wei,
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L469-483)
```rust
    /// Move an existing withdrawal request to the back of the queue.
    pub fn reschedule_withdrawal_request<R: Into<WithdrawalRequest>>(&mut self, request: R) {
        let request = request.into();
        assert_eq!(
            self.pending_withdrawal_requests
                .iter()
                .filter(|r| r.cketh_ledger_burn_index() == request.cketh_ledger_burn_index())
                .count(),
            1,
            "BUG: expected exactly one withdrawal request with ckETH ledger burn index {}",
            request.cketh_ledger_burn_index()
        );
        self.remove_withdrawal_request(&request);
        self.record_withdrawal_request(request);
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1125-1133)
```rust
            let tx_amount = match request.withdrawal_amount.checked_sub(max_transaction_fee) {
                Some(tx_amount) => tx_amount,
                None => {
                    return Err(CreateTransactionError::InsufficientTransactionFee {
                        cketh_ledger_burn_index: request.ledger_burn_index,
                        allowed_max_transaction_fee: request.withdrawal_amount,
                        actual_max_transaction_fee: max_transaction_fee,
                    });
                }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1159-1168)
```rust
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
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2026_05_29.md (L21-24)
```markdown
* Reduce the minimum ETH withdrawal amount by a factor of 6, from 0.03 ETH (`30_000_000_000_000_000` wei) to 0.005 ETH (`5_000_000_000_000_000` wei) — approximately $10 at current prices. The reasoning is as follows:
    * The current minimum dates back to December 2023, when the ckETH minter was installed (see proposal [126171](https://dashboard.internetcomputer.org/proposal/126171)). At that time ETH traded in a similar USD range (around $2000), but Ethereum mainnet transaction fees were averaging $5–$10 per transaction ([source](https://bitinfocharts.com/comparison/ethereum-transactionfees.html#3y)).
    * Today, Ethereum mainnet fees are in the order of cents and rarely exceed $1.
    * As explained [here](https://github.com/dfinity/ic/blob/14382b5abb14b8e7de2bd4a3fb402ba069b82861/rs/ethereum/cketh/docs/cketh.adoc?plain=1#L208), an order-of-magnitude safety margin is preserved so the minter can always submit the transaction even when the Ethereum network is congested and one or more resubmissions are needed (each resubmission requires at least a 10% fee bump). With current Ethereum fees of ~$0.10–$1, a $10 minimum still preserves the ~10× safety margin even after several fee bumps.
```
