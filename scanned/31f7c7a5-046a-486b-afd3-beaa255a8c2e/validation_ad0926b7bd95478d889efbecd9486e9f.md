### Title
Missing Slippage Protection in SNS Treasury Manager `deposit()` and `withdraw()` Operations - (File: `rs/sns/governance/src/extensions.rs`, `rs/sns/treasury_manager/treasury_manager.did`)

---

### Summary

The SNS Treasury Manager framework, which allows SNS DAOs to deposit treasury assets (SNS tokens + ICP) into external DEX liquidity pools, provides no slippage tolerance mechanism for either the `deposit()` or `withdraw()` operations. The `DepositRequest` type carries no minimum LP token output field, and the `WithdrawRequest` validation layer actively rejects any user-supplied arguments, making it structurally impossible to specify a minimum token return. This mirrors the StaderHavenStakingManager pattern exactly: assets are exchanged for a different asset at a rate determined at execution time, with no user-controlled floor.

---

### Finding Description

The SNS governance canister executes treasury manager operations via `execute_treasury_manager_deposit()` and `execute_treasury_manager_withdraw()` in `rs/sns/governance/src/extensions.rs`.

**Deposit path:** `execute_treasury_manager_deposit()` approves the treasury manager canister to spend SNS tokens and ICP, then calls `deposit()` on the treasury manager with a `DepositRequest { allowances: vec![...] }`. The `DepositRequest` type contains only allowance amounts — there is no `min_lp_tokens_out` or equivalent field. [1](#0-0) 

The `treasury_manager.did` file itself acknowledges this as a known security risk: [2](#0-1) 

**Withdraw path:** `construct_treasury_manager_withdraw_payload()` always constructs a `WithdrawRequest { withdraw_accounts: None }` — no minimum output amounts are encoded: [3](#0-2) 

Critically, `ValidatedWithdrawOperationArg::try_from()` **actively rejects any non-empty argument**, making it structurally impossible for a governance proposal to ever supply a minimum output floor for the withdraw operation: [4](#0-3) 

The proposal rendering code in `rs/sns/governance/src/proposal.rs` acknowledges the front-running risk for deposits but only as a warning comment, not as an enforced protocol constraint: [5](#0-4) 

---

### Impact Explanation

An SNS DAO's treasury assets (SNS tokens + ICP) can be deposited into a DEX liquidity pool at an arbitrarily unfavorable price ratio, or withdrawn at an arbitrarily unfavorable rate, with no on-chain protection. Because SNS governance proposals have a mandatory voting period (typically days), the exact execution time is publicly known in advance. A malicious canister or MEV-aware actor can:

1. Observe the pending governance proposal specifying the deposit/withdraw amounts.
2. Sandwich the execution: manipulate the DEX pool price immediately before the governance canister calls `deposit()` or `withdraw()`.
3. The treasury manager executes at the manipulated price with no minimum output check.
4. The attacker profits; the SNS treasury permanently loses value.

For `withdraw()`, the situation is worse than for `deposit()`: the code structurally prevents any future governance proposal from supplying a minimum output, because `ValidatedWithdrawOperationArg::try_from()` rejects all non-empty arguments at the validation layer. [6](#0-5) 

---

### Likelihood Explanation

- SNS governance proposals are public and have predictable execution windows, making front-running straightforward for any on-chain observer.
- The IC supports canister-to-canister calls, so a malicious canister can atomically manipulate a DEX pool and observe the governance execution in the same round or adjacent rounds.
- The `treasury_manager.did` file itself documents this as a "Known Security Risk," confirming the developers are aware the attack surface exists.
- No privileged access, key compromise, or subnet-majority attack is required — only the ability to interact with the DEX canister before the governance execution fires. [2](#0-1) 

---

### Recommendation

**For `deposit()`:** Add a `min_lp_tokens_out: opt nat` field to `DepositRequest` in `treasury_manager.did` and propagate it through `ValidatedDepositOperationArg` and `construct_treasury_manager_deposit_payload()`. The treasury manager implementation must enforce this floor before accepting LP tokens from the DEX.

**For `withdraw()`:** Remove the blanket rejection of arguments in `ValidatedWithdrawOperationArg::try_from()` and add a `min_token_amounts_out` map (ledger canister ID → minimum amount) to `WithdrawRequest`. The `construct_treasury_manager_withdraw_payload()` function must encode these minimums and the treasury manager must enforce them before accepting returned tokens. [7](#0-6) [8](#0-7) 

---

### Proof of Concept

1. An SNS DAO submits a governance proposal: `ExecuteExtensionOperation { operation_name: "deposit", operation_arg: { treasury_allocation_sns_e8s: 1_000_000_000, treasury_allocation_icp_e8s: 500_000_000 } }`.
2. The proposal enters the voting period (publicly visible on-chain).
3. A malicious canister monitors the proposal. When it is about to execute, it calls the DEX canister to heavily buy SNS tokens, skewing the pool ratio against ICP.
4. The governance canister calls `execute_treasury_manager_deposit()`: [9](#0-8) 
5. The treasury manager calls `deposit()` on the DEX at the manipulated price. The SNS treasury receives far fewer LP tokens than the fair-market equivalent.
6. The malicious canister immediately sells its SNS tokens back, restoring the price and pocketing the difference.
7. For `withdraw()`: submit `ExecuteExtensionOperation { operation_name: "withdraw", operation_arg: None }`. The governance canister calls `construct_treasury_manager_withdraw_payload()` which always sends `WithdrawRequest { withdraw_accounts: None }` with no minimum output. A sandwich attack on the DEX before this call causes the treasury to receive fewer tokens than it deposited. [3](#0-2)

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

**File:** rs/sns/governance/src/extensions.rs (L1102-1110)
```rust
fn construct_treasury_manager_withdraw_payload(_value: Precise) -> Result<Vec<u8>, String> {
    let arg = WithdrawRequest {
        withdraw_accounts: None,
    };
    let arg =
        candid::encode_one(&arg).map_err(|err| format!("Error encoding WithdrawRequest: {err}"))?;

    Ok(arg)
}
```

**File:** rs/sns/governance/src/extensions.rs (L1566-1601)
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

**File:** rs/sns/governance/src/extensions.rs (L1733-1746)
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
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```

**File:** rs/sns/treasury_manager/src/lib.rs (L302-306)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct WithdrawRequest {
    /// If not set, accounts specified at the time of deposit will be used for the withdrawal.
    pub withdraw_accounts: Option<BTreeMap<Principal, Account>>,
}
```
