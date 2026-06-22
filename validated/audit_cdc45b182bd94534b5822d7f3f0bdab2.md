### Title
Unconstrained `args.from` in `icrc152_burn` Allows Controller to Burn Tokens from Any Arbitrary Account - (File: `rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary
The `icrc152_burn` endpoint in the ICRC-1 ledger canister restricts callers to controllers via `is_controller`, but performs **no validation** that the `args.from` account in `Icrc152BurnArgs` belongs to or is authorized by the caller. Any controller can supply an arbitrary victim account as `args.from` and irreversibly burn that account's entire token balance without the account owner's knowledge or consent.

---

### Finding Description
In `rs/ledger_suite/icrc1/ledger/src/main.rs`, the function `icrc152_burn_not_async` (lines 998–1086) implements the ICRC-152 authorized burn operation:

```rust
fn icrc152_burn_not_async(caller: Principal, args: Icrc152BurnArgs) -> Result<u64, Icrc152BurnError> {
    let block_idx = Access::with_ledger_mut(|ledger| {
        // ...
        if !ic_cdk::api::is_controller(&caller) {          // ← only caller check
            return Err(Icrc152BurnError::Unauthorized(...));
        }
        // ... amount/account sanity checks (zero, anonymous, minting account) ...
        let tx = Transaction {
            operation: Operation::AuthorizedBurn {
                from: args.from,   // ← args.from is NEVER validated against caller
                amount,
                caller: Some(caller),
                ...
            },
            ...
        };
        apply_transaction(ledger, tx, now, Tokens::zero())  // debits args.from
    })
}
```

The `Icrc152BurnArgs` struct (defined in `packages/icrc-ledger-types/src/icrc152/mod.rs`, lines 22–28) exposes `from: Account` as a fully caller-controlled field:

```rust
pub struct Icrc152BurnArgs {
    pub from: Account,   // ← no binding to caller identity
    pub amount: Nat,
    pub created_at_time: u64,
    pub reason: Option<String>,
}
```

The `AuthorizedBurn` operation is applied in `rs/ledger_suite/icrc1/src/lib.rs` (line 562–563):

```rust
Operation::AuthorizedBurn { from, amount, .. } => {
    context.balances_mut().burn(from, amount.clone())?;
}
```

This directly debits `from` with no allowance check, no ownership check, and no consent from the account owner. The only guards in `icrc152_burn_not_async` are:
- `args.from.owner != Principal::anonymous()`
- `args.from != minting_account`

Neither guard prevents a controller from targeting any legitimate user account.

---

### Impact Explanation
A controller of any ICRC-152-enabled ICRC-1 ledger canister can call `icrc152_burn` with `args.from` set to any user's account and `args.amount` up to that account's full balance. The `AuthorizedBurn` operation bypasses the normal allowance/approval mechanism entirely (confirmed by `rs/ledger_suite/test_utils/in_memory_ledger/src/tests.rs` lines 574–578: *"AuthorizedBurn is a privileged operation that bypasses the transfer API"*). The burn is irreversible and permanently destroys the victim's tokens, reducing total supply. The `reason` field is optional and unvalidated, so no justification is required.

**Impact**: Direct, irreversible financial loss for any token holder on any ICRC-152-enabled ledger. The attacker can drain any account to zero.

---

### Likelihood Explanation
The ICRC-152 feature is opt-in via `feature_flags.icrc152` and is currently deployed on the ICRC-1 ledger canister. The controller of a ledger is typically a governance canister (SNS or NNS), but could also be a developer-controlled canister or principal during early deployment. Any entity that achieves controller status — through a governance proposal, a compromised upgrade path, or a misconfigured deployment — can immediately exploit this. The attack requires no special cryptographic material, no subnet majority, and no social engineering beyond obtaining controller status. The call is a standard ingress update message to a public endpoint.

---

### Recommendation
Add a validation step in `icrc152_burn_not_async` that enforces a relationship between the caller and `args.from`. Options include:

1. **Require `args.from.owner == caller`**: The controller can only burn from accounts they own.
2. **Maintain an explicit allowlist**: Store a mapping of `(controller, account)` pairs that a controller is permitted to burn from, and validate `args.from` against it before executing.
3. **Require a prior on-chain authorization from the account owner**: Introduce a two-step flow where the account owner pre-authorizes the controller to burn a specific amount (analogous to ICRC-2 `approve`).

At minimum, the `reason` field should be made mandatory and logged immutably so that unauthorized burns are auditable.

---

### Proof of Concept

1. Deploy an ICRC-1 ledger with `feature_flags.icrc152 = true`. The attacker is a controller of this ledger.
2. Victim `p1` holds 1,000,000 tokens: `balance_of(p1) == 1_000_000`.
3. Attacker (controller) calls:
   ```
   icrc152_burn(Icrc152BurnArgs {
       from: Account { owner: p1, subaccount: None },
       amount: 1_000_000,
       created_at_time: <current_time>,
       reason: Some("compliance"),
   })
   ```
4. `icrc152_burn_not_async` passes the `is_controller` check (attacker is a controller).
5. `args.from` is not validated against the caller.
6. `Operation::AuthorizedBurn { from: p1, amount: 1_000_000, ... }` is applied.
7. `context.balances_mut().burn(&p1, 1_000_000)` executes, zeroing `p1`'s balance.
8. `balance_of(p1) == 0`. Tokens are permanently destroyed.

This matches the pattern demonstrated in the existing integration test at `rs/ledger_suite/tests/sm-tests/src/lib.rs` lines 6498–6518, where the controller burns from `p1`'s account — the test confirms the operation succeeds with no consent from `p1`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L998-1013)
```rust
fn icrc152_burn_not_async(
    caller: Principal,
    args: Icrc152BurnArgs,
) -> Result<u64, Icrc152BurnError> {
    let block_idx = Access::with_ledger_mut(|ledger| {
        if !ledger.feature_flags().icrc152 {
            return Err(Icrc152BurnError::GenericError {
                error_code: Nat::from(0_u64),
                message: "ICRC-152 is not enabled".to_string(),
            });
        }
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

**File:** rs/ledger_suite/icrc1/src/lib.rs (L562-564)
```rust
            Operation::AuthorizedBurn { from, amount, .. } => {
                context.balances_mut().burn(from, amount.clone())?;
            }
```

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L6498-6518)
```rust
    // --- Burn ---
    let burn_amount = 2_000_000_u64;
    let burn_result = icrc152_burn(
        &env,
        canister_id,
        controller,
        &Icrc152BurnArgs {
            from: Account::from(p1.0),
            amount: Nat::from(burn_amount),
            created_at_time: now_nanos(&env),
            reason: Some("test burn".to_string()),
        },
    );
    let burn_block_idx = burn_result.expect("icrc152_burn should succeed");
    assert_eq!(burn_block_idx, Nat::from(1_u64));

    assert_eq!(
        balance_of(&env, canister_id, p1.0),
        mint_amount - burn_amount
    );
    assert_eq!(total_supply(&env, canister_id), mint_amount - burn_amount);
```
