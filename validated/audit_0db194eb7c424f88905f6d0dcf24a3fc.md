All code references check out. The vulnerability is confirmed. Here is the audit report:

---

Audit Report

## Title
Byzantine Flexible-Committee Member Poisons Entire Response Group via Non-Candid Success Body — (`rs/https_outcalls/consensus/src/payload_builder.rs`)

## Summary
`flexible_ok_responses_into_consensus_response` uses `collect::<Option<_>>()?` to decode all `Success(data)` entries as `CanisterHttpResponsePayload`, causing the entire response group to be silently dropped if any single entry fails Candid decoding. `validate_canister_http_payload_impl` never checks the Candid validity of `Success(data)` bytes, so a Byzantine flex-committee member can submit a correctly-signed `Success(non_candid_bytes)` share that passes all validation, gets included by an honest proposer, and permanently suppresses the canister's HTTP outcall response until timeout.

## Finding Description
**Drop logic in `flexible_ok_responses_into_consensus_response`** (`payload_builder.rs` L1000–1014):
```rust
let payloads: Vec<_> = response_group
    .responses
    .into_iter()
    .filter_map(|entry| match entry.response.content {
        CanisterHttpResponseContent::Success(data) => {
            Some(Decode!(&data, CanisterHttpResponsePayload).ok())
        }
        ...
    })
    .collect::<Option<_>>()?;
```
`filter_map` returns `Some(None)` when `Decode!` fails. `collect::<Option<Vec<_>>>()` short-circuits to `None` on the first `None` element, and `?` propagates it — no `ConsensusResponse` is produced for the entire group.

**Validator never checks Candid encoding** (`payload_builder.rs` L596–663): The flexible-response validation loop checks callback ID consistency, response count bounds, committee membership, no duplicate signers, content hash match, content size match, `is_reject` flag match, and defers signature verification. None of these checks inspect the semantic content of `Success(data)` bytes.

**`validate_flexible_response_with_proof`** (`utils.rs` L147–197) confirms: only structural/cryptographic checks are performed; `Success(data)` bytes are never decoded.

**Exploit path:**
1. Byzantine node is a member of `flex_committee` for a targeted request.
2. It submits `Success(arbitrary_non_candid_bytes)`, signs the share over the correct `crypto_hash` of those bytes.
3. `validate_flexible_response_with_proof` passes: hash matches, size matches, `is_reject` is false, signature is valid.
4. An honest proposer includes this entry alongside honest `Success` entries in a `FlexibleCanisterHttpResponses` group.
5. At `into_messages` time, `flexible_ok_responses_into_consensus_response` attempts to Candid-decode all entries; the Byzantine entry fails, `collect::<Option<_>>()` returns `None`, the group is dropped.
6. The canister's callback is never delivered.

**Existing test confirms the drop** (`tests.rs` L3003–3026): `flexible_ok_responses_into_messages_decode_failure_is_skipped` explicitly asserts `responses.len() == 0` when one entry has invalid Candid — the test treats this as expected behavior, but it is the exact impact the attacker exploits.

## Impact Explanation
The `ConsensusResponse` for the affected callback is never delivered to execution. The canister's outstanding HTTP outcall callback is unresolved until `CANISTER_HTTP_TIMEOUT_INTERVAL` expires, at which point it receives a `SysTransient` reject while burning all cycles allocated for the request. A Byzantine node can repeat this for every flexible outcall it is assigned to, selectively and persistently suppressing HTTP outcall responses for targeted canisters. This constitutes a targeted, repeatable application-level DoS on the HTTP outcalls subsystem — matching the **High** impact class: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

## Likelihood Explanation
Requires control of exactly one subnet node that is a member of the `flex_committee` for the targeted request — well below the consensus fault threshold (f < n/3). The Byzantine node only needs to deviate in the HTTP adapter layer (return arbitrary bytes as the response body) and sign the share over the correct content hash of those bytes. No key theft or majority corruption is needed. The attack is fully deterministic and repeatable for every flexible outcall the Byzantine node is assigned to.

## Recommendation
**Option A (preferred):** Add a Candid decode check inside `validate_canister_http_payload_impl` for each `Success(data)` entry in `flexible_responses` (at `payload_builder.rs` L639–656), rejecting the payload with `InvalidArtifact(InvalidCanisterHttpPayloadReason::...)` if any entry fails to decode as `CanisterHttpResponsePayload`. This catches the malformed payload at validation time before finalization.

**Option B:** Change `flexible_ok_responses_into_consensus_response` to skip (filter out) entries that fail Candid decoding rather than returning `None` for the entire group — consistent with how `Reject` entries are already filtered via `filter_map` returning `None`. This is a weaker fix because it degrades silently at execution time rather than rejecting at validation time.

Option A is safer and should be preferred.

## Proof of Concept
The existing test `flexible_ok_responses_into_messages_decode_failure_is_skipped` (`tests.rs` L3003–3026) already proves the drop half. To prove the full exploit, add a test that:
1. Constructs a `FlexibleCanisterHttpResponses` group with one valid and one non-Candid `Success` entry.
2. Calls `validate_canister_http_payload_impl` with a mock state that has the corresponding `flex_committee` context.
3. Asserts that `validate_canister_http_payload_impl` returns `Ok(())` (validation passes).
4. Calls `into_messages` on the same payload and asserts `responses.len() == 0` (response is dropped).

Steps 3 and 4 together prove that a payload with a non-Candid `Success` entry passes validation and is then silently dropped at delivery time.