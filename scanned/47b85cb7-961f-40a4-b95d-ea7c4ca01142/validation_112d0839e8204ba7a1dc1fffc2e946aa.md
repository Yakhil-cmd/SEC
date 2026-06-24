### Title
Quarantined BTC UTXOs Are Permanently Locked in the ckBTC Minter with No Withdrawal Path - (File: rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs)

### Summary

The ckBTC minter canister permanently locks real Bitcoin UTXOs that are flagged as tainted by the Bitcoin checker. When a user deposits BTC and calls `update_balance`, any UTXO deemed tainted is quarantined (`SuspendedReason::Quarantined`) and the corresponding BTC remains locked in the minter's Bitcoin address with no mechanism for the depositor to retrieve it. This is the IC analog of the "no withdraw/unstake mechanism" vulnerability: funds enter the system but have no guaranteed exit path.

### Finding Description

The ckBTC minter's `update_balance` function processes deposited Bitcoin UTXOs. For each UTXO, it calls the Bitcoin checker canister. If the checker returns `CheckTransactionResponse::Failed`, the UTXO is quarantined via `quarantine_utxo`:

```rust
UtxoCheckStatus::Tainted => {
    mutate_state(|s| {
        state::audit::quarantine_utxo(s, utxo.clone(), caller_account, now, runtime)
    });
    utxo_statuses.push(UtxoStatus::Tainted(utxo.clone()));
    continue;
}
```

The quarantined UTXO is stored in `suspended_utxos` with `SuspendedReason::Quarantined`. The BTC itself remains at the minter's Bitcoin address (the minter controls the private key via threshold ECDSA). No ckBTC is minted for the depositor, and no mechanism exists for the depositor to reclaim the underlying BTC. The minter exposes no endpoint to return tainted BTC to its sender.

Quarantined UTXOs are periodically re-evaluated (once per day) when `update_balance` is called again, but if the Bitcoin checker consistently flags the UTXO as tainted, the BTC remains locked indefinitely. The `quarantined_utxos()` iterator confirms these UTXOs are tracked but never disbursed:

```rust
pub fn quarantined_utxos(&self) -> impl Iterator<Item = &Utxo> {
    self.suspended_utxos.iter().filter_map(|(u, r)| match r {
        SuspendedReason::ValueTooSmall => None,
        SuspendedReason::Quarantined => Some(u),
    })
}
```

The `retrieve_btc` / `retrieve_btc_with_approval` withdrawal endpoints only allow burning ckBTC tokens in exchange for BTC — since no ckBTC was minted for the tainted deposit, the depositor has no ckBTC to burn and therefore no way to initiate a withdrawal of the locked BTC.

### Impact Explanation

Real Bitcoin deposited to a minter-controlled address is permanently locked when the Bitcoin checker flags the UTXO as tainted. The depositor loses their BTC with no recourse. The minter accumulates BTC it cannot disburse, creating a growing pool of permanently locked funds. This is a **ledger conservation bug / chain-fusion mint/burn asymmetry**: the deposit path accepts BTC unconditionally (the BTC transfer happens on-chain before the minter checks it), but the quarantine path has no corresponding return path.

### Likelihood Explanation

Any unprivileged user who deposits BTC from an address that the Bitcoin checker considers tainted (e.g., an address that previously interacted with a mixer or sanctioned entity) will trigger this path. The Bitcoin checker is called on every new UTXO via `update_balance`. The user has no way to know in advance whether their UTXO will be flagged, since the check happens after the irreversible on-chain BTC transfer. This is reachable by any depositor without any privileged access.

### Recommendation

Implement a mechanism to return tainted BTC to the depositor's originating Bitcoin address (or a user-specified address), minus fees. This could be:
1. A `retrieve_tainted_btc(utxo, destination_address)` endpoint that allows the depositor to reclaim their BTC from a quarantined UTXO, subject to a destination address check (to avoid sending to another tainted address).
2. Alternatively, automatically send the BTC back to the originating address when a UTXO is quarantined, similar to how the ckETH minter handles blocked deposits by simply not minting (but in ckBTC the BTC is already held by the minter).

### Proof of Concept

1. User sends BTC to their minter deposit address (derived via `get_btc_address`).
2. User calls `update_balance`.
3. The minter calls the Bitcoin checker canister for the UTXO.
4. The checker returns `CheckTransactionResponse::Failed`.
5. The minter calls `quarantine_utxo`, storing the UTXO in `suspended_utxos` with `SuspendedReason::Quarantined`.
6. No ckBTC is minted. The user receives `UtxoStatus::Tainted(utxo)` in the response.
7. The user has no ckBTC to burn via `retrieve_btc` or `retrieve_btc_with_approval`.
8. The BTC remains at the minter's Bitcoin address indefinitely, with no user-accessible endpoint to reclaim it.

The relevant code path is confirmed in the test `should_do_btc_check_when_reevaluating_ignored_utxo` which shows a UTXO transitioning to `SuspendedReason::Quarantined` and the `update_balance` returning `UtxoStatus::Tainted` — with no subsequent minting or refund. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L311-317)
```rust
            UtxoCheckStatus::Tainted => {
                mutate_state(|s| {
                    state::audit::quarantine_utxo(s, utxo.clone(), caller_account, now, runtime)
                });
                utxo_statuses.push(UtxoStatus::Tainted(utxo.clone()));
                continue;
            }
```

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L149-164)
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
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1753-1758)
```rust
    pub fn quarantined_utxos(&self) -> impl Iterator<Item = &Utxo> {
        self.suspended_utxos.iter().filter_map(|(u, r)| match r {
            SuspendedReason::ValueTooSmall => None,
            SuspendedReason::Quarantined => Some(u),
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

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L414-420)
```text
type SuspendedReason = variant {
    // The minter ignored this UTXO because UTXO's value is too small to pay
    // the check fees.
    ValueTooSmall;
    // The Bitcoin checker considered this UTXO to be tainted.
    Quarantined;
};
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L146-153)
```rust
pub async fn retrieve_btc<R: CanisterRuntime>(
    args: RetrieveBtcArgs,
    runtime: &R,
) -> Result<RetrieveBtcOk, RetrieveBtcError> {
    let caller = ic_cdk::api::msg_caller();

    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcError::TemporarilyUnavailable)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/tests.rs (L84-136)
```rust
    #[tokio::test]
    async fn should_do_btc_check_when_reevaluating_ignored_utxo() {
        init_state_with_ecdsa_public_key();
        let account = ledger_account();
        let mut runtime = MockCanisterRuntime::new();
        use_ckbtc_event_logger(&mut runtime);
        mock_increasing_time(&mut runtime, NOW, Duration::from_secs(1));

        let ignored_utxo = ignored_utxo();
        mutate_state(|s| {
            audit::ignore_utxo(
                s,
                ignored_utxo.clone(),
                account,
                NOW.checked_sub(DAY).unwrap(),
                &runtime,
            )
        });
        mutate_state(|s| s.check_fee = ignored_utxo.value - 1);
        let events_before: Vec<_> = events().map(|event| event.payload).collect();

        mock_derive_user_address(&mut runtime, account);
        mock_get_utxos_for_account(&mut runtime, account, vec![ignored_utxo.clone()]);
        expect_check_transaction_returning(
            &mut runtime,
            ignored_utxo.clone(),
            CheckTransactionResponse::Failed(vec![]),
        );
        mock_schedule_now_process_logic(&mut runtime);

        let result = update_balance(
            UpdateBalanceArgs {
                owner: Some(account.owner),
                subaccount: account.subaccount,
            },
            &runtime,
        )
        .await;

        assert_eq!(result, Ok(vec![UtxoStatus::Tainted(ignored_utxo.clone())]));
        assert_has_new_events(
            &events_before,
            &[EventType::SuspendedUtxo {
                utxo: ignored_utxo.clone(),
                account,
                reason: SuspendedReason::Quarantined,
            }],
        );
        assert_eq!(
            suspended_utxo(&ignored_utxo),
            Some(SuspendedReason::Quarantined)
        );
    }
```
