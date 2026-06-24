### Title
Wrong Validation Check in `compute_new_subnet_admins` Uses Raw Input Count Instead of Final State Count — (`rs/registry/canister/src/mutations/do_update_subnet_admins.rs`)

---

### Summary

In `compute_new_subnet_admins`, the `Add` branch validates the operation by comparing `deduped_current_subnet_admins.len() + principal_ids.len()` against `MAX_SUBNET_ADMINS`. However, `principal_ids` is the raw, **non-deduplicated** input list. Deduplication and overlap-removal happen only *after* the check. This mirrors the original report's pattern exactly: the check uses a variable that is larger than the actual final state, causing legitimate operations to be incorrectly rejected.

---

### Finding Description

In `compute_new_subnet_admins` (`rs/registry/canister/src/mutations/do_update_subnet_admins.rs`, lines 166–188), the `Add` branch performs this guard:

```rust
if deduped_current_subnet_admins.len() + principal_ids.len() > MAX_SUBNET_ADMINS {
    return Err(UpdateSubnetAdminsError::TooManySubnetAdmins { ... });
}
```

`principal_ids` at this point is the raw caller-supplied `Vec<PrincipalId>`. The deduplication step that produces `deduped_provided_principal_ids` (a `HashSet`) occurs **after** the check:

```rust
let deduped_provided_principal_ids = principal_ids
    .into_iter()
    .map(PrincipalIdPb::from)
    .collect::<HashSet<PrincipalIdPb>>();

deduped_current_subnet_admins
    .union(&deduped_provided_principal_ids)
    .cloned()
    .collect()
```

The actual final admin count is `|deduped_current_subnet_admins ∪ deduped_provided_principal_ids|`, which can be strictly less than `deduped_current_subnet_admins.len() + principal_ids.len()` whenever:

1. `principal_ids` contains **duplicate entries**, or
2. `principal_ids` contains **principals already present** in `current_admins`.

The `Remove` branch has an analogous wrong check: it guards `principal_ids.len() > MAX_SUBNET_ADMINS`, comparing the count of principals *to remove* against the maximum admin count. Since the maximum number of admins is `MAX_SUBNET_ADMINS`, this check is logically incoherent — it should guard the final state, not the removal list size. [1](#0-0) 

---

### Impact Explanation

A legitimate `Add` call that would produce a valid final state (≤ `MAX_SUBNET_ADMINS` = 10 admins) is incorrectly rejected whenever the raw input list inflates the count past the limit.

**Concrete example:**
- Current admins: `[user1 … user9]` (9 admins)
- Payload: `Add([user1, user2, user10])` — `user1` and `user2` are already admins; `user10` is new
- Guard fires: `9 + 3 = 12 > 10` → **panic / operation rejected**
- Actual final state: `{user1 … user9, user10}` = 10 admins → **should succeed**

The `Remove` branch's wrong check (`principal_ids.len() > MAX_SUBNET_ADMINS`) causes a symmetric false rejection: a caller supplying more than 10 principals to remove (even if most are not current admins) is rejected, even though the resulting admin set would be valid. [2](#0-1) [3](#0-2) 

---

### Likelihood Explanation

`do_update_subnet_admins` is invoked by the registry canister's `update_subnet_admins` endpoint, which is restricted to the subnet rental canister (`SUBNET_RENTAL_CANISTER_ID`). [4](#0-3) 

The subnet rental canister is a public-facing canister that users interact with to initiate subnet rental agreements. If the subnet rental canister forwards user-supplied principal lists (which may contain duplicates or re-submissions of existing admins) directly to the registry, an unprivileged user can trigger the false rejection. Even without a direct user path, the bug causes the subnet rental canister itself to fail on otherwise valid admin-management operations, breaking the subnet rental workflow.

---

### Recommendation

Compute the actual final set **before** the guard, and check its cardinality:

```rust
OperationType::Add(principal_ids) => {
    if principal_ids.is_empty() {
        return Err(UpdateSubnetAdminsError::PrincipalListEmpty);
    }

    let deduped_provided = principal_ids
        .into_iter()
        .map(PrincipalIdPb::from)
        .collect::<HashSet<PrincipalIdPb>>();

    let new_admins: Vec<PrincipalIdPb> = deduped_current_subnet_admins
        .union(&deduped_provided)
        .cloned()
        .collect();

    if new_admins.len() > MAX_SUBNET_ADMINS {
        return Err(UpdateSubnetAdminsError::TooManySubnetAdmins {
            provided: deduped_provided.len() as u64,
            existing: deduped_current_subnet_admins.len() as u64,
            max_allowed: MAX_SUBNET_ADMINS as u64,
        });
    }

    Ok(new_admins)
}
```

For `Remove`, the guard `principal_ids.len() > MAX_SUBNET_ADMINS` is logically meaningless and should be removed entirely (removal can never increase the admin count above the maximum). [5](#0-4) 

---

### Proof of Concept

```
State: subnet has 9 admins [A1 … A9], MAX_SUBNET_ADMINS = 10

Call: do_update_subnet_admins(Add([A1, A2, A10]))
  // A1, A2 already admins; A10 is new → final set = {A1…A9, A10} = 10 ≤ 10

Guard check: deduped_current(9) + principal_ids.len()(3) = 12 > 10
  → TooManySubnetAdmins { provided: 3, existing: 9, max_allowed: 10 }
  → PANIC / operation rejected

Expected: operation succeeds, final admin list = [A1…A9, A10]
``` [6](#0-5) [7](#0-6)

### Citations

**File:** rs/registry/canister/src/mutations/do_update_subnet_admins.rs (L157-217)
```rust
    fn compute_new_subnet_admins(
        &self,
        current_subnet_admins: Vec<PrincipalIdPb>,
        operation_type: OperationType,
    ) -> Result<Vec<PrincipalIdPb>, UpdateSubnetAdminsError> {
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
            }
            OperationType::Remove(principal_ids) => {
                if principal_ids.is_empty() {
                    return Err(UpdateSubnetAdminsError::PrincipalListEmpty);
                }

                if principal_ids.len() > MAX_SUBNET_ADMINS {
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
                    .difference(&deduped_provided_principal_ids)
                    .cloned()
                    .collect()
            }
            OperationType::Clear(_) => vec![],
        };

        Ok(new_subnet_admins)
    }
```

**File:** rs/registry/canister/src/mutations/do_update_subnet_admins.rs (L426-462)
```rust
    #[test]
    #[should_panic(expected = "Too many subnet admins. Provided: 3, Existing: 9, Max allowed: 10.")]
    fn can_not_add_too_many_subnet_admins_with_existing_ones() {
        let subnet_id = subnet_test_id(1);
        let mut registry = prepare_registry_for_update_subnet_admins_test(subnet_id);

        let mut users_to_add = Vec::new();
        let mut expected_subnet_admins = Vec::new();
        for i in 0..(MAX_SUBNET_ADMINS - 1) {
            let principal = user_test_id(100 + i as u64).get();
            users_to_add.push(principal);
            expected_subnet_admins.push(PrincipalIdPb::from(principal));
        }

        let payload = UpdateSubnetAdminsPayload {
            subnet_id,
            operation_type: Some(OperationType::Add(users_to_add)),
        };
        registry.do_update_subnet_admins(payload);
        assert_updated_subnet_admins_match_expected(
            &registry.get_subnet_or_panic(subnet_id).subnet_admins,
            &expected_subnet_admins,
        );

        let mut users_to_add = Vec::new();
        for i in 0..3 {
            users_to_add.push(user_test_id(200 + i as u64).get());
        }

        let payload = UpdateSubnetAdminsPayload {
            subnet_id,
            operation_type: Some(OperationType::Add(users_to_add)),
        };

        registry.do_update_subnet_admins(payload);
    }

```

**File:** rs/registry/canister/tests/update_subnet_admins.rs (L90-113)
```rust
        // An attacker got a canister that is trying to pass for the subnet rental
        // canister...
        let attacker_canister = set_up_universal_canister(&runtime).await;
        // ... but thankfully, it does not have the right ID
        assert_ne!(
            attacker_canister.canister_id(),
            ic_nns_constants::SUBNET_RENTAL_CANISTER_ID,
        );

        // The attacker canister tries to update a subnet's subnet admins, bypassing
        // the subnet rental canister. This should have no effect.
        let payload = UpdateSubnetAdminsPayload {
            subnet_id,
            operation_type: Some(OperationType::Add(vec![user_test_id(100).get()])),
        };
        assert!(
            !forward_call_via_universal_canister(
                &attacker_canister,
                &registry,
                "update_subnet_admins",
                Encode!(&payload).unwrap()
            )
            .await
        );
```
