### Title
Missing Minimum Fee Floor in ckDOGE Minter Fee Estimation Causes Stuck Withdrawals - (File: rs/dogecoin/ckdoge/minter/src/fees/mod.rs)

### Summary

The `DogecoinFeeEstimator::estimate_nth_fee()` in the ckDOGE minter returns raw fee percentile values with no minimum floor, unlike the `BitcoinFeeEstimator` which was patched after a real production incident on 2025-06-21 to enforce a `minimum_fee_per_vbyte()` floor. If the Dogecoin fee percentiles reported by the Bitcoin canister are anomalously low, the ckDOGE minter will submit withdrawal transactions with an insufficient fee rate, causing them to never be mined and permanently blocking user withdrawals.

### Finding Description

`BitcoinFeeEstimator::estimate_nth_fee()` applies a hardcoded minimum floor via `self.minimum_fee_per_vbyte()`:

```rust
// rs/bitcoin/ckbtc/minter/src/fees/mod.rs line 112
median_fee.map(|f| f.max(self.minimum_fee_per_vbyte()))
```

This floor was introduced in PR #5742 after an incident on 2025-06-21 where the ckBTC minter selected 142 millisatoshis/vbyte (actual median was ~2,000), causing three BTC withdrawal transactions to get stuck. The upgrade proposal `rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md` documents this explicitly.

`DogecoinFeeEstimator::estimate_nth_fee()` has no equivalent protection:

```rust
// rs/dogecoin/ckdoge/minter/src/fees/mod.rs lines 53-66
fn estimate_nth_fee(&self, fee_percentiles: &[FeeRate], nth: usize) -> Option<FeeRate> {
    match &self.network {
        Network::Mainnet => {
            if fee_percentiles.len() < 100 || nth >= 100 {
                return None;
            }
            Some(fee_percentiles[nth])  // raw value, no floor
        }
        Network::Regtest => Some(DEFAULT_REGTEST_FEE),
    }
}
```

The returned fee rate flows directly into `estimate_fee_per_vbyte` (shared ckBTC library), which stores it as `last_median_fee_per_vbyte` and uses it to build and sign all outgoing Dogecoin transactions. The `fee_based_minimum_withdrawal_amount` in `DogecoinFeeEstimator` also receives this unclamped value, meaning the minimum withdrawal guard is also computed from a potentially near-zero fee rate.

The structural parallel to the external report is exact: PoolTogether capped the solver's maximum fee at the **minimum prize** (canary tier) rather than the **actual tier's prize**, so when gas exceeded the minimum prize, no prizes were claimed. Here, the ckDOGE minter uses the **raw network fee** rather than `max(raw_fee, minimum_floor)`, so when the network reports an anomalously low fee, all withdrawal transactions are submitted below the relay threshold and never mined.

### Impact Explanation

Any pending `retrieve_doge_with_approval` request batched during a period of anomalously low reported fee percentiles will produce a signed Dogecoin transaction with a fee rate below the Dogecoin relay minimum (~1 koinu/byte). Dogecoin nodes will not relay or mine such transactions. The ckDOGE minter's RBF resubmission logic will then attempt to bump the fee by `MIN_RELAY_FEE_RATE_INCREASE` (100,000 millikoinus/byte) on top of the original near-zero rate, but if the original effective fee rate is stored as near-zero, the bump may still be insufficient. Users' ckDOGE is burned on the IC ledger but the corresponding DOGE is never delivered, causing permanent loss of access to funds until a governance-approved minter upgrade is deployed.

### Likelihood Explanation

The identical failure mode already occurred in production for ckBTC on 2025-06-21 (documented in `rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md`). The ckDOGE minter reuses the same shared `estimate_fee_per_vbyte` infrastructure from `ic_ckbtc_minter` but does not inherit the fix. The Dogecoin fee market is thinner and more volatile than Bitcoin's, making anomalously low fee percentile reports more likely. No attacker action is required; natural network conditions suffice, as proven by the ckBTC incident.

### Recommendation

Add a `minimum_fee_per_byte()` method to `DogecoinFeeEstimator` analogous to `BitcoinFeeEstimator::minimum_fee_per_vbyte()`, and apply it as a floor in `estimate_nth_fee()`:

```rust
const fn minimum_fee_per_byte(&self) -> FeeRate {
    let rate = match &self.network {
        Network::Mainnet => 100_000, // e.g. 100 koinu/byte in millikoinus
        Network::Regtest => 0,
    };
    FeeRate::from_millis_per_byte(rate)
}

fn estimate_nth_fee(&self, fee_percentiles: &[FeeRate], nth: usize) -> Option<FeeRate> {
    // ...
    Some(fee_percentiles[nth]).map(|f| f.max(self.minimum_fee_per_byte()))
}
```

The minimum value should be calibrated to Dogecoin's relay fee policy (currently 1 koinu/byte = 1,000 millikoinus/byte).

### Proof of Concept

1. The Dogecoin Bitcoin canister returns fee percentiles where all 100 entries are `FeeRate::from_millis_per_byte(50)` (0.05 koinu/byte, below relay minimum).
2. `DogecoinFeeEstimator::estimate_nth_fee(&percentiles, 50)` returns `Some(FeeRate(50))` with no floor applied. [1](#0-0) 
3. `estimate_fee_per_vbyte` stores this as `last_median_fee_per_vbyte = Some(FeeRate(50))`. [2](#0-1) 
4. A user calls `retrieve_doge_with_approval`; the minter builds and signs a Dogecoin transaction using `fee_rate = FeeRate(50)`.
5. The transaction is broadcast but rejected by all Dogecoin relay nodes as below the minimum relay fee.
6. The ckDOGE ledger burn is final; the user's DOGE is permanently inaccessible until a governance upgrade.

Contrast with the fixed ckBTC path, which clamps the fee: [3](#0-2) 

The production incident confirming this class of failure is real: [4](#0-3)

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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L245-249)
```rust
                    mutate_state(|s| {
                        s.last_fee_per_vbyte = fees;
                        s.last_median_fee_per_vbyte = Some(median_fee);
                        s.fee_based_retrieve_btc_min_amount = fee_based_retrieve_btc_min_amount;
                    });
```

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L80-112)
```rust
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
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L26-33)
```markdown
1. An extremely low fee per vbyte was chosen by the minter for those transactions, which prevented them from being mined
   in the first place. We currently don’t have a satisfying explanation for how this low median fee was computed and are
   also investigating the bitcoin canister. A stop-gap solution was introduced
   in [#5742](https://github.com/dfinity/ic/pull/5742), to ensure that the fee per vbyte computed by the minter is
   always at least 1.5 sats/vbyte (for Bitcoin Mainnet).
2. There is a deterministic panic occurring in the minter when it tries to resubmit those transactions, which explains
   why those transactions are currently stuck. This should be completely fixed
   by [#5713](https://github.com/dfinity/ic/pull/5713).
```
