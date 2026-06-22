### Title
Unbounded UTXO Processing Loop in `update_balance` Enables Cycles-Drain DoS Against ckBTC Minter - (File: rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs)

---

### Summary

The ckBTC minter's `update_balance` function iterates over every processable UTXO for a caller's deposit address without any per-call cap. For each UTXO above the minimum deposit threshold, it makes up to `MAX_CHECK_TRANSACTION_RETRY` (10) inter-canister calls to the BTC checker canister plus one call to the ledger to mint ckBTC. An unprivileged attacker who sends N small-but-valid Bitcoin UTXOs to their deposit address can force the minter to execute O(N × 11) inter-canister calls in a single `update_balance` invocation, draining the minter's cycle balance and freezing it for all users.

---

### Finding Description

**Root cause — unbounded UTXO processing loop:**

In `update_balance`, after fetching all UTXOs for the caller's address, the function iterates over the full `processable_utxos` set with no upper bound:

```rust
for utxo in processable_utxos {          // no cap on N
    ...
    let status = check_utxo(&utxo, &args, runtime).await?;   // up to 10 inter-canister calls
    ...
    runtime.mint_ckbtc(amount, ...).await  // 1 inter-canister call
}
``` [1](#0-0) 

Each `check_utxo` call retries up to `MAX_CHECK_TRANSACTION_RETRY = 10` times, each consuming `CHECK_TRANSACTION_CYCLES_REQUIRED` cycles from the minter's balance: [2](#0-1) 

**Compounding root cause — unbounded pagination in `get_utxos`:**

Before the processing loop, `get_utxos` fetches all UTXO pages from the Bitcoin canister in an unbounded `while` loop:

```rust
while let Some(page) = response.next_page {
    response = bitcoin_get_utxos(&mut now, paged_request, source, runtime).await?;
    utxos.append(&mut response.utxos);
    num_pages += 1;
}
``` [3](#0-2) 

The number of pages is proportional to the number of UTXOs at the address, which is fully attacker-controlled.

**The only guard present** is `balance_update_guard`, which prevents concurrent `update_balance` calls for the **same account**: [4](#0-3) 

This guard does not limit the number of UTXOs processed per call, and does not prevent the same attacker from using different subaccounts to run multiple concurrent large-UTXO calls simultaneously.

**UTXOs below `deposit_btc_min_amount` or `check_fee` are skipped** (lines 277–300), but UTXOs above those thresholds — which are the normal case — all proceed through the full `check_utxo` + `mint_ckbtc` path: [5](#0-4) 

---

### Impact Explanation

The ckBTC minter canister (`mqygn-kiaaa-aaaar-qaadq-cai`) holds custody of all ckBTC-backing Bitcoin. If its cycle balance is drained to the freeze threshold, the canister is frozen: no deposits, no withdrawals, and no balance updates are possible for any user. This is a complete denial of service for the ckBTC bridge.

With N UTXOs above the minimum deposit amount, the minter spends up to `N × 10 × CHECK_TRANSACTION_CYCLES_REQUIRED + N × ledger_call_cost` cycles in a single `update_balance` call. The minter's cycle balance is finite and not automatically replenished by user actions.

---

### Likelihood Explanation

- The attacker must send real Bitcoin UTXOs to their deposit address. Each UTXO requires a Bitcoin transaction fee (~few hundred satoshis at low fee rates).
- The minimum deposit amount is configurable but typically ~5,000–10,000 satoshis. Sending 1,000 UTXOs at 10,000 satoshis each costs ~0.1 BTC plus fees — a modest cost relative to the damage of freezing a bridge holding hundreds of BTC.
- The attack path is a standard, unprivileged user action: send BTC to a deposit address, then call `update_balance`. No privileged keys or governance majority are required.
- The `balance_update_guard` does not mitigate this; the attacker can use a single account with many UTXOs, or multiple subaccounts in parallel.

---

### Recommendation

**Short term:** Cap the number of UTXOs processed per `update_balance` invocation (e.g., process at most 20–50 UTXOs per call and return a continuation token or require the caller to call again). Similarly, add a maximum page count to the `get_utxos` pagination loop in `management.rs`.

**Long term:** Charge the caller cycles or require a fee proportional to the number of UTXOs processed, so that the cost of the attack scales with the attacker's expenditure rather than the minter's cycle balance. Document the maximum supported UTXOs-per-address and enforce it at the protocol level.

---

### Proof of Concept

1. Attacker derives their ckBTC deposit address (a standard `get_btc_address` call).
2. Attacker sends 500 Bitcoin UTXOs, each of value `deposit_btc_min_amount + 1` satoshi, to that address and waits for `min_confirmations` blocks.
3. Attacker calls `update_balance` (an unprivileged ingress call, no special role required).
4. The minter's `update_balance` enters the loop at line 276 with 500 UTXOs. For each, it calls `check_transaction` (up to 10 times) and `mint_ckbtc` once — up to 5,500 inter-canister calls, each consuming cycles from the minter's balance.
5. Repeated across multiple subaccounts or repeated calls (after the guard releases), the minter's cycle balance is exhausted and the canister is frozen, blocking all ckBTC operations for all users. [6](#0-5) [3](#0-2)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L168-168)
```rust
    let _guard = balance_update_guard(caller_account)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L276-379)
```rust
    for utxo in processable_utxos {
        let ignored_reason = if utxo.value < deposit_btc_min_amount {
            Some(format!(
                "Ignored UTXO {} for account {caller_account} because UTXO value {} is lower than the minimum deposit amount {}",
                DisplayOutpoint(&utxo.outpoint),
                DisplayAmount(utxo.value),
                DisplayAmount(deposit_btc_min_amount)
            ))
        } else if utxo.value <= check_fee {
            Some(format!(
                "Ignored UTXO {} for account {caller_account} because UTXO value {} is not higher than the check fee {}",
                DisplayOutpoint(&utxo.outpoint),
                DisplayAmount(utxo.value),
                DisplayAmount(check_fee)
            ))
        } else {
            None
        };
        if let Some(ignored_reason) = ignored_reason {
            mutate_state(|s| {
                state::audit::ignore_utxo(s, utxo.clone(), caller_account, now, runtime)
            });
            log!(Priority::Debug, "{ignored_reason}");
            utxo_statuses.push(UtxoStatus::ValueTooSmall(utxo));
            continue;
        }
        let status = check_utxo(&utxo, &args, runtime).await?;
        match status {
            // Skip utxos that are already checked but has unknown mint status
            UtxoCheckStatus::CleanButMintUnknown => continue,
            UtxoCheckStatus::Clean => {
                mutate_state(|s| {
                    state::audit::mark_utxo_checked(s, utxo.clone(), caller_account, runtime)
                });
            }
            UtxoCheckStatus::Tainted => {
                mutate_state(|s| {
                    state::audit::quarantine_utxo(s, utxo.clone(), caller_account, now, runtime)
                });
                utxo_statuses.push(UtxoStatus::Tainted(utxo.clone()));
                continue;
            }
        };

        let amount = utxo.value - check_fee;
        let memo = MintMemo::Convert {
            txid: Some(utxo.outpoint.txid.as_ref()),
            vout: Some(utxo.outpoint.vout),
            kyt_fee: Some(check_fee),
        };

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
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L388-401)
```rust
async fn check_utxo<R: CanisterRuntime>(
    utxo: &Utxo,
    args: &UpdateBalanceArgs,
    runtime: &R,
) -> Result<UtxoCheckStatus, UpdateBalanceError> {
    use ic_btc_checker::{CHECK_TRANSACTION_CYCLES_REQUIRED, CheckTransactionStatus};

    let btc_checker_principal = read_state(|s| s.btc_checker_principal.map(Principal::from));

    if let Some(checked_utxo) = read_state(|s| s.checked_utxos.get(utxo).cloned()) {
        return Ok(checked_utxo.status);
    }
    for i in 0..MAX_CHECK_TRANSACTION_RETRY {
        match runtime
```

**File:** rs/bitcoin/ckbtc/minter/src/management.rs (L174-183)
```rust
    // Continue fetching until there are no more pages.
    while let Some(page) = response.next_page {
        let paged_request = GetUtxosRequest {
            filter: Some(bitcoin_canister::UtxosFilter::Page(page.to_vec())),
            ..request.clone()
        };
        response = bitcoin_get_utxos(&mut now, paged_request, source, runtime).await?;
        utxos.append(&mut response.utxos);
        num_pages += 1;
    }
```
