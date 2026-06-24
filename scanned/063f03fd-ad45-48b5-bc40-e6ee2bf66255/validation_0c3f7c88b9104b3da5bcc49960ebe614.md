### Title
Inflated Treasury Balance via Direct Ledger Transfer Bypasses SNS Governance Proposal Limits - (File: rs/sns/governance/token_valuation/src/lib.rs)

### Summary

The SNS Governance treasury valuation system reads the treasury balance directly from the ledger via `icrc1_balance_of` in `try_get_balance_valuation_factors`. Any unprivileged user can send tokens directly to the SNS treasury account via `icrc1_transfer`, inflating the balance that governance reads. This inflated balance can push the treasury from a permissive spending regime (`NoLimit`) into a more restrictive one (`Fraction(0.25)`), causing legitimate `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals to be rejected at submission time.

### Finding Description

In `rs/sns/governance/token_valuation/src/lib.rs`, `try_get_balance_valuation_factors` fetches the treasury balance by calling `icrc1_balance_of` on the ledger:

```rust
let balance_of_request = icrc1_client.icrc1_balance_of(account);
``` [1](#0-0) 

The result is used directly as `valuation_factors.tokens`:

```rust
let tokens = Decimal::from(u128::try_from(balance_of_response.0)...) / Decimal::from(E8);
``` [2](#0-1) 

This `Valuation` is then passed to `ProposalsAmountTotalUpperBound::in_tokens` in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs`, which classifies the treasury into three regimes based on XDR value:

- **Small** (≤ 100,000 XDR): `NoLimit` — upper bound = entire balance (100%)
- **Medium** (≤ 1,200,000 XDR): `Fraction(0.25)` — upper bound = 25% of balance
- **Large** (> 1,200,000 XDR): `Xdr(300,000)` — upper bound = fixed 300,000 XDR [3](#0-2) 

The treasury account for SNS tokens is the governance canister's treasury subaccount on the SNS ledger, and for ICP it is the governance canister's default account on the ICP ledger. Both are ordinary ICRC-1/ICP ledger accounts that any user can transfer tokens into via `icrc1_transfer`. There is no internal balance record maintained by the governance canister itself — it always reads live from the ledger.

The proposal validation path in `rs/sns/governance/src/proposal.rs` calls `assess_treasury_balance` which calls `token.assess_balance(...)` which calls `try_get_balance_valuation_factors`: [4](#0-3) [5](#0-4) 

### Impact Explanation

**Concrete DoS scenario:**

Suppose an SNS treasury holds 90,000 SNS tokens each worth 1 XDR (total: 90,000 XDR). This places the treasury in the `NoLimit` regime, so the 7-day upper bound is the full 90,000 tokens. A governance proposal to transfer 80,000 tokens is valid.

An attacker sends 20,000 SNS tokens directly to the treasury account via `icrc1_transfer`. The treasury now holds 110,000 tokens (110,000 XDR), crossing the 100,000 XDR boundary into the `Fraction(0.25)` regime. The new upper bound is `110,000 × 0.25 = 27,500 tokens`. The proposal to transfer 80,000 tokens is now **rejected at submission** with "Amount is too large."

The attacker-donated tokens remain in the treasury (the SNS keeps them), but governance is temporarily paralyzed for large transfers. The attack is effective whenever the donated amount `D < 3 × original_balance`, which is easily achievable.

The same mechanism applies to `MintSnsTokens` proposals, which use the identical valuation path: [6](#0-5) 

### Likelihood Explanation

**High likelihood.** The attacker entry path requires only:
1. Holding any amount of the SNS token (or ICP for the ICP treasury)
2. Calling `icrc1_transfer` to the treasury account — a standard, permissionless ledger operation

No privileged access, no key compromise, no social engineering, and no consensus-level attack is required. The treasury accounts are deterministic and publicly known (governance canister principal + fixed subaccount nonce). The attack is cheap relative to the governance disruption it causes: donating tokens worth slightly more than 3× the treasury value pushes it from `NoLimit` to `Fraction`, and the attacker's tokens are not destroyed — they go to the treasury.

### Recommendation

Maintain an internal record of the treasury balance within the SNS Governance canister, updated only through the official deposit/transfer mechanisms, rather than reading it live from the ledger via `icrc1_balance_of`. Alternatively, use the ledger balance but subtract any "unsolicited" deposits (i.e., amounts not recorded in governance state). At minimum, the valuation used for proposal limit enforcement should be snapshotted at a trusted point (e.g., at SNS initialization or after each executed proposal) rather than fetched live at proposal submission time.

### Proof of Concept

1. Identify an SNS whose treasury is in the `NoLimit` regime (XDR value < 100,000 XDR).
2. Determine the SNS governance canister's treasury subaccount on the SNS ledger (computed via `compute_distribution_subaccount_bytes(governance_canister_id, TREASURY_SUBACCOUNT_NONCE)`).
3. Call `icrc1_transfer` on the SNS ledger, sending enough SNS tokens to the treasury account to push the total XDR value above 100,000 XDR (i.e., donate `D` tokens where `D > 100,000 XDR / token_price - current_balance`).
4. Observe that any subsequent `TransferSnsTreasuryFunds` proposal attempting to transfer more than 25% of the now-inflated balance is rejected at submission with "Amount is too large."

Relevant code path:
- `try_get_balance_valuation_factors` reads live balance: [7](#0-6) 
- Regime classification uses that balance: [3](#0-2) 
- Proposal validation enforces the limit: [8](#0-7)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L141-191)
```rust
async fn try_get_balance_valuation_factors(
    account: Account,
    icrc1_client: &mut dyn Icrc1Client,
    icps_per_token_client: &mut dyn IcpsPerTokenClient,
    xdrs_per_icp_client: &mut dyn XdrsPerIcpClient,
) -> Result<ValuationFactors, ValuationError> {
    // Fetch the three ingredients:
    //
    //     1. balance
    //     2. token -> ICP
    //     3. ICP -> XDR
    //
    // No await here. Instead, we use join (right after this).
    let balance_of_request = icrc1_client.icrc1_balance_of(account);
    let icps_per_token_request = icps_per_token_client.get();
    let xdrs_per_icp_request = xdrs_per_icp_client.get();

    // Make all (3) requests (concurrently).
    let (balance_of_response, icps_per_token_response, xdrs_per_icp_response) = join!(
        balance_of_request,
        icps_per_token_request,
        xdrs_per_icp_request,
    );

    // Unwrap/forward errors to the caller.
    let balance_of_response = balance_of_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to obtain balance from ledger: {err:?}"))
    })?;
    let icps_per_token_response = icps_per_token_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to determine ICPs per token: {err:?}"))
    })?;
    let xdrs_per_icp_response = xdrs_per_icp_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to obtain XDR per ICP: {err:?}"))
    })?;

    // Extract and interpret the data we actually care about from the (Ok) responses.
    let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
        ValuationError::new_arithmetic(format!(
            "Balance of {account:?} does not fit in u128: {err:?}"
        ))
    })?) / Decimal::from(E8);
    let icps_per_token = icps_per_token_response;
    let xdrs_per_icp = xdrs_per_icp_response;

    // Compose the fetched/interpretted data (i.e. multiply them) to construct the final result.
    Ok(ValuationFactors {
        tokens,
        icps_per_token,
        xdrs_per_icp,
    })
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L66-114)
```rust
    fn in_tokens(mut valuation: Valuation) -> Result<Decimal, ProposalsAmountTotalLimitError> {
        Self::clamp_xdrs_per_icp(&mut valuation);

        let ValuationFactors {
            tokens: balance_tokens,
            icps_per_token,
            xdrs_per_icp,
        } = valuation.valuation_factors;

        let self_ = Self::from_valuation_xdr(valuation.to_xdr());
        let result_tokens = match self_ {
            Self::NoLimit => balance_tokens,

            Self::Fraction(fraction) => balance_tokens
                .checked_mul(fraction)
                // Overflow should not be possible, since fraction is supposed to be at most 1.0.
                .ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "Unable to perform {balance_tokens} * {fraction}.",
                    ))
                })?,

            Self::Xdr(max_xdr) => {
                let xdrs_per_token = xdrs_per_icp.checked_mul(icps_per_token).ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "XDRs per token could not be calculated from valuation: {valuation:?}"
                    ))
                })?;

                // Calculate the inverse conversion rate.
                if xdrs_per_token == Decimal::from(0) {
                    // This is not reachable, because in this case, valuation.to_xdr() would return
                    // 0, and in that case, we would have taken the NoLimit branch.
                    return Err(ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "It appears that the tokens have zero value in XDR. valuation = {valuation:?}"
                    )));
                }
                let tokens_per_xdr = xdrs_per_token.inv();

                max_xdr.checked_mul(tokens_per_xdr).ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "Max tokens could not be calculated with valuation: {valuation:?}",
                    ))
                })?
            }
        };

        Ok(result_tokens)
    }
```

**File:** rs/sns/governance/src/proposal.rs (L784-790)
```rust
    let valuation = assess_treasury_balance(
        token,
        env.canister_id(),
        sns_ledger_canister_id,
        swap_canister_id,
    )
    .await?;
```

**File:** rs/sns/governance/src/proposal.rs (L799-816)
```rust
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
```

**File:** rs/sns/governance/src/treasury.rs (L256-269)
```rust
pub(crate) async fn assess_treasury_balance(
    token: Token,
    sns_governance_canister_id: CanisterId,
    sns_ledger_canister_id: CanisterId,
    swap_canister_id: CanisterId,
) -> Result<Valuation, String> {
    let treasury_account = token.treasury_account(sns_governance_canister_id)?;
    let valuation = token
        .assess_balance(sns_ledger_canister_id, swap_canister_id, treasury_account)
        .await
        .map_err(|valuation_error| {
            format!("Unable to assess current treasury balance: {valuation_error:?}")
        })?;
    Ok(valuation)
```
