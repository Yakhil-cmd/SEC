### Title
ckBTC Minter `upgrade()` Silently Ignores Governance-Approved Increases to `min_confirmations` - (File: `rs/bitcoin/ckbtc/minter/src/state.rs`)

---

### Summary

The `CkBtcMinterState::upgrade()` function in the ckBTC minter silently discards any NNS governance-approved attempt to **increase** `min_confirmations` via a canister upgrade. This is a direct analog to the ClaggSyncAdapter bug: just as that adapter's `migrate()` could not set an empty staking address (preventing disabling of staking), the ckBTC minter's `upgrade()` cannot increase `min_confirmations`, permanently preventing governance from tightening deposit security through the standard upgrade path.

---

### Finding Description

In `CkBtcMinterState::upgrade()`, the `min_confirmations` field is updated with a one-directional guard: [1](#0-0) 

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

The condition `min_conf < self.min_confirmations` means the upgrade only applies if the new value is **strictly less** than the current value. Any attempt to increase `min_confirmations` is silently dropped with only an info-level log. No error is returned to the caller, and the NNS governance proposal is marked as successfully executed. [2](#0-1) 

`validate_config()` does not check `min_confirmations` at all, so no trap is raised. The upgrade succeeds from the governance perspective, but the intended configuration change is not applied.

The `UpgradeArgs` struct exposes `min_confirmations` as a settable field: [3](#0-2) 

And the Candid interface documents it as a parameter that "overrides" the current value: [4](#0-3) 

The only path that allows increasing `min_confirmations` is `reinit()`, which is called during a full canister reinstall — a far more disruptive operation than a normal upgrade: [5](#0-4) 

The mainnet minter has had `min_confirmations` reduced multiple times (72 → 12 → 6 → 4), as documented in upgrade proposals: [6](#0-5) 

If governance ever needs to reverse this trend, the upgrade silently fails.

---

### Impact Explanation

NNS governance cannot increase `min_confirmations` via the standard canister upgrade path. If a Bitcoin security event — such as a mining pool gaining excessive hash power, increased reorganization risk, or a known double-spend attempt — requires more confirmations before minting ckBTC, governance would be unable to respond through the normal upgrade mechanism. The minter would continue accepting deposits with fewer confirmations than governance intended, potentially allowing ckBTC to be minted for Bitcoin transactions that are later reorganized out of the chain (double-spend). The only workaround is a full canister reinstall, which is significantly more disruptive, requires stopping the minter, and risks losing in-flight state.

**Vulnerability class:** Governance authorization bug / chain-fusion mint/burn configuration bug.

---

### Likelihood Explanation

Medium. The ckBTC mainnet minter currently uses 4 confirmations — the lowest it has ever been. The upgrade path to increase this value is broken. A future Bitcoin security event requiring more confirmations would expose this gap: governance would pass a proposal, it would be marked as executed, and the minter would silently continue operating at 4 confirmations. The discrepancy would only be discovered by querying minter state after the fact.

---

### Recommendation

Remove the one-directional constraint in `upgrade()`. The `min_confirmations` field should be updatable in both directions via a normal upgrade, consistent with how `retrieve_btc_min_amount`, `deposit_btc_min_amount`, and `max_time_in_queue_nanos` are handled (unconditional assignment when `Some`). If there is a concern about pending deposits being affected by an increase, emit an explicit warning log but still apply the change, or return an error to governance rather than silently ignoring it.

---

### Proof of Concept

1. ckBTC mainnet minter is running with `min_confirmations = 4`.
2. NNS governance passes a proposal: upgrade minter with `UpgradeArgs { min_confirmations: Some(6), .. }`.
3. Proposal is executed successfully (no error returned).
4. Query minter state via `get_minter_info` — `min_confirmations` is still `4`.
5. The minter log contains: `"Didn't increase min_confirmations to 6 (current value: 4)"` — but governance has no visibility into this.
6. All subsequent `update_balance` calls continue using `min_confirmations = 4`: [7](#0-6) 

The governance-approved security change is permanently lost until a full reinstall is performed.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L663-665)
```rust
        if let Some(min_confirmations) = min_confirmations {
            self.min_confirmations = min_confirmations;
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

**File:** rs/bitcoin/ckbtc/minter/src/lifecycle/upgrade.rs (L22-26)
```rust
    /// Specifies the minimum number of confirmations on the Bitcoin network
    /// required for the minter to accept a transaction.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub min_confirmations: Option<u32>,

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

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L172-183)
```rust
    let (btc_network, min_confirmations) =
        state::read_state(|s| (s.btc_network, s.min_confirmations));

    let utxos = get_utxos(
        btc_network,
        &address,
        min_confirmations,
        CallSource::Client,
        runtime,
    )
    .await?
    .utxos;
```
