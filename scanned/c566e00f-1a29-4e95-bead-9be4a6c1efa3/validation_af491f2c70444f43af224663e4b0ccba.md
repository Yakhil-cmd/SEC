### Title
Missing Slippage Protection in SNS Treasury Manager DEX Deposit — (`File: rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS Treasury Manager framework, which allows SNS DAOs to deposit treasury assets (SNS tokens and ICP) into external DEX liquidity pools, does not enforce any slippage check or minimum-output validation at the governance execution layer. The `execute_treasury_manager_deposit` function in `rs/sns/governance/src/extensions.rs` approves and forwards treasury funds to the Treasury Manager canister's `deposit` endpoint without any price-ratio or minimum-LP-token-out guard. The `treasury_manager.did` interface itself explicitly acknowledges this as a known security risk. An attacker who can observe a pending governance proposal can manipulate the target DEX pool between proposal adoption and execution, causing the SNS treasury to receive far fewer LP tokens than expected at the time the proposal was voted on.

---

### Finding Description

The `execute_treasury_manager_deposit` function in `rs/sns/governance/src/extensions.rs` performs two steps:

1. **Approve** the Treasury Manager canister to spend `treasury_allocation_sns_e8s` SNS tokens and `treasury_allocation_icp_e8s` ICP from the SNS governance treasury via ICRC-2 allowances.
2. **Call** `deposit` on the Treasury Manager canister with the approved allowances. [1](#0-0) 

Neither step includes any slippage parameter, minimum-output assertion, or oracle-price comparison. The `DepositRequest` type passed to the Treasury Manager contains only `allowances` (the amounts to deposit), with no `min_lp_out`, `max_price_impact`, or equivalent field. [2](#0-1) 

The `treasury_manager.did` interface itself explicitly documents this gap under "Known Security Risks":

> *Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved.* [3](#0-2) 

The `approve_treasury_manager` function sets a 1-hour expiry on the ICRC-2 allowance, meaning the Treasury Manager has up to one hour to call `icrc2_transfer_from` and execute the DEX deposit — a window during which pool manipulation can occur. [4](#0-3) 

The `ValidatedDepositOperationArg` carries only `treasury_allocation_sns_e8s`, `treasury_allocation_icp_e8s`, and the raw `original` Precise value — no slippage bounds are validated or forwarded. [5](#0-4) 

---

### Impact Explanation

An attacker who observes a pending `ExecuteExtensionOperation` deposit proposal (all proposals are public on-chain) can:

1. **Front-run**: Before the proposal executes, use a large swap on the target DEX pool to skew the SNS/ICP price ratio significantly.
2. **Trigger execution**: The governance canister calls `execute_treasury_manager_deposit`, which approves and deposits at the manipulated price. The SNS treasury receives far fewer LP tokens than the community expected when voting.
3. **Back-run**: The attacker swaps back through the pool, profiting from the price impact absorbed by the SNS treasury deposit.

The result is a direct, quantifiable loss of SNS DAO treasury value — the treasury contributes assets at an unfavorable ratio and receives LP tokens worth less than the deposited assets. This is a governance-level ledger conservation bug: the SNS treasury's net asset value decreases by an attacker-controlled amount with no protocol-level bound.

---

### Likelihood Explanation

- SNS governance proposals are fully public and their execution is deterministic and observable.
- The IC does not have a mempool in the traditional sense, but the proposal execution timing is predictable (proposals execute after adoption, and the 1-hour ICRC-2 allowance window is known).
- Any canister or user on the IC can interact with the same DEX liquidity pool used by the Treasury Manager.
- No privileged access is required — the attacker only needs to hold enough tokens to move the DEX pool price, which is achievable via flash-loan-equivalent patterns on IC DEXes (e.g., atomic multi-call sequences within a single canister execution).
- The `ExecuteExtensionOperation` action is classified as `Critical` topic `TreasuryAssetManagement`, meaning it requires a supermajority to pass, but once passed, execution is automatic and unguarded. [6](#0-5) 

---

### Recommendation

1. **Add a `min_lp_tokens_out` or `min_price_ratio` field to `DepositRequest`** in `treasury_manager.did` and propagate it through `execute_treasury_manager_deposit`. The governance proposal should encode the minimum acceptable LP token output, and the Treasury Manager must revert if the DEX returns less.

2. **Enforce slippage bounds in `execute_treasury_manager_deposit`**: After the `deposit` call returns `Balances`, compare the actual LP tokens received against the proposal's expected minimum. If the output is below the threshold, revert and reclaim the allowance.

3. **Reduce the ICRC-2 allowance expiry window** from 1 hour to the minimum needed, limiting the manipulation window.

4. **Consider making deposit execution time-bounded**: Reject execution if the block timestamp is more than N seconds after proposal adoption, preventing stale proposals from being exploited.

---

### Proof of Concept

**Setup**: An SNS DAO has a Treasury Manager registered against a DEX pool for SNS/ICP. A governance proposal is submitted and adopted to deposit 100,000 SNS tokens and 10,000 ICP into the pool.

**Attack sequence**:

1. Attacker observes the adopted proposal on-chain.
2. Attacker calls the DEX to swap a large amount of ICP for SNS tokens, driving the SNS price up (ICP price in the pool drops).
3. The SNS governance canister executes `execute_treasury_manager_deposit`:
   - `approve_treasury_manager` grants the Treasury Manager an ICRC-2 allowance for 100,000 SNS and 10,000 ICP.
   - `call_canister(..., "deposit", arg_blob)` is called with no slippage guard. [7](#0-6) 
4. The Treasury Manager calls the DEX `deposit` at the manipulated price. The pool now requires far more ICP per SNS (or vice versa), so the treasury receives significantly fewer LP tokens than expected.
5. Attacker swaps back (SNS → ICP), restoring the pool price and pocketing the profit extracted from the treasury's unfavorable deposit.
6. The `execute_treasury_manager_deposit` function logs success and returns `Ok(())` with no validation of the LP token output. [8](#0-7) 

The SNS treasury has permanently lost value proportional to the attacker's price manipulation, with no protocol-level recourse.

### Citations

**File:** rs/sns/governance/src/extensions.rs (L788-803)
```rust
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
```

**File:** rs/sns/governance/src/extensions.rs (L1551-1555)
```rust
    let ValidatedDepositOperationArg {
        treasury_allocation_sns_e8s,
        treasury_allocation_icp_e8s,
        original,
    } = arg;
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

**File:** rs/sns/governance/src/extensions.rs (L1603-1609)
```rust
    log!(
        INFO,
        "TreasuryManager.deposit succeeded with response: {:?}",
        balances
    );

    Ok(())
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

**File:** rs/sns/governance/src/governance/proposal_topics_tests.rs (L223-233)
```rust
    test_cases.push((
        pb::proposal::Action::ExecuteExtensionOperation(ExecuteExtensionOperation {
            extension_canister_id: Some(extension_canister_id.get()),
            operation_name: Some("deposit".to_string()),
            operation_arg: None,
        }),
        Ok((
            Some(pb::Topic::TreasuryAssetManagement),
            ProposalCriticality::Critical,
        )),
    ));
```
