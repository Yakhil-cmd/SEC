### Title
SNS Governance `merge_maturity` Re-Entrancy During Async Ledger Call Enables Double-Minting - (`rs/sns/governance/src/governance.rs`)

### Summary
The SNS governance `merge_maturity` function reads a neuron's maturity before an async cross-canister ledger call and only decrements it after the call returns. Because the IC canister model allows other messages to be processed during an `await`, two concurrent `merge_maturity` calls for the same neuron can both read the same (not-yet-decremented) maturity, both mint tokens, and then both decrement the maturity — resulting in more tokens minted than maturity consumed.

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `merge_maturity` function:

1. Reads `neuron.maturity_e8s_equivalent` and computes `maturity_to_merge` synchronously.
2. Makes an async cross-canister call to the ledger (`self.ledger.transfer_funds(...).await`).
3. Only after the await returns does it re-fetch the neuron and call `saturating_sub(maturity_to_merge)`. [1](#0-0) [2](#0-1) [3](#0-2) 

There is no neuron-level lock acquired before the async call. During the `await` at the ledger transfer, the canister's message queue is unblocked and a second concurrent `merge_maturity` call for the same neuron can be processed. That second call reads the same `maturity_e8s_equivalent` (not yet decremented by the first call) and initiates its own minting transfer.

When both calls resume:
- Call 1 decrements maturity by `M` → maturity becomes `0`
- Call 2 decrements maturity by `M` via `saturating_sub` → maturity stays `0` (no underflow panic)
- But both calls have already minted `M` tokens each → **2M tokens minted for M maturity consumed**

This is structurally identical to the reported vulnerability: state is read before an external call, the external call creates a re-entrancy window, and the post-call check (here: `saturating_sub`) does not prevent the over-issuance because the tokens are already minted.

### Impact Explanation

An SNS neuron controller can double-mint (or N×-mint) SNS tokens by submitting concurrent `merge_maturity` calls. This violates the ledger conservation invariant: total SNS token supply increases by more than the maturity consumed. The attacker gains free SNS tokens at the expense of the SNS treasury's backing ratio, diluting all other token holders.

### Likelihood Explanation

Any principal that controls an SNS neuron with non-zero maturity can trigger this. No privileged role, governance majority, or key compromise is required. The attacker simply submits two `merge_maturity` ingress messages in the same IC round (or in rapid succession). The IC's asynchronous execution model guarantees the window exists at every `await` point without a guard.

### Recommendation

Acquire a per-neuron in-flight lock (analogous to NNS governance's `neuron_in_flight_command`) **before** computing `maturity_to_merge` and **release it only after** the maturity has been decremented. Alternatively, decrement the maturity optimistically before the async call and restore it on failure — the checks-effects-interactions pattern applied to IC async calls.

### Proof of Concept

1. Attacker controls SNS neuron `N` with `maturity_e8s_equivalent = 1_000_000`.
2. Attacker submits two ingress `manage_neuron { merge_maturity { percentage_to_merge: 100 } }` calls targeting neuron `N` in the same IC round.
3. The governance canister processes Call 1 synchronously up to the `transfer_funds(...).await` at line 1507, then suspends.
4. The governance canister processes Call 2 synchronously up to the same `await`. Both calls have captured `maturity_to_merge = 1_000_000`.
5. Both ledger minting transfers succeed; the SNS ledger credits the neuron's subaccount with `1_000_000` tokens twice.
6. Call 1 resumes: `neuron.maturity_e8s_equivalent = 1_000_000 - 1_000_000 = 0`.
7. Call 2 resumes: `neuron.maturity_e8s_equivalent = saturating_sub(0, 1_000_000) = 0`.
8. Net result: `2_000_000` SNS tokens minted, `1_000_000` maturity consumed. Ledger conservation violated. [4](#0-3)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1453-1527)
```rust
    pub async fn merge_maturity(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        merge_maturity: &manage_neuron::MergeMaturity,
    ) -> Result<MergeMaturityResponse, GovernanceError> {
        let now = self.env.now();

        let neuron = self.get_neuron_result(id)?.clone();

        neuron.check_authorized(caller, NeuronPermissionType::MergeMaturity)?;

        if merge_maturity.percentage_to_merge > 100 || merge_maturity.percentage_to_merge == 0 {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "The percentage of maturity to merge must be a value between 1 and 100 (inclusive).",
            ));
        }

        let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();

        let mut maturity_to_merge =
            (neuron.maturity_e8s_equivalent * merge_maturity.percentage_to_merge as u64) / 100;

        // Converting u64 to f64 can cause the u64 to be "rounded up", so we
        // need to account for this possibility.
        if maturity_to_merge > neuron.maturity_e8s_equivalent {
            maturity_to_merge = neuron.maturity_e8s_equivalent;
        }

        if maturity_to_merge <= transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Tried to merge {maturity_to_merge} e8s, but can't merge an amount less than the transaction fee of {transaction_fee_e8s} e8s"
                ),
            ));
        }

        let nid = neuron.id.as_ref().expect("Neurons must have an id");
        let subaccount = neuron.subaccount()?;

        // Do the transfer, this is a minting transfer, from the governance canister's
        // (which is also the minting canister) main account into the neuron's
        // subaccount.
        #[rustfmt::skip]
        let _block_height: u64 = self
            .ledger
            .transfer_funds(
                maturity_to_merge,
                0, // Minting transfer don't pay a fee
                None, // This is a minting transfer, no 'from' account is needed
                self.neuron_account_id(subaccount), // The account of the neuron on the ledger
                self.env.insecure_random_u64(), // Random memo(nonce) for the ledger's transaction
            )
            .await?;

        // Adjust the maturity, stake, and age of the neuron
        let neuron = self
            .get_neuron_result_mut(nid)
            .expect("Expected the neuron to exist");

        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_merge);
        let new_stake = neuron
            .cached_neuron_stake_e8s
            .saturating_add(maturity_to_merge);
        neuron.update_stake(new_stake, now);
        let new_stake_e8s = neuron.cached_neuron_stake_e8s;

        Ok(MergeMaturityResponse {
            merged_maturity_e8s: maturity_to_merge,
            new_stake_e8s,
        })
```
