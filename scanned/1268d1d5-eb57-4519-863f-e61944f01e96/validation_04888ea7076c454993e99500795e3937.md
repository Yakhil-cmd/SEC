### Title
SNS Swap Finalization Permanently Bricked by Pre-existing Neuron ID Collision in Memo Range - (`rs/sns/swap/src/swap.rs`, `rs/sns/swap/src/types.rs`)

---

### Summary

The SNS Swap canister's `finalize` flow can be permanently bricked if any swap participant pre-creates an SNS neuron whose ID collides with a neuron the swap will attempt to claim during finalization. The neuron ID is derived deterministically from `(buyer_principal, memo)` using `compute_neuron_staking_subaccount_bytes`. The swap assigns memos starting at `NEURON_BASKET_MEMO_RANGE_START = 1_000_000`. Any buyer who participates in the swap and also creates a neuron with memo `1_000_000` (or any memo in `[1_000_000, 1_000_000 + basket_count)`) before the swap commits will cause `claim_swap_neurons` to receive `ClaimedSwapNeuronStatus::AlreadyExists` from SNS Governance. This status is mapped to `ClaimedStatus::Invalid`, which permanently marks the recipe as unretriable and causes `finalize` to halt with an error on every subsequent call.

---

### Finding Description

**Step 1 – Neuron ID derivation is deterministic and publicly known.**

The SNS Swap assigns neuron basket memos starting at `NEURON_BASKET_MEMO_RANGE_START`: [1](#0-0) 

For each direct participant, the swap creates `basket_count` neurons with memos `NEURON_BASKET_MEMO_RANGE_START + 0` through `NEURON_BASKET_MEMO_RANGE_START + (basket_count - 1)`: [2](#0-1) 

The neuron ID is `compute_neuron_staking_subaccount_bytes(buyer_principal, memo)`, which is a public, deterministic hash.

**Step 2 – SNS Governance does not restrict memo values for user-created neurons.**

A user can stake tokens to the SNS governance canister at subaccount `compute_neuron_staking_subaccount_bytes(user_principal, 1_000_000)` and claim a neuron via `ManageNeuron::ClaimOrRefresh`. The `new_neuron_id` function only checks for duplicate IDs, not for memo range restrictions: [3](#0-2) 

The memo-range restriction only applies to developer neurons during SNS initialization, not to user-created neurons: [4](#0-3) 

**Step 3 – SNS Governance returns `AlreadyExists` for colliding neuron IDs.**

When `claim_swap_neurons` is called during finalization, SNS Governance checks whether the neuron already exists and returns `ClaimedSwapNeuronStatus::AlreadyExists`: [5](#0-4) 

**Step 4 – `AlreadyExists` is mapped to `ClaimedStatus::Invalid` in the Swap canister.**

The critical mapping in `rs/sns/swap/src/types.rs`: [6](#0-5) 

`ClaimedStatus::Invalid` is a terminal state. In `to_neuron_recipe`, any recipe with `ClaimedStatus::Invalid` returns `ConversionError::Invalid` on every subsequent call, incrementing `sweep_result.invalid` permanently: [7](#0-6) 

**Step 5 – Any `invalid` count halts finalization permanently.**

`set_claim_neuron_result` treats any `invalid` count as a fatal error: [8](#0-7) 

Because `ClaimedStatus::Invalid` recipes are never retried, every subsequent call to `finalize` will also produce `invalid > 0`, permanently halting finalization.

---

### Impact Explanation

A single malicious buyer who participates in the swap (even with the minimum amount) and pre-creates a neuron with memo `NEURON_BASKET_MEMO_RANGE_START` can permanently brick the entire swap finalization for **all participants**:

1. `sweep_sns` transfers SNS tokens to the neuron staking subaccount (succeeds, tokens are now stuck in the governance canister's subaccount with no associated neuron)
2. `claim_swap_neurons` returns `invalid > 0` → finalization halts
3. SNS Governance is never set to normal mode (`set_mode` is never called)
4. Dapp controllers are never transferred to the SNS
5. Every subsequent `finalize` call also fails permanently

The SNS tokens transferred in `sweep_sns` are permanently locked in the governance canister's subaccount because no neuron was claimed for them, and the swap has no mechanism to recover them.

---

### Likelihood Explanation

The attack requires only that a buyer:
1. Participate in the swap (call `refresh_buyer_tokens` with any valid amount)
2. Before the swap commits, stake tokens to the SNS governance canister at `compute_neuron_staking_subaccount_bytes(self, 1_000_000)` and call `ManageNeuron::ClaimOrRefresh` with memo `1_000_000`

Both steps are fully unprivileged ingress calls available to any principal. The attack can also happen accidentally if a legitimate user happens to create a neuron with memo `1_000_000` while also participating in a swap. The `NEURON_BASKET_MEMO_RANGE_START` value of `1_000_000` is a publicly documented constant.

---

### Recommendation

1. **In SNS Governance `claim_swap_neurons`:** Treat `AlreadyExists` as `ClaimedStatus::Success` (idempotent success) rather than `ClaimedStatus::Invalid`, since the neuron already exists and the swap's goal of having a neuron at that ID is effectively achieved. The existing comment in the `ClaimedSwapNeuronStatus` definition already states this intent: *"Future attempts to claim the same Neuron will result in `ClaimedSwapNeuronStatus::AlreadyExists`"* — implying it should be treated as a prior success. [9](#0-8) 

2. **In the Swap canister `types.rs`:** Change the mapping so `AlreadyExists` maps to `ClaimedStatus::Success` instead of `ClaimedStatus::Invalid`: [10](#0-9) 

3. **In SNS Governance `new_neuron_id`:** Reject memo values in the range `[NEURON_BASKET_MEMO_RANGE_START, SALE_NEURON_MEMO_RANGE_END]` for user-created neurons (via `ManageNeuron::ClaimOrRefresh`) when a swap is active, analogous to the existing restriction on developer neurons.

---

### Proof of Concept

```
1. Deploy an SNS with a swap configured with basket_count = 3.
2. Attacker calls refresh_buyer_tokens to participate in the swap with the minimum amount.
3. Attacker stakes tokens to SNS governance at subaccount:
       compute_neuron_staking_subaccount_bytes(attacker_principal, 1_000_000)
   and calls ManageNeuron::ClaimOrRefresh { memo: 1_000_000 }.
   → SNS Governance creates a neuron with ID = hash(attacker_principal, 1_000_000).
4. Swap reaches its end time and commits (Lifecycle::Committed).
5. Anyone calls finalize_swap.
6. sweep_sns transfers SNS tokens to the neuron staking subaccount for the attacker
   (succeeds — ICRC1 ledger does not care about existing neurons).
7. claim_swap_neurons sends a ClaimSwapNeuronsRequest to SNS Governance for the attacker's
   neuron basket. SNS Governance finds the neuron at memo 1_000_000 already exists and
   returns ClaimedSwapNeuronStatus::AlreadyExists.
8. The Swap canister maps AlreadyExists → ClaimedStatus::Invalid.
9. set_claim_neuron_result detects invalid > 0 and sets error_message.
10. finalize returns with error. SNS Governance is never set to normal mode.
11. Every subsequent finalize call also fails permanently (Invalid recipes are never retried).
Result: Swap finalization is permanently bricked. SNS tokens are stuck.
```

The root cause is in:
- [11](#0-10)  — `AlreadyExists` mapped to `Invalid`
- [7](#0-6)  — `Invalid` recipes are never retried
- [8](#0-7)  — any `invalid` count halts finalization

### Citations

**File:** rs/sns/swap/src/swap.rs (L89-90)
```rust
pub const NEURON_BASKET_MEMO_RANGE_START: u64 = 1_000_000;
pub const SALE_NEURON_MEMO_RANGE_END: u64 = 10_000_000;
```

**File:** rs/sns/swap/src/swap.rs (L3320-3321)
```rust
    for (i, scheduled_vesting_event) in vesting_schedule.iter().enumerate() {
        let memo = memo_offset + i as u64;
```

**File:** rs/sns/swap/src/swap.rs (L3501-3511)
```rust
                ClaimedStatus::Invalid | ClaimedStatus::Unspecified => {
                    // If the Recipe is marked as invalid or unspecified, intervention is needed
                    // to make valid again. As part of that intervention, the recipe must be marked
                    // as ClaimedStatus::Pending to attempt again.
                    return Err((
                        ConversionError::Invalid,
                        format!(
                            "Recipe {self:?} was invalid in a previous invocation of claim_swap_neurons(). \
                        Skipping"
                        ),
                    ));
```

**File:** rs/sns/governance/src/governance.rs (L836-848)
```rust
    fn new_neuron_id(
        &mut self,
        controller: &PrincipalId,
        memo: u64,
    ) -> Result<NeuronId, GovernanceError> {
        let subaccount = ledger::compute_neuron_staking_subaccount_bytes(*controller, memo);
        let nid = NeuronId::from(subaccount);
        // Don't allow IDs that are already in use.
        if self.proto.neurons.contains_key(&nid.to_string()) {
            return Err(Self::invalid_subaccount_with_nonce(memo));
        }
        Ok(nid)
    }
```

**File:** rs/sns/governance/src/governance.rs (L4498-4505)
```rust
            // Skip this neuron if it was previously claimed.
            if self.proto.neurons.contains_key(&neuron_id.to_string()) {
                swap_neurons.push(SwapNeuron::from_neuron_recipe(
                    neuron_recipe,
                    ClaimedSwapNeuronStatus::AlreadyExists,
                ));
                continue;
            }
```

**File:** rs/sns/init/src/distributions.rs (L219-228)
```rust
        for (controller, memo) in deduped_dev_neurons.keys() {
            if NEURON_BASKET_MEMO_RANGE_START <= *memo && *memo <= SALE_NEURON_MEMO_RANGE_END {
                return Err(format!(
                    "Error: Developer neuron with controller {} cannot have a memo in the range {} to {}",
                    controller.unwrap(),
                    NEURON_BASKET_MEMO_RANGE_START,
                    SALE_NEURON_MEMO_RANGE_END
                ));
            }
        }
```

**File:** rs/sns/swap/src/types.rs (L921-928)
```rust
    pub fn set_claim_neuron_result(&mut self, claim_neuron_result: SweepResult) {
        if !claim_neuron_result.is_successful_sweep() {
            self.set_error_message(
                "Claiming SNS Neurons did not complete fully, some claims were invalid or failed. Halting swap finalization".to_string()
            );
        }
        self.claim_neuron_result = Some(claim_neuron_result);
    }
```

**File:** rs/sns/swap/src/types.rs (L1024-1034)
```rust
/// The mapping of ClaimedSwapNeuronStatus to ClaimedStatus
impl From<ClaimedSwapNeuronStatus> for ClaimedStatus {
    fn from(claimed_swap_neuron_status: ClaimedSwapNeuronStatus) -> Self {
        match claimed_swap_neuron_status {
            ClaimedSwapNeuronStatus::Success => ClaimedStatus::Success,
            ClaimedSwapNeuronStatus::Unspecified => ClaimedStatus::Failed,
            ClaimedSwapNeuronStatus::MemoryExhausted => ClaimedStatus::Failed,
            ClaimedSwapNeuronStatus::Invalid => ClaimedStatus::Invalid,
            ClaimedSwapNeuronStatus::AlreadyExists => ClaimedStatus::Invalid,
        }
    }
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L4670-4683)
```rust
    /// The Neuron was successfully created and added to Governance. Future
    /// attempts to claim the same Neuron will result in
    /// `ClaimedSwapNeuronStatus::AlreadyExists`.
    Success = 1,
    /// The Neuron could not be created because one or more of its
    /// construction parameters are invalid, i.e. its stake was not
    /// above the required minimum neuron stake. Additional retries will
    /// result in the same status.
    Invalid = 2,
    /// The neuron could not be created because it already exists
    /// within SNS Governance. Additional retries will result in
    /// the same status.
    AlreadyExists = 3,
    /// The Neuron could not be created because Governance has
```
