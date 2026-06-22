### Title
ckETH/ckERC20 Minter `retrieve_withdraw_guard` Keyed on Caller `Principal` Only, Blocking All Subaccount Withdrawals for Shared Intermediary Canisters - (File: `rs/ethereum/cketh/minter/src/guard/mod.rs`)

---

### Summary

The `retrieve_withdraw_guard` in the ckETH minter is keyed on the caller's `Principal` alone, not on the full `Account` (principal + subaccount). When an intermediary canister manages multiple users' ckETH/ckERC20 in different subaccounts and attempts concurrent withdrawals, all calls after the first trap with `AlreadyProcessing`, blocking every user sharing that intermediary canister for the duration of the in-flight async operation.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/guard/mod.rs`, the `Guard<PendingWithdrawalRequests>` struct tracks pending requests by `Principal` only:

```rust
pub struct Guard<PR: RequestsGuardedByPrincipal> {
    principal: Principal,   // ← only the owner, no subaccount
    _marker: PhantomData<PR>,
}
``` [1](#0-0) 

The guard is acquired by checking whether the caller's principal is already in `state.pending_withdrawal_principals`:

```rust
fn new(principal: Principal) -> Result<Self, GuardError> {
    mutate_state(|s| {
        ...
        if principals.contains(&principal) {
            return Err(GuardError::AlreadyProcessing);
        }
        ...
        principals.insert(principal);
``` [2](#0-1) 

Both `withdraw_eth` and `withdraw_erc20` in `rs/ethereum/cketh/minter/src/main.rs` acquire this guard keyed on the raw `caller` principal, and **trap** (not return an error) on failure:

```rust
let caller = validate_caller_not_anonymous();
let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
    ic_cdk::trap(format!(
        "Failed retrieving guard for principal {caller}: {e:?}"
    ))
});
``` [3](#0-2) [4](#0-3) 

The guard is held for the entire duration of the async operation, which spans multiple inter-canister calls (gas fee estimation, ckETH ledger burn, ckERC20 ledger burn for `withdraw_erc20`).

Critically, both `withdraw_eth` and `withdraw_erc20` explicitly accept a `from_subaccount` parameter, meaning the protocol is designed to support subaccount-based fund management by intermediary canisters:

```rust
WithdrawErc20Arg {
    ...
    from_cketh_subaccount : opt Subaccount;
    from_ckerc20_subaccount : opt Subaccount;
};
``` [5](#0-4) 

The burn is executed from `Account { owner: caller, subaccount: from_subaccount }`, so an intermediary canister holding ckETH in distinct subaccounts for distinct users is a natural and intended usage pattern.

**Contrast with ckBTC**: The ckBTC minter correctly keys its guard on the full `Account` (owner + subaccount), not just `Principal`:

```rust
pub struct Guard<PR: PendingRequests> {
    account: Account,   // ← owner + subaccount
    ...
}
``` [6](#0-5) 

This allows ckBTC's `update_balance` to process concurrent operations for different subaccounts of the same caller. The ckETH minter lacks this granularity.

---

### Impact Explanation

**Impact: Medium**

An intermediary canister (e.g., a DeFi protocol, a wallet aggregator, or any canister holding ckETH/ckERC20 in subaccounts for multiple users) can only process one withdrawal at a time across all its users. Any concurrent withdrawal attempt from a different subaccount of the same intermediary canister will **trap**, not return a graceful error. The trap propagates back to the end user as a failed call with no retry hint. All users of the intermediary are serialized behind a single in-flight withdrawal, which can span multiple IC rounds due to the async inter-canister calls involved.

---

### Likelihood Explanation

**Likelihood: Medium**

The `from_subaccount` field in `WithdrawalArg` and `WithdrawErc20Arg` explicitly signals that the protocol anticipates callers managing funds across subaccounts. Any canister-based wallet, aggregator, or DeFi protocol that holds ckETH/ckERC20 on behalf of users in per-user subaccounts will encounter this restriction. The pattern is common in IC DeFi design and is directly analogous to the `UniV3PoolHelper` intermediary described in the reference report.

---

### Recommendation

Key `retrieve_withdraw_guard` on the full `Account` (principal + subaccount) rather than on `Principal` alone, mirroring the design of ckBTC's `balance_update_guard`:

```rust
pub fn retrieve_withdraw_guard(
    account: Account,  // was: principal: Principal
) -> Result<Guard<PendingWithdrawalRequests>, GuardError> {
    Guard::new(account)
}
```

Update `PendingWithdrawalRequests` to store `BTreeSet<Account>` instead of `BTreeSet<Principal>`, and pass `Account { owner: caller, subaccount: from_subaccount }` at the call sites in `withdraw_eth` and `withdraw_erc20`. This allows concurrent withdrawals from different subaccounts of the same caller while still preventing reentrancy within a single account.

---

### Proof of Concept

1. Deploy intermediary canister `I` holding ckETH for user A in subaccount `[1u8;32]` and user B in subaccount `[2u8;32]`.
2. User A triggers a withdrawal: `I` calls `withdraw_eth({ amount: X, recipient: "0x...", from_subaccount: Some([1u8;32]) })`. The guard is acquired for `I`'s principal. The call enters the async burn flow (awaiting ledger response).
3. While step 2 is in-flight, user B triggers a withdrawal: `I` calls `withdraw_eth({ amount: Y, recipient: "0x...", from_subaccount: Some([2u8;32]) })`.
4. `retrieve_withdraw_guard(I_principal)` finds `I_principal` already in `pending_withdrawal_principals` and returns `Err(GuardError::AlreadyProcessing)`.
5. `unwrap_or_else(|e| ic_cdk::trap(...))` fires — user B's call **traps**, not returns a graceful error.
6. User B is blocked until user A's entire withdrawal pipeline (gas estimation + two ledger burns) completes and the guard is dropped. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L27-35)
```rust
impl RequestsGuardedByPrincipal for PendingWithdrawalRequests {
    fn guarded_principals(state: &mut State) -> &mut BTreeSet<Principal> {
        &mut state.pending_withdrawal_principals
    }

    fn pending_requests_count(state: &State) -> usize {
        state.eth_transactions.withdrawal_requests_len()
    }
}
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L40-44)
```rust
#[derive(Eq, PartialEq, Debug)]
pub struct Guard<PR: RequestsGuardedByPrincipal> {
    principal: Principal,
    _marker: PhantomData<PR>,
}
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L50-68)
```rust
    fn new(principal: Principal) -> Result<Self, GuardError> {
        mutate_state(|s| {
            if PR::pending_requests_count(s) >= MAX_PENDING {
                return Err(GuardError::TooManyPendingRequests);
            }
            let principals = PR::guarded_principals(s);
            if principals.contains(&principal) {
                return Err(GuardError::AlreadyProcessing);
            }
            if principals.len() >= MAX_CONCURRENT {
                return Err(GuardError::TooManyConcurrentRequests);
            }
            principals.insert(principal);
            Ok(Self {
                principal,
                _marker: PhantomData,
            })
        })
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L265-278)
```rust
#[update]
async fn withdraw_eth(
    WithdrawalArg {
        amount,
        recipient,
        from_subaccount,
    }: WithdrawalArg,
) -> Result<RetrieveEthRequest, WithdrawalError> {
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-405)
```rust
#[update]
async fn withdraw_erc20(
    WithdrawErc20Arg {
        amount,
        ckerc20_ledger_id,
        recipient,
        from_cketh_subaccount,
        from_ckerc20_subaccount,
    }: WithdrawErc20Arg,
) -> Result<RetrieveErc20Request, WithdrawErc20Error> {
    validate_ckerc20_active();
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L384-401)
```text
type WithdrawErc20Arg = record {
    // Amount of tokens to withdraw.
    // The amount is in the smallest unit of the token, e.g.,
    // ckUSDC uses 6 decimals and so to withdraw 1 ckUSDC, the amount should be 1_000_000.
    amount : nat;

    // The ledger ID for that ckERC20 token.
    ckerc20_ledger_id : principal;

    // Ethereum address to withdraw to.
    recipient : text;

    // The subaccount to burn ckETH from to pay for the transaction fee.
    from_cketh_subaccount : opt Subaccount;

    // The subaccount to burn ckERC20 from.
    from_ckerc20_subaccount : opt Subaccount;
};
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L36-39)
```rust
pub struct Guard<PR: PendingRequests> {
    account: Account,
    _marker: PhantomData<PR>,
}
```
