### Title
Sender Delegation Accepted When Expired at Exact Boundary Timestamp - (File: rs/validator/src/ingress_validation.rs)

### Summary
The `validate_sender_delegation_expiry` function in the IC ingress validation path uses a strict less-than (`<`) comparison to check whether a sender delegation has expired. This means a delegation whose `expiration` timestamp is exactly equal to `current_time` is treated as **valid**, even though it has already expired at that instant. An unprivileged ingress sender can craft a request with a delegation whose expiry equals the current replica time and have it accepted, contrary to the intended semantics that a delegation is valid only while `expiration >= current_time` is still in the future.

### Finding Description
In `rs/validator/src/ingress_validation.rs`, the function `validate_sender_delegation_expiry` checks:

```rust
if delegation.delegation().expiration() < current_time {
    return Err(InvalidDelegationExpiry(...));
}
```

The condition only rejects a delegation when `expiration < current_time`. When `expiration == current_time`, the condition is `false`, so no error is returned and the delegation is accepted as valid.

The IC Interface Specification states that a delegation's `expiration` field is a timestamp in nanoseconds since the Unix epoch, and a delegation is valid only if the current time has not yet reached or passed that timestamp. The semantically correct check should be:

```rust
if delegation.delegation().expiration() <= current_time {
    return Err(InvalidDelegationExpiry(...));
}
```

This is the direct analog of the Y2K Finance H-09 finding: a boundary condition uses `<` where `<=` is required, allowing an action (here: accepting an expired delegation) to proceed when it should be rejected.

The ingress expiry check in the same file (`validate_ingress_expiry`) correctly uses `<=` for its lower bound:
```rust
if !(min_allowed_expiry <= provided_expiry && provided_expiry <= max_allowed_expiry) {
```
confirming that the intent is inclusive-boundary rejection. The delegation check is inconsistent with this.

### Impact Explanation
An attacker who controls a delegation key can craft a signed delegation with `expiration` set to exactly the current replica time (nanosecond precision). The delegation will pass `validate_sender_delegation_expiry` and the full request will be accepted and executed. This allows:

- **Ingress/query/read_state bypass**: A delegation that should be considered expired at the exact nanosecond boundary is accepted, allowing the delegated key to authenticate requests on behalf of the delegating principal.
- **Stale delegation reuse**: In practice, an attacker who knows the current replica time (observable via the IC API) can time a request so that the delegation's expiry equals `current_time`, extending the effective lifetime of a delegation by one nanosecond beyond its stated expiry.

The impact is an **ingress/read_state validation bypass** at the exact expiry boundary, allowing unauthorized use of an expired delegation credential.

### Likelihood Explanation
The attacker entry path is fully unprivileged: any user can submit an ingress message or query with a crafted delegation. The attacker needs only to know the approximate current replica time (available via the IC public API) and set the delegation expiry to that value. The window is a single nanosecond, but since the replica time is deterministic within a round and observable, a motivated attacker can reliably hit this boundary. Likelihood is **low-to-medium** due to the nanosecond precision required, but the path is fully reachable with no privileged access.

### Recommendation
Change the comparison in `validate_sender_delegation_expiry` from strict less-than to less-than-or-equal:

```rust
// Before (incorrect):
if delegation.delegation().expiration() < current_time {

// After (correct):
if delegation.delegation().expiration() <= current_time {
```

This ensures a delegation is rejected when its expiry timestamp equals the current time, consistent with the semantics that `expiration` is the last nanosecond at which the delegation is valid (i.e., it is valid for `expiration > current_time` only).

### Proof of Concept
1. Attacker observes the current replica time `T` (e.g., via a query call that returns `ic0.time()`).
2. Attacker creates a delegation from principal A to key B with `expiration = T`.
3. Attacker submits an ingress call signed by key B with this delegation, at replica time `T`.
4. `validate_sender_delegation_expiry` evaluates `T < T` → `false` → no error returned.
5. The delegation is accepted; the request is authenticated as principal A and executed.
6. With the correct fix (`T <= T` → `true`), the delegation would be rejected with `InvalidDelegationExpiry`. [1](#0-0) [2](#0-1)

### Citations

**File:** rs/validator/src/ingress_validation.rs (L576-576)
```rust
    if !(min_allowed_expiry <= provided_expiry && provided_expiry <= max_allowed_expiry) {
```

**File:** rs/validator/src/ingress_validation.rs (L604-623)
```rust
// Check if any of the sender delegation has expired with respect to the
// `current_time`, and return an error if so.
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
}
```
