### Title
ckBTC Minter `upgrade()` Enforces One-Way Ratchet on `min_confirmations`, Permanently Locking In Insufficient Bitcoin Reorg Protection — (File: `rs/bitcoin/ckbtc/minter/src/state.rs`)

---

### Summary

The `CkBtcMinterState::upgrade()` function contains a one-directional guard that silently discards any governance attempt to **increase** `min_confirmations`. The current production value is **4** (reduced from 6 in January 2026). Because the upgrade path can only decrease this threshold, the governance has no on-chain mechanism to raise it in response to a Bitcoin reorg event, permanently locking the minter into a confirmation depth that is insufficient to survive historically observed Bitcoin chain reorganizations.

---

### Finding Description

In `rs/bitcoin/ckbtc/minter/src/state.rs`, the `upgrade()` function applies the following guard to `min_confirmations`:

```rust
if let Some(min_conf) = min_confirmations {
    if min_conf < self.min_confirmations {
        self.min_confirmations = min_conf;   // only decreases are accepted
    } else {
        log!(Priority::Info,
             "Didn't increase min_confirmations to {} (current value: {})",
             min_conf, self.min_confirmations);
    }
}
``` [1](#0-0) 

Any `UpgradeArgs` value that is **greater than or equal to** the current `min_confirmations` is silently dropped. The code-level default is `DEFAULT_MIN_CONFIRMATIONS = 6`: [2](#0-1) 

However, the January 2026 governance proposal (`minter_upgrade_2026_01_23.md`) successfully reduced the live production value to **4** by passing `min_confirmations = opt (4 : nat32)` — a decrease, which the guard permits: [3](#0-2) 

The `reinit()` path (called only on fresh canister installation, not upgrade) does accept arbitrary values: [4](#0-3) 

But `reinit()` is not reachable via a normal NNS upgrade proposal — it requires a full canister reinstall, which is a far more disruptive and rarely used governance action.

---

### Impact Explanation

The ckBTC minter mints ckBTC tokens as soon as a Bitcoin deposit UTXO accumulates `min_confirmations` (currently 4) Bitcoin blocks on top of it. If a Bitcoin chain reorganization of depth ≥ 5 occurs after minting, the deposit transaction is erased from the canonical chain while the corresponding ckBTC tokens remain in circulation on the IC ledger. This breaks the 1:1 BTC backing invariant of ckBTC, producing unbacked tokens that can be transferred, sold, or used as collateral — a direct ledger conservation violation. The governance's only remediation path (raising `min_confirmations` via upgrade) is silently blocked by the one-way ratchet, meaning the system cannot be hardened after the fact without a full canister reinstall. [5](#0-4) 

---

### Likelihood Explanation

Bitcoin mainnet has experienced reorgs of depth ≥ 4 blocks historically (the most notable being a 6-block reorg in March 2013). While such events are rare on modern Bitcoin mainnet, the probability is non-zero and increases during periods of high miner hashrate volatility or network partitions. More critically, the one-way ratchet means the governance **cannot proactively raise** `min_confirmations` even if early warning signs of instability appear — the window to act is closed the moment the value is lowered. The January 2026 proposal itself acknowledged the security trade-off ("should only marginally impact the security"), confirming the team is aware the reduction carries reorg risk. [3](#0-2) 

---

### Recommendation

Remove the one-directional guard in `CkBtcMinterState::upgrade()` so that `min_confirmations` can be both increased and decreased via governance upgrade proposals:

```rust
// Replace the current one-way guard:
if let Some(min_conf) = min_confirmations {
    self.min_confirmations = min_conf;
}
```

Additionally, consider enforcing a protocol-level floor (e.g., `min_conf.max(MIN_SAFE_CONFIRMATIONS)`) to prevent governance from accidentally setting the value to 0 or 1 via the same path. [1](#0-0) 

---

### Proof of Concept

**Step 1 — Current state:** The live ckBTC minter (`mqygn-kiaaa-aaaar-qaadq-cai`) has `min_confirmations = 4` as set by the January 2026 NNS proposal.

**Step 2 — Governance attempt to raise threshold:** Submit an NNS upgrade proposal with `UpgradeArgs { min_confirmations: Some(6), .. }`. The `upgrade()` function evaluates `6 < 4` → `false`, logs `"Didn't increase min_confirmations to 6 (current value: 4)"`, and discards the change. The minter continues operating at 4 confirmations.

**Step 3 — Reorg scenario:** A Bitcoin miner produces a competing chain of depth 5. A user's deposit at height H was confirmed at height H+4 (4 confirmations), triggering `update_balance` and minting ckBTC. After the reorg, the deposit transaction no longer exists in the canonical chain. The ckBTC tokens remain on the IC ledger, unbacked. [6](#0-5) [1](#0-0)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L437-438)
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

**File:** rs/bitcoin/ckbtc/minter/src/lifecycle/init.rs (L9-9)
```rust
pub const DEFAULT_MIN_CONFIRMATIONS: u32 = 6;
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2026_01_23.md (L20-21)
```markdown
the main motivation for this proposal is to reduce the number of confirmations required by the minter to process a deposit and mint ckBTC.
Changing that number from 6 to 4 should only marginally impact the security of the ckBTC token while improving the user experience for certain applications (e.g., being able to react to falling market prices more quickly for lending applications).
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L238-244)
```rust
        utxos.retain(|u| {
            tip_height
                < u.height
                    .checked_add(min_confirmations)
                    .expect("bug: this shouldn't overflow")
                    .checked_sub(1)
                    .expect("bug: this shouldn't underflow")
```
