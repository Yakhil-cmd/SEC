### Title
ckETH/ckERC20 Minter Burns User Tokens Without Reimbursement When Gas Fees Permanently Exceed Withdrawal Amount - (`File: rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

In the ckETH minter, when `create_transactions_batch` encounters a `CreateTransactionError::InsufficientTransactionFee` for a `WithdrawalRequest::CkEth` request, it silently re-queues the request via `reschedule_withdrawal_request` without ever reimbursing the user's already-burned ckETH. If Ethereum gas fees remain persistently above the user's withdrawal amount, the request loops in the queue indefinitely and the burned ckETH is never returned. This is the IC analog of the external report's pattern: a downstream operation silently fails/cancels, but the upstream caller (the minter) does not return the already-committed tokens to the user.

---

### Finding Description

The ckETH withdrawal flow is:

1. User calls `withdraw_eth` → minter burns `withdrawal_amount` ckETH from the user's ledger account.
2. The burn is irreversible and recorded on-chain.
3. The minter enqueues the `EthWithdrawalRequest` in `pending_withdrawal_requests`.
4. A timer periodically calls `process_retrieve_eth_requests` → `create_transactions_batch`.
5. Inside `create_transactions_batch`, `create_transaction` is called. For `WithdrawalRequest::CkEth`, if `withdrawal_amount < max_transaction_fee`, it returns `Err(CreateTransactionError::InsufficientTransactionFee)`.
6. The error handler at line 281–291 of `rs/ethereum/cketh/minter/src/withdraw.rs` calls `reschedule_withdrawal_request(request)` — moving the request to the back of the queue — **without scheduling any reimbursement**.

The user's ckETH has already been burned (step 2). The request will be retried on every timer tick. If gas fees never drop below the withdrawal amount, the tokens are permanently locked in limbo: burned but never sent to Ethereum, and never reimbursed.

Contrast this with the `withdraw_erc20` path: when the ckERC20 burn fails, the minter explicitly schedules a `FailedErc20WithdrawalRequest` reimbursement event (lines 506–531 of `rs/ethereum/cketh/minter/src/main.rs`). No equivalent reimbursement path exists for the `InsufficientTransactionFee` case in `create_transactions_batch`.

The `ckETH_minimum_withdrawal_amount` check in `withdraw_eth` (line 292 of `rs/ethereum/cketh/minter/src/main.rs`) is intended to prevent this, but it is set at deployment time and does not adapt to real-time gas spikes. During extreme Ethereum congestion, gas fees can transiently exceed the minimum withdrawal amount, causing the condition to trigger for legitimately accepted requests.

---

### Impact Explanation

**Ledger conservation bug / chain-fusion burn-without-send bug.**

- A user's ckETH is burned on the IC ledger (permanently reducing their balance).
- No ETH is ever sent on Ethereum.
- No ckETH reimbursement is ever minted back.
- The user suffers a permanent, unrecoverable loss of funds proportional to their withdrawal amount.
- The minter's internal ETH balance accounting (`eth_balance`) is also inconsistent: the burned ckETH is not reflected as a debit because no transaction was ever finalized.

---

### Likelihood Explanation

- **Trigger condition**: Ethereum gas fees must exceed the user's `withdrawal_amount` at the time `create_transactions_batch` runs. This is plausible during extreme gas spikes (e.g., NFT mints, network congestion events).
- **Attacker-controlled entry path**: Any unprivileged IC principal can call `withdraw_eth` with an amount just above `cketh_minimum_withdrawal_amount`. If gas spikes after the burn but before transaction creation, the condition fires. No privileged access is required.
- **Persistence**: The `cketh_minimum_withdrawal_amount` is a static configuration value. It is not dynamically adjusted to track real-time gas. The minter's gas fee estimate (`lazy_refresh_gas_fee_estimate`) is fetched from Ethereum JSON-RPC providers and can legitimately return values exceeding the minimum.
- **Likelihood**: Medium. Requires a gas spike, but Ethereum gas spikes are a well-known, recurring phenomenon. The minimum withdrawal amount provides a buffer, but not an absolute guarantee.

---

### Recommendation

1. **Immediate**: When `create_transactions_batch` encounters `CreateTransactionError::InsufficientTransactionFee` for a `WithdrawalRequest::CkEth` request, instead of unconditionally rescheduling, implement a retry counter or a maximum queue age. Once the threshold is exceeded, schedule a reimbursement (analogous to `EventType::FailedErc20WithdrawalRequest`) and remove the request from the queue.

2. **Structural**: Mirror the `withdraw_erc20` reimbursement pattern: emit a `FailedEthWithdrawalRequest` event that triggers `process_reimbursement` to mint back the burned ckETH minus any applicable penalty fee.

3. **Preventive**: Dynamically adjust `cketh_minimum_withdrawal_amount` based on the current gas fee estimate so that `withdraw_eth` rejects requests that cannot cover fees at acceptance time, preventing the burn from occurring in the first place.

---

### Proof of Concept

**Step 1**: Alice calls `withdraw_eth` with `amount = cketh_minimum_withdrawal_amount` (e.g., `30_000_000_000_000_000` wei). The minter burns this amount from Alice's ckETH ledger account. [1](#0-0) 

**Step 2**: The burn succeeds. The `EthWithdrawalRequest` is enqueued in `pending_withdrawal_requests`. [2](#0-1) 

**Step 3**: A gas spike occurs. The timer fires `process_retrieve_eth_requests` → `create_transactions_batch`. `create_transaction` is called for Alice's request. Since `withdrawal_amount (30_000_000_000_000_000) < max_transaction_fee` (now elevated due to the spike), it returns `Err(CreateTransactionError::InsufficientTransactionFee)`. [3](#0-2) 

**Step 4**: The error handler in `create_transactions_batch` calls `reschedule_withdrawal_request(request)`. No reimbursement is scheduled. Alice's burned ckETH is not returned. [4](#0-3) 

**Step 5**: The gas spike persists. Every subsequent timer tick repeats Step 3–4. Alice's ckETH remains burned with no path to recovery. The `process_reimbursement` function is never invoked for this request because no `ReimbursementRequest` was ever recorded. [5](#0-4) 

**Contrast**: For `withdraw_erc20`, when the ckERC20 burn fails, the minter explicitly schedules reimbursement of the already-burned ckETH fee. No equivalent path exists for the ckETH withdrawal `InsufficientTransactionFee` case. [6](#0-5)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L291-312)
```rust
    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }

    let client = read_state(LedgerClient::cketh_ledger_from_state);
    let now = ic_cdk::api::time();
    log!(INFO, "[withdraw]: burning {:?}", amount);
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L314-336)
```rust
        Ok(ledger_burn_index) => {
            let withdrawal_request = EthWithdrawalRequest {
                withdrawal_amount: amount,
                destination,
                ledger_burn_index,
                from: caller,
                from_subaccount: from_subaccount.and_then(LedgerSubaccount::from_bytes),
                created_at: Some(now),
            };

            log!(
                INFO,
                "[withdraw]: queuing withdrawal request {:?}",
                withdrawal_request,
            );

            mutate_state(|s| {
                process_event(
                    s,
                    EventType::AcceptedEthWithdrawalRequest(withdrawal_request.clone()),
                );
            });
            Ok(RetrieveEthRequest::from(withdrawal_request))
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L506-531)
```rust
                Err(ckerc20_burn_error) => {
                    let reimbursed_amount = match &ckerc20_burn_error {
                        LedgerBurnError::TemporarilyUnavailable { .. } => erc20_tx_fee, //don't penalize user in case of an error outside of their control
                        LedgerBurnError::InsufficientFunds { .. }
                        | LedgerBurnError::AmountTooLow { .. }
                        | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
                            .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
                            .unwrap_or(Wei::ZERO),
                    };
                    if reimbursed_amount > Wei::ZERO {
                        let reimbursement_request = ReimbursementRequest {
                            ledger_burn_index: cketh_ledger_burn_index,
                            reimbursed_amount: reimbursed_amount.change_units(),
                            to: cketh_account.owner,
                            to_subaccount: cketh_account
                                .subaccount
                                .and_then(LedgerSubaccount::from_bytes),
                            transaction_hash: None,
                        };
                        mutate_state(|s| {
                            process_event(
                                s,
                                EventType::FailedErc20WithdrawalRequest(reimbursement_request),
                            );
                        });
                    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1122-1134)
```rust
        WithdrawalRequest::CkEth(request) => {
            let transaction_price = gas_fee_estimate.to_price(gas_limit);
            let max_transaction_fee = transaction_price.max_transaction_fee();
            let tx_amount = match request.withdrawal_amount.checked_sub(max_transaction_fee) {
                Some(tx_amount) => tx_amount,
                None => {
                    return Err(CreateTransactionError::InsufficientTransactionFee {
                        cketh_ledger_burn_index: request.ledger_burn_index,
                        allowed_max_transaction_fee: request.withdrawal_amount,
                        actual_max_transaction_fee: max_transaction_fee,
                    });
                }
            };
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L46-95)
```rust
pub async fn process_reimbursement() {
    let _guard = match TimerGuard::new(TaskType::Reimbursement) {
        Ok(guard) => guard,
        Err(e) => {
            log!(DEBUG, "Failed retrieving reimbursement guard: {e:?}",);
            return;
        }
    };

    let reimbursements: Vec<(ReimbursementIndex, ReimbursementRequest)> = read_state(|s| {
        s.eth_transactions
            .reimbursement_requests_iter()
            .map(|(index, request)| (index.clone(), request.clone()))
            .collect()
    });
    if reimbursements.is_empty() {
        return;
    }

    let mut error_count = 0;

    for (index, reimbursement_request) in reimbursements {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this reimbursement request will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(index.clone(), |index| {
            mutate_state(|s| process_event(s, EventType::QuarantinedReimbursement { index }));
        });
        let ledger_canister_id = match index {
            ReimbursementIndex::CkEth { .. } => read_state(|s| s.cketh_ledger_id),
            ReimbursementIndex::CkErc20 { ledger_id, .. } => ledger_id,
        };
        let client = ICRC1Client {
            runtime: CdkRuntime,
            ledger_canister_id,
        };
        let memo = Memo::from(reimbursement_request.clone());
        let args = TransferArg {
            from_subaccount: None,
            to: Account {
                owner: reimbursement_request.to,
                subaccount: reimbursement_request
                    .to_subaccount
                    .map(LedgerSubaccount::to_bytes),
            },
            fee: None,
            created_at_time: None,
            memo: Some(memo),
            amount: Nat::from(reimbursement_request.reimbursed_amount),
        };
        let block_index = match client.transfer(args).await {
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
