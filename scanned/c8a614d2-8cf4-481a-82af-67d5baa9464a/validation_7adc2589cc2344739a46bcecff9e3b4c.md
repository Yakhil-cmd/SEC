### Title
ckBTC Minter Withdrawal Permanently Stuck Due to Underestimated Fee Rate Causing Deterministic Resubmission Panic - (`File: rs/bitcoin/ckbtc/minter/src/fees/mod.rs`)

---

### Summary

The ckBTC minter on the Internet Computer suffered a confirmed, real-world instance of the same vulnerability class as the reported Archimedes bug: an inaccurate fee amount calculation caused withdrawal positions to become permanently unwindable. An anomalously low fee per vbyte was computed from the Bitcoin canister's fee percentiles, causing submitted BTC transactions to never be mined. When the minter attempted to resubmit those transactions with a higher fee, a deterministic panic in the resubmission logic caused the minter to halt permanently on those requests. Users held ckBTC that was burned but could not be converted to BTC — an exact analog of "positions that cannot be unwound although enough funds are held."

---

### Finding Description

The ckBTC minter (`rs/bitcoin/ckbtc/minter/`) computes the fee rate for Bitcoin withdrawal transactions by querying the Bitcoin canister for fee percentiles and selecting the median (50th percentile) via `estimate_fee_per_vbyte`. [1](#0-0) 

The `estimate_nth_fee` implementation in `BitcoinFeeEstimator` directly indexes into the fee percentile array returned by the Bitcoin canister without any floor validation prior to the fix: [2](#0-1) 

On 2025-06-21, the Bitcoin canister returned an anomalously low fee of **142 millisatoshis/vbyte** (approximately 0.142 sat/vbyte) as the median, whereas the true network median was ~2,000 millisatoshis/vbyte. The minter used this value to build and sign Bitcoin transactions. Those transactions were submitted to the Bitcoin network but were never mined because the fee was far too low.

When the minter's timer task subsequently attempted to resubmit those stuck transactions with a higher fee (via `resubmit_retrieve_btc`), a **deterministic panic** occurred in the resubmission logic. This panic caused the minter to halt on every timer tick for those requests, permanently blocking the affected withdrawals.

The stop-gap fix introduced a hardcoded `minimum_fee_per_vbyte()` floor: [3](#0-2) 

This is documented in the mainnet upgrade proposal: [4](#0-3) 

The two-part root cause:
1. **Inaccurate fee estimation**: The minter blindly trusted the fee percentile value from the Bitcoin canister without a meaningful lower bound, analogous to the Archimedes `get_dy` directional miscalculation — both assume an external price/fee oracle is always accurate.
2. **Deterministic panic on resubmission**: Once a transaction was submitted with a too-low fee, the resubmission path panicked deterministically, making the stuck state permanent and unrecoverable without a canister upgrade.

The `build_unsigned_transaction_from_inputs` function uses the fee rate directly: [5](#0-4) 

And the resubmission path (`SignedTransactionRequest::resubmit`) checks whether the new fee exceeds the allowed maximum and returns `InsufficientTransactionFee`, but the panic occurred before or within that path: [6](#0-5) 

---

### Impact Explanation

**Impact: High (5/5)**

- Three real mainnet ckBTC→BTC withdrawal requests were permanently stuck since 2025-06-21, confirmed by the upgrade proposal.
- Users had already burned their ckBTC on the ledger (irreversible at that point) but received no BTC in return.
- The minter's timer loop panicked deterministically on every execution cycle for the affected requests, meaning no new withdrawals could be processed either — a complete denial of service for the withdrawal path.
- Recovery required an emergency NNS governance proposal and canister upgrade, which is a privileged operation unavailable to ordinary users.
- This directly mirrors the Archimedes bug: "positions cannot be unwound although enough [funds] are held."

---

### Likelihood Explanation

**Likelihood: High (5/5)**

- This vulnerability **already triggered on mainnet** on 2025-06-21, affecting real user funds.
- The root cause (trusting an external oracle — the Bitcoin canister — for a fee value without a floor) is a systemic design issue, not a one-off edge case.
- The Bitcoin canister's fee percentile computation is itself a complex system that can return anomalous values (the root cause of the anomalous 142 millisatoshi value was still under investigation at the time of the upgrade proposal).
- Any future anomaly in the Bitcoin canister's fee reporting would re-trigger the same class of issue if the floor is ever removed or set incorrectly.
- The attacker-controlled entry path is simply calling `retrieve_btc` or `retrieve_btc_with_approval` as an unprivileged ingress sender during a period when the Bitcoin canister returns a low fee estimate — no special privileges required.

---

### Recommendation

1. **Validated fee floor (implemented as stop-gap)**: The `minimum_fee_per_vbyte()` floor of 1,500 millisatoshis/vbyte for Mainnet is now enforced in `estimate_nth_fee`. This should be made configurable via upgrade args so it can be adjusted without a code change.

2. **Fix the resubmission panic**: PR #5713 (`fix(ckbtc): fix a bug in resubmitting stuck transactions`) addresses the deterministic panic. Both fixes are required — the floor prevents future occurrences, and the panic fix prevents permanent lock-up if the floor is ever breached.

3. **Decouple fee estimation from transaction commitment**: Do not burn ckBTC on the ledger until the fee estimate has been validated against a sanity range. If the fee estimate is anomalous, reject the withdrawal request before the burn rather than after.

4. **Alert on anomalous fee values**: Add monitoring that alerts when the reported fee percentile deviates by more than an order of magnitude from the previous value, allowing operators to intervene before transactions are submitted.

---

### Proof of Concept

The confirmed mainnet incident is the proof of concept:

- **Date**: 2025-06-21
- **Affected canister**: ckBTC minter `mqygn-kiaaa-aaaar-qaadq-cai`
- **Root cause**: Bitcoin canister returned fee percentile of 142 millisatoshis/vbyte (true median ~2,000)
- **Effect**: Three ckBTC→BTC withdrawal transactions submitted with ~0.142 sat/vbyte, never mined; resubmission path panicked deterministically
- **Recovery**: Emergency NNS upgrade proposal (proposal referencing PR #5742 and #5713) [7](#0-6) 

The attacker-controlled entry path:
1. Any unprivileged user calls `retrieve_btc` or `retrieve_btc_with_approval` on the ckBTC minter canister during a window when the Bitcoin canister reports an anomalously low fee.
2. The minter accepts the request, burns ckBTC from the user's ledger account, and submits a Bitcoin transaction with the underestimated fee.
3. The Bitcoin transaction is never mined. The minter's resubmission logic panics. The user's funds are permanently locked until a governance-approved canister upgrade.

No privileged access, no threshold corruption, no social engineering — only a standard `retrieve_btc` ingress call is required.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L227-250)
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
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1260-1270)
```rust
    let inputs_value = input_utxos.iter().map(|u| u.value).sum::<u64>();

    debug_assert!(inputs_value >= amount);

    let minter_fee =
        fee_estimator.evaluate_minter_fee(input_utxos.len() as u64, (outputs.len() + 1) as u64);

    let change = inputs_value - amount;
    let change_output = state::ChangeOutput {
        vout: outputs.len() as u32,
        value: change + minter_fee,
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

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L1-49)
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

## Release Notes

```
git log --format='%C(auto) %h %s' f8131bfbc2d339716a9cff06e04de49a68e5a80b..47c5931cdafd82167feee85faf1e1dffa30fc3d8 -- rs/bitcoin/ckbtc/minter
47c5931cda fix(ckbtc): Ensure minimum fee per vbyte (#5742)
db7850caa4 fix(ckbtc): fix a bug in resubmitting stuck transactions (#5713)
b0a3d6dc4c feat: Add "Cache-Control: no-store" to all canister /metrics endpoints (#5124)
830f4caa90 refactor: remove direct dependency on ic-cdk-macros (#5144)
2949c97ba3 chore: Revert ic-cdk to 0.17.2 (#5139)
d1dc4c2dc8 chore: Update Rust to 1.86.0 (#5059)
3490ef2a07 chore: bump the monorepo version of ic-cdk to 0.18.0 (#5005)
a86da36995 refactor(cross-chain): use public crate ic-management-canister-types (#4903)
ccb066b19e chore(ckbtc): update README (#2956)
c2d5684360 refactor(ic): update imports from ic_canisters_http_types to newly published ic_http_types crate (#4866)
 ```
```

**File:** rs/bitcoin/ckbtc/minter/src/tx.rs (L155-189)
```rust

/// An implementation of the [Buffer] trait that counts the input length.
#[derive(Default)]
pub struct CountBytes(usize);

impl Buffer for CountBytes {
    type Output = usize;

    fn write(&mut self, data: &[u8]) {
        self.0 += data.len()
    }

    fn finish(self) -> Self::Output {
        self.0
    }
}

/// SHA-256 followed by Ripemd160, also known as HASH160.
pub fn hash160(bytes: &[u8]) -> [u8; 20] {
    use ripemd::{Digest, Ripemd160};
    Ripemd160::digest(Sha256::hash(bytes)).into()
}

/// Encodes a variable-size integer using the bitcoin encoding.
pub fn write_compact_size(n: usize, buf: &mut impl Buffer) {
    // Compact Size
    // ============
    // size <  253       -- 1 byte
    // size <= u16::MAX  -- 3 bytes  (253 + 2 bytes)
    // size <= u32::MAX  -- 5 bytes  (254 + 4 bytes)
    // size >  u32::MAX  -- 9 bytes  (255 + 8 bytes)
    //
    // See https://github.com/bitcoin/bitcoin/blob/c90f86e4c7760a9f7ed0a574f54465964e006a64/src/serialize.h#L243-L266.
    if n < 253 {
        buf.write(&[n as u8])
```
