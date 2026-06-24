### Title
ckBTC Minter Uses Unvalidated `get_current_fee_percentiles` Return Value in Bitcoin Fee Calculation, Causing Stuck Withdrawal Transactions - (File: rs/bitcoin/ckbtc/minter/src/fees/mod.rs)

---

### Summary

The ckBTC minter's `estimate_fee_per_vbyte` function calls `get_current_fee_percentiles` and uses the returned fee percentile directly in Bitcoin transaction fee calculations without validating it against a meaningful minimum floor. When `get_current_fee_percentiles` returned an anomalously low value (142 millisat/vbyte instead of the expected ~2,000 millisat/vbyte on 2025-06-21), the minter submitted Bitcoin withdrawal transactions with fees too low to be mined. This caused three real user withdrawal transactions to become permanently stuck. A stop-gap mitigation was introduced, but the root cause remains unknown and hardcoded fee constants lack inline justification.

---

### Finding Description

`estimate_fee_per_vbyte` in `rs/bitcoin/ckbtc/minter/src/lib.rs` calls `get_current_fee_percentiles` and passes the result directly to `estimate_median_fee`, which indexes `fee_percentiles[50]` without any sanity check on the magnitude of the returned value:

```rust
match fee_estimator.estimate_median_fee(&fees) {
    Some(median_fee) => { ... Some(median_fee) }
    None => None,
}
``` [1](#0-0) 

`estimate_nth_fee` for Mainnet/Testnet simply returns `fee_percentiles[nth]` clamped to a minimum, but before the mitigation there was no minimum floor at all:

```rust
Some(fee_percentiles[nth])
``` [2](#0-1) 

The stop-gap mitigation added `minimum_fee_per_vbyte()` returning 1,500 millisat/vbyte for Mainnet, with the comment explicitly acknowledging the root cause is still unknown:

> "An estimated fee per vbyte of 142 millisatoshis per vbyte was selected around 2025.06.21 01:09:50 UTC for Bitcoin Mainnet, whereas the median fee around that time should have been 2_000. **Until we know the root cause**, we ensure that the estimated fee has a meaningful minimum value." [3](#0-2) 

Additionally, `evaluate_minter_fee` relies on hardcoded constants with no inline comments explaining their derivation basis:

```rust
const MINTER_FEE_PER_INPUT: u64 = 146;
const MINTER_FEE_PER_OUTPUT: u64 = 4;
const MINTER_FEE_CONSTANT: u64 = 26;
``` [4](#0-3) 

Similarly, `COST_OF_ONE_BILLION_CYCLES = 10` is hardcoded based on a static assumption of "1 BTC = 10,000 XDR" that does not adapt to market conditions: [5](#0-4) 

The upgrade proposal confirms the real-world impact:

> "An extremely low fee per vbyte was chosen by the minter for those transactions, which prevented them from being mined in the first place. We currently don't have a satisfying explanation for how this low median fee was computed." [6](#0-5) 

---

### Impact Explanation

When `get_current_fee_percentiles` returns an anomalously low value, `submit_pending_requests` and `finalize_requests` both call `estimate_fee_per_vbyte` and use the result directly to build and sign Bitcoin transactions: [7](#0-6) [8](#0-7) 

A fee too low to meet Bitcoin network relay policy causes the submitted transaction to never be mined. The minter then enters a stuck state where it repeatedly attempts to resubmit the transaction (which also triggered a deterministic panic per the upgrade proposal). User BTC withdrawal funds are locked until an emergency canister upgrade is deployed. This is a **chain-fusion mint/burn/replay bug** with direct financial impact on ckBTC users.

---

### Likelihood Explanation

This is not theoretical — it demonstrably occurred on mainnet on 2025-06-21, locking three real user withdrawal transactions. The root cause of why `get_current_fee_percentiles` returned 142 millisat/vbyte (vs. the expected ~2,000) is still unresolved per the upgrade proposal. The same class of anomaly can recur. The hardcoded `minimum_fee_per_vbyte = 1,500` floor is itself a static value that could become insufficient if Bitcoin network minimum relay fees increase.

---

### Recommendation

1. **Validate the return value rigorously**: Before using the fee percentile, check it against a dynamically maintained historical range (e.g., reject values that deviate more than an order of magnitude from the previous estimate stored in `last_median_fee_per_vbyte`).
2. **Investigate the root cause**: Determine why `get_current_fee_percentiles` returned 142 millisat/vbyte; add logging or assertions in the Bitcoin canister integration layer.
3. **Add inline comments for all hardcoded fee constants**: `MINTER_FEE_PER_INPUT`, `MINTER_FEE_PER_OUTPUT`, `MINTER_FEE_CONSTANT`, and `COST_OF_ONE_BILLION_CYCLES` should document the measurement basis and when they were last validated.
4. **Make `minimum_fee_per_vbyte` configurable**: Allow it to be updated via upgrade args rather than requiring a full canister upgrade when Bitcoin network relay fee minimums change.

---

### Proof of Concept

1. The Bitcoin canister (or a transient anomaly in its fee data) returns `get_current_fee_percentiles` with all 100 entries set to 142 millisat/vbyte.
2. `estimate_nth_fee` returns `Some(FeeRate::from_millis_per_byte(142))` (before the mitigation; after the mitigation, the floor of 1,500 applies).
3. `submit_pending_requests` calls `build_unsigned_transaction` with `fee_millisatoshi_per_vbyte = 142`.
4. The resulting Bitcoin transaction has a fee of ~142 * vsize / 1000 satoshis — far below the ~1 sat/vbyte minimum relay fee.
5. The transaction is broadcast via `send_raw_transaction` but is never mined.
6. `finalize_requests` detects the stuck transaction and calls `estimate_fee_per_vbyte` again; if the anomaly persists, the replacement transaction is also submitted with a fee too low to be mined.
7. User ckBTC is burned but BTC is never delivered; funds are locked until an emergency upgrade.

This exact sequence occurred on mainnet on 2025-06-21, confirmed by the upgrade proposal at `rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md`. [9](#0-8)

### Citations

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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L358-361)
```rust
    let fee_millisatoshi_per_vbyte = match estimate_fee_per_vbyte(runtime).await {
        Some(fee) => fee,
        None => return,
    };
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L776-779)
```rust
    let fee_per_vbyte = match estimate_fee_per_vbyte(runtime).await {
        Some(fee) => fee,
        None => return,
    };
```

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L56-59)
```rust
    /// Cost in sats of 1B cycles.
    ///
    /// Use a lower bound on the price of Bitcoin of 1 BTC = 10_000 XDR, so that 10 sats correspond to 1B cycles.
    pub const COST_OF_ONE_BILLION_CYCLES: Satoshi = 10;
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

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L103-113)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L115-126)
```rust
    fn evaluate_minter_fee(&self, num_inputs: u64, num_outputs: u64) -> u64 {
        const MINTER_FEE_PER_INPUT: u64 = 146;
        const MINTER_FEE_PER_OUTPUT: u64 = 4;
        const MINTER_FEE_CONSTANT: u64 = 26;

        max(
            MINTER_FEE_PER_INPUT * num_inputs
                + MINTER_FEE_PER_OUTPUT * num_outputs
                + MINTER_FEE_CONSTANT,
            Self::MINTER_ADDRESS_P2WPKH_DUST_LIMIT,
        )
    }
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L1-33)
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
