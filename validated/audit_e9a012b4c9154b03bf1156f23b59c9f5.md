### Title
ckBTC Minter Burns User ckBTC Without Reimbursement When Pending Withdrawal Is Dropped as `AmountTooLow` — (`rs/bitcoin/ckbtc/minter/src/lib.rs`)

---

### Summary

When a pending ckBTC withdrawal request is dropped because the withdrawal amount is too low to cover Bitcoin network fees at batch-processing time, the ckBTC that was already burned from the user's account is **not reimbursed**. The minter retains the equivalent BTC value. This is the direct IC analog of the Symmio M-1 bug: a fee paid upfront for a pending operation is kept by the protocol when that operation is cancelled/dropped rather than returned to the user.

---

### Finding Description

**Vulnerability class:** Chain-fusion mint/burn ledger conservation bug.

The ckBTC withdrawal flow works as follows:

1. A user calls `retrieve_btc` or `retrieve_btc_with_approval`.
2. The minter burns the user's ckBTC on the ledger (the burn is the "fee" paid upfront).
3. A `RetrieveBtcRequest` is queued with a `reimbursement_account` set to the caller.
4. Later, `submit_pending_requests` batches pending requests and calls `build_unsigned_transaction`. [1](#0-0) 

When `build_unsigned_transaction` returns `BuildTxError::AmountTooLow` (the total batch amount cannot cover the Bitcoin network fee) or `BuildTxError::DustOutput` (a single request's amount is below the dust threshold), the affected requests are silently finalized with `FinalizedStatus::AmountTooLow` via `remove_retrieve_btc_request` — **with no reimbursement**: [2](#0-1) [3](#0-2) 

In stark contrast, when `BuildTxError::InvalidTransaction` occurs (e.g., too many inputs), `reimburse_canceled_requests` **is** called and users receive their ckBTC back minus a small processing fee: [4](#0-3) 

The reimbursement infrastructure is fully in place — `reimbursement_account` is populated on every `RetrieveBtcRequest` — but it is simply never invoked in the `AmountTooLow` / `DustOutput` paths: [5](#0-4) 

The `FinalizedStatus::AmountTooLow` state is a terminal state with no reimbursement path: [6](#0-5) 

The eventlog replay confirms that `RemovedRetrieveBtcRequest` only pushes a finalized record — no reimbursement event is emitted: [7](#0-6) 

---

### Impact Explanation

A user who calls `retrieve_btc` with an amount that passes the minimum check at submission time has their ckBTC burned immediately. If Bitcoin network fees spike significantly before the minter processes the batch, `build_unsigned_transaction` can return `AmountTooLow` and the request is dropped. The user's burned ckBTC is permanently lost — no Bitcoin is sent and no ckBTC is minted back. The minter's BTC pool retains the value. This is a direct ledger conservation violation: ckBTC supply decreases without a corresponding BTC transfer to the user.

The `total_unspent_tx_fees` metric in the ckETH minter shows the IC is aware of the concept of unspent fees needing accounting; the ckBTC minter has no equivalent recovery for the `AmountTooLow` case: [8](#0-7) 

---

### Likelihood Explanation

Bitcoin network fees are volatile. A user submitting a withdrawal request just above the dynamic minimum (`fee_based_retrieve_btc_min_amount`) can have their request become `AmountTooLow` if fees spike before the minter's next `submit_pending_requests` timer fires. This is an unprivileged, externally reachable path: any principal can call `retrieve_btc_with_approval` and be affected. No admin key, governance majority, or threshold corruption is required. The scenario is more likely during periods of Bitcoin network congestion. [9](#0-8) 

---

### Recommendation

In the `BuildTxError::AmountTooLow` and `BuildTxError::DustOutput` branches of `submit_pending_requests`, call `reimburse_canceled_requests` (or `state::audit::reimburse_withdrawal` per request) using the existing `reimbursement_account` on each `RetrieveBtcRequest`, analogous to how `InvalidTransaction` is handled. A small processing fee (e.g., `reimbursement_fee_for_pending_withdrawal_requests`) can be deducted to cover the minter's ledger burn cost, but the remainder must be minted back to the user. The `WithdrawalReimbursementReason` enum should be extended with an `AmountTooLow` variant to record the event in the audit log. [10](#0-9) 

---

### Proof of Concept

```
1. User calls retrieve_btc_with_approval(amount = retrieve_btc_min_amount + ε).
   → ckBTC is burned from user's account (ledger burn index N).
   → RetrieveBtcRequest { amount, reimbursement_account: Some(user), ... } is queued.

2. Bitcoin network fees spike 2x before the next submit_pending_requests timer fires.

3. submit_pending_requests batches the request and calls build_unsigned_transaction.
   → Returns BuildTxError::AmountTooLow because amount < bitcoin_fee + minter_fee.

4. Code path taken (lib.rs ~L426):
       state::audit::remove_retrieve_btc_request(s, request, FinalizedStatus::AmountTooLow, runtime);
   → No reimburse_withdrawal call. No ScheduleWithdrawalReimbursement event emitted.

5. retrieve_btc_status_v2(N) → RetrieveBtcStatusV2::AmountTooLow
   User's ckBTC is permanently gone. No Bitcoin was sent.
``` [11](#0-10) [12](#0-11)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L204-222)
```rust
    let burn_memo = BurnMemo::Convert {
        address: Some(&args.address),
        kyt_fee: None,
        status: Some(Status::Accepted),
    };
    let block_index =
        burn_ckbtcs(caller, args.amount, crate::memo::encode(&burn_memo).into()).await?;

    let request = RetrieveBtcRequest {
        amount: args.amount,
        address: parsed_address,
        block_index,
        received_at: ic_cdk::api::time(),
        kyt_provider: None,
        reimbursement_account: Some(Account {
            owner: caller,
            subaccount: None,
        }),
    };
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L292-329)
```rust
fn reimburse_canceled_requests<R: CanisterRuntime>(
    state: &mut state::CkBtcMinterState,
    requests: BTreeSet<state::RetrieveBtcRequest>,
    reason: WithdrawalReimbursementReason,
    total_fee: u64,
    runtime: &R,
) {
    assert!(!requests.is_empty());
    let fees = distribute(total_fee, requests.len() as u64);
    // This assertion makes sure the fee is smaller than each request amount
    assert!(
        fees[0] <= state.retrieve_btc_min_amount,
        "BUG: fees {fees:?} for {} withdrawal requests are larger than `retrieve_btc_min_amount` {}",
        requests.len(),
        state.retrieve_btc_min_amount
    );
    for (request, fee) in requests.into_iter().zip(fees.into_iter()) {
        if let Some(account) = request.reimbursement_account {
            let amount = request.amount.saturating_sub(fee);
            if amount > 0 {
                state::audit::reimburse_withdrawal(
                    state,
                    request.block_index,
                    amount,
                    account,
                    reason.clone(),
                    runtime,
                );
            }
        } else {
            log!(
                Priority::Info,
                "[reimburse_canceled_requests]: account is not found for retrieve_btc request ({:?})",
                request
            );
        }
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L400-410)
```rust
            Err(BuildTxError::InvalidTransaction(err)) => {
                log!(
                    Priority::Info,
                    "[submit_pending_requests]: error in building transaction ({:?})",
                    err
                );
                let reason = reimbursement::WithdrawalReimbursementReason::InvalidTransaction(err);
                let reimbursement_fee = fee_estimator
                    .reimbursement_fee_for_pending_withdrawal_requests(batch.len() as u64);
                reimburse_canceled_requests(s, batch, reason, reimbursement_fee, runtime);
                None
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L412-434)
```rust
            Err(BuildTxError::AmountTooLow) => {
                log!(
                    Priority::Info,
                    "[submit_pending_requests]: dropping requests for total BTC amount {} to addresses {} (too low to cover the fees)",
                    tx::DisplayAmount(batch.iter().map(|req| req.amount).sum::<u64>()),
                    batch
                        .iter()
                        .map(|req| req.address.display(s.btc_network))
                        .collect::<Vec<_>>()
                        .join(",")
                );

                // There is no point in retrying the request because the
                // amount is too low.
                for request in batch {
                    state::audit::remove_retrieve_btc_request(
                        s,
                        request,
                        state::FinalizedStatus::AmountTooLow,
                        runtime,
                    );
                }
                None
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L436-453)
```rust
            Err(BuildTxError::DustOutput { address, amount }) => {
                log!(
                    Priority::Info,
                    "[submit_pending_requests]: dropping a request for BTC amount {} to {} (too low to cover the fees)",
                    tx::DisplayAmount(amount),
                    address.display(s.btc_network)
                );

                let mut requests_to_put_back = BTreeSet::new();
                for request in batch {
                    if request.address == address && request.amount == amount {
                        // Finalize the request that we cannot fulfill.
                        state::audit::remove_retrieve_btc_request(
                            s,
                            request,
                            state::FinalizedStatus::AmountTooLow,
                            runtime,
                        );
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L257-267)
```rust
/// The outcome of a retrieve_btc request.
#[derive(Clone, Eq, PartialEq, Debug, Deserialize, Serialize)]
pub enum FinalizedStatus {
    /// The request amount was to low to cover the fees.
    AmountTooLow,
    /// The transaction that retrieves BTC got enough confirmations.
    Confirmed {
        /// The witness transaction identifier of the transaction.
        txid: Txid,
    },
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L329-337)
```rust
    /// The retrieval amount was too low. Satisfying the request is impossible.
    AmountTooLow,
    /// Confirmed a transaction satisfying this request.
    Confirmed { txid: Txid },
    /// The retrieve bitcoin request has been reimbursed.
    Reimbursed(ReimbursedDeposit),
    /// The minter will try to reimburse this transaction.
    WillReimburse(ReimburseDepositTask),
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L621-625)
```rust
pub struct WithdrawalCancellation {
    pub fee: u64,
    pub reason: WithdrawalReimbursementReason,
    pub requests: BTreeSet<RetrieveBtcRequest>,
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state/eventlog.rs (L405-418)
```rust
                EventType::RemovedRetrieveBtcRequest { block_index } => {
                    let request = state
                    .remove_pending_retrieve_btc_request(block_index)
                    .ok_or_else(|| {
                        ReplayLogError::InconsistentLog(format!(
                            "Attempted to remove a non-pending retrieve_btc request {block_index}"
                        ))
                    })?;

                    state.push_finalized_request(FinalizedBtcRequest {
                        request: request.into(),
                        state: FinalizedStatus::AmountTooLow,
                    })
                }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L362-375)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L32-38)
```rust
    /// Reimbursement fee in base unit for when a batch of *pending* withdrawal requests could not be processed,
    /// e.g., because it would require too many inputs.
    ///
    /// No transaction was issued (not signed and not sent) but the minter still did some work:
    /// 1) Burn on the ledger for each withdrawal request.
    /// 2) Build transaction candidate to cover the amount in the batch of withdrawal requests.
    fn reimbursement_fee_for_pending_withdrawal_requests(&self, num_requests: u64) -> u64;
```

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L128-147)
```rust
    /// Returns the minimum withdrawal amount based on the current median fee rate (in millisatoshi per byte).
    /// The returned amount is in satoshi.
    fn fee_based_minimum_withdrawal_amount(&self, median_fee_rate: FeeRate) -> Satoshi {
        match self.network {
            Network::Mainnet | Network::Testnet => {
                const PER_REQUEST_RBF_BOUND: u64 = 22_100;
                const PER_REQUEST_VSIZE_BOUND: u64 = 221;
                const PER_REQUEST_MINTER_FEE_BOUND: u64 = 305;

                ((PER_REQUEST_RBF_BOUND
                    + median_fee_rate.fee_ceil(PER_REQUEST_VSIZE_BOUND)
                    + PER_REQUEST_MINTER_FEE_BOUND
                    + self.check_fee)
                    / 50_000) //TODO DEFI-2187: adjust increment of minimum withdrawal amount to be a multiple of retrieve_btc_min_amount/2
                    * 50_000
                    + self.retrieve_btc_min_amount
            }
            Network::Regtest => self.retrieve_btc_min_amount,
        }
    }
```
