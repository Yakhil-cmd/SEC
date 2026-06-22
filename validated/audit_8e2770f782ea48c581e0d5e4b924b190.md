### Title
Checks-Effects-Interactions Violation in `claim_neurons` Allows Duplicate Neuron Claiming - (File: rs/nns/gtc/src/lib.rs)

### Summary
The Genesis Token Canister (GTC) `claim_neurons` method sets the `has_claimed` flag **after** an inter-canister call to the Governance canister. On the Internet Computer, between the `await` point and the response, the canister can process other ingress messages. A caller can exploit this window to invoke `claim_neurons` a second time before `has_claimed = true` is committed, causing the Governance canister to transfer neuron ownership twice.

### Finding Description
In `rs/nns/gtc/src/lib.rs`, the `claim_neurons` method follows a broken checks-effects-interactions pattern:

```
// Line 62-64: guard check
if account.has_claimed {
    return Ok(account.neuron_ids.clone());
}

// Line 66: inter-canister call — await point
GovernanceCanister::claim_gtc_neurons(caller, account.neuron_ids.clone()).await?;

// Line 68: flag set AFTER the call
account.has_claimed = true;
``` [1](#0-0) 

The `has_claimed` flag is only set to `true` after the `await` on line 66 returns. On the IC, an `await` is a message boundary: the canister's execution is suspended and other ingress messages can be processed. A second concurrent call to `claim_neurons` from the same principal will pass the `has_claimed` check (still `false`) and issue another `claim_gtc_neurons` call to Governance.

The same pattern exists in `donate_account`, where `account.has_donated = true` is set after `account.transfer(custodian_neuron_id).await?` returns:

```rust
account.transfer(custodian_neuron_id).await?;
account.has_donated = true;
``` [2](#0-1) 

The `AccountState::transfer` method itself also loops over `neuron_ids` and issues one inter-canister call per neuron, with no in-progress lock set before the loop:

```rust
for neuron_id in neuron_ids {
    let result =
        GovernanceCanister::transfer_gtc_neuron(neuron_id, custodian_neuron_id).await;
    ...
}
``` [3](#0-2) 

The canister entry points are publicly callable update methods:

```rust
#[unsafe(export_name = "canister_update claim_neurons")]
fn claim_neurons() {
    over_async(candid_one, claim_neurons_)
}
``` [4](#0-3) 

### Impact Explanation
An attacker who owns a GTC account can send two concurrent `claim_neurons` ingress messages. Both will observe `has_claimed == false` before either sets it to `true`. Both will call `GovernanceCanister::claim_gtc_neurons`, which transfers neuron ownership to the caller's principal. The Governance `claim_gtc_neurons` method is synchronous and idempotent in the sense that it simply sets the controller — calling it twice does not cause a double-mint of tokens, but it does mean the GTC's internal accounting (`has_claimed`) is inconsistent with the actual state, and the second call succeeds when it should be rejected. More critically, for `donate_account`, a concurrent call can cause the same neurons to be transferred twice to the custodian neuron, potentially corrupting the Governance neuron state or causing a failed transfer that leaves the GTC in an inconsistent state (neurons partially removed from `neuron_ids` but `has_donated` not yet set). [5](#0-4) 

### Likelihood Explanation
The GTC is a live NNS canister. Any GTC account holder can send two concurrent ingress messages to `claim_neurons` or `donate_account`. The IC's message scheduling guarantees that between any two `await` points, other messages can be processed. This is a well-known IC reentrancy pattern. The attacker only needs to be the legitimate owner of a GTC account (i.e., hold the corresponding private key) — no privileged access is required.

### Recommendation
Set `has_claimed = true` (or `has_donated = true`) **before** the inter-canister `await` call, following the checks-effects-interactions pattern. If the downstream call fails, roll back the flag. Alternatively, introduce an in-progress lock (similar to the `balance_update_guard` used in ckBTC minter at `rs/bitcoin/ckbtc/minter/src/guard.rs`) that is set before the first `await` and cleared on completion. [6](#0-5) 

### Proof of Concept

1. Attacker holds a GTC account with `has_claimed = false` and N neuron IDs.
2. Attacker sends two concurrent ingress messages calling `claim_neurons(public_key_hex)` to the GTC canister.
3. Message A executes: passes the `has_claimed` check (false), issues `claim_gtc_neurons` to Governance, and suspends at `await`.
4. Before Message A's response arrives, Message B executes: also passes the `has_claimed` check (still false, because Message A has not yet set it), issues a second `claim_gtc_neurons` to Governance.
5. Both calls to Governance succeed; `claim_gtc_neurons` in Governance sets the neuron controller to the caller's principal for both invocations.
6. Message A resumes, sets `has_claimed = true`.
7. Message B resumes, sets `has_claimed = true` again (no-op).
8. Result: two successful `claim_gtc_neurons` calls were made when only one should have been permitted. The GTC's one-time-claim invariant is violated. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/gtc/src/lib.rs (L62-69)
```rust
        if account.has_claimed {
            return Ok(account.neuron_ids.clone());
        }

        GovernanceCanister::claim_gtc_neurons(caller, account.neuron_ids.clone()).await?;

        account.has_claimed = true;
        Ok(account.neuron_ids.clone())
```

**File:** rs/nns/gtc/src/lib.rs (L75-93)
```rust
    pub async fn donate_account(
        &mut self,
        caller: &PrincipalId,
        public_key_hex: String,
    ) -> Result<(), String> {
        let public_key = decode_hex_public_key(&public_key_hex)?;
        validate_public_key_against_caller(&public_key, caller)?;

        let custodian_neuron_id = self.donate_account_recipient_neuron_id;

        let address = public_key_to_gtc_address(&public_key);
        let account = self.get_account_mut(&address)?;
        account.authenticated_principal_id = Some(*caller);

        account.transfer(custodian_neuron_id).await?;
        account.has_donated = true;

        Ok(())
    }
```

**File:** rs/nns/gtc/src/lib.rs (L188-190)
```rust
        for neuron_id in neuron_ids {
            let result =
                GovernanceCanister::transfer_gtc_neuron(neuron_id, custodian_neuron_id).await;
```

**File:** rs/nns/gtc/canister/canister.rs (L144-148)
```rust
#[unsafe(export_name = "canister_update claim_neurons")]
fn claim_neurons() {
    println!("{LOG_PREFIX}claim_neurons");
    over_async(candid_one, claim_neurons_)
}
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L41-60)
```rust
impl<PR: PendingRequests> Guard<PR> {
    /// Attempts to create a new guard for the current block. Fails if there is
    /// already a pending request for the specified [principal] or if there
    /// are at least [MAX_CONCURRENT] pending requests.
    pub fn new(account: Account) -> Result<Self, GuardError> {
        mutate_state(|s| {
            let accounts = PR::pending_requests(s);
            if accounts.contains(&account) {
                return Err(GuardError::AlreadyProcessing);
            }
            if accounts.len() >= MAX_CONCURRENT {
                return Err(GuardError::TooManyConcurrentRequests);
            }
            accounts.insert(account);
            Ok(Self {
                account,
                _marker: PhantomData,
            })
        })
    }
```

**File:** rs/nns/governance/src/governance.rs (L1820-1855)
```rust
    pub fn claim_gtc_neurons(
        &mut self,
        caller: &PrincipalId,
        new_controller: PrincipalId,
        neuron_ids: Vec<NeuronId>,
    ) -> Result<(), GovernanceError> {
        if caller != GENESIS_TOKEN_CANISTER_ID.get_ref() {
            return Err(GovernanceError::new(ErrorType::NotAuthorized));
        }

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

        let now = self.env.now();
        for neuron_id in neuron_ids {
            self.with_neuron_mut(&neuron_id, |neuron| {
                neuron.created_timestamp_seconds = now;
                neuron.set_controller(new_controller)
            })
            .unwrap();
        }

        Ok(())
    }
```
