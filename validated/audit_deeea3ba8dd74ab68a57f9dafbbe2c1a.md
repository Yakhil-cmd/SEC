### Title
Predictable Subaccount DoS on `spawn_neuron` with Nonce via Pre-emptive Neuron Claim - (File: `rs/nns/governance/src/governance.rs`)

### Summary

When a neuron controller calls `spawn_neuron` with an explicit `nonce`, the child neuron's subaccount is computed deterministically using the same public formula (`compute_neuron_staking_subaccount(child_controller, nonce)`) that the open `ClaimOrRefresh` ingress path also uses. An unprivileged actor who knows the intended `child_controller` and `nonce` can preemptively stake ICP to that subaccount and claim a neuron there, causing `ensure_subaccount_available` to return `SubaccountAlreadyExists` and permanently blocking that `(child_controller, nonce)` pair for `spawn_neuron`.

### Finding Description

In `rs/nns/governance/src/governance.rs`, `spawn_neuron` branches on whether a nonce is supplied:

```rust
let to_subaccount = match spawn.nonce {
    None => self.neuron_store.new_neuron_subaccount(&mut *self.randomness)?,
    Some(nonce_val) => {
        let to_subaccount =
            ledger::compute_neuron_staking_subaccount(child_controller, nonce_val);
        self.neuron_store
            .ensure_subaccount_available(to_subaccount)?   // ← hard fail, no retry
    }
};
``` [1](#0-0) 

`ensure_subaccount_available` is documented to fail immediately with no retry because deterministic subaccounts always produce the same result:

```rust
pub fn ensure_subaccount_available(&self, subaccount: Subaccount) -> Result<Subaccount, NeuronStoreError> {
    if self.has_neuron_with_subaccount(subaccount) {
        return Err(NeuronStoreError::SubaccountAlreadyExists { subaccount });
    }
    Ok(subaccount)
}
``` [2](#0-1) [3](#0-2) 

The subaccount formula used here is `compute_neuron_staking_subaccount(child_controller, nonce_val)`: [4](#0-3) 

This is **identical** to the formula used by the open `ClaimOrRefresh::MemoAndController` ingress path in `claim_or_refresh_neuron_by_memo_and_controller`:

```rust
let subaccount = ledger::compute_neuron_staking_subaccount(controller, memo);
match self.neuron_store.get_neuron_id_for_subaccount(subaccount) {
    Some(neuron_id) => self.refresh_neuron(...).await,
    None => self.claim_neuron(subaccount, controller, ...).await,
}
``` [5](#0-4) 

Because `claim_or_refresh_neuron_by_memo_and_controller` accepts any `controller` principal (not just the caller), any unprivileged ingress sender can create a neuron owned by an arbitrary principal at an arbitrary deterministic subaccount.

### Impact Explanation

An attacker who knows the `child_controller` and `nonce` a victim intends to use in `spawn_neuron` can:

1. Compute `target_subaccount = compute_neuron_staking_subaccount(child_controller, nonce)` — fully public.
2. Transfer ≥ `neuron_minimum_stake_e8s` ICP to `governance_account[target_subaccount]`.
3. Submit `manage_neuron { ClaimOrRefresh { MemoAndController { controller: child_controller, memo: nonce } } }`.

This creates a neuron occupying `target_subaccount`. When the victim subsequently calls `spawn_neuron` with the same `child_controller` and `nonce`, `ensure_subaccount_available` returns `SubaccountAlreadyExists` and the spawn fails. The victim must choose a different nonce; the attacker can repeat for each new nonce the victim tries if they can observe the victim's intent. Automated or protocol-driven spawning workflows that rely on deterministic nonces (e.g., derived from a neuron ID or timestamp) are permanently blocked for those nonce values.

### Likelihood Explanation

The attack is reachable via a standard unprivileged ingress message to the NNS Governance canister — no special role, key, or majority is required. The cost is ≥ 1 ICP per blocked nonce (the minimum neuron stake). The attacker must know the victim's intended `child_controller` and `nonce` in advance, which is feasible when: (a) the victim uses predictable nonces (e.g., sequential integers, timestamps, or neuron IDs), (b) the victim's tooling is open-source and nonce derivation is known, or (c) the victim announces the spawn parameters. The `spawn_neuron` nonce feature is explicitly documented and used in production tooling. [6](#0-5) 

### Recommendation

Use a distinct domain separator for spawn-derived subaccounts so they cannot be pre-empted via the standard `ClaimOrRefresh` path. Replace:

```rust
ledger::compute_neuron_staking_subaccount(child_controller, nonce_val)
```

with a spawn-specific variant (analogous to how `split_neuron` uses `compute_neuron_split_subaccount_bytes` with domain `"split-neuron"` and `disburse_to_neuron` uses `compute_neuron_disburse_subaccount_bytes` with domain `"neuron-split"`): [7](#0-6) 

A new `compute_neuron_spawn_subaccount_bytes(controller, nonce)` with domain `"neuron-spawn"` would make the spawn subaccount namespace disjoint from the staking namespace, eliminating the pre-emption vector.

### Proof of Concept

```
# 1. Victim intends to call:
#    spawn_neuron(parent_id, Spawn { new_controller: VICTIM_CTRL, nonce: Some(42), ... })

# 2. Attacker computes the target subaccount (public formula):
target = SHA256(len("neuron-stake") || "neuron-stake" || VICTIM_CTRL_bytes || 42_u64_be)

# 3. Attacker transfers 1 ICP to governance_canister[target_subaccount]

# 4. Attacker submits ingress to NNS Governance:
manage_neuron {
  command: ClaimOrRefresh {
    by: MemoAndController { controller: VICTIM_CTRL, memo: 42 }
  }
}
# → neuron created at target_subaccount, owned by VICTIM_CTRL

# 5. Victim submits spawn_neuron with nonce=42
# → ensure_subaccount_available(target) returns SubaccountAlreadyExists
# → spawn_neuron fails
``` [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2678-2688)
```rust
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

**File:** rs/nns/governance/src/governance.rs (L5858-5870)
```rust
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

**File:** rs/nervous_system/common/src/ledger.rs (L12-14)
```rust
pub fn compute_neuron_staking_subaccount(controller: PrincipalId, nonce: u64) -> IcpSubaccount {
    IcpSubaccount(compute_neuron_staking_subaccount_bytes(controller, nonce))
}
```

**File:** rs/nervous_system/common/src/ledger.rs (L22-33)
```rust
pub fn compute_neuron_disburse_subaccount_bytes(controller: PrincipalId, nonce: u64) -> [u8; 32] {
    // The "domain" for neuron disburse was unfortunately chosen to be "neuron-split". It might be
    // possible to change to a more meaningful name, but there is no strong reason to do so, and
    // there is some risk that this behavior is depended on.
    compute_neuron_domain_subaccount_bytes(controller, b"neuron-split", nonce)
}

// Computes the subaccount to which neuron split transfers are made.
pub fn compute_neuron_split_subaccount_bytes(controller: PrincipalId, nonce: u64) -> [u8; 32] {
    // Unfortunately "neuron-split" is used for disburse, so we need to use a different domain.
    compute_neuron_domain_subaccount_bytes(controller, b"split-neuron", nonce)
}
```
