### Title
Async Check-Effects-Interactions Violation Allows Bypassing 7-Day Treasury Transfer Limit at Proposal Submission — (`rs/sns/governance/src/proposal.rs`)

---

### Summary

The SNS governance `make_proposal` flow for `TransferSnsTreasuryFunds` proposals contains a check-effects-interactions violation. The 7-day rolling transfer limit is read synchronously, then an async inter-canister call is awaited, and only after the await is the limit enforced. Because the IC execution model allows other messages to be processed during an `await`, two concurrent `make_proposal` calls can both read the same stale `spent_tokens = 0`, both pass the limit check, and both be inserted into governance state — allowing the combined amount of adopted proposals to exceed the 7-day cap.

---

### Finding Description

In `rs/sns/governance/src/proposal.rs`, the function `treasury_valuation_if_proposal_amount_is_small_enough_or_err` implements the 7-day rolling limit check for `TransferSnsTreasuryFunds` (and `MintSnsTokens`) proposals: [1](#0-0) 

The sequence is:

1. **Check (line 780):** `spent_tokens` is read synchronously from the current proposal state.
2. **Interaction (lines 784–790):** `assess_treasury_balance(...).await?` makes async inter-canister calls to the CMC and swap canister to get a treasury valuation.
3. **Effects (lines 801–813):** The limit check `proposal_amount_tokens > allowance_remainder_tokens` is evaluated using the `spent_tokens` captured in step 1. [2](#0-1) 

This is called during `validate_and_render_proposal`, which is the **first** thing `make_proposal` does — before the proposal is inserted into state: [3](#0-2) 

Because the proposal is not added to state until after validation completes, two concurrent `make_proposal` messages can both enter `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, both read `spent_tokens = 0` (or the same stale value), both yield at the `assess_treasury_balance` await, and both pass the limit check independently. Both proposals are then inserted into governance state.

The `validate_and_render_action` dispatch confirms `TransferSnsTreasuryFunds` goes through this path: [4](#0-3) [5](#0-4) 

---

### Impact Explanation

Two `TransferSnsTreasuryFunds` proposals, each individually within the 7-day limit, can be submitted and adopted by governance with a combined amount exceeding the limit. The execution-time check in `perform_transfer_sns_treasury_funds` provides a partial safety net: [6](#0-5) 

The `IN_PROGRESS_PROPOSAL_ID` lock prevents concurrent execution, and `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` re-checks the limit synchronously before the actual ledger transfer. This means the second proposal will **fail at execution time** after being adopted. [7](#0-6) 

The concrete impacts are:

- **Governance disruption:** Voters are deceived into approving a proposal that will fail at execution. The governance process is wasted.
- **Limit bypass at submission:** The 7-day cap is a security invariant intended to prevent large treasury drains. Bypassing it at submission time undermines the governance safety model, even if execution-time checks catch it.
- **Griefing:** An attacker with two neurons can force governance to process and vote on proposals that are guaranteed to fail, consuming governance bandwidth.
- **Partial actual bypass risk:** If the execution-time check were ever weakened or removed (e.g., during a future refactor), the submission-time bypass would become a full treasury drain vulnerability.

---

### Likelihood Explanation

Any SNS token holder with two neurons meeting the `reject_cost_e8s` and `min_dissolve_delay` requirements can trigger this. The attack requires only submitting two `TransferSnsTreasuryFunds` proposals in rapid succession (within the same or adjacent rounds), which is achievable via standard ingress messages. No privileged access is required. [8](#0-7) 

---

### Recommendation

Apply the **effects-before-interaction** pattern: record the proposal's intended amount as a "pending" entry in state **before** making the async `assess_treasury_balance` call, and include pending proposals in the `spent_tokens` calculation. Remove the pending entry if validation fails.

Alternatively, perform the `assess_treasury_balance` call first (to get the valuation), then re-read `spent_tokens` synchronously **after** the await returns and perform the limit check at that point — ensuring the check uses up-to-date state.

The execution-time check in `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` should be retained as a defense-in-depth measure regardless. [9](#0-8) 

---

### Proof of Concept

1. SNS governance is deployed with a treasury of 1,000,000 SNS tokens. The 7-day limit is 250,000 tokens (25%).
2. Attacker controls two neurons, each with sufficient stake.
3. Attacker sends two `make_proposal` ingress messages for `TransferSnsTreasuryFunds` with `amount_e8s = 200_000 * E8` (200,000 tokens each) in rapid succession.
4. The governance canister processes message A: reads `spent_tokens = 0`, then awaits `assess_treasury_balance`.
5. While awaiting, the IC processes message B: reads `spent_tokens = 0` (same state, proposal A not yet inserted), then awaits `assess_treasury_balance`.
6. Message A's await completes: checks `200,000 > 250,000 - 0`? No → passes. Proposal A inserted.
7. Message B's await completes: checks `200,000 > 250,000 - 0`? No → passes. Proposal B inserted. [10](#0-9) 

8. Both proposals are open for voting. Governance adopts both.
9. Proposal A executes: execution-time check sees `spent = 0`, `200,000 ≤ 250,000` → transfer succeeds.
10. Proposal B executes: execution-time check sees `spent = 200,000`, `200,000 > 250,000 - 200,000 = 50,000` → **execution fails**.
11. Result: Proposal B was adopted by governance but fails silently at execution. The 7-day limit was bypassed at submission time; governance resources were wasted; voters were misled. [11](#0-10)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L395-395)
```rust
    let proposals = governance_proto.proposals.values();
```

**File:** rs/sns/governance/src/proposal.rs (L461-490)
```rust
        Action::ManageSnsMetadata(manage_sns_metadata) => {
            validate_and_render_manage_sns_metadata(manage_sns_metadata)
        }
        Action::TransferSnsTreasuryFunds(transfer) => {
            return validate_and_render_transfer_sns_treasury_funds(
                transfer,
                sns_transfer_fee_e8s,
                env,
                swap_canister_id,
                sns_ledger_canister_id,
                proposals,
            )
            .await;
        }
        Action::MintSnsTokens(mint_sns_tokens) => {
            return validate_and_render_mint_sns_tokens(
                mint_sns_tokens,
                sns_transfer_fee_e8s,
                env,
                swap_canister_id,
                sns_ledger_canister_id,
                proposals,
            )
            .await;
        }
        Action::ManageLedgerParameters(manage_ledger_parameters) => {
            validate_and_render_manage_ledger_parameters(manage_ledger_parameters)
        }
        Action::ManageDappCanisterSettings(manage_dapp_canister_settings) => {
            validate_and_render_manage_dapp_canister_settings(manage_dapp_canister_settings)
```

**File:** rs/sns/governance/src/proposal.rs (L770-817)
```rust
async fn treasury_valuation_if_proposal_amount_is_small_enough_or_err<MyTokenProposalAction>(
    env: &dyn Environment,
    sns_ledger_canister_id: CanisterId,
    swap_canister_id: CanisterId,
    proposals: impl Iterator<Item = &ProposalData>,
    action: &MyTokenProposalAction,
) -> Result<Valuation, String>
where
    MyTokenProposalAction: TokenProposalAction,
{
    let spent_tokens = action.recent_amount_total_tokens(proposals, env.now())?;

    // Get valuation of the tokens in the treasury.
    let token = action.token()?;
    let valuation = assess_treasury_balance(
        token,
        env.canister_id(),
        sns_ledger_canister_id,
        swap_canister_id,
    )
    .await?;

    // From valuation, determine limit on the total from the past 7 days.
    let max_tokens = MyTokenProposalAction::recent_amount_total_upper_bound_tokens(&valuation)
        // Err is most likely a bug.
        .map_err(|treasury_limit_error| {
            format!("Unable to validate amount: {treasury_limit_error:?}",)
        })?;

    // Finally, inspect the proposal's amount: it must not exceed max - spent (remainder). Or if
    // you prefer, equivalently, amount + spent must be <= max.
    let allowance_remainder_tokens = max_tokens.checked_sub(spent_tokens).ok_or_else(|| {
        format!("Arithmetic error while performing {max_tokens} - {spent_tokens}",)
    })?;
    let proposal_amount_tokens = action.proposal_amount_tokens()?;
    if proposal_amount_tokens > allowance_remainder_tokens {
        // Although it might not be obvious to the user, their proposal is invalid, and we
        // consider it to be "their fault".
        return Err(format!(
            "Amount is too large. Within the past 7 days, a total of {spent_tokens} tokens has already \
             been executed in like proposals. Whereas, at most {max_tokens} is allowed. An additional \
             {proposal_amount_tokens} tokens from this proposal would cause that upper bound to be exceeded. \
             Maybe, try again in a few days?"
        ));
    }

    Ok(valuation)
}
```

**File:** rs/sns/governance/src/proposal.rs (L2600-2658)
```rust
pub(crate) fn transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err<'a>(
    transfer: &TransferSnsTreasuryFunds,
    valuation: Valuation,
    proposals: impl Iterator<Item = &'a ProposalData>,
    now_timestamp_seconds: u64,
) -> Result<(), GovernanceError> {
    let allowance_tokens = transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(valuation)
        .map_err(|err| {
            // This should not be possible, because valuation was already used the same way during
            // proposal submission/creation/validation.
            GovernanceError::new_with_message(
                ErrorType::InconsistentInternalData,
                format!(
                    "Unable to determined upper bound on the amount of \
                     TransferSnsTreasuryFunds proposals: {err:?}\nvaluation:{valuation:?}",
                ),
            )
        })?;

    // The total calculated here _could_ be different from what was calculated at proposal
    // submission/creation time. A difference would result from the execution of (another)
    // TransferSnsTreasuryFunds proposal between now and then.
    let spent_tokens = total_treasury_transfer_amount_tokens(
        proposals,
        transfer.from_treasury(),
        now_timestamp_seconds - 7 * ONE_DAY_SECONDS,
    )
    .map_err(|message| {
        GovernanceError::new_with_message(ErrorType::InconsistentInternalData, message)
    })?;

    let remainder_tokens = allowance_tokens - spent_tokens;
    let transfer_amount_tokens = denominations_to_tokens(transfer.amount_e8s, E8)
        // This Err cannot be provoked, because we are dividing a u64 (amount_e8s) by a positive
        // integer (E8).
        .ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::UnreachableCode,
                format!(
                    "Unable to convert proposals amount {} e8s to tokens.",
                    transfer.amount_e8s,
                ),
            )
        })?;
    if transfer_amount_tokens > remainder_tokens {
        return Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "Executing this proposal is not allowed at this time, because doing \
                 so would cause the 7 day upper bound of {allowance_tokens} tokens to be exceeded. \
                 Maybe, try again later? The total amount transferred in the past \
                 7 days stands at {spent_tokens} tokens, and the amount in this proposal is {transfer_amount_tokens} \
                 tokens. The upper bound is based on treasury valuation factors at \
                 the time of proposal submission: {valuation:?}",
            ),
        ));
    }

    Ok(())
```

**File:** rs/sns/governance/src/governance.rs (L2980-3005)
```rust
    async fn perform_transfer_sns_treasury_funds(
        &mut self,
        proposal_id: u64, // This is just to control concurrency.
        valuation: Result<Valuation, GovernanceError>,
        transfer: &TransferSnsTreasuryFunds,
    ) -> Result<(), GovernanceError> {
        // Only execute one proposal of this type at a time.
        thread_local! {
            static IN_PROGRESS_PROPOSAL_ID: RefCell<Option<u64>> = const { RefCell::new(None) };
        }
        let release_on_drop = acquire(&IN_PROGRESS_PROPOSAL_ID, proposal_id);
        if let Err(already_in_progress_proposal_id) = release_on_drop {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Another TransferSnsTreasuryFunds proposal (ID = {already_in_progress_proposal_id}) is already in progress.",
                ),
            ));
        }

        transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(
            transfer,
            valuation?,
            self.proto.proposals.values(),
            self.env.now(),
        )?;
```

**File:** rs/sns/governance/src/governance.rs (L3463-3467)
```rust
        let now_seconds = self.env.now();

        // Validate proposal
        // TODO: return the optional extension spec
        let (rendering, action_auxiliary) = self.validate_and_render_proposal(proposal).await?;
```

**File:** rs/sns/governance/src/governance.rs (L3519-3526)
```rust
        // If the current stake of the proposer neuron is less than the cost
        // of having a proposal rejected, the neuron cannot make a proposal.
        if proposer.stake_e8s() < reject_cost_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Neuron doesn't have enough stake to submit proposal.",
            ));
        }
```
