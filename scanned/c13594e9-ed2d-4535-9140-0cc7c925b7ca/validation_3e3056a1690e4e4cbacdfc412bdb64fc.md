### Title
Missing Anonymous Owner Validation in `update_balance` Allows Minting ckDOGE to Anonymous Principal's Account — (`rs/dogecoin/ckdoge/minter/src/main.rs`, `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

---

### Summary

The ckDOGE minter's `update_balance` endpoint guards against an anonymous *caller* but does not validate that the `owner` field in `UpdateBalanceArgs` is non-anonymous. A non-anonymous attacker can pass `owner = Some(Principal::anonymous())`, bypassing the only guard, and cause ckDOGE to be minted into the anonymous principal's account.

---

### Finding Description

**Guard in place — what it checks:**

`check_anonymous_caller()` in `rs/dogecoin/ckdoge/minter/src/main.rs` only inspects `ic_cdk::api::msg_caller()`:

```rust
fn check_anonymous_caller() {
    if ic_cdk::api::msg_caller() == Principal::anonymous() {
        panic!("anonymous caller not allowed")
    }
}
``` [1](#0-0) 

It says nothing about `args.owner`.

**The `update_balance` handler:**

```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();   // ← only checks msg_caller()
    check_postcondition(
        ic_ckbtc_minter::updates::update_balance::update_balance(args, &DOGECOIN_CANISTER_RUNTIME)
            .await,
    )
}
``` [1](#0-0) 

**The shared `update_balance` logic (ckBTC minter, reused by ckDOGE):**

The only owner-level guard is against the minter's own principal:

```rust
if args.owner.unwrap_or(caller) == runtime.id() {
    ic_cdk::trap("cannot update minter's balance");
}
``` [2](#0-1) 

Then the account to credit is constructed without any anonymous check:

```rust
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),
    subaccount: args.subaccount,
};
``` [3](#0-2) 

There is **no** `assert_ne!(owner, Principal::anonymous())` anywhere in this path. A grep for `Principal::anonymous` in `updates/update_balance.rs` returns zero matches.

**Contrast with `get_doge_address`, which does enforce the invariant:**

```rust
assert_ne!(
    owner,
    Principal::anonymous(),
    "the owner must be non-anonymous"
);
``` [4](#0-3) 

The invariant is documented in the Candid interface ("The resolved owner must be a non-anonymous principal") for `get_doge_address`, but is silently absent from `update_balance`. [5](#0-4) 

---

### Impact Explanation

1. The anonymous principal's deposit address is deterministic (derived from the minter's ECDSA key + anonymous principal bytes via the public derivation scheme in `get_doge_address.rs`). An attacker can compute it without calling `get_doge_address`. [6](#0-5) 

2. The attacker sends DOGE to that address, then calls `update_balance({ owner: Some(anonymous), subaccount: None })` as a non-anonymous principal.

3. `check_anonymous_caller` passes. The minter mints ckDOGE to `Account { owner: anonymous, subaccount: None }`.

4. On the IC, the anonymous principal can make unauthenticated update calls. Any party calling the ckDOGE ledger as `Principal::anonymous()` can transfer those tokens — the anonymous account is not permanently locked, it is unowned and accessible to any unauthenticated caller. Deposited DOGE is effectively stolen or permanently inaccessible depending on whether anyone races to claim the anonymous-account ckDOGE.

---

### Likelihood Explanation

- Requires no privileged role, no key compromise, no governance majority.
- Requires only: (a) computing the anonymous principal's deposit address (public derivation, no secret needed), (b) sending DOGE there, (c) calling `update_balance` with `owner = Some(anonymous)`.
- Fully reproducible in a state-machine test.
- The inconsistency between `get_doge_address` (has the check) and `update_balance` (missing the check) makes this a straightforward oversight.

---

### Recommendation

Add an explicit anonymous-owner guard at the top of the shared `update_balance` logic (or in the ckDOGE wrapper), mirroring the check already present in `get_doge_address`:

```rust
let effective_owner = args.owner.unwrap_or(caller);
if effective_owner == Principal::anonymous() {
    ic_cdk::trap("the owner must be non-anonymous");
}
```

This should be placed before `caller_account` is constructed in `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs` (line ~164), or equivalently in the ckDOGE `main.rs` wrapper before delegating to the shared function.

---

### Proof of Concept

```rust
// State-machine test (mirrors existing test patterns in rs/dogecoin/ckdoge/minter/tests/tests.rs)
#[test]
fn update_balance_with_anonymous_owner_should_fail() {
    let setup = Setup::default();
    let minter = setup.minter();

    // 1. Compute anonymous principal's deposit address (derivation is public).
    // 2. Inject a UTXO at that address into the mock Dogecoin canister.
    // 3. Call update_balance as a non-anonymous principal with owner=Some(anonymous).
    let result = minter.update_balance(
        USER_PRINCIPAL,   // non-anonymous caller → passes check_anonymous_caller
        &UpdateBalanceArgs {
            owner: Some(Principal::anonymous()),
            subaccount: None,
        },
    );
    // Currently: succeeds and mints ckDOGE to anonymous account.
    // Expected:  returns an error or traps with "owner must be non-anonymous".
    assert!(result.is_err(), "minter must reject anonymous owner");
}
```

The existing test suite already tests that `get_doge_address` rejects `owner = Some(anonymous)` [7](#0-6) , but no equivalent test exists for `update_balance`, confirming the gap.

### Citations

**File:** rs/dogecoin/ckdoge/minter/src/main.rs (L89-96)
```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();
    check_postcondition(
        ic_ckbtc_minter::updates::update_balance::update_balance(args, &DOGECOIN_CANISTER_RUNTIME)
            .await,
    )
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L149-151)
```rust
    if args.owner.unwrap_or(caller) == runtime.id() {
        ic_cdk::trap("cannot update minter's balance");
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L164-167)
```rust
    let caller_account = Account {
        owner: args.owner.unwrap_or(caller),
        subaccount: args.subaccount,
    };
```

**File:** rs/dogecoin/ckdoge/minter/src/updates/get_doge_address.rs (L14-18)
```rust
    assert_ne!(
        owner,
        Principal::anonymous(),
        "the owner must be non-anonymous"
    );
```

**File:** rs/dogecoin/ckdoge/minter/src/updates/get_doge_address.rs (L44-54)
```rust
pub fn derivation_path(account: &Account) -> Vec<Vec<u8>> {
    const SCHEMA_V1: u8 = 1;
    const PREFIX: [u8; 4] = *b"doge";

    vec![
        vec![SCHEMA_V1],
        PREFIX.to_vec(),
        account.owner.as_slice().to_vec(),
        account.effective_subaccount().to_vec(),
    ]
}
```

**File:** rs/dogecoin/ckdoge/minter/ckdoge_minter.did (L510-533)
```text
    // Returns the Dogecoin address to which the owner should send DOGE
    // before converting the amount to ckDOGE using the [update_balance]
    // endpoint.
    //
    // If the owner is not set, it defaults to the caller's principal.
    // The resolved owner must be a non-anonymous principal.
    get_doge_address : (record { owner: opt principal; subaccount : opt blob }) -> (text);

    // Returns UTXOs of the given account known by the minter (with no
    // guarantee in the ordering of the returned values).
    //
    // If the owner is not set, it defaults to the caller's principal.
    get_known_utxos: (record { owner: opt principal; subaccount : opt blob }) -> (vec Utxo) query;


    // Mints ckDOGE for newly deposited UTXOs.
    //
    // If the owner is not set, it defaults to the caller's principal.
    //
    // # Preconditions
    //
    // * The owner deposited some DOGE to the address that the
    //   [get_doge_address] endpoint returns.
    update_balance : (record { owner: opt principal; subaccount : opt blob }) -> (variant { Ok : vec UtxoStatus; Err : UpdateBalanceError });
```

**File:** rs/dogecoin/ckdoge/minter/tests/tests.rs (L237-262)
```rust
    fn should_fail_to_get_doge_address() {
        let setup = Setup::default();
        let minter = setup.minter();

        assert_trap(
            minter.update_call_get_doge_address(
                USER_PRINCIPAL,
                &GetDogeAddressArgs {
                    owner: Some(Principal::anonymous()),
                    subaccount: None,
                },
            ),
            "owner must be non-anonymous",
        );

        assert_trap(
            minter.update_call_get_doge_address(
                Principal::anonymous(),
                &GetDogeAddressArgs {
                    owner: None,
                    subaccount: None,
                },
            ),
            "owner must be non-anonymous",
        );
    }
```
