### Title
MintSnsTokens 7-Day Rolling Limit Unenforced Due to Commented-Out Code - (File: rs/sns/governance/src/proposal.rs)

### Summary
The SNS governance canister enforces a 7-day rolling aggregate limit on `TransferSnsTreasuryFunds` proposals to prevent excessive treasury outflows. An equivalent limit for `MintSnsTokens` proposals exists in the codebase but is permanently disabled via a `TODO` comment, meaning any SNS governance majority can pass an unlimited number of `MintSnsTokens` proposals within a 7-day window with no aggregate cap enforced.

### Finding Description
In `rs/sns/governance/src/proposal.rs`, the import for `mint_sns_tokens_7_day_total_upper_bound_tokens` is commented out with a `TODO(NNS1-2982)` marker, while the parallel function `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens` is actively imported and enforced: [1](#0-0) 

There are three separate NNS1-2982 markers in `proposal.rs`, indicating that the validation call sites for the mint limit are also commented out throughout the proposal validation logic. The `TransferSnsTreasuryFunds` path has its 7-day upper-bound check active, creating an asymmetry: treasury transfers are rate-limited but token minting is not.

The `MintSnsTokens` action is executed via `perform_action` in `rs/sns/governance/src/governance.rs`: [2](#0-1) 

Because the aggregate limit function is never called during proposal validation, a sequence of individually-valid `MintSnsTokens` proposals can be submitted and adopted within a single 7-day window with no cumulative cap applied.

### Impact Explanation
An SNS governance majority can mint an unbounded quantity of SNS tokens within any 7-day window by submitting multiple `MintSnsTokens` proposals in succession. Each proposal passes individual validation (per-proposal amount limits may still apply), but the intended aggregate rolling-window cap is never checked. This allows unlimited token inflation, diluting all existing SNS token holders and potentially collapsing the token's economic value. The `TransferSnsTreasuryFunds` path has the equivalent protection active, confirming the mint path is the anomaly.

### Likelihood Explanation
Any SNS instance where a governance majority exists — including a legitimately-formed majority — can trigger this. The 7-day rolling limit is a safety mechanism designed to constrain what even a legitimate governance majority can do in a short window. Because the check is entirely absent (not just weakened), the full impact is reachable with a single governance majority and a sequence of proposals. SNS governance is permissionless to participate in, so accumulating majority is a realistic attacker path.

### Recommendation
Uncomment `mint_sns_tokens_7_day_total_upper_bound_tokens` from the import in `rs/sns/governance/src/proposal.rs` and restore all three commented-out call sites guarded by `TODO(NNS1-2982)` that enforce the 7-day rolling limit during `MintSnsTokens` proposal validation. The enforcement pattern should mirror the existing `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens` check.

### Proof of Concept
1. Deploy an SNS with a governance majority controlled by a single principal.
2. Submit a `MintSnsTokens` proposal minting just below the per-proposal limit. Pass it.
3. Immediately submit another `MintSnsTokens` proposal for the same amount. Pass it.
4. Repeat step 3 as many times as desired within the 7-day window.
5. Observe that each proposal is validated and executed independently with no aggregate limit check, resulting in cumulative minting far exceeding what the 7-day rolling cap would have permitted.
6. Compare with `TransferSnsTreasuryFunds`: a second proposal in the same window is rejected once the 7-day aggregate is reached, confirming the asymmetry. [1](#0-0) [2](#0-1)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L51-54)
```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
```

**File:** rs/sns/governance/src/governance.rs (L2212-2212)
```rust
            Action::MintSnsTokens(mint) => self.perform_mint_sns_tokens(mint).await,
```
