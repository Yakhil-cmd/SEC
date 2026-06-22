### Title
Hardcoded `minimum_fee_per_vbyte` Floor in ckBTC Minter Is Not Governance-Adjustable and May Diverge from Bitcoin Network Reality — (File: `rs/bitcoin/ckbtc/minter/src/fees/mod.rs`)

---

### Summary

The ckBTC minter contains a hardcoded minimum fee-per-vbyte floor (1,500 millisatoshis/vbyte for Mainnet) introduced as a stop-gap after an anomalous fee-reading incident on 2025-06-21. The root cause of that incident is explicitly acknowledged as unknown. Unlike every other operational parameter in the minter (`retrieve_btc_min_amount`, `check_fee`, `min_confirmations`, etc.), this floor has no corresponding field in `UpgradeArgs` and therefore cannot be adjusted via NNS governance proposal — only a full code upgrade can change it. If Bitcoin network fees legitimately fall below this floor, the minter will overpay transaction fees and artificially inflate the computed minimum withdrawal amount, blocking users from withdrawing amounts that would otherwise be valid.

---

### Finding Description

In `rs/bitcoin/ckbtc/minter/src/fees/mod.rs`, the `BitcoinFeeEstimator::minimum_fee_per_vbyte` method returns a compile-time constant:

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
``` [1](#0-0) 

This floor is applied unconditionally in `estimate_nth_fee`:

```rust
median_fee.map(|f| f.max(self.minimum_fee_per_vbyte()))
``` [2](#0-1) 

The floored fee then propagates into `fee_based_minimum_withdrawal_amount`, which computes the `fee_based_retrieve_btc_min_amount` stored in minter state and used to gate all withdrawal requests: [3](#0-2) 

The minter's `UpgradeArgs` struct exposes governance-adjustable knobs for `retrieve_btc_min_amount`, `check_fee`, `min_confirmations`, `deposit_btc_min_amount`, and others — but contains **no field** for `minimum_fee_per_vbyte`: [4](#0-3) 

The stop-gap was introduced in PR #5742 and deployed via the 2025-06-27 upgrade proposal after three ckBTC → BTC withdrawals became stuck because the minter chose an anomalously low fee (142 millisatoshis/vbyte instead of the expected ~2,000). The proposal explicitly states: *"We currently don't have a satisfying explanation for how this low median fee was computed."* [5](#0-4) 

This is structurally identical to the Infrared finding: a hardcoded protocol constant (`INITIAL_DEPOSIT = 32 ether`) that cannot be updated without a code change, introduced without full understanding of the underlying protocol's behavior, and which diverges from the external system's actual requirements.

---

### Impact Explanation

**Medium.** Two concrete user-facing impacts materialize when Bitcoin network fees fall below 1.5 sats/vbyte (which has occurred historically during low-activity periods):

1. **Artificially inflated minimum withdrawal amount.** `fee_based_minimum_withdrawal_amount` adds the floored fee contribution to `retrieve_btc_min_amount`. Users attempting to withdraw amounts that are valid under actual network conditions receive `AmountTooLow` rejections. Their ckBTC is burned on the ledger before the check, so failed withdrawals require reimbursement flows.

2. **Systematic fee overpayment.** Every BTC withdrawal transaction is built using the floored fee rate, causing users to pay more in Bitcoin transaction fees than the network requires. This is a direct financial loss to all ckBTC withdrawal users during low-fee periods.

Because the root cause of the anomalous fee reading is unresolved, the floor may remain in place indefinitely, and any future Bitcoin fee environment below 1.5 sats/vbyte will trigger both impacts for all users of the ckBTC chain-fusion bridge.

---

### Likelihood Explanation

**Low.** Bitcoin Mainnet fees have generally exceeded 1.5 sats/vbyte in recent years. However, the root cause of the anomalous fee reading is explicitly unresolved, meaning the floor is an open-ended stop-gap rather than a temporary measure with a known removal date. Historical Bitcoin fee data shows fees have dropped below 1 sat/vbyte during extended low-activity periods. The combination of an unknown root cause and a non-adjustable floor creates a persistent governance gap that will materialize whenever Bitcoin fees enter a low-fee regime.

---

### Recommendation

1. **Add `minimum_fee_per_vbyte_millis` to `UpgradeArgs`** (analogous to `retrieve_btc_min_amount` and `check_fee`) so the NNS can adjust the floor via governance proposal without a code upgrade, exactly as was done for `retrieve_btc_min_amount` after the hardcoded-value incident documented in the 2024-11-13 upgrade proposal.

2. **Investigate and resolve the root cause** of the anomalous fee reading (142 millisatoshis/vbyte on 2025-06-21) in the Bitcoin canister or fee-percentile pipeline before the floor becomes a permanent fixture.

3. **Document the floor as temporary** with a clear removal criterion (e.g., "remove once root cause is identified and fixed"), to prevent it from silently becoming a permanent protocol parameter.

---

### Proof of Concept

1. Bitcoin network enters a low-fee period; the Bitcoin canister returns fee percentiles all below 1,500 millisatoshis/vbyte (e.g., 500 millisatoshis/vbyte).
2. `estimate_nth_fee` is called; the actual 50th-percentile fee (500) is floored to 1,500 by `minimum_fee_per_vbyte()`.
3. `fee_based_minimum_withdrawal_amount` computes the minimum withdrawal using 1,500 instead of 500, producing a value ~3× higher than the network actually requires.
4. `mutate_state` stores this inflated value as `fee_based_retrieve_btc_min_amount`.
5. An unprivileged user calls `retrieve_btc` or `retrieve_btc_with_approval` with an amount that is valid under actual network fees but below the inflated minimum — the call is rejected.
6. Users who do successfully withdraw pay ~3× the necessary Bitcoin transaction fee, with no recourse until an NNS code upgrade is deployed.

The attacker-controlled entry path is a standard unprivileged `retrieve_btc` ingress call; no privileged access is required. The IC code path through `estimate_nth_fee` → `fee_based_minimum_withdrawal_amount` → `fee_based_retrieve_btc_min_amount` is the necessary vulnerable step. [6](#0-5) [7](#0-6)

### Citations

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

**File:** rs/bitcoin/ckbtc/minter/src/fees/mod.rs (L130-147)
```rust
    fn fee_based_minimum_withdrawal_amount(&self, median_fee_rate: FeeRate) -> Satoshi {
        match self.network {
            Network::Mainnet | Network::Testnet => {
                const PER_REQUEST_RBF_BOUND: u64 = 22_100;
                const PER_REQUEST_VSIZE_BOUND: u64 = 221;
                const PER_REQUEST_MINTER_FEE_BOUND: u64 = 305;

                ((PER_REQUEST_RBF_BOUND
                    + median_fee_rate.fee_ceil(PER_REQUEST_VSIZE_BOUND)
                    + PER_REQUEST_MINTER_FEE_BOUND
                    + self.check_fee)
                    / 50_000) //TODO DEFI-2187: adjust increment of minimum withdrawal amount to be a multiple of retrieve_btc_min_amount/2
                    * 50_000
                    + self.retrieve_btc_min_amount
            }
            Network::Regtest => self.retrieve_btc_min_amount,
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/lifecycle/upgrade.rs (L12-64)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Default, CandidType, Deserialize, Serialize)]
pub struct UpgradeArgs {
    /// Minimum amount of bitcoin that can be deposited
    #[serde(skip_serializing_if = "Option::is_none")]
    pub deposit_btc_min_amount: Option<u64>,

    /// Minimum amount of bitcoin that can be retrieved.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retrieve_btc_min_amount: Option<u64>,

    /// Specifies the minimum number of confirmations on the Bitcoin network
    /// required for the minter to accept a transaction.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub min_confirmations: Option<u32>,

    /// Maximum time in nanoseconds that a transaction should spend in the queue
    /// before being sent.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_time_in_queue_nanos: Option<u64>,

    /// The mode in which the minter is running.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mode: Option<Mode>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub check_fee: Option<u64>,

    #[serde(skip_serializing_if = "Option::is_none")]
    #[deprecated(note = "use check_fee instead")]
    pub kyt_fee: Option<u64>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub btc_checker_principal: Option<CanisterId>,

    /// The principal of the kyt canister.
    /// NOTE: this field is optional for backward compatibility.
    #[serde(skip_serializing_if = "Option::is_none")]
    #[deprecated(note = "use btc_checker_principal instead")]
    pub kyt_principal: Option<CanisterId>,

    /// The expiration duration (in seconds) for cached entries in
    /// the get_utxos cache.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub get_utxos_cache_expiration_seconds: Option<u64>,

    /// The minimum number of available UTXOs required to trigger a conslidation.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub utxo_consolidation_threshold: Option<u64>,

    /// The maximum number of input UTXOs allowed in a transaction.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_num_inputs_in_transaction: Option<u64>,
}
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L24-33)
```markdown
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

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L236-249)
```rust
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
```
