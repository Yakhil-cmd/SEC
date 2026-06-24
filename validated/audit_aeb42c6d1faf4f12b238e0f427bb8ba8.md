Audit Report

## Title
Unprivileged Treasury Balance Inflation Restricts SNS TransferSnsTreasuryFunds Proposals - (File: rs/sns/governance/token_valuation/src/lib.rs)

## Summary

`try_get_balance_valuation_factors` reads the SNS treasury balance live from the ledger via `icrc1_balance_of`. Because the treasury accounts are ordinary ICRC-1/ICP ledger accounts, any unprivileged user can send tokens directly to them, inflating the balance used for proposal validation. This can push the treasury from the `NoLimit` regime (≤ 100,000 XDR) into the `Fraction(0.25)` regime, causing previously valid `TransferSnsTreasuryFunds` proposals to be rejected at submission with "Amount is too large." The `MintSnsTokens` claim in the report is factually incorrect: the current code returns `Decimal::MAX` as the upper bound for minting (the treasury-based limit is commented out pending NNS1-2982), so only `TransferSnsTreasuryFunds` is affected.

## Finding Description

`try_get_balance_valuation_factors` fetches the live ledger balance:

```rust
let balance_of_request = icrc1_client.icrc1_balance_of(account);
``` [1](#0-0) 

The result is used directly as `valuation_factors.tokens`: [2](#0-1) 

`ProposalsAmountTotalUpperBound::from_valuation_xdr` classifies the treasury into three regimes based on total XDR value:
- ≤ 100,000 XDR → `NoLimit` (100% of balance allowed)
- ≤ 1,200,000 XDR → `Fraction(0.25)` (25% of balance allowed)
- > 1,200,000 XDR → `Xdr(300,000)` (fixed 300,000 XDR cap) [3](#0-2) 

The SNS token treasury account is `governance_canister_id` with subaccount `compute_distribution_subaccount_bytes(governance_canister_id, TREASURY_SUBACCOUNT_NONCE)`, and the ICP treasury is the governance canister's default account — both are ordinary permissionless ledger accounts: [4](#0-3) 

The proposal validation path calls `assess_treasury_balance` → `token.assess_balance` → `try_get_balance_valuation_factors`, then enforces the limit at submission: [5](#0-4) 

**Correction to the submitted claim:** `MintSnsTokens` is NOT affected. Its `recent_amount_total_upper_bound_tokens` currently returns `Decimal::MAX` (the treasury-based limit is commented out with `TODO(NNS1-2982)`): [6](#0-5) 

## Impact Explanation

An attacker holding any SNS tokens (or ICP for the ICP treasury) can call `icrc1_transfer` to send tokens to the treasury account. If the treasury is near the 100,000 XDR boundary, a small donation pushes it into the `Fraction(0.25)` regime. Any pending or future `TransferSnsTreasuryFunds` proposal requesting more than 25% of the (now-inflated) balance is rejected at submission. The SNS governance is not completely paralyzed — proposals for ≤ 25% of the treasury still pass — but large single-proposal transfers are blocked. The attacker's tokens remain in the treasury (the SNS keeps them), making the attack economically self-limiting but repeatable. This constitutes a concrete application/platform-level DoS on SNS governance treasury operations, matching the High impact category: "Application/platform-level DoS... or SNS... security impact with concrete user or protocol harm."

## Likelihood Explanation

The attack requires only: (1) holding any amount of the relevant token, and (2) calling the standard permissionless `icrc1_transfer` to the deterministic, publicly computable treasury account. No privileged access, key compromise, or social engineering is needed. The treasury account address is fully deterministic from the governance canister principal and `TREASURY_SUBACCOUNT_NONCE = 0`. The economic cost is real (tokens go to the treasury) but can be small relative to the disruption if the treasury is near the 100,000 XDR boundary. [7](#0-6) 

## Recommendation

At proposal submission time, use a snapshotted or internally-tracked treasury balance rather than a live `icrc1_balance_of` call. Specifically: maintain an internal accounting of the treasury balance within the SNS Governance canister, updated only when proposals execute transfers or mints. Alternatively, cap the valuation used for regime classification at the balance recorded at the last executed proposal, ignoring unsolicited deposits. A minimum viable fix is to snapshot the balance at SNS initialization and update it only on executed `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals, using that snapshot for regime classification rather than the live ledger balance.

## Proof of Concept

1. Deploy a test SNS with a treasury holding ~90,000 XDR worth of SNS tokens (e.g., 90,000 tokens at 1 XDR/token). Confirm the treasury is in `NoLimit` regime.
2. Submit a `TransferSnsTreasuryFunds` proposal for 80,000 tokens. Confirm it is accepted at submission.
3. From an unprivileged account, call `icrc1_transfer` on the SNS ledger, sending 15,000 SNS tokens to `Account { owner: governance_canister_id, subaccount: Some(compute_distribution_subaccount_bytes(governance_canister_id, 0)) }`.
4. Treasury is now 105,000 tokens (105,000 XDR) → `Fraction(0.25)` regime → upper bound = 26,250 tokens.
5. Submit the same `TransferSnsTreasuryFunds` proposal for 80,000 tokens. Observe rejection: "Amount is too large... at most 26250 is allowed."

This can be implemented as a deterministic state-machine integration test extending the existing `test_transfer_sns_treasury_funds_proposals_that_are_too_big_get_blocked_at_submission` test in `rs/sns/integration_tests/src/sns_treasury.rs`, replacing the high-ICP-price mechanism with a direct `icrc1_transfer` to the treasury account. [8](#0-7)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L154-154)
```rust
    let balance_of_request = icrc1_client.icrc1_balance_of(account);
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L177-181)
```rust
    let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
        ValuationError::new_arithmetic(format!(
            "Balance of {account:?} does not fit in u128: {err:?}"
        ))
    })?) / Decimal::from(E8);
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L116-135)
```rust
    fn from_valuation_xdr(valuation_xdr: Decimal) -> Self {
        // Ideally, this would be checked at compile time. In principal, this should be possible,
        // since all the inputs are const, but I'm not sure how to do that. Therefore,
        // debug_assert_eq is used instead, and should be very nearly as good, because this will be
        // run during CI.
        debug_assert_eq!(
            Self::MAX_MEDIUM_TREASURY_SIZE_XDR.checked_mul(ONE_QUARTER),
            Some(Self::MAX_XDR),
        );

        if valuation_xdr <= Self::MAX_SMALL_TREASURY_SIZE_XDR {
            return Self::NoLimit;
        }

        if valuation_xdr <= Self::MAX_MEDIUM_TREASURY_SIZE_XDR {
            return Self::Fraction(ONE_QUARTER);
        }

        Self::Xdr(Self::MAX_XDR)
    }
```

**File:** rs/sns/governance/src/proposal.rs (L710-729)
```rust
impl TreasuryAccount for Token {
    fn treasury_account(self, sns_governance_canister_id: CanisterId) -> Result<Account, String> {
        let sns_governance_canister_id = PrincipalId::from(sns_governance_canister_id);
        let owner = Principal::from(sns_governance_canister_id);

        match self {
            Self::Icp => Ok(Account {
                owner,
                subaccount: None,
            }),

            Self::SnsToken => Ok(Account {
                owner,
                subaccount: Some(compute_distribution_subaccount_bytes(
                    sns_governance_canister_id,
                    TREASURY_SUBACCOUNT_NONCE,
                )),
            }),
        }
    }
```

**File:** rs/sns/governance/src/proposal.rs (L784-814)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L178-178)
```rust
pub const TREASURY_SUBACCOUNT_NONCE: u64 = 0;
```

**File:** rs/sns/integration_tests/src/sns_treasury.rs (L517-594)
```rust
#[test]
fn test_transfer_sns_treasury_funds_proposals_that_are_too_big_get_blocked_at_submission() {
    // Step 1: Prepare the world. What happens here is similar to what happens in Step 1 of the
    // previous test. The difference is that the price of ICP here is (unrealistically) high in
    // order to provoke a giant treasury valuation, which then puts a lower cap on the number of
    // tokens that proposals can transfer from the treasury.

    let state_machine = state_machine_builder_for_sns_tests().build();

    let (whale_neuron_id, sns_test_canister_ids) = new_treasury_scenario(&state_machine);

    let SnsTestCanisterIds {
        governance_canister_id,
        ledger_canister_id: sns_ledger_canister_id,

        root_canister_id: _,
        swap_canister_id: _,
        index_canister_id: _,
    } = sns_test_canister_ids;

    let treasury_icp_account = governance_canister_id.default_account();

    let treasury_sns_token_account = Account {
        owner: PrincipalId::from(governance_canister_id).0,
        subaccount: Some(
            compute_distribution_subaccount(
                PrincipalId::from(governance_canister_id),
                TREASURY_SUBACCOUNT_NONCE,
            )
            .0,
        ),
    };

    let start_timestamp_seconds = state_machine
        .time()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap()
        .as_secs();

    // This is where the difference described at the top of this section happens.
    state_machine
        .execute_ingress_as(
            PrincipalId::from(GOVERNANCE_CANISTER_ID), // sender
            CYCLES_MINTING_CANISTER_ID,                // destination
            "set_icp_xdr_conversion_rate",
            Encode!(&UpdateIcpXdrConversionRatePayload {
                data_source: "STONE TABLETS FROM HEAVEN".to_string(),
                timestamp_seconds: start_timestamp_seconds,
                // More specifically, here is where ICP is worth an (unrealistically) large amount.
                xdr_permyriad_per_icp: 5_000_000 * 10_000,
                reason: None,
            })
            .unwrap(),
        )
        .unwrap();

    // Steps 2: Run the code under test.

    // Whale proposes to give himself NNS treasury for dapp
    let take_icp_result_make_proposal_result = sns_make_proposal(
        &state_machine,
        governance_canister_id,
        *WHALE,
        whale_neuron_id.clone(),
        Proposal {
            title: "Transfer treasury NNS".to_string(),
            summary: "Transfer treasury to user".to_string(),
            url: "".to_string(),
            action: Some(Action::TransferSnsTreasuryFunds(TransferSnsTreasuryFunds {
                from_treasury: TransferFrom::IcpTreasury.into(),
                amount_e8s: 10000 * E8S_PER_TOKEN - NNS_DEFAULT_TRANSFER_FEE.get_e8s(),
                memo: None,
                to_principal: Some(*WHALE),
                to_subaccount: None,
            })),
        },
    );

```
