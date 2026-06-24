### Title
Increasing `deposit_btc_min_amount` via Governance Upgrade Immediately Suspends Previously-Valid User UTXOs — (File: `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

### Summary
The ckBTC minter's `deposit_btc_min_amount` parameter can be freely increased via NNS governance upgrade. Unlike `min_confirmations` (which is guarded against increases), there is no protection preventing `deposit_btc_min_amount` from being raised. When it is raised, UTXOs that users have already sent to their deposit addresses — based on the old, lower minimum — are immediately suspended as `ValueTooSmall` the next time `update_balance` is called. The user's BTC is locked in limbo: they cannot mint ckBTC and must pay Bitcoin network fees to retrieve their funds.

### Finding Description
In `CkBtcMinterState::upgrade()`, `deposit_btc_min_amount` is unconditionally overwritten with the new value:

```rust
if let Some(deposit_btc_min_amount) = deposit_btc_min_amount {
    self.deposit_btc_min_amount = deposit_btc_min_amount;
}
``` [1](#0-0) 

Compare this to `min_confirmations`, which has an explicit guard preventing increases:

```rust
if let Some(min_conf) = min_confirmations {
    if min_conf < self.min_confirmations {
        self.min_confirmations = min_conf;
    } else {
        log!(...); // silently ignored
    }
}
``` [2](#0-1) 

In `update_balance`, every processable UTXO is checked against the current `deposit_btc_min_amount` at call time:

```rust
let (deposit_btc_min_amount, check_fee) =
    read_state(|s| (s.deposit_btc_min_amount, s.check_fee));
for utxo in processable_utxos {
    let ignored_reason = if utxo.value < deposit_btc_min_amount {
        Some(format!("Ignored UTXO ... because UTXO value {} is lower than the minimum deposit amount {}", ...))
    } else if utxo.value <= check_fee { ... } else { None };
    if let Some(_) = ignored_reason {
        mutate_state(|s| state::audit::ignore_utxo(s, utxo.clone(), caller_account, now, runtime));
        utxo_statuses.push(UtxoStatus::ValueTooSmall(utxo));
        continue;
    }
``` [3](#0-2) 

The `effective_deposit_min_btc_amount()` helper confirms the effective floor is `max(deposit_btc_min_amount, check_fee + 1)`, but the check in `update_balance` uses the raw fields separately, meaning any increase to either field immediately re-classifies previously-valid UTXOs. [4](#0-3) 

Suspended UTXOs are re-evaluated after a 1-day cooldown, but only if the parameter is subsequently lowered. If the parameter remains elevated, the UTXO is re-suspended on every retry. [5](#0-4) 

### Impact Explanation
A user who:
1. Queries `get_minter_info()` and observes `deposit_btc_min_amount = X`
2. Sends a Bitcoin UTXO of value `Y` where `X < Y` (above the minimum)
3. Waits for the required confirmations
4. Calls `update_balance`

…will find their UTXO suspended as `ValueTooSmall` if a governance upgrade raised `deposit_btc_min_amount` to `Z > Y` between steps 2 and 4. The user cannot mint ckBTC. To recover their BTC they must create a new Bitcoin transaction sending the UTXO back to themselves, paying Bitcoin network fees. This constitutes a direct financial loss and a denial of the deposit service for funds already committed on-chain.

The `UpgradeArgs` interface explicitly exposes `deposit_btc_min_amount` as an upgradeable field: [6](#0-5) 

### Likelihood Explanation
NNS governance has already exercised this upgrade path in production — the January 2026 upgrade proposal simultaneously raised `deposit_btc_min_amount` to 300 sats and lowered `min_confirmations` to 4: [7](#0-6) 

Any future governance proposal that raises `deposit_btc_min_amount` (e.g., in response to rising Bitcoin check fees) will retroactively suspend UTXOs that users sent under the old minimum. The window between a user sending BTC and calling `update_balance` can span many Bitcoin blocks (hours), making the race condition realistic.

### Recommendation
1. **Mirror the `min_confirmations` guard**: In `CkBtcMinterState::upgrade()`, only allow `deposit_btc_min_amount` to be decreased, not increased, to prevent retroactive suspension of existing UTXOs.
2. **Snapshot the minimum at UTXO receipt time**: Record the `deposit_btc_min_amount` in effect when a UTXO is first observed, and evaluate it against that snapshot rather than the current value.
3. **Provide a no-fee BTC return path**: If a UTXO is suspended due to a parameter change (not user error), allow the user to reclaim their BTC without paying an additional Bitcoin transaction fee.

### Proof of Concept
```
1. User calls get_minter_info() → deposit_btc_min_amount = 1_000 sats
2. User sends 1_500 sats to their ckBTC deposit address (above minimum)
3. NNS governance passes upgrade: deposit_btc_min_amount = 2_000 sats
   (upgrade() sets self.deposit_btc_min_amount = 2_000 unconditionally)
4. User's UTXO reaches required confirmations; user calls update_balance()
5. update_balance() reads deposit_btc_min_amount = 2_000
   → 1_500 < 2_000 → UTXO suspended as ValueTooSmall
   → user receives UtxoStatus::ValueTooSmall(utxo)
6. User cannot mint ckBTC; must pay Bitcoin fees to recover funds
```

The test `should_mint_reevaluated_ignored_utxo` in `rs/bitcoin/ckbtc/minter/src/updates/tests.rs` already demonstrates that changing `deposit_btc_min_amount` between calls changes which UTXOs are processable, confirming the parameter takes effect immediately with no grace period for in-flight deposits. [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L705-707)
```rust
        if let Some(deposit_btc_min_amount) = deposit_btc_min_amount {
            self.deposit_btc_min_amount = deposit_btc_min_amount;
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1382-1409)
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
        }

        (processable_utxos, suspended_utxos)
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1831-1836)
```rust
    /// Compute the minimum BTC amount that can be deposited.
    /// UTXOs with a lower value will be ignored.
    pub fn effective_deposit_min_btc_amount(&self) -> u64 {
        self.deposit_btc_min_amount
            .max(self.check_fee.saturating_add(1))
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L272-300)
```rust
    let (deposit_btc_min_amount, check_fee) =
        read_state(|s| (s.deposit_btc_min_amount, s.check_fee));
    let mut utxo_statuses: Vec<UtxoStatus> = vec![];

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
```

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L248-252)
```text
type UpgradeArgs = record {
    // The minimal amount of BTC that can be converted to ckBTC.
    // UTXOs with lower values will be ignored.
    deposit_btc_min_amount : opt nat64;

```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2026_01_23.md (L40-49)
```markdown
## Upgrade args

* Change the number of confirmations required by the minter to process a deposit and mint ckBTC to 4.
* Ensure that the deposit amount is at least 300 sats, which corresponds to the dust limit of the Bitcoin network for the type of addresses used for deposits (P2WPKH).

```
git fetch
git checkout b2d93fe83a8f878a331d73df1cffed72022860b2
didc encode -d rs/bitcoin/ckbtc/minter/ckbtc_minter.did -t '(MinterArg)' '(variant { Upgrade = opt record { deposit_btc_min_amount = opt (300 : nat64); min_confirmations = opt (4 : nat32); } })' | xxd -r -p | sha256sum
```
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/tests.rs (L282-343)
```rust
    #[tokio::test]
    async fn should_mint_reevaluated_ignored_utxo() {
        struct TestCase {
            utxo_value: u64,
            initial_check_fee: u64,
            initial_deposit_btc_min_amount: u64,
            updated_check_fee: u64,
            updated_deposit_btc_min_amount: u64,
            minted_amount: u64,
        }

        for TestCase {
            utxo_value,
            initial_check_fee,
            initial_deposit_btc_min_amount,
            updated_check_fee,
            updated_deposit_btc_min_amount,
            minted_amount,
        } in [
            TestCase {
                utxo_value: DEFAULT_CHECK_FEE,
                initial_check_fee: DEFAULT_CHECK_FEE,
                initial_deposit_btc_min_amount: 0,
                updated_check_fee: DEFAULT_CHECK_FEE - 1,
                updated_deposit_btc_min_amount: 0,
                minted_amount: 1,
            },
            TestCase {
                utxo_value: 1,
                initial_check_fee: 0,
                initial_deposit_btc_min_amount: 2,
                updated_check_fee: 0,
                updated_deposit_btc_min_amount: 1,
                minted_amount: 1,
            },
        ] {
            init_state_with_ecdsa_public_key();
            let account = ledger_account();
            let mut runtime = MockCanisterRuntime::new();

            use_ckbtc_event_logger(&mut runtime);
            mock_increasing_time(&mut runtime, NOW, Duration::from_secs(1));

            let ignored_utxo = Utxo {
                value: utxo_value,
                ..ignored_utxo()
            };
            mutate_state(|s| {
                s.deposit_btc_min_amount = initial_deposit_btc_min_amount;
                s.check_fee = initial_check_fee;
                audit::ignore_utxo(
                    s,
                    ignored_utxo.clone(),
                    account,
                    NOW.checked_sub(DAY).unwrap(),
                    &runtime,
                )
            });
            mutate_state(|s| {
                s.deposit_btc_min_amount = updated_deposit_btc_min_amount;
                s.check_fee = updated_check_fee;
            });
```
