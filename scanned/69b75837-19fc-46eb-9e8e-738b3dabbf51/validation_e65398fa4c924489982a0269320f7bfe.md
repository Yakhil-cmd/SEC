### Title
Unbounded `transfer_fee` in SNS `ManageLedgerParameters` Proposal Allows Governance Majority to Drain User Funds - (File: rs/sns/governance/src/proposal.rs)

### Summary
The `validate_and_render_manage_ledger_parameters` function in SNS governance performs no upper-bound validation on the `transfer_fee` field of a `ManageLedgerParameters` proposal. A governance majority (or a malicious SNS with a concentrated voting stake) can set the SNS ledger's transfer fee to an arbitrarily large value — up to `u64::MAX` — effectively confiscating the entire balance of any user who subsequently attempts a transfer.

### Finding Description
The `ManageLedgerParameters` proposal action allows SNS governance to change the SNS ICRC-1 ledger's `transfer_fee`. The validation function `validate_and_render_manage_ledger_parameters` checks only that at least one field is `Some`, and validates `token_name`, `token_symbol`, and `token_logo` with dedicated validators. However, the `transfer_fee` field is accepted without any range check:

```rust
// rs/sns/governance/src/proposal.rs:1773-1776
if let Some(transfer_fee) = transfer_fee {
    render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
    change = true;
}
```

No ceiling is enforced. The `ManageLedgerParameters` struct itself is a plain `Option<u64>`:

```rust
// rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs:558-567
pub struct ManageLedgerParameters {
    pub transfer_fee: ::core::option::Option<u64>,
    ...
}
```

When the proposal is executed, `perform_manage_ledger_parameters` upgrades the SNS ledger canister with the new fee via `LedgerUpgradeArgs`, and then unconditionally writes the new fee into `NervousSystemParameters.transaction_fee_e8s`:

```rust
// rs/sns/governance/src/governance.rs:3191-3194
if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
    && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
{
    nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
}
```

The ICRC-1 ledger's `upgrade` function applies the new fee directly with no upper-bound check:

```rust
// rs/ledger_suite/icrc1/ledger/src/lib.rs:927-933
if let Some(transfer_fee) = args.transfer_fee {
    self.transfer_fee = Tokens::try_from(transfer_fee.clone()).unwrap_or_else(|e| {
        ic_cdk::trap(...)
    });
}
```

The only validation at the ledger level is a conversion check (not a range check). Any value up to `u64::MAX` is accepted.

By contrast, `NervousSystemParameters` (changed via `ManageNervousSystemParameters`) has no ceiling on `transaction_fee_e8s` either, but that path is a separate proposal type. The `ManageLedgerParameters` path is the direct, on-chain mechanism to change the live ledger fee with no guard.

### Impact Explanation
**High.** A governance majority that passes a `ManageLedgerParameters` proposal with `transfer_fee = u64::MAX` causes the SNS ledger to require a fee equal to `u64::MAX` token-quantums for every transfer. Since no user can hold that many tokens, all transfers become permanently impossible, effectively freezing the token. Alternatively, setting the fee to just below a user's balance causes the entire balance to be consumed as fee on the next transfer. This is a direct, on-chain, irreversible loss of user funds.

### Likelihood Explanation
**Low-to-Medium.** Exploiting this requires a governance majority — either a malicious founding team that retained concentrated voting power, or a governance attack where an attacker accumulates enough voting stake. SNS DAOs with poorly distributed voting power (e.g., a whale neuron holder or a team that did not fully decentralize) are realistic targets. The attack requires no off-chain capability, no key compromise, and no subnet-level access — only a passed governance proposal.

### Recommendation
Add an explicit upper-bound check on `transfer_fee` inside `validate_and_render_manage_ledger_parameters` in `rs/sns/governance/src/proposal.rs`. A reasonable ceiling (e.g., `1_000_000_000` token-quantums, or a constant analogous to `NervousSystemParameters::MAX_PROPOSALS_TO_KEEP_PER_ACTION_CEILING`) should be defined and enforced:

```rust
if let Some(transfer_fee) = transfer_fee {
    const MAX_TRANSFER_FEE: u64 = 1_000_000_000; // example ceiling
    if *transfer_fee > MAX_TRANSFER_FEE {
        return Err(format!(
            "transfer_fee ({transfer_fee}) exceeds the maximum allowed value ({MAX_TRANSFER_FEE})"
        ));
    }
    render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n");
    change = true;
}
```

This mirrors the pattern already used for other `NervousSystemParameters` fields such as `max_proposals_to_keep_per_action`.

### Proof of Concept

1. An SNS is deployed with a normal `transfer_fee` of `10_000` e8s.
2. A neuron holder with majority voting power submits a `ManageLedgerParameters` proposal with `transfer_fee = Some(u64::MAX)`.
3. `validate_and_render_manage_ledger_parameters` accepts the proposal — no upper-bound check exists.
4. The proposal passes and `perform_manage_ledger_parameters` upgrades the SNS ledger with `transfer_fee = u64::MAX`.
5. The ICRC-1 ledger's `upgrade` function sets `self.transfer_fee = u64::MAX` with no rejection.
6. All subsequent user `icrc1_transfer` calls fail with `BadFee { expected_fee: u64::MAX }` since no user can supply that fee.
7. All SNS token balances are permanently frozen; the token is rendered non-transferable.

**Root cause location:** [1](#0-0) 

**Execution path — no upper-bound applied during upgrade:** [2](#0-1) 

**Ledger upgrade applies fee without range check:** [3](#0-2) 

**Contrast: other parameters have explicit ceilings:** [4](#0-3)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L1773-1776)
```rust
    if let Some(transfer_fee) = transfer_fee {
        render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
        change = true;
    }
```

**File:** rs/sns/governance/src/governance.rs (L3191-3195)
```rust
                    if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
                        && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
                    {
                        nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
                    }
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L927-933)
```rust
        if let Some(transfer_fee) = args.transfer_fee {
            self.transfer_fee = Tokens::try_from(transfer_fee.clone()).unwrap_or_else(|e| {
                ic_cdk::trap(format!(
                    "failed to convert transfer fee {transfer_fee} to tokens: {e}"
                ))
            });
        }
```

**File:** rs/sns/governance/src/types.rs (L379-431)
```rust
    /// This is an upper bound for `max_proposals_to_keep_per_action`. Exceeding it
    /// may cause degradation in the governance canister or the subnet hosting the SNS.
    pub const MAX_PROPOSALS_TO_KEEP_PER_ACTION_CEILING: u32 = 700;

    /// This is an upper bound for `max_number_of_neurons`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    /// See also: `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`.
    pub const MAX_NUMBER_OF_NEURONS_CEILING: u64 = 200_000;

    /// This is an upper bound for `max_number_of_proposals_with_ballots`. Exceeding
    /// it may cause degradation in the governance canister or the subnet hosting the SNS.
    pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING: u64 = 700;

    /// This is an upper bound for `initial_voting_period_seconds`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const INITIAL_VOTING_PERIOD_SECONDS_CEILING: u64 = 30 * ONE_DAY_SECONDS;

    /// This is a lower bound for `initial_voting_period_seconds`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const INITIAL_VOTING_PERIOD_SECONDS_FLOOR: u64 = ONE_DAY_SECONDS;

    /// This is an upper bound for `wait_for_quiet_deadline_increase_seconds`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS_CEILING: u64 = 30 * ONE_DAY_SECONDS;

    /// This is a lower bound for `wait_for_quiet_deadline_increase_seconds`. We're setting it to
    /// 1 instead of 0 because values of 0 are not currently well-tested.
    pub const WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS_FLOOR: u64 = 1;

    /// This is an upper bound for `max_followees_per_function`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const MAX_FOLLOWEES_PER_FUNCTION_CEILING: u64 = 15;

    /// This is an upper bound for `max_number_of_principals_per_neuron`. Exceeding
    /// it may cause may cause degradation in the governance canister or the subnet
    /// hosting the SNS.
    pub const MAX_NUMBER_OF_PRINCIPALS_PER_NEURON_CEILING: u64 = 15;

    /// This is a lower bound for `max_number_of_principals_per_neuron`.
    /// Decreasing it below this number is problematic because SNS Swap assumes
    /// that there are allowed to be at least 5 principals per
    /// neuron during ClaimSwapNeuronsRequest.
    pub const MAX_NUMBER_OF_PRINCIPALS_PER_NEURON_FLOOR: u64 = 5;

    /// This is an upper bound for `max_dissolve_delay_bonus_percentage`. High values
    /// may improve the incentives when voting, but too-high values may also lead
    /// to an over-concentration of voting power. The value used by the NNS is 100.
    pub const MAX_DISSOLVE_DELAY_BONUS_PERCENTAGE_CEILING: u64 = 900;

    /// This is an upper bound for `max_age_bonus_percentage`. High values
    /// may improve the incentives when voting, but too-high values may also lead
    /// to an over-concentration of voting power. The value used by the NNS is 25.
    pub const MAX_AGE_BONUS_PERCENTAGE_CEILING: u64 = 400;
```
