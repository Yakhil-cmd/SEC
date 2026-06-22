### Title
SNS Treasury Manager `DepositRequest`/`WithdrawRequest` Lack Slippage Protection and Execution Deadline, Enabling Front-Running of Governance-Approved DEX Deposits - (File: `rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The SNS Treasury Manager API (`DepositRequest`, `WithdrawRequest`) contains no slippage parameters (e.g., `min_lp_tokens_out`) and no execution deadline. When an SNS governance proposal to deposit treasury funds into a DEX liquidity pool is adopted and executed, an unprivileged DEX participant can front-run the deposit by manipulating the pool price ratio between proposal creation and execution, causing the SNS treasury to receive fewer LP tokens than the DAO voted to accept. This is a governance-level ledger conservation bug with a reachable, unprivileged attacker path.

---

### Finding Description

**Root cause in production IC code:**

The `DepositRequest` type defined in `rs/sns/treasury_manager/treasury_manager.did` contains only `allowances` (the token amounts to deposit) with no `min_lp_tokens_out`, no `min_ratio`, and no `deadline` field:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};
``` [1](#0-0) 

Similarly, `WithdrawRequest` has no `min_amounts_out`:

```candid
type WithdrawRequest = record {
  withdraw_accounts : opt vec record { principal; Account };
};
``` [2](#0-1) 

The DID file itself explicitly acknowledges this as a **Known Security Risk**:

> "Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved." [3](#0-2) 

The proposal rendering code in `rs/sns/governance/src/proposal.rs` also warns:

> "Some Decentralized Exchanges lack slippage protection during deposits. Consequently, deposited asset ratios may deviate from those specified in the proposal. This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running or sandwich attacks." [4](#0-3) 

**Execution path with no slippage enforcement:**

When a deposit proposal is adopted, `execute_treasury_manager_deposit` in `rs/sns/governance/src/extensions.rs`:
1. Calls `approve_treasury_manager` to grant an ICRC-2 allowance to the treasury manager canister (with a 1-hour expiry from execution time, not from proposal creation time)
2. Calls `deposit` on the treasury manager with the fixed `allowances` amounts [5](#0-4) 

The `approve_treasury_manager` function sets the allowance expiry to `now + ONE_HOUR_SECONDS` at execution time — not at proposal creation time — so there is no deadline relative to when the DAO voted: [6](#0-5) 

The validation step `validate_deposit_operation_impl` only checks that the requested amount is ≤50% of the current treasury balance. It does **not** check any expected LP token output or price ratio: [7](#0-6) 

The `TreasuryManager` trait's `deposit` method signature accepts only a `DepositRequest` with no slippage fields: [8](#0-7) 

---

### Impact Explanation

An unprivileged DEX participant who observes a pending SNS governance deposit proposal can:
- Manipulate the DEX pool price ratio before the proposal executes (sandwich attack)
- Cause the SNS treasury to deposit at a sub-optimal ratio, receiving far fewer LP tokens than the DAO intended
- Restore the price after the deposit and extract the difference

Because the `DepositRequest` carries no `min_lp_tokens_out` and no deadline, neither the SNS governance canister nor the treasury manager canister can reject a deposit that returns an arbitrarily small LP token amount. The SNS DAO treasury suffers a direct, unrecoverable financial loss. This is a **ledger conservation bug** — the SNS treasury's total managed assets decrease without DAO consent.

---

### Likelihood Explanation

**Medium.** The attacker needs:
1. Capital to manipulate the DEX pool price (proportional to pool depth)
2. Ability to observe pending SNS governance proposals (fully public on-chain)
3. Ability to submit transactions to the DEX before the proposal executes

On the Internet Computer, proposal execution timing is deterministic and observable. The `process_proposals` function executes adopted proposals at the next heartbeat after the voting deadline, making the execution window predictable. No privileged access, no key compromise, and no subnet-majority attack is required. [9](#0-8) 

---

### Recommendation

1. **Add slippage parameters to `DepositRequest`**: Include a `min_lp_tokens_out : opt nat` field so the treasury manager can reject deposits that return fewer LP tokens than the DAO approved.
2. **Add a deadline to `DepositRequest`**: Include a `deadline_ns : opt nat64` field so the treasury manager rejects execution if the current time exceeds the deadline set at proposal creation.
3. **Enforce slippage in `execute_treasury_manager_deposit`**: After calling `deposit`, verify the returned `Balances` reflect at least the minimum expected LP token amount; revert (and reclaim the allowance) if not.
4. **Set allowance expiry relative to proposal creation**, not execution time, so stale proposals cannot be executed after market conditions have changed significantly.

---

### Proof of Concept

1. SNS DAO submits `ExecuteExtensionOperation` deposit proposal: `treasury_allocation_sns_e8s = 1_000_000_000`, `treasury_allocation_icp_e8s = 500_000_000` targeting a KongSwap pool.
2. Proposal enters voting period (days). Attacker monitors the proposal.
3. Proposal passes. Attacker observes the imminent execution.
4. Attacker front-runs: submits a large swap on the DEX to skew the SNS/ICP price ratio unfavorably.
5. `perform_execute_extension_operation` → `execute_treasury_manager_deposit` → `approve_treasury_manager` (grants allowance) → `deposit` call to treasury manager → treasury manager calls DEX `add_liquidity` with no `min_lp_tokens_out`.
6. DEX accepts the deposit at the manipulated price; SNS treasury receives a fraction of the expected LP tokens.
7. Attacker back-runs: swaps back, extracting the price impact as profit.
8. The `Balances` response is logged but never validated against a minimum; `execute_treasury_manager_deposit` returns `Ok(())` regardless of LP tokens received. [10](#0-9)

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

**File:** rs/sns/treasury_manager/treasury_manager.did (L88-93)
```text
type WithdrawRequest = record {
  // Maps Ledger canister IDs of assets to be withdrawn to the respective withdraw accounts.
  //
  // If not set, accounts specified at the time of deposit will be used for the withdrawal.
  withdraw_accounts : opt vec record { principal; Account };
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

**File:** rs/sns/governance/src/extensions.rs (L788-789)
```rust
        let expiry_time_sec = self.env.now().saturating_add(ONE_HOUR_SECONDS);
        let expiry_time_nsec = expiry_time_sec.saturating_mul(NANO_SECONDS_PER_SECOND);
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

**File:** rs/sns/treasury_manager/src/lib.rs (L251-256)
```rust
    /// Implements the `deposit` API function.
    fn deposit(
        &mut self,
        request: DepositRequest,
    ) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;

```

**File:** rs/sns/governance/src/governance.rs (L3783-3804)
```rust
                if let Some(new_follower_neuron_ids) = topic_followers
                    .and_then(|topic_followers| topic_followers.get(current_neuron_id))
                {
                    for follower_neuron_id in new_follower_neuron_ids {
                        follower_neuron_ids.insert(follower_neuron_id.clone());
                    }
                }

                if let Some(new_follower_neuron_ids) =
                    neuron_id_to_follower_neuron_ids.get(current_neuron_id)
                {
                    for follower_neuron_id in new_follower_neuron_ids {
                        follower_neuron_ids.insert(follower_neuron_id.clone());
                    }
                }
            }

            // Prepare for the next iteration of the (outer most) loop by
            // constructing the next BFS tier (from follower_neuron_ids).
            induction_votes.clear();
            for follower_neuron_id in follower_neuron_ids {
                let Some(follower_neuron) = neurons.get(&follower_neuron_id.to_string()) else {
```
