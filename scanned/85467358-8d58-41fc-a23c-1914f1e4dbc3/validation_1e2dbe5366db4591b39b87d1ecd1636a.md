### Title
Centralized Controller Can Burn Tokens From Any Arbitrary User Account Without Consent — (`File: rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

The `icrc152_burn` endpoint in the ICRC-1 ledger canister grants any canister controller the ability to burn tokens from **any arbitrary user account** without that account owner's knowledge or consent. The `from` field is fully caller-supplied, and the only authorization check is `is_controller`. This is a direct analog to the `salesAddress` centralization finding: a single privileged role (controller) holds unchecked power over the entire token supply of all users.

---

### Finding Description

In `rs/ledger_suite/icrc1/ledger/src/main.rs`, the `icrc152_burn_not_async` function implements the ICRC-152 authorized burn endpoint:

```rust
fn icrc152_burn_not_async(caller: Principal, args: Icrc152BurnArgs) -> Result<u64, Icrc152BurnError> {
    let block_idx = Access::with_ledger_mut(|ledger| {
        if !ledger.feature_flags().icrc152 { ... }
        if !ic_cdk::api::is_controller(&caller) {
            return Err(Icrc152BurnError::Unauthorized(...));
        }
        // ... amount/account sanity checks ...
        let tx = Transaction {
            operation: Operation::AuthorizedBurn {
                from: args.from,   // <-- fully caller-supplied, no ownership check
                amount,
                ...
            },
            ...
        };
        apply_transaction(ledger, tx, now, Tokens::zero())
    })
}
```

The `args.from` account is accepted verbatim from the caller. There is no check that `args.from.owner == caller`, no ICRC-2 allowance check, and no consent mechanism. Any controller of the ledger canister can burn tokens from any non-anonymous, non-minting account.

The `Icrc152BurnArgs` struct accepts an arbitrary `from: Account`:

```rust
pub struct Icrc152BurnArgs {
    pub from: Account,
    pub amount: Nat,
    pub created_at_time: u64,
    pub reason: Option<String>,
}
```

The endpoint is publicly exposed in the Candid interface and is reachable by any ingress sender who is a controller.

The `FeatureFlags` struct defaults `icrc152` to `false`, but any deployer who enables it (setting `icrc152: true` in `InitArgs`) exposes all token holders to this centralized burn authority — without any documented warning.

---

### Impact Explanation

**Vulnerability class:** Governance authorization bug / Ledger conservation bug.

When ICRC-152 is enabled, any controller of the ledger canister can:
1. Call `icrc152_burn` with `from` set to any victim's account.
2. Burn an arbitrary amount (up to the victim's balance) from that account.
3. Permanently destroy the victim's token holdings.

This is a complete loss of funds for any token holder on an ICRC-152-enabled ledger whose controller is compromised or acts maliciously. The controller role is a single point of failure over the entire token supply of all users — directly analogous to the `salesAddress` centralization risk in the reference report.

---

### Likelihood Explanation

**Medium.** The `icrc152` feature flag defaults to `false`, limiting exposure to ledgers that explicitly opt in. However:
- The ckETH/ckBTC ledger suite orchestrator and other production deployments explicitly set `icrc152: false`, suggesting awareness of the risk, but the design is undocumented for deployers who enable it.
- Any SNS-deployed or third-party ICRC-1 ledger that enables `icrc152: true` exposes all token holders to this risk.
- The controller of an ICRC-1 ledger is often a single principal or a small set of principals, making controller compromise a realistic threat.
- There is no on-chain documentation, consent message, or user-facing warning that enabling ICRC-152 grants controllers the ability to burn any user's tokens.

---

### Recommendation

1. Add an ownership/consent check to `icrc152_burn`: require either `args.from.owner == caller` or a valid ICRC-2 allowance from the account owner to the controller.
2. If the intended design is that controllers can burn from any account (e.g., for regulatory compliance), document this explicitly in the Candid interface, the `icrc1_supported_standards` response, and user-facing documentation.
3. Consider emitting a warning or requiring explicit acknowledgment during ledger initialization when `icrc152: true` is set, analogous to the recommendation in the reference report.

---

### Proof of Concept

**Setup:** Deploy an ICRC-1 ledger with `feature_flags: Some(FeatureFlags { icrc2: true, icrc152: true })`. Fund a victim account `p1`.

**Attack:** As the ledger controller, call:
```candid
icrc152_burn(record {
  from = record { owner = principal "<victim_p1>"; subaccount = null };
  amount = <victim_balance>;
  created_at_time = <now>;
  reason = opt "compliance"
})
```

**Result:** The victim's entire balance is burned. The victim receives no notification and had no opportunity to consent. The block is recorded with `btype = "122burn"` and `caller = <controller_principal>`, providing an audit trail but no recourse.

The attacker-controlled entry path is a direct ingress call to `icrc152_burn` on the ledger canister, with `from` set to any victim account. The IC code at lines 1009 and 1044–1051 of `rs/ledger_suite/icrc1/ledger/src/main.rs` is the necessary vulnerable step. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L638-639)
```text
  icrc152_mint : (Icrc152MintArgs) -> (Icrc152MintResult);
  icrc152_burn : (Icrc152BurnArgs) -> (Icrc152BurnResult);
```
