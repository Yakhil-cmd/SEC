### Title
Missing Moratorium Guard in `donate_account` Allows Irreversible Neuron Transfer During Genesis Lockout Period — (`rs/nns/gtc/src/lib.rs`)

---

### Summary

`donate_account` permanently transfers a GTC account's genesis neurons to the custodian neuron but does not enforce the 3-day post-genesis moratorium that `claim_neurons` enforces. Any legitimate GTC account owner can call `donate_account` immediately after genesis, before the moratorium expires, causing an irreversible loss of their genesis ICP stake during a period the protocol explicitly intends to be locked.

---

### Finding Description

`claim_neurons` opens with a mandatory guard:

```rust
self.assert_claim_neurons_can_be_called()?;
``` [1](#0-0) 

That guard rejects any call made within 3 days of genesis:

```rust
fn assert_claim_neurons_can_be_called(&self) -> Result<(), String> {
    if now_secs() - self.genesis_timestamp_seconds < SECONDS_UNTIL_CLAIM_NEURONS_CAN_BE_CALLED {
        Err("claim_neurons cannot be called yet".to_string())
    } else {
        Ok(())
    }
}
``` [2](#0-1) 

`donate_account`, however, contains no such guard. Its entire body is:

```rust
pub async fn donate_account(
    &mut self,
    caller: &PrincipalId,
    public_key_hex: String,
) -> Result<(), String> {
    let public_key = decode_hex_public_key(&public_key_hex)?;
    validate_public_key_against_caller(&public_key, caller)?;
    ...
    account.transfer(custodian_neuron_id).await?;
    account.has_donated = true;
    Ok(())
}
``` [3](#0-2) 

`account.transfer` immediately calls `GovernanceCanister::transfer_gtc_neuron` for every neuron in the account, removing them from the GTC account permanently: [4](#0-3) 

Once `has_donated = true` is set, the account can never be claimed or forwarded. The transfer is irreversible.

---

### Impact Explanation

A GTC account owner who calls `donate_account` during the moratorium permanently forfeits their entire genesis ICP stake to the custodian neuron. There is no undo path: `transfer` sets `has_donated = true`, and `claim_neurons` subsequently rejects the account with `"Account has previously donated its funds"`. [5](#0-4) 

The moratorium constant is 3 days: [6](#0-5) 

---

### Likelihood Explanation

The attacker is any holder of a genesis token account — an unprivileged principal reachable via normal ingress. No admin key, governance majority, or privileged access is required. The window is the first 3 days after genesis, which is a known, finite, and historically real period. The call is straightforward and requires only the caller's own secp256k1 public key.

---

### Recommendation

Add the same moratorium check to `donate_account` that exists in `claim_neurons`:

```rust
pub async fn donate_account(...) -> Result<(), String> {
    self.assert_claim_neurons_can_be_called()?;  // add this line
    ...
}
```

Alternatively, introduce a dedicated `assert_account_actions_can_be_called` guard and apply it to both `donate_account` and `claim_neurons`.

---

### Proof of Concept

State-machine test outline:

1. Initialize the GTC canister with `genesis_timestamp_seconds = now_secs()` (moratorium active).
2. Call `donate_account` from a principal whose secp256k1 public key maps to a known GTC address.
3. **Expected (with fix):** call returns `Err("claim_neurons cannot be called yet")`.
4. **Actual (without fix):** call returns `Ok(())`, `has_donated` is set to `true`, and the neurons are transferred to the custodian — permanently and irreversibly — while the moratorium is still active.

### Citations

**File:** rs/nns/gtc/src/lib.rs (L21-21)
```rust
pub const SECONDS_UNTIL_CLAIM_NEURONS_CAN_BE_CALLED: u64 = 3 * 86400; // 3 days
```

**File:** rs/nns/gtc/src/lib.rs (L45-45)
```rust
        self.assert_claim_neurons_can_be_called()?;
```

**File:** rs/nns/gtc/src/lib.rs (L54-56)
```rust
        if account.has_donated {
            return Err("Account has previously donated its funds".to_string());
        }
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

**File:** rs/nns/gtc/src/lib.rs (L150-156)
```rust
    fn assert_claim_neurons_can_be_called(&self) -> Result<(), String> {
        if now_secs() - self.genesis_timestamp_seconds < SECONDS_UNTIL_CLAIM_NEURONS_CAN_BE_CALLED {
            Err("claim_neurons cannot be called yet".to_string())
        } else {
            Ok(())
        }
    }
```

**File:** rs/nns/gtc/src/lib.rs (L188-192)
```rust
        for neuron_id in neuron_ids {
            let result =
                GovernanceCanister::transfer_gtc_neuron(neuron_id, custodian_neuron_id).await;

            self.neuron_ids.retain(|id| id != &neuron_id);
```
