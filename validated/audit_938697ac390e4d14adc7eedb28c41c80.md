Audit Report

## Title
Partial Neuron Transfer Silently Locks GTC Account Permanently — (`rs/nns/gtc/src/lib.rs`)

## Summary
`AccountState::transfer` unconditionally returns `Ok(())` even when individual `transfer_gtc_neuron` governance calls fail, while simultaneously removing failed neurons from `neuron_ids`. Because `donate_account` relies on `transfer` returning `Err` to suppress the `has_donated = true` assignment, any partial failure causes the account to be permanently locked with orphaned neurons that are neither owned by the custodian nor recoverable by the original owner.

## Finding Description

**Root cause 1 — `transfer()` swallows per-neuron errors**

The loop at lines 188–207 calls `GovernanceCanister::transfer_gtc_neuron` for each neuron. On failure the error is pushed to `failed_transferred_neurons`, but the function falls through to `Ok(())` at line 209 regardless of how many neurons failed. [1](#0-0) 

**Root cause 2 — neuron removed from `neuron_ids` unconditionally before result is checked**

Line 192 calls `retain` to remove the neuron from the live list *before* the `match result` block at line 200. A failed transfer still removes the neuron from `neuron_ids`, leaving it in neither the owner's list nor the custodian's control. [2](#0-1) 

**Root cause 3 — `donate_account` sets `has_donated = true` unconditionally**

Because `transfer()` never returns `Err`, the `?` propagation on line 89 never fires, and line 90 always executes. [3](#0-2) 

**Permanent lock-out — all recovery paths gated on `has_donated`**

- `claim_neurons` (lines 54–56): returns `Err` if `has_donated`. [4](#0-3) 
- `transfer()` re-entry guard (lines 177–178): returns `Err` if `has_donated`. [5](#0-4) 
- `forward_whitelisted_unclaimed_accounts` (lines 113–115): skips accounts where `has_donated`. [6](#0-5) 

After a partial failure: `neuron_ids` is empty (neurons removed), `has_donated = true` (all paths blocked), failed neurons are in `failed_transferred_neurons` but were never transferred to the custodian. There is no code path that reads `failed_transferred_neurons` to retry or recover.

## Impact Explanation

Any GTC account holder whose `donate_account` call coincides with a transient governance error on even one neuron suffers permanent, irrecoverable loss of the ICP staked in those neurons. GTC genesis neurons held substantial ICP stakes. The failed neurons are permanently orphaned: not owned by the custodian (governance call failed), not owned by the original account (removed from `neuron_ids`, account locked). This constitutes permanent loss of in-scope NNS governance assets (staked ICP neurons), matching the **High** impact class: *Unauthorized access to neurons, governance assets, wallets, identities, ledgers, or canister-controlled funds* and *permanent loss of ICP/Cycles or in-scope chain-key/ledger assets*.

## Likelihood Explanation

No adversarial action is required. The trigger is a transient inter-canister call failure (reject code, canister-busy, upgrade window) from the governance canister during `transfer_gtc_neuron`. Such failures are a normal operational reality on the IC. The account owner has no way to detect the partial failure before it is too late, because `donate_account` returns `Ok(())` to the caller. Any remaining unclaimed GTC accounts that attempt to donate are at risk.

## Recommendation

`transfer()` must propagate per-neuron failures to the caller:

1. **Fail-fast**: return `Err(...)` on the first failed `transfer_gtc_neuron` call and do not call `self.neuron_ids.retain(...)` until after a confirmed successful transfer.
2. **Fail-aggregate**: collect all errors and return `Err(aggregate)` at the end if any neuron failed; only remove a neuron from `neuron_ids` after its transfer is confirmed successful.

`donate_account` must only set `has_donated = true` after `transfer()` confirms that every neuron was successfully transferred. The current `?` propagation on line 89 is structurally correct but ineffective because `transfer()` never returns `Err`.

## Proof of Concept

```
1. Create a GTC account with neuron_ids = [N1, N2, N3].
2. Configure a mock governance that succeeds for N1, fails for N2, succeeds for N3.
3. Call donate_account.
4. Observe:
   - transfer() returns Ok(()).
   - has_donated is set to true.
   - successfully_transferred_neurons = [N1, N3]
   - failed_transferred_neurons       = [N2]  ← not owned by custodian
   - neuron_ids                        = []   ← N2 removed despite failure
5. Call claim_neurons  → Err("Account has previously donated its funds")
6. Call donate_account → Err("Account has already donated its funds")
7. N2's staked ICP is permanently inaccessible.
```

A deterministic unit test can be written against `AccountState::transfer` using a mock `GovernanceCanister` that returns `Err` for a designated neuron ID, then asserting that `failed_transferred_neurons` is non-empty, `neuron_ids` is empty, and `has_donated` is `true` — confirming the orphaned-neuron state with no recovery path.

### Citations

**File:** rs/nns/gtc/src/lib.rs (L54-56)
```rust
        if account.has_donated {
            return Err("Account has previously donated its funds".to_string());
        }
```

**File:** rs/nns/gtc/src/lib.rs (L89-90)
```rust
        account.transfer(custodian_neuron_id).await?;
        account.has_donated = true;
```

**File:** rs/nns/gtc/src/lib.rs (L113-115)
```rust
            if !account.has_claimed
                && !account.has_donated
                && !account.has_forwarded
```

**File:** rs/nns/gtc/src/lib.rs (L177-178)
```rust
        } else if self.has_donated {
            return Err("Account has already donated its funds".to_string());
```

**File:** rs/nns/gtc/src/lib.rs (L192-192)
```rust
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
