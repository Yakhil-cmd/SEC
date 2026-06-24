### Title
Missing Minimum Fee-Rate Floor in `DogecoinFeeEstimator::estimate_nth_fee` Allows Arbitrarily Low Transaction Fees - (`File: rs/dogecoin/ckdoge/minter/src/fees/mod.rs`)

### Summary

The `DogecoinFeeEstimator` used by the ckDOGE minter lacks a minimum fee-rate floor in its `estimate_nth_fee` implementation. This is the exact same class of bug that was confirmed and patched in the ckBTC minter (documented in `rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md`): an anomalously low fee-rate reported by the underlying coin canister is accepted verbatim, causing the minter to submit Bitcoin-protocol-level transactions with fees so low they are never mined, permanently locking user funds.

### Finding Description

The `BitcoinFeeEstimator` for ckBTC applies a hard minimum floor to the fee rate returned by the Bitcoin canister:

```rust
// rs/bitcoin/ckbtc/minter/src/fees/mod.rs, line 112
median_fee.map(|f| f.max(self.minimum_fee_per_vbyte()))
```

where `minimum_fee_per_vbyte()` returns `1_500` millis/vbyte for Mainnet. This floor was added as a stop-gap after real-world transactions became stuck on 2025-06-21 because the minter selected a fee of only 142 millis/vbyte when the true market rate was ~2,000.

The `DogecoinFeeEstimator` for ckDOGE has **no equivalent floor**:

```rust
// rs/dogecoin/ckdoge/minter/src/fees/mod.rs, lines 53-66
fn estimate_nth_fee(&self, fee_percentiles: &[FeeRate], nth: usize) -> Option<FeeRate> {
    match &self.network {
        Network::Mainnet => {
            if fee_percentiles.len() < 100 || nth >= 100 {
                return None;
            }
            Some(fee_percentiles[nth])   // ← raw value, no floor
        }
        Network::Regtest => Some(DEFAULT_REGTEST_FEE),
    }
}
```

The raw percentile value from the Dogecoin canister is returned directly. If the Dogecoin canister ever reports an anomalously low fee (due to a bug, stale data, or a sparse mempool), the ckDOGE minter will build and sign a Dogecoin transaction with that fee, submit it to the network, and the transaction will sit unconfirmed indefinitely. Because the minter's state machine tracks the submitted transaction and waits for confirmation before processing new requests, the withdrawal is permanently stuck until a canister upgrade intervenes.

The `last_median_fee_per_vbyte` stored in state (initialized to `FeeRate::from_millis_per_byte(1)`) is used directly when building real transactions via `submit_pending_requests` → `estimate_fee_per_vbyte` → `build_unsigned_transaction`. There is no guard between the fee-canister response and the transaction builder.

### Impact Explanation

A ckDOGE withdrawal (retrieve_doge) burns ckDOGE from the user's ledger account and submits a Dogecoin transaction. If the submitted transaction carries a fee too low to be mined:

- The user's ckDOGE is already burned (irreversible on the ledger).
- The Dogecoin transaction is never confirmed.
- The minter's stuck-transaction resubmission logic will attempt RBF bumps, but each bump starts from the same anomalously low base fee, so bumps may still be below the relay threshold.
- User funds are effectively locked until a governance-approved canister upgrade is deployed, exactly as happened with ckBTC on 2025-06-21.

### Likelihood Explanation

The ckBTC minter experienced this exact failure in production on 2025-06-21 with no external attacker required — the Dogecoin canister returning a bad fee percentile is sufficient. The Dogecoin network has historically had very low and volatile fee markets, making anomalous fee-percentile readings more likely than on Bitcoin. Any unprivileged user who calls `retrieve_doge` / `retrieve_doge_with_approval` during a window when the fee canister returns a low value will trigger the stuck-transaction scenario.

### Recommendation

Apply the same fix used for ckBTC: add a `minimum_fee_per_vbyte` constant to `DogecoinFeeEstimator` and clamp the returned fee rate:

```rust
const fn minimum_fee_per_vbyte(&self) -> FeeRate {
    // Dogecoin minimum relay fee is 1 koinu/byte = 1_000 millikoinu/byte
    FeeRate::from_millis_per_byte(1_000)
}

fn estimate_nth_fee(&self, fee_percentiles: &[FeeRate], nth: usize) -> Option<FeeRate> {
    match &self.network {
        Network::Mainnet => {
            if fee_percentiles.len() < 100 || nth >= 100 {
                return None;
            }
            Some(fee_percentiles[nth].max(self.minimum_fee_per_vbyte()))
        }
        Network::Regtest => Some(DEFAULT_REGTEST_FEE),
    }
}
```

### Proof of Concept

**ckBTC confirmed analog (already fixed):** [1](#0-0) 

**ckBTC fix — floor applied:** [2](#0-1) 

**ckDOGE — missing floor, raw value returned:** [3](#0-2) 

**State initialized with fee = 1 millis/vbyte, used directly for real transactions:** [4](#0-3) 

**`estimate_fee_per_vbyte` stores the raw (unguarded for ckDOGE) fee into state and returns it for use in `build_unsigned_transaction`:** [5](#0-4) 

**`submit_pending_requests` uses this fee directly to build and sign the real Bitcoin/Dogecoin transaction:** [6](#0-5)

### Citations

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L26-30)
```markdown
1. An extremely low fee per vbyte was chosen by the minter for those transactions, which prevented them from being mined
   in the first place. We currently don’t have a satisfying explanation for how this low median fee was computed and are
   also investigating the bitcoin canister. A stop-gap solution was introduced
   in [#5742](https://github.com/dfinity/ic/pull/5742), to ensure that the fee per vbyte computed by the minter is
   always at least 1.5 sats/vbyte (for Bitcoin Mainnet).
```

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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L2065-2066)
```rust
            last_fee_per_vbyte: vec![FeeRate::from_millis_per_byte(1); 100],
            last_median_fee_per_vbyte: Some(FeeRate::from_millis_per_byte(1)),
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L227-263)
```rust
pub async fn estimate_fee_per_vbyte<R: CanisterRuntime>(runtime: &R) -> Option<FeeRate> {
    let btc_network = state::read_state(|s| s.btc_network);
    match runtime
        .get_current_fee_percentiles(&bitcoin_canister::GetCurrentFeePercentilesRequest {
            network: btc_network.into(),
        })
        .await
    {
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
                }
                None => None,
            }
        }
        Err(err) => {
            log!(
                Priority::Info,
                "[estimate_fee_per_vbyte]: failed to get median fee per vbyte: {}",
                err
            );
            None
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
