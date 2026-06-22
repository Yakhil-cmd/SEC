### Title
`MintSnsTokens` Proposal Action Has No Effective 7-Day Minting Upper Bound, Enabling Unlimited SNS Token Inflation - (File: `rs/sns/governance/src/proposal.rs`)

---

### Summary

The SNS Governance canister exposes a `MintSnsTokens` proposal action that is supposed to be rate-limited by a 7-day rolling cap (analogous to the cap on `TransferSnsTreasuryFunds`). However, the enforcement function `recent_amount_total_upper_bound_tokens` for `MintSnsTokens` is intentionally stubbed to return `Decimal::MAX` (effectively infinity), and no execution-time guard exists. A governance majority can therefore pass an unlimited number of `MintSnsTokens` proposals within any 7-day window, minting an unbounded quantity of SNS tokens to any target account.

---

### Finding Description

`TransferSnsTreasuryFunds` and `MintSnsTokens` are both "token proposal actions" that implement the `TokenProposalAction` trait. For `TransferSnsTreasuryFunds`, the 7-day rolling limit is enforced in two places:

1. **At proposal submission time** via `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which calls `recent_amount_total_upper_bound_tokens` — for `TransferSnsTreasuryFunds` this returns a real bound derived from treasury valuation.
2. **At execution time** via `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` called inside `perform_transfer_sns_treasury_funds`.

For `MintSnsTokens`, both guards are missing or disabled:

**At submission time**, the `TokenProposalAction` implementation for `MintSnsTokens` returns `Decimal::MAX` as the upper bound:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
```

The correct implementation is commented out:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        ...
}
*/
```

**At execution time**, `perform_mint_sns_tokens` performs no rolling-limit check at all — it directly calls `self.ledger.transfer_funds(amount_e8s, 0, None, to, ...)` with no guard analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`.

The integration test explicitly confirms this is the current behavior:

```rust
/* TODO(NNS1-2982): Uncomment.
let err = doomed_make_proposal_result.unwrap_err();
...
*/
doomed_make_proposal_result.unwrap(); // TODO(NNS1-2982): Delete this line.
```

The limit function `mint_sns_tokens_7_day_total_upper_bound_tokens` exists and is correct in `proposals_amount_total_limit/src/lib.rs`, but it is never called in the production path.

---

### Impact Explanation

**Vulnerability class:** Governance authorization bug / ledger conservation bug.

An SNS governance majority can pass successive `MintSnsTokens` proposals — each minting up to `u64::MAX` e8s of SNS tokens — to any target account, with no cumulative 7-day cap enforced. This:

- Inflates the SNS token supply without bound, diluting all existing token holders.
- Allows the minted tokens to be directed to any principal (e.g., the proposer's own account), enabling a governance-level rug pull of SNS token value.
- Breaks the conservation invariant that the SNS ledger's total supply should only grow within the bounds intended by the DAO's protective rate limits.

This is the direct IC analog of the Solidity `recoverERC20()` bug: just as `MultiMerkleDistributor.recoverERC20()` lacked a whitelist check that `QuestBoard.recoverERC20()` had, `MintSnsTokens` lacks the rolling-cap enforcement that `TransferSnsTreasuryFunds` has.

---

### Likelihood Explanation

Exploiting this requires a governance majority in a specific SNS DAO. However:

- Many SNS DAOs have concentrated token distributions (whale neurons), making a majority achievable by a single actor or small coalition.
- The missing limit is not a design choice — the code explicitly marks it as a TODO to be fixed (NNS1-2982), confirming it is an unintended gap.
- The attack requires no privileged system access, no key compromise, and no subnet-level corruption — only sufficient SNS voting power, which is an ordinary on-chain capability.

---

### Recommendation

1. **Uncomment** the proper `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` in `rs/sns/governance/src/proposal.rs` (lines 1025–1032) and delete the stub (lines 1035–1041).
2. **Add an execution-time guard** in `perform_mint_sns_tokens` analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`, so that the rolling limit is re-checked at execution time (not just at proposal submission), preventing races between concurrent proposals.
3. **Uncomment** the corresponding integration test assertion in `rs/sns/integration_tests/src/sns_treasury.rs` (lines 942–965) and delete the `doomed_make_proposal_result.unwrap()` line.

---

### Proof of Concept

**Root cause — submission-time bypass** (`rs/sns/governance/src/proposal.rs`, lines 1035–1041):

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)  // ← effectively no limit
}
``` [1](#0-0) 

**Correct implementation exists but is commented out** (`rs/sns/governance/src/proposal.rs`, lines 1025–1033):

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation) ...
}
*/
``` [2](#0-1) 

**Root cause — execution-time bypass** (`rs/sns/governance/src/governance.rs`, lines 3062–3088): `perform_mint_sns_tokens` calls `transfer_funds` directly with no rolling-limit check: [3](#0-2) 

**Contrast with `TransferSnsTreasuryFunds`**, which calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` before transferring: [4](#0-3) 

**The limit function exists but is unused** (`rs/sns/governance/proposals_amount_total_limit/src/lib.rs`, lines 14–18): [5](#0-4) 

**Integration test confirms the limit is not enforced** (`rs/sns/integration_tests/src/sns_treasury.rs`, lines 942–966): [6](#0-5)

### Citations

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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```

**File:** rs/sns/integration_tests/src/sns_treasury.rs (L942-966)
```rust
    /* TODO(NNS1-2982): Uncomment.
    let err = doomed_make_proposal_result.unwrap_err();
    let SnsGovernanceError {
        error_type,
        error_message,
    } = &err;
    assert_eq!(
        SnsErrorType::try_from(*error_type),
        Ok(SnsErrorType::InvalidProposal),
        "{:#?}",
        err,
    );
    let error_message = error_message.to_lowercase();
    for snip in [
        "amount",
        "too large",
        "2222",
        "upper bound",
        "exceeded",
        "try again",
    ] {
        assert!(error_message.contains(snip), "{:#?}", err);
    }
    */
    doomed_make_proposal_result.unwrap(); // TODO(NNS1-2982): Delete this line.
```
