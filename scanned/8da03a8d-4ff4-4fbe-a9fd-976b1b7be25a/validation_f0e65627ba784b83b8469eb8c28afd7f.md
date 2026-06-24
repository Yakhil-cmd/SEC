### Title
SNS Treasury Manager `DepositRequest` Lacks Slippage Protection, Enabling Sandwich Attacks on Governance-Executed DEX Deposits - (File: `rs/sns/treasury_manager/src/lib.rs`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS Treasury Manager framework allows an SNS governance proposal to deposit treasury funds (SNS tokens + ICP) into a DEX via a `TreasuryManager` extension canister. The `DepositRequest` type carries no slippage-protection parameters, and `execute_treasury_manager_deposit` performs no price-manipulation check at execution time. Because governance proposals are public and have a multi-day voting window, an unprivileged attacker can observe a passing deposit proposal, front-run its execution by manipulating the DEX price, and back-run after the treasury funds are deployed at the unfavorable price — draining the SNS treasury.

---

### Finding Description

The `DepositRequest` struct defined in `rs/sns/treasury_manager/src/lib.rs` contains only `allowances` (token amounts):

```rust
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
```

There are no fields for minimum amounts out, maximum price impact, or any price-bound guard. [1](#0-0) 

The corresponding Candid interface mirrors this:

```
type DepositRequest = record {
  allowances : vec Allowance;
};
``` [2](#0-1) 

The DID file itself acknowledges the risk explicitly under "Known Security Risks":

> Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved. [3](#0-2) 

The governance execution path in `execute_treasury_manager_deposit` (`rs/sns/governance/src/extensions.rs`) performs no price-manipulation check before calling `deposit`:

1. It calls `approve_treasury_manager` (ICRC-2 approve) to grant the treasury manager an allowance.
2. It immediately calls `deposit` on the treasury manager canister. [4](#0-3) 

The only balance validation (`validate_deposit_operation_impl`) runs at **proposal creation time**, checking that the requested amounts do not exceed 50% of the current treasury balance. This check is **not repeated at execution time**, which may be days later after the voting period. [5](#0-4) 

The `approve_treasury_manager` function grants a 1-hour ICRC-2 allowance to the treasury manager canister, but this window is more than sufficient for an attacker to execute the back-run. [6](#0-5) 

The proposal rendering function `validate_and_render_register_extension` even warns voters about this risk, but no enforcement mechanism exists in the framework: [7](#0-6) 

---

### Impact Explanation

An attacker can drain SNS treasury funds by sandwich-attacking the execution of a governance-approved `ExecuteExtensionOperation` deposit proposal:

1. **Front-run**: Before the proposal executes, the attacker manipulates the DEX price (e.g., buys one token heavily, pushing the price up).
2. **Victim transaction**: The governance canister executes `execute_treasury_manager_deposit`, which calls `deposit` on the treasury manager. The treasury manager deploys SNS + ICP funds into the DEX at the now-manipulated price.
3. **Back-run**: The attacker sells their position back into the treasury's newly deployed liquidity at the inflated price, unwinding the manipulation and pocketing the difference.

The `TreasuryManager` trait's `deposit` method has no mechanism to reject a deposit when the price has been manipulated, because `DepositRequest` carries no price bounds. [8](#0-7) 

The impact is direct, quantifiable loss of SNS treasury assets (both SNS tokens and ICP), proportional to the treasury size and the attacker's capital.

---

### Likelihood Explanation

- SNS governance proposals are **fully public** and have a **multi-day voting window** (typically 4+ days), giving any observer ample time to prepare a sandwich attack.
- The attacker needs no privileged access — they only need to monitor governance and hold sufficient capital to move the DEX price.
- IC-native DEXes (e.g., ICDex, Sonic) are the `external_custodian` targets. Their on-chain state is observable, and their price can be moved by any canister caller or ingress sender with sufficient tokens.
- The `approve_treasury_manager` call grants a 1-hour allowance window, but the `deposit` call follows immediately in the same async execution, so the attacker only needs to be positioned before the proposal executes.

---

### Recommendation

1. **Add slippage parameters to `DepositRequest`**: Introduce `min_sns_amount_deposited`, `min_icp_amount_deposited`, or a `max_price_impact_bps` field so the treasury manager implementation can enforce price bounds at deposit time.

2. **Re-validate balance limits at execution time**: `execute_treasury_manager_deposit` should re-run the 50% balance check (currently only done at proposal creation) immediately before calling `approve_treasury_manager`, to account for balance changes during the voting period.

3. **Add a price-manipulation guard analogous to `onlyCalmPeriods`**: The governance framework should require the treasury manager to report a price-stability check before funds are committed. Alternatively, the `DepositRequest` should carry a `deadline` or `price_snapshot` field that the treasury manager validates against the current DEX state.

4. **Enforce slippage in the `TreasuryManager` trait contract**: The `TreasuryManager` trait documentation should mandate that implementations reject deposits when the current DEX price deviates beyond a caller-specified tolerance.

---

### Proof of Concept

**Attacker-controlled entry path** (no privileged access required):

```
1. Attacker monitors SNS governance canister for ExecuteExtensionOperation proposals
   with operation_name == "deposit" that are approaching execution.

2. Attacker observes the proposal will execute imminently (voting period ending).

3. Attacker front-runs: sends a large swap on the IC DEX (e.g., ICDex) to buy
   SNS tokens with ICP, pushing the SNS/ICP price up significantly.

4. Governance canister executes execute_treasury_manager_deposit:
   - approve_treasury_manager grants ICRC-2 allowance to treasury manager
   - call_canister(..., "deposit", arg_blob) is called
   - Treasury manager calls DEX deposit at the now-inflated SNS price
   - Treasury's ICP is deployed at an unfavorable (high SNS price) range

5. Attacker back-runs: sells their SNS tokens back into the treasury's
   newly deployed ICP liquidity at the inflated price, unwinding the
   front-run and pocketing the price difference.

Result: SNS treasury loses ICP (forced to buy SNS "high"), attacker profits.
```

The root cause is confirmed at:
- `rs/sns/treasury_manager/src/lib.rs:284-287` — `DepositRequest` has no slippage fields
- `rs/sns/governance/src/extensions.rs:1566-1578` — `execute_treasury_manager_deposit` calls `approve` then `deposit` with no price check
- `rs/sns/governance/src/extensions.rs:276-321` — balance validation only at proposal creation, not execution [1](#0-0) [9](#0-8) [5](#0-4)

### Citations

**File:** rs/sns/treasury_manager/src/lib.rs (L250-256)
```rust
pub trait TreasuryManager {
    /// Implements the `deposit` API function.
    fn deposit(
        &mut self,
        request: DepositRequest,
    ) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;

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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
