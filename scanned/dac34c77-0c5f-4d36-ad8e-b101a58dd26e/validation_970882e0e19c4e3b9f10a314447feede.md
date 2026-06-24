### Title
Missing Minimum Fee Floor in ckDOGE Minter Allows Abnormally Low Fee Rates to Produce Stuck Dogecoin Withdrawal Transactions - (File: rs/dogecoin/ckdoge/minter/src/fees/mod.rs)

---

### Summary

The `DogecoinFeeEstimator::estimate_nth_fee` in the ckDOGE minter has no minimum fee floor, unlike the analogous `BitcoinFeeEstimator::estimate_nth_fee` in the ckBTC minter. The ckBTC minter suffered a real production incident on 2025-06-21 where an abnormally low fee estimate caused BTC withdrawal transactions to get stuck. A stop-gap fix was applied to ckBTC (`minimum_fee_per_vbyte`), but the same fix was never applied to the ckDOGE minter, leaving it exposed to the same class of bug.

---

### Finding Description

In `rs/bitcoin/ckbtc/minter/src/fees/mod.rs`, `BitcoinFeeEstimator::estimate_nth_fee` applies a hardcoded minimum floor via `minimum_fee_per_vbyte()`:

```rust
median_fee.map(|f| f.max(self.minimum_fee_per_vbyte()))
```

This floor was introduced after a confirmed production incident where the ckBTC minter selected a fee of only 142 millisatoshis/vbyte (instead of ~2,000), causing three BTC withdrawal transactions to get stuck in the Bitcoin mempool. The upgrade proposal `rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md` documents this explicitly.

In contrast, `DogecoinFeeEstimator::estimate_nth_fee` in `rs/dogecoin/ckdoge/minter/src/fees/mod.rs` returns the raw percentile value with no floor:

```rust
Network::Mainnet => {
    if fee_percentiles.len() < 100 || nth >= 100 {
        return None;
    }
    Some(fee_percentiles[nth])  // No .max(minimum_fee_per_byte()) applied
}
```

If the Dogecoin canister returns an anomalously low fee percentile array (due to the same unknown root cause that affected the Bitcoin canister), the ckDOGE minter will use that abnormally low fee rate to build and submit Dogecoin withdrawal transactions. Those transactions will be priced below the Dogecoin network's relay threshold and will either be rejected by nodes or sit unconfirmed indefinitely.

---

### Impact Explanation

- **Chain-fusion mint/burn/replay bug class**: ckDOGE withdrawal transactions (ckDOGE → DOGE) can get permanently stuck in the Dogecoin mempool or be dropped, with users' ckDOGE already burned on the IC ledger. The minter's RBF resubmission logic (`resubmit_transactions`) will attempt to replace stuck transactions by incrementing the fee by `MIN_RELAY_FEE_RATE_INCREASE`, but if the initial fee was near zero, the replacement transactions will also be below the relay threshold for many rounds, prolonging the stuck state.
- Users who submitted valid `retrieve_doge` requests will have their ckDOGE burned but receive no DOGE, until the minter is manually upgraded with a governance proposal (as was required for ckBTC).
- The `COST_OF_ONE_BILLION_CYCLES` reimbursement fee is deducted even on cancellation, so users suffer a financial loss.

---

### Likelihood Explanation

The ckBTC minter experienced this exact failure mode on 2025-06-21 with no clear root cause identified (the upgrade proposal states: "We currently don't have a satisfying explanation for how this low median fee was computed and are also investigating the bitcoin canister"). Since the ckDOGE minter uses the same `get_current_fee_percentiles` mechanism via `dogecoin_canister::dogecoin_get_fee_percentiles`, the same anomaly can recur. The ckBTC fix was explicitly described as a "stop-gap" and was not ported to ckDOGE. Likelihood is **medium**: the root cause is unresolved and the same infrastructure is shared.

---

### Recommendation

Apply the same minimum fee floor pattern to `DogecoinFeeEstimator::estimate_nth_fee` in `rs/dogecoin/ckdoge/minter/src/fees/mod.rs`. Add a `minimum_fee_per_byte()` method to `DogecoinFeeEstimator` with a network-appropriate floor (e.g., the Dogecoin minimum relay fee of 1,000 koinu/byte = 1,000,000 millikoinu/byte), and apply it via `.max(self.minimum_fee_per_byte())` before returning the estimated fee, mirroring the ckBTC fix.

---

### Proof of Concept

**Root cause — missing floor in ckDOGE:** [1](#0-0) 

**Contrast: ckBTC has the floor applied:** [2](#0-1) 

**The floor itself in ckBTC:** [3](#0-2) 

**Production incident confirming the ckBTC version of this bug occurred:** [4](#0-3) 

**Test confirming ckBTC now uses the floor when fees are anomalously low:** [5](#0-4) 

**ckDOGE uses the same `estimate_nth_fee` path for Mainnet withdrawals:** [6](#0-5)

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

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L99-113)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/tests/tests.rs (L2882-2887)
```rust
    // unusually low fees, use hardcoded minimum value instead of median
    test(
        &[FeeRate::from_millis_per_byte(142); 100],
        FeeRate::from_millis_per_byte(142),
        FeeRate::from_millis_per_byte(1_500),
    );
```

**File:** rs/dogecoin/ckdoge/minter/src/lib.rs (L57-66)
```rust
impl CanisterRuntime for DogeCanisterRuntime {
    type Estimator = DogecoinFeeEstimator;
    type EventLogger = CkDogeEventLogger;

    fn fee_estimator(&self, state: &CkBtcMinterState) -> DogecoinFeeEstimator {
        DogecoinFeeEstimator::from_state(state)
    }

    fn event_logger(&self) -> Self::EventLogger {
        CkDogeEventLogger
```
