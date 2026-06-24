### Title
SNS Treasury Manager Deposit Lacks Slippage Protection, Enabling Treasury Fund Loss - (`rs/sns/governance/src/extensions.rs`)

### Summary

The `execute_treasury_manager_deposit` function in SNS Governance approves and deposits a fixed amount of SNS tokens and ICP into a DEX via the Treasury Manager without any minimum output (slippage protection) parameter. The `DepositRequest` API itself has no field for specifying a minimum LP token output. Because SNS governance proposals take days to pass, the DEX price ratio at execution time can differ substantially from the ratio at proposal creation time, causing the SNS treasury to permanently lose value. The DID file explicitly acknowledges this as a "Known Security Risk" but the code provides no mechanism to mitigate it.

### Finding Description

`execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` performs two steps:

1. It calls `approve_treasury_manager`, which sets ICRC-2 allowances for the treasury manager to pull exactly `treasury_allocation_sns_e8s` SNS tokens and `treasury_allocation_icp_e8s` ICP from the SNS governance treasury.
2. It calls `deposit` on the treasury manager canister with a `DepositRequest { allowances }` — no minimum output amount is included. [1](#0-0) 

The `DepositRequest` struct contains only `allowances: Vec<Allowance>`: [2](#0-1) 

The `Allowance` struct carries `amount_decimals` (the maximum to spend) and `owner_account` (for refunding excess), but no minimum output guarantee: [3](#0-2) 

The governance proposal validation (`validate_deposit_operation_impl`) only checks that the requested amounts are ≤ 50% of the current treasury balance at proposal creation time. It does not validate any minimum LP token output: [4](#0-3) 

The DID file for the Treasury Manager interface explicitly acknowledges this structural gap: [5](#0-4) 

The root cause is that the `DepositRequest` type has no field for a minimum output amount, so even a Treasury Manager implementer who wants to enforce slippage protection cannot receive that constraint from the governance caller. The governance code also does not inspect the returned `Balances` to verify that the output meets any threshold.

### Impact Explanation

SNS treasury funds (SNS tokens + ICP) can be permanently lost to slippage when depositing into a DEX. The treasury receives fewer LP tokens than the value deposited. Because the SNS governance canister holds community funds, this is a direct, irreversible financial loss for all SNS token holders. The loss is proportional to the price movement between proposal creation and execution.

### Likelihood Explanation

High. SNS governance proposals require a multi-day voting period (typically 4+ days). DEX prices routinely move by 10–50% over such periods. Additionally, an attacker who monitors the mempool or the governance canister's pending proposals can execute a sandwich attack: manipulate the DEX price immediately before the proposal executes, causing the treasury to deposit at an unfavorable ratio, then reverse the manipulation for profit. No privileged access is required — any actor who can trade on the DEX can perform this attack.

### Recommendation

1. Add a `min_lp_tokens_out` (or equivalent) field to `DepositRequest` in `rs/sns/treasury_manager/src/lib.rs` so that Treasury Manager implementers can enforce a minimum output.
2. Modify `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` to accept and forward a minimum output parameter from the governance proposal.
3. Modify `validate_deposit_operation_impl` to require that the governance proposal includes a minimum output amount, validated against current DEX state at proposal creation time.
4. After the `deposit` call returns, inspect the returned `Balances` to verify the output meets the minimum threshold; revert (or trigger a withdrawal) if it does not.

### Proof of Concept

1. An SNS DAO submits a governance proposal to deposit 10,000 SNS tokens and 1,000 ICP into a DEX liquidity pool. At proposal creation, the SNS/ICP ratio is 10:1.
2. `validate_deposit_operation_impl` checks: 10,000 SNS ≤ 50% of SNS balance ✓, 1,000 ICP ≤ 50% of ICP balance ✓. Proposal passes validation.
3. The proposal enters the 4-day voting period. During this time, the SNS token price drops 40% relative to ICP (ratio becomes 6:1).
4. The proposal passes and `execute_treasury_manager_deposit` executes. It approves the treasury manager for 10,000 SNS + 1,000 ICP and calls `deposit`.
5. The DEX accepts the deposit at the current 6:1 ratio. To maintain pool balance, only ~6,000 SNS worth of value is matched by the 1,000 ICP. The remaining ~4,000 SNS worth of value is deposited at the unfavorable ratio, or the excess SNS is returned — but the ICP is fully consumed at the worse rate.
6. The SNS treasury permanently loses value relative to what the governance voters approved, with no slippage check to revert the transaction. [6](#0-5) [7](#0-6)

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

**File:** rs/sns/governance/src/extensions.rs (L1545-1610)
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
}
```

**File:** rs/sns/treasury_manager/src/lib.rs (L250-287)
```rust
pub trait TreasuryManager {
    /// Implements the `deposit` API function.
    fn deposit(
        &mut self,
        request: DepositRequest,
    ) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;

    /// Implements the `withdraw` API function.
    fn withdraw(
        &mut self,
        request: WithdrawRequest,
    ) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;

    /// Implements the `audit_trail` API query function.
    fn audit_trail(&self, request: AuditTrailRequest) -> AuditTrail;

    /// Implements the `balances` API query function.
    fn balances(&self, request: BalancesRequest) -> TreasuryManagerResult;

    // While the following methods go beyond just the Treasury Manager API agreement, they guide
    // the implementers to organize the code in a reasonable and predictable way.

    /// Context: the source of truth for balances are some remote canisters (e.g., the ledgers).
    /// The Treasury Manager needs to have a local cache of these balances to be able to make
    /// important decisions, e.g., how much can be refunded / withdrawn. That cache should be
    /// regularly updated, and this is the function that should do that.
    ///
    /// Should not be exposed as an API function, but rather called periodically by the canister.
    fn refresh_balances(&mut self) -> impl std::future::Future<Output = ()> + Send;

    /// Should not be exposed as an API function, but rather called periodically by the canister.
    fn issue_rewards(&mut self) -> impl std::future::Future<Output = ()> + Send;
}

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

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```
