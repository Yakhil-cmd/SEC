### Title
SNS TreasuryManager `DepositRequest` Interface Lacks Slippage Protection for DEX Liquidity Pool Deposits - (File: rs/sns/treasury_manager/src/lib.rs)

### Summary
The `DepositRequest` struct defined in the IC codebase for the SNS TreasuryManager extension interface contains no `min_lp_tokens_out` or equivalent minimum-output parameter. The `execute_treasury_manager_deposit` function in SNS governance also performs no post-call validation of returned LP token balances. This means SNS treasury deposits into DEX liquidity pools via a TreasuryManager extension can be front-run or sandwiched by an unprivileged external actor, causing the SNS DAO to receive far fewer LP tokens than expected with no on-chain protection. The codebase itself explicitly acknowledges this as a "Known Security Risk."

### Finding Description
The `DepositRequest` type is defined in `rs/sns/treasury_manager/src/lib.rs`:

```rust
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
```

It contains only the token amounts to deposit (`allowances`), with no field for a minimum acceptable LP token output. [1](#0-0) 

The `execute_treasury_manager_deposit` function in `rs/sns/governance/src/extensions.rs` approves the treasury manager, calls `deposit`, and logs the result — but never validates the returned `Balances` against any minimum expected LP token amount:

```rust
// 2. Call deposit on treasury manager
let balances = governance
    .env
    .call_canister(extension_canister_id, "deposit", arg_blob)
    ...?;

log!(INFO, "TreasuryManager.deposit succeeded with response: {:?}", balances);
Ok(())
``` [2](#0-1) 

The `DepositRequest` is constructed from only the `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` fields — no slippage bound is ever threaded through: [3](#0-2) 

The `treasury_manager.did` interface specification itself documents this as a known risk:

```
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
``` [4](#0-3) 

The SNS governance proposal renderer also warns voters:

> "Some Decentralized Exchanges lack slippage protection during deposits. Consequently, deposited asset ratios may deviate from those specified in the proposal. This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks." [5](#0-4) 

### Impact Explanation
When an SNS governance proposal to deposit treasury funds into a DEX liquidity pool is executed, a front-runner (MEV bot) can observe the pending canister call and sandwich it: first skewing the pool price, then letting the deposit execute at the unfavorable rate, then arbitraging back. Because `DepositRequest` carries no `min_lp_tokens_out` and `execute_treasury_manager_deposit` performs no post-call balance validation, the deposit succeeds regardless of how few LP tokens are returned. The SNS DAO treasury permanently loses value — ICP and SNS tokens are deposited but the LP position received is worth significantly less. The `validate_deposit_operation_impl` function only checks that the requested amount does not exceed 50% of the current treasury balance; it does not protect against adverse execution price. [6](#0-5) 

### Likelihood Explanation
The `ALLOWED_EXTENSIONS` map is currently empty in production (KongSwap was removed after ceasing operations on April 6, 2026), so no active TreasuryManager extension is currently registered. [7](#0-6)  However, the interface is a draft API explicitly designed for future DEX integrations, and the structural flaw is baked into the `DepositRequest` type and the `execute_treasury_manager_deposit` execution path. Any future blessed TreasuryManager implementation would inherit this gap. The attack requires only an unprivileged actor capable of observing pending IC canister calls and interacting with the same DEX — a standard MEV capability.

### Recommendation
1. Add a `min_lp_tokens_out` (or equivalent) field to `DepositRequest` in `rs/sns/treasury_manager/src/lib.rs` and to the `DepositRequest` record in `rs/sns/treasury_manager/treasury_manager.did`.
2. Require the SNS governance proposal payload (`treasury_allocation_sns_e8s` / `treasury_allocation_icp_e8s`) to also carry a `min_lp_tokens_out` value, validated in `validate_deposit_operation_impl`.
3. In `execute_treasury_manager_deposit`, after the `deposit` call returns `Balances`, assert that the LP token balance in `external_custodian` meets the minimum specified in the proposal.
4. Update the `TreasuryManager` trait in `rs/sns/treasury_manager/src/lib.rs` to require implementations to enforce the minimum output or return an error.

### Proof of Concept
1. An SNS governance proposal is submitted and approved to deposit `X` SNS tokens and `Y` ICP into a DEX liquidity pool via a registered TreasuryManager extension.
2. The proposal execution triggers `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs`.
3. Before the `deposit` canister call reaches the TreasuryManager extension, a front-runner observes the pending call and executes a large swap on the DEX to skew the pool price.
4. The TreasuryManager extension calls the DEX `add_liquidity` (or equivalent) with the approved token amounts and `min_lp_tokens = 0` (since `DepositRequest` carries no minimum).
5. The deposit succeeds, but the SNS DAO receives a fraction of the LP tokens it would have received at the pre-attack price.
6. The front-runner reverses their swap, profiting from the price impact.
7. `execute_treasury_manager_deposit` logs success and returns `Ok(())` with no validation of the LP token amount received. [8](#0-7)

### Citations

**File:** rs/sns/treasury_manager/src/lib.rs (L284-287)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
```

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

**File:** rs/sns/governance/src/extensions.rs (L1546-1610)
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
