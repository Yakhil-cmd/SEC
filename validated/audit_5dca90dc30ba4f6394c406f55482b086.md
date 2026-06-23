### Title
No Slippage Protection in SNS TreasuryManager `DepositRequest` API Allows SNS Treasury Fund Loss - (`rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS TreasuryManager framework's `DepositRequest` type contains no minimum-received-amount or slippage-tolerance field. When SNS governance executes a `TreasuryManagerDeposit` proposal, it approves and forwards treasury funds to a DEX-backed TreasuryManager canister with no on-chain enforcement of a minimum number of LP tokens or assets received in return. The codebase itself explicitly labels this a "Known Security Risk." The result is that SNS treasury funds — collectively owned by all token holders — can be deposited into a DEX liquidity pool at a severely unfavorable ratio, with no protocol-level protection.

---

### Finding Description

**Root cause — `DepositRequest` has no slippage field:**

The `DepositRequest` type in the TreasuryManager API only carries `allowances` (the amounts to deposit). There is no `min_received`, `min_lp_tokens`, or any slippage-tolerance field:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
``` [1](#0-0) 

The codebase explicitly acknowledges this gap as a "Known Security Risk":

```
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
``` [2](#0-1) 

**Execution path — `execute_treasury_manager_deposit` does not verify received amounts:**

When an SNS governance proposal of type `TreasuryManagerDeposit` is adopted and executed, `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs`:

1. Approves the TreasuryManager to spend the specified SNS and ICP amounts.
2. Calls `deposit` on the TreasuryManager canister.
3. Logs the returned `Balances` response but **does not verify** that the received LP tokens or other assets meet any minimum threshold. [3](#0-2) 

The proposal-rendering code in `rs/sns/governance/src/proposal.rs` warns about this in the human-readable proposal text, but the warning is informational only — no enforcement exists at the protocol level:

```
## WARNING
Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
``` [4](#0-3) 

The note that "undeposited tokens are automatically returned" only covers the case where tokens are entirely rejected by the DEX — it does not protect against the case where tokens are accepted at a severely unfavorable price ratio.

**Validation gap — `validate_deposit_operation_impl` only checks the 50% cap:**

The validation step only enforces that the requested amounts do not exceed 50% of the current treasury balance. It does not validate any minimum-output constraint: [5](#0-4) 

---

### Impact Explanation

An SNS DAO that passes a `TreasuryManagerDeposit` proposal to provide liquidity to a DEX pool can have its treasury funds deposited at a severely unfavorable token ratio. Because IC governance proposals have multi-day voting periods, there is a substantial window between proposal adoption and execution during which:

- Natural market price movements can shift the pool ratio significantly.
- A sophisticated attacker who observes the adopted proposal can manipulate the DEX pool price (e.g., by making large trades) immediately before the deposit executes, then reverse the manipulation after, profiting at the expense of the SNS treasury.

The SNS treasury is collectively owned by all token holders. A significant loss of treasury funds directly harms all holders and the DAO's operational capacity. The `TreasuryManager` framework is designed to manage real SNS and ICP treasury assets deposited into external DEX canisters. [6](#0-5) 

---

### Likelihood Explanation

**Medium.** The attack requires:

1. An SNS DAO to pass a `TreasuryManagerDeposit` proposal (a normal governance action).
2. A DEX pool that lacks its own internal slippage protection.
3. An attacker (or natural market movement) to shift the pool price between proposal adoption and execution.

The multi-day governance voting period makes condition 3 highly likely even without a malicious actor. Any SNS that uses the TreasuryManager framework to provide liquidity is exposed to this risk on every deposit proposal.

---

### Recommendation

1. **Add a `min_received` field to `DepositRequest`** (e.g., `min_lp_tokens_received : opt nat`) so that the TreasuryManager implementation can enforce a minimum output at the DEX level.
2. **Add a post-deposit balance check in `execute_treasury_manager_deposit`**: after calling `deposit`, compare the returned `external_custodian` balance against a minimum threshold specified in the proposal arguments.
3. **Add a `max_slippage_bps` or `min_received` field to `ValidatedDepositOperationArg`** so that SNS governance can enforce slippage bounds at the framework level, independent of the TreasuryManager implementation.

---

### Proof of Concept

1. An SNS DAO adopts a `TreasuryManagerDeposit` proposal to deposit 1,000,000 SNS tokens and 500 ICP into a DEX liquidity pool at the current 2:1 ratio.
2. The proposal enters a multi-day voting period. An attacker observes the adopted proposal.
3. Just before execution, the attacker makes a large trade on the DEX pool, shifting the ratio to 10:1 (SNS:ICP).
4. `execute_treasury_manager_deposit` calls `deposit` with `allowances` for 1,000,000 SNS and 500 ICP. The DEX accepts the deposit at the manipulated 10:1 ratio, consuming all 1,000,000 SNS but only ~100 ICP worth of value in LP tokens.
5. The attacker reverses their trade, profiting from the price impact. The SNS treasury has lost ~400 ICP worth of value with no protocol-level recourse.
6. The `execute_treasury_manager_deposit` function logs the returned `Balances` and returns `Ok(())` — no minimum-output check is performed. [7](#0-6) [1](#0-0)

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

**File:** rs/sns/governance/src/extensions.rs (L1566-1609)
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

    log!(
        INFO,
        "TreasuryManager.deposit succeeded with response: {:?}",
        balances
    );

    Ok(())
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
