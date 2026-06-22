### Title
SNS Treasury Manager `withdraw` Lacks Slippage Protection and Actively Rejects Minimum-Amount Arguments - (`rs/sns/governance/src/extensions.rs`, `rs/sns/treasury_manager/src/lib.rs`)

---

### Summary

The SNS Treasury Manager's `withdraw` operation, executed via SNS governance proposals, provides no mechanism for specifying minimum acceptable token amounts upon withdrawal from a DEX liquidity pool. Worse, the governance validation layer **actively rejects any arguments** to the withdraw operation, making it structurally impossible to add slippage protection at the proposal level. This exposes SNS DAO treasuries to receiving fewer tokens than expected when withdrawing from a DEX pool.

---

### Finding Description

The `WithdrawRequest` struct in `rs/sns/treasury_manager/src/lib.rs` contains only a destination account field — no `min_amounts`, no `min_sns_e8s`, no `min_icp_e8s`:

```rust
pub struct WithdrawRequest {
    pub withdraw_accounts: Option<BTreeMap<Principal, Account>>,
}
``` [1](#0-0) 

The `validate_withdraw_operation` function in `rs/sns/governance/src/extensions.rs` enforces that **no arguments whatsoever** may be passed to a withdraw proposal:

```rust
fn validate_withdraw_operation(...) -> ... {
    ...
    if original.value.is_some() {
        return Err("Withdraw operation does not accept arguments at this time".to_string());
    }
    ...
}
``` [2](#0-1) [3](#0-2) 

The `execute_treasury_manager_withdraw` function then calls the treasury manager's `withdraw` endpoint and only checks for a call-level error — it performs no post-execution check that the returned token amounts meet any minimum threshold:

```rust
async fn execute_treasury_manager_withdraw(...) {
    let arg_blob = construct_treasury_manager_withdraw_payload(arg.original)...;
    let balances = governance.env.call_canister(extension_canister_id, "withdraw", arg_blob)
        .await...?
        .map_err(...)?;
    log!(INFO, "TreasuryManager.withdraw succeeded with response: {:?}", balances);
    Ok(())
}
``` [4](#0-3) 

By contrast, the `treasury_manager.did` specification explicitly acknowledges slippage risk — but **only for deposits**, not for withdrawals:

```
// Known Security Risks:
// Some liquidity pools do not implement slippage protection
// for deposits.
``` [5](#0-4) 

The `validate_and_render_register_extension` function in `rs/sns/governance/src/proposal.rs` also warns about slippage for deposits only, not withdrawals: [6](#0-5) 

---

### Impact Explanation

An SNS DAO treasury that has deposited SNS tokens and ICP into a DEX liquidity pool via the Treasury Manager extension can be drained of value when withdrawing. Between the time a governance proposal to withdraw is submitted and the time it executes (which spans the full governance voting period — typically days), a malicious DEX participant can manipulate the pool's price ratio. When the withdrawal executes, the treasury receives fewer SNS tokens and/or ICP than it should. Because the protocol actively rejects any minimum-amount arguments in the withdraw proposal, there is no on-chain mechanism to abort the withdrawal if the returned amounts are below an acceptable threshold. The financial loss accrues directly to the SNS DAO treasury (i.e., all token holders of that SNS).

---

### Likelihood Explanation

The attack window is the entire governance voting period (days to weeks). Any unprivileged DEX participant can manipulate the pool price during this window by making large swaps. The IC's asynchronous execution model means the withdrawal executes at whatever price the pool has at execution time. The `validate_deposit_operation_impl` checks treasury balances at proposal validation time but not at execution time, and the withdrawal path has no analogous check at all. The `treasury_manager.did` itself acknowledges this class of risk for deposits, confirming the developers are aware of the attack vector — yet the withdrawal path has no protection and the governance layer structurally prevents adding it.

---

### Recommendation

1. Add `min_sns_e8s` and `min_icp_e8s` fields to `WithdrawRequest` in `rs/sns/treasury_manager/src/lib.rs` and `treasury_manager.did`.
2. Remove the blanket rejection of withdraw arguments in `validate_withdraw_operation` (`rs/sns/governance/src/extensions.rs` lines 1739–1743) and instead parse and validate minimum-amount fields.
3. In `execute_treasury_manager_withdraw`, decode the returned `Balances` and verify that `treasury_owner` balances meet the minimums specified in the proposal before considering the operation successful.
4. Extend the "Known Security Risks" comment in `treasury_manager.did` to cover withdrawals, not just deposits.

---

### Proof of Concept

1. An SNS DAO submits a governance proposal: `ExecuteExtensionOperation { operation_name: "withdraw", operation_arg: { value: None } }`. Any attempt to include minimum-amount fields is rejected by `validate_withdraw_operation` with `"Withdraw operation does not accept arguments at this time"`. [7](#0-6) 

2. The proposal enters the voting period (e.g., 4 days). During this window, a malicious actor executes large swaps on the DEX pool, shifting the SNS/ICP price ratio significantly against the treasury.

3. The proposal passes and `execute_treasury_manager_withdraw` is called. It calls `extension_canister_id.withdraw(WithdrawRequest { withdraw_accounts: None })` with no minimum-amount constraint. [8](#0-7) 

4. The DEX returns tokens at the manipulated price. The governance canister logs success and returns `Ok(())` regardless of how few tokens were returned. [9](#0-8) 

5. The SNS treasury receives materially fewer tokens than it held before the deposit, with no recourse.

### Citations

**File:** rs/sns/treasury_manager/src/lib.rs (L302-306)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct WithdrawRequest {
    /// If not set, accounts specified at the time of deposit will be used for the withdrawal.
    pub withdraw_accounts: Option<BTreeMap<Principal, Account>>,
}
```

**File:** rs/sns/governance/src/extensions.rs (L396-407)
```rust
/// Validates withdraw operation arguments (currently requires empty arguments)
fn validate_withdraw_operation(
    _governance: &Governance,
    arg: ExtensionOperationArg,
) -> BoxFuture<'_, Result<ValidatedOperationArg, String>> {
    Box::pin(async move {
        let ExtensionOperationArg { value } = arg;

        ValidatedWithdrawOperationArg::try_from(value)
            .map(ValidatedOperationArg::TreasuryManagerWithdraw)
    })
}
```

**File:** rs/sns/governance/src/extensions.rs (L1612-1661)
```rust
/// Execute a treasury manager withdraw operation
async fn execute_treasury_manager_withdraw(
    governance: &Governance,
    extension_canister_id: CanisterId,
    arg: ValidatedWithdrawOperationArg,
) -> Result<(), GovernanceError> {
    let arg_blob = construct_treasury_manager_withdraw_payload(arg.original).map_err(|err| {
        GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!("Failed to construct treasury manager withdraw payload: {err}"),
        )
    })?;

    let balances = governance
        .env
        .call_canister(extension_canister_id, "withdraw", arg_blob)
        .await
        .map_err(|(code, err)| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!(
                    "Canister method call {extension_canister_id}.withdraw failed with code {code:?}: {err}"
                ),
            )
        })
        .and_then(|blob| {
            Decode!(&blob, sns_treasury_manager::TreasuryManagerResult).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!(
                        "Error decoding TreasuryManager.withdraw response: {err:?}"
                    ),
                )
            })
        })?
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("TreasuryManager.withdraw failed: {err:?}"),
            )
        })?;

    log!(
        INFO,
        "TreasuryManager.withdraw succeeded with response: {:?}",
        balances
    );

    Ok(())
}
```

**File:** rs/sns/governance/src/extensions.rs (L1733-1747)
```rust
impl TryFrom<Option<Precise>> for ValidatedWithdrawOperationArg {
    type Error = String;

    fn try_from(value: Option<Precise>) -> Result<Self, Self::Error> {
        let original = value.unwrap_or_default();

        // For now, only allow empty arguments
        // This ensures withdraw operations don't accept parameters yet
        if original.value.is_some() {
            return Err("Withdraw operation does not accept arguments at this time".to_string());
        }

        Ok(Self { original })
    }
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

**File:** rs/sns/governance/src/proposal.rs (L1542-1545)
```rust
Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
