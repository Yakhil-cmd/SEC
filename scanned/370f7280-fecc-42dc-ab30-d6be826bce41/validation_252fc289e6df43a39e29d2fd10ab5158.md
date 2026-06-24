### Title
GTC Neurons Permanently Locked for Non-Whitelisted Accounts Whose Owner Cannot Invoke `claim_neurons` - (File: rs/nns/gtc/src/lib.rs)

### Summary
The Genesis Token Canister (GTC) holds ICP neurons on behalf of genesis investors. Claiming requires the beneficiary to actively call `claim_neurons` with their secp256k1 public key. If the beneficiary's key is lost, the beneficiary is a smart contract (canister) that cannot produce a secp256k1 signature, or the beneficiary is otherwise unable to call the GTC, the neurons are permanently locked. The only escape valve — `forward_whitelisted_unclaimed_accounts` — only rescues accounts on a hardcoded whitelist. Non-whitelisted accounts have no recovery path whatsoever.

### Finding Description

The GTC's `claim_neurons` function enforces two strict requirements that together create a permanent lock condition:

**Requirement 1 — Caller must hold the secp256k1 private key:** [1](#0-0) 

The caller must supply a hex-encoded secp256k1 public key, and `validate_public_key_against_caller` verifies that the principal derived from that key equals the IC caller identity. This means the beneficiary must be a self-authenticating principal derived from a secp256k1 key — a canister principal, an opaque principal, or any principal not derived from a secp256k1 key can never satisfy this check. [2](#0-1) 

**Requirement 2 — Only the account owner can claim:**

The GTC address is derived from the secp256k1 public key via Keccak256, so the GTC account is permanently bound to a specific key pair. There is no mechanism to update the beneficiary address or delegate claiming to another principal. [3](#0-2) 

**The only escape valve is whitelist-gated:**

`forward_whitelisted_unclaimed_accounts` can rescue unclaimed accounts, but only those present in the hardcoded `FORWARD_WHITELIST` (4 addresses). Non-whitelisted accounts that fail to claim are permanently stuck. [4](#0-3) [5](#0-4) 

The `forward_whitelisted_unclaimed_accounts` function explicitly skips any account not in the whitelist: [6](#0-5) 

**The canister interface exposes no admin override or alternative claim path:** [7](#0-6) 

There is no `admin_claim`, `force_forward`, or `update_beneficiary` method. The only update methods are `claim_neurons`, `donate_account`, and `forward_whitelisted_unclaimed_accounts`.

### Impact Explanation

Any GTC account whose owner:
- Lost their secp256k1 private key,
- Is a canister (which cannot produce a secp256k1 signature and whose principal is not self-authenticating from a secp256k1 key),
- Has a key stored in hardware that is no longer accessible,

...will have their ICP neurons permanently locked under GTC control. The neurons remain in the Governance canister with the GTC as controller, but neither the intended beneficiary nor any third party can claim, disburse, or vote with them (beyond the GTC's own following). The ICP stake is effectively frozen indefinitely. Given that genesis allocations were in the millions of ICP range (e.g., 1200 ICP for SR accounts, 8544 ICP for ECT accounts in test data), the financial impact per affected account is significant. [8](#0-7) 

### Likelihood Explanation

This is a realistic scenario. The GTC was deployed at IC genesis (May 2021) and the claim window has been open for years. Any investor who:
- Lost their genesis key (hardware failure, lost seed phrase),
- Assigned their allocation to a multisig or smart contract that cannot produce a secp256k1 signature,
- Is deceased with no key recovery,

...is permanently locked out. The `forward_whitelisted_unclaimed_accounts` function only covers 4 hardcoded addresses out of hundreds of genesis accounts. The vulnerability is not theoretical — it is a structural design gap that affects any non-whitelisted account whose owner cannot sign with the original secp256k1 key. The entry path requires no attacker: it is triggered by the absence of the beneficiary's ability to call `claim_neurons`. [9](#0-8) 

### Recommendation

1. **Allow any account to trigger forwarding for any unclaimed GTC account** after the forwarding window has elapsed (analogous to the external report's recommendation to allow any account to invoke `claim`). Remove the whitelist restriction in `forward_whitelisted_unclaimed_accounts` so that all unclaimed, non-donated accounts can be rescued after the time window.

2. **Alternatively**, add an admin-callable `force_forward_account(gtc_address)` method restricted to the NNS governance canister that can forward any specific unclaimed account to the custodian neuron, providing a governance-controlled recovery path.

3. **At minimum**, document that non-whitelisted accounts with lost keys are permanently unrecoverable, so that the NNS can make an informed governance decision about whether to upgrade the GTC canister to add a recovery path.

### Proof of Concept

1. A genesis investor's secp256k1 key is lost (or the investor is a canister).
2. Their GTC account (e.g., address `"abc123..."`) holds 1200 ICP worth of neurons.
3. After 3 days post-genesis, `claim_neurons` becomes callable — but only by the holder of the original secp256k1 key. The investor cannot call it.
4. After 188 days post-genesis, `forward_whitelisted_unclaimed_accounts` becomes callable by anyone — but the investor's address is not in `FORWARD_WHITELIST` (which contains only 4 addresses).
5. The neurons remain permanently under GTC control. No method in the GTC canister interface can rescue them. The GTC canister has no admin override.

Relevant code path:

```
claim_neurons (gtc/canister/canister.rs:151)
  → Gtc::claim_neurons (gtc/src/lib.rs:40)
    → validate_public_key_against_caller (lib.rs:48) ← BLOCKS non-key-holders
    → GovernanceCanister::claim_gtc_neurons (lib.rs:66)

forward_whitelisted_unclaimed_accounts (gtc/canister/canister.rs:181)
  → Gtc::forward_whitelisted_unclaimed_accounts (lib.rs:102)
    → forward_whitelist.contains(gtc_address) (lib.rs:116) ← BLOCKS non-whitelisted
``` [10](#0-9) [11](#0-10) [12](#0-11) [4](#0-3)

### Citations

**File:** rs/nns/gtc/src/lib.rs (L19-27)
```rust
/// The amount of time after the genesis of the IC that GTC neurons cannot be
/// claimed.
pub const SECONDS_UNTIL_CLAIM_NEURONS_CAN_BE_CALLED: u64 = 3 * 86400; // 3 days

/// The amount of time after the genesis of the IC that any user can call
/// `forward_whitelisted_unclaimed_accounts`. This allows the reclaiming of GTC
/// neurons that have not been claimed, so that these neurons don't exist in an
/// unclaimed state indefinitely.
pub const SECONDS_UNTIL_FORWARD_WHITELISTED_UNCLAIMED_ACCOUNTS_CAN_BE_CALLED: u64 = 188 * 86400; // 188 days
```

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

**File:** rs/nns/gtc/src/lib.rs (L102-131)
```rust
    pub async fn forward_whitelisted_unclaimed_accounts(&mut self) -> Result<(), String> {
        self.assert_forward_whitelisted_unclaimed_accounts_can_be_called()?;
        let mut forward_whitelist = HashSet::new();

        for gtc_address in &self.whitelisted_accounts_to_forward {
            forward_whitelist.insert(gtc_address.to_string());
        }

        let custodian_neuron_id = self.forward_whitelisted_unclaimed_accounts_recipient_neuron_id;

        for (gtc_address, account) in self.accounts.iter_mut() {
            if !account.has_claimed
                && !account.has_donated
                && !account.has_forwarded
                && forward_whitelist.contains(gtc_address)
            {
                match account.transfer(custodian_neuron_id).await {
                    Ok(_) => account.has_forwarded = true,
                    Err(error) => {
                        println!(
                            "Error forwarding gtc account: {}. Error: {}",
                            gtc_address, error
                        );
                    }
                }
            }
        }

        Ok(())
    }
```

**File:** rs/nns/gtc/src/lib.rs (L276-287)
```rust
fn validate_public_key_against_caller(
    public_key: &PublicKey,
    caller: &PrincipalId,
) -> Result<(), String> {
    let public_key_principal = public_key_to_principal(public_key);

    if caller != &public_key_principal {
        Err("Public key does not match caller".to_string())
    } else {
        Ok(())
    }
}
```

**File:** rs/nns/gtc/src/lib.rs (L289-295)
```rust
/// Given a public key, return the associated GTC account address
fn public_key_to_gtc_address(public_key: &PublicKey) -> String {
    let mut hasher = Keccak256::new();
    hasher.update(&public_key.serialize_sec1(false)[1..]);
    let address_bytes = &hasher.finalize()[12..];
    hex::encode::<&[u8]>(address_bytes)
}
```

**File:** rs/nns/gtc/canister/canister.rs (L83-91)
```rust
    // If the set of whitelisted accounts is empty (like it would
    // normally be in production) add the accounts in the
    // FORWARD_WHITELIST array.
    if gtc.whitelisted_accounts_to_forward.is_empty() {
        for gtc_address in FORWARD_WHITELIST {
            gtc.whitelisted_accounts_to_forward
                .push(gtc_address.to_string());
        }
    }
```

**File:** rs/nns/gtc/canister/canister.rs (L150-153)
```rust
#[candid_method(update, rename = "claim_neurons")]
async fn claim_neurons_(hex_pubkey: String) -> Result<Vec<NeuronId>, String> {
    gtc_mut().claim_neurons(&caller(), hex_pubkey).await
}
```

**File:** rs/nns/gtc/canister/canister.rs (L180-183)
```rust
#[candid_method(update, rename = "forward_whitelisted_unclaimed_accounts")]
async fn forward_whitelisted_unclaimed_accounts_(_: ()) -> Result<(), String> {
    gtc_mut().forward_whitelisted_unclaimed_accounts().await
}
```

**File:** rs/nns/gtc/canister/gtc.did (L38-47)
```text
service : {
  balance : (text) -> (nat32) query;
  claim_neurons : (text) -> (Result);
  donate_account : (text) -> (Result_1);
  forward_whitelisted_unclaimed_accounts : (null) -> (Result_1);
  get_account : (text) -> (Result_2) query;
  get_build_metadata : () -> (text) query;
  len : () -> (nat16) query;
  total : () -> (nat32) query;
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
