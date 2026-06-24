Audit Report

## Title
GTC Canister `donate_account`/`claim_neurons` Reentrancy Allows Concurrent In-Flight Governance Calls, Breaking Mutual Exclusion and Causing Permanent Neuron Loss — (`rs/nns/gtc/src/lib.rs`)

## Summary

Neither `donate_account` nor `claim_neurons` sets any guard flag before yielding to governance via `.await`. On the IC, a canister processes queued ingress messages while suspended at an `.await` point, so an account owner who submits both calls in rapid succession can have both in-flight simultaneously. This breaks the `has_donated`/`has_claimed` mutual exclusion invariant and, in the most damaging interleaving, causes permanent irrecoverable loss of a subset of the owner's GTC neurons to the custodian.

## Finding Description

**Root cause — no pre-await guard in `donate_account`:** [1](#0-0) 

`account.transfer(custodian_neuron_id).await?` is called with `has_donated` still `false`. Inside `transfer()`, the synchronous guard checks pass: [2](#0-1) 

The loop then immediately yields at the first inter-canister call: [3](#0-2) 

**Root cause — no pre-await guard in `claim_neurons`:** [4](#0-3) 

While `donate_account` is suspended at `transfer_gtc_neuron(N1).await`, the GTC processes the queued `claim_neurons` message. At this moment `has_donated = false` and `has_claimed = false`, so all checks pass and `claim_gtc_neurons(alice, [N1..Nn]).await` is issued — a second concurrent in-flight call to governance for the same neuron IDs.

**Critical: `transfer()` swallows individual neuron failures:** [5](#0-4) 

Individual `transfer_gtc_neuron` failures are logged but not propagated. `transfer()` always returns `Ok(())`, so `donate_account` unconditionally sets `has_donated = true` afterward regardless of how many neurons actually transferred.

**Governance `claim_gtc_neurons` is atomic — all-or-nothing on GTC-controller check:** [6](#0-5) 

This means the race outcome depends entirely on which governance message is processed first.

**Exploit flow (most damaging interleaving):**

1. Alice submits `donate_account`. GTC enters `transfer()`, synchronous checks pass, calls `transfer_gtc_neuron(N1, custodian).await`. GTC suspended; `has_donated = false`, `has_claimed = false`, `neuron_ids = [N1..Nn]`.
2. Alice submits `claim_neurons`. GTC processes it while suspended: `has_donated = false` ✓, `has_claimed = false` ✓. `neuron_ids` still contains N1 (line 192 executes only after the await returns). GTC calls `claim_gtc_neurons(alice, [N1..Nn]).await`.
3. Governance processes `transfer_gtc_neuron(N1, custodian)` first → N1 donated, deleted from governance, stake merged into custodian.
4. `donate_account` resumes: N1 removed from `neuron_ids`, loop continues to N2 with `transfer_gtc_neuron(N2).await`. GTC suspended again.
5. `claim_neurons` resumes: `claim_gtc_neurons(alice, [N1..Nn])` — N1 no longer exists in governance → governance returns `PreconditionFailed`. `claim_neurons` propagates the error; `has_claimed` is never set.
6. Remaining `transfer_gtc_neuron` calls for N2..Nn succeed (still GTC-controlled). `transfer()` returns `Ok(())`.
7. `donate_account` sets `has_donated = true`.

**Result:** N1 is permanently and irrecoverably donated to the custodian. N2..Nn are also donated. `has_donated = true`, `has_claimed = false`. Alice loses all neurons with no recourse.

**Alternative interleaving (Case A — invariant broken, no loss):** If `claim_gtc_neurons` wins the race, Alice claims all neurons, all `transfer_gtc_neuron` calls fail silently, and `donate_account` sets `has_donated = true`. Both `has_claimed = true` and `has_donated = true` simultaneously — the mutual exclusion invariant is broken.

## Impact Explanation

The most damaging interleaving causes **permanent, irrecoverable loss of GTC neurons** for the account owner. GTC neurons represent significant ICP stakes distributed at genesis. The state inconsistency (`has_donated = true` with neurons never actually donated, or both flags simultaneously true) corrupts the NNS GTC canister's accounting permanently with no recovery path. This matches the High impact class: **significant NNS security impact with concrete user or protocol harm**, specifically unauthorized permanent loss of governance assets (neurons) triggerable by an unprivileged user with no special access.

## Likelihood Explanation

Exploitation requires only the account owner to submit two ingress update calls (`donate_account` and `claim_neurons`) before the first inter-canister response from governance returns. The suspension window spans multiple consensus rounds (the full round-trip latency to governance), which is wide enough for a second ingress to be queued and processed. No privileged access, key compromise, or subnet majority is required. Any IC wallet or agent that allows submitting multiple update calls in parallel (standard behavior) can trigger this. The window is deterministic and repeatable.

## Recommendation

Set the guard flag **before** any inter-canister call, and roll back on error (optimistic locking with rollback):

```rust
// donate_account
account.has_donated = true;                          // set BEFORE await
if let Err(e) = account.transfer(custodian_neuron_id).await {
    account.has_donated = false;                     // rollback on error
    return Err(e);
}
```

```rust
// claim_neurons
account.has_claimed = true;                          // set BEFORE await
if let Err(e) = GovernanceCanister::claim_gtc_neurons(caller, account.neuron_ids.clone()).await {
    account.has_claimed = false;                     // rollback on error
    return Err(e);
}
```

This is the standard IC reentrancy mitigation pattern. The flag set before the `.await` is visible to any message processed during the suspension, blocking the interleaved call at the guard check.

## Proof of Concept

A deterministic PocketIC integration test can reproduce this:

1. Initialize GTC canister with an account holding neurons N1..Nn.
2. Concurrently submit `donate_account(pubkey)` and `claim_neurons(pubkey)` from the same principal without awaiting the first response.
3. Advance the PocketIC state machine to process both ingress messages and deliver governance responses in the order: `transfer_gtc_neuron(N1)` → `claim_gtc_neurons([N1..Nn])` → remaining `transfer_gtc_neuron` calls.
4. Assert: `get_account(address).has_donated == true` AND N1 is no longer present in governance (permanently lost to custodian).

Alternatively, assert the invariant-broken case by delivering `claim_gtc_neurons` first and verifying `has_claimed == true && has_donated == true` simultaneously after both calls complete.

### Citations

**File:** rs/nns/gtc/src/lib.rs (L54-68)
```rust
        if account.has_donated {
            return Err("Account has previously donated its funds".to_string());
        }

        if account.has_forwarded {
            return Err("Account has previously forwarded its funds".to_string());
        }

        if account.has_claimed {
            return Ok(account.neuron_ids.clone());
        }

        GovernanceCanister::claim_gtc_neurons(caller, account.neuron_ids.clone()).await?;

        account.has_claimed = true;
```

**File:** rs/nns/gtc/src/lib.rs (L89-90)
```rust
        account.transfer(custodian_neuron_id).await?;
        account.has_donated = true;
```

**File:** rs/nns/gtc/src/lib.rs (L175-183)
```rust
        if self.has_claimed {
            return Err("Neurons already claimed".to_string());
        } else if self.has_donated {
            return Err("Account has already donated its funds".to_string());
        } else if self.has_forwarded {
            return Err("Account has already forwarded its funds".to_string());
        } else if custodian_neuron_id.is_none() {
            return Err("No custodian neuron ID is defined".to_string());
        }
```

**File:** rs/nns/gtc/src/lib.rs (L188-192)
```rust
        for neuron_id in neuron_ids {
            let result =
                GovernanceCanister::transfer_gtc_neuron(neuron_id, custodian_neuron_id).await;

            self.neuron_ids.retain(|id| id != &neuron_id);
```

**File:** rs/nns/gtc/src/lib.rs (L200-209)
```rust
            match result {
                Ok(_) => self.successfully_transferred_neurons.push(donated_neuron),
                Err(e) => {
                    donated_neuron.error = Some(e.to_string());
                    self.failed_transferred_neurons.push(donated_neuron)
                }
            }
        }

        Ok(())
```

**File:** rs/nns/governance/src/governance.rs (L1830-1843)
```rust
        let ids_are_valid = neuron_ids.iter().all(|id| {
            self.with_neuron(id, |neuron| {
                neuron.controller() == *GENESIS_TOKEN_CANISTER_ID.get_ref()
            })
            .unwrap_or(false)
        });

        if !ids_are_valid {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "At least one supplied NeuronId either does not have an associated Neuron \
                or the associated Neuron is not controlled by the GTC",
            ));
        }
```
