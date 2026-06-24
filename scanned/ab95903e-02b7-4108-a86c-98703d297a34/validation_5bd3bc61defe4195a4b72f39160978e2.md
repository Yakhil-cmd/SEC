### Title
ICRC-152 Controller Has Unilateral Power to Mint and Burn Any User's Tokens Without Governance — (`File: rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

The ICRC-152 extension (`icrc152_mint` / `icrc152_burn`) grants any canister **controller** the unilateral ability to mint tokens to any account and burn tokens from **any user's account** without that user's consent, without an allowance, and without any DAO/governance approval. This is the direct IC analog of the Gearbox "Configurator has too many rights" class: a single privileged role holds excessive power over user funds with no multi-party check.

---

### Finding Description

The ICRC-1 ledger canister exposes two privileged update endpoints when the `icrc152` feature flag is enabled:

- `icrc152_mint(args: Icrc152MintArgs)` — mints arbitrary tokens to any target account.
- `icrc152_burn(args: Icrc152BurnArgs)` — burns arbitrary tokens **from any source account**, with no user consent or allowance required.

The sole authorization check in both handlers is:

```rust
if !ic_cdk::api::is_controller(&caller) {
    return Err(Icrc152BurnError::Unauthorized(...));
}
``` [1](#0-0) 

The `icrc152_burn` endpoint accepts a caller-supplied `from` field and burns from that account directly, bypassing the ICRC-2 allowance/approval mechanism entirely:

```rust
Operation::AuthorizedBurn { from: args.from, amount, ... }
``` [2](#0-1) 

This is confirmed by the test `should_not_consume_allowance_on_authorized_burn`, which explicitly documents that `AuthorizedBurn` bypasses any existing allowance:

> "AuthorizedBurn is a privileged operation that bypasses the transfer API, so it must not deduct from any existing allowance even if the caller would match an approved spender." [3](#0-2) 

The `icrc152_mint` endpoint similarly mints to any target account with no governance check: [4](#0-3) 

The feature is opt-in via `FeatureFlags { icrc152: bool }`, defaulting to `false`: [5](#0-4) 

But once enabled, **every controller** of the ledger canister holds this power unconditionally. There is no time-lock, no multi-sig, no NNS/SNS governance proposal requirement, and no user notification.

---

### Impact Explanation

**Governance authorization bug / ledger conservation bug.**

Any controller of an ICRC-152-enabled ledger can:

1. **Drain any user's balance** by calling `icrc152_burn` with `from = <victim_account>` and `amount = <full_balance>`. The victim has no recourse and no prior consent is required.
2. **Inflate the token supply** arbitrarily by calling `icrc152_mint`, diluting all existing holders.

Both operations are recorded on-chain (block types `122burn` / `122mint`) but execute immediately and irreversibly. The `from` field in `icrc152_burn` is fully attacker-controlled: [6](#0-5) 

The ledger DID confirms these are publicly exposed update endpoints: [7](#0-6) 

---

### Likelihood Explanation

**Medium.** The `icrc152` feature flag must be explicitly enabled at init or upgrade time. However:

- Any ledger that opts into ICRC-152 immediately exposes this attack surface to all its controllers.
- Controllers are not required to be governance canisters — a single developer key or a compromised canister controller suffices.
- The IC canister model allows up to 10 controllers per canister; any one of them can act unilaterally.
- There is no on-chain record of *intent* before execution — the burn is atomic and irreversible.

The attacker entry path is a direct ingress call or inter-canister call from any controller principal to `icrc152_burn` with an arbitrary `from` account.

---

### Recommendation

1. **Require governance approval** for `icrc152_burn` and `icrc152_mint` operations, analogous to how the NNS governance canister gates critical ledger mutations. The controller check should be replaced or supplemented with a DAO proposal mechanism.
2. **Restrict `icrc152_burn` to the caller's own account** (i.e., `args.from.owner == caller`), removing the ability to burn from arbitrary third-party accounts. Burning from other accounts should require an ICRC-2 allowance.
3. **Emit a time-delayed or two-phase commit** for large burns, giving users an opportunity to observe and react.
4. **Require the controller to be a governance canister** (e.g., NNS root or SNS governance) rather than any arbitrary principal, mirroring the pattern used in `rs/nns/common/src/access_control.rs`. [8](#0-7) 

---

### Proof of Concept

**Preconditions:**
- An ICRC-1 ledger is deployed with `feature_flags: Some(FeatureFlags { icrc2: true, icrc152: true })`.
- The attacker controls one of the ledger canister's controller principals (e.g., a developer key or a compromised canister).
- Victim `p1` holds a balance of `10_000_000` tokens.

**Attack steps:**

```
// Attacker (controller) calls icrc152_burn directly:
icrc152_burn({
    from: { owner: p1, subaccount: null },
    amount: 10_000_000,
    created_at_time: <current_time_nanos>,
    reason: Some("compliance")
})
// → Returns Ok(block_idx)
// → p1's balance is now 0. No consent, no allowance, no governance vote.
```

This is exactly the pattern exercised by the existing integration test `test_icrc152_mint_and_burn`, which demonstrates a controller burning from `p1`'s account without any approval from `p1`: [9](#0-8) 

The `AuthorizedBurn` operation is applied directly to the ledger balances: [10](#0-9)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L916-920)
```rust
        if !ic_cdk::api::is_controller(&caller) {
            return Err(Icrc152MintError::Unauthorized(
                "caller is not a controller".to_string(),
            ));
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1009-1012)
```rust
        if !ic_cdk::api::is_controller(&caller) {
            return Err(Icrc152BurnError::Unauthorized(
                "caller is not a controller".to_string(),
            ));
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

**File:** rs/ledger_suite/test_utils/in_memory_ledger/src/tests.rs (L574-578)
```rust
#[test]
fn should_not_consume_allowance_on_authorized_burn() {
    // AuthorizedBurn is a privileged operation that bypasses the transfer API,
    // so it must not deduct from any existing allowance even if the caller
    // would match an approved spender.
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L595-608)
```rust
#[derive(Clone, Eq, PartialEq, Debug, CandidType, Deserialize, Serialize)]
pub struct FeatureFlags {
    pub icrc2: bool,
    #[serde(default)]
    pub icrc152: bool,
}

impl FeatureFlags {
    const fn const_default() -> Self {
        Self {
            icrc2: true,
            icrc152: false,
        }
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

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L638-639)
```text
  icrc152_mint : (Icrc152MintArgs) -> (Icrc152MintResult);
  icrc152_burn : (Icrc152BurnArgs) -> (Icrc152BurnResult);
```

**File:** rs/nns/common/src/access_control.rs (L25-29)
```rust
pub fn check_caller_is_governance() {
    if caller() != PrincipalId::from(ic_nns_constants::GOVERNANCE_CANISTER_ID) {
        panic!("Only the Governance canister is allowed to call this method");
    }
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

**File:** rs/ledger_suite/icrc1/src/lib.rs (L562-563)
```rust
            Operation::AuthorizedBurn { from, amount, .. } => {
                context.balances_mut().burn(from, amount.clone())?;
```
