### Title
Anonymous Principal Accepted as NNS Neuron Hot Key, Enabling Unauthenticated Voting and Following - (File: rs/nns/governance/src/neuron/types.rs)

### Summary
The `add_hot_key` function in NNS governance does not validate that the supplied hot key is not the anonymous principal. A neuron controller (an authorized agent) can register `PrincipalId::new_anonymous()` as a hot key. Because `is_authorized_to_vote` and `is_hotkey_or_controller` check only whether the caller appears in the `hot_keys` list, any unauthenticated ingress message—whose caller is the anonymous principal by protocol default—can then vote, follow, and toggle Neurons' Fund membership on behalf of that neuron.

### Finding Description

`add_hot_key` in `rs/nns/governance/src/neuron/types.rs` enforces only two invariants: no duplicate keys and no more than `MAX_NUM_HOT_KEYS_PER_NEURON` keys. [1](#0-0) 

There is no guard against the anonymous principal (`[4]` bytes, `2vxsx-fae`). The authorization path used for voting and following is: [2](#0-1) 

`is_hotkey_or_controller` returns `true` for any principal in `hot_keys`, including the anonymous principal. The `configure` dispatch that calls `add_hot_key` also performs no such check: [3](#0-2) 

The authorization gate for `JoinCommunityFund` / `LeaveCommunityFund` and for `Follow` / `RegisterVote` is `is_hotkey_or_controller`: [4](#0-3) 

Once the anonymous principal is in `hot_keys`, the IC protocol delivers any ingress message whose sender field is absent (i.e., the anonymous principal) as a valid caller. The governance canister has no additional rejection of the anonymous principal in `manage_neuron`.

The parallel to the DebtRegistry bug is exact:

| DebtRegistry | NNS Governance |
|---|---|
| `insert` / `modifyBeneficiary` accept null address | `add_hot_key` accepts anonymous principal |
| `RepaymentRouter.repay` assumes non-null beneficiary | `is_authorized_to_vote` assumes hot keys are real principals |
| Null beneficiary breaks "entry is valid" check | Anonymous hot key breaks "caller is authenticated" invariant |

### Impact Explanation

After a controller adds `PrincipalId::new_anonymous()` as a hot key:

1. **Unauthorized voting** – any unauthenticated ingress sender can call `manage_neuron` → `RegisterVote` on behalf of the neuron, casting votes on NNS proposals without holding the neuron's private key.
2. **Unauthorized following** – any unauthenticated sender can call `Follow` to redirect the neuron's automatic votes to an arbitrary set of followees, silently altering its governance influence.
3. **Unauthorized Neurons' Fund membership** – any unauthenticated sender can call `JoinCommunityFund` or `LeaveCommunityFund`, affecting the neuron's maturity commitment to SNS swaps.

All three operations affect live NNS governance outcomes. A neuron with significant voting power whose controller accidentally or negligently registers the anonymous principal as a hot key becomes permanently manipulable by any internet user until the controller removes it.

### Likelihood Explanation

The root cause requires the neuron controller to call `AddHotKey` with the anonymous principal. This can happen through:

- A buggy dapp or wallet that constructs a `PrincipalId` from an empty or missing field and passes it to `AddHotKey` without validation.
- A social-engineering attack that tricks a controller into submitting a crafted `manage_neuron` payload.
- A developer testing scenario that is accidentally promoted to mainnet.

The controller can undo the damage by calling `RemoveHotKey`, but only if they notice the misconfiguration. The governance canister provides no warning or rejection at insertion time.

### Recommendation

Add an explicit check in `add_hot_key` (and in the `configure` dispatch that calls it) to reject the anonymous principal and the management canister principal:

```rust
fn add_hot_key(&mut self, new_hot_key: &PrincipalId) -> Result<(), GovernanceError> {
    if *new_hot_key == PrincipalId::new_anonymous() {
        return Err(GovernanceError::new_with_message(
            ErrorType::InvalidCommand,
            "Hot key must not be the anonymous principal.",
        ));
    }
    // existing duplicate and max-count checks …
}
```

This mirrors the fix applied to the DebtRegistry (`d314446`): add a null-check at the point of insertion rather than relying on downstream callers to handle the invalid state.

### Proof of Concept

1. Controller `P` owns neuron `N`.
2. `P` calls `manage_neuron` with:
   ```
   Configure { operation: AddHotKey { new_hot_key: Some(PrincipalId::new_anonymous()) } }
   ```
   The call succeeds; `N.hot_keys` now contains the anonymous principal.
3. An unauthenticated attacker sends an ingress `manage_neuron` message (caller = anonymous principal) with:
   ```
   Follow { topic: NetworkEconomics, followees: [attacker_neuron_id] }
   ```
4. `is_authorized_to_vote(anonymous_principal)` → `hot_keys.contains(anonymous_principal)` → `true`.
5. The governance canister accepts the `Follow` command and updates `N`'s followees, redirecting its automatic votes to the attacker's neuron—without the controller's knowledge or consent. [1](#0-0) [5](#0-4)

### Citations

**File:** rs/nns/governance/src/neuron/types.rs (L243-255)
```rust
    pub(crate) fn is_authorized_to_vote(&self, principal: &PrincipalId) -> bool {
        self.is_hotkey_or_controller(principal)
    }

    /// Returns true if and only if `principal` is authorized to
    /// call simulate_manage_neuron requests on this neuron
    pub(crate) fn is_authorized_to_simulate_manage_neuron(&self, principal: &PrincipalId) -> bool {
        self.is_hotkey_or_controller(principal)
    }

    /// Returns true if and only if `principal` is either the controller or a hotkey
    fn is_hotkey_or_controller(&self, principal: &PrincipalId) -> bool {
        self.is_controlled_by(principal) || self.hot_keys.contains(principal)
```

**File:** rs/nns/governance/src/neuron/types.rs (L657-676)
```rust
    fn add_hot_key(&mut self, new_hot_key: &PrincipalId) -> Result<(), GovernanceError> {
        // Make sure that the same hot key is not added twice.
        for key in &self.hot_keys {
            if *key == *new_hot_key {
                return Err(GovernanceError::new_with_message(
                    ErrorType::HotKey,
                    "Hot key duplicated.",
                ));
            }
        }
        // Allow at most 10 hot keys per neuron.
        if self.hot_keys.len() >= MAX_NUM_HOT_KEYS_PER_NEURON {
            return Err(GovernanceError::new_with_message(
                ErrorType::ResourceExhausted,
                "Reached the maximum number of hotkeys.",
            ));
        }
        self.hot_keys.push(*new_hot_key);
        Ok(())
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L778-791)
```rust
        match configure {
            // The controller and hotkeys are allowed to change Neuron Fund membership.
            JoinCommunityFund(_) | LeaveCommunityFund(_) => {
                if self.is_hotkey_or_controller(caller) {
                    Ok(())
                } else {
                    Err(GovernanceError::new_with_message(
                        ErrorType::NotAuthorized,
                        format!(
                            "Caller '{caller:?}' must be the controller or hotkey of the neuron to join or leave the neuron fund.",
                        ),
                    ))
                }
            }
```

**File:** rs/nns/governance/src/neuron/types.rs (L875-883)
```rust
            Operation::AddHotKey(k) => {
                let hot_key = k.new_hot_key.as_ref().ok_or_else(|| {
                    GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    "Operation AddHotKey requires the hot key to add to be specified in the input",
                )
                })?;
                self.add_hot_key(hot_key)
            }
```
