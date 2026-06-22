I found a concrete analog. Let me verify the exact code paths before writing the final output.### Title
Hot Keys Not Cleared on NNS Neuron Ownership Transfer via `spawn_neuron` - (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

When `spawn_neuron` is called with a `new_controller` (transferring the spawned neuron to a different principal), the child neuron unconditionally inherits the parent neuron's full `hot_keys` list. The previous owner's hot keys are never cleared, giving them persistent, unauthorized governance influence over the new owner's neuron without the new owner's knowledge or consent.

---

### Finding Description

In `rs/nns/governance/src/governance.rs`, `spawn_neuron` supports an optional `new_controller` field that assigns the spawned child neuron to a different principal than the parent's controller. This is the NNS equivalent of an ownership transfer. However, the child neuron is constructed by unconditionally cloning the parent's hot keys:

```rust
// rs/nns/governance/src/governance.rs lines 2704–2718
let child_neuron = NeuronBuilder::new(
    child_nid,
    to_subaccount,
    child_controller,          // ← new owner
    DissolveStateAndAge::DissolvingOrDissolved { ... },
    created_timestamp_seconds,
)
.with_spawn_at_timestamp_seconds(dissolve_and_spawn_at_timestamp_seconds)
.with_hot_keys(parent_neuron.hot_keys.clone())   // ← parent's hot keys copied verbatim
.with_followees(parent_neuron.followees.clone())
.with_kyc_verified(parent_neuron.kyc_verified)
.with_maturity_e8s_equivalent(maturity_to_spawn)
.build();
```

There is no conditional check: if `child_controller != parent_neuron.controller()`, the hot keys should be cleared. The same pattern exists in `split_neuron` (lines 2241–2257, `.with_hot_keys(parent_neuron.hot_keys.clone())`), though `split_neuron` always keeps the same controller so the impact is lower there.

Hot keys in NNS governance are authorized to:
- Vote on proposals (`RegisterVote`)
- Set following (`Follow` / `SetFollowing`)
- Join or leave the Neurons' Fund (`JoinCommunityFund` / `LeaveCommunityFund`)

These are all governance-affecting operations. The new controller has no way to enumerate inherited hot keys unless they explicitly query their neuron's full state, and there is no notification mechanism.

The `Spawn` message is defined in `rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto` lines 862–870 and the `new_controller` field is explicitly designed for cross-principal spawning.

---

### Impact Explanation

**Vulnerability class:** Governance authorization bug — permissions not reset on ownership transfer.

An attacker who controls a parent NNS neuron with accumulated maturity can:
1. Pre-register one or more malicious hot keys on their neuron.
2. Spawn a child neuron with `new_controller = victim_principal`.
3. The spawned neuron, now owned by the victim, retains the attacker's hot keys.
4. The attacker's hot keys can vote on NNS governance proposals on behalf of the victim's neuron for as long as the victim does not discover and remove them.

Because NNS voting power is proportional to stake × dissolve delay × age, a spawned neuron with significant maturity converted to stake can carry meaningful voting power. The attacker silently influences NNS governance outcomes through a neuron they no longer own.

---

### Likelihood Explanation

- `spawn_neuron` with `new_controller` is a documented, reachable, unprivileged ingress path — any NNS neuron controller with sufficient maturity can call it.
- The attacker controls the entire setup: they choose when to add hot keys and when to spawn.
- The victim has no notification that hot keys were inherited; they must proactively call `get_full_neuron` and inspect the `hot_keys` field.
- The attack requires no privileged access, no threshold corruption, and no social engineering beyond convincing a victim to accept a spawned neuron (or exploiting a scenario where spawning to another principal is routine, e.g., institutional staking services).

Likelihood: **Medium** — requires intentional setup by the parent controller, but the entry path is fully unprivileged and the victim has no automatic defense.

---

### Recommendation

In `spawn_neuron`, when `child_controller != parent_neuron.controller()`, do not copy the parent's hot keys to the child neuron. The child neuron should be initialized with an empty hot-keys list when ownership is being transferred to a new principal:

```rust
let hot_keys = if child_controller == parent_neuron.controller() {
    parent_neuron.hot_keys.clone()
} else {
    vec![]
};

let child_neuron = NeuronBuilder::new(...)
    .with_hot_keys(hot_keys)
    ...
```

Similarly, review `split_neuron` for the same pattern, though its impact is lower since the controller does not change.

---

### Proof of Concept

**Actors:** Alice (attacker, parent neuron controller), Bob (attacker's hot key), Charlie (victim, `new_controller`).

1. Alice holds NNS neuron `N` with sufficient maturity (≥ `neuron_minimum_spawn_stake_e8s`).
2. Alice calls `manage_neuron` → `Configure` → `AddHotKey { new_hot_key: Bob }` on neuron `N`.
3. Alice calls `manage_neuron` → `Spawn { new_controller: Some(Charlie), nonce: Some(42), percentage_to_spawn: Some(100) }`.
4. NNS Governance executes `spawn_neuron` at `rs/nns/governance/src/governance.rs:2613`. At line 2714, `parent_neuron.hot_keys.clone()` copies `[Bob]` into the child neuron's hot keys. The child neuron is assigned to `child_controller = Charlie`.
5. Bob calls `manage_neuron` → `RegisterVote` or `Follow` targeting Charlie's spawned neuron. NNS Governance authorizes Bob because `hot_keys` contains Bob's principal.
6. Charlie queries `get_full_neuron` and sees `hot_keys: [Bob]` — but only if they know to look.

**Root cause line:** `rs/nns/governance/src/governance.rs:2714` — `.with_hot_keys(parent_neuron.hot_keys.clone())` inside `spawn_neuron`, with no guard on whether `child_controller == parent_neuron.controller()`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2241-2257)
```rust
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
```

**File:** rs/nns/governance/src/governance.rs (L2651-2655)
```rust
        let child_controller = if let Some(child_controller) = &spawn.new_controller {
            *child_controller
        } else {
            parent_neuron.controller()
        };
```

**File:** rs/nns/governance/src/governance.rs (L2704-2718)
```rust
        let child_neuron = NeuronBuilder::new(
            child_nid,
            to_subaccount,
            child_controller,
            DissolveStateAndAge::DissolvingOrDissolved {
                when_dissolved_timestamp_seconds: dissolve_and_spawn_at_timestamp_seconds,
            },
            created_timestamp_seconds,
        )
        .with_spawn_at_timestamp_seconds(dissolve_and_spawn_at_timestamp_seconds)
        .with_hot_keys(parent_neuron.hot_keys.clone())
        .with_followees(parent_neuron.followees.clone())
        .with_kyc_verified(parent_neuron.kyc_verified)
        .with_maturity_e8s_equivalent(maturity_to_spawn)
        .build();
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L862-870)
```text
  message Spawn {
    option (ic_base_types.pb.v1.tui_signed_message) = true;
    // If not set, the spawned neuron will have the same controller as
    // this neuron.
    ic_base_types.pb.v1.PrincipalId new_controller = 1 [(ic_base_types.pb.v1.tui_signed_display_q2_2021) = true];
    // The nonce with which to create the subaccount.
    optional uint64 nonce = 2;
    // The percentage to spawn, from 1 to 100 (inclusive).
    optional uint32 percentage_to_spawn = 3;
```
