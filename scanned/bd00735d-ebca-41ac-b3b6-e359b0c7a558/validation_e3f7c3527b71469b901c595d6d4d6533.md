### Title
No Slippage Control on SNS Treasury Manager Deposit — (`rs/sns/governance/src/extensions.rs`, `rs/sns/treasury_manager/treasury_manager.did`)

---

### Summary

The SNS Treasury Manager framework's `DepositRequest` type and the `execute_treasury_manager_deposit` execution path contain no mechanism to enforce a minimum LP token output (slippage guard). Between the time an SNS governance proposal to deposit treasury funds into a DEX liquidity pool is approved by voters and the time it is executed on-chain, a front-runner can manipulate the pool ratio, causing the SNS treasury to receive significantly fewer LP tokens than voters expected.

---

### Finding Description

The `DepositRequest` Candid type, defined in `treasury_manager.did`, carries only `allowances` (the amounts to deposit) and no `min_lp_tokens_out` or equivalent slippage parameter:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
``` [1](#0-0) 

The `execute_treasury_manager_deposit` function in `rs/sns/governance/src/extensions.rs` constructs this payload via `construct_treasury_manager_deposit_payload`, calls `deposit` on the treasury manager canister, and only checks whether the call succeeded or failed — it never validates the returned `Balances` against any minimum expected LP token amount:

```rust
let balances = governance
    .env
    .call_canister(extension_canister_id, "deposit", arg_blob)
    .await
    ...
    ?;

log!(INFO, "TreasuryManager.deposit succeeded with response: {:?}", balances);
Ok(())
``` [2](#0-1) 

The `ValidatedDepositOperationArg` struct only captures `treasury_allocation_sns_e8s`, `treasury_allocation_icp_e8s`, and the raw `original` payload — no minimum output field exists: [3](#0-2) 

The proposal validation step (`validate_deposit_operation_impl`) only enforces that the requested amounts do not exceed 50% of the current treasury balance. It performs no check on expected LP token output: [4](#0-3) 

The codebase itself acknowledges this gap in two places. In `treasury_manager.did`:

> "Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved." [5](#0-4) 

And in `proposal.rs`, the `validate_and_render_register_extension` warning shown to voters:

> "Some Decentralized Exchanges lack slippage protection during deposits... This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks." [6](#0-5) 

The acknowledgment is only a UI warning — no enforcement exists at the protocol level.

---

### Impact Explanation

An SNS DAO's treasury (holding ICP and SNS tokens) can be drained of value. When a deposit proposal executes, the SNS governance canister approves a fixed token allowance to the treasury manager and calls `deposit`. Because no `min_lp_tokens_out` is enforced anywhere in the call chain, a front-runner who manipulates the DEX pool ratio immediately before execution causes the treasury to receive far fewer LP tokens than the voters approved. The deposited tokens are not returned — only tokens that could not be deposited at all are refunded. The loss is permanent and proportional to the pool manipulation.

**Vulnerability class:** Governance authorization bug / ledger conservation bug.

---

### Likelihood Explanation

SNS governance proposals are public and their execution timing is predictable (they execute after the voting period ends and the proposal is adopted). Any on-chain actor who can observe the IC mempool or governance state can front-run the deposit. The DEX (external custodian) is explicitly part of the trust model. No privileged access is required — only the ability to interact with the DEX canister before the governance proposal executes.

---

### Recommendation

1. Add a `min_lp_tokens_out : opt nat` field to `DepositRequest` in `treasury_manager.did`.
2. Require treasury manager implementations to enforce this minimum and return an error if the actual LP tokens received fall below it.
3. In `execute_treasury_manager_deposit`, validate the returned `Balances` against the minimum specified in the proposal arg, and treat a shortfall as a hard error (triggering refund logic).
4. Expose `min_lp_tokens_out` as a required field in `ValidatedDepositOperationArg` so that SNS voters can see and approve the slippage tolerance at proposal creation time.

---

### Proof of Concept

1. An SNS DAO submits a `ExecuteExtensionOperation` proposal to deposit 1,000 ICP and 500,000 SNS tokens into a DEX liquidity pool via a registered treasury manager.
2. The proposal passes after the voting period.
3. Before the proposal executes, an attacker calls the DEX canister directly to add a large one-sided liquidity position, skewing the pool ratio.
4. `execute_treasury_manager_deposit` is called:
   - `approve_treasury_manager` grants the allowance.
   - `construct_treasury_manager_deposit_payload` builds a `DepositRequest { allowances: [...] }` with no `min_lp_tokens_out`.
   - The treasury manager calls the DEX `deposit`/`addLiquidity` at the manipulated ratio.
   - The SNS treasury receives, e.g., 40% fewer LP tokens than the ratio at proposal approval time.
5. `execute_treasury_manager_deposit` receives `Ok(balances)` and logs success — no minimum check is performed.
6. The attacker removes their liquidity position, profiting from the price impact at the SNS treasury's expense. [7](#0-6) [1](#0-0)

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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
