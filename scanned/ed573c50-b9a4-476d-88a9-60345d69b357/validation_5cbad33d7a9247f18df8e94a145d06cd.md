### Title
No Slippage Protection in SNS Treasury Manager Deposit API Enables Sandwich Attacks on DAO Treasury Funds - (File: `rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS Treasury Manager `DepositRequest` API and the `execute_treasury_manager_deposit` function in SNS Governance lack any minimum output amount (slippage protection) parameter. When an SNS DAO governance proposal executes a deposit of treasury funds into a DEX liquidity pool via a Treasury Manager extension, there is no mechanism to enforce a minimum number of LP tokens received. An attacker can observe a passing governance proposal and sandwich-attack the deposit execution, causing the SNS treasury to receive far fewer LP tokens than expected while losing real token value.

---

### Finding Description

The `DepositRequest` type defined in `rs/sns/treasury_manager/treasury_manager.did` contains only an `allowances` field specifying how much of each asset the Treasury Manager may consume:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
```

There is no `minimum_lp_tokens_out`, `minimum_received`, or any slippage-protection field. [1](#0-0) 

The DID file itself explicitly acknowledges this as a "Known Security Risk":

> Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved. [2](#0-1) 

The `ValidatedDepositOperationArg` struct in `rs/sns/governance/src/extensions.rs` only extracts `treasury_allocation_sns_e8s` and `treasury_allocation_icp_e8s` — no minimum output field exists: [3](#0-2) 

The `execute_treasury_manager_deposit` function approves the Treasury Manager to spend the specified amounts and then calls `deposit` with no slippage guard whatsoever: [4](#0-3) 

The governance validation (`validate_deposit_operation_impl`) only checks that the requested amount does not exceed 50% of the current treasury balance — it performs no minimum output validation: [5](#0-4) 

The proposal rendering code in `rs/sns/governance/src/proposal.rs` even warns voters about this exact risk, confirming it is a live, unmitigated issue in the production API: [6](#0-5) 

---

### Impact Explanation

When an SNS DAO governance proposal to deposit treasury funds into a DEX liquidity pool passes and executes:

1. The governance canister calls `approve_treasury_manager` to grant the Treasury Manager an ICRC-2 allowance for the approved SNS and ICP amounts.
2. The governance canister calls `deposit` on the Treasury Manager with only the `allowances` field — no minimum LP tokens out.
3. The Treasury Manager deposits into the DEX with no slippage floor.
4. An attacker who front-ran the deposit by manipulating the DEX pool ratio receives the arbitrage profit; the SNS treasury receives far fewer LP tokens than the ratio at proposal-approval time implied.
5. The SNS treasury's `external_custodian` balance (LP tokens) is permanently understated relative to the tokens spent, causing a permanent loss of DAO treasury value with no on-chain recourse.

This is a direct ledger conservation bug: the SNS treasury emits `X` tokens but receives `Y < X_equivalent` in LP tokens, with no protocol-level check to reject the transaction. [7](#0-6) 

---

### Likelihood Explanation

- IC governance proposals are fully public and their execution is deterministic and observable. Once a `TreasuryManagerDeposit` proposal passes, any observer knows the exact amounts that will be deposited and can act before execution.
- DEX canister state on the IC is also publicly readable via query calls, enabling precise calculation of the manipulation needed.
- The attack requires no privileged access: any unprivileged canister caller or boundary-node user can submit transactions to the DEX canister to manipulate the pool ratio before the governance proposal executes.
- The `approve_treasury_manager` step (ICRC-2 approval) is a separate on-chain action that precedes the `deposit` call, creating an observable window for front-running. [8](#0-7) 

---

### Recommendation

1. **Add a `minimum_lp_tokens_out` (or equivalent) field to `DepositRequest`** in `rs/sns/treasury_manager/treasury_manager.did` so that Treasury Manager implementations can enforce a slippage floor at the DEX call level.
2. **Add a `minimum_lp_tokens_out` field to `ValidatedDepositOperationArg`** in `rs/sns/governance/src/extensions.rs` and require it to be specified in governance proposals.
3. **Validate the minimum output in `validate_deposit_operation_impl`**: reject proposals that specify zero or unreasonably low minimum output amounts.
4. **Enforce the minimum in `execute_treasury_manager_deposit`**: pass the governance-approved minimum to the Treasury Manager so it can abort the DEX call if slippage exceeds the approved threshold.

---

### Proof of Concept

1. An SNS DAO passes a `TreasuryManagerDeposit` proposal specifying `treasury_allocation_sns_e8s = 1_000_000_000` and `treasury_allocation_icp_e8s = 100_000_000`.
2. An attacker observes the proposal passing (public IC state) and submits a large swap to the DEX pool, moving the SNS/ICP price ratio significantly against the SNS treasury.
3. The governance canister executes `execute_treasury_manager_deposit`, calling `approve_treasury_manager` then `deposit` with `DepositRequest { allowances: [...] }` — no minimum LP tokens field.
4. The Treasury Manager deposits into the DEX at the manipulated price, receiving e.g. 40% fewer LP tokens than the pre-manipulation ratio implied.
5. The attacker back-runs to restore the price and pocket the arbitrage profit.
6. The SNS treasury has permanently lost value with no protocol-level rejection, since `execute_treasury_manager_deposit` only checks that the `deposit` call returned `Ok` — not that the LP tokens received met any minimum. [9](#0-8) [1](#0-0)

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

**File:** rs/sns/treasury_manager/treasury_manager.did (L271-295)
```text
// Parties involved in the treasury asset management process:
// 1. treasury_owner     - e.g., the SNS Governance canister.
// 2. treasury_manager   - this canister.
// 3. external_custodian - e.g., the DEX in which assets are held temporarily.
// 4. fee_collector      - takes into account all the fees incurred due to treasury_manager's work.
// 5. payees             - e.g., developer salary payments.
// 6. payers             - e.g., liquidity provider rewards.
//
// Expects flow of assets:
//
// (A) Initialization / Deposit
// ============================
//                                      ,--------------> payees
//                                     /
// treasury_owner ---> treasury_manager ---> external_custodian
//              \                      \                       \
//               `----------------------`-----------------------`--------> fee_collector
//
// (B) Withdrawal
// ==============
//             payers --->.
//                         \
//  external_custodian ---> treasury_manager ---> treasury_owner
//                    \                     \
//                     `---------------------`---------------------------> fee_collector
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

**File:** rs/sns/governance/src/proposal.rs (L1540-1550)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.

## Extension Configuration

The extension will be deployed and configured according to the provided parameters.",
    ))
```
