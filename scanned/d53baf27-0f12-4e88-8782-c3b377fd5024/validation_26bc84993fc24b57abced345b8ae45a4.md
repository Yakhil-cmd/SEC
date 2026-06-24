### Title
Missing Slippage Enforcement in SNS Treasury Manager Deposit Into DEX — (`rs/sns/governance/src/extensions.rs`)

### Summary

The SNS governance `execute_treasury_manager_deposit` function approves treasury tokens and calls `deposit` on a treasury manager canister (which deposits into an external DEX/liquidity pool), but neither the governance canister nor the `DepositRequest` type carries any minimum-received-amount or slippage tolerance parameter. The returned `Balances` from the deposit call are logged but never validated against any expected minimum. This is the direct IC analog of the reported Superform bug: a user-controlled slippage bound is either absent or unenforced during the actual deposit execution.

### Finding Description

`execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` executes in two steps:

1. `approve_treasury_manager` — grants ICRC-2 allowances for `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` to the treasury manager canister.
2. `call_canister(..., "deposit", arg_blob)` — calls the treasury manager's `deposit` endpoint. [1](#0-0) 

The `DepositRequest` type passed to the treasury manager contains only `allowances` (how much may be spent), with no field for a minimum LP-token or asset amount to be received: [2](#0-1) 

The `Balances` result returned by `deposit` is decoded and checked only for an `Err` variant (i.e., the treasury manager itself rejected the call). There is no postcondition check comparing the returned balances against the amounts the governance voters approved: [3](#0-2) 

The pre-execution validation (`validate_deposit_operation_impl`) only checks that the requested amounts do not exceed 50% of the current treasury balance — it does not establish any minimum-received bound: [4](#0-3) 

The `ValidatedDepositOperationArg` struct carries only `treasury_allocation_sns_e8s`, `treasury_allocation_icp_e8s`, and the raw `original` payload — no slippage or minimum-output field: [5](#0-4) 

The DID specification itself acknowledges this as a known risk: [6](#0-5) 

The proposal rendering also warns voters, but provides no mechanism to enforce a bound at execution time: [7](#0-6) 

### Impact Explanation

When an SNS governance proposal to deposit treasury funds into a DEX liquidity pool executes, an adversary who can observe the pending execution (governance proposals are public) can front-run or sandwich the deposit on the DEX canister. Because the governance canister imposes no minimum-received constraint, the deposit proceeds regardless of how much slippage occurred. The SNS treasury permanently loses the difference between the expected and actual LP position value. Since the allowance is already granted before `deposit` is called, the treasury manager can consume the full approved amount even if the DEX price has been manipulated to be maximally unfavorable.

### Likelihood Explanation

Governance proposals and their execution timing are fully observable on-chain. Any canister or user who can call the DEX canister can manipulate the pool price in the same round or in preceding rounds before the deposit executes. The attack requires no privileged access — only the ability to call the DEX canister, which is open to any IC principal. The likelihood increases as SNS treasuries grow and DEX liquidity pools become targets of economic attacks.

### Recommendation

1. Add a `min_received` or `max_slippage_bps` field to `ValidatedDepositOperationArg` and propagate it through `DepositRequest` to the treasury manager.
2. After `deposit` returns `Ok(balances)`, verify that the `external_custodian` balance in the returned `Balances` meets the minimum specified by the governance proposal.
3. If the postcondition fails, revert the allowance (set it to zero via a follow-up `icrc2_approve`) and return an error so the proposal execution fails cleanly.

### Proof of Concept

1. SNS governance passes a `TreasuryManagerDeposit` proposal with `treasury_allocation_sns_e8s = 1_000_000_000` and `treasury_allocation_icp_e8s = 500_000_000`.
2. Proposal execution is observable; an adversary calls the DEX canister to drain liquidity and skew the price immediately before the deposit round.
3. `execute_treasury_manager_deposit` calls `approve_treasury_manager` (allowances granted), then calls `deposit`.
4. The treasury manager deposits into the DEX at the manipulated price, receiving far fewer LP tokens than expected.
5. `execute_treasury_manager_deposit` receives `Ok(balances)` — the treasury manager did not error — logs the result, and returns `Ok(())`.
6. The governance canister records the proposal as successfully executed. The SNS treasury has permanently lost value with no on-chain record of the slippage magnitude and no revert path. [8](#0-7) [9](#0-8)

### Citations

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

**File:** rs/sns/treasury_manager/treasury_manager.did (L84-86)
```text
type DepositRequest = record {
  allowances : vec Allowance;
};
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L271-295)
```text
// Parties involved in the treasury asset management process:
// 1. treasury_owner     - e.g., the SNS Governance canister.
// 2. treasury_manager   - this canister.
// 3. external_custodian - e.g., the DEX in which assets are held temporarily.
// 4. fee_collector      - takes into account all the fees incurred due to treasury_manager's work.
// 5. payees             - e.g., developer salary payments.
// 6. payers             - e.g., liquidity provider rewards.
//
// Expects flow of assets:
//
// (A) Initialization / Deposit
// ============================
//                                      ,--------------> payees
//                                     /
// treasury_owner ---> treasury_manager ---> external_custodian
//              \                      \                       \
//               `----------------------`-----------------------`--------> fee_collector
//
// (B) Withdrawal
// ==============
//             payers --->.
//                         \
//  external_custodian ---> treasury_manager ---> treasury_owner
//                    \                     \
//                     `---------------------`---------------------------> fee_collector
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1549)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.

## Extension Configuration

The extension will be deployed and configured according to the provided parameters.",
```
