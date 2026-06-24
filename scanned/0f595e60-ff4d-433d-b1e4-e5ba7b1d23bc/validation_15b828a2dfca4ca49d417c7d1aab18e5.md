### Title
Missing Minimum Fee Rate Floor in ckDOGE Minter's `DogecoinFeeEstimator::estimate_nth_fee` Allows Near-Zero Fee Dogecoin Transactions - (File: `rs/dogecoin/ckdoge/minter/src/fees/mod.rs`)

---

### Summary

The ckDOGE minter's `DogecoinFeeEstimator::estimate_nth_fee` returns raw fee percentile values from the Dogecoin canister with no minimum floor. The ckBTC minter's `BitcoinFeeEstimator` has an explicit `minimum_fee_per_vbyte()` guard added after an observed anomalously-low fee event on 2025-06-21. The ckDOGE minter lacks this same protection, meaning any period of anomalously low Dogecoin network fee percentiles will cause the minter to submit Dogecoin withdrawal transactions with near-zero fees, which the Dogecoin network will not relay or confirm.

---

### Finding Description

`BitcoinFeeEstimator::estimate_nth_fee` applies a hardcoded minimum fee floor before returning:

```rust
median_fee.map(|f| f.max(self.minimum_fee_per_vbyte()))
```

where `minimum_fee_per_vbyte()` returns 1,500 millisat/vbyte on Mainnet. The code comment explicitly documents why this was added:

> "An estimated fee per vbyte of 142 millisatoshis per vbyte was selected around 2025.06.21 01:09:50 UTC for Bitcoin Mainnet, whereas the median fee around that time should have been 2_000. Until we know the root cause, we ensure that the estimated fee has a meaningful minimum value." [1](#0-0) 

`DogecoinFeeEstimator::estimate_nth_fee` has no equivalent floor — it returns the raw percentile value directly:

```rust
Network::Mainnet => {
    if fee_percentiles.len() < 100 || nth >= 100 {
        return None;
    }
    Some(fee_percentiles[nth])   // ← no minimum floor applied
}
``` [2](#0-1) 

The returned fee rate flows directly into `estimate_fee_per_vbyte` (shared ckBTC/ckDOGE logic), which stores it as `last_median_fee_per_vbyte` and uses it to build and sign all pending Dogecoin withdrawal transactions: [3](#0-2) 

The `estimate_withdrawal_fee` query endpoint in the ckDOGE minter also reads `last_median_fee_per_vbyte` directly and passes it to `estimate_retrieve_doge_fee`, so users receive a misleadingly low fee estimate: [4](#0-3) 

---

### Impact Explanation

When the Dogecoin canister returns anomalously low fee percentiles (e.g., 0 or near-zero millikoinu/byte), the ckDOGE minter will:

1. Store the near-zero fee as `last_median_fee_per_vbyte`.
2. Compute a near-zero `fee_based_retrieve_btc_min_amount` (since `median_fee_rate.fee_ceil(PER_REQUEST_SIZE_BOUND)` ≈ 0), allowing users to submit withdrawal requests that would normally be rejected.
3. Build and sign Dogecoin transactions with near-zero fees using expensive threshold ECDSA signing.
4. Submit those transactions to the Dogecoin network, where they will not be relayed or confirmed because they fall below the Dogecoin relay fee threshold (Dogecoin requires at least 0.001 DOGE/kB = 1,000 koinu/byte).

The result is: users' ckDOGE is already burned on the IC ledger, but their DOGE withdrawal is permanently stuck. The minter wastes threshold ECDSA signing cycles (each signing round costs ~29B cycles per input) on transactions that will never confirm. The minter's RBF resubmission logic (`MIN_RELAY_FEE_RATE_INCREASE = 100_000 millikoinu/byte`) will eventually bump the fee, but only after the `MIN_RESUBMISSION_DELAY` passes and only if the fee percentiles have recovered — there is no guaranteed recovery path. [5](#0-4) 

---

### Likelihood Explanation

The ckBTC minter already experienced this exact failure mode on 2025-06-21 (documented in the source code), which is why `minimum_fee_per_vbyte()` was added to `BitcoinFeeEstimator`. The ckDOGE minter shares the same fee-refresh infrastructure (`estimate_fee_per_vbyte` in `rs/bitcoin/ckbtc/minter/src/lib.rs`) and the same Dogecoin canister integration, making it equally susceptible. Any unprivileged user calling `retrieve_doge_with_approval` during such a window triggers the impact. No special privileges are required. [6](#0-5) 

---

### Recommendation

Add a `minimum_fee_per_byte()` method to `DogecoinFeeEstimator` analogous to `BitcoinFeeEstimator::minimum_fee_per_vbyte()`, and apply it in `estimate_nth_fee`:

```rust
const fn minimum_fee_per_byte(&self) -> FeeRate {
    let rate = match &self.network {
        Network::Mainnet => 1_000_000, // 1 koinu/byte = 1_000_000 millikoinu/byte
        Network::Regtest => 0,
    };
    FeeRate::from_millis_per_byte(rate)
}
```

Then in `estimate_nth_fee`:
```rust
Some(fee_percentiles[nth].max(self.minimum_fee_per_byte()))
```

The minimum should be at least the Dogecoin relay fee threshold (1 koinu/byte = 1,000,000 millikoinu/byte). [2](#0-1) 

---

### Proof of Concept

1. The Dogecoin canister returns 100 fee percentiles all set to `FeeRate::from_millis_per_byte(0)` (as can happen during anomalous conditions, mirroring the ckBTC event of 2025-06-21).
2. The ckDOGE minter's timer fires `estimate_fee_per_vbyte`, which calls `fee_estimator.estimate_median_fee(&fees)` → `estimate_nth_fee(&fees, 50)`.
3. `DogecoinFeeEstimator::estimate_nth_fee` returns `Some(FeeRate::from_millis_per_byte(0))` with no floor applied.
4. `last_median_fee_per_vbyte` is set to `FeeRate(0)` and `fee_based_retrieve_btc_min_amount` is set to `retrieve_doge_min_amount` (the base minimum, with no fee component).
5. A user calls `retrieve_doge_with_approval` with the minimum amount. The minter burns their ckDOGE and queues the withdrawal.
6. The minter builds a Dogecoin transaction with `evaluate_transaction_fee(tx, FeeRate(0))` = 0 koinu fee.
7. The transaction is signed (consuming ~29B cycles × num_inputs) and submitted to the Dogecoin network.
8. The Dogecoin network rejects/ignores the zero-fee transaction. The user's ckDOGE is burned but DOGE is never received. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L77-113)
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
}

impl FeeEstimator for BitcoinFeeEstimator {
    // The default dustRelayFee is 3 sat/vB,
    // which translates to a dust threshold of 546 satoshi for P2PKH outputs.
    // The threshold for other types is lower,
    // so we simply use 546 satoshi as the minimum amount per output.
    const DUST_LIMIT: u64 = 546;

    const MIN_RELAY_FEE_RATE_INCREASE: FeeRate = FeeRate::from_millis_per_byte(1_000);

    fn estimate_nth_fee(&self, fee_percentiles: &[FeeRate], nth: usize) -> Option<FeeRate> {
        /// The default fee we use on regtest networks.
        const DEFAULT_REGTEST_FEE: FeeRate = FeeRate::from_millis_per_byte(5_000);

        let median_fee = match &self.network {
            Network::Mainnet | Network::Testnet => {
                if fee_percentiles.len() < 100 || nth >= 100 {
                    return None;
                }
                Some(fee_percentiles[nth])
            }
            Network::Regtest => Some(DEFAULT_REGTEST_FEE),
        };
        median_fee.map(|f| f.max(self.minimum_fee_per_vbyte()))
    }
```

**File:** rs/dogecoin/ckdoge/minter/src/fees/mod.rs (L49-51)
```rust
    // Incremental fee rate for resubmission is 100 koinu/byte,
    // corresponding to 100k millikoinus/byte
    const MIN_RELAY_FEE_RATE_INCREASE: FeeRate = FeeRate::from_millis_per_byte(100_000);
```

**File:** rs/dogecoin/ckdoge/minter/src/fees/mod.rs (L53-66)
```rust
    fn estimate_nth_fee(&self, fee_percentiles: &[FeeRate], nth: usize) -> Option<FeeRate> {
        const DEFAULT_REGTEST_FEE: FeeRate =
            FeeRate::from_millis_per_byte(DogecoinFeeEstimator::DUST_LIMIT * 1_000);

        match &self.network {
            Network::Mainnet => {
                if fee_percentiles.len() < 100 || nth >= 100 {
                    return None;
                }
                Some(fee_percentiles[nth])
            }
            Network::Regtest => Some(DEFAULT_REGTEST_FEE),
        }
    }
```

**File:** rs/dogecoin/ckdoge/minter/src/fees/mod.rs (L118-121)
```rust
    fn evaluate_transaction_fee(&self, tx: &UnsignedTransaction, fee_rate: FeeRate) -> u64 {
        let tx_size = DogecoinTransactionSigner::fake_sign(tx).len();
        fee_rate.fee_ceil(tx_size as u64)
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L235-250)
```rust
        Ok(fees) => {
            let fee_estimator = state::read_state(|s| runtime.fee_estimator(s));
            match fee_estimator.estimate_median_fee(&fees) {
                Some(median_fee) => {
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
                    Some(median_fee)
```

**File:** rs/dogecoin/ckdoge/minter/src/main.rs (L99-127)
```rust
fn estimate_withdrawal_fee(
    arg: EstimateFeeArg,
) -> Result<WithdrawalFee, EstimateWithdrawalFeeError> {
    // This is a **query** endpoint, so mutating the state is not an issue
    // (even when called in replicated mode) since any change will be discarded.
    ic_ckbtc_minter::state::mutate_state(|s| {
        let fee_estimator = DOGECOIN_CANISTER_RUNTIME.fee_estimator(s);
        let withdrawal_amount = arg.amount.unwrap_or(s.fee_based_retrieve_btc_min_amount);

        ic_ckdoge_minter::fees::estimate_retrieve_doge_fee(
            &mut s.available_utxos,
            withdrawal_amount,
            s.last_median_fee_per_vbyte
                .expect("Bitcoin current fee percentiles not retrieved yet."),
            s.max_num_inputs_in_transaction,
            &fee_estimator,
        )
        .map_err(|e| match e {
            BuildTxError::NotEnoughFunds
            | BuildTxError::InvalidTransaction(InvalidTransactionError::TooManyInputs { .. }) => {
                EstimateWithdrawalFeeError::AmountTooHigh
            }
            BuildTxError::AmountTooLow | BuildTxError::DustOutput { .. } => {
                EstimateWithdrawalFeeError::AmountTooLow {
                    min_amount: s.fee_based_retrieve_btc_min_amount,
                }
            }
        })
    })
```

**File:** rs/dogecoin/ckdoge/minter/src/main.rs (L130-143)
```rust
#[update]
async fn retrieve_doge_with_approval(
    args: RetrieveDogeWithApprovalArgs,
) -> Result<RetrieveDogeOk, RetrieveDogeWithApprovalError> {
    check_anonymous_caller();
    let result = ic_ckbtc_minter::updates::retrieve_btc::retrieve_btc_with_approval(
        args.into(),
        &DOGECOIN_CANISTER_RUNTIME,
    )
    .await
    .map(RetrieveDogeOk::from)
    .map_err(RetrieveDogeWithApprovalError::from);
    check_postcondition(result)
}
```
