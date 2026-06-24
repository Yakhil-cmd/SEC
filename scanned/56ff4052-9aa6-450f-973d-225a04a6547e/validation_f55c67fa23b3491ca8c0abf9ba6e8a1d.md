### Title
Hardcoded BTC/XDR Price Assumption in Chain-Fusion Minter Fee Accounting Leads to Wrong Reimbursement Deductions - (File: rs/bitcoin/ckbtc/minter/src/fees/mod.rs)

---

### Summary
The ckBTC minter hardcodes a BTC/XDR price assumption (`1 BTC = 10,000 XDR`) to derive `COST_OF_ONE_BILLION_CYCLES = 10 satoshis`, which is used to deduct a reimbursement fee from user withdrawal amounts when a batch cannot be processed. If the BTC/XDR price changes significantly (as it has historically), this constant becomes stale, causing the minter to either over-charge or under-charge users during reimbursement — directly analogous to the EVM hardcoded gas cost issue.

---

### Finding Description

In `rs/bitcoin/ckbtc/minter/src/fees/mod.rs`, `BitcoinFeeEstimator` defines:

```rust
/// Cost in sats of 1B cycles.
///
/// Use a lower bound on the price of Bitcoin of 1 BTC = 10_000 XDR,
/// so that 10 sats correspond to 1B cycles.
pub const COST_OF_ONE_BILLION_CYCLES: Satoshi = 10;
```

This constant is used in `reimbursement_fee_for_pending_withdrawal_requests`:

```rust
fn reimbursement_fee_for_pending_withdrawal_requests(&self, num_requests: u64) -> u64 {
    num_requests.saturating_mul(Self::COST_OF_ONE_BILLION_CYCLES)
}
```

This function is called in `rs/bitcoin/ckbtc/minter/src/lib.rs` when a batch of withdrawal requests cannot be processed (e.g., `TooManyInputs`):

```rust
let reimbursement_fee = fee_estimator
    .reimbursement_fee_for_pending_withdrawal_requests(batch.len() as u64);
reimburse_canceled_requests(s, batch, reason, reimbursement_fee, runtime);
```

The fee is then subtracted from each user's withdrawal amount before minting the reimbursement. The constant `10 satoshis per 1B cycles` is derived from the assumption that `1 BTC = 10,000 XDR`. This assumption is baked in at compile time with no mechanism to update it. The same pattern exists in `rs/dogecoin/ckdoge/minter/src/fees/mod.rs` with `COST_OF_ONE_BILLION_CYCLES = 5_000_000 koinu` based on `50 DOGE = 1 XDR`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

When a user's withdrawal request is canceled due to `TooManyInputs`, the minter deducts `COST_OF_ONE_BILLION_CYCLES * num_requests` satoshis from the reimbursed amount. If BTC price has risen significantly above the assumed 10,000 XDR/BTC floor (BTC has historically traded at 30,000–100,000+ USD, far above 10,000 XDR), the constant underestimates the real cost in satoshis, meaning the minter under-charges users and absorbs the difference — a ledger conservation bug. Conversely, if BTC price dropped below the assumed floor, users would be over-charged. The deducted amount is burned from the user's ckBTC balance without recourse. This is a **ledger conservation bug** affecting chain-fusion users: the reimbursement amount minted to users is incorrect relative to actual costs incurred.

---

### Likelihood Explanation

**Medium.** BTC price has historically varied by orders of magnitude. The assumption of `1 BTC = 10,000 XDR` is a lower bound chosen conservatively, but the IC already has an Exchange Rate Canister (XRC) that provides live ICP/XDR rates, and the CMC uses live rates for cycle minting. The ckBTC minter does not use any live rate for this constant. Any user whose batch is canceled due to `TooManyInputs` is affected. This scenario is reachable by any user who submits a large withdrawal that triggers the `TooManyInputs` path. [5](#0-4) 

---

### Recommendation

Replace the compile-time constant `COST_OF_ONE_BILLION_CYCLES` with a value derived from the live BTC/XDR exchange rate, sourced from the Exchange Rate Canister (XRC) or the Cycles Minting Canister (CMC), which already maintains a live ICP/XDR rate. Alternatively, store the cost-per-billion-cycles as a configurable minter state field (similar to how `retrieve_btc_min_amount` and `check_fee` are stored in `CkBtcMinterState`) that can be updated via upgrade arguments or a governance proposal when the BTC/XDR rate changes materially. [6](#0-5) 

---

### Proof of Concept

1. A user submits a large ckBTC withdrawal that requires more than `DEFAULT_MAX_NUM_INPUTS_IN_TRANSACTION` UTXOs.
2. The minter's `submit_pending_requests` calls `build_unsigned_transaction`, which returns `BuildTxError::InvalidTransaction(TooManyInputs { ... })`.
3. The minter calls `reimbursement_fee_for_pending_withdrawal_requests(batch.len())`, which returns `batch.len() * 10` satoshis (hardcoded).
4. `reimburse_canceled_requests` distributes this fee across requests and mints `request.amount - fee` ckBTC back to each user.
5. At current BTC prices (~$100,000 USD ≈ ~80,000 XDR), the actual cost of 1B cycles in satoshis is approximately `1B cycles / (1T cycles/XDR) * (1 BTC / 80,000 XDR) * 100,000,000 sats/BTC ≈ 1.25 sats`, meaning the hardcoded `10 sats` over-charges users by ~8x at current prices. If BTC were at $1,000,000 USD, the over-charge would be ~80x. [2](#0-1) [7](#0-6)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L41-49)
```rust
#[derive(Clone, Debug, PartialEq)]
pub struct BitcoinFeeEstimator {
    /// The Bitcoin network that the minter will connect to
    network: Network,
    /// Minimum amount of bitcoin that can be retrieved
    retrieve_btc_min_amount: u64,
    /// The fee for a single Bitcoin check request.
    check_fee: u64,
}
```

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L56-59)
```rust
    /// Cost in sats of 1B cycles.
    ///
    /// Use a lower bound on the price of Bitcoin of 1 BTC = 10_000 XDR, so that 10 sats correspond to 1B cycles.
    pub const COST_OF_ONE_BILLION_CYCLES: Satoshi = 10;
```

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L154-158)
```rust
    fn reimbursement_fee_for_pending_withdrawal_requests(&self, num_requests: u64) -> u64 {
        // Heuristic:
        // * charge 1B cycles for each request (a burn on the ledger on the fiduciary subnet is probably around 50M cycles).
        num_requests.saturating_mul(Self::COST_OF_ONE_BILLION_CYCLES)
    }
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

**File:** rs/dogecoin/ckdoge/minter/src/fees/mod.rs (L24-27)
```rust
    /// Cost in koinu of 1B cycles.
    ///
    /// Use a lower bound on the price of Doge of 50 DOGE = 1 XDR, so that 5M koinus correspond to 1B cycles.
    pub const COST_OF_ONE_BILLION_CYCLES: u64 = 5_000_000;
```
