### Title
SNS Treasury Manager Withdrawal Losses Are Untracked, Creating Permanent Unaccounted Treasury Shortfalls - (File: `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS governance framework allows treasury assets to be deposited into external custodians (e.g., DEX liquidity pools) via the `TreasuryManager` extension. When assets are withdrawn from those external custodians, the `execute_treasury_manager_withdraw` function receives the returned balance but performs no comparison against the originally deposited amount, records no loss, and provides no mechanism to compensate for or socialize any shortfall. If the external custodian returns fewer tokens than were deposited (due to impermanent loss, slippage, or a DEX exploit), the SNS treasury silently loses those tokens permanently with no accounting trail.

---

### Finding Description

The `execute_treasury_manager_withdraw` function in `rs/sns/governance/src/extensions.rs` calls `withdraw` on the extension canister and receives a `Balances` response, but only logs it:

```rust
log!(
    INFO,
    "TreasuryManager.withdraw succeeded with response: {:?}",
    balances
);
Ok(())
```

There is no code that:
1. Records the amount originally deposited (at `execute_treasury_manager_deposit` time)
2. Compares the returned amount to the deposited amount
3. Tracks any loss as protocol debt
4. Alerts governance of a shortfall
5. Socializes the loss fairly among token holders

The `treasury_manager.did` interface explicitly acknowledges the risk of losses in the `Known Security Risks` section, noting that "some liquidity pools do not implement slippage protection for deposits" and that "the price ratio at the time of execution may differ from the ratio at the time the proposal was approved." Despite this acknowledgment, no loss-accounting mechanism exists anywhere in the withdrawal path.

The `BalanceBook` type in `rs/sns/treasury_manager/src/lib.rs` defines a `suspense` field for transient errors, but there is no `loss` or `deficit` field, and the invariant documented in the DID (`managed_assets[k] == managed_assets[k-1] + payers[k] - payees[k] - fee_collector[k]`) silently breaks whenever the external custodian returns less than was deposited.

The deposit path enforces a 50% cap per operation via `validate_deposit_operation_impl`, meaning up to 50% of the SNS treasury can be exposed to untracked loss in a single governance cycle.

---

### Impact Explanation

When an external custodian (DEX liquidity pool) returns fewer tokens than were deposited — a routine occurrence due to impermanent loss in AMM pools — the SNS treasury permanently loses those tokens. The governance canister has no record of the loss. Subsequent `TransferSnsTreasuryFunds` proposals are validated against the actual on-chain ledger balance (which is now reduced), so the spending limit check will eventually catch an overdraft at the ledger level. However:

- The loss is invisible to governance: no event is emitted, no proposal is required to acknowledge it, and no audit trail records the deficit.
- There is no mechanism to recover the loss from future DEX yield before distributing profits.
- The loss is borne entirely by whoever holds SNS tokens at the time of withdrawal, with no fair socialization mechanism.
- Early depositors who withdraw before a loss event are made whole; later token holders absorb the full loss — a direct "first out" advantage identical to the reported vulnerability class.

**Vulnerability type:** Ledger conservation bug (governance layer).

---

### Likelihood Explanation

Impermanent loss is an inherent property of AMM-based DEX liquidity pools and occurs whenever the price ratio of the two deposited assets changes between deposit and withdrawal. For any SNS that deposits treasury assets into a DEX (the explicit design intent of the `TreasuryManager` extension), impermanent loss is not a hypothetical — it is the expected outcome whenever market prices move. No attacker action is required; normal market conditions are sufficient to trigger the loss. The only requirement is that an SNS governance majority votes to register a treasury manager extension and deposit funds, which is the intended use of the feature.

---

### Recommendation

**Short term:** In `execute_treasury_manager_withdraw`, record the deposited amounts at deposit time (in governance state or an event log), compare the returned `Balances` against those recorded amounts, and emit a governance event if a shortfall is detected.

**Long term:** Implement a loss-accounting mechanism in the `BalanceBook` (add a `loss` or `deficit` field), require future DEX yield to pay down recorded losses before being distributed, and implement a loss-socialization mechanism that distributes unavoidable losses proportionally across all SNS token holders rather than concentrating them on the last withdrawers.

---

### Proof of Concept

1. SNS governance votes to register a `TreasuryManager` extension backed by a KongSwap DEX pool.
2. SNS governance votes to deposit 50% of the ICP treasury (e.g., 10,000 ICP) and 50% of the SNS token treasury into the DEX pool via `ExecuteExtensionOperation { operation_name: "deposit" }`.
3. Market prices shift, causing impermanent loss. The DEX pool now holds assets worth only 9,000 ICP equivalent.
4. SNS governance votes to withdraw via `ExecuteExtensionOperation { operation_name: "withdraw" }`.
5. `execute_treasury_manager_withdraw` calls `withdraw` on the extension canister, receives `Balances` showing 9,000 ICP returned, logs the response, and returns `Ok(())`.
6. The 1,000 ICP loss is never recorded anywhere in governance state. No event is emitted. No proposal is required to acknowledge the deficit.
7. The SNS treasury now holds 9,000 ICP less than it held before the deposit/withdraw cycle, with no audit trail of the loss and no mechanism to compensate future token holders. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L143-160)
```text
/// Let `k` denote a particular state, `party[k]` denote the account balance of `party`
/// in state `k`, and `managed_assets` be the sum of all assets managed on behalf of
/// the treasury owner in state `k`.
///
/// Initial managed assets
/// ----------------------
/// managed_assets[0] == treasury_manager[0]
///
///     (treasury_owner[0] == external_custodian[0] == fee_collector[0]
///      == payees[0] == payers[0] == suspense[0] == 0)
///
/// Current managed assets
/// ----------------------
/// managed_assets[k] == treasury_manager[k] + treasury_owner[k] + external_custodian[k]
///
/// Under "normal operations", the following invariants hold for all k > 0:
/// 1) suspense[k] == 0
/// 2) managed_assets[k] == managed_assets[k-1] + payers[k] - payees[k] - fee_collector[k]
```

**File:** rs/sns/treasury_manager/src/lib.rs (L48-59)
```rust
#[derive(CandidType, Clone, Debug, Default, Deserialize, PartialEq)]
pub struct BalanceBook {
    pub treasury_owner: Option<Balance>,
    pub treasury_manager: Option<Balance>,
    pub external_custodian: Option<Balance>,
    pub fee_collector: Option<Balance>,
    pub payees: Option<Balance>,
    pub payers: Option<Balance>,
    /// An account in which items are entered temporarily before allocation to the correct
    /// or final account, e.g., due to transient errors.
    pub suspense: Option<Balance>,
}
```
