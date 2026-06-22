### Title
ckDOGE Minter Lacks Minimum Fee-Rate Floor in `DogecoinFeeEstimator`, Enabling Anomalously Low-Fee Withdrawal Transactions to Get Stuck — (`rs/dogecoin/ckdoge/minter/src/fees/mod.rs`)

---

### Summary

`DogecoinFeeEstimator::estimate_nth_fee` returns the raw fee-percentile value with no minimum floor. `BitcoinFeeEstimator::estimate_nth_fee` was patched after a confirmed mainnet incident (2025-06-21) to clamp the result to `minimum_fee_per_vbyte()`. The ckDOGE minter never received the equivalent fix. If the Dogecoin canister delivers anomalously low fee percentiles — exactly as the Bitcoin canister did — the ckDOGE minter will build and submit withdrawal transactions at a fee rate too low to be relayed or mined, locking user funds and the minter's UTXO set.

---

### Finding Description

`BitcoinFeeEstimator::estimate_nth_fee` applies a hard floor:

```rust
// rs/bitcoin/ckbtc/minter/src/fees/mod.rs  line 112
median_fee.map(|f| f.max(self.minimum_fee_per_vbyte()))
```

`minimum_fee_per_vbyte()` returns 1 500 ms/vbyte for Mainnet and 1 000 ms/vbyte for Testnet. The comment on that function explicitly records the incident:

> "An estimated fee per vbyte of 142 millisatoshis per vbyte was selected around 2025.06.21 01:09:50 UTC for Bitcoin Mainnet, whereas the median fee around that time should have been 2_000. Until we know the root cause, we ensure that the estimated fee has a meaningful minimum value."

`DogecoinFeeEstimator::estimate_nth_fee` has no such floor:

```rust
// rs/dogecoin/ckdoge/minter/src/fees/mod.rs  lines 53-66
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

The returned value is stored directly into `last_median_fee_per_vbyte` by the shared `estimate_fee_per_vbyte` path in `rs/bitcoin/ckbtc/minter/src/lib.rs` (lines 245-248) and is subsequently used verbatim when building every withdrawal transaction.

The upgrade proposal `rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md` confirms the Bitcoin incident caused three real ckBTC withdrawals to get stuck and required an emergency canister upgrade. The ckDOGE minter shares the same `estimate_fee_per_vbyte` / `build_unsigned_transaction` pipeline but was not given the same protection.

---

### Impact Explanation

When the Dogecoin canister returns anomalously low fee percentiles:

1. `DogecoinFeeEstimator::estimate_nth_fee` returns `Some(<very_low_rate>)` — it does not return `None`, so the minter does not skip the update.
2. `last_median_fee_per_vbyte` is overwritten with the low rate.
3. Every subsequent `retrieve_doge` call builds a transaction at that rate.
4. Submitted transactions are below the Dogecoin relay threshold and are never mined.
5. The UTXOs used by those transactions are locked in `submitted_transactions` and cannot be reused.
6. Users' ckDOGE is already burned on the ledger; their DOGE is not delivered. Funds are effectively frozen until an emergency upgrade is deployed.

---

### Likelihood Explanation

The Bitcoin canister delivered exactly this anomaly on 2025-06-21 (fee 142 ms/vbyte vs. expected ~2 000 ms/vbyte). The root cause is still described as unknown in the upgrade notes. The Dogecoin canister is a separate implementation that could exhibit the same or a different bug producing the same symptom. No privileged access or attacker is required; the trigger is a misbehaving on-chain canister that is already part of the normal execution path. The ckBTC fix was applied only to `BitcoinFeeEstimator`; the ckDOGE minter was not updated.

---

### Recommendation

Add a `minimum_fee_per_koinu_per_byte()` constant to `DogecoinFeeEstimator` (analogous to `BitcoinFeeEstimator::minimum_fee_per_vbyte`) and apply it in `estimate_nth_fee`:

```rust
fn estimate_nth_fee(&self, fee_percentiles: &[FeeRate], nth: usize) -> Option<FeeRate> {
    match &self.network {
        Network::Mainnet => {
            if fee_percentiles.len() < 100 || nth >= 100 {
                return None;
            }
            Some(fee_percentiles[nth].max(self.minimum_fee_per_koinu_per_byte()))
        }
        Network::Regtest => Some(DEFAULT_REGTEST_FEE),
    }
}
```

The minimum value should be calibrated to the Dogecoin relay policy (currently 1 koinu/byte = 1 000 millikoinus/byte).

---

### Proof of Concept

1. The Dogecoin canister returns 100 fee-percentile entries all set to `FeeRate::from_millis_per_byte(100)` (well below the relay threshold).
2. `estimate_fee_per_vbyte` calls `DogecoinFeeEstimator::estimate_median_fee(&fees)`.
3. `estimate_nth_fee(&fees, 50)` returns `Some(FeeRate::from_millis_per_byte(100))` — no floor is applied.
4. `last_median_fee_per_vbyte` is set to 100 millikoinus/byte.
5. A user calls `retrieve_doge`; the minter builds a transaction using `fee_millisatoshi_per_vbyte = 100`.
6. The transaction is broadcast but rejected by Dogecoin nodes as below the minimum relay fee.
7. The UTXOs are locked in `submitted_transactions`; the user's ckDOGE has already been burned; DOGE is not delivered.

Contrast: with `BitcoinFeeEstimator`, step 3 would return `Some(FeeRate::from_millis_per_byte(1_500))` due to the `f.max(self.minimum_fee_per_vbyte())` clamp, preventing the stuck-transaction scenario. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L235-253)
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
                }
                None => None,
            }
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L1-34)
```markdown
# Proposal to upgrade the ckBTC minter canister

Repository: `https://github.com/dfinity/ic.git`

Git hash: `47c5931cdafd82167feee85faf1e1dffa30fc3d8`

New compressed Wasm hash: `2c3aa7ce48ab9412a9189fea4758c8e4630fda4cc429ebf1a52b9aa09c5f5dbd`

Upgrade args hash: `0fee102bd16b053022b69f2c65fd5e2f41d150ce9c214ac8731cfaf496ebda4e`

Target canister: `mqygn-kiaaa-aaaar-qaadq-cai`

Previous ckBTC minter proposal: https://dashboard.internetcomputer.org/proposal/136598

---

## Motivation

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
