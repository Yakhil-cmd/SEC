### Title
ckERC20 Withdrawal Permanently Stuck When Gas Fees Spike After Request Acceptance — (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter's `withdraw_erc20` endpoint burns a fixed `max_transaction_fee` of ckETH at request-acceptance time, based on the gas fee estimate at that instant. If Ethereum gas fees spike significantly before the minter's timer processes the queued request, `create_transaction` returns `CreateTransactionError::InsufficientTransactionFee` and the request is indefinitely rescheduled with no automatic reimbursement path. Both the user's ckETH (gas fee) and ckERC20 (withdrawal amount) remain permanently burned with no on-chain escape hatch.

---

### Finding Description

**Step 1 — Fee locked at request time.**

When a user calls `withdraw_erc20`, the minter immediately calls `estimate_erc20_transaction_fee()` to obtain the current gas fee estimate and burns that amount of ckETH from the user's account. This burned amount is stored as `Erc20WithdrawalRequest.max_transaction_fee` and is immutable for the lifetime of the request. [1](#0-0) [2](#0-1) 

**Step 2 — Request queued, execution deferred.**

After both burns succeed, the `Erc20WithdrawalRequest` is enqueued in `pending_withdrawal_requests`. Actual Ethereum transaction creation happens asynchronously on a periodic timer via `process_retrieve_eth_requests()`. [3](#0-2) 

**Step 3 — Gas fee re-estimated at execution time.**

When the timer fires, `lazy_refresh_gas_fee_estimate()` fetches the *current* Ethereum gas fee (up to 60 seconds stale). This fresh estimate is passed to `create_transactions_batch`, which calls `create_transaction` for each pending request. [4](#0-3) 

**Step 4 — Mismatch causes indefinite rescheduling.**

Inside `create_transaction`, for `WithdrawalRequest::CkErc20`, the code checks whether `actual_min_max_fee_per_gas > request_max_fee_per_gas`. If gas fees have spiked beyond the user's locked `max_transaction_fee`, the function returns `Err(CreateTransactionError::InsufficientTransactionFee)`. [5](#0-4) 

Back in `create_transactions_batch`, this error path calls only `reschedule_withdrawal_request` — moving the request to the back of the queue — with no reimbursement triggered: [6](#0-5) 

**Step 5 — No reimbursement path exists for stuck pending requests.**

The `maybe_reimburse` set (which drives the reimbursement flow) is only populated inside `record_created_transaction`, which is only reached when `create_transaction` *succeeds*. A request that never passes `create_transaction` never enters the reimbursement pipeline. [7](#0-6) 

The same gap exists for the post-creation resubmission path: if a sent ckERC20 transaction cannot be resubmitted because the new fee exceeds `allowed_max_transaction_fee`, the error is only logged: [8](#0-7) 

---

### Impact Explanation

A user who calls `withdraw_erc20` during a period of low Ethereum gas fees, but whose request is not processed before a significant gas fee spike, will have:

- Their ckETH (gas fee) permanently burned with no reimbursement.
- Their ckERC20 tokens (withdrawal amount) permanently burned with no reimbursement.
- The withdrawal request cycling indefinitely in `pending_withdrawal_requests` until gas fees fall back below the locked `max_transaction_fee`.

If gas fees remain elevated for an extended period (as occurred during major Ethereum network events), the user's funds are effectively frozen with no protocol-level escape hatch. There is no `cancel_withdrawal` endpoint for ckERC20 requests. The `max_transaction_fee` is fixed at burn time and cannot be topped up.

This is a direct ledger conservation bug: ckETH and ckERC20 tokens are burned (supply reduced) but the corresponding ERC-20 transfer never executes, and no reimbursement mint occurs.

---

### Likelihood Explanation

Ethereum gas fees are highly volatile. Spikes of 5–20× within minutes are historically common during NFT mints, protocol launches, or network congestion events. The minter's gas fee estimate uses a 60-second cache (`MAX_AGE_NS = 60_000_000_000`) and a 2× base-fee multiplier, which provides only a short-term buffer. [9](#0-8) 

Any unprivileged user can trigger this condition by submitting a `withdraw_erc20` call during a low-fee window. The minter's processing queue introduces a natural delay between request acceptance and transaction creation, widening the window for fee divergence. The condition is reachable via normal ingress to the public `withdraw_erc20` endpoint. [10](#0-9) 

---

### Recommendation

1. **Add a reimbursement path for indefinitely stuck pending requests.** When `InsufficientTransactionFee` is returned in `create_transactions_batch`, instead of only rescheduling, track a retry count per request. After N consecutive failures (or after a configurable timeout relative to `created_at`), trigger the existing reimbursement flow to return both the burned ckETH and ckERC20 to the user.

2. **Add a user-callable cancellation endpoint for pending ckERC20 withdrawals.** Allow the original requester to cancel a withdrawal that has not yet had a transaction created, triggering reimbursement of both burned amounts minus a small penalty fee (mirroring the existing ckERC20 burn-failure reimbursement logic).

3. **Estimate gas fee with a wider safety margin or cap the accepted request only when the minter has sufficient headroom.** Alternatively, defer the ckETH burn until the minter is about to create the transaction, so the burned amount reflects the actual execution-time fee rather than the request-time estimate.

---

### Proof of Concept

1. Ethereum gas fees are at 10 gwei. User calls `withdraw_erc20` for 100 ckUSDC. Minter estimates `max_transaction_fee` = `65_000 * (2 * 10 + 1.5) gwei` ≈ 1.4M gwei ≈ 0.0014 ETH. Minter burns 0.0014 ckETH and 100 ckUSDC from user.

2. Before the minter's next processing timer fires, Ethereum gas fees spike to 200 gwei (a 20× increase, historically observed).

3. `process_retrieve_eth_requests` fires. `lazy_refresh_gas_fee_estimate` returns 200 gwei. `create_transaction` computes `actual_min_max_fee_per_gas = 200 + 1.5 = 201.5 gwei`. `request_max_fee_per_gas = 0.0014 ETH / 65_000 = 21.5 gwei`. Since `201.5 > 21.5`, `InsufficientTransactionFee` is returned. [11](#0-10) 

4. `create_transactions_batch` calls `reschedule_withdrawal_request`. No reimbursement is scheduled. [12](#0-11) 

5. The request cycles indefinitely. The user's 0.0014 ckETH and 100 ckUSDC are permanently burned. `retrieve_eth_status` shows `Pending` indefinitely. No cancel endpoint exists. The user has no recourse within the protocol. [13](#0-12)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-398)
```rust
#[update]
async fn withdraw_erc20(
    WithdrawErc20Arg {
        amount,
        ckerc20_ledger_id,
        recipient,
        from_cketh_subaccount,
        from_ckerc20_subaccount,
    }: WithdrawErc20Arg,
) -> Result<RetrieveErc20Request, WithdrawErc20Error> {
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-451)
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
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L144-177)
```rust
/// ERC-20 withdrawal request issued by the user.
#[derive(Clone, Eq, PartialEq, Decode, Encode)]
pub struct Erc20WithdrawalRequest {
    /// Amount of burn ckETH that can be used to pay for the Ethereum transaction fees.
    #[n(0)]
    pub max_transaction_fee: Wei,
    /// The ERC-20 amount that the receiver will get.
    #[n(1)]
    pub withdrawal_amount: Erc20Value,
    /// The recipient's address of the sent ERC-20 tokens.
    #[n(2)]
    pub destination: Address,
    /// The transaction ID of the ckETH burn operation on the ckETH ledger.
    #[cbor(n(3), with = "crate::cbor::id")]
    pub cketh_ledger_burn_index: LedgerBurnIndex,
    /// Address of the ERC-20 smart contract that is the message call's recipient.
    #[n(4)]
    pub erc20_contract_address: Address,
    /// The ckERC20 ledger on which the minter burned the ckERC20 tokens.
    #[cbor(n(5), with = "icrc_cbor::principal")]
    pub ckerc20_ledger_id: Principal,
    /// The transaction ID of the ckERC20 burn operation on the ckERC20 ledger.
    #[cbor(n(6), with = "crate::cbor::id")]
    pub ckerc20_ledger_burn_index: LedgerBurnIndex,
    /// The owner of the account from which the minter burned ckETH.
    #[cbor(n(7), with = "icrc_cbor::principal")]
    pub from: Principal,
    /// The subaccount from which the minter burned ckETH.
    #[n(8)]
    pub from_subaccount: Option<LedgerSubaccount>,
    /// The IC time at which the withdrawal request arrived.
    #[n(9)]
    pub created_at: u64,
}
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L547-552)
```rust
        assert_eq!(
            self.processed_withdrawal_requests
                .insert(withdrawal_id, withdrawal_request),
            None
        );
        assert!(self.maybe_reimburse.insert(withdrawal_id));
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1155-1168)
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
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L150-183)
```rust
pub async fn process_retrieve_eth_requests() {
    let _guard = match TimerGuard::new(TaskType::RetrieveEth) {
        Ok(guard) => guard,
        Err(e) => {
            log!(
                DEBUG,
                "Failed retrieving timer guard to process ETH requests: {e:?}",
            );
            return;
        }
    };

    if read_state(|s| !s.eth_transactions.has_pending_requests()) {
        return;
    }

    let gas_fee_estimate = match lazy_refresh_gas_fee_estimate().await {
        Some(gas_fee_estimate) => gas_fee_estimate,
        None => {
            log!(
                INFO,
                "Failed retrieving gas fee estimate to process ETH requests",
            );
            return;
        }
    };

    let latest_transaction_count = latest_transaction_count().await;
    resubmit_transactions_batch(latest_transaction_count, &gas_fee_estimate).await;
    create_transactions_batch(gas_fee_estimate);
    sign_transactions_batch().await;
    send_transactions_batch(latest_transaction_count).await;
    finalize_transactions_batch().await;

```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L241-246)
```rust
            }
            Err(e) => {
                log!(INFO, "Failed to resubmit transaction: {e:?}");
            }
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L281-292)
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
        };
```

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
