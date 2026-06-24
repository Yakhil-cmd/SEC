### Title
Missing Execution-Time Re-Check and Concurrency Lock in `MintSnsTokens` Allows 7-Day Minting Limit Bypass - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

`perform_mint_sns_tokens` lacks both the execution-time spending-limit re-check and the concurrency lock that its sibling `perform_transfer_sns_treasury_funds` has. When the `MintSnsTokens` 7-day minting cap is activated (TODO NNS1-2982, already scaffolded in the codebase), two or more adopted proposals can each pass the submission-time check against a stale state and then execute without any re-validation, minting more SNS tokens than the governance-enforced limit allows.

---

### Finding Description

**`TransferSnsTreasuryFunds` execution path** (`perform_transfer_sns_treasury_funds`) has three layers of protection:

1. **Submission-time check** – `treasury_valuation_if_proposal_amount_is_small_enough_or_err` validates the 7-day rolling total against the current state of executed proposals.
2. **Execution-time re-check** – `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` re-reads `self.proto.proposals.values()` immediately before the actual ledger call, catching any state changes that occurred between submission and execution.
3. **Concurrency lock** – a `thread_local! { static IN_PROGRESS_PROPOSAL_ID }` mutex prevents a second `TransferSnsTreasuryFunds` proposal from entering the critical section while the first is awaiting the ledger response. [1](#0-0) 

**`MintSnsTokens` execution path** (`perform_mint_sns_tokens`) has **none** of these protections:

```rust
async fn perform_mint_sns_tokens(
    &mut self,
    mint: MintSnsTokens,
) -> Result<(), GovernanceError> {
    // ... parse to/amount ...
    self.ledger
        .transfer_funds(amount_e8s, 0, None, to, mint.memo())
        .await?;
    Ok(())
}
```

No lock, no re-check — the function goes straight to the ledger call. [2](#0-1) 

The 7-day minting upper bound for `MintSnsTokens` is currently stubbed to `Decimal::MAX` (effectively unlimited) with an explicit TODO to enable the real limit:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
``` [3](#0-2) 

The real limit function (`mint_sns_tokens_7_day_total_upper_bound_tokens`) is already implemented and is identical in logic to the `TransferSnsTreasuryFunds` cap: [4](#0-3) 

The commented-out enforcement block is ready to be uncommented: [5](#0-4) 

The submission-time validation path (`treasury_valuation_if_proposal_amount_is_small_enough_or_err`) reads the proposal list **before** any `await`, so its snapshot of `spent_tokens` is stale by the time execution occurs: [6](#0-5) 

---

### Impact Explanation

Once TODO NNS1-2982 is activated, an attacker who controls a governance majority (or a single whale neuron) can:

1. Submit N `MintSnsTokens` proposals simultaneously, each for amount `A` where `A < limit L` and `N × A >> L`.
2. All N proposals pass the submission-time check because `spent_tokens = 0` at the moment each is validated.
3. All N proposals are adopted and begin executing. Because there is no concurrency lock, proposals 2…N can enter `perform_mint_sns_tokens` while proposal 1 is suspended at `self.ledger.transfer_funds(...).await`.
4. Even in the sequential case (separate heartbeat rounds), proposals 2…N execute without any execution-time re-check, so the already-updated `executed_timestamp_seconds` of proposal 1 is never consulted.
5. Total minted = `N × A`, which can far exceed `L`.

The impact is **unauthorized SNS token inflation** — the governance-enforced 7-day minting cap is bypassed, allowing the treasury to mint an unbounded quantity of SNS tokens to an attacker-controlled account.

---

### Likelihood Explanation

- Requires a governance majority (or a single whale neuron with sufficient voting power), which is a realistic attacker model for SNS DAOs with concentrated token holdings.
- The attack requires no privileged system access, no key compromise, and no external oracle manipulation.
- The vulnerability becomes active the moment TODO NNS1-2982 is merged — a planned, tracked code change.
- The `TransferSnsTreasuryFunds` path already demonstrates that DFINITY is aware of this class of bug and has mitigated it there; the omission in `MintSnsTokens` is an oversight, not a design choice.

---

### Recommendation

Mirror the `TransferSnsTreasuryFunds` pattern in `perform_mint_sns_tokens`:

1. **Add a concurrency lock** — a `thread_local! { static IN_PROGRESS_PROPOSAL_ID }` guard identical to the one in `perform_transfer_sns_treasury_funds`.
2. **Add an execution-time re-check** — before calling `self.ledger.transfer_funds`, call an analog of `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` that reads `self.proto.proposals.values()` and verifies the rolling 7-day minted total plus the current proposal amount does not exceed the limit.
3. **Do not activate TODO NNS1-2982 without first implementing (1) and (2).** [7](#0-6) 

---

### Proof of Concept

**Setup:** SNS with a medium-sized treasury (e.g., 10 000 SNS tokens worth ~420 000 XDR). The 7-day minting cap is 25 % = 2 500 tokens. TODO NNS1-2982 has been activated.

**Steps:**

1. Whale neuron holder submits **Proposal A**: `MintSnsTokens { amount_e8s: 2_400 * E8, to_principal: attacker }`.
   - Submission-time check: `spent = 0`, `limit = 2500`, `2400 < 2500` → **passes**.
2. Whale neuron holder submits **Proposal B**: `MintSnsTokens { amount_e8s: 2_400 * E8, to_principal: attacker }`.
   - Submission-time check: `spent = 0` (A not yet executed), `limit = 2500`, `2400 < 2500` → **passes**.
3. Both proposals are adopted (whale votes yes on both).
4. Governance heartbeat executes Proposal A:
   - `perform_mint_sns_tokens` calls `self.ledger.transfer_funds(2_400 * E8, ...).await` → yields.
5. During the yield, governance heartbeat executes Proposal B:
   - `perform_mint_sns_tokens` calls `self.ledger.transfer_funds(2_400 * E8, ...).await` → no lock, no re-check.
6. Both ledger calls complete. Attacker receives **4 800 SNS tokens**, nearly double the 2 500-token cap.

Even without concurrency (sequential execution across two heartbeats), step 5 still succeeds because `perform_mint_sns_tokens` never reads `self.proto.proposals.values()` to verify the updated rolling total. [2](#0-1) [3](#0-2) [5](#0-4)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L3062-3088)
```rust
    async fn perform_mint_sns_tokens(
        &mut self,
        mint: MintSnsTokens,
    ) -> Result<(), GovernanceError> {
        let to = Account {
            owner: mint
                .to_principal
                .ok_or(GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    "Expected mint to have a target principal",
                ))?
                .0,
            subaccount: mint
                .to_subaccount
                .as_ref()
                .map(|s| bytes_to_subaccount(&s.subaccount[..]))
                .transpose()?,
        };
        let amount_e8s = mint.amount_e8s.ok_or(GovernanceError::new_with_message(
            ErrorType::InvalidProposal,
            "Expected MintSnsTokens to have an an amount_e8s",
        ))?;
        self.ledger
            .transfer_funds(amount_e8s, 0, None, to, mint.memo())
            .await?;
        Ok(())
    }
```

**File:** rs/sns/governance/src/proposal.rs (L780-814)
```rust
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
```

**File:** rs/sns/governance/src/proposal.rs (L1025-1033)
```rust
    /* TODO(NNS1-2982): Uncomment.
    fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
        mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
            // Err is most likely a bug.
            .map_err(|treasury_limit_error| {
                format!("Unable to validate amount: {:?}", treasury_limit_error,)
            })
    }
    */
```

**File:** rs/sns/governance/src/proposal.rs (L1035-1041)
```rust
    // TODO(NNS1-2982): Delete.
    fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
        // Ideally, we'd return infinity, but Decimal does not have that. This is the next best
        // thing, and should be good enough, because we have already planned the obselences of this
        // code (see tickets NNS1-298(1|2)).
        Ok(Decimal::MAX)
    }
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```
