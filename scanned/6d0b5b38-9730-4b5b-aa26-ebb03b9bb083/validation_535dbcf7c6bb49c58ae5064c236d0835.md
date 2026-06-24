### Title
ckBTC Minter Withdrawal Permanently Bricked by Anomalous Fee Estimation and Deterministic Panic in Resubmission Logic - (`rs/bitcoin/ckbtc/minter/src/lib.rs`, `rs/bitcoin/ckbtc/minter/src/fees/mod.rs`)

---

### Summary

The ckBTC minter's `retrieve_btc` / `retrieve_btc_with_approval` withdrawal path has a single-path dependency on correct fee estimation from the Bitcoin canister. When the Bitcoin canister returned an anomalously low fee (142 millis/vbyte instead of ~2000) on 2025-06-21, submitted transactions were never mined. When the minter's timer then attempted to resubmit those stuck transactions, a **deterministic panic** occurred on every replica, permanently bricking all ckBTC → BTC withdrawals until an emergency governance upgrade was executed. There is no fallback withdrawal mechanism. The root cause of the anomalous fee computation remains unknown. Additionally, the ckDOGE minter (`rs/dogecoin/ckdoge/minter/src/fees/mod.rs`) lacks even the stop-gap minimum-fee-floor protection that was added to ckBTC after this incident.

---

### Finding Description

**Single-path withdrawal dependency in ckBTC minter:**

The `estimate_fee_per_vbyte` function queries the Bitcoin canister for fee percentiles and uses the median value directly: [1](#0-0) 

If the Bitcoin canister returns anomalously low percentiles, the minter uses a fee that is too low for transactions to be mined. The `resubmit_transactions` function is the only recovery path: [2](#0-1) 

When the resubmission logic panicked deterministically (PR #5713 fix), every replica hit the same trap on every timer tick, permanently halting all ckBTC → BTC withdrawals. This was confirmed in the mainnet upgrade proposal: [3](#0-2) 

The stop-gap fix added a hardcoded minimum fee floor of 1.5 sats/vbyte for mainnet: [4](#0-3) 

The comment explicitly acknowledges the root cause is still unknown: [5](#0-4) 

**ckDOGE minter lacks the minimum-fee-floor protection entirely:**

The `DogecoinFeeEstimator::estimate_nth_fee` returns the raw percentile value with no floor: [6](#0-5) 

Unlike `BitcoinFeeEstimator`, there is no `minimum_fee_per_vbyte()` call, leaving ckDOGE → DOGE withdrawals exposed to the same class of failure.

**No fallback withdrawal mechanism exists** in either minter. There is no alternative path (analogous to the Uniswap swap fallback recommended in H-07) to complete a withdrawal when the primary Bitcoin/Dogecoin transaction path is unavailable.

---

### Impact Explanation

- All pending `retrieve_btc` / `retrieve_btc_with_approval` requests are permanently stuck until a governance upgrade is executed.
- Users who have already burned ckBTC (ledger burn is committed before the Bitcoin transaction is sent) cannot recover their funds without an emergency canister upgrade.
- The `reimbursement_account` mechanism exists but only triggers after a transaction is finalized or explicitly cancelled — it cannot fire while the minter is panicking on every timer tick.
- For ckDOGE, the same scenario can occur with no stop-gap protection at all.

---

### Likelihood Explanation

- This vulnerability **already occurred in production** on 2025-06-21, bricking three ckBTC → BTC withdrawals and requiring an emergency governance proposal.
- The root cause of the anomalous fee computation is explicitly stated as still unknown.
- The minimum fee floor (1.5 sats/vbyte) is a hardcoded stop-gap; if the underlying bug produces a fee above this floor but still too low for mempool acceptance, the issue recurs.
- The ckDOGE minter has zero protection and is reachable by any user calling `retrieve_doge` / `retrieve_doge_with_approval`.
- Entry path: any unprivileged user calling `retrieve_btc_with_approval` or the ckDOGE equivalent triggers the withdrawal pipeline; the vulnerability manifests in the minter's background timer logic.

---

### Recommendation

1. **Identify and fix the root cause** of the anomalous fee computation in the Bitcoin canister rather than relying on a hardcoded floor.
2. **Add a fallback withdrawal mechanism**: if a submitted transaction remains unconfirmed beyond a configurable threshold and cannot be resubmitted (e.g., fees are too high to cover), automatically reimburse the user's ckBTC/ckDOGE rather than leaving the request permanently stuck.
3. **Apply the minimum-fee-floor protection to ckDOGE** (`DogecoinFeeEstimator::estimate_nth_fee`) immediately, mirroring the ckBTC stop-gap.
4. **Harden the resubmission logic** against all panic paths so that a single stuck transaction cannot halt the entire minter timer.

---

### Proof of Concept

1. User calls `retrieve_btc_with_approval` — ckBTC is burned on the ledger (irreversible at this point).
2. The minter's timer calls `estimate_fee_per_vbyte`, which queries the Bitcoin canister and receives anomalously low percentiles (e.g., 142 millis/vbyte).
3. `estimate_nth_fee` returns this value (currently floored at 1500 millis/vbyte for ckBTC, but not for ckDOGE).
4. The minter builds and submits a Bitcoin transaction with a fee too low for mempool acceptance.
5. On the next timer tick, `finalize_requests` detects the transaction as stuck and calls `resubmit_transactions`.
6. If a panic occurs in `resubmit_transactions` (as happened on 2025-06-21), every replica traps deterministically on every subsequent timer tick.
7. All ckBTC → BTC withdrawals are permanently stuck; users cannot recover funds without a governance upgrade. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L801-816)
```rust
pub async fn resubmit_transactions<
    R: CanisterRuntime,
    G: Fn(Txid, state::SubmittedBtcTransaction, state::eventlog::ReplacedReason),
    Fee: FeeEstimator,
>(
    key_name: &str,
    fee_rate: FeeRate,
    main_address: BitcoinAddress,
    ecdsa_public_key: ECDSAPublicKey,
    btc_network: Network,
    retrieve_btc_min_amount: u64,
    transactions: BTreeMap<Txid, state::SubmittedBtcTransaction>,
    replace_transaction: G,
    runtime: &R,
    fee_estimator: &Fee,
) {
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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L940-952)
```rust
        let (unsigned_tx, change_output, total_fee) = match build_result {
            Ok(tx) => tx,
            // If it's impossible to build a new transaction, the fees probably became too high.
            // Let's ignore this transaction and wait for fees to go down.
            Err(err) => {
                log!(
                    Priority::Debug,
                    "[resubmit_transactions]: failed to rebuild stuck transaction {}: {:?}",
                    &submitted_tx.txid,
                    err
                );
                continue;
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

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L205-237)
```rust
pub fn replace_transaction<R: CanisterRuntime>(
    state: &mut CkBtcMinterState,
    old_txid: Txid,
    new_tx: SubmittedBtcTransaction,
    reason: ReplacedReason,
    runtime: &R,
) {
    // when reason is ToCancel, the utxos of new_tx has to be persisted,
    // because it is different than that of old_tx.
    let new_utxos = match reason {
        ReplacedReason::ToCancel { .. } => Some(new_tx.used_utxos.clone()),
        ReplacedReason::ToRetry => None,
    };
    record_event(
        EventType::ReplacedBtcTransaction {
            old_txid,
            new_txid: new_tx.txid,
            change_output: new_tx
                .change_output
                .clone()
                .expect("bug: all replacement transactions must have the change output"),
            submitted_at: new_tx.submitted_at,
            effective_fee_per_vbyte: new_tx
                .effective_fee_per_vbyte
                .expect("bug: all replacement transactions must have the fee")
                .millis(),
            withdrawal_fee: new_tx.withdrawal_fee,
            reason: Some(reason),
            new_utxos,
        },
        runtime,
    );
    state.replace_transaction(&old_txid, new_tx);
```
