### Title
No Maximum Fee Check in ckBTC Minter Withdrawal Exposes Users to Unexpected Fee Deduction - (File: rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs)

---

### Summary

The `retrieve_btc` and `retrieve_btc_with_approval` endpoints in the ckBTC minter burn the user's ckBTC immediately upon request, but the actual Bitcoin transaction fee is determined asynchronously when the minter processes the batch — based on the **current** Bitcoin network fee rate at that later time. Neither endpoint accepts a `max_fee` parameter, so users have no way to bound the fee they will pay. If Bitcoin fees spike between request submission and batch processing, users receive significantly less BTC than expected, and in extreme cases their ckBTC is burned with no BTC delivered.

---

### Finding Description

When a user calls `retrieve_btc` or `retrieve_btc_with_approval`, the full `args.amount` of ckBTC is burned immediately on the ledger: [1](#0-0) 

The request is then queued in `pending_retrieve_btc_requests`. Later, when the timer fires and `submit_pending_requests` runs, it fetches the **current** Bitcoin fee rate and builds the transaction: [2](#0-1) 

The actual BTC delivered to the user is `amount - bitcoin_fee - minter_fee`, where `bitcoin_fee` is computed from the live network fee rate at processing time, not at request time.

Neither `RetrieveBtcArgs` nor `RetrieveBtcWithApprovalArgs` contains a `max_fee` field: [3](#0-2) 

Users can call the `estimate_withdrawal_fee` query endpoint to get an estimate: [4](#0-3) 

However, this estimate is non-binding. The actual fee is determined later by `submit_pending_requests` using `last_median_fee_per_vbyte` at processing time, which can differ substantially from the estimate.

The `RetrieveBtcRequest` stored in state records only the `amount` and `address`; there is no stored `max_fee` constraint to enforce at processing time: [5](#0-4) 

If the fee spikes enough that `amount < bitcoin_fee + minter_fee`, the minter finalizes the request as `AmountTooLow` and removes it — the ckBTC has already been burned: [6](#0-5) 

---

### Impact Explanation

**Impact: Medium**

- Users receive less BTC than expected when Bitcoin network fees rise between request submission and batch processing.
- In extreme fee-spike scenarios, the request is finalized as `AmountTooLow`: the user's ckBTC is already burned, and they receive only a reimbursement minus a `reimbursement_fee_for_pending_withdrawal_requests` charge.
- The user has no on-chain mechanism to protect themselves from this outcome; the `estimate_withdrawal_fee` query is advisory only and not certified.

---

### Likelihood Explanation

**Likelihood: Medium**

- Bitcoin fees are volatile and can spike 5–10× within minutes during periods of high network activity (e.g., inscription mints, halving events).
- The ckBTC minter intentionally batches requests and waits up to `max_time_in_queue_nanos` before processing, creating a guaranteed window during which fees can change.
- The `fee_based_retrieve_btc_min_amount` guard is checked at **request time** using the fee rate at that moment: [7](#0-6) 

  A request accepted at a low-fee moment can still be processed at a high-fee moment, with no recourse for the user.

---

### Recommendation

Add an optional `max_fee: Option<u64>` field to both `RetrieveBtcArgs` and `RetrieveBtcWithApprovalArgs`. In `submit_pending_requests`, before building the transaction, check each request's `max_fee` constraint against the computed fee. If the fee exceeds the user's limit, reimburse the ckBTC (minus a small processing fee) rather than proceeding. This mirrors the standard slippage-protection pattern used in DeFi protocols.

---

### Proof of Concept

1. User calls `estimate_withdrawal_fee(Some(5_000_000))` and sees `bitcoin_fee = 10_000` satoshis.
2. User calls `retrieve_btc_with_approval("bc1q...", 5_000_000)`. The minter immediately burns 5,000,000 ckBTC from the user's account. [8](#0-7) 

3. Before the minter's timer fires, Bitcoin fees spike 20× (e.g., due to a large inscription event). `last_median_fee_per_vbyte` is updated to the new high value.
4. `submit_pending_requests` runs, fetches the new fee rate, and computes `bitcoin_fee = 200_000` satoshis.
5. The user receives only `5_000_000 - 200_000 - minter_fee ≈ 4_793_000` satoshis of BTC — nearly 4% less than expected with no warning.
6. If the fee spikes further such that `bitcoin_fee + minter_fee > 5_000_000`, the request is finalized as `AmountTooLow`: [9](#0-8) 

   The user's 5,000,000 ckBTC is burned and they receive no BTC, only a partial reimbursement.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L25-45)
```rust
#[derive(Clone, Eq, PartialEq, Debug, CandidType, Deserialize)]
pub struct RetrieveBtcArgs {
    // amount to retrieve in satoshi
    pub amount: u64,

    // address where to send bitcoins
    pub address: String,
}

/// The arguments of the [retrieve_btc_with_approval] endpoint.
#[derive(Clone, Eq, PartialEq, Debug, CandidType, Deserialize)]
pub struct RetrieveBtcWithApprovalArgs {
    // amount to retrieve in satoshi
    pub amount: u64,

    // address where to send bitcoins
    pub address: String,

    // The subaccount to burn ckBTC from.
    pub from_subaccount: Option<Subaccount>,
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L166-171)
```rust
    let (min_retrieve_amount, btc_network) =
        read_state(|s| (s.fee_based_retrieve_btc_min_amount, s.btc_network));

    if args.amount < min_retrieve_amount {
        return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L209-210)
```rust
    let block_index =
        burn_ckbtcs(caller, args.amount, crate::memo::encode(&burn_memo).into()).await?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L314-319)
```rust
    let block_index = burn_ckbtcs_icrc2(
        caller_account,
        args.amount,
        crate::memo::encode(&burn_memo_icrc2).into(),
    )
    .await?;
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L358-384)
```rust
    let fee_millisatoshi_per_vbyte = match estimate_fee_per_vbyte(runtime).await {
        Some(fee) => fee,
        None => return,
    };
    let fee_estimator = read_state(|s| runtime.fee_estimator(s));
    let max_num_inputs_in_transaction = read_state(|s| s.max_num_inputs_in_transaction);

    let maybe_sign_request = state::mutate_state(|s| {
        let batch = s.build_batch(MAX_REQUESTS_PER_BATCH);

        if batch.is_empty() {
            return None;
        }

        let outputs: Vec<_> = batch
            .iter()
            .map(|req| (req.address.clone(), req.amount))
            .collect();

        match build_unsigned_transaction(
            &mut s.available_utxos,
            outputs,
            &main_address,
            max_num_inputs_in_transaction,
            fee_millisatoshi_per_vbyte,
            &fee_estimator,
        ) {
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

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L219-248)
```rust
#[query]
fn estimate_withdrawal_fee(arg: EstimateFeeArg) -> WithdrawalFee {
    // This is a **query** endpoint, so mutating the state is not an issue
    // (even when called in replicated mode) since any change will be discarded.
    match mutate_state(|s| {
        let fee_estimator = IC_CANISTER_RUNTIME.fee_estimator(s);
        let withdrawal_amount = arg.amount.unwrap_or(s.fee_based_retrieve_btc_min_amount);
        ic_ckbtc_minter::estimate_retrieve_btc_fee(
            &mut s.available_utxos,
            withdrawal_amount,
            s.last_median_fee_per_vbyte
                .expect("Bitcoin current fee percentiles not retrieved yet."),
            s.max_num_inputs_in_transaction,
            &fee_estimator,
        )
    }) {
        Ok(fee) => fee,
        Err(BuildTxError::NotEnoughFunds) => {
            panic!("ERROR: withdrawal amount is too large for the minter")
        }
        Err(e @ BuildTxError::DustOutput { .. } | e @ BuildTxError::AmountTooLow) => panic!(
            "BUG: withdrawal amount is too low ({e:?}), but the withdrawal amount should be large enough to prevent this"
        ),
        Err(BuildTxError::InvalidTransaction(
            e @ InvalidTransactionError::TooManyInputs { .. },
        )) => panic!(
            "ERROR: the minter cannot currently process such a large withdrawal amount because it would require too many inputs ({e:?}), \
            resulting in the transaction being potentially non-standard"
        ),
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L64-85)
```rust
pub struct RetrieveBtcRequest {
    /// The amount to convert to BTC.
    /// The minter withdraws BTC transfer fees from this amount.
    pub amount: u64,
    /// The destination BTC address.
    pub address: BitcoinAddress,
    /// The BURN transaction index on the ledger.
    /// Serves as a unique request identifier.
    pub block_index: u64,
    /// The time at which the minter accepted the request.
    pub received_at: u64,
    /// The KYT provider that validated this request.
    /// The field is optional because old retrieve_btc requests
    /// didn't go through the KYT check.
    #[serde(rename = "kyt_provider")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub kyt_provider: Option<Principal>,
    /// The reimbursement_account of the retrieve_btc transaction.
    #[serde(rename = "reimbursement_account")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reimbursement_account: Option<Account>,
}
```
