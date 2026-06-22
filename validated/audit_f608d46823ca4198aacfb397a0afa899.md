### Title
Unbounded `allowed_when_resources_are_low` Proposal Bypass Enables NNS Governance Heap Exhaustion - (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

The NNS governance canister's `make_proposal` function contains two independent spam-protection gates. Both are unconditionally bypassed for proposals whose action returns `true` from `allowed_when_resources_are_low()` — currently `InstallCode` targeting protocol canisters, `UpdateCanisterSettings` for protocol canisters, and certain `ExecuteNnsFunction` variants. No separate cap exists for these "emergency" proposals, and the heap-growth guard is also skipped for them. An unprivileged actor holding a neuron with ≥ 6-month dissolve delay and sufficient ICP stake can submit an unbounded stream of such proposals, growing the governance canister's heap without limit until it becomes unresponsive.

---

### Finding Description

**Gate 1 — heap growth check is skipped:**

```rust
// rs/nns/governance/src/governance.rs  lines 5143-5145
if !action.allowed_when_resources_are_low() {
    self.check_heap_can_grow()?;
}
``` [1](#0-0) 

**Gate 2 — `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` (200) is bypassed:**

```rust
// lines 5254-5269
if self.heap_data.proposals.values()
    .filter(|info| !info.ballots.is_empty() && !info.is_manage_neuron())
    .count()
    >= MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS
    && !action.allowed_when_resources_are_low()   // ← bypass
{
    return Err(...)
}
``` [2](#0-1) 

**Which proposals qualify:**

`InstallCode` proposals targeting any canister whose topic resolves to `Topic::ProtocolCanisterManagement` (Governance, Root, Lifeline, Registry, Ledger, CMC, …) return `true`:

```rust
// rs/nns/governance/src/proposals/install_code.rs  lines 146-151
pub fn allowed_when_resources_are_low(&self) -> bool {
    let Ok(canister_id) = self.valid_canister_id() else { return false; };
    topic_to_manage_canister(&canister_id) == Topic::ProtocolCanisterManagement
}
``` [3](#0-2) 

The same bypass is available for `UpdateCanisterSettings` and certain `ExecuteNnsFunction` variants via the same `allowed_when_resources_are_low` dispatch: [4](#0-3) 

**No per-proposer or per-type cap exists.** The global constant is only checked for non-exempt proposals: [5](#0-4) 

**Each submitted proposal allocates one ballot per eligible neuron** (up to `MAX_NUMBER_OF_NEURONS = 500 000`), stored in the governance canister's heap. The heap soft limit is 3.5 GiB: [6](#0-5) 

The `reject_cost_e8s` fee is charged upfront as `neuron_fees_e8s` (reducing the proposer's voting power), but it does not prevent submission as long as `minted_stake_e8s >= proposal_submission_fee`: [7](#0-6) 

---

### Impact Explanation

Each `InstallCode` proposal for a protocol canister creates a full ballot map (~500 000 entries × ~20 bytes ≈ 10 MB of heap per proposal). Because both the heap-growth guard and the 200-proposal cap are bypassed, an attacker can submit proposals continuously. At roughly 350–450 proposals the governance canister's heap reaches the 3.5 GiB soft limit; further state writes trap, rendering the canister unresponsive. This halts all NNS governance operations: no new proposals, no voting, no neuron management, no reward distribution — a complete governance freeze of the Internet Computer's root nervous system.

---

### Likelihood Explanation

**Entry path (no privileged role required):**
- Obtain a neuron with ≥ 6-month dissolve delay (fixed minimum to propose non-ManageNeuron proposals, per the April 2026 changelog).
- Stake enough ICP so that `minted_stake_e8s ≥ reject_cost_e8s` (default 1 ICP) after each fee deduction. Submitting N proposals requires staking ≥ N ICP.
- Submit `InstallCode` proposals targeting the Governance canister with arbitrary (but hash-consistent) WASM payloads.

**Cost estimate:** ~350–450 ICP to exhaust the heap. This is a non-trivial but realistic sum for a motivated attacker. No governance majority, no admin key, no social engineering, and no threshold corruption is required — only a staked neuron and repeated `manage_neuron` calls.

**Likelihood: Low-Medium.** The economic barrier (hundreds of ICP) limits casual abuse, but a well-funded adversary targeting the NNS can execute this without any privileged access.

---

### Recommendation

1. **Add a separate cap** for `allowed_when_resources_are_low` proposals (e.g., `MAX_NUMBER_OF_EMERGENCY_PROPOSALS_WITH_BALLOTS = 10`). These proposals are intended for rare recovery scenarios, not bulk submission.
2. **Apply the heap-growth check to all proposals**, but use a higher threshold for emergency proposals (e.g., allow submission up to 95% heap utilisation instead of the current soft limit, while still blocking at 100%).
3. **Add a per-neuron open-proposal limit** across all proposal types to prevent a single actor from monopolising the proposal queue.
4. Consider **increasing `reject_cost_e8s`** for `ProtocolCanisterManagement` proposals specifically, since their ballot maps are the largest.

---

### Proof of Concept

```
1. Create neuron N with dissolve_delay = 6 months, cached_neuron_stake_e8s = 500 * E8.

2. In a loop (i = 1..500):
     submit manage_neuron {
       command: MakeProposal {
         action: InstallCode {
           canister_id: GOVERNANCE_CANISTER_ID,
           install_mode: Upgrade,
           wasm_module: [0x00, 0x61, 0x73, 0x6d, ...],   // minimal valid WASM
           wasm_module_hash: sha256(wasm_module),
         }
       }
     }

   Each call succeeds because:
     - allowed_when_resources_are_low() == true  → heap check skipped
     - allowed_when_resources_are_low() == true  → 200-proposal cap skipped
     - minted_stake_e8s (500 ICP - i ICP fees) >= reject_cost_e8s (1 ICP)

3. After ~350–450 iterations the governance canister heap is exhausted.
   Subsequent state-modifying calls (voting, new proposals, reward distribution)
   trap with an out-of-memory error, freezing the NNS.
```

### Citations

**File:** rs/nns/governance/src/governance.rs (L250-255)
```rust
/// The max number of unsettled proposals -- that is proposals for which ballots
/// are still stored.
pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS: usize = 200;

/// The max number of open manage neuron proposals.
pub const MAX_NUMBER_OF_OPEN_MANAGE_NEURON_PROPOSALS: usize = 10_000;
```

**File:** rs/nns/governance/src/governance.rs (L260-268)
```rust
const MAX_HEAP_SIZE_IN_KIB: usize = 4 * 1024 * 1024;
const WASM32_PAGE_SIZE_IN_KIB: usize = 64;

/// Max number of wasm32 pages for the heap after which we consider that there
/// is a risk to the ability to grow the heap.
///
/// This is 7/8 of the maximum number of pages. This corresponds to 3.5 GiB.
pub const HEAP_SIZE_SOFT_LIMIT_IN_WASM32_PAGES: usize =
    MAX_HEAP_SIZE_IN_KIB / WASM32_PAGE_SIZE_IN_KIB * 7 / 8;
```

**File:** rs/nns/governance/src/governance.rs (L5143-5145)
```rust
        if !action.allowed_when_resources_are_low() {
            self.check_heap_can_grow()?;
        }
```

**File:** rs/nns/governance/src/governance.rs (L5254-5269)
```rust
            if self
                .heap_data
                .proposals
                .values()
                .filter(|info| !info.ballots.is_empty() && !info.is_manage_neuron())
                .count()
                >= MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS
                && !action.allowed_when_resources_are_low()
            {
                return Err(GovernanceError::new_with_message(
                    ErrorType::ResourceExhausted,
                    "Reached maximum number of proposals that have not yet \
                    been taken into account for voting rewards. \
                    Please try again later.",
                ));
            }
```

**File:** rs/nns/governance/src/governance.rs (L5351-5359)
```rust
        // Charge the proposal submission fee upfront.
        // This will protect from DOS in couple of ways:
        // - It prevents a neuron from having too many proposals outstanding.
        // - It reduces the voting power of the submitter so that for every proposal
        //   outstanding the submitter will have less voting power to get it approved.
        self.with_neuron_mut(proposer_id, |neuron| {
            neuron.neuron_fees_e8s += proposal_submission_fee;
        })
        .expect("Proposer not found.");
```

**File:** rs/nns/governance/src/proposals/install_code.rs (L146-151)
```rust
    pub fn allowed_when_resources_are_low(&self) -> bool {
        let Ok(canister_id) = self.valid_canister_id() else {
            return false;
        };
        topic_to_manage_canister(&canister_id) == Topic::ProtocolCanisterManagement
    }
```

**File:** rs/nns/governance/src/proposals/mod.rs (L195-208)
```rust
    pub fn allowed_when_resources_are_low(&self) -> bool {
        match self {
            ValidProposalAction::ExecuteNnsFunction(execute_nns_function) => {
                execute_nns_function.allowed_when_resources_are_low()
            }
            ValidProposalAction::InstallCode(install_code) => {
                install_code.allowed_when_resources_are_low()
            }
            ValidProposalAction::UpdateCanisterSettings(update_settings) => {
                update_settings.allowed_when_resources_are_low()
            }
            _ => false,
        }
    }
```
