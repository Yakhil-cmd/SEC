### Title
Reentrancy in GTC `claim_neurons` Allows Duplicate Neuron Claims - (File: `rs/nns/gtc/src/lib.rs`)

### Summary
The `claim_neurons` function in the Genesis Token Canister (GTC) sets the `has_claimed` guard flag **after** an inter-canister call to the Governance canister, violating the Checks-Effects-Interactions pattern. On the Internet Computer, a canister suspends execution at every `await` point, allowing concurrent ingress messages to interleave. An attacker who owns a GTC account can send multiple concurrent `claim_neurons` ingress messages, all of which pass the `has_claimed` check before any of them sets it to `true`, causing `claim_gtc_neurons` to be dispatched to the Governance canister multiple times for the same account.

### Finding Description

In `rs/nns/gtc/src/lib.rs`, the `claim_neurons` method on `Gtc` performs the following sequence:

1. Checks `account.has_claimed` — returns early if already claimed.
2. Calls `GovernanceCanister::claim_gtc_neurons(caller, account.neuron_ids.clone()).await?` — an inter-canister call that suspends the GTC canister.
3. Sets `account.has_claimed = true` — **only after** the call returns. [1](#0-0) 

The canister entry point is the public `update` method `claim_neurons` in `rs/nns/gtc/canister/canister.rs`: [2](#0-1) 

Because IC canisters are single-threaded but yield at every `await`, multiple ingress messages queued before any response arrives will each execute up to the `await` point in turn. The sequence for two concurrent calls is:

```
Call A: checks has_claimed → false → dispatches claim_gtc_neurons → suspends
Call B: checks has_claimed → false (still!) → dispatches claim_gtc_neurons → suspends
Call A: resumes → sets has_claimed = true
Call B: resumes → sets has_claimed = true (no-op, but damage done)
```

Both calls have already dispatched `claim_gtc_neurons` to the Governance canister with the same `neuron_ids`. [3](#0-2) 

The `GovernanceCanister::claim_gtc_neurons` call is a real inter-canister call using `dfn_core::api::call`: [4](#0-3) 

### Impact Explanation

The GTC canister manages genesis-allocated neurons. `claim_gtc_neurons` in the Governance canister transfers ownership of those neurons to the caller's principal. If dispatched twice for the same account, the Governance canister processes two ownership-transfer requests for the same set of neuron IDs. Depending on Governance canister behavior, this can result in:

- The same neurons being claimed/transferred twice, potentially corrupting neuron ownership state in the Governance canister.
- The attacker gaining control of neurons they should only be able to claim once, or triggering unexpected state in the NNS Governance canister for those neuron IDs.

Since GTC neurons represent real ICP stake allocated at genesis, any duplication of claims is a ledger conservation / governance authorization bug with direct financial and governance impact on the NNS.

### Likelihood Explanation

The attack requires only that the attacker own a valid GTC account (i.e., possess the corresponding private key). They simply submit multiple concurrent ingress messages to the `claim_neurons` endpoint before any response is processed. This is trivially achievable by any GTC account holder using standard IC agent tooling. No privileged access, admin keys, or threshold corruption is required.

### Recommendation

Set `account.has_claimed = true` **before** the inter-canister call to `GovernanceCanister::claim_gtc_neurons`, following the Checks-Effects-Interactions pattern. If the governance call subsequently fails, the flag can be reset (or the error propagated and the flag left set, requiring a separate recovery path). The minimal fix:

```rust
// Set state BEFORE the external call
account.has_claimed = true;

// Now make the inter-canister call
GovernanceCanister::claim_gtc_neurons(caller, account.neuron_ids.clone()).await?;
// If this errors, has_claimed is already true — caller cannot retry,
// but neurons were never actually claimed. A recovery mechanism may be needed.
```

A more robust approach mirrors the `scopeguard` pattern used in the ckBTC minter: [5](#0-4) 

### Proof of Concept

1. Attacker holds a GTC account with neuron IDs `[N1, N2]` and `has_claimed = false`.
2. Attacker submits two ingress messages to `claim_neurons` on the GTC canister simultaneously (before either completes).
3. The IC scheduler executes Call A up to the `await` at line 66 of `rs/nns/gtc/src/lib.rs`, dispatching `claim_gtc_neurons([N1, N2])` to Governance, then suspends.
4. The IC scheduler executes Call B. `has_claimed` is still `false`. Call B also dispatches `claim_gtc_neurons([N1, N2])` to Governance, then suspends.
5. Both calls resume after Governance responds. Both set `has_claimed = true`.
6. The Governance canister has received two `claim_gtc_neurons` requests for the same neuron IDs, potentially processing both and corrupting neuron ownership state. [1](#0-0) [2](#0-1)

### Citations

**File:** rs/nns/gtc/src/lib.rs (L40-70)
```rust
    pub async fn claim_neurons(
        &mut self,
        caller: &PrincipalId,
        public_key_hex: String,
    ) -> Result<Vec<NeuronId>, String> {
        self.assert_claim_neurons_can_be_called()?;

        let public_key = decode_hex_public_key(&public_key_hex)?;
        validate_public_key_against_caller(&public_key, caller)?;

        let address = public_key_to_gtc_address(&public_key);
        let account = self.get_account_mut(&address)?;
        account.authenticated_principal_id = Some(*caller);

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
        Ok(account.neuron_ids.clone())
    }
```

**File:** rs/nns/gtc/src/lib.rs (L216-237)
```rust
    pub async fn claim_gtc_neurons(
        caller: &PrincipalId,
        neuron_ids: Vec<NeuronId>,
    ) -> Result<(), String> {
        let result: Result<Result<(), GovernanceError>, (Option<i32>, String)> = call(
            GOVERNANCE_CANISTER_ID,
            "claim_gtc_neurons",
            candid,
            (*caller, neuron_ids),
        )
        .await;

        let result = result.map_err(|(code, msg)| {
            format!(
                "Error calling method 'claim_gtc_neurons' of the Governance canister. Code: {code:?}. Message: {msg}"
            )
        })?;

        result.map_err(|e| {
            format!("Error returned by 'claim_gtc_neurons' of the Governance canister: {e:?}")
        })
    }
```

**File:** rs/nns/gtc/canister/canister.rs (L144-153)
```rust
#[unsafe(export_name = "canister_update claim_neurons")]
fn claim_neurons() {
    println!("{LOG_PREFIX}claim_neurons");
    over_async(candid_one, claim_neurons_)
}

#[candid_method(update, rename = "claim_neurons")]
async fn claim_neurons_(hex_pubkey: String) -> Result<Vec<NeuronId>, String> {
    gtc_mut().claim_neurons(&caller(), hex_pubkey).await
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L333-337)
```rust
        let guard = scopeguard::guard((utxo.clone(), caller_account), |(utxo, account)| {
            mutate_state(|s| {
                state::audit::mark_utxo_checked_mint_unknown(s, utxo, account, runtime)
            });
        });
```
