The code confirms the claims. Let me verify the complete picture before rendering judgment.

All code references check out. The vulnerability is confirmed by the source. Let me render the final judgment.

Audit Report

## Title
Unauthenticated Owner Field Used as Concurrency Guard Key Enables Targeted Per-Account DoS in `update_balance` - (File: rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs)

## Summary
The `update_balance` endpoint accepts an optional `owner` principal that is never validated against the authenticated caller. The per-account concurrency guard (`balance_update_guard`) is keyed on the caller-supplied `owner`, not on the authenticated caller's identity. Any non-anonymous IC principal can therefore continuously acquire and re-acquire the guard for any victim's account, causing every `update_balance` call from the legitimate owner to return `AlreadyProcessing` indefinitely.

## Finding Description
**Root cause — unauthenticated owner field used as lock key**

In `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs` (L164–168), the account used to acquire the guard is resolved from the caller-supplied argument:

```rust
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),   // user-supplied, not validated
    subaccount: args.subaccount,
};
let _guard = balance_update_guard(caller_account)?;
```

The only identity check present (L149–151) only prevents the minter's own principal from being targeted:

```rust
if args.owner.unwrap_or(caller) == runtime.id() {
    ic_cdk::trap("cannot update minter's balance");
}
```

The mode/whitelist check (L159–160) is performed on `caller`, not on the resolved owner, so it does not restrict which account can be locked:

```rust
state::read_state(|s| s.mode.is_deposit_available_for(&caller))
    .map_err(UpdateBalanceError::TemporarilyUnavailable)?;
```

There is no check that `args.owner`, when supplied, matches `caller`.

**Guard semantics — one holder blocks all others for the same account**

In `rs/bitcoin/ckbtc/minter/src/guard.rs` (L48–54), `Guard::new` inserts the account into a global `BTreeSet`; any subsequent attempt for the same account returns `Err(GuardError::AlreadyProcessing)` until the first holder drops it:

```rust
if accounts.contains(&account) {
    return Err(GuardError::AlreadyProcessing);
}
accounts.insert(account);
```

The guard is held across at least two async inter-canister calls: a `get_utxos` call at the configured `min_confirmations` (L175–183) and, when no new UTXOs exist, a second `get_utxos` call with zero confirmations (L229–236). This provides a non-trivial window during which the victim's call is rejected.

**Entry point — publicly reachable update call**

The canister endpoint in `rs/bitcoin/ckbtc/minter/src/main.rs` (L196–200) only rejects anonymous callers:

```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();
    check_postcondition(updates::update_balance::update_balance(args, &IC_CANISTER_RUNTIME).await)
}
```

Any authenticated principal (including a freshly created, zero-balance one) can call `update_balance` with `owner = <victim_principal>`.

**Exploit flow**

1. Attacker submits `update_balance({ owner: Some(victim), subaccount: None })`.
2. Guard for `Account { owner: victim, subaccount: None }` is acquired; the call begins async Bitcoin-canister round-trips.
3. Victim submits `update_balance({})` (defaulting to their own principal). Guard acquisition fails → `AlreadyProcessing` returned immediately.
4. Attacker's call completes and releases the guard.
5. Attacker immediately re-submits step 1. Because IC message ordering is FIFO per sender, the attacker can pipeline calls so the guard is re-acquired before the victim's next attempt.
6. Repeat indefinitely at negligible cost.

The `MAX_CONCURRENT` cap of 100 (guard.rs L6) is minter-wide, not per-attacker; a single attacker holding one slot for the victim's account is sufficient to block that victim.

## Impact Explanation
This constitutes a targeted, sustained application-level denial-of-service against any specific ckBTC user's deposit flow. The victim is permanently prevented from self-initiating the `update_balance` minting step. While the attacker's calls will eventually process the victim's UTXOs (minting ckBTC to the victim's account), the victim loses control over when and how their minting is triggered. More critically, when the attacker targets an account with no pending UTXOs, the attacker's calls return `NoNewUtxos` quickly (after the second `get_utxos` call), enabling rapid lock cycling that keeps the victim's account perpetually locked even as new deposits arrive. This matches the allowed impact: **High — Application/platform-level DoS with concrete user harm in a Chain Fusion / ck-token component**.

## Likelihood Explanation
The attack requires no privileged access, no tokens, and no special setup — only a valid (non-anonymous) IC identity. The `owner` field is part of the public interface and is documented as intentionally accepting any principal. The attack is trivially scriptable, can be sustained indefinitely, and can be targeted at any specific ckBTC user. No victim interaction or mistake is required.

## Recommendation
Key the concurrency guard on the **authenticated caller** rather than on the user-supplied `owner`. If the design intent is to allow third-party minting on behalf of others, use a composite guard key of `(caller, target_account)` so that different callers do not block each other for the same target account. Alternatively, validate that `args.owner`, when present, matches `caller` unless the caller is a whitelisted service principal. The minting operation itself is idempotent, so removing per-account exclusivity for third-party callers is also a viable option.

## Proof of Concept
```
# Attacker identity: any non-anonymous IC principal (e.g., freshly generated)
# Victim: principal "aaaaa-victim-principal-..."

# Step 1: Attacker acquires the victim's update_balance lock
dfx canister --network ic call ckbtc-minter update_balance \
  '(record { owner = opt principal "aaaaa-victim-principal-..."; subaccount = null })'
# → Lock acquired; async Bitcoin canister call in flight

# Step 2 (concurrent): Victim attempts their own update_balance
dfx canister --network ic call ckbtc-minter update_balance '(record {})'
# → Err(AlreadyProcessing)   ← victim is blocked

# Step 3: Attacker loops step 1 immediately after each completion
# → Victim's every subsequent call returns AlreadyProcessing
```

A deterministic integration test using PocketIC can reproduce this by: (1) initializing the minter, (2) calling `update_balance` from principal A with `owner = Some(principal_B)`, (3) immediately calling `update_balance` from principal B with no owner, and (4) asserting the second call returns `UpdateBalanceError::AlreadyProcessing`.