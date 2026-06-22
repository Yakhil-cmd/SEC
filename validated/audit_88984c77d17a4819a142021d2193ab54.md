The code is fully visible. Let me trace the exact logic before rendering a verdict.

### Title
Private Custom Section Existence Oracle via Differential HTTP Status Codes in `can_read_canister_metadata` — (`rs/http_endpoints/public/src/read_state.rs`)

---

### Summary

`can_read_canister_metadata` returns `Err(403)` only when a private custom section with the queried name actually exists and the caller is a non-controller. In every other case — canister absent, no execution state, section absent, or section is public — it returns `Ok(())`, which propagates to an HTTP 200 with an Absent leaf. This asymmetry lets any unprivileged caller enumerate the names of private custom sections on any canister.

---

### Finding Description

The decision tree in `can_read_canister_metadata` produces exactly one distinguishable error path: [1](#0-0) 

| State | Return value | HTTP outcome |
|---|---|---|
| Canister not in state | `Ok(())` line 601 | 200 + Absent leaf |
| Canister exists, no execution state | `Ok(())` line 628 | 200 + Absent leaf |
| Canister exists, section absent | `Ok(())` line 611 | 200 + Absent leaf |
| Canister exists, section **private**, non-controller | `Err(403)` lines 618–623 | **HTTP 403** |
| Canister exists, section public | `Ok(())` line 626 | 200 + leaf value |

The error is propagated directly to the HTTP response without any normalisation: [2](#0-1) 

So the attacker's oracle is:

- **HTTP 403** → the canister exists, has a Wasm installed, and has a private custom section whose name is exactly the probed string.
- **HTTP 200** → none of the above is true.

The lifecycle-transition variant described in the question is a corollary: cycling a canister through install → delete while probing the same section name produces 403 → 200, confirming the section existed before deletion. But the oracle is equally effective against a live canister without any lifecycle manipulation.

---

### Impact Explanation

An unprivileged caller can:

1. Enumerate whether a specific canister has a private custom section with any chosen name (e.g., `candid`, `dfx`, application-specific secrets stored as section names).
2. Confirm whether a canister is currently installed (has an execution state) vs. empty or deleted, when the section name is known.
3. Distinguish "canister deleted" from "canister never had section X" by comparing responses across time.

The information gained is the *existence* of named private sections, not their content. Impact is scoped to limited information disclosure.

---

### Likelihood Explanation

- Requires no privilege, no key, no governance majority.
- Entry point is the public `/api/v2/canister/{id}/read_state` or `/api/v3/canister/{id}/read_state` endpoint, reachable by any anonymous or authenticated caller.
- The request only needs a valid signature (anonymous identity is accepted by the validator for read_state).
- The probe is a single HTTP POST; no side effects, no cycles consumed.
- Likelihood: **High** for any canister whose private section names are guessable or known (e.g., the `candid` section name is a well-known convention).

---

### Recommendation

Normalise the response for non-controllers regardless of whether the private section exists. The fix is to return `Ok(())` (allowing the request to proceed to the certified state layer, which will return an Absent leaf) whenever the caller is not a controller, rather than returning 403 only when the section is found. Concretely, in `can_read_canister_metadata`, the check should be restructured so that a non-controller always receives `Ok(())` for any private section path — the certified state tree will naturally return Absent for paths the caller is not entitled to read, without leaking existence. [3](#0-2) 

---

### Proof of Concept

```
// Step 1: install a canister with a private custom section named "secret"
// Step 2: as a non-controller, send:
//   POST /api/v2/canister/<canister_id>/read_state
//   paths: [["canister", <canister_id_bytes>, "metadata", "secret"]]
// Observe: HTTP 403 → section "secret" exists and is private

// Step 3: delete the canister (or query before install)
// Step 4: repeat the same read_state request
// Observe: HTTP 200 with Absent leaf → section no longer present

// The transition 403 → 200 is a certified oracle for private section existence
// across the canister lifecycle.
```

A state-machine test cycling a canister through `install_code` → `stop_canister` → `delete_canister` while asserting that the HTTP status code for a non-controller metadata probe is indistinguishable at all lifecycle stages would reproduce this finding deterministically.

### Citations

**File:** rs/http_endpoints/public/src/read_state.rs (L270-282)
```rust
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

**File:** rs/http_endpoints/public/src/read_state.rs (L599-628)
```rust
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
```
