### Title
SNS Developer Neuron Reduced `voting_power_percentage_multiplier` Bypassed via Disburse and Re-stake - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

SNS developer neurons are created at genesis with a reduced `voting_power_percentage_multiplier` (< 100%) to limit their voting influence proportionally during the decentralization swap. However, once the neuron's dissolve delay (and optional vesting period) expires, the developer can disburse the restricted neuron's stake back to their ledger account and re-stake it as a brand-new neuron. The newly claimed neuron receives `DEFAULT_VOTING_POWER_PERCENTAGE_MULTIPLIER = 100`, fully bypassing the intended restriction.

---

### Finding Description

The SNS `FractionalDeveloperVotingPower` distribution strategy assigns developer neurons a `voting_power_percentage_multiplier` calculated as `initial_swap_amount_e8s / total_e8s`. When only a fraction of the total swap tokens are sold in the initial round, this multiplier is less than 100, intentionally capping developer voting power. [1](#0-0) 

The multiplier is applied in `Neuron::voting_power()` as a final percentage reduction: [2](#0-1) 

The `split_neuron` function correctly propagates the restricted multiplier to child neurons: [3](#0-2) 

However, the bypass path is through `Disburse` followed by `ClaimOrRefresh`. When a neuron is claimed fresh, it receives the default multiplier of 100: [4](#0-3) 

The proto documentation explicitly states the restriction is only for neurons created at initialization: [5](#0-4) 

There is no check in the `Disburse` handler or the neuron-claiming path that detects whether the staker was previously a restricted developer neuron and enforces the multiplier on the new neuron.

**Attack path:**
1. Developer holds a genesis neuron with `voting_power_percentage_multiplier = M` (where M < 100).
2. Developer waits for the dissolve delay (and vesting period, if set) to expire.
3. Developer calls `manage_neuron { Disburse }` — tokens are transferred to their ledger account; the restricted neuron is destroyed.
4. Developer transfers tokens back to a new neuron subaccount and calls `manage_neuron { ClaimOrRefresh }`.
5. The new neuron is created with `voting_power_percentage_multiplier = 100`, giving full unrestricted voting power.

---

### Impact Explanation

A developer who was intentionally restricted to, say, 50% voting power during the decentralization phase can immediately obtain 100% voting power after their dissolve delay expires, without waiting for subsequent swap rounds to organically raise the multiplier. This undermines the decentralization guarantee of the `FractionalDeveloperVotingPower` mechanism: developers can gain disproportionate governance control and pass proposals that benefit themselves at the expense of community token holders.

---

### Likelihood Explanation

The bypass requires the developer's dissolve delay (minimum 6 months by default) and any vesting period to expire. For SNS instances with multi-round swap designs where `initial_swap_amount_e8s < total_e8s`, this is a realistic scenario. The developer controls their own neuron and needs no special privilege beyond being the neuron controller. The `Disburse` and `ClaimOrRefresh` operations are standard, publicly accessible `manage_neuron` commands. [6](#0-5) 

---

### Recommendation

When a principal claims or refreshes a neuron, check whether the staking subaccount corresponds to a previously restricted developer neuron (or whether the principal was a developer at genesis) and carry forward the original `voting_power_percentage_multiplier` rather than defaulting to 100. Alternatively, enforce the multiplier at the principal level rather than the neuron level, so that re-staking does not reset it.

---

### Proof of Concept

```
// Setup: SNS with initial_swap_amount_e8s = 500_000_000, total_e8s = 1_000_000_000
// Developer neuron gets voting_power_percentage_multiplier = 50

// Step 1: Wait for dissolve delay to expire (e.g., 6 months)

// Step 2: Disburse the restricted developer neuron
manage_neuron({
  id: developer_neuron_id,
  command: Disburse({ to_account: developer_ledger_account })
})
// Developer neuron is gone; tokens are in developer_ledger_account

// Step 3: Transfer tokens to a new neuron subaccount and claim
// (transfer to governance subaccount derived from developer principal + new memo)
icrc1_transfer({ to: new_neuron_subaccount, amount: stake_e8s })

manage_neuron({
  command: ClaimOrRefresh({ by: MemoAndController({ memo: new_memo, controller: developer_principal }) })
})
// New neuron created with voting_power_percentage_multiplier = 100 (DEFAULT)
// Developer now has DOUBLE the intended voting power
``` [4](#0-3) [7](#0-6)

### Citations

**File:** rs/sns/init/src/distributions.rs (L59-66)
```rust
        // Multiplying this way will give the developer_voting_power_percentage_multiplier
        // as a percentage while also allowing use of checked_div.
        let developer_voting_power_percentage_multiplier = ((swap.initial_swap_amount_e8s as u128)
            * 100)
            .checked_div(swap.total_e8s as u128)
            .expect(
                "Underflow detected when calculating developer voting power percentage multiplier",
            ) as u64;
```

**File:** rs/sns/init/src/distributions.rs (L151-162)
```rust
        Ok(Neuron {
            id: Some(NeuronId {
                id: subaccount.to_vec(),
            }),
            permissions: vec![permission],
            cached_neuron_stake_e8s: stake_e8s,
            followees: btreemap! {},
            dissolve_state: Some(DissolveState::DissolveDelaySeconds(dissolve_delay_seconds)),
            voting_power_percentage_multiplier,
            vesting_period_seconds,
            ..Default::default()
        })
```

**File:** rs/sns/governance/src/neuron.rs (L27-28)
```rust
/// The default voting_power_percentage_multiplier applied to a neuron.
pub const DEFAULT_VOTING_POWER_PERCENTAGE_MULTIPLIER: u64 = 100;
```

**File:** rs/sns/governance/src/neuron.rs (L237-245)
```rust
        let v = self.voting_power_percentage_multiplier as u128;

        // Apply the multiplier to 'ad_stake' and divide by 100 to have the same effect as
        // multiplying by a percent.
        let vad_stake = ad_stake
            .checked_mul(v)
            .expect("Overflow detected when calculating voting power")
            .checked_div(100)
            .expect("Underflow detected when calculating voting power");
```

**File:** rs/sns/governance/src/governance.rs (L1300-1316)
```rust

        let min_stake = self
            .proto
            .parameters
            .as_ref()
            .expect("Governance must have NervousSystemParameters.")
            .neuron_minimum_stake_e8s
            .expect("NervousSystemParameters must have neuron_minimum_stake_e8s");

        let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();

        // Get the neuron and clone to appease the borrow checker.
        // We'll get a mutable reference when we need to change it later.
        let parent_neuron = self.get_neuron_result(id)?.clone();
        let parent_nid = parent_neuron.id.as_ref().expect("Neurons must have an id");

        parent_neuron.check_authorized(caller, NeuronPermissionType::Split)?;
```

**File:** rs/sns/governance/src/governance.rs (L1374-1376)
```rust
            dissolve_state: parent_neuron.dissolve_state,
            voting_power_percentage_multiplier: parent_neuron.voting_power_percentage_multiplier,
            source_nns_neuron_id: parent_neuron.source_nns_neuron_id,
```

**File:** rs/sns/governance/api/src/ic_sns_governance.pb.v1.rs (L148-152)
```rust
    /// A percentage multiplier to be applied when calculating the voting power of a neuron.
    /// The multiplier's unit is a integer percentage in the range of 0 to 100. The
    /// voting_power_percentage_multiplier can only be less than 100 for a developer neuron
    /// that is created at SNS initialization.
    pub voting_power_percentage_multiplier: u64,
```
