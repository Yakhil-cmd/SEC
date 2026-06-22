### Title
Division by Zero in Node Provider Reward Calculation Causes Governance Canister Trap - (File: rs/nns/governance/src/governance.rs)

### Summary
The `get_node_provider_reward` function in `rs/nns/governance/src/governance.rs` performs an integer division by `xdr_permyriad_per_icp` without any zero-guard. If this value is zero — which is possible when the ICP/XDR conversion rate is unset or falls to zero — the governance canister will trap (Wasm integer division by zero), permanently blocking the monthly node provider reward distribution for that epoch.

### Finding Description
In `get_node_provider_reward`, the ICP e8s reward amount is computed as:

```rust
let amount_e8s = ((xdr_permyriad_reward as u128 * TOKEN_SUBDIVIDABLE_BY as u128)
    / xdr_permyriad_per_icp as u128) as u64;
``` [1](#0-0) 

There is no guard checking whether `xdr_permyriad_per_icp` is zero before the division. In Rust, integer division by zero causes a panic, which in a Wasm canister becomes a trap, rolling back the entire call and preventing the reward distribution from completing.

The `minimum_icp_xdr_rate` field in `NetworkEconomics` is a `u64` protobuf field that defaults to `0`: [2](#0-1) 

If the governance canister's `NetworkEconomics` has `minimum_icp_xdr_rate = 0` (the protobuf default) and the actual ICP/XDR conversion rate is also zero (e.g., the CMC has not yet set a rate, or the rate oracle has not been called), then the effective rate passed to `get_node_provider_reward` is zero, triggering the trap.

Contrast this with the existing zero-guard in the voting rewards path, which explicitly checks `total_voting_rights < 0.001` before dividing: [3](#0-2) 

No equivalent guard exists in `get_node_provider_reward`.

### Impact Explanation
A trap in the governance canister during the monthly node provider reward distribution causes the entire `distribute_rewards` execution to roll back. Node providers receive no ICP for that reward period. If the zero-rate condition persists across multiple epochs, all node provider payments are permanently blocked until the rate is corrected via a governance proposal. This is a **ledger conservation / governance liveness bug**: legitimate node provider rewards are silently lost for the affected period.

### Likelihood Explanation
The NNS in production maintains a non-zero `minimum_icp_xdr_rate`. However:
- The protobuf default for `minimum_icp_xdr_rate` is `0`, meaning a freshly initialized or misconfigured governance canister is vulnerable.
- The ICP/XDR rate is set externally by the CMC; if the CMC fails to push a rate update and the stored rate expires or is reset, the effective rate used could be zero.
- No code-level enforcement prevents `xdr_permyriad_per_icp = 0` from reaching the division.

Likelihood is **medium** for a production NNS (rate is normally non-zero) but **high** for any SNS or newly deployed governance instance that inherits the same code path.

### Recommendation
Add an explicit zero-guard before the division in `get_node_provider_reward`, mirroring the pattern already used in the voting rewards path:

```rust
pub fn get_node_provider_reward(
    np: &NodeProvider,
    xdr_permyriad_reward: u64,
    xdr_permyriad_per_icp: u64,
) -> Option<RewardNodeProvider> {
    if xdr_permyriad_per_icp == 0 {
        return None; // or log a warning and skip
    }
    // ... existing logic
}
```

Additionally, enforce a non-zero minimum at the call site when selecting the effective rate from `NetworkEconomics`.

### Proof of Concept
1. Deploy a governance canister with default `NetworkEconomics` (`minimum_icp_xdr_rate = 0`).
2. Ensure no ICP/XDR conversion rate has been set (or set it to `0` via an `UpdateIcpXdrConversionRate` proposal with `xdr_permyriad_per_icp = 0`).
3. Trigger the monthly node provider reward distribution (either via the automated timer or by submitting a `RewardNodeProviders` proposal).
4. The governance canister traps at the division in `get_node_provider_reward` (line 8254–8255), rolling back the call.
5. Node providers receive no rewards for the period; the distribution cannot complete until the rate is corrected. [4](#0-3)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6712-6719)
```rust
        let reward_distribution = if total_voting_rights < 0.001 {
            println!(
                "{}WARNING: total_voting_rights == {}, even though considered_proposals \
                 is nonempty (see earlier log). Therefore, we skip incrementing maturity \
                 to avoid dividing by zero (or super small number).",
                LOG_PREFIX, total_voting_rights,
            );
            None
```

**File:** rs/nns/governance/src/governance.rs (L8248-8271)
```rust
pub fn get_node_provider_reward(
    np: &NodeProvider,
    xdr_permyriad_reward: u64,
    xdr_permyriad_per_icp: u64,
) -> Option<RewardNodeProvider> {
    if let Some(np_id) = np.id.as_ref() {
        let amount_e8s = ((xdr_permyriad_reward as u128 * TOKEN_SUBDIVIDABLE_BY as u128)
            / xdr_permyriad_per_icp as u128) as u64;

        let to_account = Some(if let Some(account) = &np.reward_account {
            account.clone()
        } else {
            AccountIdentifier::from(*np_id).into()
        });

        Some(RewardNodeProvider {
            node_provider: Some(np.clone()),
            amount_e8s,
            reward_mode: Some(RewardMode::RewardToAccount(RewardToAccount { to_account })),
        })
    } else {
        None
    }
}
```

**File:** rs/nns/governance/src/gen/ic_nns_governance.pb.v1.rs (L2062-2068)
```rust
    /// Measured in XDR (the currency code of IMF SDR) to two decimal
    /// places.
    ///
    /// See /rs/protobuf/def/registry/conversion_rate/v1/conversion_rate.proto
    /// for more information on the rate itself.
    #[prost(uint64, tag = "5")]
    pub minimum_icp_xdr_rate: u64,
```
