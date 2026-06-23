### Title
SNS Treasury Manager Deposit Executes Without Slippage Protection, Enabling Sandwich Attacks on DAO Treasury Funds — (`File: rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS governance canister's `TreasuryManagerDeposit` execution path sends treasury tokens to a DEX-backed extension canister via a `DepositRequest` that contains only token allowance amounts and no slippage protection parameters. An unprivileged on-chain attacker who observes a pending `ExecuteExtensionOperation` proposal can sandwich the deposit to drain the value difference between the expected and actual LP token allocation, at the expense of the SNS DAO treasury.

---

### Finding Description

When an SNS DAO approves a `TreasuryManagerDeposit` proposal, the execution path in `execute_treasury_manager_deposit` constructs a `DepositRequest` via `construct_treasury_manager_deposit_payload`, which encodes only the raw token allowance amounts (`treasury_allocation_sns_e8s`, `treasury_allocation_icp_e8s`) with no minimum LP tokens to receive, no maximum price impact, and no deadline. [1](#0-0) 

The `DepositRequest` type itself has no field for slippage bounds: [2](#0-1) 

The `treasury_manager.did` interface confirms this structural gap and explicitly acknowledges it as a known risk: [3](#0-2) 

The SNS governance proposal rendering also warns about this, but the warning is informational only — no enforcement mechanism exists: [4](#0-3) 

The full deposit execution flow — approve ICRC-2 allowances, then call `deposit` — passes no price bounds to the extension canister: [5](#0-4) 

---

### Impact Explanation

An attacker observing the IC state can detect a pending `ExecuteExtensionOperation` deposit proposal before it executes. By front-running the proposal execution with a large trade that skews the DEX pool price, and back-running after the deposit settles, the attacker extracts the price difference. The SNS treasury receives far fewer LP tokens than the deposited assets are worth at the pre-manipulation price. The loss is bounded only by the deposit size (up to 50% of treasury balance per proposal, per the 50%-cap validation). [6](#0-5) 

---

### Likelihood Explanation

The `ALLOWED_EXTENSIONS` list is currently empty in production (KongSwap ceased operations April 2026), so no extension can be registered today without an NNS upgrade adding a new blessed WASM hash. However, the code path is fully present and the vulnerability is structural: the `DepositRequest` API has no slippage field, so any future blessed extension that deposits into a DEX pool inherits this exposure. Once a new extension is registered and an SNS approves a deposit proposal, the attack is executable by any unprivileged observer with access to the DEX. [7](#0-6) 

---

### Recommendation

1. **Add slippage parameters to `DepositRequest`**: Extend the `DepositRequest` type in `rs/sns/treasury_manager/treasury_manager.did` and `rs/sns/treasury_manager/src/lib.rs` with a `min_lp_tokens_out` or equivalent field that the treasury manager must enforce before completing the DEX deposit.

2. **Thread slippage bounds through the governance proposal**: Extend `ValidatedDepositOperationArg` and `construct_treasury_manager_deposit_payload` in `rs/sns/governance/src/extensions.rs` to accept and forward a user-specified minimum output amount, so the SNS DAO can encode its price tolerance at proposal creation time.

3. **Enforce at execution time**: The treasury manager implementation must reject the DEX deposit if the actual LP tokens received fall below the specified minimum, and refund the full allowance to the treasury owner.

---

### Proof of Concept

1. An SNS DAO approves an `ExecuteExtensionOperation` proposal with `operation_name = "deposit"` and `treasury_allocation_sns_e8s = X`, `treasury_allocation_icp_e8s = Y`.
2. The proposal enters the execution queue. An attacker observes this on-chain.
3. The attacker submits a large swap on the DEX pool (e.g., KongSwap) that moves the SNS/ICP price significantly against the DAO (e.g., dumps SNS tokens to inflate the ICP side).
4. `execute_treasury_manager_deposit` fires: it calls `approve_treasury_manager` granting the extension canister an ICRC-2 allowance for `X` SNS and `Y` ICP, then calls `deposit` with a `DepositRequest { allowances: [...] }` containing no price floor.
5. The DEX accepts the deposit at the manipulated price. The DAO receives LP tokens worth significantly less than `X` SNS + `Y` ICP at the pre-attack market price.
6. The attacker back-runs by reversing their initial trade, profiting from the spread.

The root cause — no `min_lp_tokens_out` in `DepositRequest` — means step 4 cannot be aborted even if the price has moved arbitrarily far from the proposal-time price. [8](#0-7) [9](#0-8)

### Citations

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

**File:** rs/sns/governance/src/extensions.rs (L307-318)
```rust
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

**File:** rs/sns/treasury_manager/src/lib.rs (L284-287)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
