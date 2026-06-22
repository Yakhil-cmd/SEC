### Title
Controller Can Burn Tokens From Any Account Without Holder Consent via `icrc152_burn` - (File: `rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

The `icrc152_burn` update endpoint in the ICRC-1 ledger canister allows any **controller** of the canister to burn tokens from **any arbitrary account** without the token holder's knowledge or consent. This is a direct on-chain analog to the `DegenERC20.sol` owner-burn vulnerability: a privileged actor can unilaterally destroy another user's token balance.

---

### Finding Description

The public `#[update]` method `icrc152_burn` (and its synchronous core `icrc152_burn_not_async`) accepts an `Icrc152BurnArgs` struct containing a caller-supplied `from: Account` field. The only authorization check is `ic_cdk::api::is_controller(&caller)`. Once that passes, the function constructs an `Operation::AuthorizedBurn` transaction and applies it directly to the ledger state, debiting the specified account with no consent from the account owner. [1](#0-0) 

The core logic: [2](#0-1) 

The `Icrc152BurnArgs` type exposes `from` as a fully caller-controlled field: [3](#0-2) 

The resulting `Operation::AuthorizedBurn` variant carries no approval or allowance check — it bypasses the normal ICRC-2 allowance mechanism entirely: [4](#0-3) 

This is confirmed by the in-memory ledger test comment: *"AuthorizedBurn is a privileged operation that bypasses the transfer API, so it must not deduct from any existing allowance even if the caller would match an approved spender."* [5](#0-4) 

---

### Impact Explanation

Any controller of an ICRC-1 ledger canister with the `icrc152` feature flag enabled can call `icrc152_burn` to burn the entire balance of any user's account in a single ingress message. The token holder:

- receives no prior notice,
- has no on-chain mechanism to prevent or reverse the burn,
- cannot distinguish a legitimate compliance burn from a malicious one until after the fact.

The impact is identical to the `DegenERC20.sol` finding: complete, irreversible destruction of a victim's token holdings by a privileged actor, with no consent required.

---

### Likelihood Explanation

The `icrc152` feature is an opt-in flag set at ledger initialization or upgrade. Any ledger deployment that enables it exposes all token holders to this risk for the lifetime of the canister. Controllers are set at deployment and can include automated systems, DAOs, or individual principals. A malicious or compromised controller — or one acting under regulatory pressure — can invoke this endpoint at any time against any account. The attack requires only a single update call from a controller principal; no threshold, subnet majority, or key compromise is needed.

---

### Recommendation

1. **Require explicit account-holder consent or governance approval** before an `AuthorizedBurn` can be executed against a non-consenting account. For example, require the target account to have pre-approved the burn via a signed intent or an on-chain allowance.
2. **Emit a time-locked intent** (e.g., a pending burn that the account holder can cancel within a window) rather than executing immediately.
3. **Restrict the `from` field** so that a controller can only burn from accounts that have explicitly opted in to controller-initiated burns (e.g., via a separate `icrc152_authorize_burn` call from the account owner).
4. At minimum, **document prominently** in the ledger's interface that enabling ICRC-152 grants controllers the power to burn any account's balance without consent, so token holders can make an informed decision about which ledgers to trust.

---

### Proof of Concept

**Attacker-controlled entry path:**

1. Attacker is (or becomes) a controller of an ICRC-1 ledger canister with `icrc152 = true` in its feature flags.
2. Attacker calls the public update endpoint:

```
icrc152_burn(Icrc152BurnArgs {
    from: Account { owner: victim_principal, subaccount: None },
    amount: Nat::from(10_000_000_u64),  // victim's entire balance
    created_at_time: <current_time>,
    reason: Some("compliance".to_string()),
})
```

3. `icrc152_burn_not_async` passes the `is_controller` check, constructs `Operation::AuthorizedBurn { from: victim_account, amount: 10_000_000, ... }`, and calls `apply_transaction`.
4. `apply_transaction` calls `balances.burn(&victim_account, amount)`, which debits the victim's balance to zero with no allowance check and no notification to the victim. [6](#0-5) [7](#0-6) 

The victim's balance is permanently destroyed. The block is recorded on-chain with the controller's principal as `caller` and an optional `reason` string — but the victim has no recourse.

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

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1044-1055)
```rust
        let tx = Transaction {
            operation: Operation::AuthorizedBurn {
                from: args.from,
                amount,
                caller: Some(caller),
                mthd: Some(MTHD_152_BURN.to_string()),
                reason: args.reason,
            },
            created_at_time: Some(args.created_at_time),
            memo: None,
        };
        let (block_idx, _) =
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

**File:** rs/ledger_suite/icrc1/src/lib.rs (L65-71)
```rust
    AuthorizedBurn {
        from: Account,
        amount: Tokens,
        caller: Option<Principal>,
        mthd: Option<String>,
        reason: Option<String>,
    },
```

**File:** rs/ledger_suite/test_utils/in_memory_ledger/src/tests.rs (L575-578)
```rust
fn should_not_consume_allowance_on_authorized_burn() {
    // AuthorizedBurn is a privileged operation that bypasses the transfer API,
    // so it must not deduct from any existing allowance even if the caller
    // would match an approved spender.
```

**File:** rs/ledger_suite/common/ledger_core/src/balances.rs (L132-143)
```rust
    pub fn burn(
        &mut self,
        from: &S::AccountId,
        amount: S::Tokens,
    ) -> Result<(), BalanceError<S::Tokens>> {
        self.debit(from, amount.clone())?;
        self.token_pool = self
            .token_pool
            .checked_add(&amount)
            .expect("Overflow of the token pool while burning");
        Ok(())
    }
```
