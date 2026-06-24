### Title
Permanently Stuck UTXOs with No Recovery Mechanism in ckBTC Minter — (File: `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

---

### Summary

The ckBTC minter's `update_balance` function permanently skips UTXOs that reach the `CleanButMintUnknown` state, with no automated recovery path. Once a UTXO enters this state, the user's deposited BTC is locked in the minter's Bitcoin address indefinitely, and neither ckBTC is minted nor is a BTC refund issued. The code itself acknowledges this requires "manual intervention," but no such mechanism is exposed.

---

### Finding Description

In `update_balance`, after a UTXO passes the Bitcoin checker, the minter calls `mint_ckbtc` on the ICRC-1 ledger. A `scopeguard` is installed before the async call to handle the case where execution panics or traps after `mint_ckbtc` returns but before the minter's state is persisted: [1](#0-0) 

If this guard fires (i.e., a panic occurs in the callback after `mint_ckbtc` returns), the UTXO is marked `CleanButMintUnknown` via `mark_utxo_checked_mint_unknown`: [2](#0-1) 

This sets the UTXO's status in `checked_utxos` to `UtxoCheckStatus::CleanButMintUnknown`: [3](#0-2) 

On every subsequent call to `update_balance`, `check_utxo` reads the UTXO's status from `checked_utxos` and returns `CleanButMintUnknown`. The main processing loop then **permanently skips** it with no retry, no refund, and no state transition: [4](#0-3) 

The minter exposes `mint_status_unknown_utxos()` to enumerate these stuck UTXOs, but provides no corresponding recovery function: [5](#0-4) 

The `SuspendedReason` enum, which does have a periodic re-evaluation path, does not include `CleanButMintUnknown` — that state lives in `checked_utxos`, not `suspended_utxos`, so it is never re-evaluated: [6](#0-5) 

---

### Impact Explanation

If a UTXO reaches `CleanButMintUnknown` in the scenario where `mint_ckbtc` returned `Err` (ledger unavailable) and the callback then panicked:

- The ledger did **not** mint ckBTC (the call failed).
- The minter's state rolled back, so the UTXO is not in `utxos_state_addresses` or `finalized_utxos`.
- The scopeguard fires and marks the UTXO `CleanButMintUnknown` in `checked_utxos`.
- All future `update_balance` calls skip this UTXO unconditionally.
- The user's BTC remains locked in the minter's Bitcoin address with no ckBTC minted and no refund path.

This is a direct analog to the Atlas Protocol finding: a deposit/UTXO enters a terminal failure state with no automated recovery mechanism, resulting in permanent fund loss for the user.

---

### Likelihood Explanation

The trigger requires a panic or trap in the IC callback execution window between the return of the `mint_ckbtc` inter-canister call and the `ScopeGuard::into_inner` defuse. While the code in that window is simple, IC canister execution can trap due to instruction limit exhaustion, out-of-memory conditions, or future code changes introducing a panic in that path. The probability per individual `update_balance` call is low, but the impact when it occurs is **permanent and irreversible** without manual operator intervention, which is not guaranteed or formalized.

---

### Recommendation

1. Add a canister-level recovery function (callable by governance or a privileged role) that re-attempts minting for UTXOs in `CleanButMintUnknown` state, using the existing `mint_status_unknown_utxos()` enumeration.
2. Alternatively, integrate `CleanButMintUnknown` UTXOs into the periodic re-evaluation loop (similar to `SuspendedReason::Quarantined`) so that `update_balance` retries minting after a cooldown period, rather than skipping permanently.
3. If minting repeatedly fails, implement a BTC refund path analogous to the Atlas Protocol remediation.

---

### Proof of Concept

1. User calls `update_balance` with a valid UTXO.
2. The UTXO passes the Bitcoin checker; `mint_ckbtc` is called on the ledger and returns `Err` (e.g., ledger temporarily unavailable).
3. A panic occurs in the callback (e.g., due to instruction limit exhaustion from a large `utxo_statuses` vector) before `ScopeGuard::into_inner` is reached.
4. IC state rolls back; the scopeguard fires and calls `mark_utxo_checked_mint_unknown`, persisting `CleanButMintUnknown` in `checked_utxos`.
5. User calls `update_balance` again. `check_utxo` returns `CleanButMintUnknown`; the loop hits `continue` at line 305.
6. The UTXO is never processed again. No ckBTC is minted. No BTC refund is issued. Funds are permanently stuck. [4](#0-3) [7](#0-6)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L302-306)
```rust
        let status = check_utxo(&utxo, &args, runtime).await?;
        match status {
            // Skip utxos that are already checked but has unknown mint status
            UtxoCheckStatus::CleanButMintUnknown => continue,
            UtxoCheckStatus::Clean => {
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L327-378)
```rust
        // After the call to `mint_ckbtc` returns, in a very unlikely situation the
        // execution may panic/trap without persisting state changes and then we will
        // have no idea whether the mint actually succeeded or not. If this happens
        // the use of the guard below will help set the utxo to `CleanButMintUnknown`
        // status so that it will not be minted again. Utxos with this status will
        // require manual intervention.
        let guard = scopeguard::guard((utxo.clone(), caller_account), |(utxo, account)| {
            mutate_state(|s| {
                state::audit::mark_utxo_checked_mint_unknown(s, utxo, account, runtime)
            });
        });

        match runtime
            .mint_ckbtc(amount, caller_account, crate::memo::encode(&memo).into())
            .await
        {
            Ok(block_index) => {
                log!(
                    Priority::Debug,
                    "Minted {amount} {token_name} for account {caller_account} corresponding to utxo {} with value {}",
                    DisplayOutpoint(&utxo.outpoint),
                    DisplayAmount(utxo.value),
                );
                state::mutate_state(|s| {
                    state::audit::add_utxos(
                        s,
                        Some(block_index),
                        caller_account,
                        vec![utxo.clone()],
                        runtime,
                    )
                });
                utxo_statuses.push(UtxoStatus::Minted {
                    block_index,
                    utxo,
                    minted_amount: amount,
                });
            }
            Err(err) => {
                log!(
                    Priority::Info,
                    "Failed to mint ckBTC for UTXO {}: {:?}",
                    DisplayOutpoint(&utxo.outpoint),
                    err
                );
                utxo_statuses.push(UtxoStatus::Checked(utxo));
            }
        }
        // Defuse the guard. Note that In case of a panic (either before or after this point)
        // the defuse will not be effective (due to state rollback), and the guard that was
        // setup before the `mint_ckbtc` async call will be invoked.
        scopeguard::ScopeGuard::into_inner(guard);
```

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L133-147)
```rust
pub fn mark_utxo_checked_mint_unknown<R: CanisterRuntime>(
    state: &mut CkBtcMinterState,
    utxo: Utxo,
    account: Account,
    runtime: &R,
) {
    record_event(
        EventType::CheckedUtxoMintUnknown {
            utxo: utxo.clone(),
            account,
        },
        runtime,
    );
    state.mark_utxo_checked_mint_unknown(utxo, &account);
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1473-1489)
```rust
    /// Marks the given UTXO as successfully checked but minting failed.
    fn mark_utxo_checked_mint_unknown(&mut self, utxo: Utxo, account: &Account) {
        // It should have already been removed from suspended_utxos
        debug_assert_eq!(
            self.suspended_utxos.contains_utxo(&utxo, account),
            (None, None),
            "BUG: UTXO was still suspended and cannot be marked as mint unknown"
        );
        self.checked_utxos.insert(
            utxo,
            CheckedUtxo {
                uuid: None,
                status: UtxoCheckStatus::CleanButMintUnknown,
                kyt_provider: None,
            },
        );
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1760-1768)
```rust
    pub fn mint_status_unknown_utxos(&self) -> impl Iterator<Item = &Utxo> {
        self.checked_utxos.iter().filter_map(|(utxo, checked)| {
            if checked.status == UtxoCheckStatus::CleanButMintUnknown {
                Some(utxo)
            } else {
                None
            }
        })
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1911-1917)
```rust
#[derive(Clone, Copy, Eq, PartialEq, Debug, CandidType, Serialize, Deserialize)]
pub enum SuspendedReason {
    /// UTXO whose value is too small to pay the Bitcoin check fee.
    ValueTooSmall,
    /// UTXO that the Bitcoin checker considered tainted.
    Quarantined,
}
```
