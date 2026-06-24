### Title
SNS Treasury Manager Deposit Lacks On-Chain Slippage Enforcement, Enabling Sandwich Attacks on Treasury Funds - (File: rs/sns/governance/src/extensions.rs)

### Summary
The SNS Treasury Manager deposit flow does not include any slippage protection parameters in either the `DepositRequest` API or the `execute_treasury_manager_deposit` execution path. An unprivileged attacker can front-run a pending SNS governance proposal execution by manipulating the DEX pool state, causing the SNS treasury to deposit at an unfavorable token ratio and extracting value from the treasury.

### Finding Description
The `execute_treasury_manager_deposit` function in `rs/sns/governance/src/extensions.rs` executes treasury deposits into DEX pools through the Treasury Manager extension. The `DepositRequest` struct in `rs/sns/treasury_manager/src/lib.rs` contains only `allowances` (token amounts), with no slippage tolerance, minimum LP tokens out, or minimum amounts out fields.

The deposit flow has a critical gap:

**Step 1 — Proposal validation** (`validate_deposit_operation_impl`): Checks that the requested amounts do not exceed 50% of the current treasury balance. This check is performed at proposal submission/rendering time. [1](#0-0) 

**Step 2 — Proposal execution** (`execute_treasury_manager_deposit`): Calls `approve_treasury_manager` to set ICRC-2 allowances for the full requested amounts, then calls `deposit` on the treasury manager canister. No slippage constraints are passed. [2](#0-1) 

**Step 3 — ICRC-2 approval**: The `approve_treasury_manager` function sets allowances for the exact amounts specified in the proposal, with no minimum-out constraint. [3](#0-2) 

The `DepositRequest` struct carries only `allowances` — no slippage field exists: [4](#0-3) 

The `treasury_manager.did` interface confirms the same — `DepositRequest` has no slippage parameter: [5](#0-4) 

The codebase itself acknowledges this as a known risk in two places. In the DID file: [6](#0-5) 

And in the proposal rendering for `RegisterExtension`: [7](#0-6) 

The warning is informational only — there is no on-chain enforcement of slippage protection at any point in the deposit execution path.

**Timing gap**: The 50% balance check is performed at proposal validation time, but SNS governance proposals have a voting period of days. Between validation and execution, the DEX pool state can be freely manipulated by any user. The execution path does not re-validate the balance or enforce any price constraints.

### Impact Explanation
An attacker can execute a sandwich attack against any SNS treasury deposit into a DEX:

1. The attacker monitors the IC for SNS governance proposals with `ExecuteExtensionOperation` / `deposit` actions.
2. Before the proposal executes, the attacker adds a large amount of one token to the DEX pool, skewing the price ratio.
3. The SNS treasury deposit executes at the manipulated ratio — the treasury receives fewer LP tokens than it should for the deposited value.
4. The attacker removes their liquidity, capturing the price impact.

The SNS treasury permanently loses value proportional to the price impact of the manipulation. Since SNS governance proposals are public and their execution timing is predictable (end of voting period), the attack is straightforward to time. The impact is a direct ledger conservation violation: the SNS treasury's net asset value decreases without any corresponding governance-approved transfer.

### Likelihood Explanation
**Medium**. The attack requires:
- An SNS with a registered Treasury Manager extension (currently the `ALLOWED_EXTENSIONS` list is empty in production after KongSwap ceased operations, but the code path is designed for future extensions). [8](#0-7) 
- A pending governance proposal to deposit treasury funds into a DEX.
- Sufficient capital to meaningfully move the DEX pool price.

When a new extension is registered via NNS governance, the attack surface immediately opens. Governance proposal execution timing is fully predictable on-chain, making front-running straightforward for any well-capitalized actor.

### Recommendation
1. Add a `min_lp_tokens_out` or per-asset `min_amount_out` field to `DepositRequest` in `rs/sns/treasury_manager/src/lib.rs` and `rs/sns/treasury_manager/treasury_manager.did`.
2. Require the SNS governance proposal (`ExecuteExtensionOperation` with `deposit`) to include slippage tolerance parameters, validated at both proposal submission and execution time.
3. In `execute_treasury_manager_deposit`, pass the slippage constraints to the treasury manager's `deposit` call and treat a slippage violation as a hard failure (not a soft warning).
4. Consider adding a time-lock or commit-reveal mechanism to prevent predictable front-running of governance proposal execution.

### Proof of Concept

**Setup**: An SNS has a Treasury Manager extension registered pointing to a DEX pool containing 1,000,000 SNS tokens and 1,000,000 ICP.

**Governance proposal**: An SNS governance proposal is submitted to deposit 100,000 SNS tokens and 100,000 ICP into the DEX pool. The proposal passes the 50% balance check at validation time. [9](#0-8) 

**Attack**:
1. Attacker observes the proposal on-chain and calculates its execution block.
2. Attacker sends a transaction to the DEX adding 900,000 ICP to the pool (skewing the ratio to 1,000,000 SNS : 1,900,000 ICP).
3. The governance proposal executes: `approve_treasury_manager` sets ICRC-2 allowances for 100,000 SNS and 100,000 ICP; `deposit` is called with no slippage constraint. [10](#0-9) 
4. The DEX accepts the deposit at the manipulated ratio. The SNS treasury receives LP tokens representing a position worth significantly less than 200,000 tokens at fair market value.
5. Attacker removes their 900,000 ICP liquidity, now receiving back ICP plus a portion of the SNS tokens deposited by the treasury.
6. The SNS treasury has permanently lost value with no recourse, as the governance proposal executed successfully from the IC protocol's perspective.

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

**File:** rs/sns/governance/src/extensions.rs (L777-831)
```rust
    async fn approve_treasury_manager(
        &self,
        treasury_manager_canister_id: CanisterId,
        sns_amount_e8s: u64,
        icp_amount_e8s: u64,
    ) -> Result<(), GovernanceError> {
        let to = Account {
            owner: treasury_manager_canister_id.get().0,
            subaccount: None,
        };

        let expiry_time_sec = self.env.now().saturating_add(ONE_HOUR_SECONDS);
        let expiry_time_nsec = expiry_time_sec.saturating_mul(NANO_SECONDS_PER_SECOND);

        // If expected_allowance is None, the ledger *blindly* overwrites any existing
        // allowance (even if non-zero). Therefore, there is no risk of double spending.

        self.ledger
            .icrc2_approve(
                to,
                sns_amount_e8s,
                Some(expiry_time_nsec),
                self.transaction_fee_e8s_or_panic(),
                self.sns_treasury_subaccount(),
                None,
            )
            .await
            .map(|_| ())
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error making SNS Token treasury transfer: {e}"),
                )
            })?;

        self.nns_ledger
            .icrc2_approve(
                to,
                icp_amount_e8s,
                Some(expiry_time_nsec),
                icp_ledger::DEFAULT_TRANSFER_FEE.get_e8s(),
                self.icp_treasury_subaccount(),
                None,
            )
            .await
            .map(|_| ())
            .map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error making ICP Token treasury transfer: {e}"),
                )
            })?;

        Ok(())
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1546)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.

```
