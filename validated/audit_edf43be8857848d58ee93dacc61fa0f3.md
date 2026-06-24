Let me verify the key code references in this claim.

Audit Report

## Title
Fee spike between `retrieve_btc` acceptance and batch processing causes permanent ckBTC burn with no BTC delivery and no reimbursement — (`rs/bitcoin/ckbtc/minter/src/lib.rs`, `rs/bitcoin/ckbtc/minter/src/fees/mod.rs`)

## Summary
The `fee_based_retrieve_btc_min_amount` guard in `retrieve_btc` is evaluated at call time using the last cached fee rate. When `submit_pending_requests` fires, it fetches a fresh fee rate and uses it for `build_unsigned_transaction`. If the fee rate has spiked sufficiently since acceptance, `BuildTxError::AmountTooLow` is returned. Unlike `BuildTxError::InvalidTransaction`, the `AmountTooLow` branch finalizes the request without scheduling any reimbursement, permanently destroying the user's ckBTC with no BTC delivered.

## Finding Description
**Step 1 — Acceptance check uses cached fee rate:**
`retrieve_btc` reads `fee_based_retrieve_btc_min_amount` from state at call time and accepts the request if `args.amount >= min_retrieve_amount`. [1](#0-0) 

**Step 2 — ckBTC is burned before the request is queued:** [2](#0-1) 

**Step 3 — Timer fetches a fresh fee rate and atomically updates `fee_based_retrieve_btc_min_amount`:** [3](#0-2) 

**Step 4 — `build_unsigned_transaction` is called with the new fee rate; `AmountTooLow` is returned when `fee + minter_fee > amount`:** [4](#0-3) 

**Step 5 — `AmountTooLow` branch finalizes with no reimbursement:** [5](#0-4) 

**Contrast with `InvalidTransaction`, which does call `reimburse_canceled_requests`:** [6](#0-5) 

**`remove_retrieve_btc_request` only records an event and pushes to finalized — no reimbursement is scheduled:** [7](#0-6) 

**`WithdrawalReimbursementReason` has no `AmountTooLow` variant — the reimbursement path is structurally absent:** [8](#0-7) 

**The RBF buffer provides only ~100 sat/vbyte of headroom for a 221-vbyte transaction:** [9](#0-8) 

**The codebase itself documents a real incident (June 2025) where the fee estimate was off by ~14×:** [10](#0-9) 

## Impact Explanation
A user who calls `retrieve_btc` at or near the minimum amount during a low-fee period permanently loses their ckBTC: the ledger records a burn, the minter records `FinalizedStatus::AmountTooLow`, no BTC is sent, and no ckBTC is minted back. This is a concrete, permanent loss of ckBTC — an in-scope chain-key asset — with no recovery path. This matches the allowed High impact: "Significant Chain Fusion, ck-token, ledger … security impact with concrete user or protocol harm."

## Likelihood Explanation
Any unprivileged user calling `retrieve_btc` near the minimum amount is at risk whenever Bitcoin network fees spike between acceptance and batch processing. The window can be minutes to hours (`max_time_in_queue_nanos`). Bitcoin fee spikes exceeding 100 sat/vbyte from a low base are historically common (Ordinals, BRC-20, halving events). The minter's own codebase documents a June 2025 incident where the fee estimate was off by ~14×, confirming this is not theoretical. No special privileges or attacker action are required — the loss is triggered by normal network conditions.

## Recommendation
1. **Reimburse on `AmountTooLow`**: Add an `AmountTooLow` variant to `WithdrawalReimbursementReason` and treat `BuildTxError::AmountTooLow` the same as `InvalidTransaction` — call `reimburse_canceled_requests` so the user's ckBTC is returned minus a small processing fee.
2. **Re-validate at batch time**: Before finalizing a request as `AmountTooLow`, verify whether the request amount was above the minimum at acceptance time; if so, reimburse in full.
3. **Scale the RBF buffer**: Make `PER_REQUEST_RBF_BOUND` proportional to the current fee rate rather than a fixed constant, so the minimum withdrawal amount scales with fee volatility.

## Proof of Concept
State-machine test:
1. Initialize minter with fee percentiles at 1 sat/vbyte → `fee_based_retrieve_btc_min_amount` = 100,000 sats.
2. Call `retrieve_btc(amount=100_000)` → accepted, ckBTC burned.
3. Update fee percentiles to 500 sat/vbyte in the mock Bitcoin canister.
4. Tick the timer → `submit_pending_requests` → `estimate_fee_per_vbyte` updates state → `build_unsigned_transaction` with 500 sat/vbyte → `fee (110,500) + minter_fee (300) > amount (100,000)` → `BuildTxError::AmountTooLow`.
5. Assert: `retrieve_btc_status(block_index) == FinalizedStatus::AmountTooLow`, no reimbursement entry in `pending_withdrawal_reimbursements`, and no mint on the ledger.

### Citations

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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L245-249)
```rust
                    mutate_state(|s| {
                        s.last_fee_per_vbyte = fees;
                        s.last_median_fee_per_vbyte = Some(median_fee);
                        s.fee_based_retrieve_btc_min_amount = fee_based_retrieve_btc_min_amount;
                    });
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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1304-1308)
```rust
    let fee = fee_estimator.evaluate_transaction_fee(&unsigned_tx, fee_rate);

    if fee + minter_fee > amount {
        return Err(BuildTxError::AmountTooLow);
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L67-84)
```rust
pub fn remove_retrieve_btc_request<R: CanisterRuntime>(
    state: &mut CkBtcMinterState,
    request: RetrieveBtcRequest,
    status: FinalizedStatus,
    runtime: &R,
) {
    record_event(
        EventType::RemovedRetrieveBtcRequest {
            block_index: request.block_index,
        },
        runtime,
    );

    state.push_finalized_request(FinalizedBtcRequest {
        request: request.into(),
        state: status,
    });
}
```

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L39-43)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Deserialize, Serialize, candid::CandidType)]
pub enum WithdrawalReimbursementReason {
    #[serde(rename = "invalid_transaction")]
    InvalidTransaction(InvalidTransactionError),
}
```

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L77-87)
```rust
    /// An estimated fee per vbyte of 142 millisatoshis per vbyte was selected around 2025.06.21 01:09:50 UTC
    /// for Bitcoin Mainnet, whereas the median fee around that time should have been 2_000.
    /// Until we know the root cause, we ensure that the estimated fee has a meaningful minimum value.
    const fn minimum_fee_per_vbyte(&self) -> FeeRate {
        let rate = match &self.network {
            Network::Mainnet => 1_500,
            Network::Testnet => 1_000,
            Network::Regtest => 0,
        };
        FeeRate::from_millis_per_byte(rate)
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
