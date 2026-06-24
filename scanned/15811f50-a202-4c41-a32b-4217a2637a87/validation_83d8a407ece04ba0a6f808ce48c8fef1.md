### Title
SNS Treasury Manager `DepositRequest` Lacks Slippage Protection Parameters, Enabling Price-Manipulation Loss of DAO Treasury Funds — (`rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS Treasury Manager deposit API (`DepositRequest`) and its governance-side validation (`validate_deposit_operation_impl`) accept only token amounts with no slippage protection parameters. When an SNS governance proposal to deposit treasury funds into a DEX liquidity pool is approved, the actual execution can occur at a price ratio arbitrarily different from what was approved, because no `min_lp_tokens_out`, `max_price_deviation`, or equivalent bound is enforced anywhere in the IC production code. This is the direct IC analog of the MANTRA DEX M-11 finding where `belief_price: None` is hardcoded, voiding slippage protection.

---

### Finding Description

The `DepositRequest` type in the Treasury Manager API contains only `allowances: Vec<Allowance>`, where each `Allowance` carries only `asset`, `amount_decimals`, and `owner_account`. [1](#0-0) 

No field for a minimum LP token output, a maximum acceptable price deviation, or any equivalent slippage bound exists in the type.

The governance-side validation function `validate_deposit_operation_impl` in `rs/sns/governance/src/extensions.rs` performs exactly two checks:

1. Structural validity (required fields present).
2. That the requested amount does not exceed 50% of the current treasury balance. [2](#0-1) 

The `ValidatedDepositOperationArg` struct that carries the validated result contains only `treasury_allocation_sns_e8s`, `treasury_allocation_icp_e8s`, and `original`. [3](#0-2) 

No slippage bound is ever extracted, validated, or forwarded to the Treasury Manager canister. The codebase itself acknowledges this gap explicitly: [4](#0-3) 

The proposal-rendering function `validate_and_render_register_extension` in `rs/sns/governance/src/proposal.rs` emits a human-readable warning about this risk, but the warning is informational only — no enforcement follows. [5](#0-4) 

---

### Impact Explanation

An SNS governance proposal to deposit treasury funds into a DEX liquidity pool carries a specific `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s`. Voters approve the proposal based on the implied price ratio at proposal creation time. Because IC governance voting periods span days, the DEX pool price can shift substantially before execution. Since no slippage bound is encoded in the proposal or enforced by `validate_deposit_operation_impl`, the Treasury Manager canister will execute the deposit at whatever price the pool holds at execution time. The SNS treasury receives fewer LP tokens than the ratio implied at approval time, with no protocol-level recourse. The loss is permanent and proportional to the price movement.

---

### Likelihood Explanation

The attack window is the entire governance voting period — typically multiple days on SNS. Any actor who can move the DEX pool price (e.g., by providing or removing liquidity, or by executing large swaps) during that window can cause the deposit to execute at an unfavorable ratio. The SNS treasury is a known, publicly visible target with predictable deposit timing once a proposal is adopted. The `ALLOWED_EXTENSIONS` map is currently empty (KongSwap ceased operations April 2026), so no live extension is currently exploitable, but the structural gap will affect any future blessed Treasury Manager extension that deposits into a DEX. [6](#0-5) 

---

### Recommendation

Add slippage protection fields to `DepositRequest` and `ValidatedDepositOperationArg`:

- `min_lp_tokens_out: Option<Nat>` — minimum LP tokens the Treasury Manager must receive, or the deposit must abort.
- `max_price_deviation_bps: Option<u64>` — maximum acceptable deviation from the price ratio implied by `treasury_allocation_sns_e8s / treasury_allocation_icp_e8s`.

`validate_deposit_operation_impl` should enforce that these fields are present and within reasonable bounds (e.g., `max_price_deviation_bps ≤ 500`). The Treasury Manager canister's `deposit` implementation must pass these bounds to the DEX call and abort if the DEX returns fewer LP tokens than `min_lp_tokens_out`.

---

### Proof of Concept

1. SNS governance proposal submitted: deposit 1,000 ICP + 10,000 SNS tokens into a KongSwap pool. At proposal creation, pool price is 10 SNS/ICP. Voters approve.
2. During the 4-day voting period, an attacker executes large swaps on the pool, shifting the price to 100 SNS/ICP.
3. Proposal executes. `validate_deposit_operation_impl` checks only that 1,000 ICP ≤ 50% of ICP treasury balance and 10,000 SNS ≤ 50% of SNS treasury balance — both pass.
4. The Treasury Manager deposits at the manipulated 100 SNS/ICP ratio. The SNS treasury receives LP tokens representing a position worth ~10% of the ICP value it contributed, with no protocol-level check or revert.
5. The attacker reverses their position after the deposit, extracting value from the pool at the treasury's expense.

The root cause — absence of any slippage parameter in `DepositRequest` and absence of any price-bound check in `validate_deposit_operation_impl` — is entirely within IC production code, not in the external DEX. [2](#0-1) [1](#0-0)

### Citations

**File:** rs/sns/treasury_manager/src/lib.rs (L284-287)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
```

**File:** rs/sns/governance/src/extensions.rs (L48-54)
```rust
thread_local! {
    static ALLOWED_EXTENSIONS: RefCell<BTreeMap<[u8; 32], ExtensionSpec>> = const { RefCell::new(btreemap! {
        // This collection is intentionally left empty. The Kong Swap extension used to be here,
        // but they ceased operations on April 6, 2026. Consequently, that was removed
        // from this list.
    }) };
}
```

**File:** rs/sns/governance/src/extensions.rs (L276-321)
```rust
async fn validate_deposit_operation_impl(
    governance: &Governance,
    value: Option<Precise>,
) -> Result<ValidatedDepositOperationArg, String> {
    let structurally_valid = ValidatedDepositOperationArg::try_from(value)?;

    let sns_subaccount = governance.sns_treasury_subaccount();
    let icp_subaccount = governance.icp_treasury_subaccount();

    // Fail if either is asking for more than 50% of current balance.  The balance could have changed
    // since the proposal was created, and we don't assume that the proposal should work
    let sns_balance = governance
        .ledger
        .account_balance(Account {
            owner: governance.env.canister_id().get().0,
            subaccount: sns_subaccount,
        })
        .await
        .map_err(|e| format!("Failed to get SNS treasury balance: {e:?}"))?;
    let icp_balance = governance
        .nns_ledger
        .account_balance(Account {
            owner: governance.env.canister_id().get().0,
            subaccount: icp_subaccount,
        })
        .await
        .map_err(|e| format!("Failed to get ICP treasury balance: {e:?}"))?;

    let icp_requested = Tokens::from_e8s(structurally_valid.treasury_allocation_icp_e8s);
    let sns_requested = Tokens::from_e8s(structurally_valid.treasury_allocation_sns_e8s);

    // Unwrap is safe, only fails if divisor is zero, which we don't do.
    if sns_requested > sns_balance.checked_div(2).unwrap() {
        return Err(format!(
            "SNS treasury deposit request of {sns_requested} exceeds 50% of current SNS Token balance of {sns_balance}"
        ));
    }

    if icp_requested > icp_balance.checked_div(2).unwrap() {
        return Err(format!(
            "ICP treasury deposit request of {icp_requested} exceeds 50% of current ICP balance of {icp_balance}"
        ));
    }

    Ok(structurally_valid)
}
```

**File:** rs/sns/governance/src/extensions.rs (L1663-1708)
```rust
/// Validated deposit operation arguments
#[derive(Debug, Clone)]
pub struct ValidatedDepositOperationArg {
    /// Amount of SNS tokens to allocate from treasury
    pub treasury_allocation_sns_e8s: u64,
    /// Amount of ICP tokens to allocate from treasury
    pub treasury_allocation_icp_e8s: u64,
    /// Original Precise value with all fields
    pub original: Precise,
}

impl TryFrom<Option<Precise>> for ValidatedDepositOperationArg {
    type Error = String;

    fn try_from(value: Option<Precise>) -> Result<Self, Self::Error> {
        let Some(original) = value else {
            return Err("Deposit operation arguments must be provided".to_string());
        };

        let map = match &original.value {
            Some(precise::Value::Map(PreciseMap { map })) => map,
            _ => return Err("Deposit operation arguments must be a PreciseMap".to_string()),
        };

        let treasury_allocation_sns_e8s = map
            .get("treasury_allocation_sns_e8s")
            .and_then(|p| match &p.value {
                Some(precise::Value::Nat(n)) => Some(*n),
                _ => None,
            })
            .ok_or_else(|| "treasury_allocation_sns_e8s must be a Nat value".to_string())?;

        let treasury_allocation_icp_e8s = map
            .get("treasury_allocation_icp_e8s")
            .and_then(|p| match &p.value {
                Some(precise::Value::Nat(n)) => Some(*n),
                _ => None,
            })
            .ok_or_else(|| "treasury_allocation_icp_e8s must be a Nat value".to_string())?;

        Ok(Self {
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
            original,
        })
    }
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
