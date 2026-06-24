Now I have enough context to write the finding. The analog is clear: the ckBTC minter's `update_balance` function is publicly callable by any non-anonymous user, and it re-evaluates `Quarantined` UTXOs (tainted by the Bitcoin checker) every 24 hours by calling `check_transaction` on the Bitcoin checker canister — a call that costs **40 billion cycles** from the minter's own balance. Since the SDN list only changes via NNS upgrade, a quarantined UTXO will always remain tainted, making each re-evaluation a guaranteed cycle drain that any attacker can trigger for any account.

---

### Title
Unprivileged Caller Can Repeatedly Drain ckBTC Minter Cycles by Triggering Re-evaluation of Permanently-Quarantined UTXOs - (File: `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

### Summary
The ckBTC minter's `update_balance` endpoint is publicly callable by any non-anonymous principal and accepts an arbitrary `owner` field, allowing any caller to trigger re-evaluation of quarantined UTXOs belonging to any account. Re-evaluating a quarantined UTXO unconditionally calls `check_transaction` on the Bitcoin checker canister, costing up to 400 billion cycles (10 retries × 40B cycles each) from the minter's own balance per call. Because the SDN list that drives the taint decision is immutable without an NNS upgrade, quarantined UTXOs will always remain tainted, making every re-evaluation a guaranteed cycle drain. An attacker can repeat this every 24 hours per UTXO, across all accounts with quarantined UTXOs, indefinitely.

### Finding Description
The `update_balance` function in `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs` accepts an `UpdateBalanceArgs` struct with an optional `owner` field. There is no check that `args.owner` matches the caller:

```rust
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),
    subaccount: args.subaccount,
};
``` [1](#0-0) 

The function then calls `processable_utxos_for_account`, which re-queues any suspended UTXO (including `Quarantined` ones) whose `last_time_checked` timestamp is at least 24 hours old:

```rust
Some(elapsed) if elapsed >= DAY => {
    processable_utxos.insert_once_suspended_utxo(utxo, reason);
}
``` [2](#0-1) [3](#0-2) 

For each re-queued `Quarantined` UTXO, `check_utxo` is called. This function has no cache hit for quarantined UTXOs (they are not stored in `checked_utxos`) and unconditionally calls `check_transaction` on the Bitcoin checker canister, attaching `CHECK_TRANSACTION_CYCLES_REQUIRED` (40 billion cycles) from the minter's balance, up to `MAX_CHECK_TRANSACTION_RETRY = 10` times: [4](#0-3) [5](#0-4) 

The Bitcoin checker's SDN list is immutable without an NNS canister upgrade, so the result for a quarantined UTXO is always `Failed` → `Tainted`. The UTXO is then re-quarantined with a fresh timestamp, resetting the 24-hour cooldown and enabling the cycle to repeat: [6](#0-5) [7](#0-6) 

The `SuspendedUtxos::insert` call updates `last_time_checked_cache` even when the UTXO is already quarantined for the same reason (no new event is emitted, but the timestamp resets): [8](#0-7) 

### Impact Explanation
Each `update_balance` call targeting an account with N quarantined UTXOs that have aged past 24 hours drains up to `N × 10 × 40B = N × 400B` cycles from the ckBTC minter canister. With enough quarantined UTXOs across the system (visible via the public dashboard or event log), an attacker can systematically drain the minter's cycle balance. If the minter runs out of cycles, it stops processing all ckBTC deposits and withdrawals for all users, constituting a denial-of-service against the entire ckBTC protocol. The minter's cycle balance is finite and replenishment requires governance action. [9](#0-8) 

### Likelihood Explanation
The attack requires only a non-anonymous IC principal (freely obtainable) and knowledge of accounts with quarantined UTXOs. The ckBTC minter's public dashboard exposes quarantined UTXOs, and the event log is queryable. The 24-hour cooldown per UTXO limits the rate but does not prevent sustained draining across many UTXOs. The attack is low-cost for the attacker (only ingress message fees) and high-cost for the minter (40B cycles per `check_transaction` call paid by the minter). As ckBTC adoption grows, the number of quarantined UTXOs grows, amplifying the impact. [10](#0-9) [11](#0-10) 

### Recommendation
1. **Skip re-evaluation of `Quarantined` UTXOs entirely** in `processable_utxos_for_account`. Unlike `ValueTooSmall` UTXOs (which can become processable if the minimum deposit amount changes), `Quarantined` UTXOs are permanently tainted until the SDN list changes via NNS upgrade. They should not be re-evaluated on a user-triggered timer.
2. **Alternatively**, cache the `Tainted` result in `checked_utxos` so that `check_utxo` returns immediately without spending cycles on a repeat `check_transaction` call.
3. **Restrict `args.owner`** to require that the caller matches the owner, preventing third-party triggering of re-evaluation for arbitrary accounts. [12](#0-11) [13](#0-12) 

### Proof of Concept
1. Identify an account `A` with a quarantined UTXO (visible on the ckBTC minter dashboard or via `get_events` query).
2. Wait 24 hours after the UTXO's last evaluation timestamp.
3. Call `update_balance(record { owner = opt A; subaccount = null })` from any non-anonymous principal.
4. The minter calls `check_transaction` on the Bitcoin checker canister, spending 40B cycles from the minter's balance. The result is `Failed` (tainted). The UTXO is re-quarantined with a new timestamp.
5. Repeat step 2–4 every 24 hours. With K quarantined UTXOs across the system, an attacker can drain up to `K × 400B` cycles per day from the minter. [14](#0-13) [15](#0-14)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L18-20)
```rust
// Max number of times of calling check_transaction with cycle payment, to avoid spending too
// many cycles.
const MAX_CHECK_TRANSACTION_RETRY: usize = 10;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L143-168)
```rust
/// Notifies the ckBTC minter to update the balance of the user subaccount.
pub async fn update_balance<R: CanisterRuntime>(
    args: UpdateBalanceArgs,
    runtime: &R,
) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    let caller = runtime.caller();
    if args.owner.unwrap_or(caller) == runtime.id() {
        ic_cdk::trap("cannot update minter's balance");
    }

    // Record start time of method execution for metrics
    let start_time = runtime.time();

    // When the minter is in the mode using a whitelist we only want a certain
    // set of principal to be able to mint. But we also want those principals
    // to mint at any desired address. Therefore, the check below is on "caller".
    state::read_state(|s| s.mode.is_deposit_available_for(&caller))
        .map_err(UpdateBalanceError::TemporarilyUnavailable)?;

    init_ecdsa_public_key().await;

    let caller_account = Account {
        owner: args.owner.unwrap_or(caller),
        subaccount: args.subaccount,
    };
    let _guard = balance_update_guard(caller_account)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L186-187)
```rust
    let (processable_utxos, suspended_utxos) =
        state::read_state(|s| s.processable_utxos_for_account(utxos, &caller_account, &now));
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L311-316)
```rust
            UtxoCheckStatus::Tainted => {
                mutate_state(|s| {
                    state::audit::quarantine_utxo(s, utxo.clone(), caller_account, now, runtime)
                });
                utxo_statuses.push(UtxoStatus::Tainted(utxo.clone()));
                continue;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L395-406)
```rust
    let btc_checker_principal = read_state(|s| s.btc_checker_principal.map(Principal::from));

    if let Some(checked_utxo) = read_state(|s| s.checked_utxos.get(utxo).cloned()) {
        return Ok(checked_utxo.status);
    }
    for i in 0..MAX_CHECK_TRANSACTION_RETRY {
        match runtime
            .check_transaction(
                btc_checker_principal,
                utxo,
                CHECK_TRANSACTION_CYCLES_REQUIRED,
            )
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1382-1406)
```rust
        for utxo in all_utxos_for_account.into_iter() {
            match self.suspended_utxos.contains_utxo(&utxo, account) {
                (Some(last_time_checked), Some(reason)) => {
                    match now.checked_duration_since(*last_time_checked) {
                        Some(elapsed) if elapsed >= DAY => {
                            processable_utxos.insert_once_suspended_utxo(utxo, reason);
                        }
                        _ => suspended_utxos.push(SuspendedUtxo {
                            utxo,
                            reason: *reason,
                            earliest_retry: last_time_checked
                                .saturating_add(DAY)
                                .as_nanos_since_unix_epoch(),
                        }),
                    }
                }
                (None, Some(reason)) => {
                    processable_utxos.insert_once_suspended_utxo(utxo, reason);
                }
                (_, None) => {
                    if !is_known(&utxo) {
                        processable_utxos.insert_once_new_utxo(utxo);
                    }
                }
            }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1912-1917)
```rust
pub enum SuspendedReason {
    /// UTXO whose value is too small to pay the Bitcoin check fee.
    ValueTooSmall,
    /// UTXO that the Bitcoin checker considered tainted.
    Quarantined,
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1927-1936)
```rust
        if let Some(timestamp) = now {
            self.last_time_checked_cache.insert(utxo.clone(), timestamp);
        }
        if self.utxos.get(&account).and_then(|u| u.get(&utxo)) == Some(&reason) {
            return false;
        }
        self.utxos_without_account.remove(&utxo);
        let utxos = self.utxos.entry(account).or_default();
        utxos.insert(utxo, reason);
        true
```

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L149-163)
```rust
pub fn quarantine_utxo<R: CanisterRuntime>(
    state: &mut CkBtcMinterState,
    utxo: Utxo,
    account: Account,
    now: Timestamp,
    runtime: &R,
) {
    discard_utxo(
        state,
        utxo,
        account,
        SuspendedReason::Quarantined,
        now,
        runtime,
    );
```

**File:** rs/bitcoin/ckbtc/minter/src/dashboard.rs (L541-560)
```rust
    pub fn build_quarantined_utxos(&self, s: &CkBtcMinterState) -> String {
        with_utf8_buffer(|buf| {
            for utxo in s.quarantined_utxos() {
                writeln!(
                    buf,
                    "<tr>
                    <td>{}</td>
                    <td>{}</td>
                    <td>{}</td>
                    <td>{}</td>
                </tr>",
                    self.txid_link(&utxo.outpoint.txid),
                    utxo.outpoint.vout,
                    utxo.height,
                    DisplayAmount(utxo.value)
                )
                .unwrap()
            }
        })
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L196-200)
```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();
    check_postcondition(updates::update_balance::update_balance(args, &IC_CANISTER_RUNTIME).await)
}
```
