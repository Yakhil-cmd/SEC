### Title
ckBTC Minter `min_confirmations` Cannot Be Increased via Upgrade — (`rs/bitcoin/ckbtc/minter/src/state.rs`)

---

### Summary

The ckBTC minter's `upgrade()` function silently discards any request to **increase** `min_confirmations`, only accepting decreases. All other security-relevant parameters (`check_fee`, `retrieve_btc_min_amount`, `deposit_btc_min_amount`, `mode`, `btc_checker_principal`) are freely settable in both directions via the same upgrade path. This creates an asymmetric missing-setter: the NNS can weaken the confirmation threshold but cannot strengthen it through the standard upgrade mechanism.

---

### Finding Description

In `CkBtcMinterState::upgrade()`, the `min_confirmations` field is guarded by a one-way check:

```rust
if let Some(min_conf) = min_confirmations {
    if min_conf < self.min_confirmations {
        self.min_confirmations = min_conf;
    } else {
        log!(
            Priority::Info,
            "Didn't increase min_confirmations to {} (current value: {})",
            min_conf,
            self.min_confirmations
        );
    }
}
``` [1](#0-0) 

Any `UpgradeArgs` payload with `min_confirmations` set to a value **greater than or equal to** the current value is silently dropped. The log line is the only observable effect.

By contrast, `reinit()` — called only during a full canister re-initialization — applies `min_confirmations` unconditionally:

```rust
if let Some(min_confirmations) = min_confirmations {
    self.min_confirmations = min_confirmations;
}
``` [2](#0-1) 

The `UpgradeArgs` type exposes `min_confirmations` as a first-class field, and the Candid interface documents it as a settable parameter: [3](#0-2) 

Yet the setter is effectively absent for the increase direction. All other mutable parameters in `UpgradeArgs` — `check_fee`, `retrieve_btc_min_amount`, `deposit_btc_min_amount`, `mode`, `btc_checker_principal`, `max_time_in_queue_nanos` — are applied unconditionally: [4](#0-3) 

The analog to the ToyBox report is exact: `discountTokens` is read but has no setter (discount is always 0); here `min_confirmations` is read in `update_balance` to gate UTXO acceptance but has no setter for the increase direction (the threshold can never be raised via upgrade).

`min_confirmations` is consumed in the deposit flow: [5](#0-4) 

---

### Impact Explanation

The ckBTC minter uses `min_confirmations` to decide whether a Bitcoin UTXO has enough on-chain depth before minting ckBTC. If the Bitcoin network experiences a deep reorganization or a sustained 51%-style attack, the NNS must be able to **raise** this threshold quickly to prevent double-spend mints. The one-way restriction means:

1. The NNS submits an upgrade proposal with `min_confirmations = 12` (raising from 4).
2. The upgrade executes successfully (no error, no trap).
3. `min_confirmations` remains at 4.
4. Deposits continue to be accepted at the lower threshold during the attack window.

The only remediation path is a full canister re-initialization (`MinterArg::Init`), which resets all minter state — pending requests, finalized transactions, UTXO maps — making it impractical on a live production canister holding real BTC value.

**Impact: Medium** — chain-fusion mint integrity; ckBTC conservation invariant at risk during a Bitcoin network security event.

---

### Likelihood Explanation

**Likelihood: Low** — A Bitcoin reorganization deep enough to require raising `min_confirmations` is rare. However:

- The NNS has already exercised this parameter twice on mainnet (12 → 6 in May 2024, 6 → 4 in January 2026), demonstrating it is a live operational control.
- The restriction is invisible to NNS proposal authors: the upgrade succeeds silently, giving false confidence that the threshold was raised.
- The asymmetry (decrease allowed, increase blocked) is the opposite of what a security-hardening policy would require. [6](#0-5) 

---

### Recommendation

Remove the one-way guard in `CkBtcMinterState::upgrade()` and allow `min_confirmations` to be set to any valid value, consistent with how all other `UpgradeArgs` fields are handled:

```rust
if let Some(min_conf) = min_confirmations {
    self.min_confirmations = min_conf;
}
```

If the intent is to prevent accidental security degradation, add an explicit validation in `validate_config()` with a minimum floor constant, rather than silently discarding increases. The current behavior violates the principle of least surprise: the Candid interface advertises `min_confirmations` as a settable upgrade parameter, but the implementation only honors half of the contract. [7](#0-6) 

---

### Proof of Concept

```
1. Deploy ckBTC minter with min_confirmations = 4 (current mainnet state).

2. NNS submits upgrade proposal:
   didc encode -d ckbtc_minter.did -t '(MinterArg)' \
     '(variant { Upgrade = opt record { min_confirmations = opt (12 : nat32) } })'

3. Proposal executes successfully (no error, no trap).

4. Query get_minter_info:
   → min_confirmations = 4   ← unchanged

5. A UTXO with 8 confirmations is accepted as valid (8 >= 4),
   even though the NNS intended to require 12.
```

The test `update_balance_should_return_correct_confirmations` in `rs/bitcoin/ckbtc/minter/tests/tests.rs` demonstrates the decrease path works (sets `min_confirmations = 3` from default 6); the increase path has no corresponding test because it silently no-ops. [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L437-439)
```rust
    /// The minimum number of confirmations on the Bitcoin chain.
    pub min_confirmations: u32,

```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L663-665)
```rust
        if let Some(min_confirmations) = min_confirmations {
            self.min_confirmations = min_confirmations;
        }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L705-714)
```rust
        if let Some(deposit_btc_min_amount) = deposit_btc_min_amount {
            self.deposit_btc_min_amount = deposit_btc_min_amount;
        }
        if let Some(retrieve_btc_min_amount) = retrieve_btc_min_amount {
            self.retrieve_btc_min_amount = retrieve_btc_min_amount;
            self.fee_based_retrieve_btc_min_amount = retrieve_btc_min_amount;
        }
        if let Some(max_time_in_queue_nanos) = max_time_in_queue_nanos {
            self.max_time_in_queue_nanos = max_time_in_queue_nanos;
        }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L715-726)
```rust
        if let Some(min_conf) = min_confirmations {
            if min_conf < self.min_confirmations {
                self.min_confirmations = min_conf;
            } else {
                log!(
                    Priority::Info,
                    "Didn't increase min_confirmations to {} (current value: {})",
                    min_conf,
                    self.min_confirmations
                );
            }
        }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L759-769)
```rust
    pub fn validate_config(&self) {
        if self.check_fee > self.retrieve_btc_min_amount {
            ic_cdk::trap("check_fee cannot be greater than retrieve_btc_min_amount");
        }
        if self.ecdsa_key_name.is_empty() {
            ic_cdk::trap("ecdsa_key_name is not set");
        }
        if self.btc_checker_principal.is_none() {
            ic_cdk::trap("Bitcoin checker principal is not set");
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L260-263)
```text
    /// The minimum number of confirmations required for the minter to
    /// accept a Bitcoin transaction.
    min_confirmations : opt nat32;

```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2026_01_23.md (L40-48)
```markdown
## Upgrade args

* Change the number of confirmations required by the minter to process a deposit and mint ckBTC to 4.
* Ensure that the deposit amount is at least 300 sats, which corresponds to the dust limit of the Bitcoin network for the type of addresses used for deposits (P2WPKH).

```
git fetch
git checkout b2d93fe83a8f878a331d73df1cffed72022860b2
didc encode -d rs/bitcoin/ckbtc/minter/ckbtc_minter.did -t '(MinterArg)' '(variant { Upgrade = opt record { deposit_btc_min_amount = opt (300 : nat64); min_confirmations = opt (4 : nat32); } })' | xxd -r -p | sha256sum
```

**File:** rs/bitcoin/ckbtc/minter/tests/tests.rs (L617-633)
```rust
#[test]
fn update_balance_should_return_correct_confirmations() {
    let ckbtc = CkBtcSetup::new();
    let upgrade_args = UpgradeArgs {
        min_confirmations: Some(3),
        ..Default::default()
    };
    let minter_arg = MinterArg::Upgrade(Some(upgrade_args));
    ckbtc
        .env
        .upgrade_canister(
            ckbtc.minter_id,
            minter_wasm(),
            Encode!(&minter_arg).unwrap(),
        )
        .expect("Failed to upgrade the minter canister");

```
