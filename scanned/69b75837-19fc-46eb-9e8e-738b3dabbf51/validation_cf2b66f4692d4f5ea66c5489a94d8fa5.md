### Title
Missing Neuron Lock in SNS Governance `disburse_neuron` Allows Concurrent Calls to Double-Burn Management Fees - (File: rs/sns/governance/src/governance.rs)

---

### Summary

`SNS Governance::disburse_neuron` performs two sequential inter-canister `await` calls to the SNS ledger (fee burn, then stake disburse) without first acquiring a per-neuron reentrancy lock. Because the IC processes other ingress messages between `await` suspension points, two concurrent `Disburse` commands on the same dissolved neuron can interleave, causing the neuron's management fees to be burned twice from the on-chain ledger subaccount while the governance accounting is left in an inconsistent state that permanently prevents the neuron owner from recovering their full stake.

---

### Finding Description

`SNS Governance::disburse_neuron` at `rs/sns/governance/src/governance.rs` lines 1119–1237 is an `async fn` that makes two sequential `await` calls to the ledger:

1. **Transfer 1** – burn `max_burnable_fee` from the neuron's subaccount to the minting account.
2. **Transfer 2** – transfer `disburse_amount_e8s` from the neuron's subaccount to the caller's account.

Both `max_burnable_fee` and `disburse_amount_e8s` are computed **before** any `await`, from the neuron's state at call entry. The governance state (`cached_neuron_stake_e8s`, `neuron_fees_e8s`) is updated **after** each `await` returns.

Critically, **no per-neuron lock is acquired** before the first `await`. Compare with the NNS governance counterpart at `rs/nns/governance/src/governance.rs` lines 2031–2037, which calls `lock_neuron_for_command` before any ledger interaction. [1](#0-0) [2](#0-1) [3](#0-2) 

The NNS version that correctly acquires the lock: [4](#0-3) 

Because the IC canister execution model allows other ingress messages to be processed between `await` suspension points, two concurrent `Disburse` calls on the same neuron can interleave as follows:

```
Call C1: reads stake=S, fees=F, max_burnable_fee=F, disburse_amount=S-F-tx_fee
C1 suspends at: await burn(F)          ← IC processes other messages here
Call C2: reads stake=S, fees=F, max_burnable_fee=F, disburse_amount=S-F-tx_fee
C2 suspends at: await burn(F)
C1 resumes: burn(F) succeeded → updates stake=S-F, fees=0
C2 resumes: burn(F) succeeded (if S-F ≥ F) → updates stake=S-2F, fees=0 (already 0)
C1 suspends at: await transfer(S-F-tx_fee)
C2 suspends at: await transfer(S-F-tx_fee)
C1 resumes: transfer fails (ledger balance S-2F < S-F-tx_fee when F > tx_fee)
C2 resumes: transfer fails (same reason)
```

Result: `2F` has been burned from the neuron's on-chain subaccount, but the neuron owner received nothing. The governance `cached_neuron_stake_e8s` is now `S-2F`, which matches the ledger balance, but the owner has permanently lost `F` in fees that were burned twice.

---

### Impact Explanation

A dissolved SNS neuron owner with `neuron_fees_e8s > transaction_fee_e8s` loses funds equal to one full round of management fees (`max_burnable_fee`) that are burned a second time from their neuron's ledger subaccount. Both `Disburse` calls fail to transfer the stake, so the owner also temporarily cannot disburse until they retry with a reduced amount. The governance `cached_neuron_stake_e8s` ends up consistent with the ledger (both show `S-2F`), but the owner has suffered an irreversible loss of `F` tokens. This is a **ledger conservation bug**: tokens are destroyed without corresponding governance credit.

---

### Likelihood Explanation

Any principal holding `DisburseMaturity` or `Disburse` permission on a dissolved SNS neuron with non-trivial management fees can trigger this by submitting two `manage_neuron { command: Disburse }` ingress messages in rapid succession. No privileged access, governance majority, or threshold corruption is required. The IC boundary node accepts concurrent ingress messages from the same sender, and the two messages will be placed in the canister's input queue and processed in separate rounds, with the first `await` in round N and the second message starting in round N (between the two awaits of the first). This is a realistic user-level action.

---

### Recommendation

Acquire a per-neuron lock before the first `await` in `SNS Governance::disburse_neuron`, mirroring the NNS governance pattern:

```rust
let _neuron_lock = self.lock_neuron_for_command(
    id,
    NeuronInFlightCommand { ... },
)?;
```

The lock must be held across both `await` points and released only when the function returns (via `Drop`). This is exactly the pattern used in `rs/nns/governance/src/governance.rs` at lines 2031–2037. [5](#0-4) 

---

### Proof of Concept

1. Deploy an SNS with a neuron that has `neuron_fees_e8s = F` where `F > transaction_fee_e8s` and `cached_neuron_stake_e8s = S` where `S >= 2F`. The neuron must be in `Dissolved` state.
2. As the neuron controller, submit two concurrent ingress messages to SNS governance `manage_neuron` with `command: Disburse { amount: None, to_account: None }`.
3. The IC will process both messages. The first message runs until `await burn(F)` and suspends. The second message starts, reads the same pre-burn state, and also issues `await burn(F)`.
4. Both fee burns succeed on the ledger (total `2F` burned from the neuron subaccount).
5. Both subsequent disburse transfers fail because the ledger balance `S-2F` is less than the requested `S-F-tx_fee` (since `F > tx_fee`).
6. Observe: the neuron owner received `0` tokens but `2F` tokens were burned. The neuron's governance state shows `cached_neuron_stake_e8s = S-2F`, `neuron_fees_e8s = 0`. The owner has lost `F` tokens permanently. [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1119-1125)
```rust
    pub async fn disburse_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse: &manage_neuron::Disburse,
    ) -> Result<u64, GovernanceError> {
        // First check authorized
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

**File:** rs/sns/governance/src/governance.rs (L1214-1234)
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

        let nid = id.to_string();
        let neuron = self
            .proto
            .neurons
            .get_mut(&nid)
            .expect("Expected the parent neuron to exist");

        let to_deduct = disburse_amount_e8s + transaction_fee_e8s;
        // The transfer was successful we can change the stake of the neuron.
        neuron.cached_neuron_stake_e8s = neuron.cached_neuron_stake_e8s.saturating_sub(to_deduct);
```

**File:** rs/nns/governance/src/governance.rs (L2029-2064)
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

        // We need to do 2 transfers:
        // 1 - Burn the neuron management fees.
        // 2 - Transfer the the disbursed amount to the target account

        // Transfer 1 - burn the fees, but only if the value exceeds the cost of
        // a transaction fee, as the ledger doesn't support burn transfers for
        // an amount less than the transaction fee.
        if fees_amount_e8s > transaction_fee_e8s {
            let now = self.env.now();
            tla_log_label!("DisburseNeuron_Fee");
            tla_log_locals! {
                fees_amount: fees_amount_e8s,
                neuron_id: id.id,
                to_account: tla::account_to_tla(to_account),
                disburse_amount: disburse_amount_e8s
            };
            let _result = self
                .ledger
                .transfer_funds(
                    fees_amount_e8s,
                    0, // Burning transfers don't pay a fee.
                    Some(neuron_subaccount),
                    governance_minting_account(),
                    now,
                )
                .await?;
```
