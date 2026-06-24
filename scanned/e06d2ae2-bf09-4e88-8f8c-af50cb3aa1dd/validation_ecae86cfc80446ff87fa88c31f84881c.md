### Title
Lack of Slippage Protection in SNS Treasury Manager Deposit Enables Sandwich Attacks on Governance-Approved Deposits - (File: `rs/sns/governance/src/extensions.rs`)

---

### Summary
The SNS governance `ExecuteExtensionOperation` deposit flow constructs a `DepositRequest` containing only token allowance amounts and sends it to the KongSwap Adaptor treasury manager with no slippage bounds, minimum output, or price-impact limit. Because SNS governance proposals are public and have a predictable execution window, any unprivileged attacker can sandwich the on-chain execution: manipulate the KongSwap pool price before the proposal executes, let the deposit fill at the distorted price, then restore the price and pocket the difference — draining value from the SNS treasury.

---

### Finding Description

When an SNS governance proposal of type `ExecuteExtensionOperation` with `operation_name = "deposit"` is adopted, the governance canister calls `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs`. [1](#0-0) 

Inside that function, `construct_treasury_manager_deposit_payload` is called with the validated `original` `Precise` value: [2](#0-1) 

`construct_treasury_manager_deposit_payload` delegates to `construct_treasury_manager_deposit_allowances`, which enforces that the `PreciseMap` has **exactly two entries** — `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` — and builds a `DepositRequest { allowances }` containing only token amounts: [3](#0-2) 

The resulting `DepositRequest` type, as defined in the treasury manager interface, carries **no slippage parameters, no minimum LP-token output, and no maximum price-impact field**: [4](#0-3) 

The treasury manager DID file itself explicitly acknowledges this gap under **Known Security Risks**: [5](#0-4) 

> *"Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved."*

The validation path in `validate_deposit_operation_impl` only checks that the requested amounts do not exceed 50 % of the current treasury balance; it performs no price-bound or slippage check: [6](#0-5) 

The `ExecuteExtensionOperation` proposal type and its `ExtensionOperationArg` are publicly visible on-chain: [7](#0-6) 

---

### Impact Explanation

An attacker who monitors the SNS governance canister can observe a pending deposit proposal and its expected execution time (end of voting period). By front-running the execution on KongSwap — swapping a large amount of one asset to skew the pool ratio — the attacker forces the treasury deposit to execute at an artificially distorted price. The SNS treasury receives fewer LP tokens than it would at the fair market price. The attacker then swaps back, restoring the price and capturing the spread. The loss is borne entirely by the SNS treasury (i.e., all SNS token holders). Repeated attacks across multiple deposit proposals can drain a material fraction of the treasury over time.

---

### Likelihood Explanation

SNS governance proposals are fully public; their adoption status and execution timing are observable by anyone querying the governance canister. The attack requires only sufficient capital to move the KongSwap pool price, which is a standard MEV/sandwich technique requiring no privileged access, no governance majority, and no insider knowledge. The treasury manager DID file's own "Known Security Risks" section confirms the developers are aware the condition exists and that it is not currently mitigated in the deposit payload.

---

### Recommendation

1. **Add slippage parameters to `DepositRequest`**: Extend the `DepositRequest` type in `rs/sns/treasury_manager/treasury_manager.did` with a `min_lp_tokens_out` or `max_price_impact_bps` field, and have the KongSwap Adaptor enforce it.
2. **Propagate slippage bounds through governance**: Extend `construct_treasury_manager_deposit_payload` in `rs/sns/governance/src/extensions.rs` to accept and forward proposer-specified slippage bounds, validated against a reasonable on-chain price oracle at proposal-execution time.
3. **Time-lock or commit-reveal**: Introduce a short randomized execution delay or a commit-reveal scheme so the exact execution block cannot be predicted by front-runners.

---

### Proof of Concept

1. An SNS adopts an `ExecuteExtensionOperation` deposit proposal to deposit `X` SNS tokens and `Y` ICP into the KongSwap Adaptor. The proposal is public; its execution is scheduled at the end of the voting period.
2. The attacker observes the pending proposal via a query to the SNS governance canister.
3. Immediately before execution, the attacker swaps a large amount of ICP for SNS tokens on KongSwap, artificially lowering the SNS/ICP price in the pool.
4. The governance canister executes `execute_treasury_manager_deposit`, which calls `construct_treasury_manager_deposit_payload` and sends `DepositRequest { allowances: [X SNS, Y ICP] }` to the KongSwap Adaptor — with no slippage bound. [8](#0-7) 

5. The KongSwap Adaptor deposits at the distorted price; the SNS treasury receives fewer LP tokens than the fair-market equivalent.
6. The attacker swaps back (SNS → ICP), restoring the pool price and realizing a profit equal to the slippage extracted from the treasury deposit.

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

**File:** rs/sns/governance/src/extensions.rs (L1037-1068)
```rust
fn construct_treasury_manager_deposit_allowances(
    context: TreasuryManagerDepositContext,
    value: Precise,
) -> Result<Vec<Allowance>, String> {
    // See ic_sns_init::distributions::FractionalDeveloperVotingPower.insert_treasury_accounts
    let (treasury_sns_subaccount, treasury_icp_subaccount) = treasury_subaccounts(context.clone());

    let allowances = treasury_manager::construct_deposit_allowances(
        value,
        Asset::Token {
            symbol: context.sns_token_symbol,
            ledger_canister_id: context.sns_ledger_canister_id.get().0,
            ledger_fee_decimals: Nat::from(context.sns_ledger_transaction_fee_e8s),
        },
        Asset::Token {
            symbol: "ICP".to_string(),
            ledger_canister_id: context.icp_ledger_canister_id.get().0,
            ledger_fee_decimals: Nat::from(icp_ledger::DEFAULT_TRANSFER_FEE.get_e8s()),
        },
        sns_treasury_manager::Account {
            owner: context.sns_governance_canister_id.get().0,
            subaccount: treasury_sns_subaccount,
        },
        sns_treasury_manager::Account {
            owner: context.sns_governance_canister_id.get().0,
            subaccount: treasury_icp_subaccount,
        },
    )
    .map_err(|err| format!("Error extracting initial allowances: {err}"))?;

    Ok(allowances)
}
```

**File:** rs/sns/governance/src/extensions.rs (L1088-1099)
```rust
fn construct_treasury_manager_deposit_payload(
    context: TreasuryManagerDepositContext,
    value: Precise,
) -> Result<Vec<u8>, String> {
    let allowances = construct_treasury_manager_deposit_allowances(context, value)?;

    let arg = DepositRequest { allowances };
    let arg =
        candid::encode_one(&arg).map_err(|err| format!("Error encoding DepositRequest: {err}"))?;

    Ok(arg)
}
```

**File:** rs/sns/governance/src/extensions.rs (L1545-1579)
```rust
/// Execute a treasury manager deposit operation
async fn execute_treasury_manager_deposit(
    governance: &Governance,
    extension_canister_id: CanisterId,
    arg: ValidatedDepositOperationArg,
) -> Result<(), GovernanceError> {
    let ValidatedDepositOperationArg {
        treasury_allocation_sns_e8s,
        treasury_allocation_icp_e8s,
        original,
    } = arg;

    let context = governance.treasury_manager_deposit_context().await?;
    let arg_blob =
        construct_treasury_manager_deposit_payload(context, original).map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Failed to construct treasury manager deposit payload: {err}"),
            )
        })?;

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

**File:** rs/sns/governance/canister/governance.did (L776-782)
```text
type ExecuteExtensionOperation = record {
  extension_canister_id : opt principal;

  operation_name : opt text;

  operation_arg : opt ExtensionOperationArg;
};
```
