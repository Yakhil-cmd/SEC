### Title
EIP-7702 Chain-Agnostic Authorization Replay Across Chains Enables Unauthorized Account Delegation - (File: engine-transactions/src/eip_7702.rs)

### Summary

The Aurora Engine's EIP-7702 authorization validation explicitly accepts authorizations signed with `chain_id = 0` (chain-agnostic) as valid on Aurora. An attacker can take a chain-agnostic EIP-7702 authorization signed by a victim on any other EVM chain (e.g., Ethereum mainnet) and replay it on Aurora, installing arbitrary delegation code on the victim's Aurora account and draining their ETH balance.

### Finding Description

In `authorization_list()` inside `SignedTransaction7702`, the chain_id validation for each `AuthorizationTuple` is:

```rust
(auth.chain_id.is_zero() || auth.chain_id == current_tx_chain_id)
    && auth.parity <= U256::one()
``` [1](#0-0) 

When `auth.chain_id` is zero, the authorization passes chain validation unconditionally. The `current_tx_chain_id` used here is the outer transaction's `chain_id` field (`U256::from(self.transaction.chain_id)`), which is separately validated against Aurora's stored chain_id in `engine.rs`: [2](#0-1) 

This means the outer EIP-7702 transaction is correctly bound to Aurora's chain_id (1313161554), but the individual authorization tuples within it are not — any authorization signed with `chain_id = 0` on any EVM chain is accepted as valid on Aurora, as long as the authority's nonce matches.

The authorization hash is `keccak256(0x05 || rlp([chain_id, address, nonce]))`: [3](#0-2) 

When `chain_id = 0` is encoded in the hash, the resulting signature is identical regardless of which chain processes it. Aurora's own test utilities explicitly document and use this property: [4](#0-3) 

Once a valid authorization is processed, the EVM installs delegation code (`EF0100 || contract_address`) on the authority's account, as confirmed by the test suite: [5](#0-4) 

### Impact Explanation

An attacker who successfully replays a chain-agnostic authorization on Aurora installs arbitrary delegation code on the victim's account. Under EIP-7702, any subsequent call to the victim's address executes the delegated contract's code in the victim's storage and balance context. A malicious delegated contract can transfer all ETH from the victim's Aurora account to the attacker. This is **direct theft of user funds**.

The `NormalizedEthTransaction` conversion confirms the authorization list is passed directly into EVM execution: [6](#0-5) 

### Likelihood Explanation

The preconditions are:
1. The victim must have signed a chain-agnostic (`chain_id = 0`) EIP-7702 authorization on any EVM chain. This is a documented and supported use case (the Aurora test suite itself uses `chain_id = 0` for revocations).
2. The victim's Aurora account nonce must equal the nonce in the authorization. Fresh accounts (nonce = 0) are extremely common, and many Ethereum users who bridge to Aurora for the first time have nonce 0 on Aurora.
3. The attacker must deploy a malicious contract at the same address as the one in the authorization on Aurora. This is achievable via CREATE2 if the attacker controls the deployer, or by front-running deployment.

All three conditions are realistic and can be engineered by an attacker. The authorization is publicly visible on-chain once submitted on the origin chain.

### Recommendation

Aurora Engine should reject EIP-7702 authorization tuples with `chain_id = 0` when processing transactions. Since Aurora is a distinct chain with a fixed chain_id, there is no legitimate use case for accepting chain-agnostic authorizations — they provide no benefit over chain-specific ones and introduce cross-chain replay risk. The validation should be changed to:

```rust
auth.chain_id == current_tx_chain_id && auth.parity <= U256::one()
``` [1](#0-0) 

### Proof of Concept

1. Alice signs a chain-agnostic EIP-7702 authorization on Ethereum mainnet:
   - `chain_id = 0`, `address = <MaliciousContract>`, `nonce = 0`
   - This authorization is submitted in an Ethereum EIP-7702 transaction and is now public on-chain.

2. Attacker deploys `MaliciousContract` on Aurora at the same address (using CREATE2 with the same salt and deployer, or by being first to deploy). The contract contains logic to `SELFDESTRUCT` or transfer all ETH to the attacker.

3. Attacker constructs an Aurora EIP-7702 transaction:
   - Outer tx: `chain_id = 1313161554` (Aurora mainnet), any valid sender
   - `authorization_list = [{ chain_id: 0, address: MaliciousContract, nonce: 0, parity/r/s: Alice's signature }]`

4. Aurora's `authorization_list()` validates: `auth.chain_id.is_zero()` → `true` → authorization is accepted.

5. The EVM installs `EF0100 || MaliciousContract` as Alice's code on Aurora.

6. Attacker calls Alice's Aurora address. The EVM executes `MaliciousContract` in Alice's context, draining her ETH balance. [7](#0-6)

### Citations

**File:** engine-transactions/src/eip_7702.rs (L168-216)
```rust
        for auth in &self.transaction.authorization_list {
            // According to EIP-7702 step 1. validation, we should verify it as
            // `chain_id = 0 || current_chain_id`.
            // AS `current_chain_id` we used `transaction.chain_id` as we will validate `chain_id` in
            // Engine `submit_transaction` method.

            // Step 2 - validation logic inside EVM itself.
            // Step 3. Checking: authority = ecrecover(keccak(MAGIC || rlp([chain_id, address, nonce])), y_parity, r, s])
            // Validate the signature, as in tests it is possible to have invalid signature values.
            // Value `v` shouldn't be greater than 1
            let mut is_valid = if auth.s > SECP256K1N_HALF {
                false
            } else {
                (auth.chain_id.is_zero() || auth.chain_id == current_tx_chain_id)
                    && auth.parity <= U256::one()
            };

            let auth_address = if is_valid {
                rlp_stream.begin_list(3);
                rlp_stream.append(&auth.chain_id);
                rlp_stream.append(&auth.address);
                rlp_stream.append(&auth.nonce);

                message_bytes.extend_from_slice(rlp_stream.as_raw());

                let signature_hash = aurora_engine_sdk::keccak(&message_bytes);
                // U256::as_u32() is safe because here we're sure that the parity <= 1.
                let v = u8::try_from(auth.parity.as_u32()).unwrap_or(u8::MAX);
                let auth_address = ecrecover(signature_hash, &super::vrs_to_arr(v, auth.r, auth.s))
                    .unwrap_or_else(|_| {
                        is_valid = false;
                        Address::default()
                    });

                message_bytes.truncate(1);
                rlp_stream.clear();

                auth_address
            } else {
                Address::default()
            };

            // Validations steps 2,4-9 0f EIP-7702 provided by EVM itself.
            authorization_list.push(Authorization {
                authority: auth_address.raw(),
                address: auth.address,
                nonce: auth.nonce,
                is_valid,
            });
```

**File:** engine/src/engine.rs (L1054-1059)
```rust
    // Validate the chain ID, if provided inside the signature:
    if let Some(chain_id) = transaction.chain_id
        && U256::from(chain_id) != U256::from_big_endian(&state.chain_id)
    {
        return Err(EngineErrorKind::InvalidChainId.into());
    }
```

**File:** engine-tests/src/utils/mod.rs (L896-903)
```rust
/// Signs an EIP-7702 authorization list, returning a ready-to-use [`AuthorizationTuple`].
///
/// The signed message is `keccak256(0x05 || rlp([chain_id, address, nonce]))` per EIP-7702.
/// The resulting tuple can be included in `Transaction7702.authorization_list`.
///
/// ## Arguments
/// * `chain_id` — target chain id (0 for chain-agnostic authorization)
/// * `address` — contract address to delegate code execution to
```

**File:** engine-tests/src/tests/transaction.rs (L312-318)
```rust
    // ── Verify delegation was installed ──
    let expected_delegation_code = format!("ef0100{}", hex::encode(contract_address.as_bytes()));
    assert_eq!(
        hex::encode(runner.get_code(authority_address)),
        expected_delegation_code,
        "authority must have EF0100 delegation designator"
    );
```

**File:** engine-transactions/src/lib.rs (L145-157)
```rust
            Eip7702(tx) => Self {
                address: tx.sender()?,
                chain_id: Some(tx.transaction.chain_id),
                nonce: tx.transaction.nonce,
                gas_limit: tx.transaction.gas_limit,
                max_priority_fee_per_gas: tx.transaction.max_priority_fee_per_gas,
                max_fee_per_gas: tx.transaction.max_fee_per_gas,
                to: Some(tx.transaction.to),
                value: tx.transaction.value,
                data: tx.transaction.data.clone(),
                access_list: tx.transaction.access_list.clone(),
                authorization_list: tx.authorization_list()?,
            },
```
