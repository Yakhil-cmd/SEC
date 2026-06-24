Audit Report

## Title
ckBTC Minter Permanently Burns User ckBTC Without Reimbursement on `AmountTooLow` / `DustOutput` — (`rs/bitcoin/ckbtc/minter/src/lib.rs`)

## Summary
When a ckBTC withdrawal request is dropped at batch-processing time because the amount is too low to cover Bitcoin network fees (`BuildTxError::AmountTooLow` or `BuildTxError::DustOutput`), the minter finalizes the request with `FinalizedStatus::AmountTooLow` and emits no reimbursement event. The user's ckBTC was already burned at submission time and is permanently lost — no BTC is sent and no ckBTC is minted back. This is a ledger conservation violation in the ckBTC chain-fusion system.

## Finding Description
**Burn at submission time:** In `retrieve_btc_with_approval`, `burn_ckbtcs` is called before the request is queued, and `reimbursement_account` is populated on every `RetrieveBtcRequest`. [1](#0-0) 

**`InvalidTransaction` path — reimbursement IS performed:** When `build_unsigned_transaction` returns `BuildTxError::InvalidTransaction`, `reimburse_canceled_requests` is called, returning ckBTC minus a small processing fee. [2](#0-1) 

**`AmountTooLow` path — no reimbursement:** When `BuildTxError::AmountTooLow` is returned, the code iterates the batch and calls only `state::audit::remove_retrieve_btc_request` with `FinalizedStatus::AmountTooLow`. `reimburse_canceled_requests` is never called. [3](#0-2) 

**`DustOutput` path — same omission:** The matching request is also finalized with `FinalizedStatus::AmountTooLow` and no reimbursement. [4](#0-3) 

**`FinalizedStatus::AmountTooLow` is a terminal state** with no reimbursement variant and no recovery path. [5](#0-4) 

**`RetrieveBtcStatusV2::AmountTooLow` is also terminal** — distinct from `Reimbursed` and `WillReimburse`, confirming no reimbursement is ever scheduled. [6](#0-5) 

**`reimburse_canceled_requests` infrastructure is fully in place** and already handles the `reimbursement_account` field correctly; it is simply never invoked for the `AmountTooLow`/`DustOutput` branches. [7](#0-6) 

## Impact Explanation
This is a **High** severity finding matching: *"Significant Chain Fusion, ck-token, ledger, Rosetta, boundary/API, XRC, Internet Identity, NNS, SNS, or infrastructure security impact with concrete user or protocol harm."*

Every affected user suffers a permanent, unrecoverable loss of ckBTC. The ckBTC supply decreases without a corresponding BTC transfer, violating the 1:1 backing invariant of the chain-key token. The minter's UTXO pool retains the BTC value. While each individual loss is bounded by the withdrawal amount, the bug is reachable by any unprivileged principal on every `submit_pending_requests` timer tick during fee spikes, making cumulative losses realistic.

## Likelihood Explanation
Bitcoin network fees are historically volatile. A user submitting a withdrawal just above the dynamic minimum (`fee_based_minimum_withdrawal_amount`) can have their request become `AmountTooLow` if fees spike before the minter's next batch-processing timer fires. No special privileges, governance majority, or key compromise is required — any principal calling `retrieve_btc_with_approval` is exposed. The condition is more likely during periods of Bitcoin network congestion (e.g., Ordinals/Runes activity), which have occurred multiple times on mainnet. [8](#0-7) 

## Recommendation
In the `BuildTxError::AmountTooLow` and `BuildTxError::DustOutput` branches of `submit_pending_requests` in `rs/bitcoin/ckbtc/minter/src/lib.rs`, call `reimburse_canceled_requests` (or `state::audit::reimburse_withdrawal` per request) using the existing `reimbursement_account` on each `RetrieveBtcRequest`, analogous to the `InvalidTransaction` branch. A small processing fee deducted via `reimbursement_fee_for_pending_withdrawal_requests` is acceptable to cover the minter's ledger burn cost, but the remainder must be minted back to the user. Extend `WithdrawalReimbursementReason` with an `AmountTooLow` variant to record the event in the audit log. The `FinalizedStatus` for reimbursed-due-to-low-amount requests should transition through `WillReimburse` → `Reimbursed`, not `AmountTooLow`. [9](#0-8) 

## Proof of Concept
```
1. Call retrieve_btc_with_approval(amount = fee_based_minimum_withdrawal_amount(current_fee_rate) + 1_000 sat).
   → burn_ckbtcs executes; ledger burn index N is recorded.
   → RetrieveBtcRequest { amount, reimbursement_account: Some(caller), ... } is queued as Pending.

2. Bitcoin network fees spike (e.g., 2x) before the next submit_pending_requests timer fires.

3. submit_pending_requests batches the request and calls build_unsigned_transaction.
   → Returns BuildTxError::AmountTooLow because amount < new_bitcoin_fee + minter_fee.

4. Code path (lib.rs L426-432):
       state::audit::remove_retrieve_btc_request(s, request, FinalizedStatus::AmountTooLow, runtime);
   → No reimburse_withdrawal call. No ScheduleWithdrawalReimbursement event emitted.

5. retrieve_btc_status_v2(N) → RetrieveBtcStatusV2::AmountTooLow (terminal).
   User's ckBTC is permanently gone. No Bitcoin was sent. No ckBTC minted back.

Verification: Add an integration/PocketIC test that:
  - Sets up a minter with a known fee rate.
  - Submits a withdrawal just above the minimum.
  - Advances the mock fee estimator to 2x the original rate.
  - Triggers submit_pending_requests.
  - Asserts retrieve_btc_status_v2 == AmountTooLow AND user ckBTC balance is unchanged
    (currently the balance will be lower, confirming the bug).
```

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L209-222)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L400-411)
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
            }
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L329-336)
```rust
    /// The retrieval amount was too low. Satisfying the request is impossible.
    AmountTooLow,
    /// Confirmed a transaction satisfying this request.
    Confirmed { txid: Txid },
    /// The retrieve bitcoin request has been reimbursed.
    Reimbursed(ReimbursedDeposit),
    /// The minter will try to reimburse this transaction.
    WillReimburse(ReimburseDepositTask),
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
