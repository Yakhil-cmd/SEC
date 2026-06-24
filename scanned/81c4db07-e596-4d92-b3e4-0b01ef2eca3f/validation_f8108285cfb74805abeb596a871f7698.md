### Title
GTC `donate_account` Permanently Locks User Out of Claiming Neurons After Partial Transfer Failure - (File: rs/nns/gtc/src/lib.rs)

### Summary
The Genesis Token Canister (GTC) `donate_account` method calls `AccountState::transfer`, which removes each neuron ID from `self.neuron_ids` unconditionally (regardless of success or failure) during iteration. If the cross-canister call to `transfer_gtc_neuron` on Governance fails for any neuron, that neuron ID is removed from `neuron_ids` and placed in `failed_transferred_neurons`. After the loop, `has_donated` is set to `true`. On any subsequent call to `claim_neurons`, the user is permanently blocked with "Account has previously donated its funds." The user's neurons that failed to transfer remain controlled by the GTC canister, are no longer in `neuron_ids`, and cannot be claimed or recovered by the user.

### Finding Description

In `AccountState::transfer` (`rs/nns/gtc/src/lib.rs`, lines 188–207), the loop iterates over all neuron IDs and, **regardless of whether the cross-canister `transfer_gtc_neuron` call succeeds or fails**, unconditionally removes the neuron ID from `self.neuron_ids` via `self.neuron_ids.retain(|id| id != &neuron_id)` at line 192:

```rust
for neuron_id in neuron_ids {
    let result =
        GovernanceCanister::transfer_gtc_neuron(neuron_id, custodian_neuron_id).await;

    self.neuron_ids.retain(|id| id != &neuron_id);  // always removed

    match result {
        Ok(_) => self.successfully_transferred_neurons.push(donated_neuron),
        Err(e) => {
            donated_neuron.error = Some(e.to_string());
            self.failed_transferred_neurons.push(donated_neuron)
        }
    }
}
```

After `transfer` returns `Ok(())`, the caller `donate_account` sets `account.has_donated = true` (line 90). From this point on:

- `claim_neurons` checks `account.has_donated` first and returns `Err("Account has previously donated its funds")` (line 54–56).
- `donate_account` → `transfer` checks `self.has_donated` and returns `Err("Account has already donated its funds")` (line 177–178).
- The neurons that failed to transfer are still controlled by the GTC canister in Governance (they were not successfully transferred), but their IDs are gone from `neuron_ids` and the account is permanently locked.

The cross-canister call to `transfer_gtc_neuron` on Governance can fail transiently (e.g., Governance canister temporarily unavailable, ledger call inside `transfer_gtc_neuron` fails, or the call itself returns a canister error). This is a realistic scenario on the IC.

### Impact Explanation

A GTC account holder who calls `donate_account` during a period of transient Governance or ledger unavailability will have:
1. Some neurons successfully transferred to the custodian.
2. Some neurons that failed to transfer — their IDs are removed from `neuron_ids` and placed in `failed_transferred_neurons`.
3. `has_donated = true` set permanently.

The user can no longer call `claim_neurons` (blocked by `has_donated`), cannot retry `donate_account` (blocked by `has_donated`), and cannot recover the failed neurons through any GTC interface. The neurons remain in Governance under GTC control but are inaccessible to the user. This is a permanent, irreversible loss of the user's genesis allocation — analogous to the M-05 report where a one-time eligibility flag is consumed even when the underlying operation partially fails.

### Likelihood Explanation

The GTC `donate_account` flow involves two sequential cross-canister calls per neuron: one to `transfer_gtc_neuron` on Governance, which itself calls `transfer_funds` on the ICP ledger. A GTC account can have up to 48 neurons (SR accounts). Any transient failure on any one of those 48 calls — during the async await point — will cause partial failure. The IC's canister messaging system can return errors for many reasons (cycles exhausted, canister stopped, ledger temporarily unavailable). The likelihood is low in normal operation but non-zero, and the consequence is permanent and irreversible for the affected user.

### Recommendation

1. **Do not set `has_donated = true` if any neuron transfer failed.** Only mark the account as donated when all neurons have been successfully transferred.
2. **Do not remove neuron IDs from `self.neuron_ids` on failure.** Only remove a neuron ID from `neuron_ids` after a confirmed successful transfer, so that failed neurons remain retryable.
3. **Allow retrying `donate_account`** when `failed_transferred_neurons` is non-empty and `has_donated` is false, so users can recover from partial failures.

### Proof of Concept

1. A GTC account holder with 48 SR neurons calls `donate_account`.
2. The first 30 neurons transfer successfully; on neuron 31, the Governance canister returns a transient error (e.g., ledger temporarily unavailable).
3. In `AccountState::transfer`, neuron 31's ID is removed from `self.neuron_ids` (line 192) and placed in `failed_transferred_neurons` (line 204). The loop continues, and neurons 32–48 also fail similarly.
4. `transfer` returns `Ok(())` (line 209) — it never returns `Err` for individual neuron failures.
5. Back in `donate_account` (line 90), `account.has_donated = true` is set.
6. The user now calls `claim_neurons` to recover their 18 failed neurons. They receive: `Err("Account has previously donated its funds")`.
7. The user calls `donate_account` again to retry. They receive: `Err("Account has already donated its funds")` from `transfer` (line 177–178).
8. The 18 neurons remain in Governance under GTC control, permanently inaccessible to the user.

**Root cause lines:** [1](#0-0) 

**`has_donated` set unconditionally after partial failure:** [2](#0-1) 

**`claim_neurons` permanently blocked by `has_donated`:** [3](#0-2) 

**`transfer` blocks retry when `has_donated` is true:** [4](#0-3) 

**`AccountState` proto showing `has_donated` as a permanent boolean flag with no partial-state tracking:** [5](#0-4)

### Citations

**File:** rs/nns/gtc/src/lib.rs (L54-56)
```rust
        if account.has_donated {
            return Err("Account has previously donated its funds".to_string());
        }
```

**File:** rs/nns/gtc/src/lib.rs (L89-91)
```rust
        account.transfer(custodian_neuron_id).await?;
        account.has_donated = true;

```

**File:** rs/nns/gtc/src/lib.rs (L177-178)
```rust
        } else if self.has_donated {
            return Err("Account has already donated its funds".to_string());
```

**File:** rs/nns/gtc/src/lib.rs (L188-207)
```rust
        for neuron_id in neuron_ids {
            let result =
                GovernanceCanister::transfer_gtc_neuron(neuron_id, custodian_neuron_id).await;

            self.neuron_ids.retain(|id| id != &neuron_id);

            let mut donated_neuron = TransferredNeuron {
                neuron_id: Some(neuron_id),
                timestamp_seconds: now_secs(),
                error: None,
            };

            match result {
                Ok(_) => self.successfully_transferred_neurons.push(donated_neuron),
                Err(e) => {
                    donated_neuron.error = Some(e.to_string());
                    self.failed_transferred_neurons.push(donated_neuron)
                }
            }
        }
```

**File:** rs/nns/gtc/proto/ic_nns_gtc/pb/v1/gtc.proto (L56-60)
```text
  // If `true`, the neurons in `neuron_ids` have been donated.
  bool has_donated = 6;

  // If `true`, the neurons in `neuron_ids` have been forwarded.
  bool has_forwarded = 7;
```
