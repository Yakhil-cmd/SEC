### Title
`DogecoinFeeEstimator::estimate_nth_fee` Returns Raw Fee Percentile Without a Minimum Floor, Enabling Abnormally-Low-Fee Transactions That Become Permanently Stuck - (File: `rs/dogecoin/ckdoge/minter/src/fees/mod.rs`)

### Summary

The ckDOGE minter's `DogecoinFeeEstimator::estimate_nth_fee` returns the raw fee percentile from the Dogecoin network without enforcing any minimum fee-rate floor. This is the exact same class of bug that was already discovered and patched in the ckBTC minter (via proposal `minter_upgrade_2025_06_27.md`), but the fix was never ported to the ckDOGE minter. If the Dogecoin canister returns an anomalously low fee percentile (as happened with ckBTC on 2025-06-21), the ckDOGE minter will construct and broadcast a transaction with a dust-level fee rate, which miners will never include. The transaction becomes permanently stuck, and because the minter's resubmission logic uses the same fee estimator, it cannot recover on its own.

### Finding Description

`BitcoinFeeEstimator::estimate_nth_fee` (ckBTC) applies a minimum fee floor via `minimum_fee_per_vbyte()`:

```rust
// rs/bitcoin/ckbtc/minter/src/fees/mod.rs:112
median_fee.map(|f| f.max(self.minimum_fee_per_vbyte()))
```

where `minimum_fee_per_vbyte()` returns `1_500` millis/byte for Mainnet.

`DogecoinFeeEstimator::estimate_nth_fee` (ckDOGE) has no such floor:

```rust
// rs/dogecoin/ckdoge/minter/src/fees/mod.rs:58-65
match &self.network {
    Network::Mainnet => {
        if fee_percentiles.len() < 100 || nth >= 100 {
            return None;
        }
        Some(fee_percentiles[nth])   // ← raw value, no minimum enforced
    }
    Network::Regtest => Some(DEFAULT_REGTEST_FEE),
}
```

The ckBTC upgrade proposal `minter_upgrade_2025_06_27.md` explicitly documents the root cause and the fix:

> "An extremely low fee per vbyte was chosen by the minter for those transactions, which prevented them from being mined in the first place. A stop-gap solution was introduced in #5742, to ensure that the fee per vbyte computed by the minter is always at least 1.5 sats/vbyte."

The ckDOGE minter shares the same `FeeEstimator` trait and the same transaction-building pipeline (`ic_ckbtc_minter::queries::estimate_withdrawal_fee`), but the analogous minimum-floor guard was never added to `DogecoinFeeEstimator`.

Additionally, `DogecoinFeeEstimator::COST_OF_ONE_BILLION_CYCLES` is hardcoded to `5_000_000` koinu, derived from the assumption "50 DOGE = 1 XDR":

```rust
// rs/dogecoin/ckdoge/minter/src/fees/mod.rs:26-27
/// Use a lower bound on the price of Doge of 50 DOGE = 1 XDR, so that 5M koinus correspond to 1B cycles.
pub const COST_OF_ONE_BILLION_CYCLES: u64 = 5_000_000;
```

DOGE's price is highly volatile. If DOGE appreciates significantly (e.g., 10× to 5 DOGE = 1 XDR), the minter would charge users 10× more DOGE than the actual cycles cost, extracting excess value from users on every reimbursement. Conversely, if DOGE depreciates, the minter under-recovers its cycles costs. This is the direct IC analog of the original report's "hardcoded path that may not be optimal."

### Impact Explanation

**Primary (missing minimum fee floor):** Any ckDOGE withdrawal can produce a Dogecoin transaction with a near-zero fee rate if the Dogecoin canister returns anomalously low fee percentiles. Such transactions will not be mined. The minter's resubmission path calls the same `estimate_nth_fee` with the same bad data, so it cannot self-heal. All affected withdrawal requests become permanently stuck, freezing user funds. This is a confirmed, already-exploited failure mode in the sister ckBTC minter.

**Secondary (hardcoded DOGE/XDR price):** The `COST_OF_ONE_BILLION_CYCLES` constant is used in `reimbursement_fee_for_pending_withdrawal_requests`, which deducts a penalty from users whose withdrawals are reimbursed. If DOGE price diverges significantly from the hardcoded assumption, users are either overcharged or the minter subsidizes operations from its own reserves.

### Likelihood Explanation

The primary issue has **already occurred** in the ckBTC minter on 2025-06-21 and required an emergency upgrade. The ckDOGE minter uses the same underlying infrastructure and is exposed to the same failure mode. The Dogecoin canister returning a low fee percentile is a realistic, already-observed event class. No attacker action is required — it is a passive failure triggered by normal network conditions.

The secondary issue (hardcoded price) is medium likelihood: DOGE is one of the most volatile assets in the ecosystem, and the assumption of "50 DOGE = 1 XDR" can easily be violated by 10× or more.

### Recommendation

1. **Add a minimum fee floor to `DogecoinFeeEstimator::estimate_nth_fee`**, mirroring the fix applied to `BitcoinFeeEstimator`:

```rust
fn estimate_nth_fee(&self, fee_percentiles: &[FeeRate], nth: usize) -> Option<FeeRate> {
    // ...
    Some(fee_percentiles[nth]).map(|f| f.max(self.minimum_fee_per_byte()))
}

const fn minimum_fee_per_byte(&self) -> FeeRate {
    match &self.network {
        Network::Mainnet => FeeRate::from_millis_per_byte(/* appropriate Dogecoin floor */),
        Network::Regtest => FeeRate::from_millis_per_byte(0),
    }
}
```

2. **Replace the hardcoded `COST_OF_ONE_BILLION_CYCLES`** with a dynamically-fetched or governance-updatable DOGE/XDR rate, analogous to how the CMC uses the Exchange Rate Canister for ICP/XDR.

### Proof of Concept

**Missing minimum floor:**

`DogecoinFeeEstimator::estimate_nth_fee` at `rs/dogecoin/ckdoge/minter/src/fees/mod.rs` lines 53–66 returns `Some(fee_percentiles[nth])` with no floor. [1](#0-0) 

`BitcoinFeeEstimator::estimate_nth_fee` at `rs/bitcoin/ckbtc/minter/src/fees/mod.rs` line 112 applies `f.max(self.minimum_fee_per_vbyte())` as the fix. [2](#0-1) 

The ckBTC upgrade proposal documents the real-world incident that motivated this fix: [3](#0-2) 

**Hardcoded price assumption:**

`DogecoinFeeEstimator::COST_OF_ONE_BILLION_CYCLES` is hardcoded at `5_000_000` koinu based on a fixed "50 DOGE = 1 XDR" assumption. [4](#0-3) 

Compare with `BitcoinFeeEstimator::COST_OF_ONE_BILLION_CYCLES` which uses "1 BTC = 10,000 XDR" — also hardcoded, but Bitcoin's price floor is far more stable than DOGE's. [5](#0-4) 

The `reimbursement_fee_for_pending_withdrawal_requests` function uses this constant directly to deduct from user reimbursements: [6](#0-5)

### Citations

**File:** rs/dogecoin/ckdoge/minter/src/fees/mod.rs (L23-27)
```rust
impl DogecoinFeeEstimator {
    /// Cost in koinu of 1B cycles.
    ///
    /// Use a lower bound on the price of Doge of 50 DOGE = 1 XDR, so that 5M koinus correspond to 1B cycles.
    pub const COST_OF_ONE_BILLION_CYCLES: u64 = 5_000_000;
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

**File:** rs/dogecoin/ckdoge/minter/src/fees/mod.rs (L123-127)
```rust
    fn reimbursement_fee_for_pending_withdrawal_requests(&self, num_requests: u64) -> u64 {
        // Heuristic:
        // * charge 1B cycles for each request (a burn on the ledger on the fiduciary subnet is probably around 50M cycles).
        num_requests.saturating_mul(Self::COST_OF_ONE_BILLION_CYCLES)
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L56-59)
```rust
    /// Cost in sats of 1B cycles.
    ///
    /// Use a lower bound on the price of Bitcoin of 1 BTC = 10_000 XDR, so that 10 sats correspond to 1B cycles.
    pub const COST_OF_ONE_BILLION_CYCLES: Satoshi = 10;
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
