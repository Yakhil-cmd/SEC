### Title
Byzantine Peer Can Trigger Spurious Peer Fetches via `StrippedBlockProposal` with `value=None` and Non-Empty `ingress_messages` — (`rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs`)

---

### Summary

The `TryFrom<pb::StrippedBlockProposal>` implementation validates the ingress payload only when `pruned_block_proposal_proto.value` is `Some`. When `value` is `None`, the check passes trivially, but the `ingress_messages` list is still populated from the proto's separate field. A Byzantine peer can exploit this to craft a `StrippedBlockProposal` that passes deserialization, causes `assemble_message` to spawn peer-fetch tasks for every fake ingress ID listed, and only fails (at deserialization) after all those fetches have been awaited.

---

### Finding Description

**Guard in `TryFrom<pb::StrippedBlockProposal>`** (`stripped.rs` lines 75–83):

```rust
if pruned_block_proposal_proto
    .value
    .as_ref()
    .is_some_and(|block| block.ingress_payload.is_some())
{
    return Err(ProxyDecodeError::Other(...));
}
```

`Option::is_some_and` returns `false` when the outer `Option` is `None`. So a proto with `value = None` passes this guard unconditionally. [1](#0-0) 

The `ingress_messages` field is then populated independently from the proto's top-level `ingress_messages` list, not from `value`: [2](#0-1) 

This means a proto with `value = None` and an arbitrarily large `ingress_messages` list produces a structurally valid `StrippedBlockProposal` with non-empty `stripped_ingress_payload.ingress_messages`.

**Peer fetches are triggered before structural validity is confirmed** (`assembler.rs` lines 209–226):

`BlockProposalAssembler::new` seeds `self.ingress_messages` from `stripped_ingress_payload.ingress_messages`, and `missing_stripped_messages()` returns all of them. `assemble_message` then spawns one `get_or_fetch` task per entry — each of which contacts peers over the transport layer — before ever calling `try_assemble()`. [3](#0-2) 

**`try_assemble()` skips reconstruction and fails at deserialization** (`assembler.rs` lines 630–639):

```rust
if let Some(block) = reconstructed_block_proposal_proto.value.as_mut() {
    Self::try_reconstruct_payload(ingress_messages, &mut block.ingress_payload)?;
    ...
}
// value is None → branch not taken
let reconstructed_block_proposal: BlockProposal = reconstructed_block_proposal_proto
    .try_into()
    .map_err(AssemblyError::DeserializationFailed)?;
```

Since `value` is `None`, the `if let Some` branch is skipped entirely, and the subsequent `try_into()` fails with `DeserializationFailed`. The assembly returns `AssembleResult::Unwanted` — but only after all peer fetches have already been awaited. [4](#0-3) 

---

### Impact Explanation

A Byzantine subnet peer can:
1. Advertise a `StrippedConsensusMessageId` matching a real, currently-wanted block (so the bouncer accepts it).
2. Serve a crafted `StrippedBlockProposal` with `value = None` and N fake ingress message IDs.
3. Force the receiving node to spawn N `get_or_fetch` tasks, each of which queries the ingress pool and then contacts random peers over the transport layer.
4. The assembly ultimately fails and returns `Unwanted`, but only after all N fetches complete or the bouncer fires.

The attacker can amplify the effect by maximizing N (the number of fake ingress IDs). The bouncer bounds the duration per block ID, but the attacker can rotate across multiple currently-wanted block IDs. The result is unnecessary outbound RPC traffic and Tokio task/thread consumption on the victim node.

---

### Likelihood Explanation

The attacker must be a Byzantine subnet node (below the BFT fault threshold). Such a node has full knowledge of the current consensus state and can trivially identify wanted block IDs. The crafted proto requires no cryptographic material and passes all existing deserialization guards. The exploit is repeatable across different block heights/rounds as long as the attacker remains a subnet member.

---

### Recommendation

In `TryFrom<pb::StrippedBlockProposal>`, add an explicit consistency check: if `pruned_block_proposal_proto.value` is `None`, then both `ingress_messages` and `stripped_idkg_dealings` must be empty. For example, immediately after the existing `is_some_and` guard:

```rust
if pruned_block_proposal_proto.value.is_none()
    && (!value.ingress_messages.is_empty() || !value.stripped_idkg_dealings.is_empty())
{
    return Err(ProxyDecodeError::Other(String::from(
        "pruned_block_proposal has no value but stripped lists are non-empty",
    )));
}
```

This enforces the invariant that structural validity of the pruned proto is verified before any peer-fetch work is initiated. [5](#0-4) 

---

### Proof of Concept

State-machine test (no network required):

```rust
// Craft a pb::StrippedBlockProposal with value=None but non-empty ingress_messages
let fake_ingress_id = /* any valid pb::StrippedIngressMessage */;
let proto = pb::StrippedBlockProposal {
    pruned_block_proposal: Some(pb::BlockProposal { value: None, .. }),
    ingress_messages: vec![fake_ingress_id; 100],
    unstripped_consensus_message_id: Some(/* real wanted block id */),
    stripped_idkg_dealings: vec![],
};

// TryFrom succeeds today — this is the bug
let stripped = StrippedBlockProposal::try_from(proto).unwrap();

// missing_stripped_messages returns 100 entries
let assembler = BlockProposalAssembler::new(stripped);
assert_eq!(assembler.missing_stripped_messages().len(), 100);
// → 100 get_or_fetch tasks would be spawned before try_assemble() is ever called
```

### Citations

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/types/stripped.rs (L67-131)
```rust
impl TryFrom<pb::StrippedBlockProposal> for StrippedBlockProposal {
    type Error = ProxyDecodeError;

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
}
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L209-226)
```rust
        let mut assembler = BlockProposalAssembler::new(stripped_block_proposal);

        let stripped_message_ids = assembler.missing_stripped_messages();
        // For each stripped object in the message, try to fetch it either from the local pools
        // or from a random peer who is advertising it.
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

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/assembler.rs (L630-639)
```rust
        if let Some(block) = reconstructed_block_proposal_proto.value.as_mut() {
            Self::try_reconstruct_payload(ingress_messages, &mut block.ingress_payload)?;
            if let Some(idkg) = block.idkg_payload.as_mut() {
                Self::try_reconstruct_payload(signed_dealings, idkg)?;
            }
        }

        let reconstructed_block_proposal: BlockProposal = reconstructed_block_proposal_proto
            .try_into()
            .map_err(AssemblyError::DeserializationFailed)?;
```
