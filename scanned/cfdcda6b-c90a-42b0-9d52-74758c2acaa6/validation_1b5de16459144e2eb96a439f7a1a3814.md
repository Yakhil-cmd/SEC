### Title
IC Ingress Delegation Without Canister Targets Grants Unrestricted Access to All Canisters — (File: rs/validator/src/ingress_validation.rs)

### Summary
The IC ingress delegation system treats any `Delegation` with no `targets` field as granting access to **all** canisters (`CanisterIdSet::all()`). Because `Delegation::new()` omits targets by default, any session key or delegated key created without explicit canister scoping can act on behalf of the delegating principal across the entire IC — including governance, ledger, and every other canister — with no restriction.

### Finding Description
In `rs/validator/src/ingress_validation.rs`, `validate_delegation()` explicitly returns `CanisterIdSet::all()` when `delegation.targets()` is `None`:

```rust
Ok(match delegation.targets().map_err(DelegationTargetError)? {
    None => CanisterIdSet::all(),   // ← unrestricted
    Some(targets) => CanisterIdSet::try_from_iter(targets)...
})
``` [1](#0-0) 

The `Delegation::new()` constructor in `rs/types/types/src/messages/http.rs` sets `targets: None` by default:

```rust
pub fn new(pubkey: Vec<u8>, expiration: Time) -> Self {
    Self { pubkey: Blob(pubkey), expiration, targets: None }
}
``` [2](#0-1) 

`validate_delegations()` initializes the running target set to `CanisterIdSet::all()` and intersects it with each delegation's targets. If no delegation in the chain specifies targets, the intersection remains `CanisterIdSet::all()` throughout, and the final `validate_request_target()` check passes for **any** canister ID. [3](#0-2) 

The `Delegation` struct's `targets` field is `Option<Vec<Blob>>`, so omitting it is structurally valid and produces no warning or error at construction time. [4](#0-3) 

### Impact Explanation
A user who creates a delegation (e.g., a dApp session key via Internet Identity or directly) without specifying canister targets inadvertently grants the delegated key full ingress access to every canister under their principal. If the session key is compromised, the dApp frontend is malicious, or an XSS attack steals the key, the attacker can:

- Transfer ICP or ICRC-1 tokens from the user's ledger account
- Vote on NNS/SNS governance proposals as the user
- Call any other canister (including management canister operations the user is authorized for) as the user

This is a direct ingress authentication scope bypass: the delegated key bypasses the user's intent to restrict access because the restriction was never encoded. The IC specification and code are self-consistent, but the default is maximally permissive, mirroring the EVM finding that "delegations are unrestricted by default."

### Likelihood Explanation
Medium. Internet Identity and many dApps create session key delegations. The default `Delegation::new()` path produces an unrestricted delegation. A compromised dApp frontend, a stolen session key, or a malicious dApp immediately yields full principal-level access to all canisters for the duration of the delegation's `expiration`. There is no on-chain revocation mechanism for user-level delegations; the only mitigation is waiting for expiry. [5](#0-4) 

### Recommendation
- Tooling (agents, wallets, Internet Identity) should default to scoped delegations with explicit `targets` rather than unrestricted ones.
- Consider adding a protocol-level warning or a distinct "unrestricted delegation" marker so that validators and monitoring infrastructure can flag or rate-limit unrestricted delegations.
- Documentation should prominently state that `targets: None` means unrestricted access to all canisters, not "no delegation targets configured."

### Proof of Concept
1. User creates `Delegation::new(session_key_pubkey, expiration)` — `targets` is `None`.
2. `validate_delegation()` returns `CanisterIdSet::all()` (line 834–835 of `rs/validator/src/ingress_validation.rs`).
3. `validate_delegations()` initializes `targets = CanisterIdSet::all()` and intersects with `CanisterIdSet::all()` → still `CanisterIdSet::all()`.
4. `validate_request_target()` passes for any canister ID the attacker chooses.
5. The session key can now sign ingress messages to **any** canister as the delegating user — NNS governance, ICP ledger, SNS, or any dApp canister. [6](#0-5)

### Citations

**File:** rs/validator/src/ingress_validation.rs (L223-233)
```rust
fn validate_request_target<C: HasCanisterId>(
    request: &HttpRequest<C>,
    targets: &CanisterIdSet,
) -> Result<(), RequestValidationError> {
    if targets.contains(&request.content().canister_id()) {
        Ok(())
    } else {
        Err(CanisterNotInDelegationTargets(
            request.content().canister_id(),
        ))
    }
```

**File:** rs/validator/src/ingress_validation.rs (L606-622)
```rust
fn validate_sender_delegation_expiry(
    sender_delegation: &Option<Vec<SignedDelegation>>,
    current_time: Time,
) -> Result<(), RequestValidationError> {
    if let Some(delegations) = &sender_delegation {
        for delegation in delegations.iter() {
            let expiry = delegation.delegation().expiration();
            if delegation.delegation().expiration() < current_time {
                return Err(InvalidDelegationExpiry(format!(
                    "Specified sender delegation has expired:\n\
                     Provided expiry:    {expiry}\n\
                     Local replica time: {current_time}",
                )));
            }
        }
    }
    Ok(())
```

**File:** rs/validator/src/ingress_validation.rs (L730-752)
```rust
    ensure_delegations_does_not_contain_cycles(&pubkey, signed_delegations)?;
    ensure_delegations_does_not_contain_too_many_targets(signed_delegations)?;
    // Initially, assume that the delegations target all possible canister IDs.
    let mut targets = CanisterIdSet::all();

    for sd in signed_delegations {
        let delegation = sd.delegation();
        let signature = sd.signature();

        let new_targets = validate_delegation(
            validator,
            signature,
            delegation,
            &pubkey,
            root_of_trust_provider,
        )
        .map_err(InvalidDelegation)?;
        // Restrict the canister targets to the ones specified in the delegation.
        targets = targets.intersect(new_targets);
        pubkey = delegation.pubkey().to_vec();
    }

    Ok((pubkey, targets))
```

**File:** rs/validator/src/ingress_validation.rs (L833-838)
```rust
    // Validation succeeded. Return the targets of this delegation.
    Ok(match delegation.targets().map_err(DelegationTargetError)? {
        None => CanisterIdSet::all(),
        Some(targets) => CanisterIdSet::try_from_iter(targets)
            .map_err(|e| DelegationTargetError(format!("{e}")))?,
    })
```

**File:** rs/types/types/src/messages/http.rs (L543-547)
```rust
pub struct Delegation {
    pubkey: Blob,
    expiration: Time,
    targets: Option<Vec<Blob>>,
}
```

**File:** rs/types/types/src/messages/http.rs (L550-556)
```rust
    pub fn new(pubkey: Vec<u8>, expiration: Time) -> Self {
        Self {
            pubkey: Blob(pubkey),
            expiration,
            targets: None,
        }
    }
```
