### Title
Anonymous Principal as Neuron Hotkey Grants Unrestricted Governance Access to Any Caller - (`rs/nns/governance/src/neuron/types.rs`)

### Summary

The NNS Governance canister's `add_hot_key` function does not validate that the supplied `new_hot_key` is not the anonymous principal (`[4]`). If a neuron controller adds the anonymous principal as a hotkey — deliberately or accidentally — any unprivileged caller sending an unsigned (anonymous) ingress message can vote, make proposals, and change followees on behalf of that neuron, because the IC runtime sets `caller()` to the anonymous principal for all unsigned calls and the hotkey authorization check has no anonymous-principal guard.

### Finding Description

**Root cause — missing anonymous-principal check in `add_hot_key`:**

`rs/nns/governance/src/neuron/types.rs` lines 657–675:

```rust
fn add_hot_key(&mut self, new_hot_key: &PrincipalId) -> Result<(), GovernanceError> {
    for key in &self.hot_keys {
        if *key == *new_hot_key {
            return Err(GovernanceError::new_with_message(
                ErrorType::HotKey,
                "Hot key duplicated.",
            ));
        }
    }
    if self.hot_keys.len() >= MAX_NUM_HOT_KEYS_PER_NEURON {
        return Err(GovernanceError::new_with_message(
            ErrorType::ResourceExhausted,
            "Reached the maximum number of hotkeys.",
        ));
    }
    self.hot_keys.push(*new_hot_key);
    Ok(())
}
```

The only guards are duplicate detection and a count cap. There is no `new_hot_key.is_anonymous()` check.

**Authorization path that is then exploitable:**

`rs/nns/governance/src/neuron/types.rs` lines 253–256:

```rust
fn is_hotkey_or_controller(&self, principal: &PrincipalId) -> bool {
    self.is_controlled_by(principal) || self.hot_keys.contains(principal)
}
```

`is_authorized_to_vote` (line 243) and `is_authorized_to_configure_or_err` (line 781) both delegate to `is_hotkey_or_controller`. No anonymous-principal guard exists at either call site.

**Why unsigned calls reach the canister with `caller() == anonymous_principal`:**

The ingress validator in `rs/validator/src/ingress_validation.rs` lines 853–858 explicitly *allows* unsigned messages from the anonymous principal through to canisters:

```rust
None => {
    if sender.get().is_anonymous() {
        return Ok(CanisterIdSet::all());
    }
    Err(MissingSignature(*sender))
}
```

So any attacker can send an unsigned `manage_neuron` call; the canister receives it with `caller() == PrincipalId::new_anonymous()`. If that principal is in `hot_keys`, `is_hotkey_or_controller` returns `true`.

**Exploit path:**

1. Neuron controller calls `manage_neuron` → `Configure` → `AddHotKey { new_hot_key: PrincipalId::new_anonymous() }`. This succeeds because `add_hot_key` has no anonymous guard.
2. From this point, any party on the internet sends an unsigned `manage_neuron` call targeting that neuron.
3. The canister's `register_vote`, `make_proposal`, or `Follow` handler calls `is_authorized_to_vote(anonymous_principal)` → `is_hotkey_or_controller(anonymous_principal)` → `hot_keys.contains(anonymous_principal)` → `true`.
4. The operation executes as if the neuron's legitimate hotkey authorized it.

### Impact Explanation

Any unprivileged external caller can:
- Cast votes on NNS proposals on behalf of the affected neuron (governance integrity break).
- Submit NNS proposals using the neuron's stake (bypassing the proposal fee intent).
- Change the neuron's followees on any topic except `ManageNeuron`.
- Join or leave the Neuron Fund on behalf of the neuron.

For a high-voting-power neuron (e.g., a foundation or exchange neuron), this is a critical governance authorization bypass: a single anonymous HTTP request can swing proposal outcomes.

### Likelihood Explanation

A controller might add the anonymous principal as a hotkey:
- Accidentally, by passing a zero-length or default-constructed `PrincipalId` that serializes to the anonymous byte `[4]`.
- Intentionally but naively, believing it creates a "public" or "open" voting key without understanding the security implication.
- Via a buggy frontend or tooling that constructs the wrong principal.

The original M-11 report noted the same pattern: admins sending hats to `address(0)` for seemingly innocuous reasons without realizing the multisig impact. The same reasoning applies here.

### Recommendation

Add an anonymous-principal guard at the top of `add_hot_key` in `rs/nns/governance/src/neuron/types.rs`:

```rust
fn add_hot_key(&mut self, new_hot_key: &PrincipalId) -> Result<(), GovernanceError> {
    if new_hot_key.is_anonymous() {
        return Err(GovernanceError::new_with_message(
            ErrorType::InvalidCommand,
            "The anonymous principal cannot be added as a hot key.",
        ));
    }
    // ... existing duplicate and count checks
}
```

For defense-in-depth, also add an anonymous check inside `is_hotkey_or_controller`:

```rust
fn is_hotkey_or_controller(&self, principal: &PrincipalId) -> bool {
    if principal.is_anonymous() {
        return false;
    }
    self.is_controlled_by(principal) || self.hot_keys.contains(principal)
}
```

### Proof of Concept

1. Deploy NNS locally. Create a neuron with controller `C`.
2. As `C`, call:
   ```
   manage_neuron(Configure(AddHotKey { new_hot_key: PrincipalId::new_anonymous() }))
   ```
   This succeeds — `add_hot_key` pushes `[4]` into `hot_keys`.
3. From any unauthenticated HTTP client, send an unsigned `manage_neuron` call with `sender = [4]` (anonymous) targeting the neuron with `RegisterVote { proposal_id: X, vote: Yes }`.
4. The ingress validator passes the unsigned call through. The governance canister calls `is_authorized_to_vote(anonymous_principal)` → `hot_keys.contains([4])` → `true`. The vote is cast. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/nns/governance/src/neuron/types.rs (L243-245)
```rust
    pub(crate) fn is_authorized_to_vote(&self, principal: &PrincipalId) -> bool {
        self.is_hotkey_or_controller(principal)
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L253-256)
```rust
    /// Returns true if and only if `principal` is either the controller or a hotkey
    fn is_hotkey_or_controller(&self, principal: &PrincipalId) -> bool {
        self.is_controlled_by(principal) || self.hot_keys.contains(principal)
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L657-675)
```rust
    fn add_hot_key(&mut self, new_hot_key: &PrincipalId) -> Result<(), GovernanceError> {
        // Make sure that the same hot key is not added twice.
        for key in &self.hot_keys {
            if *key == *new_hot_key {
                return Err(GovernanceError::new_with_message(
                    ErrorType::HotKey,
                    "Hot key duplicated.",
                ));
            }
        }
        // Allow at most 10 hot keys per neuron.
        if self.hot_keys.len() >= MAX_NUM_HOT_KEYS_PER_NEURON {
            return Err(GovernanceError::new_with_message(
                ErrorType::ResourceExhausted,
                "Reached the maximum number of hotkeys.",
            ));
        }
        self.hot_keys.push(*new_hot_key);
        Ok(())
```

**File:** rs/validator/src/ingress_validation.rs (L853-858)
```rust
    match signature {
        None => {
            if sender.get().is_anonymous() {
                return Ok(CanisterIdSet::all());
            }
            Err(MissingSignature(*sender))
```

**File:** rs/types/base_types/src/principal_id.rs (L353-355)
```rust
    pub fn is_anonymous(&self) -> bool {
        self.as_slice() == [PrincipalIdClass::Anonymous as u8]
    }
```
