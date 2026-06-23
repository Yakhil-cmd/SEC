### Title
ICRC-152 `icrc152_burn` Allows Any Canister Controller to Burn Tokens of Any User Without Their Consent - (File: rs/ledger_suite/icrc1/ledger/src/main.rs)

### Summary
The `icrc152_burn` endpoint in the ICRC-1 ledger canister allows any canister controller to burn an arbitrary amount of tokens from any user's account without that user's knowledge or consent. This is an exact analog of the reported EVM vulnerability: a privileged role (controller) can burn tokens belonging to any arbitrary account.

### Finding Description
The `icrc152_burn` function in `rs/ledger_suite/icrc1/ledger/src/main.rs` implements the ICRC-152 authorized burn operation. When the `icrc152` feature flag is enabled, any principal that is a controller of the ledger canister can call `icrc152_burn` with an arbitrary `from` account and `amount`, burning tokens from that account with no consent from the account owner.

The authorization check is:

```rust
if !ic_cdk::api::is_controller(&caller) {
    return Err(Icrc152BurnError::Unauthorized(
        "caller is not a controller".to_string(),
    ));
}
```

After passing this single check, the burn proceeds unconditionally against any `args.from` account:

```rust
let tx = Transaction {
    operation: Operation::AuthorizedBurn {
        from: args.from,   // attacker-controlled: any user's account
        amount,            // attacker-controlled: any amount
        ...
    },
    ...
};
```

There is no check that `args.from` is the caller's own account, no allowance check, and no consent mechanism. The `AuthorizedBurn` operation path in `apply_transaction` also explicitly bypasses the normal allowance deduction that a regular `Burn` would require.

The `icrc152` feature flag defaults to `false` but can be enabled at init time or via upgrade args by the controller. Once enabled, every controller of the ledger canister gains the ability to burn any user's tokens.

### Impact Explanation
**Ledger conservation bug / governance authorization bug.** Any canister controller of an ICRC-152-enabled ledger can:
- Burn the entire token balance of any user account without their consent.
- Reduce total supply arbitrarily, destroying user funds.
- Target specific accounts (e.g., competitors, governance participants) for token destruction.

This directly violates the fundamental ledger invariant that a user's tokens can only be spent with their authorization (either as the sender or via an approved allowance).

### Likelihood Explanation
The `icrc152` feature flag is opt-in and disabled by default. However, for any deployed ledger that enables ICRC-152 (e.g., for compliance/regulatory use cases, which is the stated purpose of the feature), all controllers of that canister gain this power. On the IC, a canister can have multiple controllers, and the controller list can be changed via governance proposals. Any controller — including a malicious one added later — can exploit this. The attack requires only a valid ingress message from a controller principal, which is a standard unprivileged ingress path on the IC.

### Recommendation
The `icrc152_burn` endpoint should require that `args.from.owner == caller` (i.e., a controller can only burn their own tokens), OR implement an explicit allowance/consent mechanism where the account owner must pre-authorize the burn. If the intent is to allow controllers to burn any account (e.g., for regulatory compliance), this should be prominently documented as a trust assumption and the controller list should be tightly restricted by governance, with on-chain transparency mechanisms (e.g., mandatory `reason` field, event logging) enforced rather than optional.

### Proof of Concept
1. Deploy the ICRC-1 ledger with `feature_flags: Some(FeatureFlags { icrc2: true, icrc152: true })`.
2. Fund victim account `victim_account` with 1,000,000 tokens.
3. As a controller of the ledger canister, call:
   ```
   icrc152_burn(Icrc152BurnArgs {
       from: victim_account,
       amount: 1_000_000,
       created_at_time: <now>,
       reason: None,
   })
   ```
4. The call succeeds and `victim_account`'s balance is reduced to 0 with no consent from the victim.

The authorization gate is solely `is_controller(&caller)` with no account-ownership check: [1](#0-0) 

The burn then proceeds against the caller-supplied `args.from` account: [2](#0-1) 

The `AuthorizedBurn` operation bypasses allowance checks entirely (unlike regular `Burn`): [3](#0-2) 

The feature is opt-in via `FeatureFlags.icrc152` which defaults to `false` but can be enabled at init or upgrade: [4](#0-3)

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
