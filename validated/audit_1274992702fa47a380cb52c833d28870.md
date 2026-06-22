### Title
Private Canister Metadata Section Existence Enumeration via Differential HTTP Response - (File: `rs/http_endpoints/public/src/read_state.rs`)

---

### Summary

The `read_state` endpoint's `can_read_canister_metadata` function returns a distinct HTTP 403 response when a non-controller requests a **private** custom section that **exists**, but silently returns HTTP 200 (with the path absent in the certified state tree) when the section does not exist. Any unprivileged caller can exploit this differential response to enumerate the names of private custom sections on any canister.

---

### Finding Description

The function `can_read_canister_metadata` in `rs/http_endpoints/public/src/read_state.rs` is invoked during `verify_paths` when a `read_state` request includes a path of the form `["canister", <canister_id>, "metadata", <section_name>]`. [1](#0-0) 

The logic produces two distinct observable outcomes for a non-controller caller:

| Condition | HTTP Response |
|---|---|
| Section **exists** and is `Private`, caller is not a controller | **HTTP 403 FORBIDDEN** with body `"Custom section <name> can only be requested by the controllers of the canister."` |
| Section **does not exist** (or canister has no Wasm) | **HTTP 200 OK** with a certified state tree showing the path as `Absent` | [2](#0-1) 

This is called from `verify_paths` for the `canister/metadata` path arm: [3](#0-2) 

The `read_state` handler itself passes the authenticated (or anonymous) `user` directly into `verify_paths`: [4](#0-3) 

---

### Impact Explanation

An unprivileged attacker can brute-force the names of private custom sections (`icp:private`) on any canister by observing the differential response. Private sections are intended to be readable only by controllers; their **existence** is also meant to be opaque to non-controllers. Common section names such as `candid`, `git_commit_id`, `dfx_version`, and `motoko_version` are well-known and predictable. Confirming their presence leaks information about the canister's internal structure, toolchain, and potentially sensitive interface definitions.

**Impact: 4 / 10** — Information disclosure of private metadata section existence; does not directly expose section content, but narrows the attack surface for further exploitation.

---

### Likelihood Explanation

The `read_state` endpoint is publicly reachable by any boundary/API user without any credentials. The attacker only needs to know (or guess) a canister ID and a candidate section name. Canister IDs are public, and private section names follow predictable conventions (`candid`, `git_commit_id`, etc.). No privileged role, key material, or subnet-majority corruption is required.

**Likelihood: 3 / 10** — Requires knowledge of a canister ID and iterating over a finite, well-known set of section name candidates.

---

### Recommendation

Normalize the response for non-controller callers regardless of whether the private section exists. When a non-controller requests any `icp:private` section path, the handler should return HTTP 200 with the path treated as `Absent` in the certified state tree — identical to the response for a non-existent section. This eliminates the observable difference that enables enumeration.

Concretely, in `can_read_canister_metadata`, replace the 403 error return with `Ok(())` for non-controllers, and rely solely on the certified state tree to return an `Absent` node for private sections when the caller is not a controller (mirroring the behavior already implemented in `get_canister_metadata` in `rs/execution_environment/src/canister_manager.rs`). [5](#0-4) 

---

### Proof of Concept

**Step 1.** Identify a target canister ID (public information).

**Step 2.** Send an anonymous or unprivileged signed `read_state` request to the boundary node:

```
POST /api/v2/canister/<canister_id>/read_state
Content-Type: application/cbor

{
  "request_type": "read_state",
  "sender": <anonymous_principal>,
  "paths": [["canister", <canister_id_bytes>, "metadata", "candid"]],
  "ingress_expiry": <valid_expiry>
}
```

**Step 3.** Observe the response:
- **HTTP 403** → the private section named `"candid"` **exists** on this canister.
- **HTTP 200** with `Absent` in the certified tree → the section does not exist.

**Step 4.** Repeat with candidate names (`git_commit_id`, `dfx_version`, `motoko_version`, etc.) to enumerate all private sections.

This is confirmed by the integration test at: [6](#0-5) 

which explicitly asserts that a non-controller receives HTTP 403 for an existing private section, while a non-existent section returns a successful (200+absent) response at line 528. [7](#0-6)

### Citations

**File:** rs/http_endpoints/public/src/read_state.rs (L269-282)
```rust
        // Verify authorization for requested paths.
        if let Err(HttpError { status, message }) = verify_paths(
            &metrics,
            target,
            version,
            certified_state_reader.get_state(),
            &read_state.source,
            &read_state.paths,
            &targets,
            effective_canister_id.into(),
            nns_subnet_id,
        ) {
            return (status, message).into_response();
        }
```

**File:** rs/http_endpoints/public/src/read_state.rs (L444-459)
```rust
            [b"canister", canister_id, b"metadata", name] if target == Target::Canister => {
                let name = String::from_utf8(Vec::from(*name)).map_err(|err| HttpError {
                    status: StatusCode::BAD_REQUEST,
                    message: format!("Could not parse the custom section name: {err}."),
                })?;
                // Get principal id from byte slice.
                let principal_id = parse_principal_id(canister_id)?;
                // Verify that canister id and effective canister id match.
                verify_principal_ids(&principal_id, &effective_principal_id)?;
                can_read_canister_metadata(
                    user,
                    &CanisterId::unchecked_from_principal(principal_id),
                    &name,
                    state,
                )?;
                metrics.observe_read_state_path(endpoint, "canister_metadata");
```

**File:** rs/http_endpoints/public/src/read_state.rs (L593-630)
```rust
fn can_read_canister_metadata(
    user: &UserId,
    canister_id: &CanisterId,
    custom_section_name: &str,
    state: &ReplicatedState,
) -> Result<(), HttpError> {
    let canister = match state.canister_state(canister_id) {
        Some(canister) => canister,
        None => return Ok(()),
    };

    match &canister.execution_state {
        Some(execution_state) => {
            let custom_section = match execution_state
                .metadata
                .get_custom_section(custom_section_name)
            {
                Some(section) => section,
                None => return Ok(()),
            };

            // Only the controller can request this custom section.
            if custom_section.visibility() == CustomSectionType::Private
                && !canister.system_state.controllers.contains(&user.get())
            {
                return Err(HttpError {
                    status: StatusCode::FORBIDDEN,
                    message: format!(
                        "Custom section {custom_section_name:.100} can only be requested by the controllers of the canister."
                    ),
                });
            }

            Ok(())
        }
        None => Ok(()),
    }
}
```

**File:** rs/execution_environment/src/canister_manager.rs (L1168-1182)
```rust
        let is_sender_controller = canister.controllers().contains(&sender);
        let can_non_controller_read_section = match custom_section.visibility() {
            CustomSectionType::Public => true,
            CustomSectionType::Private => false,
        };
        if is_sender_controller || can_non_controller_read_section {
            Ok(CanisterMetadataResponse::new(
                custom_section.content().to_vec(),
            ))
        } else {
            Err(CanisterManagerError::CanisterMetadataSectionNotFound {
                canister_id: canister.canister_id(),
                section_name: section_name.to_string(),
            })
        }
```

**File:** rs/tests/networking/read_state_test.rs (L520-528)
```rust
    // Non-existing metadata section
    let value = lookup_metadata(
        &env,
        &canister_id,
        "foo".as_bytes(),
        get_identity(),
        endpoint,
    );
    assert_matches!(value, Err(AgentError::LookupPathAbsent(_)));
```

**File:** rs/tests/networking/read_state_test.rs (L570-589)
```rust
        // Anonymous identity
        let res = lookup_metadata(
            &env,
            &canister_id,
            section_name,
            AnonymousIdentity,
            endpoint,
        );
        assert_matches!(res, Err(AgentError::HttpError(payload)) if payload.status == 403);

        // Non-controller identity
        let res = lookup_metadata(
            &env,
            &canister_id,
            section_name,
            random_ed25519_identity(),
            endpoint,
        );
        assert_matches!(res, Err(AgentError::HttpError(payload)) if payload.status == 403);
    }
```
