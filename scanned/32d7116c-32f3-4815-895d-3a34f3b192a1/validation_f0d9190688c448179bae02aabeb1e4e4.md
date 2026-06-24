### Title
Root Proposal Upgrade Commitment Excludes `arg` — Upgrade Arguments Can Be Falsified - (File: `rs/nns/handlers/root/impl/src/root_proposals.rs`)

### Summary

The NNS root canister's "root proposal" mechanism for upgrading the governance canister commits voters only to the WASM hash (`proposed_wasm_sha`), not to the upgrade arguments (`arg` field of `ChangeCanisterRequest`). A malicious NNS node operator can submit a proposal with a legitimate WASM but arbitrary upgrade arguments, collect votes from other node operators who can only verify the WASM hash, and execute the upgrade with hidden malicious arguments.

### Finding Description

`submit_root_proposal_to_upgrade_governance_canister` in `rs/nns/handlers/root/impl/src/root_proposals.rs` computes the proposal's identity hash from only the WASM module bytes:

```rust
let proposed_wasm_sha = ic_crypto_sha2::Sha256::hash(&request.wasm_module).to_vec();
``` [1](#0-0) 

The full `ChangeCanisterRequest` payload — including the `arg` field — is stored in `proposal.payload` and will be passed verbatim to `change_canister` on execution, but `arg` is never hashed or committed to:

```rust
pub struct ChangeCanisterRequest {
    ...
    pub wasm_module: Vec<u8>,  // hashed → proposed_wasm_sha
    pub arg: Vec<u8>,          // NOT hashed, NOT committed to
}
``` [2](#0-1) 

When other node operators vote, the voting function only accepts and checks a `wasm_sha256` parameter:

```rust
pub async fn vote_on_root_proposal_to_upgrade_governance_canister(
    caller: PrincipalId,
    proposer: PrincipalId,
    wasm_sha256: Vec<u8>,   // only WASM hash — no arg_sha256
    ballot: RootProposalBallot,
``` [3](#0-2) 

The only check performed is:

```rust
if wasm_sha256 != proposal.proposed_wasm_sha { ... }
``` [4](#0-3) 

No check on `arg` is ever performed. Compounding this, `get_pending_root_proposals_to_upgrade_governance_canister` actively strips both `wasm_module` and `arg` from the returned proposals before exposing them to voters:

```rust
p.payload.wasm_module = vec![];
p.payload.arg = vec![];
``` [5](#0-4) 

This means voters have no standard on-chain mechanism to inspect or verify the upgrade arguments they are approving.

On execution, the full payload (including the unverified `arg`) is passed directly to `change_canister`:

```rust
let payload = proposal.payload.clone();
...
let _ = change_canister(payload).await;
``` [6](#0-5) 

The canister entry point in `rs/nns/handlers/root/impl/canister/canister.rs` confirms the voting interface exposes no `arg_sha256`: [7](#0-6) 

### Impact Explanation

The `arg` passed to `install_code` during a governance canister upgrade configures critical governance parameters (voting thresholds, reward rates, neuron parameters, etc.). A malicious proposer can embed arbitrary upgrade arguments that reconfigure the NNS governance canister in ways that were never reviewed or approved by the voting node operators. Since the governance canister controls the entire NNS, this could be used to, for example, lower voting thresholds, grant unauthorized principals elevated permissions, or disable safety checks — all while voters believe they approved a routine WASM upgrade.

### Likelihood Explanation

This requires a single malicious NNS node operator to submit the proposal. The other node operators vote based solely on the WASM hash, which is the only commitment the protocol asks them to make. The standard query interface (`get_pending_root_proposals`) strips the `arg` field, so voters have no in-protocol way to detect the discrepancy. The attack does not require a majority of malicious operators — only one proposer and enough honest operators voting Yes on what appears to be a legitimate WASM upgrade. This is analogous to the original Connext finding, which was also downgraded to Medium because it requires a malicious or compromised governance participant.

### Recommendation

1. Add a `proposed_arg_sha` field to `GovernanceUpgradeRootProposal` computed at submission time:
   ```rust
   let proposed_arg_sha = ic_crypto_sha2::Sha256::hash(&request.arg).to_vec();
   ```
2. Include `arg_sha256` as a required parameter in `vote_on_root_proposal_to_upgrade_governance_canister` and verify it against `proposal.proposed_arg_sha`, mirroring the existing `wasm_sha256` check.
3. Do not strip `arg` from proposals returned by `get_pending_root_proposals_to_upgrade_governance_canister`, or at minimum expose the `arg_sha256` so voters can independently verify the upgrade arguments.

### Proof of Concept

1. Malicious NNS node operator A calls `submit_root_proposal_to_upgrade_governance_canister` with:
   - `wasm_module` = legitimate, publicly audited governance WASM (hash = `H_wasm`)
   - `arg` = malicious Candid-encoded upgrade arguments (e.g., setting `voting_power_economics` to attacker-favorable values)
2. Other node operators query `get_pending_root_proposals_to_upgrade_governance_canister`. They see `proposed_wasm_sha = H_wasm` and `arg = []` (stripped). They verify the WASM independently and find it matches the published source.
3. Node operators call `vote_on_root_proposal_to_upgrade_governance_canister(proposer=A, wasm_sha256=H_wasm, ballot=Yes)`. The canister accepts the vote — no `arg` check exists.
4. Once Byzantine majority is reached, `change_canister(payload)` is called with the full original `ChangeCanisterRequest`, including the malicious `arg`. The governance canister is upgraded with attacker-controlled initialization arguments. [8](#0-7)

### Citations

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L79-104)
```rust
#[derive(Clone, Debug, CandidType, Deserialize)]
pub struct GovernanceUpgradeRootProposal {
    /// The id of the NNS subnet.
    pub nns_subnet_id: SubnetId,
    /// The expected sha256 hash of the governance canister
    /// wasm. This must match the sha of the currently running
    /// governance canister.
    #[serde(with = "serde_bytes")]
    pub current_wasm_sha: Vec<u8>,
    /// The proposal payload to upgrade the governance canister.
    pub payload: ChangeCanisterRequest,
    /// The sha of the binary the proposer wants to upgrade to.
    #[serde(with = "serde_bytes")]
    pub proposed_wasm_sha: Vec<u8>,
    /// The principal id of the proposer (must be one of the node
    /// operators of the NNS subnet according to the registry at
    /// time of submission).
    pub proposer: PrincipalId,
    /// The registry version at which the membership was retrieved
    /// for purposes of tallying votes for this proposal.
    pub subnet_membership_registry_version: u64,
    /// The ballots cast by node operators.
    pub node_operator_ballots: Vec<(PrincipalId, RootProposalBallot)>,
    /// The timestamp, in seconds, at which the proposal was submitted.
    pub submission_timestamp_seconds: u64,
}
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L264-264)
```rust
        let proposed_wasm_sha = ic_crypto_sha2::Sha256::hash(&request.wasm_module).to_vec();
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L303-308)
```rust
pub async fn vote_on_root_proposal_to_upgrade_governance_canister(
    caller: PrincipalId,
    proposer: PrincipalId,
    wasm_sha256: Vec<u8>,
    ballot: RootProposalBallot,
) -> Result<(), String> {
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L354-361)
```rust
        if wasm_sha256 != proposal.proposed_wasm_sha {
            let message = format!(
                "{}The sha of the wasm in the governance upgrade proposal that the voter intends to vote on: {:?}\
                 is not the same as the sha of the wasm: {:?} proposed by: {}", LOG_PREFIX, wasm_sha256,
                proposal.proposed_wasm_sha, proposer);
            println!("{message}");
            return Err(message);
        }
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L407-421)
```rust
        let payload = proposal.payload.clone();
        PROPOSALS.with(|proposals| proposals.borrow_mut().remove(&proposer));
        // Check that the wasm of the governance canister is still the same.

        let current_governance_wasm_sha = get_current_governance_canister_wasm().await;
        if current_governance_wasm_sha != proposal.current_wasm_sha {
            let message = format!(
                "{}Invalid proposal. Expected governance wasm sha must match \
             the currently running governance wasm's sha. Current: {:?}. Expected: {:?}",
                LOG_PREFIX, current_governance_wasm_sha, proposal.current_wasm_sha
            );
            println!("{message}");
            return Err(message);
        }
        let _ = change_canister(payload).await;
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L455-458)
```rust
                let mut p = p.clone();
                p.payload.wasm_module = vec![];
                p.payload.arg = vec![];
                p
```

**File:** rs/nervous_system/root/src/change_canister.rs (L71-82)
```rust
    /// The new wasm module to ship.
    #[serde(with = "serde_bytes")]
    pub wasm_module: Vec<u8>,

    /// If the entire WASM does not fit into the 2 MiB ingress limit, then `wasm_module`
    /// should be empty, and this field should be set instead.
    pub chunked_canister_wasm: Option<ChunkedCanisterWasm>,

    /// The new canister args
    #[serde(with = "serde_bytes")]
    pub arg: Vec<u8>,
}
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L113-126)
```rust
#[update(hidden = true)]
async fn vote_on_root_proposal_to_upgrade_governance_canister(
    proposer: PrincipalId,
    wasm_sha256: serde_bytes::ByteBuf,
    ballot: RootProposalBallot,
) -> Result<(), String> {
    ic_nns_handler_root::root_proposals::vote_on_root_proposal_to_upgrade_governance_canister(
        caller(),
        proposer,
        wasm_sha256.to_vec(),
        ballot,
    )
    .await
}
```
