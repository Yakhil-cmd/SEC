### Title
SNS-WASM Canister Allows Duplicate SNS Subnet IDs to Be Added Without Deduplication Check - (`rs/nns/sns-wasm/src/sns_wasm.rs`)

---

### Summary

The `update_sns_subnet_list` function in the SNS-WASM canister appends subnet IDs to an internal `Vec<SubnetId>` without checking for duplicates. A governance-approved NNS proposal can add the same subnet ID multiple times, causing `sns_subnet_ids` to contain duplicate entries. While the current `get_available_sns_subnet` always picks `sns_subnet_ids[0]`, the list is exposed externally and the TODO comment in the code explicitly anticipates future logic that iterates over the list to select subnets based on load. Any such future iteration would count a duplicated subnet multiple times, skewing load-balancing decisions. More concretely, the duplicate entries are already observable via `get_sns_subnet_ids`, and a `retain`-based removal of a subnet ID removes **all** occurrences, meaning a single remove proposal can silently wipe a subnet that was added twice — creating an inconsistency between the intended and actual subnet list.

---

### Finding Description

In `rs/nns/sns-wasm/src/sns_wasm.rs`, the `update_sns_subnet_list` method iterates over `request.sns_subnet_ids_to_add` and unconditionally pushes each entry into `self.sns_subnet_ids`:

```rust
pub fn update_sns_subnet_list(
    &mut self,
    request: UpdateSnsSubnetListRequest,
) -> UpdateSnsSubnetListResponse {
    for subnet_id_to_add in request.sns_subnet_ids_to_add {
        self.sns_subnet_ids.push(SubnetId::new(subnet_id_to_add));  // no dedup check
    }
    for subnet_id_to_remove in request.sns_subnet_ids_to_remove {
        self.sns_subnet_ids
            .retain(|id| id != &SubnetId::new(subnet_id_to_remove));  // removes ALL occurrences
    }
    UpdateSnsSubnetListResponse::ok()
}
```

There is no check that the subnet being added is not already present in `self.sns_subnet_ids`. The same subnet ID can be added via two separate NNS proposals (or a single proposal with a repeated entry in `sns_subnet_ids_to_add`).

The `get_available_sns_subnet` function currently always returns `sns_subnet_ids[0]`, so the immediate routing impact is limited. However:

1. The TODO comment in `get_available_sns_subnet` explicitly anticipates future logic that selects subnets based on deployment counts — any such iteration over the list would count a duplicated subnet multiple times, biasing all SNS deployments toward that subnet.
2. The `retain`-based removal removes **all** occurrences of a subnet ID. If a subnet was added twice and then a single remove proposal is submitted, both entries are silently removed. This means the subnet disappears from the list even though only one "add" was intended to be undone, creating a state inconsistency that is not surfaced as an error.
3. The `get_sns_subnet_ids` query returns the raw list including duplicates, so any external tooling or monitoring that counts distinct subnets will be misled. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

- **Incorrect subnet removal**: A single `UpdateSnsSubnetListRequest` with a subnet in `sns_subnet_ids_to_remove` will silently remove all duplicate entries for that subnet, even if only one was intended to be removed. This can cause a valid SNS deployment subnet to disappear from the list entirely after a routine governance operation, resulting in SNS deployment failures (`"No SNS Subnet is available"`).
- **Future load-balancing skew**: The TODO comment in `get_available_sns_subnet` anticipates future logic that iterates over `sns_subnet_ids` to pick subnets based on load. Duplicates would cause that logic to over-weight the duplicated subnet, concentrating all SNS deployments on it.
- **Misleading state**: `get_sns_subnet_ids` returns duplicates, causing external observers and tooling to see an inflated or incorrect subnet list. [3](#0-2) [4](#0-3) 

---

### Likelihood Explanation

NNS governance proposals to update the SNS subnet list are submitted via `ic-admin` using `propose-to-update-sns-subnet-ids-in-sns-wasm`. The operational scripts in the repository (e.g., `set_sns_wasms_allowed_subnets`) first fetch the current list and then submit a proposal to remove all existing entries and add the new one. If such a script is run twice, or if a proposal is submitted with a repeated subnet ID in `sns_subnet_ids_to_add`, duplicates are silently introduced. This is a realistic operational mistake, analogous to the Hubble finding where running a deployment script twice caused the same AMM to be whitelisted twice. [5](#0-4) 

---

### Recommendation

Change `update_sns_subnet_list` to use a `BTreeSet` or deduplicate on insertion:

```rust
pub fn update_sns_subnet_list(
    &mut self,
    request: UpdateSnsSubnetListRequest,
) -> UpdateSnsSubnetListResponse {
    for subnet_id_to_add in request.sns_subnet_ids_to_add {
        let subnet_id = SubnetId::new(subnet_id_to_add);
        if !self.sns_subnet_ids.contains(&subnet_id) {
            self.sns_subnet_ids.push(subnet_id);
        }
    }
    for subnet_id_to_remove in request.sns_subnet_ids_to_remove {
        self.sns_subnet_ids
            .retain(|id| id != &SubnetId::new(subnet_id_to_remove));
    }
    UpdateSnsSubnetListResponse::ok()
}
```

Alternatively, store `sns_subnet_ids` as a `BTreeSet<SubnetId>` to enforce uniqueness structurally, consistent with how `subnet_types_to_subnets` uses `BTreeSet` in the CMC canister. [6](#0-5) 

---

### Proof of Concept

1. NNS governance submits two sequential `UpdateSnsWasmSnsSubnetIds` proposals, both adding `subnet_A` to `sns_subnet_ids_to_add` with empty `sns_subnet_ids_to_remove`.
2. After both proposals execute, `get_sns_subnet_ids` returns `[subnet_A, subnet_A]`.
3. A third proposal is submitted with `subnet_A` in `sns_subnet_ids_to_remove`.
4. After execution, `get_sns_subnet_ids` returns `[]` — the subnet is gone entirely, even though only one of the two additions was intended to be undone.
5. All subsequent `deploy_new_sns` calls fail with `"No SNS Subnet is available"`. [1](#0-0) [7](#0-6)

### Citations

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L824-826)
```rust
        let subnet_id = thread_safe_sns
            .with(|sns_canister| sns_canister.borrow().get_available_sns_subnet())
            .map_err(validation_deploy_error)?;
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L1632-1639)
```rust
    fn get_available_sns_subnet(&self) -> Result<SubnetId, String> {
        // TODO We need a way to find "available" subnets based on SNS deployments (limiting numbers per Subnet)
        if !self.sns_subnet_ids.is_empty() {
            Ok(self.sns_subnet_ids[0])
        } else {
            Err("No SNS Subnet is available".to_string())
        }
    }
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L1763-1777)
```rust
    pub fn update_sns_subnet_list(
        &mut self,
        request: UpdateSnsSubnetListRequest,
    ) -> UpdateSnsSubnetListResponse {
        for subnet_id_to_add in request.sns_subnet_ids_to_add {
            self.sns_subnet_ids.push(SubnetId::new(subnet_id_to_add));
        }

        for subnet_id_to_remove in request.sns_subnet_ids_to_remove {
            self.sns_subnet_ids
                .retain(|id| id != &SubnetId::new(subnet_id_to_remove));
        }

        UpdateSnsSubnetListResponse::ok()
    }
```

**File:** rs/nns/sns-wasm/canister/canister.rs (L404-414)
```rust
/// Add or remove SNS subnet IDs from the list of subnet IDs that SNS instances will be deployed to
#[update]
fn update_sns_subnet_list(request: UpdateSnsSubnetListRequest) -> UpdateSnsSubnetListResponse {
    if caller() != GOVERNANCE_CANISTER_ID.into() {
        UpdateSnsSubnetListResponse::error(
            "update_sns_subnet_list can only be called by NNS Governance",
        )
    } else {
        SNS_WASM.with(|sns_wasm| sns_wasm.borrow_mut().update_sns_subnet_list(request))
    }
}
```

**File:** rs/nns/cmc/src/main.rs (L700-706)
```rust
        match subnet_types_to_subnets.entry(subnet_type.clone()) {
            Entry::Occupied(mut entry) => {
                print(format!(
                    "[cycles] Adding subnets {subnets:?} to type: {subnet_type}"
                ));
                let existing_subnets = entry.get_mut();
                existing_subnets.extend(subnets);
```
