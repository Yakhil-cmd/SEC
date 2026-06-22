### Title
User-Provided Nonce in NNS Governance `spawn` Enables Subaccount Griefing - (File: rs/nns/governance/src/governance.rs)

### Summary
The NNS Governance `spawn` operation accepts a caller-supplied `nonce` to deterministically compute the child neuron's subaccount via `compute_neuron_staking_subaccount(child_controller, nonce)`. Because `claim_or_refresh_neuron_by_memo_and_controller` uses the **same hash domain** and accepts an arbitrary `controller` principal from any caller, an attacker who knows the intended `(child_controller, nonce)` pair can preemptively stake a neuron at that subaccount, causing the victim's `spawn` to fail with `SubaccountAlreadyExists`.

### Finding Description
When `spawn` is called with an explicit nonce, the child neuron's subaccount is computed and then checked for availability:

```rust
// rs/nns/governance/src/governance.rs ~2682-2687
Some(nonce_val) => {
    let to_subaccount =
        ledger::compute_neuron_staking_subaccount(child_controller, nonce_val);
    self.neuron_store
        .ensure_subaccount_available(to_subaccount)?
}
``` [1](#0-0) 

`ensure_subaccount_available` fails immediately on collision because retrying would produce the same result:

```rust
// rs/nns/governance/src/neuron_store.rs ~488-495
pub fn ensure_subaccount_available(
    &self,
    subaccount: Subaccount,
) -> Result<Subaccount, NeuronStoreError> {
    if self.has_neuron_with_subaccount(subaccount) {
        return Err(NeuronStoreError::SubaccountAlreadyExists { subaccount });
    }
    Ok(subaccount)
}
``` [2](#0-1) 

The public `claim_or_refresh_neuron_by_memo_and_controller` endpoint uses the **identical** `compute_neuron_staking_subaccount` call (domain `"neuron-stake"`) and accepts any `controller` principal — it does not require the caller to be that controller:

```rust
// rs/nns/governance/src/governance.rs ~5858-5860
let controller = memo_and_controller.controller.unwrap_or(*caller);
let memo = memo_and_controller.memo;
let subaccount = ledger::compute_neuron_staking_subaccount(controller, memo);
``` [3](#0-2) 

Both paths hash with the same domain:

```rust
// rs/nervous_system/common/src/ledger.rs ~6-7
pub fn compute_neuron_staking_subaccount_bytes(controller: PrincipalId, nonce: u64) -> [u8; 32] {
    compute_neuron_domain_subaccount_bytes(controller, b"neuron-stake", nonce)
}
``` [4](#0-3) 

An attacker who knows the victim's intended `(child_controller, nonce)` pair can:
1. Transfer the minimum ICP stake to `AccountIdentifier::new(GOVERNANCE_CANISTER_ID, Some(compute_neuron_staking_subaccount(child_controller, nonce)))`.
2. Call `manage_neuron` → `ClaimOrRefresh` → `MemoAndController { controller: child_controller, memo: nonce }` from any principal.
3. This creates a neuron occupying that subaccount with `child_controller` as its controller.
4. The victim's subsequent `spawn` call hits `ensure_subaccount_available` and returns `SubaccountAlreadyExists`, permanently blocking that nonce.

The same domain collision does **not** apply to `disburse_to_neuron`, which uses `compute_neuron_disburse_subaccount_bytes` (domain `"neuron-split"`), so that path is not reachable via the claim flow. [5](#0-4) 

### Impact Explanation
Any neuron controller who uses a predictable or publicly known nonce when spawning a child neuron can be griefed: the spawn fails, the maturity is not converted, and the user must retry with a different nonce. The attacker gains nothing (the created neuron's controller is the victim's principal, not the attacker's), making this a pure griefing attack matching the external report's impact class. The attacker does lose the staked ICP, which limits scale but does not eliminate the attack.

### Likelihood Explanation
The IC has no public mempool, so real-time frontrunning of a specific in-flight message is not possible. However, the attack does not require frontrunning: it only requires knowing the `(child_controller, nonce)` pair **before** the victim's spawn executes. This is realistic when:
- The nonce is predictable (e.g., sequential integers, timestamps, or a fixed constant used by tooling — the test suite itself uses `NONCE = 12345`).
- The victim announces the nonce out-of-band (e.g., in a DAO forum post or dapp UI).
- The `child_controller` is the parent neuron's controller, which is public on-chain.

The cost barrier (minimum ICP stake) reduces likelihood but does not eliminate it for a motivated griefing actor. [6](#0-5) 

### Recommendation
1. **Remove the user-supplied nonce path for spawn entirely**: when no nonce is provided, a random subaccount is already generated safely via `new_neuron_subaccount` with collision retry. The deterministic nonce path adds no user-facing benefit that cannot be achieved by recording the returned neuron ID.
2. If deterministic subaccounts are required, use a **spawn-specific domain** (e.g., `"neuron-spawn"`) distinct from `"neuron-stake"` so that `claim_or_refresh_neuron_by_memo_and_controller` cannot collide with spawn-reserved subaccounts.
3. Alternatively, derive the child subaccount from `(parent_neuron_id, nonce)` rather than `(child_controller, nonce)`, making the subaccount unguessable without knowledge of the parent neuron ID.

### Proof of Concept
```
// Victim Alice owns neuron with controller X and wants to spawn
// a child neuron using nonce N = 0.

// Step 1 (Attacker Bob): Transfer minimum ICP stake to the target subaccount.
//   subaccount = compute_neuron_staking_subaccount(X, 0)
//   destination = AccountIdentifier::new(GOVERNANCE_CANISTER_ID, Some(subaccount))
ledger.icrc1_transfer({ to: destination, amount: min_stake });

// Step 2 (Attacker Bob): Claim a neuron at that subaccount for controller X.
governance.manage_neuron({
    command: ClaimOrRefresh {
        by: MemoAndController { controller: Some(X), memo: 0 }
    }
});
// => Succeeds. Neuron created with subaccount = compute_neuron_staking_subaccount(X, 0).
//    Controller is X (not Bob), so Bob gains nothing.

// Step 3 (Victim Alice): Attempt to spawn with nonce = 0.
governance.manage_neuron({
    id: alice_neuron_id,
    command: Spawn { new_controller: Some(X), nonce: Some(0), ... }
});
// => Fails: NeuronStoreError::SubaccountAlreadyExists { subaccount }
//    Alice must retry with a different nonce or omit the nonce entirely.
``` [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2677-2688)
```rust
        // Use provided sub-account if any, otherwise generate a random one.
        let to_subaccount = match spawn.nonce {
            None => self
                .neuron_store
                .new_neuron_subaccount(&mut *self.randomness)?,
            Some(nonce_val) => {
                let to_subaccount =
                    ledger::compute_neuron_staking_subaccount(child_controller, nonce_val);
                self.neuron_store
                    .ensure_subaccount_available(to_subaccount)?
            }
        };
```

**File:** rs/nns/governance/src/governance.rs (L2968-2977)
```rust
        // The account is derived from the new owner's principal so it can be found by
        // the owner on the ledger. There is no need to length-prefix the
        // principal since the nonce is constant length, and so there is no risk
        // of ambiguity.
        let to_subaccount = Subaccount(ledger::compute_neuron_disburse_subaccount_bytes(
            child_controller,
            disburse_to_neuron.nonce,
        ));
        self.neuron_store
            .ensure_subaccount_available(to_subaccount)?;
```

**File:** rs/nns/governance/src/governance.rs (L5852-5871)
```rust
    async fn claim_or_refresh_neuron_by_memo_and_controller(
        &mut self,
        caller: &PrincipalId,
        memo_and_controller: MemoAndController,
        claim_or_refresh: &ClaimOrRefresh,
    ) -> Result<NeuronId, GovernanceError> {
        let controller = memo_and_controller.controller.unwrap_or(*caller);
        let memo = memo_and_controller.memo;
        let subaccount = ledger::compute_neuron_staking_subaccount(controller, memo);
        match self.neuron_store.get_neuron_id_for_subaccount(subaccount) {
            Some(neuron_id) => {
                self.refresh_neuron(neuron_id, subaccount, claim_or_refresh)
                    .await
            }
            None => {
                self.claim_neuron(subaccount, controller, claim_or_refresh)
                    .await
            }
        }
    }
```

**File:** rs/nns/governance/src/neuron_store.rs (L323-346)
```rust
    /// Generates a unique random neuron subaccount, retrying on collision.
    pub fn new_neuron_subaccount(
        &self,
        random: &mut dyn RandomnessGenerator,
    ) -> Result<Subaccount, NeuronStoreError> {
        loop {
            let subaccount = Subaccount(
                random
                    .random_byte_array()
                    .map_err(|_| NeuronStoreError::NeuronSubaccountGenerationUnavailable)?,
            );

            if !self.has_neuron_with_subaccount(subaccount) {
                return Ok(subaccount);
            }

            ic_cdk::println!(
                "{}WARNING: A suspiciously near-impossible event has just occurred: \
                 we randomly picked a neuron subaccount, but it's already used: \
                 {:?}. Trying again...",
                LOG_PREFIX,
                subaccount,
            );
        }
```

**File:** rs/nns/governance/src/neuron_store.rs (L485-496)
```rust
    /// Checks that a deterministic (caller-supplied) subaccount is not already
    /// in use. Unlike random subaccounts (which retry on collision), deterministic
    /// subaccounts must fail immediately since retrying would produce the same result.
    pub fn ensure_subaccount_available(
        &self,
        subaccount: Subaccount,
    ) -> Result<Subaccount, NeuronStoreError> {
        if self.has_neuron_with_subaccount(subaccount) {
            return Err(NeuronStoreError::SubaccountAlreadyExists { subaccount });
        }
        Ok(subaccount)
    }
```

**File:** rs/nervous_system/common/src/ledger.rs (L6-7)
```rust
pub fn compute_neuron_staking_subaccount_bytes(controller: PrincipalId, nonce: u64) -> [u8; 32] {
    compute_neuron_domain_subaccount_bytes(controller, b"neuron-stake", nonce)
```
