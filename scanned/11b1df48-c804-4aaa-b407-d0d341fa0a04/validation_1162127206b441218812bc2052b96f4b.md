### Title
`compute_new_subnet_admins` Incorrectly Rejects Add Operations When Provided Principals Already Exist or Contain Duplicates - (File: `rs/registry/canister/src/mutations/do_update_subnet_admins.rs`)

---

### Summary

In `compute_new_subnet_admins`, the capacity guard for `OperationType::Add` computes `deduped_current_subnet_admins.len() + principal_ids.len()` using the **raw, un-deduplicated** input vector before any overlap with the existing set is resolved. This causes the check to fire and reject operations that would not actually exceed `MAX_SUBNET_ADMINS`, mirroring the KUMASwap M-04 pattern exactly.

---

### Finding Description

`compute_new_subnet_admins` in `rs/registry/canister/src/mutations/do_update_subnet_admins.rs` performs the following guard at line 172:

```rust
if deduped_current_subnet_admins.len() + principal_ids.len() > MAX_SUBNET_ADMINS {
    return Err(UpdateSubnetAdminsError::TooManySubnetAdmins { ... });
}
``` [1](#0-0) 

`principal_ids` at this point is the raw `Vec<PrincipalId>` from the caller payload — it has **not** been deduplicated against itself, and it has **not** been intersected with `deduped_current_subnet_admins`. The actual union is only computed afterward:

```rust
let deduped_provided_principal_ids = principal_ids
    .into_iter()
    .map(PrincipalIdPb::from)
    .collect::<HashSet<PrincipalIdPb>>();

deduped_current_subnet_admins
    .union(&deduped_provided_principal_ids)
    .cloned()
    .collect()
``` [2](#0-1) 

Two distinct false-rejection scenarios arise:

**Scenario A — Provided list contains principals already in the existing set:**
- Existing admins: 10 (= `MAX_SUBNET_ADMINS`)
- Provided: `[A]` where `A` is already an admin
- Guard: `10 + 1 = 11 > 10` → **ERROR** (panic)
- Actual union result: still 10 admins — the operation is a no-op and should succeed

**Scenario B — Provided list contains internal duplicates:**
- Existing admins: 9
- Provided: `[A, A]`
- Guard: `9 + 2 = 11 > 10` → **ERROR** (panic)
- Actual union result: 10 admins — within the limit, should succeed

`MAX_SUBNET_ADMINS` is 10. [3](#0-2) 

On error the function returns `Err(...)`, which the caller `do_update_subnet_admins` converts to a `panic!`:

```rust
Err(err) => {
    panic!(
        "{LOG_PREFIX}do_update_subnet_admins: Error while updating subnet admins of {subnet_id}: {err}",
    );
}
``` [4](#0-3) 

In the IC execution model a canister panic traps the update call and rolls back state, so the registry mutation is never applied.

---

### Impact Explanation

Any NNS governance proposal (or authorized caller) that attempts to add a principal to a rented subnet's admin list when that subnet already has exactly `MAX_SUBNET_ADMINS` (10) admins will be incorrectly rejected — even if the principal is already an admin (idempotent re-add) or if the provided list contains duplicates that would not actually grow the set. The registry canister traps, the proposal execution fails, and the subnet admin list cannot be updated through that proposal. This blocks legitimate, idempotent subnet admin management on fully-populated rented subnets.

---

### Likelihood Explanation

Rented application subnets with exactly 10 admins are a realistic operational state. A governance proposal to idempotently re-confirm an existing admin (e.g., as part of a bulk update or retry) would silently fail. The condition is specific but reachable in normal operation without any adversarial action.

---

### Recommendation

Move the capacity check to after both deduplication steps, checking the actual resulting union size:

```rust
OperationType::Add(principal_ids) => {
    if principal_ids.is_empty() {
        return Err(UpdateSubnetAdminsError::PrincipalListEmpty);
    }

    let deduped_provided_principal_ids = principal_ids
        .into_iter()
        .map(PrincipalIdPb::from)
        .collect::<HashSet<PrincipalIdPb>>();

    // Check the actual resulting set size, not the naive sum
    let resulting_size = deduped_current_subnet_admins
        .union(&deduped_provided_principal_ids)
        .count();

    if resulting_size > MAX_SUBNET_ADMINS {
        return Err(UpdateSubnetAdminsError::TooManySubnetAdmins {
            provided: deduped_provided_principal_ids.len() as u64,
            existing: deduped_current_subnet_admins.len() as u64,
            max_allowed: MAX_SUBNET_ADMINS as u64,
        });
    }

    deduped_current_subnet_admins
        .union(&deduped_provided_principal_ids)
        .cloned()
        .collect()
}
```

---

### Proof of Concept

The existing test `can_not_add_existing_subnet_admins` (line 485) demonstrates that re-adding an existing admin is intended to be a no-op: [5](#0-4) 

However, this test only passes because the existing set has 1 admin and `MAX_SUBNET_ADMINS = 10`, so `1 + 1 = 2 ≤ 10`. A test demonstrating the bug:

```rust
#[test]
fn adding_existing_admin_when_at_max_should_succeed() {
    let subnet_id = subnet_test_id(1);
    let mut registry = prepare_registry_for_update_subnet_admins_test(subnet_id);

    // Fill to MAX_SUBNET_ADMINS (10)
    let mut users: Vec<PrincipalId> = (0..MAX_SUBNET_ADMINS)
        .map(|i| user_test_id(100 + i as u64).get())
        .collect();
    registry.do_update_subnet_admins(UpdateSubnetAdminsPayload {
        subnet_id,
        operation_type: Some(OperationType::Add(users.clone())),
    });

    // Re-adding an existing admin should be a no-op, not a panic
    // BUG: this panics with "Too many subnet admins. Provided: 1, Existing: 10, Max allowed: 10."
    registry.do_update_subnet_admins(UpdateSubnetAdminsPayload {
        subnet_id,
        operation_type: Some(OperationType::Add(vec![users[0]])),
    });
}
``` [6](#0-5)

### Citations

**File:** rs/registry/canister/src/mutations/do_update_subnet_admins.rs (L147-154)
```rust
        match res {
            Ok(()) => {}
            Err(err) => {
                panic!(
                    "{LOG_PREFIX}do_update_subnet_admins: Error while updating subnet admins of {subnet_id}: {err}",
                );
            }
        }
```

**File:** rs/registry/canister/src/mutations/do_update_subnet_admins.rs (L162-188)
```rust
        let deduped_current_subnet_admins = current_subnet_admins
            .into_iter()
            .collect::<HashSet<PrincipalIdPb>>();

        let new_subnet_admins = match operation_type {
            OperationType::Add(principal_ids) => {
                if principal_ids.is_empty() {
                    return Err(UpdateSubnetAdminsError::PrincipalListEmpty);
                }

                if deduped_current_subnet_admins.len() + principal_ids.len() > MAX_SUBNET_ADMINS {
                    return Err(UpdateSubnetAdminsError::TooManySubnetAdmins {
                        provided: principal_ids.len() as u64,
                        existing: deduped_current_subnet_admins.len() as u64,
                        max_allowed: MAX_SUBNET_ADMINS as u64,
                    });
                }

                let deduped_provided_principal_ids = principal_ids
                    .into_iter()
                    .map(PrincipalIdPb::from)
                    .collect::<HashSet<PrincipalIdPb>>();

                deduped_current_subnet_admins
                    .union(&deduped_provided_principal_ids)
                    .cloned()
                    .collect()
```

**File:** rs/registry/canister/src/mutations/do_update_subnet_admins.rs (L484-508)
```rust
    #[test]
    fn can_not_add_existing_subnet_admins() {
        let subnet_id = subnet_test_id(1);
        let mut registry = prepare_registry_for_update_subnet_admins_test(subnet_id);

        let user1 = user_test_id(100).get();
        let payload = UpdateSubnetAdminsPayload {
            subnet_id,
            operation_type: Some(OperationType::Add(vec![user1])),
        };

        registry.do_update_subnet_admins(payload.clone());
        let expected_subnet_admins = vec![PrincipalIdPb::from(user1)];
        assert_updated_subnet_admins_match_expected(
            &registry.get_subnet_or_panic(subnet_id).subnet_admins,
            &expected_subnet_admins,
        );

        // Attempt to add the same user again. Should be a no-op.
        registry.do_update_subnet_admins(payload);
        assert_updated_subnet_admins_match_expected(
            &registry.get_subnet_or_panic(subnet_id).subnet_admins,
            &expected_subnet_admins,
        );
    }
```

**File:** rs/registry/canister/src/invariants/subnet.rs (L1-5)
```rust
use std::{
    collections::{BTreeMap, HashSet},
    convert::TryFrom,
};

```
