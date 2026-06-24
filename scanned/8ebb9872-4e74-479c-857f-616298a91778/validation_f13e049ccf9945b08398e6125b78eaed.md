### Title
ckBTC Minter `Mode` Deposit Restriction Not Enforced in Reimbursement Minting Path — (`rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs`)

---

### Summary

The ckBTC minter enforces its `Mode` restriction (e.g., `ReadOnly`, `DepositsRestrictedTo`) in the direct deposit path (`update_balance`) but **not** in the reimbursement minting path (`reimburse_withdrawals`). This is a direct structural analog to the JOJO M-5 finding: a deposit-restriction flag is checked in one code path but silently skipped in an alternative path that also results in ckBTC being minted.

---

### Finding Description

The ckBTC minter exposes a `Mode` enum that controls which operations are permitted: [1](#0-0) 

`Mode::ReadOnly` is documented as "Minter's state is read-only" (no state modifications), and `Mode::DepositsRestrictedTo(allow_list)` restricts BTC-to-ckBTC conversion to a specific principal allow-list.

**Guarded path — `update_balance`:**

The direct deposit entry point correctly calls `is_deposit_available_for` before any minting occurs: [2](#0-1) 

**Unguarded path — `reimburse_withdrawals`:**

The background reimbursement task mints ckBTC directly via `runtime.mint_ckbtc()` with **no mode check whatsoever**: [3](#0-2) 

The function iterates `pending_withdrawal_reimbursements` and calls `mint_ckbtc` for each entry without consulting `s.mode.is_deposit_available_for()` at any point. The same omission applies to the failed-deposit reimbursement path (`pending_reimbursements`), which also mints ckBTC to reimburse users whose UTXOs were found tainted.

A pending reimbursement is created when a user calls `retrieve_btc_with_approval` and the resulting withdrawal is cancelled (e.g., `TooManyInputs`): [4](#0-3) 

The reimbursement is then processed asynchronously by the task scheduler, completely independently of the current `Mode`.

---

### Impact Explanation

**`Mode::ReadOnly` violated:** The mode is documented to prevent all state modifications. However, any reimbursements that were queued before the mode switch will still execute, minting ckBTC and modifying ledger state. If `ReadOnly` is activated as an emergency freeze (e.g., due to a suspected minting bug), the reimbursement path continues to invoke the same minting logic, potentially amplifying the very condition the operator was trying to halt.

**`Mode::DepositsRestrictedTo` partially bypassed:** In this mode, `is_withdrawal_available_for` explicitly returns `Ok(())` for all principals: [5](#0-4) 

This means a principal not on the deposit allow-list can call `retrieve_btc_with_approval` (withdrawals are unrestricted in this mode), deliberately trigger a `TooManyInputs` cancellation, and receive a ckBTC mint via reimbursement — bypassing the deposit restriction entirely. The `Mode::DepositsRestrictedTo` invariant ("only specified principals can convert BTC to ckBTC") is rendered ineffective for this path.

---

### Likelihood Explanation

The attacker-controlled entry path is:

1. Minter is in `GeneralAvailability` mode; user accumulates many small UTXOs via `update_balance`.
2. User calls `retrieve_btc_with_approval` with an amount requiring more than `max_num_inputs_in_transaction` UTXOs — this is accepted and a burn is recorded.
3. Operator switches minter to `DepositsRestrictedTo([trusted_principal])` or `ReadOnly`.
4. The minter's task scheduler runs `reimburse_withdrawals`; ckBTC is minted to the user without any mode check.

Step 2 is directly user-controllable (the `TooManyInputs` threshold is a known constant `DEFAULT_MAX_NUM_INPUTS_IN_TRANSACTION`). Steps 3–4 follow deterministically. No privileged key or majority corruption is required on the attacker's side. [6](#0-5) 

---

### Recommendation

Add a `Mode` check inside `reimburse_withdrawals` (and the analogous failed-deposit reimbursement loop) before calling `mint_ckbtc`. Because blocking reimbursements in `ReadOnly` mode could strand user funds, the preferred fix is to either:

- Explicitly document that reimbursements are exempt from `Mode` restrictions and rename/clarify the `ReadOnly` semantics accordingly, **or**
- Drain all pending reimbursements before transitioning to `ReadOnly` or `DepositsRestrictedTo` mode (process them as part of the mode-change procedure).

At minimum, the `is_deposit_available_for` check should be applied consistently, mirroring the pattern already used in `update_balance`:

```rust
// Inside reimburse_withdrawals, before mint_ckbtc:
if let Err(reason) = state::read_state(|s| s.mode.is_deposit_available_for(&reimbursement.account.owner)) {
    log!(Priority::Info, "[reimburse_withdrawals]: Skipping reimbursement in restricted mode: {reason}");
    continue; // or queue for later
}
```

---

### Proof of Concept

1. Deploy ckBTC minter in `GeneralAvailability` mode.
2. Deposit `DEFAULT_MAX_NUM_INPUTS_IN_TRANSACTION + 1` small UTXOs via `update_balance` to accumulate many inputs.
3. Call `retrieve_btc_with_approval` for an amount that requires all inputs — the minter accepts the burn and queues the request.
4. Upgrade the minter with `mode = DepositsRestrictedTo([some_other_principal])`.
5. Advance time past `max_time_in_queue_nanos` so the task scheduler runs.
6. Observe that `retrieve_btc_status_v2` transitions to `Reimbursed` and the user's ckBTC balance is restored — despite the user's principal not being on the deposit allow-list. [7](#0-6) [2](#0-1) [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L339-388)
```rust
/// Controls which operations the minter can perform.
#[derive(
    Default, Clone, Eq, PartialEq, Debug, Serialize, candid::CandidType, serde::Deserialize,
)]
pub enum Mode {
    /// Minter's state is read-only.
    ReadOnly,
    /// Only the specified principals can modify the minter's state.
    RestrictedTo(Vec<Principal>),
    /// Only the specified principals can deposit BTC.
    DepositsRestrictedTo(Vec<Principal>),
    #[default]
    /// No restrictions on the minter interactions.
    GeneralAvailability,
}

impl Mode {
    /// Returns Ok if the specified principal can convert BTC to ckBTC.
    pub fn is_deposit_available_for(&self, p: &Principal) -> Result<(), String> {
        match self {
            Self::GeneralAvailability => Ok(()),
            Self::ReadOnly => Err("the minter is in read-only mode".to_string()),
            Self::RestrictedTo(allow_list) => {
                if !allow_list.contains(p) {
                    return Err("access to the minter is temporarily restricted".to_string());
                }
                Ok(())
            }
            Self::DepositsRestrictedTo(allow_list) => {
                if !allow_list.contains(p) {
                    return Err("BTC deposits are temporarily restricted".to_string());
                }
                Ok(())
            }
        }
    }

    /// Returns Ok if the specified principal can convert ckBTC to BTC.
    pub fn is_withdrawal_available_for(&self, p: &Principal) -> Result<(), String> {
        match self {
            Self::GeneralAvailability | Self::DepositsRestrictedTo(_) => Ok(()),
            Self::ReadOnly => Err("the minter is in read-only mode".to_string()),
            Self::RestrictedTo(allow_list) => {
                if !allow_list.contains(p) {
                    return Err("BTC withdrawals are temporarily restricted".to_string());
                }
                Ok(())
            }
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L156-160)
```rust
    // When the minter is in the mode using a whitelist we only want a certain
    // set of principal to be able to mint. But we also want those principals
    // to mint at any desired address. Therefore, the check below is on "caller".
    state::read_state(|s| s.mode.is_deposit_available_for(&caller))
        .map_err(UpdateBalanceError::TemporarilyUnavailable)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/reimbursement/mod.rs (L57-116)
```rust
/// Reimburse withdrawals that were canceled.
pub async fn reimburse_withdrawals<R: CanisterRuntime>(runtime: &R) {
    if state::read_state(|s| s.pending_withdrawal_reimbursements.is_empty()) {
        return;
    }
    let pending_reimbursements = state::read_state(|s| s.pending_withdrawal_reimbursements.clone());
    let mut error_count = 0;
    for (burn_index, reimbursement) in pending_reimbursements {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this reimbursement request will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(burn_index, |index| {
            state::mutate_state(|s| {
                state::audit::quarantine_withdrawal_reimbursement(s, index, runtime)
            });
        });
        let memo = MintMemo::ReimburseWithdrawal {
            withdrawal_id: burn_index,
        };
        match runtime
            .mint_ckbtc(
                reimbursement.amount,
                reimbursement.account,
                Memo::from(crate::memo::encode(&memo)),
            )
            .await
        {
            Ok(mint_index) => {
                log!(
                    Priority::Debug,
                    "[reimburse_withdrawals]: Successfully reimbursed {:?} at mint block index {}",
                    reimbursement,
                    mint_index
                );
                state::mutate_state(|s| {
                    state::audit::reimburse_withdrawal_completed(s, burn_index, mint_index, runtime)
                });
            }
            Err(err) => {
                log!(
                    Priority::Info,
                    "[reimburse_withdrawals]: Failed to reimburse {:?}: {:?}. Will retry later",
                    reimbursement,
                    err
                );
                error_count += 1;
            }
        }
        // Defuse the guard. Note that in case of a panic in the callback (either before or after this point)
        // the defuse will not be effective (due to state rollback), and the guard that was
        // setup before the `mint_ckbtc` async call will be invoked.
        scopeguard::ScopeGuard::into_inner(prevent_double_minting_guard);
    }

    if error_count > 0 {
        log!(
            Priority::Info,
            "[reimburse_withdrawals] Failed to reimburse {error_count} withdrawal requests, retrying later."
        );
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L240-265)
```rust
pub fn reimburse_withdrawal<R: CanisterRuntime>(
    state: &mut CkBtcMinterState,
    burn_block_index: LedgerBurnIndex,
    reimbursed_amount: u64,
    reimbursement_account: Account,
    reason: WithdrawalReimbursementReason,
    runtime: &R,
) {
    record_event(
        EventType::ScheduleWithdrawalReimbursement {
            account: reimbursement_account,
            amount: reimbursed_amount,
            reason: reason.clone(),
            burn_block_index,
        },
        runtime,
    );
    state.schedule_withdrawal_reimbursement(
        burn_block_index,
        ReimburseWithdrawalTask {
            account: reimbursement_account,
            amount: reimbursed_amount,
            reason,
        },
    )
}
```
