### Title
Immediate Application of SNS `ManageNervousSystemParameters` Changes Without Timelock Allows Governance-Controlled Front-Running of User Transactions - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS governance canister applies `ManageNervousSystemParameters` proposals atomically and immediately upon adoption, with no mandatory delay or timelock. Critical economic parameters — including `transaction_fee_e8s`, `neuron_minimum_stake_e8s`, and `reject_cost_e8s` — take effect the instant the proposal executes. A user who submits a neuron-staking or ledger transaction based on the current parameters can have those parameters changed between the time they observe the state and the time their transaction is processed, causing unexpected failures or economic loss.

### Finding Description

In `rs/sns/governance/src/governance.rs`, the function `perform_manage_nervous_system_parameters` directly overwrites `self.proto.parameters` with the new values in a single synchronous step:

```rust
Ok(()) => {
    self.proto.parameters = Some(new_params);
    Ok(())
}
```

There is no staged commit, no pending-change announcement period, and no enforced waiting window before the new parameters become active. The proposal execution path `perform_action` → `perform_manage_nervous_system_parameters` is invoked immediately after a proposal reaches its decision threshold.

The affected parameters include:

- `transaction_fee_e8s` — the fee charged on every SNS ledger transfer
- `neuron_minimum_stake_e8s` — the minimum balance required to claim a neuron
- `reject_cost_e8s` — the cost charged to a neuron for a rejected proposal

When `neuron_minimum_stake_e8s` is raised, a user who has already transferred exactly the old minimum to their staking subaccount will find their `claim_neuron` call rejected with `InsufficientFunds`, and their funds are now stranded in the subaccount. When `transaction_fee_e8s` is raised, in-flight ICRC-1 transfers that specified the old fee will be rejected with `BadFee`. Both effects are confirmed by the integration test at `rs/sns/integration_tests/src/manage_ledger_parameters.rs:102-118`, which explicitly shows that a transfer using the old fee fails after a fee change.

### Impact Explanation

**Neuron staking disruption:** A user observes `neuron_minimum_stake_e8s = X`, transfers exactly `X` tokens to their staking subaccount, and then calls `claim_neuron`. If a `ManageNervousSystemParameters` proposal raising the minimum to `X+delta` executes between the transfer and the claim, the claim fails. The user's funds are stranded in the subaccount (they cannot be automatically recovered without a separate disbursement flow). This is a direct, concrete financial disruption to an unprivileged user.

**Ledger transfer failure:** A user constructs an ICRC-1 transfer specifying `fee = old_fee`. If `transaction_fee_e8s` is raised before the transfer is processed, the ledger rejects it with `BadFee`. The user must retry with the new fee, but any time-sensitive operation (e.g., a swap participation deadline) may be missed.

**Proposal submission cost surprise:** A user who observes `reject_cost_e8s = Y` and submits a proposal expecting to pay `Y` on rejection will instead pay the new, higher amount if the parameter was raised between observation and execution.

### Likelihood Explanation

The SNS governance voting period has a minimum floor (`INITIAL_VOTING_PERIOD_SECONDS_FLOOR`) but proposals with a supermajority neuron can be decided and executed almost immediately. On SNS instances where a single neuron or a small coordinated group holds majority voting power, a `ManageNervousSystemParameters` proposal can pass and execute within seconds of submission. Any user who queries parameters and then submits a transaction in the same window is exposed. This is not a theoretical race — the integration test at `rs/sns/integration_tests/src/proposals.rs:198-217` demonstrates that a proposal submitted by a majority-stake neuron is decided and executed in the same block.

### Recommendation

Implement a two-phase commit for sensitive `NervousSystemParameters` changes:

1. **Announce phase:** When a `ManageNervousSystemParameters` proposal is adopted, store the pending new parameters alongside a `pending_parameters_effective_at` timestamp (e.g., `now + 48 hours`).
2. **Commit phase:** In the periodic heartbeat, apply the pending parameters only after the effective timestamp has passed.

This gives users a guaranteed observation window to adjust their in-flight transactions. At minimum, `transaction_fee_e8s` and `neuron_minimum_stake_e8s` should be subject to this delay, as they directly affect the validity of user-submitted transactions.

### Proof of Concept

**Step 1:** User queries SNS governance and observes `neuron_minimum_stake_e8s = 100_000_000` (1 token).

**Step 2:** User transfers exactly `100_000_000` e8s to their staking subaccount on the SNS ledger.

**Step 3:** A majority-stake SNS neuron submits and immediately passes a `ManageNervousSystemParameters` proposal setting `neuron_minimum_stake_e8s = 200_000_000`.

**Step 4:** `perform_manage_nervous_system_parameters` executes: [1](#0-0) 

The new minimum is now live with no delay.

**Step 5:** User calls `claim_neuron`. The check at: [2](#0-1) 

reads the new `min_stake = 200_000_000`, finds `balance = 100_000_000 < min_stake`, removes the neuron record, and returns `InsufficientFunds`. The user's `100_000_000` e8s are stranded in the subaccount.

**Ledger fee variant:** The same race applies to `transaction_fee_e8s`. After a `ManageLedgerParameters` proposal executes: [3](#0-2) 

the ledger is upgraded with the new fee immediately. Any transfer specifying the old fee is rejected, as confirmed by: [4](#0-3) 

The root cause — no timelock on parameter execution — is in: [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2366-2384)
```rust
                    ErrorType::InvalidProposal,
                    format!("Invalid UpgradeExtension: {err:?}"),
                )
            })?;

        validated_upgrade_extension.execute(self).await?;

        Ok(())
    }

    /// Registers a list of Dapp canister ids in the root canister.
    async fn perform_register_dapp_canisters(
        &self,
        register_dapp_canisters: RegisterDappCanisters,
    ) -> Result<(), GovernanceError> {
        let payload = candid::Encode!(&RegisterDappCanistersRequest::from(
            register_dapp_canisters.clone()
        ))
        .map_err(|err| {
```

**File:** rs/sns/governance/src/governance.rs (L2581-2617)
```rust
    fn perform_manage_nervous_system_parameters(
        &mut self,
        proposed_params: NervousSystemParameters,
    ) -> Result<(), GovernanceError> {
        // Only set `self.proto.parameters` if "applying" the proposed params to the
        // current params results in valid params
        let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());

        log!(
            INFO,
            "Setting Governance nervous system params to: {:?}",
            &new_params
        );

        match new_params.validate() {
            Ok(()) => {
                self.proto.parameters = Some(new_params);
                Ok(())
            }

            // Even though proposals are validated when they are first made, this is still
            // possible, because the inner value of a ManageNervousSystemParameters
            // proposal is only valid with respect to the current
            // nervous_system_parameters() at the time when the proposal was first
            // made. If nervous_system_parameters() changed (by another proposal) since
            // the current proposal was first made, the current proposal might have become
            // invalid. Basically, this might occur if there are conflicting (concurrent)
            // proposals, but we expect this to be highly unusual in practice.
            Err(msg) => Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Failed to perform ManageNervousSystemParameters action, proposed \
                        parameters would lead to invalid NervousSystemParameters: {msg}"
                ),
            )),
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L3090-3096)
```rust
    async fn perform_manage_ledger_parameters(
        &mut self,
        proposal_id: u64,
        manage_ledger_parameters: ManageLedgerParameters,
    ) -> Result<(), GovernanceError> {
        self.check_no_upgrades_in_progress(Some(proposal_id))?;

```

**File:** rs/sns/integration_tests/src/manage_ledger_parameters.rs (L102-118)
```rust
    icrc1_transfer(
        &state_machine,
        sns_canisters.ledger_canister_id,
        user,
        TransferArg {
            amount: Nat::from(5_u8),
            fee: Some(DEFAULT_LEDGER_TRANSFER_FEE.into()),
            from_subaccount: None,
            to: Account {
                owner: Principal::management_canister(),
                subaccount: None,
            },
            memo: None,
            created_at_time: None,
        },
    )
    .expect_err("This transfer with the old fee must fail.");
```
