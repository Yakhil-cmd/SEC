Audit Report

## Title
Unprivileged Caller Can Indefinitely Block Victim's ckBTC Minting via `update_balance` Owner Front-Running - (File: rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs)

## Summary
The `update_balance` endpoint accepts an `owner: Option<Principal>` argument and constructs the concurrency guard key from the caller-supplied value without any authorization check. Any unprivileged caller can pass a victim's principal as `owner`, acquire the per-account guard for the victim's account, and hold it for the duration of an async Bitcoin canister round-trip. By re-calling immediately after each guard release, the attacker can permanently prevent the victim from minting ckBTC from confirmed Bitcoin deposits.

## Finding Description
In `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`, the effective account is derived from the caller-supplied argument:

```rust
// L164-168
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),   // attacker-controlled
    subaccount: args.subaccount,
};
let _guard = balance_update_guard(caller_account)?;
```

The only restriction on `args.owner` is that it must not equal the minter's own canister ID (L149-151). There is no check that `args.owner` equals `caller`.

`balance_update_guard` inserts the account into the global `update_balance_accounts: BTreeSet<Account>` (guard.rs L21-23, L54). The guard is held across at least two async `get_utxos` calls to the Bitcoin canister (update_balance.rs L175-183, L229-236). While held, any concurrent call for the same account returns `GuardError::AlreadyProcessing` (guard.rs L48-49), which surfaces as `UpdateBalanceError::AlreadyProcessing` (update_balance.rs L111).

The guard is only released on `Drop` (guard.rs L63-66). The attacker's call completes with `NoNewUtxos` (the attacker has no UTXOs at the victim's derived address), the guard drops, and the attacker immediately re-calls. The victim's every attempt to call `update_balance` races against the attacker's re-entry and loses.

The `UpdateBalanceArgs` struct exposes `owner` as a public Candid field (update_balance.rs L35-41), so this is reachable from any ingress message or canister call.

## Impact Explanation
**High.** A victim who has sent BTC to their ckBTC deposit address and waited for confirmations is permanently unable to mint ckBTC. Their Bitcoin remains locked on-chain with no conversion path as long as the attack continues. This constitutes a targeted, sustained denial of service against ck-token minting for specific users, matching the allowed impact: *"Significant Chain Fusion, ck-token, ledger... security impact with concrete user or protocol harm."* The `MAX_CONCURRENT` limit of 100 (guard.rs L6) also allows simultaneous censorship of up to 100 distinct victim accounts.

## Likelihood Explanation
**Medium.** No special privileges are required; any principal can call `update_balance` with an arbitrary `owner`. A victim's principal is publicly observable from their ckBTC ledger history or prior on-chain activity. The attack cost is limited to cycles for repeated inter-canister calls to the Bitcoin canister, with no minimum deposit or stake. Large depositors or protocols performing automated BTC-to-ckBTC conversions are natural high-value targets.

## Recommendation
Remove the ability to specify a foreign `owner` in the guard path. The effective account for the concurrency guard should always be keyed on the actual caller:

```rust
let caller_account = Account {
    owner: caller,          // always use caller for the guard
    subaccount: args.subaccount,
};
let _guard = balance_update_guard(caller_account)?;
```

If third-party deposit notification is intentionally supported (e.g., a canister depositing on behalf of a user), introduce a separate non-locking notification path or require explicit pre-authorization from the owner principal before allowing a third party to trigger the guard on their behalf.

## Proof of Concept
1. Victim `V` sends BTC to their ckBTC deposit address and waits for the required confirmations.
2. Attacker `A` (any unprivileged principal) calls `update_balance({ owner: Some(V), subaccount: None })`.
3. Minter constructs `caller_account = { owner: V, subaccount: None }` and inserts it into `update_balance_accounts` via `balance_update_guard` (update_balance.rs L164-168).
4. Minter makes async `get_utxos` call to the Bitcoin canister for V's derived address; guard is held during the round-trip (update_balance.rs L175-183).
5. `V` calls `update_balance({ owner: None, subaccount: None })`; minter constructs the same account, finds it in `update_balance_accounts`, and returns `UpdateBalanceError::AlreadyProcessing` (guard.rs L48-49, update_balance.rs L111).
6. `A`'s call completes with `NoNewUtxos`; guard drops (guard.rs L63-66).
7. `A` immediately re-calls step 2. Steps 3–7 repeat indefinitely.
8. `V` can never successfully call `update_balance`; their confirmed BTC deposit cannot be converted to ckBTC.

A deterministic integration test using PocketIC can reproduce this by spawning two concurrent calls — one from an attacker principal with `owner: Some(victim)` and one from the victim — and asserting that the victim's call always returns `AlreadyProcessing` while the attacker's call cycles through `NoNewUtxos`.