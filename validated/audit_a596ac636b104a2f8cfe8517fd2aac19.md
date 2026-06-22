### Title
Missing Minimum Fee Rate Floor in `DogecoinFeeEstimator::estimate_nth_fee` Can Cause ckDOGE Withdrawal Transactions to Become Permanently Stuck - (File: `rs/dogecoin/ckdoge/minter/src/fees/mod.rs`)

---

### Summary

The `DogecoinFeeEstimator` used by the ckDOGE minter lacks the minimum fee rate floor that was added to `BitcoinFeeEstimator` after a real-world incident on 2025-06-21 where anomalously low fee percentiles from the Bitcoin canister caused ckBTC withdrawal transactions to become stuck. If the Dogecoin canister similarly returns anomalously low fee percentiles, the ckDOGE minter will use that low fee rate, causing withdrawal transactions to be stuck in the mempool and user funds to be frozen — the same dual impact (user fund loss / functionality unavailability) as the reference report.

---

### Finding Description

The `BitcoinFeeEstimator::estimate_nth_fee()` applies a hard-coded minimum floor via `minimum_fee_per_vbyte()` (1,500 millis/byte for Mainnet, 1,000 for Testnet) to guard against anomalously low fee oracle responses:

```rust
// rs/bitcoin/ckbtc/minter/src/fees/mod.rs
median_fee.map(|f| f.max(self.minimum_fee_per_vbyte()))
```

This floor was introduced as a stop-gap fix (PR #5742) after a documented production incident where an estimated fee of 142 millisatoshis/vbyte was selected instead of the expected ~2,000, causing three ckBTC withdrawal transactions to become stuck.

The `DogecoinFeeEstimator::estimate_nth_fee()` does **not** apply any such floor — it returns the raw fee percentile from the Dogecoin canister without any minimum bound:

```rust
// rs/dogecoin/ckdoge/minter/src/fees/mod.rs
fn estimate_nth_fee(&self, fee_percentiles: &[FeeRate], nth: usize) -> Option<FeeRate> {
    match &self.network {
        Network::Mainnet => {
            if fee_percentiles.len() < 100 || nth >= 100 {
                return None;
            }
            Some(fee_percentiles[nth])  // No floor applied
        }
        Network::Regtest => Some(DEFAULT_REGTEST_FEE),
    }
}
```

The shared `estimate_fee_per_vbyte()` function in `rs/bitcoin/ckbtc/minter/src/lib.rs` calls `fee_estimator.estimate_median_fee()` → `estimate_nth_fee()` and uses the result to build and submit withdrawal transactions. For ckDOGE, this path goes through `DogecoinFeeEstimator`, which has no floor.

Additionally, the resubmission logic in `resubmit_transactions()` (shared via `ic_ckbtc_minter`) requires that the new fee rate strictly exceeds the previous one by at least `MIN_RELAY_FEE_RATE_INCREASE`. If the initial transaction was submitted with an anomalously low fee, the resubmission arithmetic can produce a deterministic panic — exactly as documented in the ckBTC incident (PR #5713 fixed a panic in the resubmission path).

---

### Impact Explanation

1. **User fund loss / frozen funds**: A user calls `retrieve_doge_with_approval` (public update endpoint). The minter burns ckDOGE from the user's ledger account and submits a Dogecoin transaction with an anomalously low fee. The transaction is not mined. The user's ckDOGE is gone but DOGE is not received. Funds are frozen until an operator manually intervenes with a canister upgrade.

2. **Withdrawal functionality unavailability**: If the resubmission path panics deterministically (as happened with ckBTC), the minter's timer-driven `finalize_requests` loop becomes stuck, blocking all pending ckDOGE withdrawals — not just the one that triggered the low-fee transaction.

---

### Likelihood Explanation

The exact same failure mode already occurred in production with ckBTC on 2025-06-21. The ckDOGE minter reuses the same shared `ic_ckbtc_minter` infrastructure (same `estimate_fee_per_vbyte`, same `resubmit_transactions`, same `FeeEstimator` trait) and is susceptible to the same oracle anomaly from the Dogecoin canister. The root cause of the ckBTC incident (anomalously low fee percentiles returned by the canister) is not specific to Bitcoin and can equally affect the Dogecoin canister integration. Any user calling `retrieve_doge_with_approval` during such a period would trigger the stuck-transaction scenario.

---

### Recommendation

Add a `minimum_fee_per_vbyte()` method to `DogecoinFeeEstimator` (analogous to `BitcoinFeeEstimator::minimum_fee_per_vbyte()`) and apply it in `estimate_nth_fee()`:

```rust
impl DogecoinFeeEstimator {
    const fn minimum_fee_per_vbyte(&self) -> FeeRate {
        // Set a meaningful minimum based on Dogecoin network characteristics
        FeeRate::from_millis_per_byte(100_000) // e.g., 100 koinu/byte
    }
}

// In estimate_nth_fee:
Some(fee_percentiles[nth].max(self.minimum_fee_per_vbyte()))
```

The exact value should be calibrated to Dogecoin's typical fee market, but any meaningful floor prevents the zero/near-zero fee scenario that causes transactions to be permanently stuck.

---

### Proof of Concept

**Attacker-controlled entry path**: Any unprivileged user calling the public `retrieve_doge_with_approval` update endpoint on the ckDOGE minter canister.

**Step-by-step**:

1. The Dogecoin canister returns fee percentiles with anomalously low values (e.g., all entries near 0 millikoinus/byte — the same anomaly that affected Bitcoin on 2025-06-21).

2. `estimate_fee_per_vbyte()` calls `DogecoinFeeEstimator::estimate_median_fee()` → `estimate_nth_fee(&fees, 50)`, which returns the raw 50th-percentile value with no floor applied. [1](#0-0) 

3. The minter builds and signs a Dogecoin withdrawal transaction using this near-zero fee rate. The transaction is broadcast but never mined because it does not meet the minimum relay fee.

4. On the next timer tick, `finalize_requests()` detects the stuck transaction and calls `resubmit_transactions()`. The resubmission arithmetic — `fee_rate.max(prev_fee_rate + Fee::MIN_RELAY_FEE_RATE_INCREASE)` — may produce a panic if the stored `effective_fee_per_vbyte` is inconsistent with the new transaction size, exactly as documented in the ckBTC incident. [2](#0-1) 

5. The user's ckDOGE is burned (ledger burn is irreversible at this point) but DOGE is never delivered. All subsequent ckDOGE withdrawals are also blocked until a canister upgrade is deployed.

**Contrast with the fixed ckBTC path**: `BitcoinFeeEstimator::estimate_nth_fee()` applies `f.max(self.minimum_fee_per_vbyte())`, ensuring the fee is always at least 1,500 millis/byte on Mainnet, preventing this scenario. [3](#0-2) 

**Production evidence**: The ckBTC minter upgrade proposal `rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md` explicitly documents this failure mode and the stop-gap fix. The ckDOGE minter has not received the equivalent protection. [4](#0-3)

### Citations

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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L860-873)
```rust
        let tx_fee_per_vbyte = match submitted_tx.effective_fee_per_vbyte {
            Some(prev_fee_rate) => {
                // There are 2 requirements on the fee of a replacement transaction:
                // 1) The fee rate strictly increases. Although not required from [BIP-125](https://en.bitcoin.it/wiki/BIP_0125),
                // it is actually required by the [implementation](https://github.com/bitcoin/bitcoin/blob/d2ecd6815d89c9b089b55bc96fdf93b023be8dda/src/policy/rbf.cpp#L149).
                // 2) The total fee of the replacement transaction must be at least as high as the previous transaction fee plus the minimum relay fee.
                //
                // To satisfy both conditions, we choose the new fee rate to be the previous one plus the minimum relay fee rate increase.
                // This will satisfy 2) because the computed total fee of a transaction is not dependent on the variable signature sizes
                // (see `FeeEstimator::evaluate_transaction_fee` and `fake_sign`)
                fee_rate.max(prev_fee_rate + Fee::MIN_RELAY_FEE_RATE_INCREASE)
            }
            None => fee_rate,
        };
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
