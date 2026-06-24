### Title
SNS Treasury Manager `DepositRequest` Lacks Slippage and Deadline Protection, Enabling Sandwich Attacks on SNS Treasury Liquidity Deposits - (`rs/sns/governance/src/extensions.rs`, `rs/sns/treasury_manager/treasury_manager.did`)

---

### Summary

The SNS Treasury Manager extension framework, which allows SNS DAOs to deposit treasury funds (SNS tokens + ICP) into external DEX liquidity pools, does not include any slippage protection or deadline parameters in the `DepositRequest` interface or in the `execute_treasury_manager_deposit` execution path. Because governance proposals are public for days before execution, an unprivileged attacker can front-run the deposit by manipulating the DEX pool ratio, causing the SNS treasury to deposit at an unfavorable price and lose value. The protocol itself acknowledges this as a "Known Security Risk" in the interface specification but provides no on-chain mitigation.

---

### Finding Description

The `TreasuryManager` Candid interface defines `DepositRequest` as:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
``` [1](#0-0) 

There are no fields for `min_lp_tokens_out`, `max_price_deviation`, or `deadline`. The interface specification itself acknowledges this gap:

```
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
``` [2](#0-1) 

The SNS Governance canister's `execute_treasury_manager_deposit` function approves ICRC-2 allowances and then calls `deposit` on the treasury manager canister with no slippage parameters:

```rust
// 1. Transfer funds from treasury to treasury manager
governance.approve_treasury_manager(
    extension_canister_id,
    treasury_allocation_sns_e8s,
    treasury_allocation_icp_e8s,
).await?;

// 2. Call deposit on treasury manager
let balances = governance.env.call_canister(extension_canister_id, "deposit", arg_blob).await ...
``` [3](#0-2) 

The validation step `validate_deposit_operation_impl` only checks that the requested amounts do not exceed 50% of the current treasury balance. It performs no price-ratio check, no minimum LP token output check, and no deadline enforcement: [4](#0-3) 

The proposal rendering function for `RegisterExtension` explicitly warns about this in the rendered proposal text shown to voters, but no enforcement exists at the protocol level:

```
Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
``` [5](#0-4) 

The disclaimer "any undeposited tokens are automatically returned" only covers the case where the DEX rejects the deposit entirely. It does not protect against the case where the deposit succeeds but at a manipulated, unfavorable ratio — which is the core of the sandwich attack.

---

### Impact Explanation

An attacker can cause an SNS treasury to deposit liquidity into a DEX pool at a manipulated price ratio, resulting in:

1. The SNS treasury receiving fewer LP tokens than the fair-value equivalent of the deposited SNS + ICP tokens.
2. The attacker profiting by selling tokens back into the newly added liquidity at the inflated price.

This is a direct loss of SNS treasury funds (both SNS tokens and ICP). The 50% cap on deposits means the maximum loss per proposal is bounded, but repeated proposals or a large treasury can still result in significant losses. The `external_custodian` (DEX) holds the deposited assets, and the SNS treasury receives LP tokens of lesser value. [6](#0-5) 

---

### Likelihood Explanation

The attack is realistic for the following reasons:

1. **Governance proposals are public for days.** Any IC user can observe a pending `ExecuteExtensionOperation` deposit proposal and know the exact amounts (`treasury_allocation_sns_e8s`, `treasury_allocation_icp_e8s`) that will be deposited. [7](#0-6) 

2. **Execution timing is predictable.** SNS governance proposals execute after the voting period ends, giving the attacker a large, known window to manipulate the DEX pool state in advance.

3. **No mempool protection exists.** On the IC, an attacker can submit DEX swap transactions in a block immediately before the governance execution block. Since the proposal execution time is known, the attacker can time their manipulation precisely.

4. **The DEX canister is an external, unprivileged-accessible canister.** Any IC principal can call the DEX canister to swap tokens and manipulate the pool ratio. No special privilege is required.

5. **The protocol's own documentation confirms the attack surface** — the `treasury_manager.did` lists this as a "Known Security Risk" and the proposal renderer warns voters, confirming the developers are aware the attack is feasible.

---

### Recommendation

1. **Add slippage parameters to `DepositRequest`**: Extend the `DepositRequest` type in `treasury_manager.did` to include `min_lp_tokens_out` (or equivalent minimum output guarantee) and `deadline_ns` fields. Treasury manager implementations must enforce these bounds before executing the DEX deposit.

2. **Add slippage validation in `validate_deposit_operation_impl`**: The SNS Governance canister should require that deposit proposals include a `max_price_deviation_bps` or `min_lp_tokens_out` field, and validate it against a reference price (e.g., a TWAP from the DEX or an external oracle) at proposal execution time.

3. **Add a deadline check in `execute_treasury_manager_deposit`**: Reject execution if the current time exceeds a deadline specified in the proposal, preventing stale proposals from executing at a time chosen by an attacker. [8](#0-7) 

---

### Proof of Concept

**Setup**: An SNS DAO has 200 SNS tokens and 200 ICP in its treasury. A governance proposal is submitted to deposit 100 SNS + 100 ICP into a KongSwap-style DEX pool via `ExecuteExtensionOperation { operation_name: "deposit", ... }`.

**Attack sequence**:

1. Attacker observes the pending proposal (public on-chain) with `treasury_allocation_sns_e8s = 100_0000_0000` and `treasury_allocation_icp_e8s = 100_0000_0000`.

2. Before the proposal executes, the attacker calls the DEX canister to swap a large amount of ICP for SNS tokens, pushing the pool ratio away from 1:1 (e.g., to 1 ICP = 2 SNS).

3. The governance proposal executes. `execute_treasury_manager_deposit` calls `approve_treasury_manager` granting the allowance, then calls `deposit` on the treasury manager with no slippage check. [9](#0-8) 

4. The treasury manager deposits 100 SNS + 100 ICP into the pool at the manipulated 1:2 ratio. The SNS treasury receives LP tokens representing a position worth less than 200 ICP equivalent.

5. The attacker sells the SNS tokens they acquired in step 2 back into the pool, profiting from the price impact of the treasury's deposit.

6. The `validate_deposit_operation_impl` 50% balance check passed at proposal creation time and does not re-check the pool price at execution time. [10](#0-9)

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

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
