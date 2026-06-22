### Title
Stale UTXO Compliance Check Result Allows Minting ckBTC for OFAC-Sanctioned Addresses — (File: `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

---

### Summary

The ckBTC minter caches UTXO compliance check results in `checked_utxos`. When a UTXO is checked as `Clean` but minting subsequently fails (e.g., ledger temporarily unavailable), the cached `Clean` result is used on retry without re-querying the Bitcoin checker canister. If the Bitcoin checker canister is upgraded to include new OFAC-sanctioned addresses between the initial check and the retry, ckBTC is minted for UTXOs from now-sanctioned sources using stale local compliance state — the exact structural analog to the reported bridge vulnerability.

---

### Finding Description

In `check_utxo`, the minter unconditionally short-circuits on any cached entry in `checked_utxos`:

```rust
// rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs
if let Some(checked_utxo) = read_state(|s| s.checked_utxos.get(utxo).cloned()) {
    return Ok(checked_utxo.status);
}
``` [1](#0-0) 

This cache is populated by `mark_utxo_checked` **before** minting is attempted:

```rust
UtxoCheckStatus::Clean => {
    mutate_state(|s| {
        state::audit::mark_utxo_checked(s, utxo.clone(), caller_account, runtime)
    });
}
``` [2](#0-1) 

If the subsequent `mint_ckbtc` call fails with a transient error (not a panic), the scopeguard is defused and the UTXO remains in `checked_utxos` with `Clean` status but is **not** added to the minter's UTXO set:

```rust
Err(err) => {
    log!(...);
    utxo_statuses.push(UtxoStatus::Checked(utxo));
}
``` [3](#0-2) 

On the next `update_balance` call, the UTXO reappears as a new unprocessed UTXO (not yet in the minter's UTXO set), `check_utxo` is called again, and the cached `Clean` result is returned immediately — no call to the Bitcoin checker canister is made. The minter then proceeds to mint ckBTC.

The Bitcoin checker canister stores the OFAC SDN list internally and it is only updated via NNS-governed canister upgrades:

> "The Bitcoin checker canister stores a copy of the SDN list internally. The list can only be modified by upgrading the Bitcoin checker canister itself, which requires an NNS proposal." [4](#0-3) 

Upgrade proposals that update the OFAC list are a documented, recurring operational event: [5](#0-4) 

The `checked_utxos` map is part of the minter's persistent replicated state and is not cleared on canister upgrade: [6](#0-5) 

---

### Impact Explanation

An attacker whose Bitcoin address is added to the OFAC SDN list after their deposit UTXO was already checked as `Clean` — but before minting succeeded — can retry `update_balance` and receive ckBTC using the stale cached compliance result. The Bitcoin checker canister's updated SDN list is never consulted. This is the direct IC analog to the reported pattern: compliance metadata is evaluated once, cached locally, and the cached (now-stale) state is used for the minting decision, bypassing the updated restriction.

The concrete consequence is minting ckBTC from OFAC-sanctioned Bitcoin sources, which is the primary compliance guarantee the checker canister is designed to enforce.

---

### Likelihood Explanation

The window requires two independent events to overlap:

1. A transient minting failure after the UTXO check passes (ledger temporarily unavailable — a realistic operational condition).
2. A Bitcoin checker canister upgrade that adds the depositor's address to the OFAC list within that window.

Both events are individually plausible and documented. A sophisticated actor aware of their impending sanctioning could exploit this window deliberately by depositing BTC just before the NNS proposal to update the SDN list is executed, then retrying `update_balance` after the upgrade. The likelihood is **low** but non-zero and the impact is a direct compliance bypass.

---

### Recommendation

1. **Re-check on retry**: Remove the early-return cache hit in `check_utxo`. Always query the Bitcoin checker canister for UTXOs that have not yet been successfully minted (i.e., not yet in the minter's UTXO set), regardless of their entry in `checked_utxos`.

2. **Alternatively, invalidate the cache on checker upgrade**: When the minter is notified of or detects a Bitcoin checker canister upgrade (e.g., via a version field), clear or invalidate `checked_utxos` entries that have not yet been minted, forcing a fresh compliance check.

3. **Conservative fallback**: If re-checking is infeasible, treat any UTXO in `checked_utxos` with `Clean` status but not yet in the minter's UTXO set as requiring a fresh check, applying the most restrictive outcome (quarantine) if the checker is unavailable.

---

### Proof of Concept

1. Attacker deposits BTC from address `A` (not currently on the OFAC SDN list) to the ckBTC minter's deposit address.
2. `update_balance` is called. The UTXO passes the Bitcoin checker (`CheckTransactionResponse::Passed`). `mark_utxo_checked` is called, adding the UTXO to `checked_utxos` with `UtxoCheckStatus::Clean`.
3. The call to `mint_ckbtc` fails with a transient error (e.g., the ckBTC ledger is temporarily unavailable). The UTXO is returned as `UtxoStatus::Checked` and remains in `checked_utxos` with `Clean` status but is **not** added to the minter's UTXO set.
4. An NNS proposal is executed to upgrade the Bitcoin checker canister with an updated OFAC list that now includes address `A`.
5. Attacker calls `update_balance` again. The UTXO is still unprocessed (not in the minter's UTXO set). `check_utxo` is called, finds the UTXO in `checked_utxos`, and returns `Ok(UtxoCheckStatus::Clean)` without calling the Bitcoin checker canister.
6. The minter proceeds to mint ckBTC for the UTXO from the now-sanctioned address `A`, bypassing the updated OFAC compliance check.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L306-310)
```rust
            UtxoCheckStatus::Clean => {
                mutate_state(|s| {
                    state::audit::mark_utxo_checked(s, utxo.clone(), caller_account, runtime)
                });
            }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L365-373)
```rust
            Err(err) => {
                log!(
                    Priority::Info,
                    "Failed to mint ckBTC for UTXO {}: {:?}",
                    DisplayOutpoint(&utxo.outpoint),
                    err
                );
                utxo_statuses.push(UtxoStatus::Checked(utxo));
            }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L397-399)
```rust
    if let Some(checked_utxo) = read_state(|s| s.checked_utxos.get(utxo).cloned()) {
        return Ok(checked_utxo.status);
    }
```

**File:** rs/bitcoin/checker/README.md (L20-20)
```markdown
The Bitcoin checker canister stores a copy of the SDN list internally. The list can only be modified by upgrading the Bitcoin checker canister itself, which requires an NNS proposal as the NNS is the only controller of the Bitcoin checker canister.
```

**File:** rs/bitcoin/ckbtc/mainnet/btc_checker_upgrade_2025_02_25.md (L19-21)
```markdown
Update the Bitcoin checker canister to include the latest code changes, notably:
* Update the OFAC checklist.
* Add new `check_transaction_query` and `check_transaction_str` methods.
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L391-410)
```rust
/// The outcome of a UTXO check.
#[derive(Copy, Clone, Eq, PartialEq, Debug)]
pub enum UtxoCheckStatus {
    /// The Bitcoin check did not reveal any problems.
    Clean,
    /// The UTXO in question is tainted.
    Tainted,
    /// The UTXO is clean but minting failed.
    CleanButMintUnknown,
}

/// Relevant data for a checked UTXO. The UUID and `kyt_provider` are kept for
/// backward-compatibility reasons. They should be set to `None` since
/// we dont use KYT providers anymore.
#[derive(Clone, Eq, PartialEq, Debug)]
pub struct CheckedUtxo {
    pub(crate) status: UtxoCheckStatus,
    uuid: Option<String>,
    kyt_provider: Option<Principal>,
}
```
