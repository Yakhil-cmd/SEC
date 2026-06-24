### Title
Inconsistent Address Key Format in GTC Canister Causes Permanent Denial of Neuron Claims - (File: rs/nns/gtc/src/lib.rs)

### Summary
The Genesis Token Canister (GTC) derives Ethereum-style account addresses using `public_key_to_gtc_address`, which always produces **lowercase hex without a `0x` prefix** via `hex::encode`. These derived addresses are used as keys to look up entries in the `accounts` `HashMap<String, AccountState>`. The `accounts` map is populated at genesis initialization from externally supplied string addresses with no normalization or validation of case or prefix format. If the genesis initialization data supplies addresses in any other format (e.g., EIP-55 mixed-case checksum, or with a `0x` prefix), the lookup in `claim_neurons` and `donate_account` will permanently fail with "Account not found" for every affected account, making it impossible for legitimate genesis token holders to ever claim their neurons.

### Finding Description

`public_key_to_gtc_address` in `rs/nns/gtc/src/lib.rs` computes the GTC address from a caller-supplied public key:

```rust
fn public_key_to_gtc_address(public_key: &PublicKey) -> String {
    let mut hasher = Keccak256::new();
    hasher.update(&public_key.serialize_sec1(false)[1..]);
    let address_bytes = &hasher.finalize()[12..];
    hex::encode::<&[u8]>(address_bytes)  // always lowercase, no 0x prefix
}
``` [1](#0-0) 

This lowercase-no-prefix string is then used as the key to look up the account in `self.accounts`:

```rust
fn get_account_mut(&mut self, address: &str) -> Result<&mut AccountState, String> {
    self.accounts
        .get_mut(address)
        .ok_or_else(|| "Account not found".to_string())
}
``` [2](#0-1) 

Both `claim_neurons` and `donate_account` call `public_key_to_gtc_address` and then `get_account_mut`: [3](#0-2) [4](#0-3) 

The `accounts` map is a `HashMap<String, AccountState>` populated at genesis initialization: [5](#0-4) 

The initialization builder (`GenesisTokenCanisterInitPayloadBuilder`) accepts raw `&str` addresses with no normalization: [6](#0-5) 

The `accounts` map is built directly from these raw strings: [7](#0-6) 

There is no normalization (lowercase, strip `0x`) applied to addresses at initialization time, nor any validation that the stored key format matches what `public_key_to_gtc_address` will produce at claim time. The same inconsistency affects `forward_whitelisted_unclaimed_accounts`, where `whitelisted_accounts_to_forward` strings are compared against `accounts` map keys: [8](#0-7) 

### Impact Explanation

If any genesis account address was stored with a `0x` prefix, uppercase letters, or EIP-55 mixed-case checksum format (all common Ethereum address representations), the `claim_neurons` and `donate_account` calls for those accounts will always return `"Account not found"`. The affected genesis token holders permanently lose the ability to claim or donate their neurons — the neurons remain locked in the GTC forever. Similarly, if `whitelisted_accounts_to_forward` entries use a different case than the `accounts` map keys, the forwarding logic silently skips those accounts.

### Likelihood Explanation

The GTC was initialized at genesis with a fixed dataset. The test constants confirm the expected format is lowercase-no-prefix (e.g., `"bdf51dc6fbb698be9c2ce5a6e91ada4d987cd5f0"`), and the mainnet GTC appears to have been initialized consistently. However, the code contains **no enforcement** of this format contract — no normalization, no validation, no compile-time or runtime check. Any future re-initialization, upgrade, or tooling that supplies addresses in standard Ethereum format (`0x`-prefixed or EIP-55 mixed-case) would silently break all claims. The attacker-controlled entry path is the ingress `claim_neurons` call: a legitimate user submitting their valid public key will receive "Account not found" if their address was stored in a mismatched format.

### Recommendation

Normalize all address strings to a canonical form (lowercase, no `0x` prefix) both at initialization time (in `GenesisTokenCanisterInitPayloadBuilder::add_sr_neurons` / `add_ect_neurons`) and at lookup time (in `get_account_mut` / `get_account`). Add a validation step in `canister_init` / `canister_post_upgrade` that asserts all keys in `accounts` and all entries in `whitelisted_accounts_to_forward` conform to the canonical format produced by `public_key_to_gtc_address`.

### Proof of Concept

1. Initialize the GTC with an account address in EIP-55 format: `"0xBdF51DC6FBb698bE9C2CE5A6E91AdA4D987CD5F0"`.
2. The `accounts` map now contains the key `"0xBdF51DC6FBb698bE9C2CE5A6E91AdA4D987CD5F0"`.
3. The legitimate owner calls `claim_neurons` with their valid secp256k1 public key.
4. `public_key_to_gtc_address` derives `"bdf51dc6fbb698be9c2ce5a6e91ada4d987cd5f0"` (lowercase, no prefix).
5. `self.accounts.get_mut("bdf51dc6fbb698be9c2ce5a6e91ada4d987cd5f0")` returns `None`.
6. `claim_neurons` returns `Err("Account not found")` — the user can never claim their genesis neurons. [9](#0-8) [1](#0-0)

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

**File:** rs/nns/gtc/src/lib.rs (L85-86)
```rust
        let address = public_key_to_gtc_address(&public_key);
        let account = self.get_account_mut(&address)?;
```

**File:** rs/nns/gtc/src/lib.rs (L104-116)
```rust
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
```

**File:** rs/nns/gtc/src/lib.rs (L135-139)
```rust
    fn get_account_mut(&mut self, address: &str) -> Result<&mut AccountState, String> {
        self.accounts
            .get_mut(address)
            .ok_or_else(|| "Account not found".to_string())
    }
```

**File:** rs/nns/gtc/src/lib.rs (L290-295)
```rust
fn public_key_to_gtc_address(public_key: &PublicKey) -> String {
    let mut hasher = Keccak256::new();
    hasher.update(&public_key.serialize_sec1(false)[1..]);
    let address_bytes = &hasher.finalize()[12..];
    hex::encode::<&[u8]>(address_bytes)
}
```

**File:** rs/nns/gtc/src/gen/ic_nns_gtc.pb.v1.rs (L6-7)
```rust
    #[prost(map = "string, message", tag = "1")]
    pub accounts: ::std::collections::HashMap<::prost::alloc::string::String, AccountState>,
```

**File:** rs/nns/test_utils/src/gtc_helpers.rs (L72-85)
```rust
        for (address, icpts) in sr_accounts.iter() {
            self.total_alloc += *icpts;
            let icpts = Tokens::from_tokens(*icpts as u64).unwrap();
            let sr_stakes = evenly_split_e8s(icpts.get_e8s(), sr_months_to_release);
            let aging_since_timestamp_seconds = self.aging_since_timestamp_seconds;
            let mut sr_neurons = make_neurons(
                address,
                INVESTOR_TYPE_SR,
                sr_stakes,
                self.get_rng(None),
                aging_since_timestamp_seconds,
            );
            let entry = self.gtc_neurons.entry(address.to_string()).or_default();
            entry.append(&mut sr_neurons);
```

**File:** rs/nns/test_utils/src/gtc_helpers.rs (L163-179)
```rust
    pub fn build(&mut self) -> Gtc {
        let accounts = self
            .gtc_neurons
            .iter()
            .map(|(address, neurons)| (address.clone(), account_state_from(neurons)))
            .collect();

        Gtc {
            accounts,
            total_alloc: self.total_alloc,
            genesis_timestamp_seconds: self.genesis_timestamp_seconds,
            donate_account_recipient_neuron_id: self.donate_account_recipient_neuron_id,
            forward_whitelisted_unclaimed_accounts_recipient_neuron_id: self
                .forward_whitelisted_unclaimed_accounts_recipient_neuron_id,
            whitelisted_accounts_to_forward: self.forward_unclaimed_accounts_whitelist.clone(),
        }
    }
```
