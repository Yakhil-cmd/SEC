### Title
NNS Governance `follow()` Allows Delegation to Voting-Ineligible (Dissolved) Neurons Without Eligibility Check - (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

The NNS governance `follow()` function and its `modify_followees()` helper allow a neuron to establish a follow (vote-delegation) relationship with a dissolved neuron that is permanently ineligible to vote. No check is performed on the followee's dissolve delay or voting eligibility before the relationship is persisted. As a result, the follower neuron silently never has its vote automatically cast, causing it to miss voting rewards — the direct IC analog of delegating stake to an inactive validator.

---

### Finding Description

In `rs/nns/governance/src/governance.rs`, the `follow()` function (line 5718) handles `manage_neuron::Follow` ingress commands. When neuron-follow restrictions are enabled, it delegates to `modify_followees()` (line 8370). That helper validates two things about each new followee:

1. Whether the followee neuron **exists** in the neuron store.
2. Whether the followee neuron is **public**, or shares a controller/hot-key with the follower. [1](#0-0) 

What `modify_followees()` does **not** check is whether the followee neuron has a `dissolve_delay_seconds` value that meets the minimum threshold required to be voting-eligible. A dissolved neuron (`dissolve_delay == 0`) is permanently excluded from receiving ballots on new proposals and therefore can never trigger the cascade that would cast the follower's vote.

When `is_neuron_follow_restrictions_enabled()` returns `false`, there is no validation at all — the follow relationship is inserted unconditionally: [2](#0-1) 

In both code paths the missing guard is a check equivalent to:

```rust
neuron.dissolve_delay_seconds(now) >= MIN_DISSOLVE_DELAY_FOR_VOTE_ELIGIBILITY_SECONDS
```

The `follow()` entry point itself performs no such check either: [3](#0-2) 

The voting cascade in `rs/nns/governance/src/voting.rs` only propagates votes through neurons that already hold a ballot. A dissolved neuron receives no ballot at proposal creation time, so the cascade never reaches the follower: [4](#0-3) 

---

### Impact Explanation

**Impact: Medium.**

A neuron that follows a dissolved (voting-ineligible) neuron will silently fail to have its vote automatically cast on every proposal for which the followee has no ballot. The follower neuron therefore:

- Misses voting rewards on all proposals where it would otherwise have voted via the follow mechanism.
- Has no indication that the follow relationship is non-functional; the `Follow` command succeeds with `FollowResponse {}`.

This is the direct IC analog of the reported finding: staker funds delegated to an inactive validator that generates no rewards.

---

### Likelihood Explanation

**Likelihood: Medium.**

Two realistic paths exist:

1. **Stale follow after dissolution**: A neuron establishes a follow relationship to an active neuron. Over time the followee's dissolve timer reaches zero. The follow relationship persists (no automatic cleanup), and from that point forward the follower's vote is never cast.

2. **Direct follow of a dissolved neuron**: A neuron owner calls `manage_neuron` with a `Follow` command targeting a neuron that is already dissolved. The `modify_followees()` check confirms the followee exists and is public — both conditions pass for a dissolved neuron — and the relationship is accepted. The integration test `neuron_follow_nonexistent_neuron_fails` (marked `#[should_panic]`) confirms that following a *nonexistent* neuron fails, but a *dissolved* neuron still exists and passes all current checks. [5](#0-4) 

---

### Recommendation

Inside `modify_followees()`, after confirming the followee neuron exists, add a voting-eligibility check:

```rust
let followee_dissolve_delay = neuron_store
    .with_neuron(followee, |n| n.dissolve_delay_seconds(now_seconds))
    .unwrap_or(0);

if followee_dissolve_delay < MIN_DISSOLVE_DELAY_FOR_VOTE_ELIGIBILITY_SECONDS {
    invalid_followees = invalid_followees.saturating_add(1);
    error_message.push_str(&format!(
        "{}: Neuron {} is dissolved and cannot vote. ...\n",
        invalid_followees, followee.id
    ));
}
```

This mirrors the existing pattern for visibility/controller validation at lines 8421–8452 and ensures that follow relationships are only established with neurons that can actually cast votes.

---

### Proof of Concept

**Scenario A — follow of an already-dissolved neuron:**

1. Neuron B exists with `dissolve_delay_seconds == 0` (dissolved state, `NeuronState::Dissolved`).
2. Neuron A's controller calls `manage_neuron` with `Command::Follow { topic: T, followees: [B] }`.
3. `follow()` → `modify_followees()`: Neuron B exists ✓, Neuron B is public ✓ — no voting-eligibility check.
4. Follow relationship is persisted successfully (`FollowResponse {}`).
5. A new proposal on topic T is created. Neuron B receives no ballot (dissolved).
6. The voting cascade never fires for Neuron A; its vote remains `Unspecified`.
7. Neuron A misses voting rewards for this proposal and all future proposals on topic T.

**Scenario B — followee dissolves after follow is established:**

1. Neuron A follows Neuron B (active, `dissolve_delay > 6 months`).
2. Neuron B starts dissolving; eventually `dissolve_delay == 0`.
3. No cleanup of the follow relationship occurs.
4. From this point forward, Neuron A's vote is never automatically cast via Neuron B.
5. Neuron A silently loses voting rewards indefinitely. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5718-5791)
```rust
    fn follow(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        follow_request: &manage_neuron::Follow,
    ) -> Result<(), GovernanceError> {
        // Find the neuron to modify.
        let (is_neuron_controlled_by_caller, is_caller_authorized_to_vote) =
            self.with_neuron(id, |neuron| {
                (
                    neuron.is_controlled_by(caller),
                    neuron.is_authorized_to_vote(caller),
                )
            })?;

        // Only the controller, or a proposal (which passes the controller as the
        // caller), can change the followees for the ManageNeuron topic.
        if follow_request.topic() == Topic::NeuronManagement && !is_neuron_controlled_by_caller {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                "Caller is not authorized to manage following of neuron for the ManageNeuron topic.",
            ));
        } else {
            // Check that the caller is authorized, i.e., either the
            // controller or a registered hot key.
            if !is_caller_authorized_to_vote {
                return Err(GovernanceError::new_with_message(
                    ErrorType::NotAuthorized,
                    "Caller is not authorized to manage following of neuron.",
                ));
            }
        }

        // Check that the list of followees is not too
        // long. Allowing neurons to follow too many neurons
        // allows a memory exhaustion attack on the neurons
        // canister.
        if follow_request.followees.len() > MAX_FOLLOWEES_PER_TOPIC {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "Too many followees.",
            ));
        }

        // Validate topic exists
        let topic = Topic::try_from(follow_request.topic).map_err(|_| {
            GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                format!("Not a known topic number. Follow:\n{follow_request:#?}"),
            )
        })?;

        let now_seconds = self.env.now();
        let new_neuron_followees = self.with_neuron(id, |neuron| {
            modify_followees(
                &self.neuron_store,
                neuron,
                &neuron.followees,
                topic as i32,
                Followees {
                    followees: follow_request.followees.clone(),
                },
            )
        })??;

        self.with_neuron_mut(id, |neuron| {
            neuron.followees = new_neuron_followees;

            // Changing followees may change the voting power, so refresh it.
            neuron.refresh_voting_power(now_seconds);
        })?;

        Ok(())
    }
```

**File:** rs/nns/governance/src/governance.rs (L8370-8460)
```rust
    new_followees: Followees,
) -> Result<HashMap<i32, Followees>, GovernanceError> {
    let controller = neuron.controller();
    let mut updated_followees = topic_to_followees.clone();
    if new_followees.followees.is_empty() {
        // If the new followees list is empty, remove the entry for the topic.
        updated_followees.remove(&topic);
        return Ok(updated_followees);
    }

    if !is_neuron_follow_restrictions_enabled() {
        // If the neuron follow restrictions are not enabled, we can directly update the entry.
        updated_followees.insert(topic, new_followees);
        return Ok(updated_followees);
    }

    if topic == Topic::NeuronManagement as i32 {
        // Neuron management followees are not subject to the follow restrictions.
        // This exception doesn't expose any security issue, as it doesn't reveal
        // any ballots regardging non-neuron-management proposals of the followees.
        updated_followees.insert(topic, new_followees);
        return Ok(updated_followees);
    }

    // Otherwise, update the entry with the new followees list.
    // A new can follow another neuron if:
    // 1. the followee neuron is a public neuron
    // 2. or if the followee neuron is a private neuron, either
    //      * they share a controller or
    //      * the controller of the follower neuron is in the hot keys of the followee neuron.
    // If in the list of followees, there are any follow relationships
    // that don't adhere to the aforementioned rules, return a GovernanceError
    // including all the invalid followees.
    let mut invalid_followees = 0_u32;
    let mut error_message = String::new();

    // To avoid looking up the already existing followees of the neuron
    // (which are not changing) we only validate the new followees.
    let old_followees = topic_to_followees
        .get(&topic)
        .map(|f| f.followees.iter().collect::<HashSet<&NeuronId>>())
        .unwrap_or_default();

    for followee in &new_followees.followees {
        if old_followees.contains(followee) {
            // An already existing follow relationship is either
            // grandfathered in, or it was already validated when it was created.
            // Hence, we don't need to validate it again.
            continue;
        }

        if let Ok((followee_visibility, followee_controller, followee_hot_keys)) = neuron_store
            .with_neuron(followee, |neuron| {
                (
                    neuron.visibility(),
                    neuron.controller(),
                    neuron.hot_keys.clone(),
                )
            })
        {
            let allowed_to_follow = followee_visibility == Visibility::Public
                || followee_controller == controller
                || followee_hot_keys.contains(&controller);

            if !allowed_to_follow {
                invalid_followees = invalid_followees.saturating_add(1);
                error_message.push_str(&format!(
                                "{}: Neuron {} is a private neuron.\n\
                                If you control neuron {}, you can follow it after adding your principal {} to its list of hotkeys or setting the neuron to public.",
                                invalid_followees,
                                followee.id,
                                followee.id,
                                controller
                            ));
            }
        } else {
            invalid_followees = invalid_followees.saturating_add(1);
            error_message.push_str(&format!(
                            "{}: The neuron with ID {} does not exist. Make sure that you copied the neuron ID correctly.\n",
                            invalid_followees,
                            followee.id
                        ));
        }
    }

    if invalid_followees > 0 {
        // Note: These error messages are matched in the nns-dapp. In case of changes, please sync with the nns dev team.
        error_message = format!(
            "The {} followee(s) listed below is(are) invalid:\n{}",
            invalid_followees, error_message
        );
```

**File:** rs/nns/governance/src/voting.rs (L527-546)
```rust
            while let Some(follower) = self.followers_to_check.pop_first() {
                let vote = match neuron_store
                    .neuron_would_follow_ballots(follower, self.topic, ballots)
                {
                    Ok(vote) => vote,
                    Err(e) => {
                        // This is a bad inconsistency, but there is
                        // nothing that can be done about it at this
                        // place.  We somehow have followers recorded that don't exist.
                        eprintln!(
                            "error in cast_vote_and_cascade_follow when gathering induction votes: {:?}",
                            e
                        );
                        Vote::Unspecified
                    }
                };
                // Casting vote immediately might affect other follower votes, which makes
                // voting resolution take fewer iterations.
                // Vote::Unspecified is ignored by cast_vote.
                self.cast_vote(ballots, follower, vote);
```

**File:** rs/nns/integration_tests/src/neuron_following.rs (L141-156)
```rust
#[test]
#[should_panic]
fn neuron_follow_nonexistent_neuron_fails() {
    let state_machine = setup_state_machine_with_nns_canisters();

    let n1 = get_neuron_1();
    let nonexistent_neuron = get_nonexistent_neuron();

    // neurons are allowed to follow nonexistent neurons
    set_followees_on_topic(
        &state_machine,
        &n1,
        &[nonexistent_neuron.neuron_id],
        VALID_TOPIC,
    );
}
```

**File:** rs/nns/governance/api/src/types.rs (L3862-3901)
```rust
pub enum NeuronState {
    /// Not a valid state. Required by Protobufs.
    Unspecified = 0,
    /// In this state, the neuron is not dissolving and has a specific
    /// `dissolve_delay`. It accrues `age` by the passage of time and it
    /// can vote if `dissolve_delay` is at least six months. The method
    /// \[Neuron::start_dissolving\] can be called to transfer the neuron
    /// to the `Dissolving` state. The method
    /// \[Neuron::increase_dissolve_delay\] can be used to increase the
    /// dissolve delay without affecting the state or the age of the
    /// neuron.
    NotDissolving = 1,
    /// In this state, the neuron's `dissolve_delay` decreases with the
    /// passage of time. While dissolving, the neuron's age is considered
    /// zero. Eventually it will reach the `Dissolved` state. The method
    /// \[Neuron::stop_dissolving\] can be called to transfer the neuron to
    /// the `NotDissolving` state, and the neuron will start aging again. The
    /// method \[Neuron::increase_dissolve_delay\] can be used to increase
    /// the dissolve delay, but this will not stop the timer or affect
    /// the age of the neuron.
    Dissolving = 2,
    /// In the dissolved state, the neuron's stake can be disbursed using
    /// the \[Governance::disburse\] method. It cannot vote as its
    /// `dissolve_delay` is considered to be zero.
    ///
    /// If the method \[Neuron::increase_dissolve_delay\] is called in this
    /// state, the neuron will no longer be dissolving, with the specified
    /// dissolve delay, and will start aging again.
    ///
    /// Neuron holders have an incentive not to keep neurons in the
    /// 'dissolved' state for a long time: if the holders wants to make
    /// their tokens liquid, they disburse the neuron's stake, and if
    /// they want to earn voting rewards, they increase the dissolve
    /// delay. If these incentives turn out to be insufficient, the NNS
    /// may decide to impose further restrictions on dissolved neurons.
    Dissolved = 3,
    /// The neuron is in spawning state, meaning it's maturity will be
    /// converted to ICP according to <https://wiki.internetcomputer.org/wiki/Maturity_modulation.>
    Spawning = 4,
}
```
