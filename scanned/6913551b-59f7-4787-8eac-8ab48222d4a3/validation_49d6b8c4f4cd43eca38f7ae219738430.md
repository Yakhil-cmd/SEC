### Title
Private Custom Section Name Enumeration via `can_read_canister_metadata` Existence Oracle — (`rs/http_endpoints/public/src/read_state.rs`)

---

### Summary

`can_read_canister_metadata` returns `Ok(())` when a custom section does not exist, but returns HTTP 403 when the section exists and is private. This asymmetric response allows any unprivileged caller to determine whether a private custom section with a given name exists on any canister, by observing the difference in HTTP response codes.

---

### Finding Description

The guard function `can_read_canister_metadata` has three distinct outcomes for a non-controller caller: [1](#0-0) 

| Condition | Return |
|---|---|
| Section does not exist | `Ok(())` → HTTP 200 |
| Section exists, `Public` | `Ok(())` → HTTP 200 |
| Section exists, `Private`, caller is not controller | `Err(403)` |

The 403 path is only reachable when the section **exists** and is private. A non-controller can therefore distinguish "private section named X exists" from "no section named X exists" by probing with candidate names and observing whether the response is 200 or 403.

The "mixing" framing in the question (combining `controllers`/`module_hash` paths with a metadata path) is not required to trigger this oracle. A request with only `canister/<C>/metadata/<name>` is sufficient.

Note the contrast with `get_canister_metadata` in the execution environment, which deliberately returns `CanisterMetadataSectionNotFound` for both "section doesn't exist" and "section exists but is private and caller is not controller" — masking existence: [2](#0-1) 

The `read_state` path does not apply this same masking.

---

### Impact Explanation

An unprivileged attacker can enumerate the names of all private custom sections on any canister. In practice, private section names include implementation-revealing strings such as `candid:args` and `motoko:stable-types`: [3](#0-2) 

The **contents** of private sections remain protected. Only the names are leaked. The practical impact is low: an attacker learns which language/framework/toolchain produced the canister, and whether specific named sections exist. No financial loss, no code execution, no state modification is possible.

---

### Likelihood Explanation

The attack requires no special privileges, no keys, and no network-level access beyond the public `/api/v2/canister/<C>/read_state` endpoint. It is trivially scriptable: iterate over a wordlist of candidate section names, send one read_state request per name, collect 403 responses. Rate limiting at the boundary node is the only practical friction.

---

### Recommendation

Apply the same masking used in `get_canister_metadata`: when a section exists and is private and the caller is not a controller, return the same response as when the section does not exist (`Ok(())`, yielding an absent leaf in the certified state tree). This makes the oracle constant with respect to existence:

```rust
// In can_read_canister_metadata, replace the current Err branch:
if custom_section.visibility() == CustomSectionType::Private
    && !canister.system_state.controllers.contains(&user.get())
{
    // Return Ok — the path is simply absent for non-controllers,
    // same as if the section did not exist.
    return Ok(());
}
``` [4](#0-3) 

---

### Proof of Concept

```
# Canister C has a private custom section named "secret-key"
# Attacker is a non-controller

# Probe 1: name that does not exist
POST /api/v2/canister/<C>/read_state
paths = [["canister", <C>, "metadata", "nonexistent"]]
→ HTTP 200, absent leaf  (section does not exist)

# Probe 2: name that exists as private
POST /api/v2/canister/<C>/read_state
paths = [["canister", <C>, "metadata", "secret-key"]]
→ HTTP 403  (section EXISTS and is private)

# Attacker concludes: "secret-key" is a private custom section on canister C.
# Repeat with a wordlist to enumerate all private section names.
```

### Citations

**File:** rs/http_endpoints/public/src/read_state.rs (L606-624)
```rust
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
```

**File:** rs/execution_environment/src/canister_manager.rs (L1160-1182)
```rust
        let custom_section = execution_state
            .metadata
            .get_custom_section(section_name)
            .ok_or(CanisterManagerError::CanisterMetadataSectionNotFound {
                canister_id: canister.canister_id(),
                section_name: section_name.to_string(),
            })?;

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

**File:** rs/execution_environment/tests/hypervisor.rs (L5684-5686)
```rust
    // Custom start=0x00028d98 end=0x00028ddc (size=0x00000044) "icp:public candid:service"
    // Custom start=0x00028de2 end=0x00028dfc (size=0x0000001a) "icp:private candid:args"
    // Custom start=0x00028e02 end=0x00028e30 (size=0x0000002e) "icp:private motoko:stable-types"
```
