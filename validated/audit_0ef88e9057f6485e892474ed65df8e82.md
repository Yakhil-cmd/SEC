The code confirms all claims. The key facts are verified:

1. `DogecoinFeeEstimator::estimate_nth_fee` returns `Some(fee_percentiles[nth])` with no floor on Mainnet.
2. `BitcoinFeeEstimator::estimate_nth_fee` applies `.max(self.minimum_fee_per_vbyte())` (1,500 millisat/vbyte on Mainnet), explicitly documented as a fix for the 2025-06-21 incident.
3. Both share the same `estimate_fee_per_vbyte` infrastructure that stores the result into `last_median_fee_per_vbyte` and `fee_based_retrieve_btc_min_amount`.
4. `retrieve_doge_with_approval` and `estimate_withdrawal_fee` both consume `last_median_fee_per_vbyte` directly.

Audit Report

## Title
Missing Minimum Fee Rate Floor in `DogecoinFeeEstimator::estimate_nth_fee` Allows Near-Zero Fee Dogecoin Transactions - (File: `rs/dogecoin/ckdoge/minter/src/fees/mod.rs`)

## Summary
`DogecoinFeeEstimator::estimate_nth_fee` returns raw fee percentile values from the Dogecoin canister with no minimum floor, unlike `BitcoinFeeEstimator::estimate_nth_fee` which applies a hardcoded `minimum_fee_per_vbyte()` guard added after an anomalously-low fee event on 2025-06-21. During any period of anomalously low Dogecoin network fee percentiles, the ckDOGE minter will store a near-zero fee, build and sign Dogecoin withdrawal transactions with near-zero fees, and submit them to the Dogecoin network where they will not be relayed or confirmed — leaving users' ckDOGE burned on the IC ledger while their DOGE withdrawal is stuck.

## Finding Description
In `rs/bitcoin/ckbtc/minter/src/fees/mod.rs`, `BitcoinFeeEstimator::estimate_nth_fee` applies a minimum floor:

```rust
median_fee.map(|f| f.max(self.minimum_fee_per_vbyte()))
```

where `minimum_fee_per_vbyte()` returns 1,500 millisat/vbyte on Mainnet, with an explicit comment documenting the 2025-06-21 incident as motivation.

In `rs/dogecoin/ckdoge/minter/src/fees/mod.rs`, `DogecoinFeeEstimator::estimate_nth_fee` has no equivalent:

```rust
Network::Mainnet => {
    if fee_percentiles.len() < 100 || nth >= 100 {
        return None;
    }
    Some(fee_percentiles[nth])  // no floor
}
```

The shared `estimate_fee_per_vbyte` function in `rs/bitcoin/ckbtc/minter/src/lib.rs` (lines 235–250) stores the result directly:

```rust
s.last_median_fee_per_vbyte = Some(median_fee);
s.fee_based_retrieve_btc_min_amount = fee_based_retrieve_btc_min_amount;
```

Both `estimate_withdrawal_fee` (query, `main.rs` L111) and the withdrawal transaction builder read `last_median_fee_per_vbyte` directly. `evaluate_transaction_fee` in `fees/mod.rs` (L118–120) computes `fee_rate.fee_ceil(tx_size)`, which yields 0 koinu when `fee_rate` is zero. The transaction is then signed (consuming ~29B cycles per input) and submitted to the Dogecoin network, which requires at least 1 koinu/byte to relay.

The RBF resubmission path (`MIN_RELAY_FEE_RATE_INCREASE = 100_000 millikoinu/byte`) will eventually bump the fee to a relayable level after `MIN_RESUBMISSION_DELAY`, but only if the anomalous fee condition has resolved; during the anomalous window, every new timer tick re-stores the near-zero fee, and new withdrawal requests continue to be processed at zero fee.

## Impact Explanation
This is a significant ck-token security impact with concrete user harm: users' ckDOGE is burned on the IC ledger while their DOGE withdrawal is temporarily stuck due to unrelayable zero-fee transactions. The minter wastes expensive threshold ECDSA signing cycles (~29B cycles per input) on transactions that will not confirm. This maps to **High ($2,000–$10,000)**: significant Chain Fusion / ck-token security impact with concrete user and protocol harm.

## Likelihood Explanation
The ckBTC minter already experienced this exact failure mode on 2025-06-21, as documented in the source code comment at `rs/bitcoin/ckbtc/minter/src/fees/mod.rs` L77–79. The ckDOGE minter shares the same fee-refresh infrastructure and is equally susceptible. No special privileges are required — any unprivileged user calling `retrieve_doge_with_approval` during an anomalous fee window triggers the impact. The condition is externally observable (Dogecoin canister fee percentiles) and not under the attacker's control, but the ckBTC precedent demonstrates it is a realistic, recurring risk.

## Recommendation
Add a `minimum_fee_per_byte()` method to `DogecoinFeeEstimator` and apply it in `estimate_nth_fee`:

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

The minimum should be at least the Dogecoin relay fee threshold (1 koinu/byte = 1,000,000 millikoinu/byte), analogous to the fix already applied to `BitcoinFeeEstimator`.

## Proof of Concept
1. The Dogecoin canister returns 100 fee percentiles all set to `FeeRate::from_millis_per_byte(0)`.
2. The ckDOGE minter timer fires `estimate_fee_per_vbyte`, calling `fee_estimator.estimate_median_fee(&fees)` → `estimate_nth_fee(&fees, 50)`.
3. `DogecoinFeeEstimator::estimate_nth_fee` returns `Some(FeeRate::from_millis_per_byte(0))` — no floor applied (`rs/dogecoin/ckdoge/minter/src/fees/mod.rs` L62).
4. `last_median_fee_per_vbyte` is set to `FeeRate(0)` and `fee_based_retrieve_btc_min_amount` is set to the base minimum (`rs/bitcoin/ckbtc/minter/src/lib.rs` L247–248).
5. A user calls `retrieve_doge_with_approval` with the minimum amount; ckDOGE is burned and the withdrawal is queued.
6. The minter calls `evaluate_transaction_fee(tx, FeeRate(0))` = 0 koinu fee (`rs/dogecoin/ckdoge/minter/src/fees/mod.rs` L118–120).
7. The transaction is signed (consuming ~29B cycles × num_inputs) and submitted to the Dogecoin network.
8. The Dogecoin network rejects the zero-fee transaction; the user's ckDOGE is burned but DOGE is not received until RBF resubmission after `MIN_RESUBMISSION_DELAY`.

A unit test can reproduce this by constructing a `DogecoinFeeEstimator` with `Network::Mainnet`, passing a 100-element `fee_percentiles` slice of all zeros to `estimate_nth_fee`, and asserting the returned `FeeRate` is greater than zero — which currently fails.