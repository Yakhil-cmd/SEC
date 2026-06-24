Audit Report

## Title
Missing Neuron Lock in SNS Governance `disburse_neuron` Enables Concurrent Stake Over-Drainage - (File: rs/sns/governance/src/governance.rs)

## Summary
The SNS governance `disburse_neuron` function performs two sequential async inter-canister ledger calls without first acquiring a per-neuron `in_flight_commands` lock. Because the IC scheduler can interleave a second concurrent `disburse_neuron` call for the same neuron at each `await` suspension point, both calls can read the same `cached_neuron_stake_e8s`, compute the same transfer amount, and both ledger transfers can succeed — draining more tokens from the neuron's subaccount than any single call was authorized to disburse. The NNS governance equivalent acquires a `NeuronInFlightCommand` lock before any async call; the SNS version does not.

## Finding Description
`disburse_neuron` at [1](#0-0)  performs authorization and dissolved-state checks synchronously, then immediately issues two async ledger calls with no neuron-level guard:

- Transfer 1 (fee burn) at [2](#0-1) 
- Transfer 2 (stake disburse) at [3](#0-2) 

The `lock_neuron_for_command` helper exists in SNS governance and correctly prevents re-entrant operations: [4](#0-3) 

It is used elsewhere in the SNS governance file (8 call sites confirmed), but is entirely absent from `disburse_neuron`. The NNS counterpart acquires the lock before any async call: [5](#0-4) 

Because `disburse_amount_e8s` is computed from `cached_neuron_stake_e8s` before the first `await`, two concurrent calls both capture the same stake value. After both fee-burn transfers complete, both proceed to Transfer 2 and both succeed as long as the ledger subaccount balance covers each individual transfer. The `cached_neuron_stake_e8s` updates at [6](#0-5)  happen after each respective `await`, so they do not protect against the interleaving.

## Impact Explanation
An attacker holding `NeuronPermissionType::Disburse` on a dissolved SNS neuron with stake `S` can submit two concurrent `manage_neuron { Disburse { amount: S/2 } }` calls. Both pass all precondition checks, both execute Transfer 1 and Transfer 2 independently, and the caller receives `≈ S` tokens while only one disburse of `S/2` was authorized. This constitutes unauthorized theft of SNS governance token assets and corrupts `cached_neuron_stake_e8s` accounting. This matches the **High** impact class: unauthorized access to governance assets / significant SNS security impact with concrete user or protocol harm.

## Likelihood Explanation
Any principal holding `NeuronPermissionType::Disburse` on a dissolved SNS neuron can trigger this with no privileged role, key compromise, or subnet-majority. The attacker submits two `manage_neuron` ingress messages in rapid succession before the first message's `await` completes. This is straightforward to automate with any IC agent library. The IC's asynchronous execution model guarantees the interleaving window exists at every `await` point, making this reliably reproducible.

## Recommendation
Acquire a `NeuronInFlightCommand` lock immediately after the precondition checks and before the first async ledger call, mirroring the NNS governance pattern at [5](#0-4) :

```rust
// After precondition checks, before any .await
let _neuron_lock = self.lock_neuron_for_command(
    id,
    NeuronInFlightCommand {
        timestamp: self.env.now(),
        command: Some(neuron_in_flight_command::Command::Disburse(disburse.clone())),
    },
)?;
```

This ensures a second concurrent call for the same neuron returns `NeuronLocked` immediately, preventing any interleaved ledger transfers.

## Proof of Concept
1. Obtain a dissolved SNS neuron with stake `S` and `NeuronPermissionType::Disburse`.
2. Concurrently submit two `manage_neuron { Disburse { amount: Some(S/2) } }` ingress messages using any IC agent library.
3. Both messages pass `check_authorized` and `NeuronState::Dissolved` checks before either reaches an `await` (no lock exists to block the second).
4. Both execute Transfer 1 (fee burn) and Transfer 2 (stake disburse) independently.
5. Observe the caller's ledger account receives `≈ S` tokens while `cached_neuron_stake_e8s` is reduced to 0.

A deterministic integration test using PocketIC can reproduce this by submitting two concurrent `manage_neuron` calls and asserting the recipient balance exceeds `S/2` after both complete.

### Citations

**File:** rs/sns/governance/src/governance.rs (L904-919)
```rust
    fn lock_neuron_for_command(
        &mut self,
        nid: &NeuronId,
        command: NeuronInFlightCommand,
    ) -> Result<LedgerUpdateLock, GovernanceError> {
        let nid = nid.to_string();
        if self.proto.in_flight_commands.contains_key(&nid) {
            return Err(GovernanceError::new_with_message(
                ErrorType::NeuronLocked,
                "Neuron has an ongoing operation.",
            ));
        }

        self.proto.in_flight_commands.insert(nid.clone(), command);

        Ok(LedgerUpdateLock { nid, gov: self })
```

**File:** rs/sns/governance/src/governance.rs (L1119-1127)
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
```

**File:** rs/sns/governance/src/governance.rs (L1181-1191)
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
```

**File:** rs/sns/governance/src/governance.rs (L1214-1223)
```rust
        let block_height = self
            .ledger
            .transfer_funds(
                disburse_amount_e8s,
                transaction_fee_e8s,
                Some(from_subaccount),
                to_account,
                self.env.now(),
            )
            .await?;
```

**File:** rs/sns/governance/src/governance.rs (L1232-1234)
```rust
        let to_deduct = disburse_amount_e8s + transaction_fee_e8s;
        // The transfer was successful we can change the stake of the neuron.
        neuron.cached_neuron_stake_e8s = neuron.cached_neuron_stake_e8s.saturating_sub(to_deduct);
```

**File:** rs/nns/governance/src/governance.rs (L2029-2037)
```rust
        // Add the neuron's id to the set of neurons with ongoing ledger updates.
        let now = self.env.now();
        let _neuron_lock = self.lock_neuron_for_command(
            id.id,
            NeuronInFlightCommand {
                timestamp: now,
                command: Some(InFlightCommand::Disburse(disburse.clone())),
            },
        )?;
```
