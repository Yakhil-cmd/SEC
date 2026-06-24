### Title
SNS Treasury Manager `DepositRequest` Lacks Slippage Protection, Enabling Front-Running of SNS Treasury Deposits into DEX Liquidity Pools - (File: rs/sns/treasury_manager/treasury_manager.did)

---

### Summary

The SNS Treasury Manager extension interface (`DepositRequest`) and the governance execution path (`execute_treasury_manager_deposit`) contain no minimum-output (slippage) parameter. When an SNS DAO executes a governance proposal to deposit treasury funds into a DEX liquidity pool via a Treasury Manager extension, an attacker can front-run the deposit by manipulating the DEX price, causing the SNS treasury to receive far fewer LP tokens than expected. The IC codebase itself acknowledges this risk but provides no mitigation at the protocol level.

---

### Finding Description

The `DepositRequest` type, defined in the IC-canonical Treasury Manager interface, contains only `allowances` (the maximum amounts to deposit) and no `min_lp_out` or `min_amount_out` field:

```candid
// rs/sns/treasury_manager/treasury_manager.did
type DepositRequest = record {
  allowances : vec Allowance;
};
``` [1](#0-0) 

The Rust struct mirrors this:

```rust
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
``` [2](#0-1) 

The governance execution function `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs` approves the treasury manager and calls `deposit` with this slippage-free request: [3](#0-2) 

The validation step (`validate_deposit_operation_impl`) only checks that the requested amount does not exceed 50% of the current treasury balance. It performs no minimum-output validation: [4](#0-3) 

The `ValidatedDepositOperationArg` struct carries only input amounts, not any expected minimum output: [5](#0-4) 

The IC codebase itself acknowledges this risk explicitly in two places:

1. In the Treasury Manager DID file: [6](#0-5) 

2. In the governance proposal rendering (as a warning, not a guard): [7](#0-6) 

The warning is informational only — no enforcement exists at the protocol level.

---

### Impact Explanation

When an SNS DAO passes a `RegisterExtension` or `ExecuteExtensionOperation` (deposit) proposal, the execution is public and its timing is predictable. An attacker can:

1. Observe the proposal passing and its scheduled execution.
2. Submit a transaction to manipulate the DEX pool price (e.g., large swap to skew the ratio) before the governance canister calls `deposit`.
3. The Treasury Manager calls the DEX with no minimum LP output constraint, accepting any ratio.
4. The attacker reverses their position, extracting value at the expense of the SNS treasury.

The result is that SNS token holders (stakers/voters) suffer a direct financial loss from their treasury. The `allowances` field only caps the *input* amount; there is no floor on the *output* LP tokens received.

**Vulnerability class:** Ledger conservation bug / cycles-resource accounting bug (SNS treasury asset loss via sandwich attack on DEX deposit).

---

### Likelihood Explanation

- Governance proposals are fully public on-chain; their execution timing is deterministic once a proposal passes.
- Any unprivileged actor can submit DEX swap transactions to manipulate pool prices.
- No mempool exists on IC, but the attacker can submit price-manipulation transactions in the same or immediately preceding consensus round before the governance heartbeat executes the proposal.
- The attack requires no privileged access, no key compromise, and no social engineering — only the ability to submit canister calls to the DEX.
- Likelihood is **medium**: requires capital to move the DEX price, but the attack is straightforward and profitable for well-capitalized actors targeting large treasury deposits.

---

### Recommendation

Add a `min_lp_out` (or `min_amount_out`) field to `DepositRequest` in `rs/sns/treasury_manager/treasury_manager.did` and `rs/sns/treasury_manager/src/lib.rs`. The SNS governance proposal for a deposit operation should require the proposer to specify this value. The `ValidatedDepositOperationArg` and `validate_deposit_operation_impl` should validate that `min_lp_out > 0`. The `execute_treasury_manager_deposit` function should pass this value through to the Treasury Manager, which must enforce it when calling the DEX.

---

### Proof of Concept

1. SNS DAO passes a deposit proposal: deposit 1000 ICP + 500 SNS tokens into a DEX pool via the Treasury Manager.
2. Proposal passes; execution is scheduled at the next governance heartbeat.
3. Attacker observes the proposal passing and submits a large swap on the DEX (e.g., dumps SNS tokens into the pool), skewing the price ratio unfavorably.
4. Governance heartbeat fires; `execute_treasury_manager_deposit` calls `approve_treasury_manager` then `deposit` on the Treasury Manager canister with `DepositRequest { allowances: [...] }` — no `min_lp_out`.
5. The Treasury Manager calls the DEX `add_liquidity` with no minimum LP output; the DEX accepts the deposit at the manipulated ratio, issuing far fewer LP tokens.
6. Attacker reverses their swap, profiting from the price impact. SNS treasury receives LP tokens worth significantly less than the deposited assets.

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

**File:** rs/sns/treasury_manager/src/lib.rs (L284-287)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
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

**File:** rs/sns/governance/src/extensions.rs (L1663-1672)
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
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
