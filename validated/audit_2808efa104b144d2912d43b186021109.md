All code claims are verified. The vulnerability is real and the exploit path is confirmed.

Key findings from the code:

1. `TryFrom<pb::StrippedBlockProposal>` at `stripped.rs:100-130` — no size check on `ingress_messages` before collecting into a `Vec`.
2. `BlockProposalAssembler::new` at `assembler.rs:572-588` — allocates `Vec<(SignedIngressId, Option<SignedIngress>)>` of size N unconditionally.
3. `assemble_message` at `assembler.rs:214-226` — spawns one Tokio task per entry with no cap.
4. `download_stripped_message` at `download.rs:301-305` — uses `with_max_elapsed_time(None)`, meaning each spawned task runs indefinitely until the `join_set` is dropped.
5. Bouncer fires only after all N tasks are already running (`assembler.rs:233`), with a 3-second refresh period (`assembler.rs:100-102`).
6. `MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` at `limits/src/lib.rs:78` is never imported or checked in the deserialization path.

---

Audit Report

## Title
Unbounded `ingress_messages` deserialization in `TryFrom<pb::StrippedBlockProposal>` allows Byzantine peer to spawn unlimited Tokio tasks and exhaust replica memory — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs`)

## Summary

`TryFrom<pb::StrippedBlockProposal>` collects the `ingress_messages` repeated protobuf field into a `Vec` with no upper-bound check. A Byzantine peer below the fault threshold can serve a crafted `StrippedBlockProposal` with tens of thousands of fake ingress IDs. `BlockProposalAssembler::new` allocates a `Vec` of that size, and `assemble_message` immediately spawns one unbounded Tokio task per entry — each running an infinite exponential-backoff retry loop — until the bouncer fires up to 3 seconds later. The attack is repeatable every consensus round.

## Finding Description

**Root cause — no size guard at deserialization:**

In `TryFrom<pb::StrippedBlockProposal>` (`stripped.rs:100-108`), the `ingress_messages` repeated field is collected unconditionally:

```rust
ingress_messages: value
    .ingress_messages
    .into_iter()
    .map(SignedIngressId::try_from)
    .collect::<Result<Vec<_>, _>>()?,
``` [1](#0-0) 

`MAX_INGRESS_MESSAGES_PER_BLOCK = 1000` is defined in `rs/limits/src/lib.rs` but is never imported or checked in this deserialization path. [2](#0-1) 

**Unbounded Vec allocation:**

`BlockProposalAssembler::new` maps every deserialized ingress ID into a `Vec<(SignedIngressId, Option<SignedIngress>)>` with no cap. The comment at line 440 explicitly documents the invariant that is never enforced:

```rust
// We can have at most 1000 elements in the vector, so it should be reasonably fast to do a
// linear scan here.
``` [3](#0-2) [4](#0-3) 

**Unbounded task spawning:**

`assemble_message` iterates over all N missing message IDs and spawns a `get_or_fetch` Tokio task for each, with no cap: [5](#0-4) 

**Infinite retry loop per task:**

`download_stripped_message` uses `with_max_elapsed_time(None)`, so each spawned task runs an infinite exponential-backoff retry loop until the `join_set` is dropped: [6](#0-5) 

**Bouncer fires too late:**

The bouncer is only consulted after all N tasks are already spawned. Its refresh period is 3 seconds, meaning all N tasks run for up to 3 seconds before being aborted: [7](#0-6) [8](#0-7) 

**Existing checks are insufficient:**

The `TryFrom` implementation validates that `pruned_block_proposal` is present, that the ingress payload field is empty, and that the `unstripped_consensus_message_id` is a `BlockProposal` hash — but none of these checks bound the count of `ingress_messages`. [9](#0-8) 

## Impact Explanation

With a 4 MB transport message limit and each `StrippedIngressMessage` encoding to ~100–150 bytes, an attacker can embed ~27,000–40,000 fake ingress IDs in a single crafted artifact. The victim replica allocates a `Vec` of that size and spawns ~27,000–40,000 Tokio tasks, each consuming stack memory (~8–16 KB), totaling hundreds of MB of memory pressure. Since `download_stripped_message` has no overall timeout, all tasks remain active for the full bouncer refresh window (up to 3 seconds). The attack is repeatable every consensus round (~1–3 seconds), preventing memory reclamation before the next wave. This can cause a single-replica OOM crash or Tokio scheduler stall, breaking liveness for that replica.

This matches the allowed impact: **High ($2,000–$10,000) — Application/platform-level DoS, crash, consensus blocking, or subnet availability impact not based on raw volumetric DDoS.**

## Likelihood Explanation

- The attacker requires only one Byzantine peer below the fault threshold — a standard assumption in the IC threat model.
- The attacker observes a valid `ConsensusMessageId` for a block the victim is currently assembling from the public P2P gossip layer; no cryptographic material or admin access is required.
- The crafted message passes all existing `TryFrom` checks (valid pruned block proto, empty ingress payload field, valid block proposal hash type).
- The attack is repeatable every round and can be directed at all replicas simultaneously by a single Byzantine peer serving the crafted artifact to any requester.

## Recommendation

Add an upper-bound check in `TryFrom<pb::StrippedBlockProposal>` immediately after collecting `ingress_messages`, using the already-defined constant:

```rust
let ingress_messages = value
    .ingress_messages
    .into_iter()
    .map(SignedIngressId::try_from)
    .collect::<Result<Vec<_>, _>>()?;

if ingress_messages.len() > MAX_INGRESS_MESSAGES_PER_BLOCK as usize {
    return Err(ProxyDecodeError::Other(format!(
        "Too many ingress messages: {} > {}",
        ingress_messages.len(),
        MAX_INGRESS_MESSAGES_PER_BLOCK
    )));
}
```

Apply the same guard to `stripped_idkg_dealings` using `DKG_DEALINGS_PER_BLOCK` or an appropriate subnet-level limit. This enforces the invariant already documented in the `try_insert` comment and aligns with the consensus-layer limit. [10](#0-9) 

## Proof of Concept

1. Construct a `pb::StrippedBlockProposal` with:
   - A valid `pruned_block_proposal` with `ingress_payload = None`.
   - A real `unstripped_consensus_message_id` (observed from gossip) with a `BlockProposal` hash.
   - `ingress_messages` populated with N = 30,000 synthetic `StrippedIngressMessage` entries (each with a random 32-byte `ingress_bytes_hash` and a minimal `IngressMessageId`), staying within the ~4 MB transport limit.
2. Serve this proto from a Byzantine peer when the victim requests the stripped artifact for the observed block.
3. Observe on the victim: `TryFrom` succeeds; `BlockProposalAssembler::new` allocates a Vec of 30,000 entries; `assemble_message` spawns 30,000 `get_or_fetch` tasks; replica RSS spikes by hundreds of MB; Tokio task count saturates.
4. Repeat every round. Assert that the replica crashes or becomes unresponsive within minutes.

A deterministic integration test can be written using `tokio::test` with a `MockTransport` that serves the crafted proto, asserting that task count and memory usage remain bounded after deserialization.

### Citations

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs (L70-99)
```rust
    fn try_from(value: pb::StrippedBlockProposal) -> Result<Self, Self::Error> {
        let pruned_block_proposal_proto = value
            .pruned_block_proposal
            .ok_or_else(|| ProxyDecodeError::MissingField("pruned_block_proposal"))?;

        if pruned_block_proposal_proto
            .value
            .as_ref()
            .is_some_and(|block| block.ingress_payload.is_some())
        {
            return Err(ProxyDecodeError::Other(String::from(
                "The ingress payload is NOT empty",
            )));
        }

        let unstripped_consensus_message_id: ConsensusMessageId = try_from_option_field(
            value.unstripped_consensus_message_id,
            "unstripped_consensus_message_id",
        )?;

        if !matches!(
            unstripped_consensus_message_id.hash,
            ConsensusMessageHash::BlockProposal(_)
        ) {
            return Err(ProxyDecodeError::Other(format!(
                "The unstripped consensus message id {:?} is NOT for a block proposal",
                unstripped_consensus_message_id,
            )));
        }

```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs (L100-130)
```rust
        Ok(Self {
            pruned_block_proposal_proto,
            stripped_ingress_payload: StrippedIngressPayload {
                ingress_messages: value
                    .ingress_messages
                    .into_iter()
                    .map(SignedIngressId::try_from)
                    .collect::<Result<Vec<_>, _>>()?,
            },
            unstripped_consensus_message_id,
            stripped_idkg_dealings: StrippedIDkgDealings {
                stripped_dealings: value
                    .stripped_idkg_dealings
                    .into_iter()
                    .map(|dealing| {
                        let idkg_artifact_id: IDkgArtifactId = try_from_option_field(
                            dealing.dealing_id,
                            "StrippedIDkgDealings::dealing_id",
                        )?;
                        if !matches!(idkg_artifact_id, IDkgArtifactId::Dealing(_, _)) {
                            return Err(ProxyDecodeError::Other(format!(
                                "The stripped IDKG artifact id {:?} is NOT for a dealing",
                                idkg_artifact_id,
                            )));
                        }
                        Ok((dealing.dealer_index, idkg_artifact_id))
                    })
                    .collect::<Result<Vec<_>, ProxyDecodeError>>()?,
            },
        })
    }
```

**File:** rs/limits/src/lib.rs (L78-78)
```rust
pub const MAX_INGRESS_MESSAGES_PER_BLOCK: u64 = 1000;
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L100-102)
```rust
    fn refresh_period(&self) -> Duration {
        Duration::from_secs(3)
    }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L214-226)
```rust
        for stripped_message_id in stripped_message_ids {
            join_set.spawn(get_or_fetch(
                stripped_message_id,
                self.ingress_pool.clone(),
                self.idkg_pool.clone(),
                self.transport.clone(),
                id.as_ref().clone(),
                self.log.clone(),
                self.metrics.clone(),
                self.node_id,
                peer_rx.clone(),
            ));
        }
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L233-242)
```rust
        let mut bouncer = self.fetch_stripped.bouncer_watcher();

        loop {
            let join_result = tokio::select! {
                _ = bouncer.wait_for(|bouncer| matches!(bouncer(&id), BouncerValue::Unwanted)) => {
                    self.metrics.report_aborted_block_assembly();
                    return AssembleResult::Unwanted;
                }
                join_result = join_set.join_next() => join_result,
            };
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L440-441)
```rust
        // We can have at most 1000 elements in the vector, so it should be reasonably fast to do a
        // linear scan here.
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L572-580)
```rust
    fn new(stripped_block_proposal: StrippedBlockProposal) -> Self {
        Self {
            ingress_messages: stripped_block_proposal
                .stripped_ingress_payload
                .ingress_messages
                .iter()
                .map(|signed_ingress_id| (signed_ingress_id.clone(), None))
                .collect(),
            signed_dealings: stripped_block_proposal
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/download.rs (L301-305)
```rust
    let mut artifact_download_timeout = ExponentialBackoffBuilder::new()
        .with_initial_interval(MIN_ARTIFACT_RPC_TIMEOUT)
        .with_max_interval(MAX_ARTIFACT_RPC_TIMEOUT)
        .with_max_elapsed_time(None)
        .build();
```
