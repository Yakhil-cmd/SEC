### Title
ICRC-152 Controller Can Burn Any Amount of Tokens from an Arbitrary Account - (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

The `icrc152_burn` endpoint of the ICRC-1 ledger canister allows any canister **controller** to burn an arbitrary amount of tokens from **any user account** without the account owner's consent. This is a direct analog to the Yieldy M-03 finding: a privileged role (here, the IC canister controller) can unilaterally destroy any holder's balance.

---

### Finding Description

The `icrc152_burn` update method is exposed as a public canister endpoint in the ICRC-1 ledger:

```
icrc152_burn : (Icrc152BurnArgs) -> (Icrc152BurnResult);
```

`Icrc152BurnArgs` contains a caller-supplied `from: Account` field — an **arbitrary** account to burn from:

```rust
pub struct Icrc152BurnArgs {
    pub from: Account,   // ← arbitrary, caller-controlled
    pub amount: Nat,
    pub created_at_time: u64,
    pub reason: Option<String>,
}
```

The implementation in `icrc152_burn_not_async` performs only a single authorization check:

```rust
if !ic_cdk::api::is_controller(&caller) {
    return Err(Icrc152BurnError::Unauthorized(...));
}
```

After passing this check, it constructs an `Operation::AuthorizedBurn` with the caller-supplied `args.from` and calls `apply_transaction`, which directly debits the target account's balance:

```rust
Operation::AuthorizedBurn { from, amount, .. } => {
    context.balances_mut().burn(from, amount.clone())?;
}
```

Critically, `AuthorizedBurn` **bypasses the normal ICRC-2 allowance/approval mechanism entirely** — no `icrc2_approve` from the victim is required. This is explicitly documented in the test suite:

> "AuthorizedBurn is a privileged operation that bypasses the transfer API, so it must not deduct from any existing allowance even if the caller would match an approved spender."

There is no check that `args.from.owner == caller` or that the account owner has consented in any way.

---

### Impact Explanation

A malicious or compromised canister controller can call `icrc152_burn` with any user's `Account` as the `from` field and any `amount` up to that user's full balance. This permanently destroys the victim's tokens with no recourse. The attack:

- Requires no approval from the victim
- Bypasses the ICRC-2 allowance system entirely
- Leaves a permanent on-chain record (the `AuthorizedBurn` block) but the tokens are already gone
- Affects **all** ICRC-1 ledger instances that have the `icrc152` feature flag enabled

This is a **ledger conservation / governance authorization bug**: a privileged role can unilaterally reduce any user's token balance to zero.

---

### Likelihood Explanation

The `icrc152` feature must be explicitly enabled via `FeatureFlags { icrc152: true }` at ledger initialization. Any ledger instance that opts into this feature exposes all its users to this risk from any of the ledger's controllers. On the IC, canister controllers are typically governance canisters (NNS/SNS) or developer-controlled principals. A compromised controller key, a malicious governance proposal, or a rogue developer with controller access is a realistic threat. The attack path is a single direct ingress call to `icrc152_burn` — no complex exploit chain is needed.

---

### Recommendation

Restrict `icrc152_burn` so that a controller can only burn tokens from accounts that have explicitly consented, or remove the ability to specify an arbitrary `from` account entirely. The analog of the Yieldy mitigation would be to require the `from` account to match the caller, or to require a prior on-chain approval (e.g., via `icrc2_approve`) before an `AuthorizedBurn` can be executed against a user's account. At minimum, the `icrc152` feature flag documentation should prominently warn that enabling it grants controllers the power to burn any user's balance without consent.

---

### Proof of Concept

**Entry path:** Any canister controller sends an ingress update call to `icrc152_burn` on an ICRC-1 ledger with `icrc152: true`.

**Call:**
```
icrc152_burn({
    from: { owner = <victim_principal>; subaccount = null },
    amount = <victim_full_balance>,
    created_at_time = <current_time>,
    reason = null
})
```

**Execution trace:**

1. `icrc152_burn` calls `icrc152_burn_not_async(ic_cdk::api::msg_caller(), args)`. [1](#0-0) 

2. `icrc152_burn_not_async` checks `is_controller(&caller)` — passes for the attacker. [2](#0-1) 

3. No check that `args.from.owner == caller`. The `Operation::AuthorizedBurn` is constructed with the victim's account as `from`. [3](#0-2) 

4. `apply_transaction` dispatches to the `AuthorizedBurn` arm, which calls `balances_mut().burn(from, amount)` — directly debiting the victim's balance with no allowance check. [4](#0-3) 

5. The `Icrc152BurnArgs.from` field is fully attacker-controlled with no ownership validation. [5](#0-4) 

6. The `AuthorizedBurn` operation bypasses the allowance system by design, confirmed by the in-memory ledger test comment. [6](#0-5) 

**Result:** The victim's entire token balance is burned. The total supply decreases. The victim has no recourse.

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1009-1013)
```rust
        if !ic_cdk::api::is_controller(&caller) {
            return Err(Icrc152BurnError::Unauthorized(
                "caller is not a controller".to_string(),
            ));
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1044-1051)
```rust
        let tx = Transaction {
            operation: Operation::AuthorizedBurn {
                from: args.from,
                amount,
                caller: Some(caller),
                mthd: Some(MTHD_152_BURN.to_string()),
                reason: args.reason,
            },
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1088-1094)
```rust
#[update]
async fn icrc152_burn(args: Icrc152BurnArgs) -> Result<Nat, Icrc152BurnError> {
    let block_idx = icrc152_burn_not_async(ic_cdk::api::msg_caller(), args)?;
    ic_cdk::api::certified_data_set(Access::with_ledger(Ledger::root_hash));
    archive_blocks::<Access>(&LOG, MAX_MESSAGE_SIZE).await;
    Ok(Nat::from(block_idx))
}
```

**File:** rs/ledger_suite/icrc1/src/lib.rs (L562-564)
```rust
            Operation::AuthorizedBurn { from, amount, .. } => {
                context.balances_mut().burn(from, amount.clone())?;
            }
```

**File:** packages/icrc-ledger-types/src/icrc152/mod.rs (L22-28)
```rust
#[derive(Clone, Debug, CandidType, Deserialize)]
pub struct Icrc152BurnArgs {
    pub from: Account,
    pub amount: Nat,
    pub created_at_time: u64,
    pub reason: Option<String>,
}
```

**File:** rs/ledger_suite/test_utils/in_memory_ledger/src/tests.rs (L575-578)
```rust
fn should_not_consume_allowance_on_authorized_burn() {
    // AuthorizedBurn is a privileged operation that bypasses the transfer API,
    // so it must not deduct from any existing allowance even if the caller
    // would match an approved spender.
```
