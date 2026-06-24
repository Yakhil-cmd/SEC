Audit Report

## Title
Missing Slippage Protection Enables Sandwich Attack on SNS Treasury DEX Deposits — (`rs/sns/governance/src/extensions.rs`, `rs/sns/treasury_manager/treasury_manager.did`)

## Summary
The SNS Treasury Manager `DepositRequest` type carries no `min_lp_tokens_out` or equivalent slippage floor, and `execute_treasury_manager_deposit` approves the full token allowance and calls `deposit` without validating the returned `Balances` against any minimum output. Any unprivileged actor who observes an adopted deposit proposal can sandwich the governance execution by manipulating the DEX pool price, causing the SNS treasury to permanently receive fewer LP tokens than the DAO voted to accept.

## Finding Description
`DepositRequest` in `treasury_manager.did` contains only `allowances`; there is no `min_lp_tokens_out` or `min_price_ratio` field: [1](#0-0) 

The `.did` file itself explicitly documents this as a known security risk: [2](#0-1) 

`ValidatedDepositOperationArg` parses only `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` from the proposal's `Precise` map — no minimum output field is parsed or stored: [3](#0-2) 

`validate_deposit_operation_impl` enforces only that the requested input amounts do not exceed 50% of the current treasury balance; it performs no check on expected output: [4](#0-3) 

`execute_treasury_manager_deposit` approves the full allowance unconditionally, calls `deposit`, decodes the returned `Balances`, logs them, and returns `Ok(())` — no comparison against any floor is performed: [5](#0-4) 

The exploit window is the mandatory SNS voting period (typically days) between proposal adoption and governance execution. All SNS proposals are public, so the attacker can observe the exact amounts to be deposited and time their DEX manipulation accordingly.

## Impact Explanation
An SNS treasury can hold substantial ICP and SNS token value. A successful sandwich attack permanently reduces the LP token share received by the treasury — the loss is irreversible once the deposit executes. This constitutes a **High** impact: unauthorized loss of SNS governance-controlled funds with concrete, repeatable harm to any SNS that registers a Treasury Manager pointing at a DEX without native slippage protection. This maps to the allowed impact class: *"Significant SNS security impact with concrete user or protocol harm"* ($2,000–$10,000).

## Likelihood Explanation
The attack requires no privileged access. Any on-chain participant can submit swap transactions to the target DEX during the voting window. The `.did` file explicitly acknowledges that "some liquidity pools do not implement slippage protection," confirming the attack surface exists in practice. The attack is repeatable for every future deposit proposal. `SECURITY.md` explicitly states that oracle manipulation and flash-loan-style attacks are **not** excluded from scope.

## Recommendation
1. Add a `min_lp_tokens_out` (or `min_price_ratio`) field to `DepositRequest` in `treasury_manager.did`.
2. In `execute_treasury_manager_deposit`, after decoding the returned `Balances`, verify that `external_custodian` LP token balance meets the minimum specified in the proposal; fail with a descriptive `GovernanceError` if not.
3. In `ValidatedDepositOperationArg::try_from`, parse and validate the `min_lp_tokens_out` field from the proposal's `Precise` map.
4. In `validate_deposit_operation_impl`, enforce that `min_lp_tokens_out` is present and non-zero.

## Proof of Concept
1. An SNS DAO adopts an `ExecuteExtensionOperation` deposit proposal specifying `treasury_allocation_sns_e8s = X` and `treasury_allocation_icp_e8s = Y` against a DEX pool that lacks native slippage protection.
2. Attacker observes the adopted proposal on-chain during the voting period.
3. Attacker submits a large swap on the DEX (e.g., ICP → SNS), skewing the pool ratio so SNS tokens are overpriced relative to ICP.
4. SNS governance canister executes `execute_treasury_manager_deposit`:
   - `approve_treasury_manager` grants allowance of `X` SNS and `Y` ICP to the Treasury Manager canister.
   - Treasury Manager calls the DEX `deposit` at the manipulated ratio.
   - The SNS treasury receives LP tokens representing a materially smaller pool share than intended.
5. Attacker reverses their swap, restoring the pool ratio and capturing the arbitrage profit.
6. `execute_treasury_manager_deposit` logs the returned `Balances` and returns `Ok(())` — no minimum check is performed, and the loss is permanent.

A deterministic integration test can reproduce this by: deploying a mock DEX canister that accepts arbitrary price ratios, registering a Treasury Manager pointing at it, adopting a deposit proposal, calling the mock DEX to skew the ratio before governance execution, triggering execution, and asserting that the LP tokens credited to `external_custodian` are below the expected floor.

### Citations

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L84-86)
```text
type DepositRequest = record {
  allowances : vec Allowance;
};
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

**File:** rs/sns/governance/src/extensions.rs (L1566-1609)
```rust
    // 1. Transfer funds from treasury to treasury manager
    governance
        .approve_treasury_manager(
            extension_canister_id,
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
        )
        .await?;

    // 2. Call deposit on treasury manager
    let balances = governance
        .env
        .call_canister(extension_canister_id, "deposit", arg_blob)
        .await
        .map_err(|(code, err)| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!(
                    "Canister method call {extension_canister_id}.deposit failed with code {code:?}: {err}"
                ),
            )
        })
        .and_then(|blob| {
            Decode!(&blob, sns_treasury_manager::TreasuryManagerResult).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error decoding TreasuryManager.deposit response: {err:?}"),
                )
            })
        })?
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("TreasuryManager.deposit failed: {err:?}"),
            )
        })?;

    log!(
        INFO,
        "TreasuryManager.deposit succeeded with response: {:?}",
        balances
    );

    Ok(())
```

**File:** rs/sns/governance/src/extensions.rs (L1663-1709)
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
}
```
