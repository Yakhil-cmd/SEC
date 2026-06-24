Audit Report

## Title
Private Custom Section Existence Oracle via Differential HTTP Status Codes in `can_read_canister_metadata` — (`rs/http_endpoints/public/src/read_state.rs`)

## Summary
`can_read_canister_metadata` returns `Err(403)` exclusively when a private custom section with the queried name exists and the caller is not a controller. All other states — canister absent, no execution state, section absent, or section public — return `Ok(())`, which results in HTTP 200 with an Absent leaf. This asymmetry allows any unprivileged caller to use differential HTTP status codes as a binary oracle to enumerate the names of private custom sections on any canister.

## Finding Description
The function `can_read_canister_metadata` (lines 593–630) implements the following decision tree:

- Line 601: canister not in state → `Ok(())`
- Line 611: section not found in execution state → `Ok(())`
- Lines 618–623: section found, `Private`, caller not a controller → `Err(HttpError { status: 403, ... })`
- Line 626: section found, `Public` → `Ok(())`
- Line 628: no execution state → `Ok(())`

This function is called at line 453–458 inside `verify_paths`, and the error is propagated directly to the HTTP response at lines 270–282 without any normalisation. The certified state layer is never reached when a 403 is returned, so the response is structurally distinguishable from the 200+Absent response produced in all other cases. An attacker probing `["canister", <id>, "metadata", "<name>"]` receives exactly one distinguishable signal: HTTP 403 iff the section exists and is private.

## Impact Explanation
An unprivileged caller can determine whether any specific private custom section name exists on any canister, without requiring any privilege, key, or governance action. This constitutes an information disclosure against the IC protocol's public HTTP API (`/api/v2/canister/{id}/read_state`, `/api/v3/...`). The impact is scoped to section name existence, not content. This fits the High tier: "Significant boundary/API security impact with concrete user or protocol harm" — private section names are explicitly designated confidential by the `CustomSectionType::Private` visibility flag, and the protocol's own access control model is bypassed at the existence-check layer.

## Likelihood Explanation
The attack requires no privilege. Anonymous identity is accepted by the read_state validator. The probe is a single HTTP POST with a valid CBOR-encoded read_state request. No side effects, no cycles consumed, no rate limiting specific to this path. The `candid` section name is a well-known convention, making at least one private section name trivially guessable across a large fraction of deployed canisters. Repeatability is unlimited.

## Recommendation
Restructure `can_read_canister_metadata` so that a non-controller always receives `Ok(())` for any private section path, regardless of whether the section exists. The certified state tree will naturally return an Absent leaf for paths the caller is not entitled to read, without leaking existence. Concretely: move the controller check before the section lookup, or unconditionally return `Ok(())` for non-controllers and let the state tree handle access control silently.

## Proof of Concept
```
// Step 1: Install a canister with a private custom section named "secret".
// Step 2: As a non-controller (anonymous or any other identity), POST:
//   /api/v2/canister/<canister_id>/read_state
//   paths: [["canister", <canister_id_bytes>, "metadata", "secret"]]
// Expected: HTTP 403 → section "secret" exists and is private.

// Step 3: Query a section name that does not exist, e.g. "nonexistent".
// Expected: HTTP 200 + Absent leaf → section does not exist.

// Step 4: Delete the canister, repeat Step 2.
// Expected: HTTP 200 + Absent leaf → canister/section no longer present.

// The 403→200 transition is a certified oracle for private section existence.
// A deterministic integration test cycling install_code → stop_canister →
// delete_canister while asserting HTTP status codes for a non-controller
// metadata probe reproduces this finding.
```