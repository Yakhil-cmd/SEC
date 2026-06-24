### Title
No Slippage Protection in SNS Treasury Manager DEX Deposit Interface - (File: rs/sns/governance/src/extensions.rs)

### Summary

The SNS Treasury Manager extension framework allows SNS governance proposals to deposit treasury funds (SNS tokens + ICP) into DEX liquidity pools via the `deposit` operation. Neither the `DepositRequest` interface nor the `execute_treasury_manager_deposit` execution path enforces any minimum received LP-token amount. This exposes SNS treasuries to sandwich attacks during the predictable window between proposal adoption and execution.

### Finding Description

The `DepositRequest` type defined in `rs/sns/treasury_manager/treasury_manager.did` contains only `allowances` — the amounts approved for the treasury manager to spend — with no field for a minimum amount of LP tokens to be received in return: [1](#0-0) 

The `treasury_manager.did` itself explicitly acknowledges this as a known risk: [2](#0-1) 

The execution path in `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs`:
1. Calls `approve_treasury_manager` to set ICRC-2 allowances for the specified amounts
2. Calls `deposit` on the treasury manager canister
3. Decodes the `Balances` response but performs **no check** on the minimum LP tokens received [3](#0-2) 

The proposal validation in `validate_deposit_operation_impl` only checks that the requested amounts do not exceed 50% of the current treasury balance. It does not validate any minimum received amount: [4](#0-3) 

The `ValidatedDepositOperationArg` struct only carries `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` — the amounts to send — with no `min_lp_tokens_received` field: [5](#0-4) 

The test suite explicitly confirms that zero-amount deposits are accepted as valid ("Positive: zero amounts"): [6](#0-5) 

The proposal rendering only warns about the risk but does not enforce any protection: [7](#0-6) 

### Impact Explanation

An attacker who observes a pending SNS governance proposal to deposit treasury funds into a DEX (e.g., KongSwap) can:

1. Monitor the public SNS governance canister for `RegisterExtension` or `ExecuteExtensionOperation` (deposit) proposals.
2. Before the proposal executes, manipulate the DEX pool price by making large trades that skew the SNS/ICP ratio.
3. The SNS governance canister executes the deposit at the manipulated price, receiving significantly fewer LP tokens than the fair-market value of the deposited tokens.
4. The attacker restores the pool price and profits from the price impact.

The SNS treasury permanently loses value — the deposited tokens are worth more than the LP tokens received. Since the `DepositRequest` interface has no `min_lp_tokens` field, there is no on-chain mechanism to revert the deposit if the received amount is below an acceptable threshold. The comment "any undeposited tokens are automatically returned" only applies to excess tokens from ratio mismatch, not to LP token shortfall from price manipulation.

**Impact: High** — direct, irreversible loss of SNS treasury funds.

### Likelihood Explanation

SNS governance proposals have a public voting period (typically days). The execution time is predictable. Any canister caller with sufficient capital to move the DEX pool can execute this attack. The DEX (e.g., KongSwap) is an on-chain canister whose state is publicly readable, making the attack straightforward to time. The IC's lack of a traditional mempool does not prevent this attack since the manipulation happens before the governance proposal executes, not in the same block.

**Likelihood: Medium** — requires capital to move the pool but no privileged access.

### Recommendation

1. Add a `min_lp_tokens_received` field to `DepositRequest` in `rs/sns/treasury_manager/treasury_manager.did` so that Treasury Manager implementations can enforce a minimum received amount.
2. Extend `ValidatedDepositOperationArg` and the deposit proposal payload to carry a `min_lp_tokens_received` parameter specified by the SNS governance proposal submitter.
3. In `execute_treasury_manager_deposit`, verify the LP tokens received (from the `Balances` response) meet the minimum, and revert (or log a critical error) if not.
4. Alternatively, require Treasury Manager implementations to compute the minimum from the current pool state at proposal validation time and embed it in the `DepositRequest`.

### Proof of Concept

**Entry path:** Any unprivileged canister caller or user with capital on the DEX.

1. SNS governance proposal `ExecuteExtensionOperation { operation_name: "deposit", ... }` is submitted with `treasury_allocation_sns_e8s = X` and `treasury_allocation_icp_e8s = Y`.
2. Proposal passes voting; execution is imminent.
3. Attacker calls the DEX canister to buy large amounts of SNS tokens, skewing the pool ratio (e.g., doubling the SNS price in the pool).
4. SNS governance executes `execute_treasury_manager_deposit` → `approve_treasury_manager(X, Y)` → `deposit(DepositRequest { allowances: [X SNS, Y ICP] })`.
5. The DEX deposits at the manipulated ratio; the SNS treasury receives LP tokens worth significantly less than `X SNS + Y ICP` at fair market price.
6. Attacker sells SNS tokens back, restoring the pool price and pocketing the profit.
7. `execute_treasury_manager_deposit` returns `Ok(())` — no minimum check fails, no revert occurs. [8](#0-7) [1](#0-0)

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

**File:** rs/sns/governance/src/extensions.rs (L2682-2689)
```rust
            (
                "Positive: zero amounts",
                100_000_000,
                200_000_000,
                0,
                0,
                Ok(()),
            ),
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
