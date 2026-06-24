### Title
SNS Neuron Owner Can Permanently Avoid Burning Rejected-Proposal Fees - (File: `rs/sns/governance/src/governance.rs`)

### Summary

In SNS governance, `neuron_fees_e8s` (the penalty tokens forfeited when a neuron's proposal is rejected) are only burned during `disburse_neuron()`. A neuron owner can permanently avoid this burn — and thus avoid the deflationary penalty — by never disbursing their neuron. No third party can force the burn, and no automatic timer exists to enforce it. This results in permanent SNS token supply inflation proportional to the unburned fees.

### Finding Description

When an SNS neuron submits a proposal that is rejected, `neuron_fees_e8s` is incremented on the neuron. The protocol comment explicitly states the intended behavior:

> "When a neuron is disbursed, these governance tokens will be burned." [1](#0-0) 

The burn is implemented exclusively inside `disburse_neuron()` in `rs/sns/governance/src/governance.rs`. The function first checks that the neuron is in `Dissolved` state, then conditionally burns `max_burnable_fee` (fees not tied to open proposals): [2](#0-1) [3](#0-2) 

There is no heartbeat, timer, or third-party callable function that burns `neuron_fees_e8s` independently of `disburse_neuron()`. The `maybe_finalize_disburse_maturity()` function handles maturity disbursements, not fee burning. [4](#0-3) 

The neuron owner controls whether and when to call `disburse_neuron()`. By keeping the neuron in `NotDissolving` or `Dissolving` state indefinitely (e.g., by repeatedly increasing the dissolve delay), the owner ensures `disburse_neuron()` can never be called, and the fees are never burned.

The DFINITY team acknowledges this design limitation with a TODO in the proto: [1](#0-0) 

### Impact Explanation

**Impact: Medium**

The unburned `neuron_fees_e8s` tokens remain in the neuron's subaccount on the SNS ledger indefinitely. Because the SNS token supply is not deflated by the expected burn, all other SNS token holders are diluted. The magnitude of the harm scales with the size of the accumulated fees. A large neuron that repeatedly submits rejected proposals and never disburses can cause measurable, permanent supply inflation. This is a ledger conservation bug: the on-chain token supply diverges from the intended economic model of the SNS.

### Likelihood Explanation

**Likelihood: Low**

The neuron owner's voting power is already reduced by `neuron_fees_e8s` (since `stake_e8s() = cached_neuron_stake_e8s - neuron_fees_e8s`), so there is no direct voting-power incentive to avoid disbursing. [5](#0-4) 

However, a malicious actor who wishes to harm an SNS community (e.g., a hostile governance participant or a competitor) could deliberately accumulate fees through rejected proposals and then lock the neuron indefinitely to prevent the deflationary burn. The cost to the attacker is the locked stake and reduced voting power; the benefit is persistent supply inflation harming all other token holders.

### Recommendation

Introduce an autonomous fee-burning mechanism that does not depend on the neuron owner calling `disburse_neuron()`. Options include:

1. **Burn fees immediately** when a proposal is rejected, rather than deferring to disburse time. This is referenced in the existing TODO (`NNS1-1052`).
2. **Allow any caller to trigger fee burning** for a neuron whose `neuron_fees_e8s` exceeds a threshold, similar to how `validateExpiredSnapshot()` was intended to work in the original report.
3. **Burn fees automatically on a heartbeat/timer** for neurons that have been in `Dissolved` state for longer than a grace period without disbursing.

### Proof of Concept

1. **Attacker** creates an SNS neuron with a large stake and a long dissolve delay (e.g., 8 years).
2. **Attacker** submits proposals that are voted down by the community. Each rejection increments `neuron_fees_e8s` by `reject_cost_e8s`.
3. **Attacker** never calls `disburse_neuron()`. Since the neuron is `NotDissolving`, `disburse_neuron()` would fail with `"Neuron is NOT dissolved"` anyway.
4. **No third party** can call any function to burn the fees — `disburse_neuron()` requires `NeuronPermissionType::Disburse` and `NeuronState::Dissolved`.
5. The fee tokens remain in the neuron's subaccount on the SNS ledger. The SNS token supply is permanently inflated by the accumulated `neuron_fees_e8s`, diluting all other token holders. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L114-120)
```text
  // TODO NNS1-1052 - Update if this ticket is done and fees are burned / minted instead of tracked in this attribute.
  //
  // The amount of governance tokens that this neuron has forfeited
  // due to making proposals that were subsequently rejected.
  // Must be smaller than 'cached_neuron_stake_e8s'. When a neuron is
  // disbursed, these governance tokens will be burned.
  uint64 neuron_fees_e8s = 4;
```

**File:** rs/sns/governance/src/governance.rs (L1119-1136)
```rust
    pub async fn disburse_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse: &manage_neuron::Disburse,
    ) -> Result<u64, GovernanceError> {
        // First check authorized
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::Disburse)?;

        // Check that the neuron is dissolved.
        let state = neuron.state(self.env.now());
        if state != NeuronState::Dissolved {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Neuron {id} is NOT dissolved. It is in state {state:?}"),
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L1181-1209)
```rust
        if max_burnable_fee > transaction_fee_e8s {
            let _result = self
                .ledger
                .transfer_funds(
                    max_burnable_fee,
                    0, // Burning transfers don't pay a fee.
                    Some(from_subaccount),
                    self.governance_minting_account(),
                    self.env.now(),
                )
                .await?;

            // We only update the cached_neuron_stake_e8s and neuron_fees_e8s if we actually
            // burn fees, otherwise this leads to ledger and governance getting out of sync.
            let nid = id.to_string();
            let neuron = self
                .proto
                .neurons
                .get_mut(&nid)
                .expect("Expected the parent neuron to exist");

            // Update the neuron's stake and management fees to reflect the burning
            // above.
            neuron.cached_neuron_stake_e8s = neuron
                .cached_neuron_stake_e8s
                .saturating_sub(max_burnable_fee);

            neuron.neuron_fees_e8s = neuron.neuron_fees_e8s.saturating_sub(max_burnable_fee);
        }
```

**File:** rs/sns/governance/src/governance.rs (L4920-4935)
```rust
    // Disburses any maturity that should be disbursed, unless this is already happening.
    async fn maybe_finalize_disburse_maturity(&mut self) {
        if !self.can_finalize_disburse_maturity() {
            return;
        }

        let maturity_modulation_basis_points =
            match self.proto.effective_maturity_modulation_basis_points() {
                Ok(maturity_modulation_basis_points) => maturity_modulation_basis_points,
                Err(message) => {
                    log!(ERROR, "{}", message.error_message);
                    return;
                }
            };

        self.proto.is_finalizing_disburse_maturity = Some(true);
```

**File:** rs/sns/governance/src/neuron.rs (L631-634)
```rust
    pub fn stake_e8s(&self) -> u64 {
        self.cached_neuron_stake_e8s
            .saturating_sub(self.neuron_fees_e8s)
    }
```

**File:** rs/sns/governance/src/governance/disburse_neuron_tests.rs (L352-374)
```rust
#[test]
fn test_disburse_neuron_with_dissolve_delay_fails() {
    // Test that disburse_neuron fails when neuron has a dissolve delay (locked)
    let (mut governance, neuron_id, ledger) =
        setup_disburse_neuron_test(DissolveState::DissolveDelaySeconds(1000), 1000);

    let disburse = manage_neuron::Disburse {
        amount: None,
        to_account: None,
    };

    let result = governance
        .disburse_neuron(&neuron_id, &A_NEURON_PRINCIPAL_ID, &disburse)
        .now_or_never()
        .unwrap();

    assert!(ledger.get_transfer_calls().is_empty());

    assert!(result.is_err());
    let error = result.unwrap_err();
    assert_eq!(error.error_type, ErrorType::PreconditionFailed as i32);
    assert!(error.error_message.contains("NOT dissolved"));
}
```
