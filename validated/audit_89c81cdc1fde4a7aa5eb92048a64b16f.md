### Title
Equivocation Proof Has No Economic Consequence — Risk-Free Liveness Attack by Node Operators - (File: rs/consensus/src/consensus/validator.rs)

---

### Summary

The Internet Computer detects block-maker equivocation via `EquivocationProof` artifacts, but the proof has zero downstream consequence in governance or the reward system. An existing node operator can configure their replica to equivocate (propose two cryptographically distinct blocks at the same height) and suffer no slashing, no node removal, and no direct reward penalty tied to the equivocation event itself. The proof is generated, stored in the validated pool, and then silently purged after finalization — never surfaced to NNS governance.

---

### Finding Description

**Root cause — `validate_blocks` in `rs/consensus/src/consensus/validator.rs`:**

When the validator detects that a block maker has signed two different blocks at the same height, it constructs an `EquivocationProof` and pushes it into the validated artifact pool: [1](#0-0) 

The proof is then used only to disqualify the offending rank locally for that height via `get_disqualified_ranks`: [2](#0-1) 

After the height is finalized, the purger discards all equivocation proofs at or below the finalized height: [3](#0-2) 

**No path from `EquivocationProof` to governance or rewards:**

Searching the entire NNS governance codebase (`rs/nns/governance/`) yields zero references to `EquivocationProof`. The `do_change_subnet_membership` mutation — the only mechanism to remove a node from a subnet — is exclusively triggered by an NNS proposal, never automatically by an equivocation event: [4](#0-3) 

**Performance-based rewards do not capture equivocation directly:**

The performance-based reward algorithm penalizes nodes based on `num_blocks_failed` (i.e., rounds where the node was the scheduled block maker but the subnet accepted a higher-rank block). An equivocating node whose block is disqualified will appear as a failed block maker in `BlockmakerMetrics`, so the performance algorithm may apply a reward reduction of up to 80%: [5](#0-4) 

However, this is an indirect, soft penalty — not a slashing event. The node remains in the subnet, continues to participate in consensus, and the equivocation proof itself is never consulted by the reward system.

**`EquivocationProof` type definition:** [6](#0-5) 

---

### Impact Explanation

A node operator who controls a subnet replica can equivocate at will:

1. **Liveness delay**: The equivocating node's rank is disqualified for that height, forcing the subnet to wait for the rank-based delay of the next block maker. Repeated equivocation by the rank-0 block maker at every height it is scheduled forces the subnet to always fall back to rank-1 or higher, measurably increasing block latency.

2. **No slashing**: The IC has no staking/slashing mechanism for node operators. The `EquivocationProof` is cryptographic evidence of misbehavior but is never acted upon economically.

3. **No automatic removal**: Node removal requires a successful NNS governance proposal. There is no automated path from a validated `EquivocationProof` to a `ChangeSubnetMembership` proposal.

4. **Reward impact is bounded and indirect**: The performance-based algorithm caps reward reduction at 80% and only applies it if the equivocating node's relative failure rate exceeds the subnet percentile threshold. A node that equivocates selectively (e.g., only when it is rank-0) may stay below the penalty threshold.

---

### Likelihood Explanation

The attacker must be an existing, NNS-approved node operator — a privileged role. However, the threat model of the IC explicitly assumes that up to `f` nodes (where `f = floor((n-1)/3)`) may be Byzantine. The IC's own malicious consensus test harness (`rs/tests/consensus/safety_test.rs`) exercises exactly this scenario: [7](#0-6) 

A node operator who has been approved but later becomes adversarial (economic incentive misalignment, key compromise, or deliberate griefing) can exploit this gap. The barrier is governance approval, not technical difficulty.

---

### Recommendation

1. **Surface `EquivocationProof` to governance**: When a validated `EquivocationProof` is included in a finalized block (or reaches a quorum of replicas), automatically submit or flag a `ChangeSubnetMembership` proposal to remove the offending node.

2. **Tie equivocation directly to reward reduction**: Extend the performance-based reward algorithm to consult the equivocation proof record, applying a deterministic reward reduction independent of the failure-rate heuristic.

3. **Persist equivocation evidence beyond finalization**: Instead of purging `EquivocationProof` artifacts after finalization, archive them in replicated state so they can be audited and acted upon by governance tooling.

4. **Document the guarded-launch posture**: Until slashing is implemented, publish explicit bounds on the economic risk of equivocation under the current model, analogous to the external report's recommendation to "document the process of a guarded launch."

---

### Proof of Concept

1. A node operator modifies their replica binary to invoke `maliciously_propose_equivocating_blocks` (already implemented in `rs/consensus/src/consensus/malicious_consensus.rs`): [8](#0-7) 

2. The validator on every honest peer detects the two distinct block hashes from the same signer and rank, constructs an `EquivocationProof`, and adds it to the validated pool.

3. The offending rank is disqualified for that height; the subnet falls back to the rank-1 block maker, incurring a rank-based delay.

4. After finalization, the purger removes the proof: [9](#0-8) 

5. No NNS proposal is created. No reward reduction is applied via the equivocation proof. The node operator's monthly ICP reward is computed solely from `BlockmakerMetrics` failure rates, which may or may not exceed the penalty threshold depending on how selectively the operator equivocates.

6. The node operator repeats indefinitely, causing sustained liveness degradation on the subnet at zero guaranteed economic cost.

### Citations

**File:** rs/consensus/src/consensus/validator.rs (L601-628)
```rust
/// Returns rank map of disqualified ranks in the given range. A rank is
/// considered disqualified at height h, if there exists an equivocation
/// proof for it at that height.
fn get_disqualified_ranks(
    pool: &PoolReader<'_>,
    membership: &Membership,
    cfg: ReplicaConfig,
    range: HeightRange,
) -> RankMap {
    let mut rank_map = RankMap::new(cfg.subnet_id);
    for proof in pool
        .pool()
        .validated()
        .equivocation_proof()
        .get_by_height_range(range)
    {
        let Ok(previous_beacon) = get_previous_beacon(pool, proof.height) else {
            continue;
        };
        let Ok(Some(rank)) =
            membership.get_block_maker_rank(proof.height, &previous_beacon, proof.signer)
        else {
            continue;
        };
        let (first_metadata, _) = proof.into_signed_metadata();
        rank_map.add_from_parts(rank, first_metadata);
    }
    rank_map
```

**File:** rs/consensus/src/consensus/validator.rs (L1116-1143)
```rust
            // Disqualify rank if equivocation was found. If there already
            // exists a validated block of the same rank as the current
            // proposal, we must generate an equivocation proof.
            if let Some(existing_metadata) =
                valid_ranks.get_block_metadata(proposal.height(), proposal.rank())
            {
                // Ensure the proposal has a different hash from the validated
                // block of same rank. Then we can construct the proof.
                if proposal.content.get_hash().get_ref() != existing_metadata.content.hash() {
                    let proof = EquivocationProof {
                        signer: proposal.signature.signer,
                        version: proposal.content.version().clone(),
                        height: proposal.height(),
                        subnet_id: self.replica_config.subnet_id,
                        hash1: proposal.content.get_hash().clone(),
                        signature1: proposal.signature.signature.clone(),
                        hash2: CryptoHashOf::new(existing_metadata.content.hash().clone()),
                        signature2: existing_metadata.signature.signature.clone(),
                    };
                    warn!(self.log, "Equivocation found. Proof: {:?}", proof,);
                    change_set.push(ChangeAction::AddToValidated(ValidatedArtifact {
                        msg: ConsensusMessage::EquivocationProof(proof),
                        timestamp: self.time_source.get_relative_time(),
                    }));
                    valid_ranks.remove(proposal.height(), proposal.rank());
                    disqualified_ranks.add(&proposal);
                    // Blocks from disqualified ranks can be ignored at this point
                    continue;
```

**File:** rs/consensus/src/consensus/purger.rs (L661-703)
```rust
    #[test]
    fn test_purge_equivocation_proofs() {
        ic_test_utilities::artifact_pool_config::with_test_pool_config(|pool_config| {
            let Dependencies {
                mut pool,
                state_manager,
                replica_config,
                registry,
                ..
            } = dependencies(pool_config, 3);
            state_manager
                .get_mut()
                .expect_latest_state_height()
                .returning(|| Height::new(0));
            let purger = Purger::new(
                replica_config,
                state_manager,
                Arc::new(FakeMessageRouting::new()),
                registry,
                no_op_logger(),
                MetricsRegistry::new(),
            );

            for i in 1..10 {
                pool.insert_validated(pool.make_equivocation_proof(Rank(0), Height::new(i)));
                pool.advance_round_normal_operation();
            }

            // Add an additional equivocation proof above the finalized height
            pool.insert_validated(pool.make_next_beacon());
            let block = pool.make_next_block();
            pool.insert_validated(block.clone());
            pool.notarize(&block);
            pool.insert_validated(pool.make_equivocation_proof(Rank(0), Height::new(11)));

            // We expect to purge equivocation proofs below AND at the finalized height.
            let pool_reader = PoolReader::new(&pool);
            let changeset = purger.on_state_change(&pool_reader);
            assert!(changeset.contains(&ChangeAction::PurgeValidatedOfTypeBelow(
                PurgeableArtifactType::EquivocationProof,
                Height::new(10),
            )));
        })
```

**File:** rs/registry/canister/src/mutations/do_change_subnet_membership.rs (L14-68)
```rust
impl Registry {
    /// Changes membership of nodes in a subnet record in the registry.
    ///
    /// This method is called by the governance canister, after a proposal
    /// for modifying a subnet by changing the membership (adding/removing) has been accepted.
    pub fn do_change_subnet_membership(&mut self, payload: ChangeSubnetMembershipPayload) {
        println!("{LOG_PREFIX}do_change_subnet_membership started: {payload:?}");

        let nodes_to_add = payload.node_ids_add.clone();
        let subnet_id = SubnetId::from(payload.subnet_id);
        let mut subnet_record = self.get_subnet_or_panic(subnet_id);

        let current_subnet_nodes: Vec<NodeId> = subnet_record
            .membership
            .iter()
            .map(|bytes| NodeId::from(PrincipalId::try_from(bytes).unwrap()))
            .collect();

        // Verify that nodes requested to be removed belong to the subnet provided in the payload
        if !payload
            .node_ids_remove
            .iter()
            .all(|n| current_subnet_nodes.contains(n))
        {
            panic!("Nodes that should be removed do not belong to the provided subnet.")
        }

        // Calculate a complete list of nodes in this subnet after the change of subnet membership is executed
        let subnet_membership_after_change = nodes_to_add
            .iter()
            .cloned()
            .chain(current_subnet_nodes)
            .filter(|node_id_in_subnet| {
                payload
                    .node_ids_remove
                    .iter()
                    .all(|node_id_to_remove| node_id_in_subnet != node_id_to_remove)
            })
            .collect();

        self.replace_subnet_record_membership(
            subnet_id,
            &mut subnet_record,
            subnet_membership_after_change,
        );
        let mutations = vec![upsert(
            make_subnet_record_key(subnet_id),
            subnet_record.encode_to_vec(),
        )];

        // Check the invariants and apply the mutations if invariants are satisfied
        self.maybe_apply_mutation_internal(mutations);

        println!("{LOG_PREFIX}do_change_subnet_membership finished: {payload:?}");
    }
```

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs (L92-109)
```rust
trait PerformanceBasedAlgorithm: AlgorithmVersion {
    /// The percentile used to calculate the failure rate for a subnet.
    const SUBNET_FAILURE_RATE_PERCENTILE: f64;

    /// The minimum and maximum failure rates for a node.
    /// Nodes with a failure rate below `MIN_FAILURE_RATE` will not be penalized.
    const MIN_FAILURE_RATE: Decimal;

    /// The maximum failure rate for a node.
    /// Nodes with a failure rate above `MAX_FAILURE_RATE` will be penalized with `MAX_REWARDS_REDUCTION`.
    const MAX_FAILURE_RATE: Decimal;

    /// The minimum rewards reduction for a node.
    const MIN_REWARDS_REDUCTION: Decimal;

    /// The maximum rewards reduction for a node.
    const MAX_REWARDS_REDUCTION: Decimal;

```

**File:** rs/types/types/src/consensus.rs (L818-831)
```rust
/// A proof that shows a block maker has produced equivocating blocks.
#[derive(Clone, Eq, PartialEq, Hash, Debug, Deserialize, Serialize)]
#[cfg_attr(test, derive(ExhaustiveSet))]
pub struct EquivocationProof {
    pub signer: NodeId,
    pub version: ReplicaVersion,
    pub height: Height,
    pub subnet_id: SubnetId,
    // Hash and signature of the first and second blocks
    pub hash1: CryptoHashOf<Block>,
    pub signature1: BasicSigOf<BlockMetadata>,
    pub hash2: CryptoHashOf<Block>,
    pub signature2: BasicSigOf<BlockMetadata>,
}
```

**File:** rs/tests/consensus/safety_test.rs (L41-55)
```rust
fn setup(env: TestEnv) {
    let malicious_behavior = MaliciousBehavior::new(true)
        .set_maliciously_propose_empty_blocks()
        .set_maliciously_notarize_all()
        .set_maliciously_finalize_all();

    InternetComputer::new()
        .add_subnet(
            Subnet::new(SubnetType::System)
                .add_nodes(3)
                .add_malicious_nodes(1, malicious_behavior),
        )
        .setup_and_start(&env)
        .expect("failed to setup IC under test");
}
```

**File:** rs/consensus/src/consensus/malicious_consensus.rs (L303-328)
```rust
        if malicious_flags.maliciously_propose_equivocating_blocks
            || malicious_flags.maliciously_propose_empty_blocks
        {
            // If maliciously_propose_empty_blocks is enabled, we should remove non-empty
            // block proposals by the honest code from the changeset.
            if malicious_flags.maliciously_propose_empty_blocks {
                changeset.retain(|change_action| {
                    !matches!(
                        change_action,
                        ChangeAction::AddToValidated(ValidatedConsensusArtifact {
                            msg: ConsensusMessage::BlockProposal(_),
                            timestamp: _
                        }),
                    )
                });
            }

            changeset.append(&mut add_all_to_validated(
                timestamp,
                self.maliciously_propose_blocks(
                    pool,
                    malicious_flags.maliciously_propose_empty_blocks,
                    malicious_flags.maliciously_propose_equivocating_blocks,
                ),
            ));
        }
```
