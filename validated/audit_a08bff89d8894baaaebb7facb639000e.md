### Title
Lack of Slippage Protection in SNS Treasury Manager Deposit Interface — (`rs/sns/treasury_manager/src/lib.rs`)

---

### Summary

The SNS Treasury Manager framework's `DepositRequest` struct and `execute_treasury_manager_deposit` execution path contain no slippage protection parameters. SNS treasury funds can be deposited into a DEX liquidity pool (e.g., KongSwap) at an arbitrarily unfavorable price ratio. The codebase explicitly acknowledges this risk but does not enforce any mitigation at the protocol level.

---

### Finding Description

The `DepositRequest` struct in `rs/sns/treasury_manager/src/lib.rs` only carries `allowances: Vec<Allowance>`:

```rust
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
``` [1](#0-0) 

The `Allowance` struct itself only encodes `asset`, `amount_decimals`, and `owner_account` — no minimum LP tokens out, no price limit, no acceptable ratio bound: [2](#0-1) 

The production execution function `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` validates only that the requested amounts do not exceed 50% of the current treasury balance, then unconditionally calls `deposit` on the extension canister with no price impact check: [3](#0-2) 

The upstream validation `validate_deposit_operation_impl` enforces only the 50% cap: [4](#0-3) 

The codebase itself acknowledges the risk in two places. In `treasury_manager.did`:

> "Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved." [5](#0-4) 

And in the rendered proposal warning in `rs/sns/governance/src/proposal.rs`:

> "Some Decentralized Exchanges lack slippage protection during deposits. Consequently, deposited asset ratios may deviate from those specified in the proposal. This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks." [6](#0-5) 

The warning is informational only — no enforcement exists anywhere in the execution path.

---

### Impact Explanation

An SNS governance proposal to deposit treasury funds into a DEX liquidity pool specifies token amounts at proposal creation time. Because the IC governance voting period spans days and proposals are public, any market participant can observe that a deposit proposal will pass and manipulate the DEX pool price before the proposal executes. The `execute_treasury_manager_deposit` function then calls `deposit` on the KongSwap adaptor with no minimum LP token output constraint, so the SNS treasury receives fewer LP tokens than the price at proposal creation implied. The attacker reverses the manipulation after execution to extract the difference. The loss is borne by all SNS token holders.

**Severity: Medium/Medium** — consistent with the reference report's classification.

---

### Likelihood Explanation

SNS governance proposals are fully public during their multi-day voting window. The deposit amounts (`treasury_allocation_sns_e8s`, `treasury_allocation_icp_e8s`) are visible on-chain before execution. Any unprivileged market participant with sufficient capital to move the KongSwap pool price can execute this attack. No privileged access, key compromise, or governance majority is required. The IC does not have a public mempool in the Ethereum sense, but the proposal execution is predictable and observable, making the attack window large rather than small.

---

### Recommendation

1. Add slippage protection fields to `DepositRequest` (e.g., `min_lp_tokens_out: Option<Nat>`) and to `Allowance` or as a top-level field.
2. Require the `TreasuryManager` trait implementers to enforce the minimum output constraint before completing the DEX deposit.
3. In `validate_deposit_operation_impl`, optionally query the current DEX price at proposal validation time and embed an acceptable price range into the validated argument, rejecting execution if the price has moved beyond the bound.
4. At minimum, surface the absence of slippage protection as a hard governance-level gate (e.g., require an explicit `slippage_tolerance_bps` field) rather than a passive warning in rendered proposal text.

---

### Proof of Concept

1. An SNS submits a `ExecuteExtensionOperation` proposal with `operation_name = "deposit"` and `treasury_allocation_icp_e8s = X`, `treasury_allocation_sns_e8s = Y` targeting the KongSwap adaptor.
2. The proposal enters the public voting period (days).
3. An attacker observes the proposal and its token amounts on-chain.
4. Before the proposal executes, the attacker swaps a large amount of ICP for SNS tokens on KongSwap, moving the pool price unfavorably for the SNS treasury.
5. The proposal passes and `execute_treasury_manager_deposit` is called. It calls `governance.env.call_canister(extension_canister_id, "deposit", arg_blob)` with no price limit.
6. The KongSwap adaptor deposits at the manipulated price; the SNS treasury receives significantly fewer LP tokens than the pre-manipulation price implied.
7. The attacker reverses their swap, profiting from the spread. The SNS treasury has permanently lost value. [7](#0-6)

### Citations

**File:** rs/sns/treasury_manager/src/lib.rs (L284-287)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
```

**File:** rs/sns/treasury_manager/src/lib.rs (L462-472)
```rust
#[derive(CandidType, Clone, Debug, PartialEq, Eq, Hash, Deserialize)]
pub struct Allowance {
    pub asset: Asset,

    /// Total amount that may be consumed, including the fees.
    #[serde(serialize_with = "serialize_nat_as_u64")]
    pub amount_decimals: Nat,

    /// The owner account is used to return the leftover assets and issue rewards.
    pub owner_account: Account,
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

**File:** rs/sns/governance/src/extensions.rs (L1546-1609)
```rust
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
