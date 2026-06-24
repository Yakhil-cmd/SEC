### Title
Missing Neuron Lock in SNS Governance `disburse_neuron` Enables Concurrent Stake Over-Drainage - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance `disburse_neuron` function makes two sequential async inter-canister calls to the ledger without first acquiring a per-neuron lock. During each `await` suspension point, the IC scheduler can interleave a second concurrent `disburse_neuron` call for the same neuron. Both calls read the same `cached_neuron_stake_e8s`, compute the same transfer amount, and both ledger transfers can succeed, draining more tokens from the neuron's subaccount than any single call was authorized to disburse.

### Finding Description

The NNS governance `disburse_neuron` acquires a `NeuronInFlightCommand` lock before any async ledger call:

```rust
// rs/nns/governance/src/governance.rs ~line 2031
let _neuron_lock = self.lock_neuron_for_command(
    id.id,
    NeuronInFlightCommand { timestamp: now, command: Some(InFlightCommand::Disburse(disburse.clone())) },
)?;
``` [1](#0-0) 

The SNS governance `disburse_neuron` has no equivalent lock. It performs authorization and state checks, then immediately issues two async ledger calls with no neuron-level guard:

```rust
// rs/sns/governance/src/governance.rs ~line 1181
// Transfer 1 – burn fees (async, no lock held)
let _result = self.ledger.transfer_funds(max_burnable_fee, 0, Some(from_subaccount), ...).await?;
// ... state update ...
// Transfer 2 – disburse stake (async, no lock held)
let block_height = self.ledger.transfer_funds(disburse_amount_e8s, transaction_fee_e8s, Some(from_subaccount), to_account, ...).await?;
``` [2](#0-1) 

The `lock_neuron_for_command` helper exists in the SNS governance and is used by `split_neuron`, `finalize_disburse_maturity`, and others, but is absent from `disburse_neuron`: [3](#0-2) 

The `in_flight_commands` map is the intended mechanism to prevent exactly this interleaving: [4](#0-3) 

### Impact Explanation

**Concrete attack scenario** (neuron with 100 SNS tokens, user calls `disburse(50)` twice concurrently):

1. Call A and Call B both enter `disburse_neuron`, both pass authorization and dissolved-state checks.
2. Both read `cached_neuron_stake_e8s = 100` and compute `disburse_amount = 50 − tx_fee`.
3. Call A suspends at `await` for Transfer 1 (fee burn); Call B is scheduled and also suspends at its Transfer 1 `await`.
4. Both fee-burn transfers complete; both calls proceed to Transfer 2.
5. Call A transfers `50 − tx_fee` tokens from the neuron subaccount (ledger balance: `50 + tx_fee`).
6. Call B transfers `50 − tx_fee` tokens (ledger balance: `2 × tx_fee`). This succeeds because `50 + tx_fee ≥ 50 − tx_fee`.
7. Call A updates `cached_neuron_stake_e8s`: `100 − 50 = 50`.
8. Call B updates `cached_neuron_stake_e8s`: `50 − 50 = 0`.

**Result**: The caller receives `2 × (50 − tx_fee) ≈ 100` tokens — the full neuron stake — while only one disburse of 50 was intended. The neuron's actual ledger balance (`2 × tx_fee`) diverges from `cached_neuron_stake_e8s` (0), corrupting governance accounting. [5](#0-4) 

### Likelihood Explanation

Any principal holding `NeuronPermissionType::Disburse` on a dissolved SNS neuron can trigger this. No privileged role, key compromise, or subnet-majority is required. The attacker simply submits two `manage_neuron { Disburse }` ingress messages in rapid succession before the first message's `await` completes. This is straightforward to automate with any IC agent library. The IC's asynchronous execution model guarantees the interleaving window exists at every `await` point. [6](#0-5) 

### Recommendation

Acquire a `NeuronInFlightCommand` lock immediately after the precondition checks and before the first async ledger call, mirroring the NNS governance pattern:

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

This ensures that a second concurrent call for the same neuron returns `NeuronLocked` immediately, preventing any interleaved ledger transfers. [3](#0-2) 

### Proof of Concept

**Entry path**: Unprivileged ingress sender who controls (or holds `Disburse` permission on) a dissolved SNS neuron.

**Steps**:
1. Obtain a dissolved SNS neuron with stake `S`.
2. Concurrently submit two `manage_neuron` ingress messages, each with `Disburse { amount: Some(S/2) }`.
3. Both messages pass the `check_authorized` and `NeuronState::Dissolved` checks before either reaches an `await`.
4. Both proceed through Transfer 1 (fee burn) and Transfer 2 (stake disburse) independently.
5. Observe that the caller's ledger account receives `≈ S` tokens (both transfers succeed) while the neuron's `cached_neuron_stake_e8s` is reduced to 0 — the full stake is drained by two half-stake disburse calls. [7](#0-6) [8](#0-7)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L1160-1172)
```rust
        let mut disburse_amount_e8s = disburse
            .amount
            .as_ref()
            .map_or(neuron.stake_e8s(), |a| a.e8s);

        // You cannot disburse more than the neuron's stake, which includes fees.
        disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s());

        // Subtract the transaction fee from the amount to disburse since it will
        // be deducted from the source (the neuron's) account.
        if disburse_amount_e8s > transaction_fee_e8s {
            disburse_amount_e8s -= transaction_fee_e8s
        }
```

**File:** rs/sns/governance/src/governance.rs (L1181-1236)
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

        // Transfer 2 - Disburse to the chosen account. This may fail if the
        // user told us to disburse more than they had in their account (but
        // the burn still happened).
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

        let nid = id.to_string();
        let neuron = self
            .proto
            .neurons
            .get_mut(&nid)
            .expect("Expected the parent neuron to exist");

        let to_deduct = disburse_amount_e8s + transaction_fee_e8s;
        // The transfer was successful we can change the stake of the neuron.
        neuron.cached_neuron_stake_e8s = neuron.cached_neuron_stake_e8s.saturating_sub(to_deduct);

        Ok(block_height)
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1471-1492)
```text
  // The in-flight neuron ledger commands as a map from neuron IDs
  // to commands.
  //
  // Whenever we change a neuron in a way that must not interleave
  // with another neuron change, we store the neuron and the issued
  // command in this map and remove it when the command is complete.
  //
  // An entry being present in this map acts like a "lock" on the neuron
  // and thus prevents concurrent changes that might happen due to the
  // interleaving of user requests and callback execution.
  //
  // If there are no ongoing requests, this map should be empty.
  //
  // If something goes fundamentally wrong (say we trap at some point
  // after issuing a transfer call) the neuron(s) involved are left in a
  // "locked" state, meaning new operations can't be applied without
  // reconciling the state.
  //
  // Because we know exactly what was going on, we should have the
  // information necessary to reconcile the state, using custom code
  // added on upgrade, if necessary.
  map<string, NeuronInFlightCommand> in_flight_commands = 10;
```
