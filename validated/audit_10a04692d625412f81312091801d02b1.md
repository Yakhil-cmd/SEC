Audit Report

## Title
Missing Parent Neuron Lock and Unchecked Subtraction in SNS `split_neuron` Enables Stake Inflation via Concurrent Splits - (File: rs/sns/governance/src/governance.rs)

## Summary
The SNS `split_neuron` function locks only the child neuron before the inter-canister ledger `await`, leaving the parent neuron unlocked and modifiable by concurrent ingress messages. A second `split_neuron` call on the same parent, interleaved during the first call's await, can reduce the parent's `cached_neuron_stake_e8s` below the first split's amount. The subsequent plain `-=` subtraction at line 1429 then wraps to near `u64::MAX` in release-mode Wasm, inflating the parent neuron's cached stake and voting power to near-maximum, enabling the attacker to unilaterally pass any SNS governance proposal.

## Finding Description

**Root cause — missing parent lock:**

In `rs/sns/governance/src/governance.rs`, `split_neuron` acquires a lock only for the child neuron:

```rust
// line 1388
let _child_lock = self.lock_neuron_for_command(&child_nid, in_flight_command)?;
``` [1](#0-0) 

The parent neuron `id` is never inserted into `proto.in_flight_commands`, so any concurrent operation on the parent proceeds unimpeded during the ledger await at lines 1397–1406. [2](#0-1) 

**Contrast with NNS:**

The NNS `split_neuron` explicitly locks the parent before the await and uses `checked_sub`:

```rust
// rs/nns/governance/src/governance.rs line 2233
let _parent_lock = self.lock_neuron_for_command(id.id, in_flight_command.clone())?;
// ...
parent_neuron.cached_neuron_stake_e8s = parent_neuron
    .cached_neuron_stake_e8s
    .checked_sub(split_amount_e8s)
    .expect("Subtracting neuron stake underflows");
``` [3](#0-2) 

**Exploit flow:**

1. Attacker owns a neuron with `cached_neuron_stake_e8s = S` where `S ≥ 3 × min_stake`.
2. Attacker sends two concurrent `split_neuron` calls, each with `amount_e8s = A` where `A = S − min_stake`. Both pass the pre-check at line 1333 (`S ≥ min_stake + A`), since neither has yet modified the parent's cached stake. [4](#0-3) 
3. Both calls reach the ledger `await`. The IC scheduler interleaves them: Call 1 awaits, Call 2 executes its pre-check (still sees `S`), then also awaits.
4. Call 1's callback resumes first: `parent.cached_neuron_stake_e8s = S − A = min_stake`. This is valid.
5. Call 2's callback resumes: `parent.cached_neuron_stake_e8s -= A` → `min_stake − A` → unsigned integer wrap → `≈ u64::MAX`. [5](#0-4) 

**Why existing checks fail:**

The stake check at line 1333 is a TOCTOU guard — it reads a snapshot of the parent neuron before the await but does not hold a lock to prevent concurrent modification. The `lock_neuron_for_command` mechanism at line 910 only blocks operations that themselves attempt to acquire a lock on the same neuron ID; since the parent is never locked, a second `split_neuron` call on the same parent proceeds freely. [6](#0-5) 

**Voting power impact:**

`voting_power_stake_e8s()` is computed as `cached_neuron_stake_e8s.saturating_sub(neuron_fees_e8s)`. With `cached_neuron_stake_e8s ≈ u64::MAX`, this returns `≈ u64::MAX`, and `voting_power()` caps the final result at `u64::MAX`, giving the attacker's neuron near-maximum voting power in the SNS. [7](#0-6) [8](#0-7) 

## Impact Explanation

**High ($2,000–$10,000) — Significant SNS governance security impact with concrete user and protocol harm.**

An attacker who inflates their neuron's `cached_neuron_stake_e8s` to near `u64::MAX` gains near-total voting power over the affected SNS instance. They can unilaterally pass any proposal — including proposals that transfer the entire SNS treasury to an attacker-controlled account, upgrade SNS canisters with malicious code, or dissolve the SNS entirely. Every SNS instance deployed on the IC is independently affected. The impact is bounded to the individual SNS (not the NNS or ICP ledger directly), placing this in the High tier, but it can escalate to Critical if the SNS treasury holds assets exceeding $1M.

## Likelihood Explanation

Any principal who holds a neuron in an SNS instance with sufficient stake (≥ 3× `neuron_minimum_stake_e8s`) can trigger this. No special privileges, social engineering, or external dependencies are required. The attacker simply submits two `manage_neuron` Split commands in rapid succession via the SNS governance canister's public `manage_neuron` endpoint. The IC's cooperative scheduling guarantees that the second message is processed during the first's ledger await. The attack is deterministic, repeatable, and requires no timing luck beyond submitting both messages before the first callback returns.

## Recommendation

1. **Lock the parent neuron** before the ledger await, mirroring the NNS implementation:
   ```rust
   let _parent_lock = self.lock_neuron_for_command(id, in_flight_command.clone())?;
   ```
2. **Replace the plain `-=` with `checked_sub`** (or `saturating_sub`) at line 1429:
   ```rust
   parent_neuron.cached_neuron_stake_e8s = parent_neuron
       .cached_neuron_stake_e8s
       .checked_sub(split.amount_e8s)
       .expect("Subtracting neuron stake underflows");
   ```
   Both fixes together eliminate the TOCTOU window and the silent underflow.

## Proof of Concept

**Minimal unit test plan (using the existing `TestLedger` pattern in `assorted_governance_tests.rs`):**

1. Create a governance instance with a parent neuron having `cached_neuron_stake_e8s = 3 × min_stake`.
2. Use a `TestLedger` that signals arrival and waits for a `continue` notification (as already done in the file for disburse interleaving tests).
3. Spawn two concurrent `split_neuron` futures, each with `amount_e8s = 2 × min_stake`.
4. Let both futures reach the ledger await (both will signal `transfer_funds_arrived`).
5. Release both ledger calls.
6. Assert that `parent_neuron.cached_neuron_stake_e8s` has NOT wrapped to near `u64::MAX` (the test will currently fail, demonstrating the bug). [9](#0-8)

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

**File:** rs/sns/governance/src/governance.rs (L1333-1347)
```rust
        if parent_neuron.stake_e8s() < min_stake + split.amount_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Trying to split {} e8s out of neuron {}. \
                     This is not allowed, because the parent has stake {} e8s. \
                     If the requested amount was subtracted from it, there would be less than \
                     the minimum allowed stake, which is {} e8s. ",
                    split.amount_e8s,
                    parent_nid,
                    parent_neuron.stake_e8s(),
                    min_stake
                ),
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L1383-1388)
```rust
        // Add the child neuron's id to the set of neurons with ongoing operations.
        let in_flight_command = NeuronInFlightCommand {
            timestamp: creation_timestamp_seconds,
            command: Some(InFlightCommand::Split(*split)),
        };
        let _child_lock = self.lock_neuron_for_command(&child_nid, in_flight_command)?;
```

**File:** rs/sns/governance/src/governance.rs (L1397-1406)
```rust
        let result: Result<u64, NervousSystemError> = self
            .ledger
            .transfer_funds(
                staked_amount,
                transaction_fee_e8s,
                Some(from_subaccount),
                self.neuron_account_id(to_subaccount),
                split.memo,
            )
            .await;
```

**File:** rs/sns/governance/src/governance.rs (L1424-1429)
```rust
        // Get the neuron again, but this time a mutable reference.
        // Expect it to exist, since we acquired a lock above.
        let parent_neuron = self.get_neuron_result_mut(id).expect("Neuron not found");

        // Update the state of the parent and child neuron.
        parent_neuron.cached_neuron_stake_e8s -= split.amount_e8s;
```

**File:** rs/nns/governance/src/governance.rs (L2233-2273)
```rust
        let _parent_lock = self.lock_neuron_for_command(id.id, in_flight_command.clone())?;

        // Before we do the transfer, we need to save the neuron in the map
        // otherwise a trap after the transfer is successful but before this
        // method finishes would cause the funds to be lost.
        // However the new neuron is not yet ready to be used as we can't know
        // whether the transfer will succeed, so we temporarily set the
        // stake to 0 and only change it after the transfer is successful.
        let child_neuron = NeuronBuilder::new(
            child_nid,
            to_subaccount,
            *caller,
            parent_neuron.dissolve_state_and_age(),
            created_timestamp_seconds,
        )
        .with_hot_keys(parent_neuron.hot_keys.clone())
        .with_followees(parent_neuron.followees.clone())
        .with_kyc_verified(parent_neuron.kyc_verified)
        .with_auto_stake_maturity(parent_neuron.auto_stake_maturity.unwrap_or(false))
        .with_not_for_profit(parent_neuron.not_for_profit)
        .with_joined_community_fund_timestamp_seconds(
            parent_neuron.joined_community_fund_timestamp_seconds,
        )
        .with_neuron_type(parent_neuron.neuron_type)
        .build();

        // Add the child neuron to the set of neurons undergoing ledger updates.
        let _child_lock = self.lock_neuron_for_command(child_nid.id, in_flight_command.clone())?;

        // We need to add the "embryo neuron" to the governance proto only after
        // acquiring the lock. Indeed, in case there is already a pending
        // command, we return without state rollback. If we had already created
        // the embryo, it would not be garbage collected.
        self.add_neuron(child_nid.id, child_neuron.clone())?;

        // Do the transfer for the parent first, to avoid double spending.
        self.neuron_store.with_neuron_mut(id, |parent_neuron| {
            parent_neuron.cached_neuron_stake_e8s = parent_neuron
                .cached_neuron_stake_e8s
                .checked_sub(split_amount_e8s)
                .expect("Subtracting neuron stake underflows");
```

**File:** rs/sns/governance/src/neuron.rs (L248-251)
```rust
        // dissolve delay, and voting power multiplier. If the stake is is greater than
        // u64::MAX divided by 2.5, the voting power may actually not
        // fit in a u64.
        std::cmp::min(vad_stake, u64::MAX as u128) as u64
```

**File:** rs/sns/governance/src/neuron.rs (L641-644)
```rust
    fn voting_power_stake_e8s(&self) -> u64 {
        self.cached_neuron_stake_e8s
            .saturating_sub(self.neuron_fees_e8s)
            .saturating_add(self.staked_maturity_e8s_equivalent.unwrap_or(0))
```

**File:** rs/sns/governance/src/governance/assorted_governance_tests.rs (L362-421)
```rust
        .run_until(async move {
            // Step 1: Prepare the world.
            let user = UserInfo::new(Sender::from_keypair(&TEST_USER1_KEYPAIR));
            let principal_id = user.sender.get_principal_id();
            // work around the fact that the type inside UserInfo is not the same as the type in this crate
            let neuron_id = crate::pb::v1::NeuronId {
                id: user.subaccount.to_vec(),
            };

            let mut governance_proto = basic_governance_proto();

            // Step 1.1: Add a neuron (so that we can operate on it).
            governance_proto.neurons.insert(
                neuron_id.to_string(),
                Neuron {
                    id: Some(neuron_id.clone()),
                    cached_neuron_stake_e8s: 10_000,
                    permissions: vec![NeuronPermission {
                        principal: Some(principal_id),
                        permission_type: NeuronPermissionType::all(),
                    }],
                    ..Default::default()
                },
            );

            // Lets us know that a transfer is in progress.
            let transfer_funds_arrived = Arc::new(tokio::sync::Notify::new());

            // Lets us tell ledger that it can proceed with the transfer.
            let transfer_funds_continue = Arc::new(tokio::sync::Notify::new());

            // Step 1.3: Create Governance that we will be sending manage_neuron calls to.
            let mut governance = Governance::new(
                ValidGovernanceProto::try_from(governance_proto).unwrap(),
                Box::<NativeEnvironment>::default(),
                Box::new(TestLedger {
                    transfer_funds_arrived: transfer_funds_arrived.clone(),
                    transfer_funds_continue: transfer_funds_continue.clone(),
                }),
                Box::new(DoNothingLedger {}),
                Box::new(FakeCmc::new()),
            );

            // Step 2: Execute code under test.

            // This lets us (later) make a second manage_neuron method call
            // while one is in flight, which is essential for this test.
            let raw_governance = &mut governance as *mut Governance;

            // Step 2.1: Begin an async that is supposed to interfere with a
            // later manage_neuron call.
            let disburse = ManageNeuron {
                subaccount: user.subaccount.to_vec(),
                command: Some(manage_neuron::Command::Disburse(manage_neuron::Disburse {
                    amount: None,
                    to_account: Some(AccountProto {
                        owner: Some(user.sender.get_principal_id()),
                        subaccount: None,
                    }),
                })),
```
