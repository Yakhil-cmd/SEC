### Title
ckBTC Minter Silently Destroys User Funds When `AmountTooLow` Is Triggered by a Fee Spike After ckBTC Burn - (`rs/bitcoin/ckbtc/minter/src/lib.rs`)

---

### Summary

The ckBTC minter's `retrieve_btc` withdrawal flow is split into two phases: (1) burn ckBTC at request time after checking `amount >= fee_based_retrieve_btc_min_amount`, and (2) build the Bitcoin transaction at processing time using the current live fee rate. Because `fee_based_retrieve_btc_min_amount` is dynamically recomputed from Bitcoin network fees, a request that was valid at submission can become unprocessable by the time the minter attempts to build the transaction. When this happens, the minter finalizes the request as `AmountTooLow` and **does not reimburse the already-burned ckBTC**, permanently destroying the user's funds. There is no rescue path.

---

### Finding Description

**Phase 1 — Request acceptance and burn** (`rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`):

The user calls `retrieve_btc`. The minter reads the current `fee_based_retrieve_btc_min_amount` from state and rejects if `args.amount < min_retrieve_amount`. If the check passes, ckBTC is burned via `burn_ckbtcs` and the request is queued as `Pending`. [1](#0-0) [2](#0-1) 

**Phase 2 — Transaction building** (`rs/bitcoin/ckbtc/minter/src/lib.rs`):

`submit_pending_requests` is called asynchronously. It first calls `estimate_fee_per_vbyte`, which fetches the current Bitcoin network fee and **updates `fee_based_retrieve_btc_min_amount` in state**. It then calls `build_unsigned_transaction` with the live fee rate. [3](#0-2) [4](#0-3) 

**The missing reimbursement** — When `build_unsigned_transaction` returns `BuildTxError::AmountTooLow`, the minter calls `remove_retrieve_btc_request` with `FinalizedStatus::AmountTooLow`. This is a **terminal state with no reimbursement**: [5](#0-4) 

Contrast this with the `InvalidTransaction` branch, which **does** call `reimburse_canceled_requests` and mints ckBTC back to the user: [6](#0-5) 

The eventlog replay confirms `RemovedRetrieveBtcRequest` only pushes `FinalizedStatus::AmountTooLow` — no reimbursement event is emitted: [7](#0-6) 

The `fee_based_retrieve_btc_min_amount` can jump by 50,000 satoshis or more per fee tier, as shown in the fee-based minimum withdrawal amount computation: [8](#0-7) 

---

### Impact Explanation

A user who calls `retrieve_btc` with an amount just above the current minimum has their ckBTC burned immediately and irreversibly. If Bitcoin network fees spike before the minter processes the request, `build_unsigned_transaction` returns `AmountTooLow`, the request is silently dropped as `FinalizedStatus::AmountTooLow`, and the user's ckBTC is permanently destroyed. The `RetrieveBtcStatusV2::AmountTooLow` variant is a terminal state: [9](#0-8) 

There is no rescue function, no reimbursement, and no way for the user to recover their funds. This is a direct ledger conservation violation: ckBTC is burned but no BTC is ever sent.

---

### Likelihood Explanation

Bitcoin network fees are highly volatile. During inscription/ordinal activity or mempool congestion, fees can spike by multiples within minutes. The `fee_based_retrieve_btc_min_amount` is updated every time `estimate_fee_per_vbyte` is called inside `submit_pending_requests`, meaning the effective minimum can increase between the user's call and the minter's processing loop. The real-world ckBTC upgrade proposal from 2025-06-27 confirms that ckBTC withdrawals have already gotten stuck due to fee-related issues: [10](#0-9) 

Any user who submits a `retrieve_btc` request near the minimum amount during a period of rising fees is at risk. This is reachable by any unprivileged user via the public `retrieve_btc` or `retrieve_btc_with_approval` endpoints.

---

### Recommendation

Add a reimbursement call in the `BuildTxError::AmountTooLow` branch of `submit_pending_requests`, mirroring the `InvalidTransaction` branch. The reimbursement should mint ckBTC back to the `reimbursement_account` stored in the `RetrieveBtcRequest`, minus a small penalty fee. The `WillReimburse` / `Reimbursed` status variants already exist in `RetrieveBtcStatusV2` and the `reimburse_withdrawal` audit function is already implemented — they simply need to be wired into the `AmountTooLow` path. [11](#0-10) 

---

### Proof of Concept

1. Bitcoin fees are low: `fee_based_retrieve_btc_min_amount = 100_000 sats`.
2. Alice calls `retrieve_btc(amount = 100_000)`. Check passes; 100,000 satoshis of ckBTC are burned from Alice's account. Request queued as `Pending`.
3. Bitcoin fees spike (e.g., ordinal inscription wave). `estimate_fee_per_vbyte` runs inside `submit_pending_requests` and updates `fee_based_retrieve_btc_min_amount = 150_000 sats`.
4. `build_unsigned_transaction` is called with Alice's request (amount = 100,000 sats) and the new fee rate. The amount is insufficient to cover the Bitcoin transaction fee → returns `BuildTxError::AmountTooLow`.
5. The minter calls `remove_retrieve_btc_request(s, request, FinalizedStatus::AmountTooLow, runtime)`. No reimbursement is triggered.
6. Alice queries `retrieve_btc_status_v2` → `AmountTooLow` (terminal). Her 100,000 satoshis of ckBTC are permanently destroyed.

The attacker-controlled entry path is the public `retrieve_btc` endpoint; the vulnerable step is the mismatch between the fee check at burn time and the fee computation at transaction-build time, with no reimbursement on the `AmountTooLow` branch. [12](#0-11) [5](#0-4)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L146-242)
```rust
pub async fn retrieve_btc<R: CanisterRuntime>(
    args: RetrieveBtcArgs,
    runtime: &R,
) -> Result<RetrieveBtcOk, RetrieveBtcError> {
    let caller = ic_cdk::api::msg_caller();

    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcError::TemporarilyUnavailable)?;

    let _ecdsa_public_key = init_ecdsa_public_key().await;
    let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }

    let _guard = retrieve_btc_guard(Account {
        owner: caller,
        subaccount: None,
    })?;
    let (min_retrieve_amount, btc_network) =
        read_state(|s| (s.fee_based_retrieve_btc_min_amount, s.btc_network));

    if args.amount < min_retrieve_amount {
        return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
    }

    let parsed_address = BitcoinAddress::parse(&args.address, btc_network)?;
    if read_state(|s| s.count_incomplete_retrieve_btc_requests() >= MAX_CONCURRENT_PENDING_REQUESTS)
    {
        return Err(RetrieveBtcError::TemporarilyUnavailable(
            "too many pending retrieve_btc requests".to_string(),
        ));
    }

    let balance = balance_of(caller).await?;
    if args.amount > balance {
        return Err(RetrieveBtcError::InsufficientFunds { balance });
    }

    let btc_checker_principal = read_state(|s| s.btc_checker_principal).map(|id| id.get().into());
    let status = check_address(btc_checker_principal, args.address.clone(), runtime).await?;
    match status {
        BtcAddressCheckStatus::Tainted => {
            log!(
                Priority::Debug,
                "rejected an attempt to withdraw {} BTC to address {} due to failed Bitcoin check",
                crate::tx::DisplayAmount(args.amount),
                args.address,
            );
            return Err(RetrieveBtcError::GenericError {
                error_message: "Destination address is tainted".to_string(),
                error_code: ErrorCode::TaintedAddress as u64,
            });
        }
        BtcAddressCheckStatus::Clean => {}
    }

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

    log!(
        Priority::Debug,
        "accepted a retrieve btc request for {} BTC to address {} (block_index = {})",
        crate::tx::DisplayAmount(request.amount),
        args.address,
        request.block_index
    );

    mutate_state(|s| state::audit::accept_retrieve_btc_request(s, request, &IC_CANISTER_RUNTIME));

    assert_eq!(
        crate::state::RetrieveBtcStatus::Pending,
        read_state(|s| s.retrieve_btc_status(block_index))
    );

    schedule_now(TaskType::ProcessLogic, &IC_CANISTER_RUNTIME);

    Ok(RetrieveBtcOk { block_index })
}
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L239-249)
```rust
                    let fee_based_retrieve_btc_min_amount =
                        fee_estimator.fee_based_minimum_withdrawal_amount(median_fee);
                    log!(
                        Priority::Debug,
                        "[estimate_fee_per_vbyte]: update median fee per vbyte to {median_fee:?} and fee-based minimum retrieve amount to {fee_based_retrieve_btc_min_amount} with {fees:?}"
                    );
                    mutate_state(|s| {
                        s.last_fee_per_vbyte = fees;
                        s.last_median_fee_per_vbyte = Some(median_fee);
                        s.fee_based_retrieve_btc_min_amount = fee_based_retrieve_btc_min_amount;
                    });
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

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L130-147)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L329-331)
```rust
    /// The retrieval amount was too low. Satisfying the request is impossible.
    AmountTooLow,
    /// Confirmed a transaction satisfying this request.
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L19-33)
```markdown
Upgrade the ckBTC minter to try to unblock three transactions ckBTC → BTC (withdrawals) that are currently stuck since
2025.06.21.

After analysis, see this
forum [**post**](https://forum.dfinity.org/t/ckbtc-a-canister-issued-bitcoin-twin-token-on-the-ic-1-1-backed-by-btc/17606/202)
for more details, the problem appears to be due to the following:

1. An extremely low fee per vbyte was chosen by the minter for those transactions, which prevented them from being mined
   in the first place. We currently don’t have a satisfying explanation for how this low median fee was computed and are
   also investigating the bitcoin canister. A stop-gap solution was introduced
   in [#5742](https://github.com/dfinity/ic/pull/5742), to ensure that the fee per vbyte computed by the minter is
   always at least 1.5 sats/vbyte (for Bitcoin Mainnet).
2. There is a deterministic panic occurring in the minter when it tries to resubmit those transactions, which explains
   why those transactions are currently stuck. This should be completely fixed
   by [#5713](https://github.com/dfinity/ic/pull/5713).
```
